"""One Codex CLI invocation, using the Kernel's allocated process lifecycle.

JSONL carries execution facts. The fixed, owned final-message file carries the
role return. Neither this module nor its capture interprets an Artifact.
"""

from __future__ import annotations

import codecs
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from agent_run.agent_schemas import (
    acceptance_schema, development_or_human_blocker_schema, publication_or_human_blocker_schema,
)
from agent_run.error_safety import bounded_error
from agent_run.worker_sandbox import _BoundedJsonlStream, bubblewrap_command, native_worker_environment

from .execution import ProcessExit, _execute
from .model import AgentCall, Role

if TYPE_CHECKING:
    from .kernel import Kernel


_MAX_FINAL_BYTES = 1024 * 1024
_MAX_CREDENTIAL_BYTES = 64 * 1024
_MAX_STDOUT_BYTES = 64 * 1024 * 1024
_GITHUB_TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def _safe_message(value: str) -> tuple[str, bool]:
    """Keep the existing API error projection before credential redaction."""
    projected = value
    decoder = json.JSONDecoder()
    for offset, character in enumerate(value):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(value, offset)
        except ValueError:
            continue
        if not isinstance(candidate, dict):
            continue
        nested = candidate.get("error")
        sources = (nested, candidate) if isinstance(nested, dict) else (candidate,)
        fields: dict[str, object] = {}
        for field in ("type", "code", "status", "message", "param"):
            for source in sources:
                item = source.get(field)
                if field in source and (item is None or isinstance(item, (str, bool, int))
                                        or isinstance(item, float) and math.isfinite(item)):
                    fields[field] = item
                    break
        if fields:
            projected = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
            break
    safe = bounded_error(projected)
    return safe, safe != value


def _read_regular(descriptor: int, limit: int) -> bytes:
    before = os.fstat(descriptor)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_nlink != 1 or before.st_size > limit):
        raise ValueError("Worker file is not an owned, bounded regular file")
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        value = stream.read(limit + 1)
    after = os.fstat(descriptor)
    if (len(value) > limit or len(value) != before.st_size
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError("Worker file changed while it was read")
    return value


def _write_member(directory: int, name: str, value: bytes) -> None:
    descriptor = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)


