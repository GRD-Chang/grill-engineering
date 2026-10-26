"""Historical fault regression; synthetic providers and private local Git only."""
from support.core_operations import prepare_scripted_agent
import json
from pathlib import Path

import pytest

def test_parent_integration_reviewer_receives_original_conflict_evidence(tmp_path, monkeypatch):
    from support.offline import Scenario
    from agent_run.model import DeliveryKind, Role
    scenario=Scenario(tmp_path/'scenario',DeliveryKind.PARENT_ONLY, reviews=('pass','pass'), edits=(
        {'shared.txt':'Candidate content\n'},{'shared.txt':'Resolved complete content\n'}))
    for _ in range(30):
        if scenario.kernel.status(scenario.delivery.key)['phase']=='integration':break
        scenario.step()
    else:raise AssertionError('Original reviewed candidate did not reach integration')
    scenario.port.external_append(scenario.delivery.target_ref,{'shared.txt':'External content\n'},'conflict')
    runner=scenario.runner();repair=None
    for _ in range(60):
        step=prepare_scripted_agent(scenario.kernel, runner.run_once())
        if step.call is None:continue
        call=step.call
        if call.role==Role.DEVELOPMENT:
            assert call.purpose=='integration' and 'integration_evidence' in call.context
            repair=call.context['integration_evidence']
        if call.role==Role.REVIEW and repair is not None:
            rendered=scenario.kernel.delivery(call.subject)
            result={'original_git_failure_reached_development':True,
                    'original_git_failure_command':repair['raw']['command'],
                    'review_context_keys':sorted(call.context),
                    'review_facts_keys':sorted(call.context['review_context']['facts']),
                    'review_has_original_integration_evidence':any(k in call.context or k in call.context['review_context']['facts'] for k in ('integration_record','integration_evidence','run_integration_record')),
                    'real_git':True,'network_requests':0}
            (tmp_path/'review-integration-evidence-observation.json').write_text(json.dumps(result,indent=2)+'\n')
            assert result['review_has_original_integration_evidence'], 'Integration repair receives raw conflict but its actual Reviewer input loses it'
            return
        runner.authorize_agent(call)
        scenario.kernel.receive(call,scenario.agent(call))
    raise AssertionError('No integration review was allocated')


def _next_call(scenario, runner, role):
    for _ in range(80):
        step = prepare_scripted_agent(scenario.kernel, runner.run_once())
        if step.call is not None:
            if step.call.role == role:
                return step.call
            runner.authorize_agent(step.call)
            scenario.kernel.receive(step.call, scenario.agent(step.call))
        else:
            assert step.kind not in {'paused', 'waiting', 'approval', 'complete'}, step
    raise AssertionError('Expected role was never allocated: '+str(role))


@pytest.mark.parametrize('new_thread', [False, True])
def test_original_integration_receipt_survives_review_continuation_and_findings(tmp_path, new_thread):
    from agent_run.core.kernel import Kernel
    from agent_run.core.prompts.role import render_role_prompt
    from agent_run.model import DeliveryKind, Role, wire_json
    from support.offline import Scenario, review_result
    scenario = Scenario(tmp_path/'scenario', DeliveryKind.PARENT_ONLY, edits=(
        {'shared.txt':'Candidate content\n'}, {'shared.txt':'Resolved complete content\n'},
        {'shared.txt':'Resolved complete content\n', 'followup.txt':'Reviewed repair\n'}))
    for _ in range(30):
        if scenario.kernel.status(scenario.delivery.key)['phase'] == 'integration':
            break
        scenario.step()
    else:
        raise AssertionError('Initial review did not reach integration')
    scenario.port.external_append(scenario.delivery.target_ref, {'shared.txt':'External content\n'}, 'conflict')
    runner = scenario.runner()
    repair = _next_call(scenario, runner, Role.DEVELOPMENT)
    original = repair.context['integration_evidence']
    runner.authorize_agent(repair)
    raw = scenario.agent(repair)
    summary = json.loads(raw)['summary']
    scenario.kernel.receive(repair, raw)
    review = _next_call(scenario, runner, Role.REVIEW)
    record = review.context['review_context']['facts']['integration_record']
    assert record['receipt'] == original
    assert record['subject'] == repair.subject and record['generation'] == 1
    assert record['development_attempt'] == repair.attempt_id and record['development_summary'] == summary
    with scenario.kernel.storage.transaction(write=False) as tx:
        effect = tx.one('SELECT subject,generation,receipt_id,status FROM effects WHERE id=?', (record['effect_id'],))
        assert tuple(effect) == (repair.subject, 1, record['receipt_id'], 'confirmed')
        assert json.loads(tx.get(record['receipt_id'], repair.subject)) == original
        assert json.loads(tx.get(record['development_result'], repair.subject))['summary'] == summary
    prompt = render_role_prompt(review)
    assert summary in prompt and 'merge_head' in prompt and record['receipt_id'] in prompt
    assert review.context['review_context']['facts']['current_review_identity']['reviewed_candidate_sha'] == review.context['reviewed_commit']
    runner.authorize_agent(review)
    scenario.kernel.receive(review, wire_json(review_result('blocked')))
    for _ in range(10):
        if runner.run_once().kind == 'paused':
            break
    else:
        raise AssertionError('Blocked Reviewer did not preserve the original Attempt')
    scenario.kernel.control('resume', 'reviewer-answer', subject=review.subject,
                            message='Use the recorded original conflict.', new_thread=new_thread)
    # The saved Attempt, rather than a new live Git read, owns continuation facts.
    scenario.kernel = Kernel(scenario.kernel.storage.path)
    runner = scenario.runner()
    continued = _next_call(scenario, runner, Role.REVIEW)
    assert continued.attempt_id == review.attempt_id
    assert (continued.thread_id != review.thread_id) == new_thread
    assert continued.context['review_context']['facts']['integration_record'] == record
    assert summary in render_role_prompt(continued) and record['receipt_id'] in render_role_prompt(continued)
    runner.authorize_agent(continued)
    scenario.kernel.receive(continued, wire_json(review_result('fail')))
    followup = _next_call(scenario, runner, Role.DEVELOPMENT)
    assert followup.purpose == 'integration' and followup.context['repair_source'] == 'acceptance'
    assert followup.context['acceptance_artifact'] == review_result('fail')
    runner.authorize_agent(followup)
    followup_raw = scenario.agent(followup)
    scenario.kernel.receive(followup, followup_raw)
    rereview = _next_call(scenario, runner, Role.REVIEW)
    updated = rereview.context['review_context']['facts']['integration_record']
    assert updated['receipt_id'] == record['receipt_id'] and updated['receipt'] == original
    assert updated['effect_id'] == record['effect_id'] and updated['generation'] == 1
    assert updated['development_attempt'] == followup.attempt_id
    assert updated['development_summary'] == json.loads(followup_raw)['summary']
    assert rereview.attempt_id != review.attempt_id and rereview.thread_id != continued.thread_id
    assert rereview.context['review_context']['facts']['previous_acceptance_artifact'] == review_result('fail')
    if not new_thread:
        from dataclasses import replace
        runner.authorize_agent(rereview)
        scenario.kernel.receive(rereview, scenario.agent(rereview))
        for _ in range(10):
            runner.run_once()
            if scenario.kernel.status(rereview.subject)['phase'] == 'ready_publication':
                break
        else:
            raise AssertionError('Repaired Review did not reach its publication boundary')
        scenario.requirements = replace(scenario.requirements, parent=replace(
            scenario.requirements.parent, body=scenario.requirements.parent.body+'\nNew requirement'))
        world = runner.observe(rereview.subject)
        old = scenario.kernel.delivery(rereview.subject)
        replacement = replace(old, source_ref='refs/heads/parent-g2', base=world.target,
                              checkout=str(Path(old.checkout).with_name('parent-g2')))
        assert scenario.kernel.requeue(rereview.subject, 'new-requirement', replacement, world) == 'applied'
        next_generation = _next_call(scenario, runner, Role.REVIEW)
        assert next_generation.context['generation'] == 2
        assert 'integration_record' not in next_generation.context['review_context']['facts']
        assert 'previous_acceptance_artifact' not in next_generation.context['review_context']['facts']


