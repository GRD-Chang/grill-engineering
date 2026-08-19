"""Ephemeral Controller-owned credentials for a single Codex Worker."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from agent_run.error_safety import bounded_error


WORKER_CREDENTIAL_LIFETIME_SECONDS = 60 * 60
WORKER_RENEWAL_MARGIN_SECONDS = 5 * 60
WORKER_RENEWAL_WINDOW_SECONDS = 10 * 60
MAX_CHANNEL_MESSAGE_BYTES = 16 * 1024 * 1024
CHANNEL_SOCKET_TIMEOUT_SECONDS = 5.0
WORKER_GH_READ_TIMEOUT_SECONDS = 60.0
WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS = 15.0
WORKER_CREDENTIAL_PROVIDER_TIMEOUT_SECONDS = (
    2 * WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS
)
WORKER_GH_RESPONSE_TIMEOUT_SECONDS = (
    2 * WORKER_GH_READ_TIMEOUT_SECONDS
    + WORKER_RENEWAL_WINDOW_SECONDS
    + 2 * WORKER_CREDENTIAL_PROVIDER_TIMEOUT_SECONDS
    + CHANNEL_SOCKET_TIMEOUT_SECONDS
)


class WorkerCredentialError(RuntimeError):
    """A Worker cannot currently obtain a read-only GitHub credential."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = safe_http_status(http_status)


class InitialCredentialUnavailable(WorkerCredentialError):
    """The first credential mint failed before a Worker process started."""


def safe_http_status(value: object) -> int | None:
    """Return the only HTTP detail safe to retain outside a credential call."""

    return value if type(value) is int and 100 <= value <= 599 else None


@dataclass(frozen=True)
class ReadCredential:
    token: str
    expires_at: float


# Providers run on the renewal thread and must perform only bounded I/O. The
# production GitHub App provider caps each network and signing operation.
CredentialProvider = Callable[[], ReadCredential | str]


