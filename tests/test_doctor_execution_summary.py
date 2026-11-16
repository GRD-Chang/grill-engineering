"""Host capability alone cannot make the combined execution report ready."""
import pytest

from agent_run.install import doctor


@pytest.fixture
def ready_dependencies(monkeypatch):
    for name in ('_python_check', '_sqlite_check', '_git_check', '_bubblewrap_check',
                 '_active_runner_check', '_path_check', '_codex_check', '_github_check', '_tool_check'):
        monkeypatch.setattr(doctor, name, lambda *a, **kw: {'status': 'ok'})
    monkeypatch.setattr(doctor, '_execution_readiness', lambda: {'status': 'ok', 'host': 'systemd-user'})
    monkeypatch.setattr(doctor, '_foreground_readiness', lambda: {
        'status': 'ready', 'host': 'foreground', 'business_execution': 'not_verified'})
    identities = {role: {'source': 'host', 'status': 'ok', 'authentication': 'not_verified'}
                  for role in ('runner', 'worker')}
    monkeypatch.setattr(doctor, 'identity_checks', lambda *a, **kw: identities)
    monkeypatch.setattr(doctor.UserDefaultsStore, 'describe', lambda self: {'github': {}})
    return identities


@pytest.mark.parametrize('mode', ['foreground', 'background'])
@pytest.mark.parametrize('missing', ['sqlite', 'git', 'codex', 'openssl', 'bubblewrap',
                                    'github', 'worker_read_provider', 'runner_identity'])
def test_execution_requires_every_declared_dependency(monkeypatch, ready_dependencies, mode, missing):
    if missing in ('worker_read_provider', 'runner_identity'):
        role = 'worker' if missing == 'worker_read_provider' else 'runner'
        ready_dependencies[role]['status'] = 'unavailable'
    else:
        check = {'openssl': '_tool_check'}.get(missing, '_' + missing + '_check')
        monkeypatch.setattr(doctor, check, lambda *a, **kw: {'status': 'unavailable'})
    report = doctor.collect(local=True, mode=mode)
    assert report['checks'][missing]['status'] == 'unavailable'
    assert missing in report['execution_readiness']['checks']
    assert report['execution_readiness']['status'] == 'unavailable'
    assert missing in report['execution_readiness']['reason']
    assert report['status'] == 'issues'
    assert report['verification']['scope'] == 'local'
    assert report['verification']['model_call'] is False


@pytest.mark.parametrize('mode,expected', [('foreground', 'ready'), ('background', 'ok')])
def test_ready_execution_preserves_mode_status_and_overall_readiness(ready_dependencies, mode, expected):
    report = doctor.collect(local=True, mode=mode)
    assert report['execution_readiness']['status'] == expected
    assert report['status'] == 'ready'
    assert report['verification']['authentication'] == 'not_verified'
    if mode == 'foreground':
        assert report['execution_readiness']['business_execution'] == 'not_verified'


@pytest.mark.parametrize('mode', ['foreground', 'background'])
def test_online_repository_read_is_also_required(ready_dependencies, mode):
    # Authentication is still unverified: repository verification cannot be inferred.
    report = doctor.collect(online=True, repository='example/project', mode=mode)
    assert report['checks']['repository_read']['status'] == 'unavailable'
    assert report['execution_readiness']['status'] == 'unavailable'
    assert 'repository_read' in report['execution_readiness']['reason']
    assert report['status'] == 'issues'
    assert report['verification']['authentication'] == 'not_verified'


@pytest.mark.parametrize('mode,probe', [('foreground', '_foreground_readiness'),
                                      ('background', '_execution_readiness')])
def test_host_failure_reason_is_preserved(monkeypatch, ready_dependencies, mode, probe):
    monkeypatch.setattr(doctor, probe, lambda: {'status': 'unavailable', 'reason': 'host proof unavailable'})
    report = doctor.collect(local=True, mode=mode)
    assert report['execution_readiness']['status'] == 'unavailable'
    assert report['execution_readiness']['reason'] == 'host proof unavailable'
    assert report['status'] == 'issues'
