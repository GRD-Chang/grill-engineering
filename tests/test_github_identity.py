import json
from pathlib import Path
import subprocess

import pytest

from agent_run.github_identity import (
    GitHubIdentityError, describe_identity, native_github_environment, normalize_github_config,
    resolve_worker_identity, resolve_runner_identity, validate_identity_snapshot, validate_worker_auth,
)
from agent_run.user_defaults import UserDefaultsStore
from support.user_defaults import settings


@pytest.mark.parametrize('config', [
    {'worker_source': 'invalid'}, {'runner_source': 'app'}, {'runner_source': None}, {'pat': {}}, {'pat': {'env': 'X', 'file': '/x'}},
    {'pat': {'env': 'not-valid'}}, {'pat': {'file': 'relative'}},
    {'worker_source': 'host', 'read_only': True}, {'pat': {'token': 'secret'}},
    {'app_read_only': 'true'}, {'app_current_repository_only': 1},
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


def test_runner_defaults_shared_reference_and_independent_override(tmp_path: Path) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    configured = store.configure(github={"worker_source": "host", "runner_source": "pat",
                                         "pat": {"env": "RUNNER_PAT"}})
    saved = store.path.read_bytes()
    runner = configured["github"]["runner"]
    assert runner == {"source": "pat", "pat": {"env": "RUNNER_PAT"}}
    assert configured["github"]["worker"] == {"source": "host"}
    assert resolve_runner_identity() == {"source": "host"}
    defaults = store.load()["github"]
    assert resolve_runner_identity(defaults, {"runner_source": "host"}) == {"source": "host"}
    assert resolve_worker_identity(defaults, {"worker_source": "pat"}) == runner
    assert store.path.read_bytes() == saved
    with pytest.raises(GitHubIdentityError):
        resolve_runner_identity({"runner_source": "pat"})
    with pytest.raises(GitHubIdentityError):
        native_github_environment(runner, {"GH_TOKEN": "host-secret"})
    assert native_github_environment(runner, {"RUNNER_PAT": "new-secret"})["GH_TOKEN"] == "new-secret"


@pytest.mark.parametrize("language", ["zh", "en"])
def test_public_settings_runner_selection(language: str, tmp_path: Path) -> None:
    code, configured = settings("configure", "--language", language,
                                "--worker-github-source", "host", "--runner-github-source", "pat",
                                "--github-pat-env", "UNSET_RUNNER_PAT")
    assert code == 0
    assert configured["github"]["runner"] == {"source": "pat", "pat": {"env": "UNSET_RUNNER_PAT"}}
    assert configured["github"]["worker"] == {"source": "host"}
    code, updated = settings("configure", "--github-pat-file", str(tmp_path / "missing"))
    assert code == 0
    assert updated["github"]["runner"]["pat"] == {"file": str(tmp_path / "missing")}
    code, shown = settings("show")
    assert code == 0
    assert shown["github"] == updated["github"]
    code, changed = settings("configure", "--runner-github-source", "host")
    assert code == 0
    assert changed["github"]["runner"] == {"source": "host"}
    assert changed["github"]["worker"] == {"source": "host"}


def test_identity_description_never_guesses_missing_runner_binding() -> None:
    worker = {"source": "host"}
    assert describe_identity(worker, "zh")["runner"] is None
    runner = {"source": "pat", "pat": {"env": "SAVED_PAT"}}
    assert describe_identity(worker, "en", runner)["runner"] == runner




def app_config(tmp_path: Path) -> dict:
    return {'worker_source': 'app', 'app': {
        'app_id': '123', 'installation_id': '456', 'private_key_path': str(tmp_path / 'key.pem')}}


@pytest.mark.parametrize('read_only,current_only', [(False, False), (True, False), (False, True), (True, True)])
def test_app_scope_and_snapshot_are_independent(tmp_path: Path, read_only: bool, current_only: bool) -> None:
    config = app_config(tmp_path)
    frozen = resolve_worker_identity(config, {'app_read_only': read_only, 'app_current_repository_only': current_only})
    assert frozen == {'source': 'app', 'app_profile': config['app'],
                      'app_read_only': read_only, 'app_current_repository_only': current_only}
    assert validate_identity_snapshot(frozen) == frozen
    assert 'app_read_only' not in config
    with pytest.raises(GitHubIdentityError):
        validate_identity_snapshot({'source': 'app', 'app_profile': config['app']})
    with pytest.raises(GitHubIdentityError):
        resolve_worker_identity(config, {'worker_source': 'host', 'app_read_only': True})


def test_public_settings_app_metadata_scope_and_read_only_query(tmp_path: Path) -> None:
    code, configured = settings('configure', '--worker-github-source', 'app',
        '--github-app-id', '123', '--github-installation-id', '456',
        '--github-private-key-path', str(tmp_path / 'unread-key'), '--github-app-read-only')
    assert code == 0
    worker = configured['github']['worker']
    assert worker['app_read_only'] is True
    assert worker['app_current_repository_only'] is False
    before = UserDefaultsStore().path.read_bytes()
    assert settings('show')[1]['github']['worker'] == worker
    assert UserDefaultsStore().path.read_bytes() == before
    selected = resolve_worker_identity(UserDefaultsStore().load()['github'], {'app_current_repository_only': True})
    assert selected['app_current_repository_only'] is True
    assert UserDefaultsStore().path.read_bytes() == before
    code, configured = settings('configure', '--no-github-app-read-only', '--github-app-current-repository-only')
    assert code == 0
    assert configured['github']['worker']['app_read_only'] is False
    assert configured['github']['worker']['app_current_repository_only'] is True


def test_legacy_app_migration_is_atomic_preserves_materials_and_explicit_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = tmp_path / 'private-key'
    key.write_text('private material is never shown')
    key.chmod(0o600)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    directory = tmp_path / 'config' / 'agent-run'
    directory.mkdir(mode=0o700, parents=True)
    legacy = directory / 'github-app.json'
    legacy.write_text(json.dumps({**app_config(tmp_path)['app'], 'private_key_path': str(key)}))
    legacy.chmod(0o600)
    store = UserDefaultsStore(directory / 'user-defaults.json')
    runner = {'source': 'pat', 'pat': {'env': 'RUNNER_PAT'}}
    store.path.write_text(json.dumps({
        'language': 'en', 'profile': {'preset': 'economy'},
        'github': {'runner_source': 'pat', 'pat': runner['pat']},
    }))
    original = store.path.read_bytes()
    material = legacy.read_bytes()
    assert store.describe()['github']['migration'] == 'required'
    assert store.describe()['github']['runner'] == runner
    assert store.path.read_bytes() == original
    with pytest.raises(GitHubIdentityError, match='migrate-github-app'):
        resolve_worker_identity({}, app_profile_path=legacy)
    with monkeypatch.context() as patch:
        patch.setattr(store, '_sync_directory', lambda: (_ for _ in ()).throw(OSError('disk failure')))
        with pytest.raises(OSError):
            store.configure(migrate_github_app=True)
    assert store.path.read_bytes() == original
    assert legacy.read_bytes() == material
    code, result = settings('configure', '--migrate-github-app')
    assert code == 0
    assert result['github']['worker'] == {'source': 'app', 'app_profile': {
        **app_config(tmp_path)['app'], 'private_key_path': str(key)},
        'app_read_only': False, 'app_current_repository_only': False}
    assert result['github']['runner'] == runner
    assert resolve_runner_identity(store.load()['github']) == runner
    assert store.load()['language'] == 'en'
    assert store.load()['profile'] == {'preset': 'economy'}
    assert legacy.read_bytes() == material
    assert key.exists()
    legacy.write_text('corrupted after migration')
    assert store.describe()['github']['worker'] == result['github']['worker']
    explicit = app_config(tmp_path)
    explicit.update(worker_source='host', app_read_only=False)
    store.configure(github=explicit)
    assert store.configure(migrate_github_app=True)['github']['worker'] == {'source': 'host'}
    assert not list(directory.glob('*.tmp'))


def test_migration_explicit_host_does_not_consume_broken_legacy(tmp_path: Path) -> None:
    store = UserDefaultsStore(tmp_path / "user-defaults.json")
    store.path.write_text(json.dumps({"github": {"worker_source": "host"}, "language": "en"}))
    legacy = tmp_path / "github-app.json"
    legacy.write_text("broken old metadata")
    assert store.configure(migrate_github_app=True)["github"]["worker"] == {"source": "host"}
    assert legacy.read_text() == "broken old metadata"
