"""Paused work continues from facts without renewing unrelated work."""
from copy import deepcopy

from typing import Any

import pytest

from agent_run.cli_surface import _resume_is_ready
from agent_run.controller import apply_resume_to_state
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.resume_intent import ResumeIntentError, bind_resume_intent, validate_resume_intent


@pytest.mark.parametrize("path", [
    ("review_budget", "development_attempts"),
    ("ci_recovery", "developments_used"),
    ("repair_integration_budgets", "2", "integration_resync", "used"),
    ("repair_job", "run_repair_integration_conflict"),
    ("repair_job", "pending_review_result"),
    ("repair_job", "run_branch_write_intent"),
    ("repair_job", "integration_publication_intent"),
    ("repair_job", "pending_semantic_attempt", "review_context"),
])
def test_repair_resume_binds_owned_budgets_and_retained_work(path: tuple[str, ...]) -> None:
    job = {
        "phase": "blocked", "blocked_reason": "unknown_integration_pause",
        "pause_resume_phase": "accepted", "repair_generation": 2,
        "run_repair_integration_conflict": {"failure": "original Git evidence"},
        "pending_review_result": {"summary": "returned Review"},
        "run_branch_write_intent": {"candidate_sha": "repair-head"},
        "integration_publication_intent": {"head_sha": "integration-head"},
        "pending_semantic_attempt": {
            "status": "pending", "role": "development",
            "review_context": {"full_range": "baseline-to-candidate"},
        },
    }
    state = {"run_id": "run", "status": "blocked", "run_acceptance": {
        "phase": "repairing", "repair_job": job,
        "review_budget": {"development_attempts": 2},
        "ci_recovery": {"developments_used": 1},
        "repair_integration_budgets": {"2": {"integration_resync": {"used": 1}}},
    }}
    before = deepcopy(state)
    intent = bind_resume_intent(state)
    assert intent["authorization"] == "continue_existing_work"
    validate_resume_intent(deepcopy(state), intent)
    changed = deepcopy(state)
    field = changed["run_acceptance"]
    for key in path[:-1]:
        field = field[key]
    field[path[-1]] = "changed authoritative fact"
    with pytest.raises(ResumeIntentError, match="授权已变化"):
        validate_resume_intent(changed, intent)
    assert state == before


