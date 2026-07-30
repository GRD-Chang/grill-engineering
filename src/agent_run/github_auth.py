from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class GitHubCredentialError(RuntimeError):
    pass


_REQUIRED_READ_PERMISSIONS = {
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read",
}


def mint_read_only_installation_token() -> str:
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
        with urllib.request.urlopen(request, timeout=15) as response:
            loaded: object = json.load(response)
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        raise GitHubCredentialError(
            "could not create a GitHub App installation token"
        ) from error
    if not isinstance(loaded, dict):
        raise GitHubCredentialError("GitHub token response is invalid")
    token = loaded.get("token")
    permissions = loaded.get("permissions")
    if not isinstance(token, str) or not token.strip():
        raise GitHubCredentialError("GitHub token response has no token")
    if permissions != _REQUIRED_READ_PERMISSIONS:
        raise GitHubCredentialError(
            "GitHub did not grant the exact worker read permissions"
        )
    return token


def _create_app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    header = _base64url({"alg": "RS256", "typ": "JWT"})
    payload = _base64url({"iat": now - 60, "exp": now + 540, "iss": app_id})
    signing_input = f"{header}.{payload}"
    key_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="agent-run-app-key-",
            delete=False,
        ) as key_file:
            key_file.write(private_key)
            key_path = Path(key_file.name)
        key_path.chmod(0o600)
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
            input=signing_input.encode(),
            capture_output=True,
            check=False,
        )
        if signed.returncode != 0:
            raise GitHubCredentialError(
                signed.stderr.decode(errors="replace").strip()
                or "could not sign GitHub App JWT"
            )
        signature = base64.urlsafe_b64encode(signed.stdout).rstrip(b"=").decode()
        return f"{signing_input}.{signature}"
    except OSError as error:
        raise GitHubCredentialError("OpenSSL is required to sign App JWTs") from error
    finally:
        if key_path is not None:
            key_path.unlink(missing_ok=True)


def _base64url(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, separators=(",", ":"), sort_keys=True
    ).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()
