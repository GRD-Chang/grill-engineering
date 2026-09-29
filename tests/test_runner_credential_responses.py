"""Classify real App response parsing through initial issuance and renewal."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any
import urllib.error

import pytest

from agent_run import github_auth, runner_credentials
from agent_run.github import GitHubReadError
from agent_run.runner_credentials import RunnerCredentialError, runner_credential_scope
from agent_run.runner_transport import runner_environment


@pytest.mark.parametrize("renewal", [False, True], ids=["initial", "renewal"])
@pytest.mark.parametrize(
    ("response", "shared"),
    [
        ("<html>gateway error</html>", False),
        ("[]", False),
        ("{}", False),
        ('{"token":"test-token"}', False),
        ('{"token":"test-token","expires_at":"invalid"}', False),
        (401, True),
        (403, True),
        (404, False),
        (503, False),
        ("missing-key", True),
        ("invalid-key", True),
    ],
)
def test_runner_app_response_failure_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    response: str | int, shared: bool, renewal: bool,
) -> None:
    now = [time.time()]
    failed = threading.Event()
    failing = [not renewal]
    key = tmp_path / "app-key.pem"
    key.write_text("invalid-private-key")
    key.chmod(0o600)
    original_sign = github_auth._create_app_jwt

    def sign(*args: Any, **kwargs: Any) -> str:
        if failing[0] and response == "invalid-key":
            return original_sign(*args, **kwargs)
        return "test-jwt"

    def open_response(request: Any, **_kwargs: Any) -> io.BytesIO:
        if failing[0]:
            if isinstance(response, int):
                raise urllib.error.HTTPError(request.full_url, response, "private response", None, None)
            return io.BytesIO(response.encode())
        expiry = datetime.fromtimestamp(now[0] + 1000, timezone.utc).isoformat()
        return io.BytesIO(json.dumps({"token": "test-token", "expires_at": expiry}).encode())

    monkeypatch.setattr(github_auth, "_create_app_jwt", sign)
    monkeypatch.setattr(github_auth.urllib.request, "urlopen", open_response)
    monkeypatch.setattr(runner_credentials, "time", SimpleNamespace(time=lambda: now[0]))
    identity = {
        "source": "app", "app_read_only": False, "app_current_repository_only": False,
        "app_profile": {"app_id": "123", "installation_id": "456", "private_key_path": str(key)},
    }
    credential = None
    with runner_credential_scope():
        if renewal:
            assert runner_environment(identity)["GH_TOKEN"] == "test-token"
            pool = runner_credentials._scope.get()
            assert pool is not None
            credential = next(iter(pool.values()))

            def clock() -> float:
                if credential.lifecycle._attempts:
                    failed.set()
                return now[0]

            credential.lifecycle._clock = clock
            failing[0] = True
        if response == "missing-key":
            key.unlink()
        if credential is not None:
            now[0] += 701
            credential.lifecycle.wake()
            assert failed.wait(2), "real provider did not finish the failed renewal"
            assert runner_environment(identity)["GH_TOKEN"] == "test-token"
            now[0] += 300
        expected = RunnerCredentialError if shared else GitHubReadError
        with pytest.raises(expected) as caught:
            runner_environment(identity)
        if not shared:
            assert caught.value.code == "runner_credential_unavailable"
        for secret in ("test-token", "test-jwt", "invalid-private-key", "private response"):
            assert secret not in str(caught.value)
    if credential is not None:
        assert not credential.lifecycle._renewal_thread.is_alive()
        assert not credential.lifecycle._expiry_thread.is_alive()
