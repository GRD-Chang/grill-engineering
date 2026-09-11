from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime
import urllib.error
import urllib.request
from typing import Any, Callable

from agent_run.worker_credentials import (
    ReadCredential,
    WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS,
    safe_http_status,
)
from agent_run.process_cleanup import (
    terminate_process_group,
)
from agent_run.github_auth_profile import GitHubAppProfile


_DEFAULT_URL_OPEN = urllib.request.urlopen


class GitHubCredentialError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = safe_http_status(http_status)


class _SigningProcessRegistry:
    """Track only the OpenSSL process owned by one App credential provider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._network_closers: list[Callable[[], None]] = []
        self._cancelled = False

    def register(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._cancelled:
                terminate = True
            else:
                self._process = process
                terminate = False
        if terminate:
            self._terminate(process)

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise OSError("GitHub App credential provider is closed")

    def register_connection(self, connection: http.client.HTTPConnection) -> None:
        self._register_network_resource(
            lambda: self._interrupt_connection(connection)
        )

    def register_response(self, response: Any) -> None:
        self._register_network_resource(lambda: self._interrupt_response(response))

    def _register_network_resource(self, close_resource: Callable[[], None]) -> None:
        with self._lock:
            if self._cancelled:
                close = True
            else:
                self._network_closers.append(close_resource)
                close = False
        if close:
            try:
                close_resource()
            except OSError:
                pass

    def clear_connection(self) -> None:
        with self._lock:
            self._network_closers.clear()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            process = self._process
            close_network = tuple(self._network_closers)
        for close_resource in close_network:
            try:
                close_resource()
            except OSError:
                pass
        if process is not None:
            self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        terminate_process_group(process, timeout=1)

    @classmethod
    def _interrupt_connection(cls, connection: http.client.HTTPConnection) -> None:
        cls._interrupt_socket(getattr(connection, "sock", None))
        connection.close()

    @classmethod
    def _interrupt_response(cls, response: Any) -> None:
        file_object = getattr(response, "fp", None)
        raw = getattr(file_object, "raw", file_object)
        cls._interrupt_socket(getattr(raw, "_sock", None))
        response.close()

    @staticmethod
    def _interrupt_socket(sock: Any) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class _GitHubAppCredentialProvider:
    """Mint App credentials with a channel-owned, cancellable signer."""

    def __init__(self, profile: GitHubAppProfile) -> None:
        self._profile = profile
        self._signing_processes = _SigningProcessRegistry()
        self._call_condition = threading.Condition()
        self._call_active = False
        self._cancelled = False

    def __call__(self) -> ReadCredential:
        with self._call_condition:
            if self._cancelled:
                raise GitHubCredentialError("GitHub App credential provider is closed")
            self._call_active = True
        try:
            return mint_read_only_installation_credential(
                self._profile,
                process_registry=self._signing_processes,
            )
        finally:
            with self._call_condition:
                self._call_active = False
                self._call_condition.notify_all()

    def cancel(self) -> None:
        with self._call_condition:
            self._cancelled = True
        self._signing_processes.cancel()
        with self._call_condition:
            self._call_condition.wait_for(
                lambda: not self._call_active,
                timeout=WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS,
            )


_REQUIRED_READ_PERMISSIONS = {
    "actions": "read",
    "checks": "read",
    "contents": "read",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read",
    "statuses": "read",
}


def mint_read_only_installation_token(profile: GitHubAppProfile) -> str:
    return mint_read_only_installation_credential(profile).token


def mint_read_only_installation_credential(
    profile: GitHubAppProfile,
    *,
    process_registry: _SigningProcessRegistry | None = None,
) -> ReadCredential:
    app_id = profile.app_id
    installation_id = profile.installation_id
    try:
        private_key = profile.private_key_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GitHubCredentialError("GitHub App private key is unavailable") from error
    if process_registry is None:
        app_jwt = _create_app_jwt(app_id, private_key)
    else:
        app_jwt = _create_app_jwt(
            app_id,
            private_key,
            process_registry=process_registry,
        )
    if process_registry is not None and process_registry.is_cancelled():
        raise GitHubCredentialError("GitHub App credential provider is closed")
    request = urllib.request.Request(
        (
            "https://api.github.com/app/installations/"
            f"{installation_id}/access_tokens"
        ),
        data=json.dumps(
            {"permissions": _REQUIRED_READ_PERMISSIONS}
        ).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {app_jwt}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-run",
        },
    )
    try:
        with _open_installation_token_request(request, process_registry) as response:
            loaded: object = json.load(response)
    except urllib.error.HTTPError as error:
        raise GitHubCredentialError(
            "could not create a GitHub App installation token",
            http_status=error.code,
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise GitHubCredentialError(
            "could not create a GitHub App installation token"
        ) from error
    finally:
        if process_registry is not None:
            process_registry.clear_connection()
    if not isinstance(loaded, dict):
        raise GitHubCredentialError("GitHub token response is invalid")
    token = loaded.get("token")
    expires_at = loaded.get("expires_at")
    permissions = loaded.get("permissions")
    if not isinstance(token, str) or not token.strip():
        raise GitHubCredentialError("GitHub token response has no token")
    if not isinstance(expires_at, str):
        raise GitHubCredentialError("GitHub token response has no expiry")
    if permissions != _REQUIRED_READ_PERMISSIONS:
        raise GitHubCredentialError(
            "GitHub did not grant the exact worker read permissions"
        )
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
    except ValueError as error:
        raise GitHubCredentialError("GitHub token response has an invalid expiry") from error
    if expiry <= time.time():
        raise GitHubCredentialError("GitHub token response has an expired token")
    return ReadCredential(token=token, expires_at=expiry)


class _CancellableHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, process_registry: _SigningProcessRegistry) -> None:
        self._context_for_open = ssl.create_default_context()
        super().__init__(context=self._context_for_open)
        self._process_registry = process_registry

    def https_open(self, request: urllib.request.Request) -> Any:
        return self.do_open(
            self._open_connection,
            request,
            context=self._context_for_open,
        )

    def _open_connection(
        self, host: str, **options: Any
    ) -> http.client.HTTPSConnection:
        connection = _CancellableHTTPSConnection(
            self._process_registry,
            host,
            **options,
        )
        self._process_registry.register_connection(connection)
        return connection


class _CancellableHTTPSConnection(http.client.HTTPSConnection):
    """Refuse to establish a transport after its credential channel closes."""

    def __init__(
        self,
        process_registry: _SigningProcessRegistry,
        host: str,
        **options: Any,
    ) -> None:
        super().__init__(host, **options)
        self._process_registry = process_registry

    def connect(self) -> None:
        self._process_registry.raise_if_cancelled()
        try:
            super().connect()
        except OSError:
            if self._process_registry.is_cancelled():
                _SigningProcessRegistry._interrupt_connection(self)
            raise
        if self._process_registry.is_cancelled():
            _SigningProcessRegistry._interrupt_connection(self)
            raise OSError("GitHub App credential provider is closed")


def _open_installation_token_request(
    request: urllib.request.Request,
    process_registry: _SigningProcessRegistry | None,
) -> Any:
    timeout = WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS
    if process_registry is None or urllib.request.urlopen is not _DEFAULT_URL_OPEN:
        return urllib.request.urlopen(request, timeout=timeout)
    opener = urllib.request.build_opener(_CancellableHTTPSHandler(process_registry))
    response = opener.open(request, timeout=timeout)
    process_registry.register_response(response)
    return response


def _create_app_jwt(
    app_id: str,
    private_key: str,
    *,
    process_registry: _SigningProcessRegistry | None = None,
) -> str:
    now = int(time.time())
    header = _base64url({"alg": "RS256", "typ": "JWT"})
    payload = _base64url({"iat": now - 60, "exp": now + 540, "iss": app_id})
    signing_input = f"{header}.{payload}"
    key_fd: int | None = None
    try:
        key_fd = os.memfd_create("agent-run-app-key", os.MFD_CLOEXEC)
        os.write(key_fd, private_key.encode())
        os.lseek(key_fd, 0, os.SEEK_SET)
        command = [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            f"/proc/self/fd/{key_fd}",
        ]
        if process_registry is None:
            signed = subprocess.run(
                command,
                input=signing_input.encode(),
                capture_output=True,
                check=False,
                pass_fds=(key_fd,),
                timeout=WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS,
            )
        else:
            signed = _run_cancellable_signing(
                command,
                signing_input.encode(),
                key_fd=key_fd,
                process_registry=process_registry,
            )
        if signed.returncode != 0:
            raise GitHubCredentialError("could not sign GitHub App JWT")
        signature = base64.urlsafe_b64encode(signed.stdout).rstrip(b"=").decode()
        return f"{signing_input}.{signature}"
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitHubCredentialError("OpenSSL is required to sign App JWTs") from error
    finally:
        if key_fd is not None:
            os.close(key_fd)


def _run_cancellable_signing(
    command: list[str],
    input_data: bytes,
    *,
    key_fd: int,
    process_registry: _SigningProcessRegistry,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(key_fd,),
        start_new_session=True,
    )
    process_registry.register(process)
    try:
        try:
            stdout, stderr = process.communicate(
                input=input_data,
                timeout=WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS,
            )
        except BaseException:
            process_registry._terminate(process)  # noqa: SLF001
            raise
    finally:
        process_registry.unregister(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _base64url(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, separators=(",", ":"), sort_keys=True
    ).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()
