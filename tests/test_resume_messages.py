from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import _bind_resume_message
from agent_run.human_responses import current_human_response_history
from support.prompt_capture import _capture_public_prompt


@pytest.mark.parametrize("invocation_status", ["completed", "failed"])
def test_resume_message_updates_separately_loaded_ticket_mirrors(invocation_status: str) -> None:
    ticket = {"ticket_number": 3, "ticket_branch_generation": 2,
              "phase": "blocked", "blocked_reason": "ticket_supervision_timeout"}
    sibling = {"ticket_number": 4, "phase": "developing"}
    state = {"run_id": "run-test", "status": "blocked",
             "active_ticket_job": deepcopy(ticket),
             "ticket_jobs": {"3": deepcopy(ticket), "4": deepcopy(sibling)},
             "active_agent_invocation": {"role": "publication", "status": invocation_status,
                                         "generation": 2, "work_subject": "ticket:3"}}
    assert _bind_resume_message(state, "恢复检查") == "appended"
    history = current_human_response_history(state["ticket_jobs"]["3"], generation=2)
    assert history[-1]["response"] == "恢复检查"
    assert state["ticket_jobs"]["3"] == state["active_ticket_job"]
    assert state["ticket_jobs"]["4"] == sibling


@pytest.mark.parametrize('role', ['development', 'fresh_acceptance', 'publication', 'reviewer', 'final_publication'])
def test_resume_message_binds_retained_role_without_releasing_gate(role: str) -> None:
    ticket: dict[str, Any] = {'ticket_number': 3, 'ticket_branch_generation': 2, 'phase': 'blocked',
                              'blocked_reason': 'command_unknown', 'repair_source': 'required_checks',
                              'ci_evidence': {'raw_log': 'failed-check'}}
    state: dict[str, Any] = {
        'run_id': 'run-test', 'status': 'execution_failed',
        'ticket_jobs': {'3': ticket, '4': {'phase': 'blocked', 'blocked_reason': 'agent_requires_human'}},
        'active_ticket_job': ticket,
        'run_acceptance': {'acceptance_generation': 2, 'phase': 'ready_for_human'},
        'run_publication': {'phase': 'ready_for_human'},
        'active_agent_invocation': {'role': role, 'status': 'failed', 'generation': 2,
                                    'work_subject': 'run-acceptance:run-test' if role == 'reviewer'
                                    else 'run-publication:run-test' if role == 'final_publication'
                                    else 'ticket:3'},
    }
    original = deepcopy(state)
    assert _bind_resume_message(state, '环境已修复；继续原任务。') == 'appended'
    owner = state['run_acceptance'] if role == 'reviewer' else state['run_publication'] if role == 'final_publication' else ticket
    assert current_human_response_history(owner, generation=2)[-1]['response'] == '环境已修复；继续原任务。'
    assert owner['phase'] == original['run_acceptance' if role == 'reviewer' else 'run_publication' if role == 'final_publication' else 'active_ticket_job']['phase']
    assert state['ticket_jobs']['4'] == original['ticket_jobs']['4']
    assert ticket['ci_evidence'] == original['active_ticket_job']['ci_evidence']
    state['resume_audit'] = {'history': [{'kind': 'execution_failure', 'human_response_supplied': True,
                                         'successor_invocation_started_at': None}]}
    assert _bind_resume_message(state, '环境已修复；继续原任务。') == 'replayed'
    assert len(owner['human_response_history']) == 1


@pytest.mark.parametrize('method,source', [('develop', 'required_checks'), ('develop', 'integration'),
                                           ('review', None), ('publication', None)])
@pytest.mark.parametrize('mode', ['resume', 'new-thread'])
def test_resume_message_reaches_actual_role_prompt(
    tmp_path: Path, monkeypatch: Any, method: str, source: str | None, mode: str,
) -> None:
    request: dict[str, Any] = {
        'acceptance_scope': 'ticket', '_invocation_mode': mode,
        'thread_id': 'same-work' if mode == 'resume' else None,
        'human_response_history': [{'generation': 2, 'human_blockers': [],
                                    'response': '恢复消息：环境已修复，保留原目标。'}],
        'current_review_identity': {'reviewed_base_sha': 'base', 'reviewed_candidate_sha': 'candidate'},
    }
    if source:
        request.update(repair_source=source)
        request['ci_evidence' if source == 'required_checks' else 'integration_evidence'] = ({'raw_log': 'original-failure-evidence'} if source == 'required_checks' else 'original-failure-evidence')
    if method == 'publication':
        request['acceptance_artifact'] = {'checks': {}}
    prompt = _capture_public_prompt(tmp_path, monkeypatch, method, request, name='resume-message')
    assert '恢复消息：环境已修复，保留原目标。' in prompt
    assert '不接受新的正式需求' in prompt
    if source:
        assert 'original-failure-evidence' in prompt
        assert ('CI' if source == 'required_checks' else '集成') in prompt


@pytest.mark.parametrize('message', [None, '环境已修复'])
def test_unknown_publication_resume_uses_current_generation_without_invocation(message: str | None) -> None:
    from agent_run.controller import apply_resume_to_state
    from agent_run.delivery_policy import DeliveryPolicy
    from agent_run.resume_audit import append_explicit_resume_audit

    owner: dict[str, Any] = {'phase': 'ready_for_human', 'blocked_reason': 'unknown_command_failure',
                             'execution_resume_phase': 'pending', 'artifact': {'title': 'kept content'}}
    state = {'run_id': 'run', 'status': 'ready_for_human',
             'run_acceptance': {'phase': 'accepted', 'acceptance_generation': 2},
             'run_publication': owner}
    if message:
        assert _bind_resume_message(state, message) == 'appended'
    event = append_explicit_resume_audit(state, new_thread=False,
                                         human_response_supplied=message is not None)
    assert event['work_subject'] == 'run-publication:run'
    assert event['generation'] == 2
    apply_resume_to_state(state, explicit_resume=True, resume_human_blocker=False,
                          new_thread=False, human_response=message, resume_budget_checkpoint=False,
                          budget_policy=DeliveryPolicy(), ticket_number=None, source_status='ready_for_human')
    assert state['status'] == 'run_publication_pending'
    assert owner['phase'] == 'pending'
    assert owner['artifact'] == {'title': 'kept content'}
    if message:
        assert state['human_response_audit'][event['resume_id']] == message


def test_stop_between_agent_calls_keeps_message_on_original_work() -> None:
    from agent_run.resume_audit import append_explicit_resume_audit

    state = {'run_id': 'run', 'status': 'operator_stopped',
             'operator_stop': {'resume_status': 'parent_delivery_pending'},
             'parent_job': {'parent_generation': 2, 'phase': 'accepted', 'candidate_sha': 'candidate'}}
    assert _bind_resume_message(state, '继续已完成的成果') == 'appended'
    event = append_explicit_resume_audit(state, new_thread=False, human_response_supplied=True)
    assert event['work_subject'] == 'parent-only:run'
    assert event['generation'] == 2
    assert state['human_response_audit'][event['resume_id']] == '继续已完成的成果'
