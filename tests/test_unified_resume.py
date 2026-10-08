"""Paused work continues from facts without renewing unrelated work."""
from copy import deepcopy

from typing import Any

import pytest

from agent_run.cli_surface import _resume_is_ready
from agent_run.controller import apply_resume_to_state
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.resume_intent import bind_resume_intent


@pytest.mark.parametrize('location,role', [
    (location, role) for location in ('ticket:2', 'parent', 'run_repair')
    for role in ('development', 'reviewer', 'publication')
] + [('run_acceptance', 'reviewer'), ('run_publication', 'publication')])
@pytest.mark.parametrize("new_thread", [False, True])
def test_unknown_reason_preserves_current_work_and_budgets(location: str, role: str, new_thread: bool) -> None:
    job = {'phase': 'blocked', 'blocked_reason': 'unknown_failure',
           'ticket_number': 2, 'ticket_branch_generation': 1,
           'pending_semantic_attempt': {'status': 'pending', 'role': role},
           'review_budget': {'window': 1, 'development_attempts': 2},
           'ci_recovery': {'used': 3}, 'candidate_sha': 'candidate',
           'repair_source': 'required_checks', 'ci_evidence': {'raw': 'original log'}}
    sibling = {'phase': 'blocked', 'blocked_reason': 'agent_requires_human',
               'ticket_number': 3, 'human_blockers': ['permission needed']}
    state: dict[str, Any] = {'run_id': 'run', 'status': 'blocked', 'ticket_jobs': {},
             'ticket_integration_holder': None, 'ticket_integration_queue': [],
             'ticket_agent_invocations': {}, 'diagnostics': [{'message': 'raw failure'}]}
    if location == 'ticket:2':
        state['ticket_jobs'] = {'2': job, '3': sibling}
    elif location == 'parent':
        state['parent_job'] = job
    elif location == 'run_repair':
        state['run_acceptance'] = {'phase': 'repairing', 'repair_job': job}
    else:
        state[location] = job
        if location == 'run_publication':
            state['run_acceptance'] = {'phase': 'accepted', 'acceptance_generation': 1}
    before = deepcopy(job)
    sibling_before = deepcopy(sibling)
    target = 2 if location == 'ticket:2' else None
    intent = bind_resume_intent(state, ticket_number=target)
    assert intent['authorization'] == 'continue_existing_work'
    if target is None:
        assert _resume_is_ready(state)
    apply_resume_to_state(state, explicit_resume=True, resume_human_blocker=False,
                          new_thread=new_thread, human_response=None, resume_budget_checkpoint=False,
                          budget_policy=DeliveryPolicy(), ticket_number=target, source_status='blocked')
    assert job['phase'] == (
        'ready_for_integration' if location == 'ticket:2' and role == 'publication' else
        'publishing' if location == 'run_publication' else
        {'development': 'repairing', 'reviewer': 'reviewing', 'publication': 'accepted'}[role]
    )
    for key in ('pending_semantic_attempt', 'review_budget', 'ci_recovery', 'candidate_sha',
                'repair_source', 'ci_evidence'):
        assert job[key] == before[key]
    flag = ('reviewer_new_thread' if location == 'run_acceptance' else
            {'development': 'development_new_thread', 'reviewer': 'review_new_thread',
             'publication': 'publication_new_thread'}[role])
    assert job.get(flag, False) is new_thread
    assert sibling == sibling_before
    assert job['pause_history'][-1]['blocked_reason'] == 'unknown_failure'
    # A duplicate application does not reopen or renew the completed pause.
    apply_resume_to_state(state, explicit_resume=True, resume_human_blocker=False,
                          new_thread=new_thread, human_response=None, resume_budget_checkpoint=False,
                          budget_policy=DeliveryPolicy(), ticket_number=target, source_status='blocked')
    assert len(job['pause_history']) == 1


def test_missing_work_facts_keeps_resume_entry_and_reports_gap() -> None:
    job = {'phase': 'blocked', 'blocked_reason': 'unknown_failure'}
    state = {'run_id': 'run', 'status': 'blocked', 'parent_job': job}
    assert _resume_is_ready(state)
    before = deepcopy(state)
    with pytest.raises(ValueError, match='missing saved continuation phase'):
        apply_resume_to_state(state, explicit_resume=True, resume_human_blocker=False,
                              new_thread=False, human_response=None, resume_budget_checkpoint=False,
                              budget_policy=DeliveryPolicy(), ticket_number=None, source_status='blocked')
    assert state == before


def test_unknown_resume_does_not_authorize_changed_scope_or_final_approval() -> None:
    for status in ('unsupported_scope_change', 'requeue_required', 'parent_approval_pending'):
        state = {'run_id': 'run', 'status': status, 'parent_job': {
            'phase': 'blocked', 'blocked_reason': 'unknown_failure',
            'execution_resume_phase': 'developing'}}
        assert not _resume_is_ready(state)
