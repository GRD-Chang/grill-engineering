"""The script supplies external facts; production Core chooses every operation."""
from pathlib import Path
from dataclasses import replace

import pytest

from agent_run.model import DeliveryKind, Requirements
from agent_run.errors import KernelError
from agent_run.model import Issue, ParentIssue
from support.offline import GraphScenario, Scenario


def test_abandon_terminal_comes_from_core_without_completing_the_delivery(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'abandon', DeliveryKind.PARENT_ONLY)
    from agent_run.adapters.local_git import LocalEffects
    before = scenario.kernel.status(scenario.delivery.key)
    inspections = LocalEffects(scenario.port.git).inspect_resources(scenario.kernel.generation_resources())
    assert scenario.kernel.control('abandon', 'original-abandon', resource_inspections=inspections) == 'pending'
    assert scenario.run().kind == 'complete'
    assert scenario.kernel.operation_state().disposition == 'complete'
    after = scenario.kernel.status(scenario.delivery.key)
    assert after['phase'] == before['phase'] == 'workspace'
    assert after['budgets'] == before['budgets']
    assert scenario.role_calls == []
    assert [write['operation'] for write in scenario.port.writes()] == ['inspect_resources', 'abandon_worktree']


def test_quiescent_run_uses_core_state_and_does_not_allocate_work(tmp_path: Path) -> None:
    source = Requirements(ParentIssue(1, 'Parent', 'Original requirement.', (2,)), (
        Issue(2, 'Ticket', 'Waiting for the operator ready label.', 'OPEN'),))
    scenario = GraphScenario(tmp_path / 'not-ready', source)
    assert scenario.kernel.operation_state().disposition == 'quiescent'
    boundary = scenario.run()
    assert (boundary.kind, boundary.reason) == ('waiting', 'quiescent')
    assert scenario.kernel.status('ticket:2')['pending_attempt'] is None
    assert scenario.role_calls == [] and scenario.port.writes() == []


