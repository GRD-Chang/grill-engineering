"""Read outages at Agent release and physical write preparation keep original work."""
import pytest

from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, Policy, ReadWait, RunSpec
from support.core_operations import agent_preparation_fixture, native_agent_fixture, prepare_scripted_agent, setup


def windows(kernel):
    with kernel.storage.transaction(write=False) as tx:
        return [tuple(row) for row in tx.rows(
            "SELECT scope_id,started_at,deadline FROM waits WHERE kind='github_convergence'")]


def test_agent_admission_read_failure_reopens_original_window(tmp_path, monkeypatch):
    base = Commit('base', 'tree')
    spec = RunSpec('read-failure', 'example/repository', 1, 'parent', 'graph', (
        Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'original', base,
                 'refs/heads/parent', 'refs/heads/main', checkout=str(tmp_path)),
    ), Policy(development_deadline_seconds=1200))
    kernel, ports = native_agent_fixture(tmp_path / 'kernel.sqlite3', spec)
    now = [1000.25]
    kernel.clock = lambda: now[0]
    runner = OperationRunner(kernel, kernel.claim_executor('read-failure-owner'), lambda _: ports)
    step = runner.run_once()
    call = prepare_scripted_agent(kernel, step).call
    before = kernel.status(call.subject)
    now[0] = 1000.25

    def unavailable(_parent):
        raise ReadUnavailable('github_read_failed', 'Original Agent release read')

    monkeypatch.setattr(ports, 'source', unavailable)
    with pytest.raises(ReadWait) as first:
        runner.authorize_agent(call)
    assert first.value.step.retry_at == 1005.25
    original = windows(kernel)
    assert original[0][1:] == (1000.25, 1600.25)

    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    reopened.clock = lambda: now[0]
    runner = OperationRunner(reopened, call.execution, lambda _: ports)
    now[0] = 1040.25
    with pytest.raises(ReadWait):
        runner.authorize_agent(call)
    assert windows(reopened) == original
    now[0] = 1600.25
    with pytest.raises(ReadWait) as expired:
        runner.authorize_agent(call)
    assert expired.value.step.reason == 'supervision_timeout'
    assert windows(reopened) == original
    state = reopened.status(call.subject)
    assert state['pending_attempt'] == call.attempt_id
    assert state['budgets'] == before['budgets']
    assert not reopened.worker_settled(call)
    with reopened.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 1
        assert tx.one('SELECT count(*) FROM leases')[0] == 1


def test_write_preparation_read_failure_never_dispatches_or_renews(tmp_path, monkeypatch):
    scenario, ports, runner = setup(tmp_path)
    now = [1000.25]
    scenario.kernel.clock = lambda: now[0]
    source = ports.source
    preparing = [False]
    effects = []

    def unavailable_at_preparation(parent):
        if preparing[0]:
            raise ReadUnavailable('github_read_failed', 'Original write preparation read')
        return source(parent)

    def prepare(effect, authority):
        effects.append(effect)
        preparing[0] = True
        try:
            authority.prepare()
        finally:
            preparing[0] = False
        pytest.fail('Unavailable preparation cannot return write authority')

    monkeypatch.setattr(ports, 'source', unavailable_at_preparation)
    monkeypatch.setattr(ports, 'execute', prepare)
    first = runner.run_once()
    assert first.kind == 'waiting' and first.retry_at == 1005.25
    original = windows(scenario.kernel)
    assert original[0][1:] == (1000.25, 1600.25)
    reopened = Kernel(scenario.kernel.storage.path, run_id=scenario.kernel.storage.run_id)
    reopened.clock = lambda: now[0]
    runner = OperationRunner(reopened, scenario.execution, lambda _: ports)
    now[0] = 1040.25
    assert runner.run_once().kind == 'waiting'
    assert windows(reopened) == original
    assert effects[0] == effects[1]
    now[0] = 1600.25
    assert runner.run_once().reason == 'supervision_timeout'
    assert windows(reopened) == original
    assert len(effects) == 2 and scenario.port.writes() == []
    with reopened.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT status,attempts FROM effects')) == ('prepared', 0)
        assert not tx.rows('SELECT id FROM invocations')


@pytest.mark.parametrize('entry', ['agent', 'effect'])
def test_revoked_execution_during_failed_read_cannot_record_new_wait(tmp_path, monkeypatch, entry):
    if entry == 'agent':
        kernel, runner, ports, step, _ = agent_preparation_fixture(tmp_path)
        call = prepare_scripted_agent(kernel, step).call
    else:
        scenario, ports, runner = setup(tmp_path)
        kernel = scenario.kernel
    source = ports.source
    preparing = [entry == 'agent']

    def revoked(parent):
        if preparing[0]:
            kernel.control('stop', 'revoke-during-original-read')
            raise ReadUnavailable('github_read_failed', 'Read failed after original identity was revoked')
        return source(parent)

    monkeypatch.setattr(ports, 'source', revoked)
    if entry == 'agent':
        with pytest.raises(KernelError, match='executor_fenced'):
            runner.authorize_agent(call)
    else:
        def prepare(effect, authority):
            preparing[0] = True
            return authority.prepare()()
        monkeypatch.setattr(ports, 'execute', prepare)
        assert runner.run_once().reason == 'executor_fenced'
        assert scenario.port.writes() == []
    assert windows(kernel) == []
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT position FROM history WHERE kind='github_read_observation'")