class _CodexCapture:
    """A concrete owned-file/JSONL capture, not a backend or recovery policy."""

    def __init__(
        self, kernel: Kernel, call: AgentCall, *, executable: str,
        environment: Mapping[str, str], credential_file: Path,
        hidden_paths: Sequence[Path], hidden_environment: Sequence[str],
    ) -> None:
        self.kernel, self.call, self.executable = kernel, call, executable
        self.source_environment = dict(environment)
        self.credential_file = credential_file
        self.hidden_paths, self.hidden_environment = tuple(hidden_paths), tuple(hidden_environment)
        self.cwd = Path(call.context["checkout"])
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.resources = ExitStack()
        self.directory: int | None = None
        self.thread = call.reported_thread
        self.observed_thread: str | None = None
        self.invalid = False
        self.stdout_bytes = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self.stream = _BoundedJsonlStream(on_line=self._line)
        self.sequence = 0
        self.failure_kind = "ordinary"
        self.diagnostic: dict[str, Any] | None = None

    def __enter__(self) -> Self:
        try:
            temporary = Path(self.resources.enter_context(tempfile.TemporaryDirectory(prefix="agent-run-kernel-codex-")))
            temporary.chmod(0o700)
            self.directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.resources.callback(os.close, self.directory)
            source = os.open(self.credential_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                credential = _read_regular(source, _MAX_CREDENTIAL_BYTES)
            finally:
                os.close(source)
            if not credential.strip():
                raise ValueError("Selected Worker credential file is empty")
            gh = temporary / "gh"
            gh.mkdir(mode=0o700)
            gh_descriptor = os.open(gh, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _write_member(gh_descriptor, "hosts.yml", credential)
            finally:
                os.close(gh_descriptor)
            schemas = {
                Role.DEVELOPMENT: development_or_human_blocker_schema,
                Role.REVIEW: acceptance_schema,
                Role.PUBLICATION: publication_or_human_blocker_schema,
            }
            schema = json.dumps(schemas[self.call.role](), separators=(",", ":")).encode()
            if len(schema) > 64 * 1024:
                raise ValueError("Worker output schema exceeds its file limit")
            _write_member(self.directory, "schema.json", schema)
            _write_member(self.directory, "last-message.txt", b"")
            self.environment = native_worker_environment(self.source_environment, gh, host=False)
            for variable in (*_GITHUB_TOKEN_VARIABLES, *self.hidden_environment):
                self.environment.pop(variable, None)
            self.environment["GH_PROMPT_DISABLED"] = "1"
            self.environment["GIT_TERMINAL_PROMPT"] = "0"
            readonly = self.call.context["readonly"]
            if type(readonly) is not bool:
                raise ValueError("Allocated checkout access must be explicit")
            if self.call.mode == "output-repair" and not readonly:
                raise ValueError("Output repair requires an allocated read-only checkout")
            command = self._command(temporary)
            self.arguments = bubblewrap_command(
                command, checkout=self.cwd, temporary=temporary, writable_checkout=not readonly,
                environment=self.environment, hidden_paths=self._protected_paths(), inherit_host_auth=False,
            )
            return self
        except BaseException:
            self.resources.close()
            raise

    def __exit__(
        self, exception_type: type[BaseException] | None, exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.resources.close()

    def _command(self, temporary: Path) -> list[str]:
        if self.call.mode == "fresh":
            if self.call.reported_thread is not None:
                raise ValueError("Fresh invocation already has a backend Thread")
            command = [self.executable, "exec", "--json", "--color", "never",
                       "--dangerously-bypass-approvals-and-sandbox", "--cd", str(self.cwd)]
        elif self.call.mode in {"resume", "output-repair", "execution-recovery"}:
            if not self.call.reported_thread:
                raise ValueError("Continuation requires its original backend Thread")
            command = [self.executable, "exec", "resume", "--json",
                       "--dangerously-bypass-approvals-and-sandbox", self.call.reported_thread]
        else:
            raise ValueError("Unknown allocated Codex invocation mode")
        command.extend(["--model", self.call.profile.model, "--config",
                        f'model_reasoning_effort="{self.call.profile.effort}"',
                        "--output-schema", str(temporary / "schema.json"),
                        "--output-last-message", str(temporary / "last-message.txt"), "-"])
        return command

    def _protected_paths(self) -> tuple[Path, ...]:
        state = self.kernel.storage.path.resolve()
        if state.parent == self.cwd.resolve():
            raise ValueError("Kernel state needs a directory separate from the Worker checkout")
        paths = [state.parent, state, Path(str(state) + "-wal"), Path(str(state) + "-shm"),
                 self.credential_file, *self.hidden_paths]
        environment = self.source_environment
        home = Path(environment.get("HOME", str(Path.home())))
        paths.extend((home / ".config" / "gh", home / ".ssh", home / ".git-credentials"))
        paths.extend((Path(environment.get("XDG_DATA_HOME", str(home / ".local" / "share"))) / "agent-run",
                      Path(environment.get("XDG_CONFIG_HOME", str(home / ".config"))) / "agent-run"))
        runtime = Path(environment.get("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}"))
        paths.extend((runtime / "bus", runtime / "systemd" / "private", runtime / "agent-run",
                      Path("/tmp") / f"agent-run-{os.geteuid()}"))
        for root in {Path("/run"), Path(f"/run/user/{os.geteuid()}"), runtime}:
            paths.extend((root / "credentials", root / "systemd" / "temporary-credentials"))
        for key in ("AGENT_RUN_INTERNAL_STATE_ROOT", "AGENT_RUN_INTERNAL_SYSTEMD_RUNTIME_DIRECTORY",
                    "AGENT_RUN_INTERNAL_CREDENTIALS_DIRECTORY", "CREDENTIALS_DIRECTORY",
                    "AGENT_RUN_GITHUB_APP_PRIVATE_KEY"):
            if environment.get(key):
                paths.append(Path(environment[key]))
        for parent in self.cwd.resolve().parents:
            if parent.name == "worktrees" and parent.parent.name == ".agent-run":
                paths.append(parent.parent)
        paths.append(self.cwd / ".agent-run")
        return tuple(paths)

    def _line(self, line: str) -> None:
        try:
            event: Any = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        self.sequence += 1
        event_type = event.get("type")
        if event_type in {"turn.started", "turn.completed", "turn.failed"}:
            self.kernel.observe_worker_activity(
                self.call, str(self.sequence), active=event_type == "turn.started",
                observed_at=self.kernel.clock(),
                evidence={"source": "codex-jsonl", "type": event_type, "sequence": self.sequence},
            )
        if event_type in {"turn.failed", "task_complete"}:
            error = event.get("error")
            machine_present = isinstance(error, dict) and "codex_error_info" in error
            machine = error.get("codex_error_info") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else error
            capacity = (machine == "server_overloaded" if machine_present else
                        message == "Selected model is at capacity. Please try a different model.")
            self.failure_kind = "capacity" if capacity else "ordinary"
            safe_message, changed = _safe_message(message if isinstance(message, str) else "")
            machine_text = json.dumps(machine, ensure_ascii=False) if machine_present else ""
            safe_machine = bounded_error(machine_text)
            # The durable projection, including JSON escaping, stays below 8 KiB.
            message_bytes, machine_bytes = safe_message.encode(), safe_machine.encode()
            self.diagnostic = {
                "source": "codex-jsonl", "type": event_type, "sequence": self.sequence,
                "machine_present": machine_present,
                "machine": machine_bytes[:1024].decode("utf-8", errors="ignore"),
                "message": message_bytes[:2048].decode("utf-8", errors="ignore"),
                "sanitized": changed or safe_machine != machine_text,
                "truncated": len(message_bytes) > 2048 or len(machine_bytes) > 1024,
            }
        if event_type != "thread.started":
            return
        candidate = event.get("thread_id")
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("Codex reported an invalid Thread ID")
        if ((self.thread is not None and candidate != self.thread)
                or self.observed_thread is not None and candidate != self.observed_thread):
            raise ValueError("Codex reported a different Thread ID")
        if self.observed_thread is None:
            self.kernel.bind_thread(self.call, candidate)
            self.observed_thread = self.thread = candidate

    def feed(self, chunk: bytes) -> None:
        try:
            self.stdout_bytes += len(chunk)
            if self.stdout_bytes > _MAX_STDOUT_BYTES:
                raise ValueError("Codex JSONL exceeds its stream limit")
            self.decoder.decode(chunk)
            self.stream.feed(chunk)
        except BaseException:
            self.invalid = True
            raise

    def finish(self) -> bytes:
        if self.invalid:
            raise ValueError("Codex execution facts were not accepted")
        self.decoder.decode(b"", final=True)
        self.stream.finish()
        if not self.thread:
            raise ValueError("Codex did not report its Thread ID")
        if self.directory is None:
            raise ValueError("Codex final-message directory is not owned")
        descriptor = os.open("last-message.txt", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.directory)
        try:
            value = _read_regular(descriptor, _MAX_FINAL_BYTES)
        finally:
            os.close(descriptor)
        value.decode("utf-8")
        return value


def execute_codex(
    kernel: Kernel, call: AgentCall, *, executable: str, prompt: str,
    environment: Mapping[str, str], credential_file: Path, hidden_paths: Sequence[Path],
    authorize_source: Callable[[], None], timeout: float | None = None,
    hidden_environment: Sequence[str] = (),
) -> ProcessExit | None:
    """Execute exactly this allocated call; no service or identity discovery."""
    capture = _CodexCapture(kernel, call, executable=executable, environment=environment,
                            credential_file=credential_file, hidden_paths=hidden_paths,
                            hidden_environment=hidden_environment)
    return _execute(
        kernel, call, arguments=(), cwd=capture.cwd, prompt=prompt,
        environment=environment, timeout=timeout, authorize_source=authorize_source, capture=capture,
    )
