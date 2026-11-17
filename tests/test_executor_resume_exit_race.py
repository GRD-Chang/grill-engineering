"""Real Executor/Core transitions at the final quiescent-read boundary."""
import pytest
from dataclasses import replace
import time

from agent_run.core.operation_runner import OperationRunner
from agent_run.errors import KernelError
from agent_run.model import DeliveryKind
from agent_run.runtime import executor
from agent_run.runtime.action_observation import _sample
from support.control_scenarios import admit_scenario
from support.notification import _NoNotifications
from support.offline import Scenario


@pytest.mark.parametrize('configure_profile', [False, True])
def test_resume_after_executor_quiescent_read_really_starts_work_before_success(tmp_path, monkeypatch, configure_profile):
    scenario = admit_scenario(Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY), tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    host = kernel.host_request('start')
    assert kernel.control_host(host, 'stop', 'stop-original-subject', subject=subject) == 'applied'
    state, run_once = OperationRunner.state, OperationRunner.run_once
    observed = []
    receipt = {'result': 'accepted', 'action': 'resume', 'action_id': 'late-resume',
               'host_action_id': 'start', 'host': 'running'}

    def at_last_state(runner):
        current = state(runner)
        if current.disposition == 'quiescent' and not observed:
            # Exact race: real state has been read, but the Host has not yet
            # committed its exit decision. Keep that stale state as its input.
            with kernel.storage.transaction(write=False) as tx:
                assert tuple(tx.one('SELECT (SELECT count(*) FROM attempts),'
                                    '(SELECT count(*) FROM effects),(SELECT count(*) FROM invocations)')) == (0, 0, 0)
            assert kernel.control_host(host, 'resume', 'late-resume', subject=subject) == 'applied'
            if configure_profile:
                kernel.configure_profiles('new-profile-for-future-threads', preset='economy')
            assert kernel.operation_state().disposition == 'ready'
            assert _sample(kernel, receipt) == (True, False, False)
            observed.append('accepted-after-quiescent-read')
        return current

    def real_operation(runner, **options):
        step = run_once(runner, **options)
        # The first actual program operation creates the workspace intent;
        # stop afterwards so this regression needs no Codex process or sleep.
        assert _sample(kernel, receipt) == (True, False, True)
        observed.append('actual-operation')
        kernel.stop_task('stop-after-confirmed-execution')
        return step

    monkeypatch.setattr(OperationRunner, 'state', at_last_state)
    monkeypatch.setattr(OperationRunner, 'run_once', real_operation)
    monkeypatch.setattr(executor, '_make_ports', scenario.make_ports)
    monkeypatch.setattr('agent_run.notifications.sender.NativeNotifications', _NoNotifications)
    assert executor.run(kernel, scenario.execution, {}) == 0
    assert observed == ['accepted-after-quiescent-read', 'actual-operation']
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM effects')[0] > 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one("SELECT count(*) FROM history WHERE kind='resume_execution' AND identity='late-resume'")[0] == 1


def test_control_after_atomic_exit_decision_is_not_applied(tmp_path):
    scenario = admit_scenario(Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY), tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    host = kernel.host_request('start')
    assert kernel.control_host(host, 'stop', 'stop-original-subject', subject=subject) == 'applied'
    assert kernel.prepare_executor_exit(scenario.execution) is True
    with pytest.raises(KernelError, match='executor_fenced'):
        kernel.control_host(host, 'resume', 'too-late-resume', subject=subject)
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT id FROM actions WHERE id='too-late-resume'")
        assert tx.one("SELECT status,usage_released FROM hosts WHERE action_id='start'")[:] == ('running', 0)
    # Fencing control admission does not pretend physical exit or lease release.
    assert kernel.active_host_action() == 'start'


def test_stop_after_exit_decision_keeps_only_the_actual_host_generation(tmp_path):
    scenario = admit_scenario(Scenario(tmp_path / 'scenario', DeliveryKind.PARENT_ONLY), tmp_path)
    kernel, subject = scenario.kernel, scenario.delivery.key
    host = kernel.host_request('start')
    original = kernel.host_usage('start')
    assert original is not None
    kernel.control_host(host, 'stop', 'stop-original-subject', subject=subject)
    assert kernel.prepare_executor_exit(scenario.execution) is True
    assert kernel.stop_task('stop-while-exit-is-prepared') == 'applied'
    assert kernel.observe_host(host, replace(original, disposition='exited', exit_status=0,
        ended_at=time.time(), usage_released=True)) == 'exited'
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows("SELECT owner FROM retired_executors WHERE status!='exited'")
    successor = kernel.admit_host('resume-after-stop-and-exit', kind='resume', command=('controlled-host',),
        environment_carrier=tmp_path / 'next-carrier', preflight=lambda: None)
    assert successor.owner != host.owner and successor.epoch > host.epoch
