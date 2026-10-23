"""The synthetic physical adapters normalize outages at the real port seam."""
from pathlib import Path

import pytest

from agent_run.git_errors import GitHubReadError
from agent_run_kernel import DeliveryKind, Requirements
from agent_run_kernel.model import ReadWait, Step
from support.offline import OfflinePort, Scenario
from support.offline_api import SyntheticGitHub
from support.effect_boundary import EffectBoundary, effect_boundary


class SourceOutage(OfflinePort):
    def source(self, requirements: Requirements, parent_number: int) -> Requirements:
        raise GitHubReadError('github_read_failed', 'Synthetic physical source is unavailable')



def test_authorization_wait_crosses_the_physical_adapter_without_a_write_or_fake_result(tmp_path: Path, monkeypatch) -> None:
    scenario = Scenario(tmp_path / 'refused', DeliveryKind.PARENT_ONLY)
    # A real native PR request is writable only after its source was published.
    with effect_boundary(scenario, monkeypatch, 'pr_write'):
        with pytest.raises(EffectBoundary) as held:
            scenario.run()
    effect = held.value.effect
    before = scenario.port.writes()
    waiting = ReadWait(Step('waiting', scenario.delivery.key, 'source_unavailable', retry_at=105.25))
    def authorize() -> None:
        raise waiting
    ports = scenario.make_ports(scenario.runner().binding)
    with pytest.raises(ReadWait) as observed:
        ports.execute(effect, authorize)
    assert observed.value is waiting
    assert scenario.port.writes() == before
    assert scenario.port.pull_request_by_ref(effect.source_ref, effect.target_ref) is None


def test_offline_bootstrap_outage_preserves_original_wait_and_unstarted_lease(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'source', DeliveryKind.TICKET)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    for _ in range(12):
        step = scenario.runner().run_once()
        if step.call is not None:
            call = step.call
            break
    else:
        raise AssertionError('Core did not allocate the original call')
    scenario.port = SourceOutage(scenario.port.root)
    runner = scenario.runner()
    with pytest.raises(ReadWait) as first:
        runner.authorize_agent(call)
    assert first.value.retry_at == 105.25
    assert runner.state().active == (call.lease_id,)
    assert not runner.worker_settled(call)
    now[0] = 101.25
    with pytest.raises(ReadWait) as second:
        runner.authorize_agent(call)
    assert second.value.retry_at == first.value.retry_at
    assert not [row for row in scenario.kernel.history(call.subject) if row['kind'] == 'effect_failed']


def test_optional_link_ack_loss_does_not_create_a_required_checks_wait(tmp_path: Path, monkeypatch) -> None:
    scenario = Scenario(tmp_path / 'optional-link', DeliveryKind.TICKET)
    scenario.kernel.clock = lambda: 1000.25
    scenario.port.lose_response.add('issue_link')
    execute = scenario.port.execute
    attempted = []

    def record(effect, authorize):
        if effect.operation == 'issue_link':
            attempted.append(effect)
        return execute(effect, authorize)

    monkeypatch.setattr(scenario.port, 'execute', record)
    for _ in range(80):
        step = scenario.step()
        if attempted:
            break
    else:
        raise AssertionError('The optional link write was never reached')
    assert step.kind == 'progress' and step.retry_at is None
    assert len(attempted) == 1
    assert scenario.kernel.status(scenario.delivery.key)['issue_link']['state'] == 'indeterminate'
    world = scenario.world()
    assert world.checks is not None and world.checks.result == 'none'
    assert world.pr is not None and world.checks.head_sha == world.pr.head.sha
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT scope_id FROM waits WHERE subject=?', (scenario.delivery.key,))
    assert [row['operation'] for row in scenario.port.writes()].count('issue_link') == 1


@pytest.mark.parametrize('failure', ['readback', 'response'])
def test_post_write_outage_keeps_sent_merge_without_command_failure_or_replay(tmp_path: Path, failure: str, monkeypatch) -> None:
    scenario = Scenario(tmp_path / failure, DeliveryKind.PARENT_ONLY)
    now = [100.25]
    scenario.kernel.clock = lambda: now[0]
    assert scenario.run().kind == 'approval'
    scenario.kernel.control('approve', 'approve-original', subject=scenario.delivery.key, world=scenario.world())
    if failure == 'readback':
        write = SyntheticGitHub.write
        def write_then_read_outage(api, endpoint, method, body):
            result = write(api, endpoint, method, body)
            if endpoint.endswith('/merge'):
                api.read_faults.add('*')
            return result
        monkeypatch.setattr(SyntheticGitHub, 'write', write_then_read_outage)
    else:
        scenario.port.lose_response.add('merge')
    runner = scenario.runner()
    for _ in range(12):
        step = runner.run_once(agent_capacity=0)
        if step.kind == 'waiting':
            break
    else:
        raise AssertionError('The physical write/read failure was not supervised')
    pending = scenario.kernel.status(scenario.delivery.key)['pending_write']
    assert pending['operation'] == 'merge' and pending['status'] == 'sent'
    assert [write['operation'] for write in scenario.port.writes()].count('merge') == 1
    assert not [row for row in scenario.kernel.history(scenario.delivery.key) if row['kind'] == 'effect_failed']
    if failure == 'readback':
        scenario.port.read_faults.clear()
    now[0] = 106.25
    assert scenario.run().kind == 'complete'
    assert [write['operation'] for write in scenario.port.writes()].count('merge') == 1
    assert scenario.kernel.status(scenario.delivery.key)['pending_write'] is None
