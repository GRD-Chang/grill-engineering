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
import threading
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from agent_run.errors import bounded_error
from agent_run.runtime.sandbox import _BoundedJsonlStream, WorkerSandboxError, bubblewrap_command, native_worker_environment

from agent_run.contracts.artifact import role_schema
from agent_run.runtime.execution import ProcessExit, ProcessExitUnknown, _execute
from agent_run.model import AgentCall, AgentPreparation

if TYPE_CHECKING:
    from agent_run.core.kernel import Kernel


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
        self, kernel: Kernel, preparation: AgentPreparation, *, executable: str,
        environment: Mapping[str, str], credential_file: Path | None,
        hidden_paths: Sequence[Path], hidden_environment: Sequence[str],
        identity: Mapping[str, Any] | None = None, cancelled: threading.Event | None = None,
    ) -> None:
        self.kernel, self.preparation, self.executable = kernel, preparation, executable
        self.call: AgentCall | None = None
        self.cleanup_complete = False
        # Local launch knowledge only. Cleared before Popen can create anything;
        # absence of a stored PID or a completed Future is never this proof.
        self.unstarted_failure: tuple[str, dict[str, Any] | str] | None = (
            "worker_preparation_failed", {"boundary": "before_worker_spawn"})
        self.source_environment = dict(environment)
        self.credential_file = credential_file
        self.identity, self.cancelled = identity, cancelled
        self.credential_exhausted = threading.Event()
        self.inherit_host_auth = False
        self.hidden_paths, self.hidden_environment = tuple(hidden_paths), tuple(hidden_environment)
        self.cwd = Path(preparation.context["checkout"])
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.resources = ExitStack()
        self.directory: int | None = None
        self.thread = preparation.reported_thread
        self.observed_thread: str | None = None
        self.invalid = False
        self.stdout_bytes = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self.stream = _BoundedJsonlStream(on_line=self._line)
        self.sequence = 0
        self.failure_kind = "ordinary"
        self.diagnostic: dict[str, Any] | None = None
        self.preparation_deadline = time.monotonic() + max(0.0, preparation.preparation_deadline - kernel.clock())

    def bind_call(self, call: AgentCall) -> None:
        if (self.call is not None or call.lease_id != self.preparation.reservation_id
                or call.attempt_id != self.preparation.attempt_id or call.execution != self.preparation.execution):
            raise ValueError("Preparation admission binding changed")
        self.call = call

    def _preparation_guard(self) -> None:
        self.guard()
        self.kernel.guard_preparation(self.preparation)

    def _close(self) -> None:
        self.resources.close()
        self.cleanup_complete = True

    def __enter__(self) -> Self:
        try:
            temporary = Path(self.resources.enter_context(tempfile.TemporaryDirectory(prefix="agent-run-kernel-codex-")))
            temporary.chmod(0o700)
            self.directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.resources.callback(os.close, self.directory)
            self._preparation_guard()
            gh = temporary / "gh"
            self.environment = self._credential_environment(gh)
            self._preparation_guard()
            schema = json.dumps(role_schema(self.preparation.role.value), separators=(",", ":")).encode()
            if len(schema) > 64 * 1024:
                raise ValueError("Worker output schema exceeds its file limit")
            _write_member(self.directory, "schema.json", schema)
            _write_member(self.directory, "last-message.txt", b"")
            for variable in self.hidden_environment:
                self.environment.pop(variable, None)
            self.environment["GH_PROMPT_DISABLED"] = "1"
            self.environment["GIT_TERMINAL_PROMPT"] = "0"
            readonly = self.preparation.context["readonly"]
            if type(readonly) is not bool:
                raise ValueError("Allocated checkout access must be explicit")
            if self.preparation.output_ordinal > 0 and not readonly:
                raise ValueError("Output repair requires an allocated read-only checkout")
            command = self._command(temporary)
            self.arguments = bubblewrap_command(
                command, checkout=self.cwd, temporary=temporary, writable_checkout=not readonly,
                environment=self.environment, hidden_paths=self._protected_paths(), inherit_host_auth=self.inherit_host_auth,
            )
            return self
        except BaseException as error:
            from agent_run.adapters.auth.credential_lifecycle import safe_http_status

            failure = error if hasattr(error, "retryable") else error.__cause__
            self.diagnostic = {"source": "worker-preparation", "error_type": type(error).__name__,
                               "http_status": safe_http_status(getattr(failure, "http_status", None))}
            try:
                self._close()
            except BaseException as cleanup_error:
                raise error from cleanup_error
            raise

    def _credential_environment(self, gh: Path) -> dict[str, str]:
        if self.credential_file is not None:
            source = os.open(self.credential_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                credential = _read_regular(source, _MAX_CREDENTIAL_BYTES)
            finally:
                os.close(source)
            if not credential.strip():
                raise ValueError("Selected Worker credential file is empty")
            gh.mkdir(mode=0o700)
            descriptor = os.open(gh, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _write_member(descriptor, "hosts.yml", credential)
            finally:
                os.close(descriptor)
            environment = native_worker_environment(self.source_environment, gh, host=False)
        else:
            from agent_run.adapters.auth.identity import native_github_environment, validate_worker_auth
            from agent_run.contracts.identity import validate_identity_snapshot

            self.identity = identity = validate_identity_snapshot(self.identity)
            repository = self.preparation.context["repository"]
            if not isinstance(repository, str) or not repository:
                raise ValueError("Allocated repository identity is unavailable")
            if identity["source"] == "app":
                host = self.source_environment.get("GH_HOST", "").strip()
                if host and host.casefold() != "github.com":
                    raise WorkerSandboxError("Worker GitHub read host is not authorized")
                from agent_run.runtime.git_command import git_environment
                from agent_run.adapters.auth.app import _GitHubAppCredentialProvider
                from agent_run.config.github_app import GitHubAppProfile
                from agent_run.adapters.auth.worker_credentials import NativeCredentialDelivery

                app = identity["app_profile"]
                provider = _GitHubAppCredentialProvider(
                    GitHubAppProfile(app["app_id"], app["installation_id"], Path(app["private_key_path"])),
                    read_only=identity["app_read_only"],
                    repository=repository if identity["app_current_repository_only"] else None,
                )
                delivery = self.resources.enter_context(NativeCredentialDelivery(
                    provider, gh, on_exhausted=lambda _message: self.credential_exhausted.set(),
                ))
                delivery.start(guard=self._preparation_guard, deadline=self.preparation_deadline)
                environment = native_worker_environment(git_environment(self.source_environment), gh, host=False)
                environment.pop("GH_HOST", None)
                environment.update({
                    "XDG_CONFIG_HOME": str(gh.parent), "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
                    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
                })
            else:
                from agent_run.adapters.auth.credential_lifecycle import prepare_initial_credential

                prepare_initial_credential(lambda: validate_worker_auth(identity, self.source_environment),
                    guard=self._preparation_guard, deadline=self.preparation_deadline)
                self.inherit_host_auth = identity["source"] == "host"
                environment = native_worker_environment(
                    native_github_environment(identity, self.source_environment), gh, host=self.inherit_host_auth,
                )
            environment["GH_REPO"] = repository
        if self.credential_file is not None or self.identity is not None and self.identity["source"] == "app":
            for variable in _GITHUB_TOKEN_VARIABLES:
                environment.pop(variable, None)
        return environment

    def guard(self) -> None:
        """Physical Host/credential cancellation does not grant a retry."""
        if self.cancelled is not None and self.cancelled.is_set():
            self.failure_kind = "fatal"
            self.diagnostic = {"source": "host-cancellation", "error_type": "InterruptedError"}
            raise InterruptedError("Host execution cancelled")
        if self.credential_exhausted.is_set():
            from agent_run.adapters.auth.credential_lifecycle import WorkerCredentialError

            self.failure_kind = "fatal"
            self.diagnostic = {"source": "worker-credentials", "error_type": "CredentialExhausted"}
            raise WorkerCredentialError("Selected Worker credential renewal exhausted")

    def __exit__(
        self, exception_type: type[BaseException] | None, exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._close()

    def _command(self, temporary: Path) -> list[str]:
        if self.preparation.mode == "fresh":
            if self.preparation.reported_thread is not None:
                raise ValueError("Fresh invocation already has a backend Thread")
            command = [self.executable, "exec", "--json", "--color", "never",
                       "--dangerously-bypass-approvals-and-sandbox", "--cd", str(self.cwd)]
        elif self.preparation.mode in {"resume", "output-repair", "execution-recovery"}:
            if not self.preparation.reported_thread:
                raise ValueError("Continuation requires its original backend Thread")
            command = [self.executable, "exec", "resume", "--json",
                       "--dangerously-bypass-approvals-and-sandbox", self.preparation.reported_thread]
        else:
            raise ValueError("Unknown allocated Codex invocation mode")
        command.extend(["--model", self.preparation.profile.model, "--config",
                        f'model_reasoning_effort="{self.preparation.profile.effort}"',
                        "--output-schema", str(temporary / "schema.json"),
                        "--output-last-message", str(temporary / "last-message.txt"), "-"])
        return command

    def _protected_paths(self) -> tuple[Path, ...]:
        state = self.kernel.storage.path.resolve()
        if state.parent == self.cwd.resolve():
            raise ValueError("Kernel state needs a directory separate from the Worker checkout")
        paths = [state.parent, state, Path(str(state) + "-wal"), Path(str(state) + "-shm"),
                 *self.hidden_paths]
        if self.credential_file is not None:
            paths.append(self.credential_file)
        if self.identity is not None:
            if self.identity["source"] == "app":
                paths.append(Path(self.identity["app_profile"]["private_key_path"]))
            elif self.identity["source"] == "pat" and "file" in self.identity["pat"]:
                paths.append(Path(self.identity["pat"]["file"]))
        environment = self.source_environment
        home = Path(environment.get("HOME", str(Path.home())))
        if not self.inherit_host_auth:
            paths.extend((home / ".config" / "gh", home / ".ssh", home / ".git-credentials"))
            paths.extend(Path(environment[key]) for key in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME") if environment.get(key))
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
        assert self.call is not None
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
        if event_type in {"turn.failed", "task_complete"} and self.failure_kind != "fatal":
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


def _execute_prepared(
    capture: _CodexCapture, *, prompt: str, timeout: float | None,
    authorize_source: Callable[[AgentCall], None],
) -> ProcessExit | None:
    call: AgentCall | None = None
    try:
        with capture:
            step = capture.kernel.admit_agent(capture.preparation)
            call = step.call
            assert call is not None
            capture.bind_call(call)
            result = _execute(capture.kernel, call, arguments=capture.arguments, cwd=capture.cwd,
                prompt=prompt, environment=capture.environment, timeout=timeout,
                authorize_source=lambda: authorize_source(call), capture=capture)
    except BaseException as error:
        # No Future or exception is an exit proof. Only successful resource
        # cleanup releases a preparation seat; admitted work uses output proof.
        if call is None and capture.cleanup_complete:
            expired = capture.kernel.clock() >= capture.preparation.preparation_deadline
            capture.kernel.preparation_failed(capture.preparation,
                "preparation_deadline_exceeded" if expired else "worker_preparation_failed",
                capture.diagnostic or {"source": "worker-preparation", "error_type": type(error).__name__})
        if call is not None and capture.cleanup_complete and capture.unstarted_failure is not None:
            try:
                capture.kernel.agent_unavailable(call, *capture.unstarted_failure)
            except BaseException as settlement_error:
                raise error from settlement_error
        if call is not None and not capture.cleanup_complete and isinstance(error, Exception):
            raise ProcessExitUnknown("Worker credential resource cleanup remains unproven") from error
        if call is None and isinstance(error, OSError) and capture.cleanup_complete:
            return None
        raise
    if capture.cleanup_complete and capture.unstarted_failure is not None:
        assert call is not None
        capture.kernel.agent_unavailable(call, *capture.unstarted_failure)
    return result


def execute_codex(
    kernel: Kernel, preparation: AgentPreparation, *, executable: str, prompt: str,
    environment: Mapping[str, str], credential_file: Path, hidden_paths: Sequence[Path],
    authorize_source: Callable[[AgentCall], None], timeout: float | None = None,
    hidden_environment: Sequence[str] = (),
) -> ProcessExit | None:
    """Prepare credentials, atomically admit this seat, then execute once."""
    capture = _CodexCapture(kernel, preparation, executable=executable, environment=environment,
                            credential_file=credential_file, hidden_paths=hidden_paths,
                            hidden_environment=hidden_environment)
    return _execute_prepared(capture, prompt=prompt, timeout=timeout, authorize_source=authorize_source)


def execute_selected_codex(
    kernel: Kernel, preparation: AgentPreparation, *, identity: Mapping[str, Any],
    environment: Mapping[str, str], prompt: str, hidden_paths: Sequence[Path],
    authorize_source: Callable[[AgentCall], None], executable: str = "codex", timeout: float | None = None,
    hidden_environment: Sequence[str] = (), cancelled: threading.Event | None = None,
) -> ProcessExit | None:
    """Own credential preparation and the admitted native invocation together."""
    capture = _CodexCapture(kernel, preparation, executable=executable, environment=environment,
                            credential_file=None, identity=identity, cancelled=cancelled,
                            hidden_paths=hidden_paths, hidden_environment=hidden_environment)
    return _execute_prepared(capture, prompt=prompt, timeout=timeout, authorize_source=authorize_source)
