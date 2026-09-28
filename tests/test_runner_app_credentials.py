"""App configuration and renewable program transport with external-tool substitutes."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run import runner_credentials
from agent_run.credential_lifecycle import ReadCredential
from agent_run.github_identity import GitHubIdentityError, resolve_runner_identity, resolve_worker_identity
from agent_run.runner_credentials import runner_credential_scope
from agent_run.runner_transport import runner_environment, runner_git_environment
from support.user_defaults import settings


def config(tmp_path: Path) -> dict:
    return {"runner_source": "app", "worker_source": "app", "app_read_only": True,
            "app_current_repository_only": True,
            "app": {"app_id": "123", "installation_id": "456", "private_key_path": str(tmp_path / "key")}}


def test_settings_runner_app_shares_definition_without_worker_scope(tmp_path: Path) -> None:
    chosen = config(tmp_path)
    code, result = settings("configure", "--runner-github-source", "app", "--worker-github-source", "app",
                            "--github-app-id", "123", "--github-installation-id", "456",
                            "--github-private-key-path", chosen["app"]["private_key_path"],
                            "--github-app-read-only", "--github-app-current-repository-only")
    assert code == 0, result
    code, result = settings("show")
    assert code == 0
    assert result["github"]["runner"] == resolve_runner_identity(chosen)
    assert result["github"]["worker"] == resolve_worker_identity(chosen)
    assert result["github"]["runner"]["app_read_only"] is False
    assert result["github"]["runner"]["app_current_repository_only"] is False


def test_runner_api_git_consume_renewal_and_cleanup(tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    issued = threading.Event()
    cancelled = threading.Event()
    now = [time.time()]
    calls = []

    class Provider:
        def __init__(self, profile, **scope):
            calls.append((profile, scope))
            self.count = 0
        def __call__(self):
            self.count += 1
            if self.count > 1:
                issued.set()
            return ReadCredential(f"app-{self.count}", now[0] + 1000)
        def cancel(self):
            cancelled.set()

    monkeypatch.setattr(runner_credentials, "_GitHubAppCredentialProvider", Provider)
    monkeypatch.setattr(runner_credentials, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    identity = resolve_runner_identity(config(tmp_path))
    with runner_credential_scope():
        assert runner_environment(identity)["GH_TOKEN"] == "app-1"
        pool = runner_credentials._scope.get()
        assert pool is not None
        credential = next(iter(pool.values()))
        credential.lifecycle._clock = lambda: now[0]
        now[0] += 701
        credential.lifecycle.wake()
        assert issued.wait(2)
        # Delivery callback and provider return share the lifecycle condition.
        with credential.lifecycle._condition:
            assert credential.lifecycle._condition.wait_for(
                lambda: credential.token() == "app-2", timeout=2,
            )
        assert runner_environment(identity)["GH_TOKEN"] == "app-2"
        git_env = runner_git_environment(identity, cwd=git_repo)
        assert git_env["GH_TOKEN"] == "app-2"
        assert git_env["GIT_ALLOW_PROTOCOL"] == "https"
        assert len(calls) == 1
        assert calls[0][1] == {}  # Installation defaults; no Worker restrictions.
    assert cancelled.is_set()
    assert not credential.lifecycle._renewal_thread.is_alive()
    assert not credential.lifecycle._expiry_thread.is_alive()
    with pytest.raises(GitHubIdentityError):
        credential.token()


def test_runner_expired_app_never_uses_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time()]
    cancelled = threading.Event()
    class Provider:
        def __init__(self, profile):
            pass
        def __call__(self):
            return ReadCredential("app-token", now[0] + 1000)
        def cancel(self):
            cancelled.set()
    monkeypatch.setattr(runner_credentials, "_GitHubAppCredentialProvider", Provider)
    monkeypatch.setattr(runner_credentials, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    identity = resolve_runner_identity(config(tmp_path))
    with runner_credential_scope():
        assert runner_environment(identity)["GH_TOKEN"] == "app-token"
        now[0] += 1001
        with pytest.raises(GitHubIdentityError, match="resume"):
            runner_environment(identity)
    assert cancelled.is_set()


def test_concurrent_worker_and_runner_scopes_do_not_share_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from agent_run import github_auth
    from agent_run.github_auth_profile import GitHubAppProfile
    from agent_run.worker_credentials import NativeCredentialDelivery

    requested = []
    def mint(profile, *, read_only=False, repository=None, process_registry=None):
        requested.append((read_only, repository))
        return ReadCredential("worker-read" if read_only else "runner-write", time.time() + 3600)
    monkeypatch.setattr(github_auth, "mint_installation_credential", mint)
    identity = resolve_runner_identity(config(tmp_path))
    profile = identity["app_profile"]
    provider = github_auth._GitHubAppCredentialProvider(
        GitHubAppProfile(profile["app_id"], profile["installation_id"], Path(profile["private_key_path"])),
        read_only=True, repository="example/project",
    )
    with runner_credential_scope(), NativeCredentialDelivery(provider, tmp_path / "worker") as worker:
        worker.start()
        assert runner_environment(identity)["GH_TOKEN"] == "runner-write"
        assert json.loads((worker.directory / "hosts.yml").read_text())["github.com"]["oauth_token"] == "worker-read"
        worker.close()
        assert runner_environment(identity)["GH_TOKEN"] == "runner-write"
    assert set(requested) == {(True, "example/project"), (False, None)}
    assert not (worker.directory / "hosts.yml").exists()
    worker.directory.rmdir()


def test_runner_transient_renewal_keeps_valid_token_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.github_auth import GitHubCredentialError

    now = [time.time()]
    failed = threading.Event()
    renewed = threading.Event()
    class Provider:
        def __init__(self, profile):
            self.calls = 0
        def __call__(self):
            self.calls += 1
            if self.calls == 2:
                raise GitHubCredentialError("temporary", http_status=503)
            if self.calls == 3:
                renewed.set()
            return ReadCredential(f"app-{self.calls}", now[0] + 1000)
        def cancel(self):
            pass
    monkeypatch.setattr(runner_credentials, "_GitHubAppCredentialProvider", Provider)
    monkeypatch.setattr(runner_credentials, "time", SimpleNamespace(time=lambda: now[0]))
    identity = resolve_runner_identity(config(tmp_path))
    with runner_credential_scope():
        assert runner_environment(identity)["GH_TOKEN"] == "app-1"
        pool = runner_credentials._scope.get()
        assert pool is not None
        credential = next(iter(pool.values()))
        def clock():
            current = now[0]
            if credential.lifecycle._attempts:
                failed.set()
            return current
        credential.lifecycle._clock = clock
        now[0] += 701
        credential.lifecycle.wake()
        assert failed.wait(2)
        assert runner_environment(identity)["GH_TOKEN"] == "app-1"
        now[0] += 2
        credential.lifecycle.wake()
        assert renewed.wait(2)
        with credential.lifecycle._condition:
            assert credential.lifecycle._condition.wait_for(
                lambda: credential.token() == "app-3", timeout=2,
            )
        assert runner_environment(identity)["GH_TOKEN"] == "app-3"


@pytest.mark.parametrize("interrupt", [False, True])
def test_runner_initial_failure_cleans_provider_and_preserves_safe_status(tmp_path, monkeypatch, interrupt):
    from agent_run.github_auth import GitHubCredentialError

    cancelled = threading.Event()

    class Provider:
        def __init__(self, profile):
            pass
        def __call__(self):
            if interrupt:
                raise KeyboardInterrupt()
            raise GitHubCredentialError("sensitive response", http_status=403)
        def cancel(self):
            cancelled.set()

    monkeypatch.setattr(runner_credentials, "_GitHubAppCredentialProvider", Provider)
    with runner_credential_scope():
        if interrupt:
            with pytest.raises(KeyboardInterrupt):
                runner_environment(resolve_runner_identity(config(tmp_path)))
        else:
            with pytest.raises(GitHubIdentityError, match="HTTP 403") as captured:
                runner_environment(resolve_runner_identity(config(tmp_path)))
            assert "sensitive response" not in str(captured.value)
    assert cancelled.is_set()
