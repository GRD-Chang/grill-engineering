"""Review repair inputs retain the active work budget and original CI facts."""
from support.core_operations import prepare_scripted_agent
import json

from agent_run.core.prompts.role import render_role_prompt
from agent_run.model import Role, wire_json
from support.core_operations import scripted_return
from support.offline import review_result
from support.core_operations import budgets


FINDING = 'LATEST_CI_REVIEW_FINDING_7e281: repair the newly reviewed CI behavior.'


def test_ci_review_findings_reach_next_development_with_original_ci_and_budget(ticket_before_ci_review) -> None:
    scenario = ticket_before_ci_review
    scenario.checks[:] = ["fail", "none"]
    scenario.reviews[:] = ["pass", "fail", "pass"]
    kernel, subject = scenario.kernel, scenario.delivery.key
    runner = scenario.runner()
    first_ci = None
    first_budget = None
    failed_review = None
    for _ in range(160):
        step = prepare_scripted_agent(kernel, runner.run_once())
        call = step.call
        if call is None:
            assert step.kind not in {'paused', 'waiting', 'approval', 'complete'}, step
            continue
        if call.role == Role.DEVELOPMENT and call.purpose == 'ci':
            if first_ci is None:
                first_ci, first_budget = call, budgets(scenario)
                assert call.context['repair_source'] == 'required_checks'
                assert call.context['ci_evidence']['result'] == 'fail'
                assert call.context['ci_evidence']['logs'] == 'Raw synthetic failed job log'
                assert 'acceptance_artifact' not in call.context
                assert 'Raw synthetic failed job log' in render_role_prompt(call)
            else:
                assert first_budget is not None and failed_review is not None
                with kernel.storage.transaction(write=False) as tx:
                    job = tx.one('SELECT last_failure,ci_failure,review_attempt FROM subjects WHERE run_id=? AND id=?',
                                 (kernel.storage.run_id, subject))
                    latest = json.loads(tx.get(job['last_failure'], subject))
                    original = json.loads(tx.get(job['ci_failure'], subject))
                    assert job['review_attempt'] == failed_review.attempt_id
                now = budgets(scenario)
                assert FINDING in json.dumps(latest)
                assert call.context['ci_evidence'] == original == first_ci.context['ci_evidence']
                assert now['ci']['window'] == first_budget['ci']['window']
                assert now['ci']['development_limit'] == first_budget['ci']['development_limit'] == 3
                assert now['ci']['developments_used'] == first_budget['ci']['developments_used'] + 1 == 1
                assert now['ci']['reviews_used'] == first_budget['ci']['reviews_used'] + 1 == 1
                assert {key: value for key, value in now.items() if key != 'ci'} == {
                    key: value for key, value in first_budget.items() if key != 'ci'}
                assert call.execution == first_ci.execution == failed_review.execution
                assert call.thread_id == first_ci.thread_id != failed_review.thread_id
                assert call.reported_thread == first_ci.reported_thread
                assert call.attempt_id not in {first_ci.attempt_id, failed_review.attempt_id}
                assert call.invocation_id != first_ci.invocation_id
                prompt = render_role_prompt(call)
                assert FINDING in json.dumps(call.context) and FINDING in prompt
                assert call.context['acceptance_artifact'] == latest
                assert call.context['repair_source'] == 'acceptance' and call.context['work_source'] == 'ci'
                assert 'Raw synthetic failed job log' in prompt
                scripted_return(kernel, runner, call, scenario.agent)
                assert budgets(scenario)['ci']['developments_used'] == 2
                return
        runner.authorize_agent(call)
        raw = json.loads(scenario.agent(call))
        if call.role == Role.REVIEW and call.purpose == 'ci':
            assert raw['checks']['spec']['status'] == 'fail'
            raw['checks']['spec']['findings'] = [FINDING]
            raw['checks']['spec']['evidence'] = 'Reviewed the exact CI candidate in its validation checkout.'
            failed_review = call
        kernel.receive(call, wire_json(raw))
    raise AssertionError('No second CI Development in the normal bounded workflow')


def test_passing_ci_review_publishes_without_an_extra_repair_or_new_window(ticket_before_ci_review) -> None:
    scenario = ticket_before_ci_review
    scenario.checks[:] = ["fail", "none"]
    assert scenario.run().kind == 'complete'
    calls = [call for call in scenario.role_calls if call['purpose'] == 'ci']
    assert [call['role'] for call in calls] == ['development', 'review', 'publication']
    assert all(call['repair_source'] == 'required_checks' for call in calls)
    current = budgets(scenario)
    assert current['ci']['window'] == 1 and current['ci']['development_limit'] == 3
    assert current['ci']['developments_used'] == current['ci']['reviews_used'] == 1
    assert current['requirements']['developments_used'] == current['requirements']['reviews_used'] == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        reviewed = tx.rows("SELECT result_id FROM attempts WHERE run_id=? AND subject=? AND purpose='ci' AND role='review'",
                           (scenario.kernel.storage.run_id, scenario.delivery.key))
        assert len(reviewed) == 1
        assert json.loads(tx.get(reviewed[0]['result_id'], scenario.delivery.key)) == review_result('pass')
