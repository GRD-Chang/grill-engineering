import json
from pathlib import Path
import subprocess

import pytest

from agent_run.github_identity import (
    GitHubIdentityError, native_github_environment, normalize_github_config,
    resolve_worker_identity, validate_identity_snapshot, validate_worker_auth,
)
from agent_run.user_defaults import UserDefaultsStore
from support.user_defaults import settings


@pytest.mark.parametrize('config', [
    {'worker_source': 'invalid'}, {'pat': {}}, {'pat': {'env': 'X', 'file': '/x'}},
    {'pat': {'env': 'not-valid'}}, {'pat': {'file': 'relative'}},
    {'worker_source': 'host', 'read_only': True}, {'pat': {'token': 'secret'}},
])
def test_invalid_configuration_is_explicit(config: dict) -> None:
    with pytest.raises(GitHubIdentityError):
        normalize_github_config(config)


def test_defaults_overrides_and_query_do_not_consume_pat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = UserDefaultsStore(tmp_path / 'user-defaults.json')
    configured = store.configure(github={'worker_source': 'pat', 'pat': {'env': 'TEST_PAT'}})
    original = store.path.read_bytes()
    assert configured['github']['worker'] == {'source': 'pat', 'pat': {'env': 'TEST_PAT'}}
    assert resolve_worker_identity(store.load()['github'], {'worker_source': 'host'}) == {'source': 'host'}
    assert store.describe()['github']['runner'] == {'source': 'host'}
    assert store.path.read_bytes() == original
    with pytest.raises(GitHubIdentityError, match='PAT'):
        native_github_environment(configured['github']['worker'], {})
    monkeypatch.setenv('TEST_PAT', 'secret-value')
    assert 'secret-value' not in json.dumps(store.describe())


def test_explicit_selection_bypasses_broken_legacy_app(tmp_path: Path) -> None:
    broken = tmp_path / 'github-app.json'
    broken.write_text('broken')
    assert resolve_worker_identity({'worker_source': 'host'}, app_profile_path=broken) == {'source': 'host'}
    assert resolve_worker_identity({'worker_source': 'pat', 'pat': {'env': 'PAT'}}, app_profile_path=broken)['source'] == 'pat'
    with pytest.raises(ValueError):
        resolve_worker_identity(app_profile_path=broken)


def test_pat_references_refresh_and_never_fallback(tmp_path: Path) -> None:
    token_file = tmp_path / 'pat'
    identity = {'source': 'pat', 'pat': {'file': str(token_file)}}
    with pytest.raises(GitHubIdentityError):
        native_github_environment(identity, {'GH_TOKEN': 'host'})
    for value in ('', ' \n'):
        token_file.write_text(value)
        with pytest.raises(GitHubIdentityError):
            native_github_environment(identity, {'GH_TOKEN': 'host'})
    for token in ('first', 'replacement'):
        token_file.write_text(token + '\n')
        environment = native_github_environment(identity, {'GITHUB_TOKEN': 'host', 'GH_ENTERPRISE_TOKEN': 'other'})
        assert environment['GH_TOKEN'] == token
        assert 'GITHUB_TOKEN' not in environment
        assert 'GH_ENTERPRISE_TOKEN' not in environment
    assert native_github_environment({'source': 'pat', 'pat': {'env': 'GITHUB_TOKEN'}}, {'GITHUB_TOKEN': 'chosen'})['GH_TOKEN'] == 'chosen'
    with pytest.raises(GitHubIdentityError):
        validate_identity_snapshot({'source': 'host', 'token': 'secret'})


def test_auth_validation_suppresses_secret_tool_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(command: list, **kwargs: object) -> subprocess.CompletedProcess:
        assert command == ['gh', 'api', '--hostname', 'github.com', 'user']
        assert kwargs['env']['GH_TOKEN'] == 'selected-secret'
        assert kwargs['stderr'] == subprocess.DEVNULL
        return subprocess.CompletedProcess(command, 1, stderr='selected-secret')
    monkeypatch.setattr(subprocess, 'run', failed)
    with pytest.raises(GitHubIdentityError) as error:
        validate_worker_auth({'source': 'pat', 'pat': {'env': 'PAT'}}, {'PAT': 'selected-secret'})
    assert 'selected-secret' not in str(error.value)


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_public_settings_pat_file_and_env(language: str, tmp_path: Path) -> None:
    code, configured = settings('configure', '--language', language, '--worker-github-source', 'pat', '--github-pat-env', 'UNSET_PAT')
    assert code == 0
    assert configured['github']['worker'] == {'source': 'pat', 'pat': {'env': 'UNSET_PAT'}}
    code, configured = settings('configure', '--github-pat-file', str(tmp_path / 'missing'))
    assert code == 0
    assert configured['github']['worker']['pat'] == {'file': str(tmp_path / 'missing')}
    code, shown = settings('show')
    assert code == 0
    assert shown['github'] == configured['github']
    code, configured = settings('configure', '--worker-github-source', 'host')
    assert code == 0
    assert configured['github']['worker'] == {'source': 'host'}


def test_pat_file_read_is_bounded_and_rejects_nonregular(tmp_path: Path) -> None:
    import os

    path = tmp_path / "pat"
    identity = {"source": "pat", "pat": {"file": str(path)}}
    os.mkfifo(path)
    with pytest.raises(GitHubIdentityError):
        native_github_environment(identity, {})
    path.unlink()
    path.write_text("x" * 65537)
    with pytest.raises(GitHubIdentityError):
        native_github_environment(identity, {})