@pytest.mark.parametrize('location,role', [
    (location, role) for location in ('ticket:2', 'parent', 'run_repair')
    for role in (('development', 'reviewer') if location == 'run_repair'
                 else ('development', 'reviewer', 'publication'))
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


@pytest.mark.parametrize("phase", ["waiting_external", "stale"])
def test_final_publication_resume_preserves_saved_reconciliation_work(phase: str) -> None:
    job = {
        "phase": "ready_for_human", "pause_resume_phase": phase,
        "blocked_reason": "github_write_outcome_unknown",
        "write_intent": {"action": "create_final_pr"},
        "artifact": {"pr_title": "Stored publication"},
        "publication_operation_retry": {"attempts": 1, "limit": 5},
    }
    state = {
        "run_id": "run", "status": "ready_for_human", "run_publication": job,
        "run_acceptance": {"phase": "accepted"},
    }
    original = deepcopy(job)
    apply_resume_to_state(
        state, explicit_resume=True, resume_human_blocker=False,
        new_thread=False, human_response=None, resume_budget_checkpoint=False,
        budget_policy=DeliveryPolicy(), ticket_number=None, source_status="ready_for_human",
    )
    assert state["status"] == "run_publication_pending"
    assert job["phase"] == phase
    for key in ("artifact", "write_intent", "publication_operation_retry"):
        assert job[key] == original[key]
    assert "pause_resume_phase" not in job
    assert "approval_grant" not in job


def test_unknown_resume_does_not_authorize_changed_scope_or_final_approval() -> None:
    for status in ('unsupported_scope_change', 'requeue_required', 'parent_approval_pending'):
        state = {'run_id': 'run', 'status': status, 'parent_job': {
            'phase': 'blocked', 'blocked_reason': 'unknown_failure',
            'execution_resume_phase': 'developing'}}
        assert not _resume_is_ready(state)


@pytest.mark.parametrize("location,scene_key,phase", [
    ("ticket:2", "integration_conflict", "accepted"),
    ("parent", "parent_integration_conflict", "accepted"),
    ("run_acceptance", "run_integration_conflict", "pending"),
])
@pytest.mark.parametrize("returned", [False, True])
def test_integration_pause_uses_retained_work_without_active_invocation(
    location: str, scene_key: str, phase: str, returned: bool,
) -> None:
    from agent_run.operator_action_facts import operator_action_facts
    from agent_run.resume_work import resume_message_owner

    scene = {"source_sha": "original-source", "failure": {"error": "raw Git evidence"}}
    if returned:
        scene["development_summary"] = "saved Agent result"
    job = {
        "phase": "blocked", "blocked_reason": "unknown_integration_pause",
        "pause_resume_phase": phase, scene_key: scene,
        "development_thread_id": "prior-developer", "ticket_number": 2,
        "ticket_branch_generation": 2, "parent_generation": 2, "acceptance_generation": 2,
        "pending_semantic_attempt": {"status": "pending", "role": "development"},
        "ticket_write_intent": {"operation": "publish", "head_sha": "original-source"},
    }
    sibling = {"ticket_number": 3, "phase": "blocked", "blocked_reason": "agent_requires_human",
               "human_blockers": ["independent approval"], "human_blocker_phase": "developing"}
    state: dict[str, Any] = {
        "run_id": "run", "status": "blocked", "active_agent_invocation": None,
        "ticket_jobs": {}, "ticket_agent_invocations": {},
        "ticket_integration_holder": None, "ticket_integration_queue": [],
    }
    target = 2 if location == "ticket:2" else None
    if target:
        state["ticket_jobs"] = {"2": job, "3": sibling}
    else:
        state["parent_job" if location == "parent" else location] = job
    identity = location if target else "parent-only:run" if location == "parent" else "run-integration:run"
    assert resume_message_owner(state, target) == (identity, job, 2)
    fact = next(fact for fact in operator_action_facts(state) if fact["location"] == location)
    assert fact["work_subject"] == identity
    assert fact["phase"] == phase
    assert fact["role"] == (None if returned else "development")
    before = deepcopy(job)
    sibling_before = deepcopy(sibling)
    apply_resume_to_state(
        state, explicit_resume=True, resume_human_blocker=False, new_thread=True,
        human_response=None, resume_budget_checkpoint=False, budget_policy=DeliveryPolicy(),
        ticket_number=target, source_status="blocked",
    )
    assert job.get("development_new_thread", False) is (not returned)
    assert "reviewer_new_thread" not in job
    assert job[scene_key] == before[scene_key]
    assert job["ticket_write_intent"] == before["ticket_write_intent"]
    assert job["pending_semantic_attempt"] == before["pending_semantic_attempt"]
    assert sibling == sibling_before


def test_new_thread_between_parent_review_and_repair_uses_retained_development() -> None:
    request = {"repair_source": "acceptance", "acceptance_artifact": {"original": "Findings"}}
    job = {"phase": "repairing", "acceptance_generation": 2, "repair_request": request}
    state = {"run_id": "run", "status": "run_acceptance_pending", "run_acceptance": job}
    apply_resume_to_state(
        state, explicit_resume=True, resume_human_blocker=False, new_thread=True,
        human_response=None, resume_budget_checkpoint=False, budget_policy=DeliveryPolicy(),
        ticket_number=None, source_status="run_acceptance_pending",
    )
    assert request["development_new_thread"] is True
    assert request["acceptance_artifact"] == {"original": "Findings"}
    assert "reviewer_new_thread" not in job


def test_human_resume_selects_ticket_invocation_without_changing_sibling() -> None:
    selected = {
        "ticket_number": 2, "ticket_branch_generation": 1, "phase": "blocked",
        "blocked_reason": "agent_requires_human", "human_blocker_phase": "accepted",
        "human_blockers": ["Confirm integration data"],
        "integration_conflict": {"head_sha": "original-head"},
        "development_thread_id": "selected-thread",
    }
    sibling = {"ticket_number": 3, "phase": "developing", "development_thread_id": "sibling-thread"}
    invocation = {"role": "development", "status": "completed", "work_subject": "ticket:3"}
    state: dict[str, Any] = {
        "run_id": "run", "status": "ready_for_human", "active_ticket_job": sibling,
        "active_agent_invocation": invocation, "ticket_jobs": {"2": selected, "3": sibling},
        "ticket_agent_invocations": {"3": invocation},
    }
    before = deepcopy(sibling), deepcopy(invocation)
    apply_resume_to_state(
        state, explicit_resume=True, resume_human_blocker=True, new_thread=True,
        human_response=None, resume_budget_checkpoint=False, budget_policy=DeliveryPolicy(),
        ticket_number=None, source_status="ready_for_human",
    )
    assert state["active_ticket_job"] is selected
    assert state["active_agent_invocation"] is None
    assert selected["development_new_thread"] is True
    assert (sibling, invocation) == before
