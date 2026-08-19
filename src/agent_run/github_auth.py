from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from datetime import datetime
import urllib.error
import urllib.request
from typing import Any

from agent_run.worker_credentials import (
    ReadCredential,
    WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS,
    safe_http_status,
)


class GitHubCredentialError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = safe_http_status(http_status)


_REQUIRED_READ_PERMISSIONS = {
    "actions": "read",
    "checks": "read",
    "contents": "read",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read",
    "statuses": "read",
}


def mint_read_only_installation_token() -> str:
    return mint_read_only_installation_credential().token


def mint_read_only_installation_credential() -> ReadCredential:
    app_id = os.environ.get("AGENT_RUN_GITHUB_APP_ID", "").strip()
    installation_id = os.environ.get(
        "AGENT_RUN_GITHUB_APP_INSTALLATION_ID", ""
    ).strip()
    private_key = os.environ.get(
        "AGENT_RUN_GITHUB_APP_PRIVATE_KEY", ""
    ).strip()
    if not app_id or not installation_id.isdigit() or not private_key:
        raise GitHubCredentialError(
            "GitHub App ID, installation ID, and private key are required"
        )
    app_jwt = _create_app_jwt(app_id, private_key)
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
        with urllib.request.urlopen(
            request, timeout=WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS
        ) as response:
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


def _create_app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    header = _base64url({"alg": "RS256", "typ": "JWT"})
    payload = _base64url({"iat": now - 60, "exp": now + 540, "iss": app_id})
    signing_input = f"{header}.{payload}"
    key_fd: int | None = None
    try:
        key_fd = os.memfd_create("agent-run-app-key", os.MFD_CLOEXEC)
        os.write(key_fd, private_key.encode())
        os.lseek(key_fd, 0, os.SEEK_SET)
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", f"/proc/self/fd/{key_fd}"],
            input=signing_input.encode(),
            capture_output=True,
            check=False,
            pass_fds=(key_fd,),
            timeout=WORKER_CREDENTIAL_PROVIDER_OPERATION_TIMEOUT_SECONDS,
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


def _base64url(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, separators=(",", ":"), sort_keys=True
    ).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()