@pytest.mark.parametrize('gate', ['stop', 'requirements'])
def test_world_before_manual_control_neither_claims_nor_allocates(tmp_path: Path, monkeypatch, gate: str) -> None:
    scenario = Scenario(tmp_path / gate, DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    if gate == 'stop':
        kernel.control('stop', 'stop-before-observation')
        kernel.confirm_executor_exit(scenario.execution)
    else:
        assert scenario.run().kind == 'approval'
        scenario.requirements = replace(scenario.requirements,
            parent=replace(scenario.requirements.parent, body='Changed original requirement.'))
        kernel.refresh(lambda: scenario.requirements)
    before = kernel.status(scenario.delivery.key)
    assert before['stopped'] if gate == 'stop' else before['gates'] == ['requeue_required']
    writes, calls = scenario.port.writes(), list(scenario.role_calls)
    with kernel.storage.transaction(write=False) as tx:
        owner = tuple(tx.one('SELECT executor,epoch FROM runs'))
        counts = tuple(tx.one('SELECT (SELECT count(*) FROM attempts),(SELECT count(*) FROM leases)'))
    monkeypatch.setattr(kernel, 'claim_executor', lambda *a, **kw: pytest.fail('manual observation must not claim'))
    world = scenario.world()
    assert world.target == scenario.initial
    after = kernel.status(scenario.delivery.key)
    assert before['phase'] == after['phase'] == ('workspace' if gate == 'stop' else 'approval')
    assert before['budgets'] == after['budgets']
    assert before['gates'] == after['gates']
    assert scenario.port.writes() == writes and scenario.role_calls == calls
    with kernel.storage.transaction(write=False) as tx:
        assert tuple(tx.one('SELECT executor,epoch FROM runs')) == owner
        assert tuple(tx.one('SELECT (SELECT count(*) FROM attempts),(SELECT count(*) FROM leases)')) == counts
    if gate == 'requirements':
        # The observed World still carries a precise source revision. A later
        # real source update must invalidate it at the manual control seam.
        scenario.requirements = replace(scenario.requirements,
            parent=replace(scenario.requirements.parent, body='A later original requirement.'))
        kernel.refresh(lambda: scenario.requirements)
        replacement = replace(scenario.delivery, source_ref=scenario.delivery.source_ref + '-new',
                              checkout=scenario.delivery.checkout + '-new', base=world.target)
        with pytest.raises(KernelError, match='replacement_requirements'):
            kernel.control('requeue', 'stale-control', subject=scenario.delivery.key,
                           replacement=replacement, world=world)
        assert kernel.control('requeue', 'fresh-control', subject=scenario.delivery.key,
                              replacement=replacement, world=scenario.world()) == 'pending'
        assert kernel.status(scenario.delivery.key)['generation'] == before['generation']


def test_first_scenario_world_is_a_real_core_observation_without_work(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'parent', DeliveryKind.PARENT_ONLY)
    before = scenario.kernel.status(scenario.delivery.key)
    world = scenario.world()
    after = scenario.kernel.status(scenario.delivery.key)
    assert world.target == scenario.initial
    assert world.publisher_actor == 'offline-publisher'
    assert before['phase'] == after['phase'] == 'workspace'
    assert after['pending_attempt'] is None
    assert after['budgets'] == before['budgets']
    assert scenario.port.writes() == []
    assert scenario.role_calls == []


def test_graph_assertion_focus_does_not_select_the_work(tmp_path: Path) -> None:
    source = Requirements(ParentIssue(1, 'Parent', 'Deliver both independent tickets.', (2, 3)), (
        Issue(2, 'First', 'Deliver the first change.', 'OPEN', frozenset({'ready-for-agent'})),
        Issue(3, 'Second', 'Deliver the second change.', 'OPEN', frozenset({'ready-for-agent'}))))
    scenario = GraphScenario(tmp_path / 'graph', source)
    scenario.select('ticket:3')
    for _ in range(10):
        step = scenario.step()
        if step.call is not None:
            assert step.call.subject == 'ticket:2'
            break
    else:
        raise AssertionError('Core did not allocate the first eligible ticket')
    assert scenario.delivery.key == 'ticket:3'
    assert scenario.role_calls[0]['subject'] == 'ticket:2'
    assert scenario.kernel.status('ticket:2')['pending_return'] is True
    assert scenario.kernel.status('ticket:3')['pending_attempt'] is None


def test_publication_cut_approval_and_cleanup_use_the_same_runner(tmp_path: Path) -> None:
    scenario = Scenario(tmp_path / 'cut', DeliveryKind.PARENT_ONLY, reviews=('fail', 'pass'))
    cut = scenario.run(cut='publication_pending')
    assert cut.kind == 'publication_pending'
    assert [call['role'] for call in scenario.role_calls] == ['development', 'review', 'development', 'review']
    assert not any(write['operation'] == 'pr_write' for write in scenario.port.writes())
    assert scenario.run().kind == 'approval'
    assert scenario.kernel.status(scenario.delivery.key)['phase'] == 'approval'
    scenario.kernel.control('approve', 'approve-exact-cut', subject=scenario.delivery.key, world=scenario.world())
    assert scenario.run().kind == 'complete'
    assert scenario.cleanup().kind == 'complete'
    assert scenario.kernel.status(scenario.delivery.key)['resource_cleanup']['state'] == 'complete'
    assert not Path(scenario.delivery.checkout).exists()
    assert scenario.port.remote_refs().keys() == {'refs/heads/main'}


def test_graph_ticket_and_final_use_one_runner_with_independent_expected_roles(tmp_path: Path) -> None:
    source = Requirements(ParentIssue(1, 'Parent', 'Deliver the declared ticket.', (2,)), (
        Issue(2, 'Ticket', 'Deliver a tested change.', 'OPEN', frozenset({'ready-for-agent'})),))
    scenario = GraphScenario(tmp_path / 'whole', source)
    assert scenario.run().kind == 'complete'
    for _ in range(10):
        scenario.step()
        final = scenario.kernel.source_status()['final_subject']
        if final is not None:
            scenario.select(final)
            break
    else:
        raise AssertionError('Core did not create Final from the real Completion')
    assert scenario.run().kind == 'approval'
    assert scenario.run(approve=True).kind == 'complete'
    assert [call['role'] for call in scenario.role_calls] == [
        'development', 'review', 'publication', 'review', 'publication']
    assert [call['subject'] for call in scenario.role_calls] == [
        'ticket:2', 'ticket:2', 'ticket:2', 'final:1', 'final:1']
    assert scenario.kernel.status('ticket:2')['budgets']['requirements']['developments_used'] == 1
    assert scenario.kernel.status('final:1')['budgets']['integration']['developments_used'] == 0
    assert [write['operation'] for write in scenario.port.writes()].count('merge') == 2
    assert scenario.port.remote_refs().keys() == {'refs/heads/main'}
    assert not list((scenario.port.root / 'work').iterdir())
