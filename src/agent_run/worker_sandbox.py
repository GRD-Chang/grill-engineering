from __future__ import annotations

import codecs
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from agent_run.git_output import git_environment


class WorkerSandboxError(RuntimeError):
    pass


class WorkerDeadlineExceeded(WorkerSandboxError):
    pass


_STREAM_CHUNK_BYTES = 64 * 1024
_STDOUT_CAPTURE_BYTES = 64 * 1024
_STDERR_CAPTURE_BYTES = 64 * 1024
_JSONL_LINE_BYTES = 64 * 1024
_WORKER_BOOTSTRAP = """\
import os
import sys
import time
gate = int(sys.argv[1])
deadline = float(sys.argv[2])
try:
    released = os.read(gate, 1)
finally:
    os.close(gate)
if released != b"1":
    raise SystemExit(125)
if deadline >= 0 and time.monotonic() >= deadline:
    raise SystemExit(124)
os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
"""


class _BoundedBytesTail:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._value = bytearray()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._value.extend(chunk[-self.limit :])
        if len(self._value) > self.limit:
            del self._value[: len(self._value) - self.limit]

    def text(self) -> str:
        return bytes(self._value).decode("utf-8", errors="replace")


class _BoundedJsonlStream:
    """Incrementally retain a bounded JSONL tail and critical event lines."""

    def __init__(
        self,
        *,
        capture_bytes: int = _STDOUT_CAPTURE_BYTES,
        line_bytes: int = _JSONL_LINE_BYTES,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        self.capture_bytes = capture_bytes
        self.line_bytes = line_bytes
        self.on_line = on_line
        self._tail = _BoundedBytesTail(capture_bytes)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line = ""
        self._discarding_line = False
        self._pinned: dict[str, str] = {}

    def feed(self, chunk: bytes) -> None:
        self._tail.append(chunk)
        self._consume(self._decoder.decode(chunk))

    def finish(self) -> None:
        self._consume(self._decoder.decode(b"", final=True), final=True)

    def text(self) -> str:
        # Critical lines share the same byte budget as the ordinary tail;
        # pinning must not silently turn a bounded capture into an unbounded one.
        per_line = max(1, self.capture_bytes // max(1, len(self._pinned) + 1))
        pinned_lines = [
            line
            for line in self._pinned.values()
            if len(line.encode("utf-8")) <= per_line
        ]
        pinned = "\n".join(pinned_lines)
        prefix = f"{pinned}\n" if pinned else ""
        prefix_bytes = prefix.encode("utf-8")
        remaining = max(0, self.capture_bytes - len(prefix_bytes))
        tail_bytes = self._tail.text().encode("utf-8")[-remaining:] if remaining else b""
        return (prefix_bytes + tail_bytes).decode("utf-8", errors="ignore")

    def _consume(self, value: str, *, final: bool = False) -> None:
        for part in value.splitlines(keepends=True):
            terminated = part.endswith(("\n", "\r"))
            content = part.rstrip("\r\n") if terminated else part
            if not self._discarding_line:
                candidate = self._line + content
                if len(candidate.encode("utf-8")) <= self.line_bytes:
                    self._line = candidate
                else:
                    self._line = ""
                    self._discarding_line = True
            if terminated:
                if not self._discarding_line:
                    self._handle_line(self._line)
                self._line = ""
                self._discarding_line = False
        if final and self._line and not self._discarding_line:
            self._handle_line(self._line)
            self._line = ""

    def _handle_line(self, line: str) -> None:
        if self.on_line is not None:
            self.on_line(line)
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(value, dict):
            return
        event_type = value.get("type")
        if event_type == "thread.started":
            self._pinned["thread.started"] = line
        elif event_type in {"task_complete", "turn.failed"}:
            self._pinned[str(event_type)] = line
        elif "error" in value:
            self._pinned["error"] = line


def native_worker_environment(
    environment: dict[str, str], gh_config: Path, *, host: bool
) -> dict[str, str]:
    """Pass the selected native identity, without Runner control secrets."""
    result = {
        key: value for key, value in environment.items()
        if not key.startswith(("AGENT_RUN_EXECUTOR_", "AGENT_RUN_INTERNAL_"))
        and key not in {
            "AGENT_RUN_GITHUB_APP_ID", "AGENT_RUN_GITHUB_APP_INSTALLATION_ID",
            "AGENT_RUN_GITHUB_APP_PRIVATE_KEY", "AGENT_RUN_GITHUB_READ_PERMISSIONS",
            "AGENT_RUN_GITHUB_READ_TOKEN", "DBUS_SESSION_BUS_ADDRESS",
        }
    }
    if not host:
        for key in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_ASKPASS", "SSH_ASKPASS"):
            result.pop(key, None)
        gh_config.mkdir(parents=True, exist_ok=True)
        result["GH_CONFIG_DIR"] = str(gh_config)
        result["GH_PROMPT_DISABLED"] = "1"
    return result


def worker_environment(
    gh_config: Path, github_read_token: str
) -> dict[str, str]:
    _validate_inherited_github_host()
    environment = {
        key: value
        for key, value in git_environment().items()
        if not key.startswith(("AGENT_RUN_EXECUTOR_", "AGENT_RUN_INTERNAL_"))
        and key
        not in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "SSH_AUTH_SOCK",
            "AGENT_RUN_GITHUB_APP_ID",
            "AGENT_RUN_GITHUB_APP_INSTALLATION_ID",
            "AGENT_RUN_GITHUB_APP_PRIVATE_KEY",
            "AGENT_RUN_GITHUB_READ_PERMISSIONS",
            "AGENT_RUN_GITHUB_READ_TOKEN",
            "GH_HOST",
            "DBUS_SESSION_BUS_ADDRESS",
        }
    }
    gh_config.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "GH_CONFIG_DIR": str(gh_config),
            "XDG_CONFIG_HOME": str(gh_config.parent),
            "GH_TOKEN": github_read_token,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    return environment


def _validate_inherited_github_host() -> None:
    host = os.environ.get("GH_HOST", "").strip()
    if host and host.casefold() != "github.com":
        raise WorkerSandboxError("Worker GitHub read host is not authorized")


def bubblewrap_command(
    command: list[str],
    *,
    checkout: Path,
    temporary: Path,
    writable_checkout: bool,
    environment: dict[str, str],
    hidden_paths: Sequence[Path] = (),
    inherit_host_auth: bool = False,
) -> list[str]:
    _reject_credentialed_http_remotes(checkout, environment)
    executable = shutil.which("bwrap")
    if executable is None:
        raise WorkerSandboxError(
            "bubblewrap is required to enforce the Publisher authority boundary"
        )
    arguments = [
        executable,
        "--die-with-parent",
        "--bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--bind",
        str(temporary),
        str(temporary),
    ]
    if writable_checkout:
        arguments.extend(["--bind", str(checkout), str(checkout)])
    else:
        arguments.extend(["--ro-bind", str(checkout), str(checkout)])
    git_metadata_paths = _git_metadata_paths(checkout)
    for git_metadata in git_metadata_paths:
        arguments.extend(
            ["--ro-bind", str(git_metadata), str(git_metadata)]
        )
    codex_home = Path(
        environment.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).resolve()
    if codex_home.exists():
        arguments.extend(["--bind", str(codex_home), str(codex_home)])
    home = Path.home()
    for secret_directory in (home / ".config" / "gh", home / ".ssh"):
        if not inherit_host_auth and secret_directory.exists():
            arguments.extend(["--tmpfs", str(secret_directory)])
    git_credentials = home / ".git-credentials"
    if not inherit_host_auth and git_credentials.exists():
        arguments.extend(["--ro-bind", "/dev/null", str(git_credentials)])
    hidden = _mask_worker_paths(
        arguments,
        hidden_paths,
        checkout=checkout,
        temporary=temporary,
    )
    # Managed checkouts live below the hidden Runner data root. Reopen only
    # this invocation's paths, then restore any narrower credential/state masks.
    reopened: list[Path] = []
    for path, binding in (
        (temporary, "--bind"),
        (checkout, "--bind" if writable_checkout else "--ro-bind"),
        *((path, "--ro-bind") for path in git_metadata_paths),
    ):
        resolved = path.resolve()
        if any(parent in hidden for parent in resolved.parents):
            arguments.extend([binding, str(resolved), str(resolved)])
            reopened.append(resolved)
    if reopened:
        _mask_worker_paths(
            arguments,
            [path for path in hidden if any(root in path.parents for root in reopened)],
            checkout=checkout,
            temporary=temporary,
        )
    arguments.extend(["--chdir", str(checkout), "--", *command])
    return arguments


def _mask_worker_paths(
    arguments: list[str],
    hidden_paths: Sequence[Path],
    *,
    checkout: Path,
    temporary: Path,
) -> set[Path]:
    checkout_root = checkout.resolve()
    temporary_root = temporary.resolve()
    paths: set[Path] = set()
    for candidate in hidden_paths:
        try:
            resolved = Path(candidate).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved in {checkout_root, temporary_root}:
            continue
        paths.add(resolved)

    directories = sorted(
        (path for path in paths if path.is_dir()),
        key=lambda path: (len(path.parts), str(path)),
    )
    mounted_directories: list[Path] = []
    for directory in directories:
        # Keep explicitly masked subdirectories available as empty private
        # mountpoints even when their parent state root is already hidden.
        arguments.extend(["--tmpfs", str(directory)])
        mounted_directories.append(directory)

    for path in sorted(paths - set(directories), key=str):
        if path.name != ".lock" and any(directory in path.parents for directory in mounted_directories):
            continue
        arguments.extend(["--ro-bind", "/dev/null", str(path)])
    return paths


def _reject_credentialed_http_remotes(
    checkout: Path, environment: dict[str, str]
) -> None:
    repository = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--git-dir"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
    )
    if repository.returncode != 0:
        if (checkout.absolute() / ".git").exists():
            raise WorkerSandboxError(
                "Could not inspect Worker Git remote URLs"
            )
        return
    configured = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "config",
            "--null",
            "--get-regexp",
            r"^remote\..*\.(url|pushurl)$",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
    )
    if configured.returncode not in {0, 1}:
        raise WorkerSandboxError("Could not inspect Worker Git remote URLs")
    for record in configured.stdout.split("\0"):
        if not record:
            continue
        _key, separator, remote_url = record.partition("\n")
        if not separator:
            raise WorkerSandboxError("Could not inspect Worker Git remote URLs")
        _reject_credentialed_http_url(remote_url)
    remotes = subprocess.run(
        ["git", "-C", str(checkout), "remote"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
    )
    if remotes.returncode != 0:
        raise WorkerSandboxError("Could not inspect Worker Git remote URLs")
    for remote in remotes.stdout.splitlines():
        for options in (("--all",), ("--push", "--all")):
            urls = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "remote",
                    "get-url",
                    *options,
                    remote,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                check=False,
            )
            if urls.returncode != 0:
                raise WorkerSandboxError(
                    "Could not inspect Worker Git remote URLs"
                )
            for remote_url in urls.stdout.splitlines():
                _reject_credentialed_http_url(remote_url)


