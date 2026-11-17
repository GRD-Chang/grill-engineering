"""Repair checks converge independently of the final PR's human approval."""
import json

import pytest

from agent_run.errors import KernelError
from support.effect_boundary import advance_to_effect, effect_state
from support.parent_repair import _requests, _run, _until


@pytest.mark.parametrize('result', ['pass', 'none', 'pending', 'unknown', 'fail'])
def test_repair_consumes_exact_checks_without_changing_budget(repair_before_pr, result):
    scenario = repair_before_pr
    scenario.checks[:] = [result]
    before = _until(scenario, lambda state: state['phase'] == 'repair_checks')
    if result in {'pass', 'none'}:
        assert _run(scenario).kind == 'approval'
    else:
        step = scenario.step()
        assert step.kind == ('progress' if result == 'fail' else 'waiting')
    state = scenario.kernel.status(scenario.delivery.key)
    repair = state['repair_history'][0]
    with scenario.kernel.storage.transaction(write=False) as tx:
        checks = json.loads(tx.get(repair['checks_id'], scenario.delivery.key))
    assert (checks['pr_number'], checks['head_sha'], checks['result']) == (
        repair['pr_number'], repair['integrated_sha'], result)
    assert state['budgets'] == before['budgets']
    assert scenario.development_count == 1 and scenario.review_count == 2
    assert scenario.port.remote_commit(scenario.delivery.target_ref) == scenario.initial
    if result in {'pass', 'none'}:
        assert repair['state'] == 'merged'
        final = scenario.port.pull_request_by_ref(scenario.delivery.source_ref, scenario.delivery.target_ref)
        assert final.state == 'OPEN'
        assert len(_requests(scenario, 'PUT', '/merge')) == 2  # Historical Ticket and repair.
    else:
        assert repair['state'] == 'open'
        assert len(_requests(scenario, 'PUT', '/merge')) == 1  # Historical Ticket only.
        assert state['phase'] == ('development' if result == 'fail' else 'repair_checks')
        if result == 'fail':
            assert state['purpose'] == 'ci'


def test_repair_replaces_prepared_pass_evidence_before_merging_none(repair_before_pr, monkeypatch):
    scenario = repair_before_pr
    scenario.checks[:] = ['pass']
    held = advance_to_effect(scenario, monkeypatch, 'merge')
    before = scenario.kernel.status(scenario.delivery.key)
    scenario.checks[:] = ['none']
    with pytest.raises(KernelError):
        held.authorize.prepare()()
    assert _run(scenario).kind == 'approval'
    state = scenario.kernel.status(scenario.delivery.key)
    assert effect_state(scenario, held.effect)[:2] == ('retired', 0)
    assert not any(row['operation'] == 'merge' and row['intent'] == held.effect.effect_id
                   for row in scenario.writes())
    assert state['budgets'] == before['budgets']
    repair = state['repair_history'][0]
    with scenario.kernel.storage.transaction(write=False) as tx:
        checks = json.loads(tx.get(repair['checks_id'], scenario.delivery.key))
    assert checks['result'] == 'none'
    assert checks['pr_number'] == held.effect.pr_number
    assert checks['head_sha'] == held.effect.after_sha
    assert repair['state'] == 'merged'
    assert len(_requests(scenario, 'PUT', '/merge')) == 2
