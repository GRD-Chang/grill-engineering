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

from agent_run.error_safety import bounded_error


WORKER_CREDENTIAL_LIFETIME_SECONDS = 60 * 60
WORKER_RENEWAL_MARGIN_SECONDS = 5 * 60
WORKER_RENEWAL_WINDOW_SECONDS = 10 * 60
MAX_CHANNEL_MESSAGE_BYTES = 16 * 1024 * 1024


class WorkerCredentialError(RuntimeError):
    """A Worker cannot currently obtain a read-only GitHub credential."""


@dataclass(frozen=True)
class ReadCredential:
    token: str
    expires_at: float


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
        self._process_group_id: int | None = None

    def allow_process_group(self, process_id: int) -> None:
        self._process_group_id = os.getpgid(process_id)

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
        for thread in (self._server_thread, self._renewal_thread):
            if thread is not None:
                thread.join(timeout=1)
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
                request = connection.recv(65536).strip()
                response: dict[str, object]
                try:
                    if not self._is_current_worker(connection):
                        raise WorkerCredentialError("credential channel belongs to another Worker")
                    response = self._request(json.loads(request))
                except (WorkerCredentialError, json.JSONDecodeError) as error:
                    response = {"error": bounded_error(str(error))}
                _send_message(connection, response)

    def _is_current_worker(self, connection: socket.socket) -> bool:
        if self._process_group_id is None:
            return False
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        peer_process_id = int.from_bytes(credentials[:4], "little")
        try:
            return os.getpgid(peer_process_id) == self._process_group_id
        except ProcessLookupError:
            return False

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
        result = subprocess.run([self._gh_executable, *arguments], env=environment, text=True, capture_output=True, check=False)
        if result.returncode and _expired_auth(result.stderr):
            self._invalidate()
            environment["GH_TOKEN"] = self._token()
            environment["GH_ENTERPRISE_TOKEN"] = environment["GH_TOKEN"]
            result = subprocess.run([self._gh_executable, *arguments], env=environment, text=True, capture_output=True, check=False)
        return result

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
        try:
            supplied = self._provider()
            credential = _coerce_credential(supplied, self._clock())
        except Exception as error:
            now = self._clock()
            self._last_error = bounded_error(str(error))
            self._retry_count += 1
            if self._failure_deadline is None:
                self._failure_deadline = now + self._renewal_window
            self._next_retry_at = now + self._retry_delay
            self._retry_delay = min(self._retry_delay * 2, 30.0)
            if require_credential:
                raise WorkerCredentialError(
                    "Could not create initial Worker read credential: "
                    f"{self._last_error}"
                ) from error
            self._condition.notify_all()
            return
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


def _send_message(connection: socket.socket, value: dict[str, object]) -> None:
    payload = json.dumps(value).encode()
    if len(payload) > MAX_CHANNEL_MESSAGE_BYTES:
        raise WorkerCredentialError("Worker GitHub read response exceeded the size limit")
    connection.sendall(len(payload).to_bytes(4, "big") + payload)


def _is_allowed_gh_read(arguments: list[str]) -> bool:
    if not arguments:
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


def _expired_auth(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in ("http 401", "bad credentials", "token expired", "unauthorized"))