class WorkerCredentialChannel:
    """Serve renewable read tokens over a temporary per-Worker Unix socket."""

    def __init__(
        self,
        provider: CredentialProvider,
        *,
        clock: Callable[[], float] = time.time,
        renewal_margin: float = WORKER_RENEWAL_MARGIN_SECONDS,
        renewal_window: float = WORKER_RENEWAL_WINDOW_SECONDS,
        gh_executable: str = "gh",
        gh_environment: dict[str, str] | None = None,
        on_exhausted: Callable[[str], None] | None = None,
    ) -> None:
        self._provider = provider
        self._clock = clock
        self._renewal_margin = renewal_margin
        self._renewal_window = renewal_window
        self._credential: ReadCredential | None = None
        self._failure_deadline: float | None = None
        self._last_error = ""
        self._retry_count = 0
        self._next_retry_at = 0.0
        self._retry_delay = 1.0
        self._gh_executable = gh_executable
        self._gh_environment = dict(gh_environment or {})
        self._on_exhausted = on_exhausted
        self._condition = threading.Condition()
        self._closed = False
        self._socket: socket.socket | None = None
        self._socket_path: Path | None = None
        self._server_thread: threading.Thread | None = None
        self._renewal_thread: threading.Thread | None = None
        self._active_gh_processes: set[subprocess.Popen[str]] = set()
        self._renewing = False

    def start(self, socket_path: Path) -> None:
        """Mint the initial credential and start the temporary channel."""

        with self._condition:
            self._renew_locked(require_credential=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.fspath(socket_path))
        listener.listen()
        listener.settimeout(0.2)
        self._socket = listener
        self._socket_path = socket_path
        self._server_thread = threading.Thread(
            target=self._serve,
            daemon=True,
            name="agent-run-worker-credential-channel",
        )
        self._renewal_thread = threading.Thread(
            target=self._renew_proactively,
            daemon=True,
            name="agent-run-worker-credential-renewal",
        )
        self._server_thread.start()
        self._renewal_thread.start()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._credential = None
            self._condition.notify_all()
        if self._socket is not None:
            self._socket.close()
        self._terminate_active_gh_processes()
        for thread in (self._server_thread, self._renewal_thread):
            if thread is not None:
                thread.join(timeout=0.1)
        if self._socket_path is not None:
            self._socket_path.unlink(missing_ok=True)

    def __enter__(self) -> WorkerCredentialChannel:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _serve(self) -> None:
        assert self._socket is not None
        while True:
            with self._condition:
                if self._closed:
                    return
            try:
                connection, _address = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(CHANNEL_SOCKET_TIMEOUT_SECONDS)
                try:
                    request = _receive_message(connection)
                    response = self._request(request)
                except (
                    WorkerCredentialError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    OSError,
                    TimeoutError,
                ) as error:
                    response = {"error": bounded_error(str(error))}
                try:
                    _send_message(connection, response)
                except WorkerCredentialError as error:
                    try:
                        _send_message(connection, {"error": bounded_error(str(error))})
                    except OSError:
                        continue
                except OSError:
                    continue

    def _request(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or request.get("kind") != "run":
            raise WorkerCredentialError("invalid Worker GitHub read request")
        arguments = request.get("arguments")
        if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
            raise WorkerCredentialError("invalid Worker GitHub read arguments")
        if not _is_allowed_gh_read(arguments):
            raise WorkerCredentialError("Worker GitHub adapter only permits read commands")
        result = self._run_gh(arguments)
        return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    def _run_gh(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        environment = dict(self._gh_environment)
        environment["GH_TOKEN"] = self._token()
        environment["GH_ENTERPRISE_TOKEN"] = environment["GH_TOKEN"]
        result = self._run_gh_once(arguments, environment)
        if result.returncode and _expired_auth(result.stderr):
            self._invalidate()
            environment["GH_TOKEN"] = self._token()
            environment["GH_ENTERPRISE_TOKEN"] = environment["GH_TOKEN"]
            result = self._run_gh_once(arguments, environment)
        return result

    def _run_gh_once(
        self, arguments: list[str], environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        try:
            process = subprocess.Popen(
                [self._gh_executable, *arguments],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise WorkerCredentialError("Could not start Worker GitHub read") from error
        with self._condition:
            self._active_gh_processes.add(process)
            closed = self._closed
        if closed:
            _terminate_gh_process(process)
        try:
            stdout, stderr = process.communicate(timeout=WORKER_GH_READ_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            _terminate_gh_process(process)
            raise WorkerCredentialError("Worker GitHub read timed out") from error
        except OSError as error:
            _terminate_gh_process(process)
            raise WorkerCredentialError("Worker GitHub read failed") from error
        finally:
            with self._condition:
                self._active_gh_processes.discard(process)
        return subprocess.CompletedProcess(
            [self._gh_executable, *arguments], process.returncode, stdout, stderr
        )

    def _terminate_active_gh_processes(self) -> None:
        with self._condition:
            processes = tuple(self._active_gh_processes)
        for process in processes:
            _terminate_gh_process(process)

    def _renew_proactively(self) -> None:
        delay = 1.0
        while True:
            with self._condition:
                if self._closed:
                    return
                credential = self._credential
                now = self._clock()
                if credential is not None and now < credential.expires_at - self._renewal_margin:
                    self._condition.wait(credential.expires_at - self._renewal_margin - now)
                    continue
                if self._renewing:
                    self._condition.wait()
                    continue
                if now < self._next_retry_at:
                    self._condition.wait(self._next_retry_at - now)
                    continue
                self._renew_locked(require_credential=False)
                if self._credential_is_valid_locked():
                    if self._last_error:
                        self._condition.wait(self._next_retry_at - now)
                    else:
                        delay = 1.0
                elif self._failure_deadline is not None and now >= self._failure_deadline:
                    self._notify_exhausted()
                    return
                else:
                    self._condition.wait(delay)
                    delay = min(delay * 2, 30.0)

    def _token(self) -> str:
        with self._condition:
            while True:
                if self._closed:
                    raise WorkerCredentialError("Worker credential channel is closed")
                if self._credential_is_valid_locked():
                    credential = self._credential
                    assert credential is not None
                    if self._clock() >= credential.expires_at - self._renewal_margin:
                        if self._clock() >= self._next_retry_at:
                            self._renew_locked(require_credential=False)
                        if (
                            self._credential is not credential
                            and self._credential_is_valid_locked()
                        ):
                            continue
                    return credential.token
                if (
                    self._failure_deadline is not None
                    and self._clock() >= self._failure_deadline
                ):
                    raise WorkerCredentialError(self._renewal_pause_message())
                if self._clock() < self._next_retry_at:
                    self._condition.wait(self._next_retry_at - self._clock())
                    continue
                if self._renewing:
                    self._condition.wait()
                    continue
                self._renew_locked(require_credential=False)
                if self._credential_is_valid_locked():
                    continue
                deadline = self._failure_deadline
                now = self._clock()
                if deadline is None or now >= deadline:
                    raise WorkerCredentialError(self._renewal_pause_message())
                self._condition.wait(min(1.0, deadline - now))

    def _invalidate(self) -> None:
        with self._condition:
            self._credential = None
            self._renew_locked(require_credential=False)
            self._condition.notify_all()

    def _credential_is_valid_locked(self) -> bool:
        return self._credential is not None and self._clock() < self._credential.expires_at

    def _renew_locked(self, *, require_credential: bool) -> None:
        if self._renewing:
            if require_credential:
                while self._renewing and not self._closed:
                    self._condition.wait()
                if self._credential_is_valid_locked():
                    return
                if self._closed:
                    raise WorkerCredentialError("Worker credential channel is closed")
                raise WorkerCredentialError(
                    "Could not create initial Worker read credential: "
                    f"{self._last_error or 'unavailable'}"
                )
            return
        self._renewing = True
        credential: ReadCredential | None = None
        renewal_error: Exception | None = None
        self._condition.release()
        try:
            credential = _coerce_credential(self._provider(), self._clock())
        except Exception as error:
            renewal_error = error
        finally:
            self._condition.acquire()
            self._renewing = False
        if self._closed:
            self._condition.notify_all()
            if require_credential:
                raise WorkerCredentialError("Worker credential channel is closed")
            return
        if renewal_error is not None:
            now = self._clock()
            self._last_error = bounded_error(str(renewal_error))
            self._retry_count += 1
            if self._failure_deadline is None:
                self._failure_deadline = now + self._renewal_window
            self._next_retry_at = now + self._retry_delay
            self._retry_delay = min(self._retry_delay * 2, 30.0)
            if require_credential:
                raise WorkerCredentialError(
                    "Could not create initial Worker read credential: "
                    f"{self._last_error}",
                    http_status=getattr(renewal_error, "http_status", None),
                ) from renewal_error
            self._condition.notify_all()
            return
        assert credential is not None
        self._credential = credential
        self._failure_deadline = None
        self._last_error = ""
        self._retry_count = 0
        self._next_retry_at = 0.0
        self._retry_delay = 1.0
        self._condition.notify_all()

    def _renewal_pause_message(self) -> str:
        return (
            "worker_credential_renewal_failed: Worker read credential renewal did not recover after "
            f"{self._retry_count} attempts; valid credential available: no; "
            f"last error: {self._last_error or 'unavailable'}; "
            "run agent-run resume to start a fresh Worker"
        )

    def _notify_exhausted(self) -> None:
        if self._on_exhausted is not None:
            self._on_exhausted(self._renewal_pause_message())


def _coerce_credential(value: ReadCredential | str, now: float) -> ReadCredential:
    if isinstance(value, ReadCredential):
        if not value.token.strip() or value.expires_at <= now:
            raise WorkerCredentialError("GitHub credential provider returned an invalid credential")
        return value
    if isinstance(value, str) and value.strip():
        return ReadCredential(value, now + WORKER_CREDENTIAL_LIFETIME_SECONDS)
    raise WorkerCredentialError("GitHub credential provider returned an empty token")


def _receive_message(connection: socket.socket) -> object:
    size = int.from_bytes(_read_exact(connection, 4), "big")
    if size > MAX_CHANNEL_MESSAGE_BYTES:
        raise WorkerCredentialError("Worker GitHub read request exceeded the size limit")
    return json.loads(_read_exact(connection, size))


def _read_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise WorkerCredentialError("Worker GitHub credential channel closed early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_message(connection: socket.socket, value: dict[str, object]) -> None:
    payload = json.dumps(value).encode()
    if len(payload) > MAX_CHANNEL_MESSAGE_BYTES:
        raise WorkerCredentialError("Worker GitHub read response exceeded the size limit")
    connection.sendall(len(payload).to_bytes(4, "big") + payload)


def _is_allowed_gh_read(arguments: list[str]) -> bool:
    if not arguments:
        return False
    if _has_external_repository(arguments):
        return False
    if arguments[0] == "api":
        return _is_get_api_request(arguments[1:])
    commands = {
        "issue": {"view", "list"}, "pr": {"view", "list", "checks"},
        "repo": {"view"}, "run": {"view", "list"}, "workflow": {"view", "list"},
    }
    return arguments[0] in {"search", "status"} or (
        len(arguments) > 1 and arguments[0] in commands and arguments[1] in commands[arguments[0]]
    )


def _is_get_api_request(arguments: list[str]) -> bool:
    """Permit only `gh api` calls that cannot carry a request body."""

    body_flags = {"-f", "-F", "--field", "--raw-field", "--input"}
    for index, argument in enumerate(arguments):
        upper = argument.upper()
        parsed = urlsplit(argument)
        if parsed.scheme or parsed.netloc:
            return False
        if argument == "--hostname" or argument.startswith("--hostname="):
            return False
        if argument in body_flags or any(argument.startswith(flag + "=") for flag in body_flags):
            return False
        if argument == "-X":
            if index + 1 >= len(arguments) or arguments[index + 1].upper() != "GET":
                return False
            continue
        if upper.startswith("-X") and upper != "-XGET":
            return False
        if argument == "--method":
            if index + 1 >= len(arguments) or arguments[index + 1].upper() != "GET":
                return False
            continue
        if upper.startswith("--METHOD=") and upper != "--METHOD=GET":
            return False
    return True


def _has_external_repository(arguments: list[str]) -> bool:
    for index, argument in enumerate(arguments):
        selector: str | None = None
        if argument in {"--repo", "-R"}:
            if index + 1 >= len(arguments):
                return True
            selector = arguments[index + 1]
        elif argument.startswith("--repo="):
            selector = argument.removeprefix("--repo=")
        elif argument.startswith("-R") and argument != "-R":
            selector = argument[2:]
        if selector is not None and not _is_github_repository(selector):
            return True
    return False


def _is_github_repository(selector: str) -> bool:
    owner, separator, repository = selector.partition("/")
    return bool(owner and separator and repository and "/" not in repository)


def _terminate_gh_process(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, 15)
    except ProcessLookupError:
        return
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            return
        process.wait(timeout=1)


def _expired_auth(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in ("http 401", "bad credentials", "token expired", "unauthorized"))