def _reject_credentialed_http_url(remote_url: str) -> None:
    try:
        parsed = urlsplit(remote_url)
    except ValueError:
        raise WorkerSandboxError(
            "Could not inspect Worker Git remote URLs"
        ) from None
    if parsed.scheme.lower() in {"http", "https"} and (
        parsed.username is not None or parsed.password is not None
    ):
        raise WorkerSandboxError(
            "Worker checkout contains credential-bearing HTTP(S) "
            "Git remote URL"
        )


def _git_metadata_paths(checkout: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    literal_git = checkout.absolute() / ".git"
    if literal_git.exists():
        paths.append(literal_git)
    resolved = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            "--git-common-dir",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if resolved.returncode == 0:
        paths.extend(
            Path(value).resolve()
            for value in resolved.stdout.splitlines()
            if value
        )
    return tuple(dict.fromkeys(paths))


def run_worker_process(
    arguments: list[str],
    *,
    cwd: Path,
    prompt: str,
    environment: dict[str, str],
    timeout: float,
    deadline_at_monotonic: float | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
    abort_event: threading.Event | None = None,
    abort_reason: Callable[[], str] | None = None,
    on_process_started: Callable[[int], None] | None = None,
    on_process_released: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    from agent_run.worker_control import check_worker_control

    check_worker_control()
    gate_write: int | None = None
    if on_process_started is None and on_process_released is None:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=environment,
            text=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    else:
        gate_read, gate_write = os.pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _WORKER_BOOTSTRAP,
                    str(gate_read),
                    str(
                        deadline_at_monotonic
                        if deadline_at_monotonic is not None
                        else -1.0
                    ),
                    *arguments,
                ],
                cwd=cwd,
                env=environment,
                text=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(gate_read,),
            )
        finally:
            os.close(gate_read)
    try:
        if on_process_started is not None:
            on_process_started(process.pid)
        check_worker_control()
        if gate_write is not None:
            if (
                deadline_at_monotonic is not None
                and time.monotonic() >= deadline_at_monotonic
            ):
                raise WorkerDeadlineExceeded("Codex worker timed out")
            os.write(gate_write, b"1")
            # Registration, persistence and control checks ran while the
            # bootstrap was blocked. Timing may start only after releasing it.
            if on_process_released is not None:
                on_process_released()
            if (
                deadline_at_monotonic is not None
                and time.monotonic() >= deadline_at_monotonic
            ):
                raise WorkerDeadlineExceeded("Codex worker timed out")
    except BaseException:
        if gate_write is not None:
            os.close(gate_write)
        try:
            _terminate_process_group(process)
            process.wait()
        finally:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        raise
    else:
        if gate_write is not None:
            os.close(gate_write)
    stdin = process.stdin
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    callback_errors: list[BaseException] = []
    reader_errors: list[BaseException] = []
    writer_errors: list[BaseException] = []
    stdout_capture: _BoundedJsonlStream | None = None
    stderr_capture: _BoundedBytesTail | None = None
    stdout_reader: threading.Thread | None = None
    stderr_reader: threading.Thread | None = None
    stdin_writer: threading.Thread | None = None
    wait_error: BaseException | None = None

    def join_started(thread: threading.Thread | None) -> None:
        # A signal or a start failure may leave a later thread unstarted.
        # Thread.join() raises for that case, so use ident as the public
        # indication that start() created a thread.
        if thread is not None and thread.ident is not None:
            thread.join(timeout=1)

    try:
        # Everything after Popen succeeds belongs to the process lifetime.
        # In particular, a signal while constructing the first Thread must
        # still reach the cleanup below; no worker may escape because the
        # reader/writer objects were not all assigned yet.
        assert stdin is not None
        assert stdout_pipe is not None
        assert stderr_pipe is not None

        def forward_stdout_line(line: str) -> None:
            if on_stdout_line is not None and not callback_errors:
                try:
                    on_stdout_line(line)
                except BaseException as error:
                    callback_errors.append(error)
                    _terminate_process_group(process)

        stdout_capture = _BoundedJsonlStream(on_line=forward_stdout_line)
        stderr_capture = _BoundedBytesTail(_STDERR_CAPTURE_BYTES)

        def read_stdout() -> None:
            try:
                while chunk := os.read(stdout_pipe.fileno(), _STREAM_CHUNK_BYTES):
                    stdout_capture.feed(chunk)
                stdout_capture.finish()
            except BaseException as error:
                reader_errors.append(error)

        def read_stderr() -> None:
            try:
                while chunk := os.read(stderr_pipe.fileno(), _STREAM_CHUNK_BYTES):
                    stderr_capture.append(chunk)
            except BaseException as error:
                reader_errors.append(error)

        def write_stdin() -> None:
            try:
                stdin.write(prompt.encode("utf-8"))
                stdin.close()
            except BrokenPipeError:
                return
            except BaseException as error:
                writer_errors.append(error)

        stdout_reader = threading.Thread(
            target=read_stdout,
            daemon=True,
            name=f"agent-run-worker-{process.pid}-stdout",
        )
        stderr_reader = threading.Thread(
            target=read_stderr,
            daemon=True,
            name=f"agent-run-worker-{process.pid}-stderr",
        )
        stdin_writer = threading.Thread(
            target=write_stdin,
            daemon=True,
            name=f"agent-run-worker-{process.pid}-stdin",
        )
        # Keep process ownership and all thread startup inside the same
        # cleanup boundary.  Popen has already created a process group, so a
        # SIGINT between these start() calls must still terminate that group.
        stdout_reader.start()
        stderr_reader.start()
        stdin_writer.start()
        deadline = (
            deadline_at_monotonic
            if deadline_at_monotonic is not None
            else time.monotonic() + timeout
        )
        while process.poll() is None:
            check_worker_control()
            if abort_event is not None and abort_event.is_set():
                wait_error = WorkerSandboxError(
                    abort_reason() if abort_reason is not None else "Worker credential renewal expired"
                )
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                wait_error = WorkerDeadlineExceeded("Codex worker timed out")
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException as error:
        wait_error = error
    finally:
        _terminate_process_group(process)
        join_started(stdin_writer)
        join_started(stdout_reader)
        join_started(stderr_reader)
        if stdin_writer is not None and stdin_writer.is_alive() and stdin is not None:
            stdin.close()
            join_started(stdin_writer)
        if (
            stdout_reader is not None
            and stdout_reader.is_alive()
            and stdout_pipe is not None
        ):
            stdout_pipe.close()
            join_started(stdout_reader)
        if (
            stderr_reader is not None
            and stderr_reader.is_alive()
            and stderr_pipe is not None
        ):
            stderr_pipe.close()
            join_started(stderr_reader)
        for pipe in (stdin, stdout_pipe, stderr_pipe):
            if pipe is not None:
                pipe.close()
    if callback_errors:
        raise callback_errors[0]
    if wait_error is not None:
        raise wait_error
    if reader_errors:
        raise reader_errors[0]
    if writer_errors:
        raise writer_errors[0]
    assert stdout_capture is not None
    assert stderr_capture is not None
    return subprocess.CompletedProcess(
        arguments,
        process.returncode,
        stdout_capture.text(),
        stderr_capture.text(),
    )


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        # The original Popen remains our child even if its group disappeared.
        # Reap it before the caller can release its Ticket work lease.
        if process.poll() is None:
            process.kill()
        process.wait()
        return
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait()