def test_ordinary_review_does_not_inherit_integration_evidence(tmp_path):
    from support.offline import Scenario
    from agent_run.model import DeliveryKind, Role
    scenario = Scenario(tmp_path/'scenario', DeliveryKind.TICKET)
    call = _next_call(scenario, scenario.runner(), Role.REVIEW)
    assert call.purpose == 'requirements'
    assert 'integration_record' not in call.context['review_context']['facts']


def test_candidate_repair_does_not_replace_original_integration_failure(tmp_path):
    from support.offline import Scenario
    from agent_run.model import DeliveryKind, Role
    scenario = Scenario(tmp_path/'scenario', DeliveryKind.PARENT_ONLY, edits=(
        {'shared.txt':'Candidate content\n'},
        {'shared.txt':'Resolved complete content\n', 'new.csv':'a,b \r\n'},
        {'new.csv':'a,b\r\n'}))
    for _ in range(30):
        if scenario.kernel.status(scenario.delivery.key)['phase'] == 'integration':
            break
        scenario.step()
    scenario.port.external_append(scenario.delivery.target_ref, {'shared.txt':'External content\n'}, 'conflict')
    runner = scenario.runner()
    repair = _next_call(scenario, runner, Role.DEVELOPMENT)
    original = repair.context['integration_evidence']
    runner.authorize_agent(repair)
    scenario.kernel.receive(repair, scenario.agent(repair))
    git_repair = _next_call(scenario, runner, Role.DEVELOPMENT)
    assert git_repair.context['repair_source'] == 'git_integrity'
    assert git_repair.context['git_integrity_evidence']['raw']['returncode'] != 0
    runner.authorize_agent(git_repair)
    raw = scenario.agent(git_repair)
    scenario.kernel.receive(git_repair, raw)
    review = _next_call(scenario, runner, Role.REVIEW)
    record = review.context['review_context']['facts']['integration_record']
    assert record['receipt'] == original
    assert record['development_attempt'] == git_repair.attempt_id
    assert record['development_summary'] == json.loads(raw)['summary']


def test_ticket_integration_repair_does_not_create_an_extra_reviewer(tmp_path):
    from support.offline import Scenario
    from agent_run.model import DeliveryKind, Role
    scenario = Scenario(tmp_path/'scenario', DeliveryKind.TICKET, edits=(
        {'shared.txt':'Candidate content\n'}, {'shared.txt':'Resolved complete content\n'}))
    for _ in range(30):
        if scenario.kernel.status(scenario.delivery.key)['phase'] == 'integration':
            break
        scenario.step()
    scenario.port.external_append(scenario.delivery.target_ref, {'shared.txt':'External content\n'}, 'conflict')
    count = scenario.review_count
    assert scenario.run().kind == 'complete'
    assert scenario.review_count == count == 1
    assert [call['role'] for call in scenario.role_calls if call['purpose'] == 'integration'] == [Role.DEVELOPMENT, Role.PUBLICATION]
