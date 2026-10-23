"""Shared identity failures retain their scope without guessing from stderr."""
from __future__ import annotations

from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run import runner_credentials
from agent_run.credential_lifecycle import ReadCredential
from agent_run.github import GitHubReadError
from agent_run.github_auth import GitHubCredentialError
from agent_run.runner_credentials import RunnerCredentialError, runner_credential_scope
from agent_run.runner_transport import runner_environment


@pytest.mark.parametrize(
    ("failure", "shared"),
    [
        (GitHubCredentialError("private response", http_status=401), True),
        (GitHubCredentialError("private response", http_status=403), True),
        (GitHubCredentialError("invalid local key", authorization_failed=True), True),
        (GitHubCredentialError("private response", http_status=503), False),
        (GitHubCredentialError("private response", retryable=True), False),
        (GitHubCredentialError("unknown non-retryable response", retryable=False), False),
        (OSError("unknown connection error"), False),
    ],
)
def test_runner_initial_credential_failure_preserves_structured_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception, shared: bool,
) -> None:
    cancelled: list[bool] = []

    class Provider:
        def __init__(self, _profile: Any) -> None:
            pass

        def __call__(self) -> None:
            raise failure

        def cancel(self) -> None:
            cancelled.append(True)

    monkeypatch.setattr(runner_credentials, "_GitHubAppCredentialProvider", Provider)
    identity = {
        "source": "app", "app_read_only": False, "app_current_repository_only": False,
        "app_profile": {"app_id": "123", "installation_id": "456",
                        "private_key_path": str(tmp_path / "key")},
    }
    expected = RunnerCredentialError if shared else GitHubReadError
    with runner_credential_scope(), pytest.raises(expected) as caught:
        runner_environment(identity)
    assert cancelled
    assert "private response" not in str(caught.value)
    if not shared:
        assert caught.value.code == "runner_credential_unavailable"


def test_missing_shared_pat_is_a_confirmed_runner_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_RUNNER_PAT", raising=False)
    with pytest.raises(RunnerCredentialError):
        runner_environment({"source": "pat", "pat": {"env": "TEST_RUNNER_PAT"}})


@pytest.mark.parametrize("authorization_failed", [False, True])
def test_expired_runner_token_retains_renewal_failure_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authorization_failed: bool,
) -> None:
    now = [time.time()]
    observed = threading.Event()

    class Provider:
        def __init__(self, _profile: Any) -> None:
            self.calls = 0

        def __call__(self) -> ReadCredential:
            self.calls += 1
            if self.calls > 1:
                raise GitHubCredentialError("unavailable", authorization_failed=authorization_failed)
            return ReadCredential("test-token", now[0] + 1000)

        def cancel(self) -> None:
            pass

    monkeypatch.setattr(runner_credentials, "_GitHubAppCredentialProvider", Provider)
    monkeypatch.setattr(runner_credentials, "time", SimpleNamespace(time=lambda: now[0]))
    identity = {
        "source": "app", "app_read_only": False, "app_current_repository_only": False,
        "app_profile": {"app_id": "123", "installation_id": "456",
                        "private_key_path": str(tmp_path / "key")},
    }
    with runner_credential_scope():
        assert runner_environment(identity)["GH_TOKEN"] == "test-token"
        credentials = runner_credentials._scope.get()
        assert credentials is not None
        credential = next(iter(credentials.values()))

        def clock() -> float:
            if credential.lifecycle._attempts:
                observed.set()
            return now[0]

        credential.lifecycle._clock = clock
        now[0] += 701
        credential.lifecycle.wake()
        assert observed.wait(2)
        assert runner_environment(identity)["GH_TOKEN"] == "test-token"
        now[0] += 300
        expected = RunnerCredentialError if authorization_failed else GitHubReadError
        with pytest.raises(expected):
            runner_environment(identity)
    assert not credential.lifecycle._renewal_thread.is_alive()
    assert not credential.lifecycle._expiry_thread.is_alive()








def test_expiry_during_inflight_renewal_does_not_prove_authorization_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [time.time()]
    entered, release = threading.Event(), threading.Event()

    class Provider:
        def __init__(self, _profile: Any) -> None:
            self.calls = 0

        def __call__(self) -> ReadCredential:
            self.calls += 1
            if self.calls > 1:
                entered.set()
                assert release.wait(5)
                raise OSError('transport unavailable')
            return ReadCredential('test-token', now[0] + 1000)

        def cancel(self) -> None:
            release.set()

    monkeypatch.setattr(runner_credentials, '_GitHubAppCredentialProvider', Provider)
    monkeypatch.setattr(runner_credentials, 'time', SimpleNamespace(time=lambda: now[0]))
    identity = {
        'source': 'app', 'app_read_only': False, 'app_current_repository_only': False,
        'app_profile': {'app_id': '123', 'installation_id': '456',
                        'private_key_path': str(tmp_path / 'key')},
    }
    with runner_credential_scope():
        runner_environment(identity)
        credential = next(iter(runner_credentials._scope.get().values()))
        credential.lifecycle._clock = lambda: now[0]
        now[0] += 701
        credential.lifecycle.wake()
        try:
            assert entered.wait(5)
            now[0] += 300
            with pytest.raises(GitHubReadError) as caught:
                runner_environment(identity)
            assert caught.value.code == 'runner_credential_unavailable'
        finally:
            release.set()
    assert not credential.lifecycle._renewal_thread.is_alive()
    assert not credential.lifecycle._expiry_thread.is_alive()
