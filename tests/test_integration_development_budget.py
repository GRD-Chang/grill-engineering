"""Cumulative integration allowance and explicit targeted renewal boundaries."""
from copy import deepcopy
import pytest
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.integration_development_budget import (
    IntegrationDevelopmentExhausted, acquire_integration_development,
    ensure_integration_development_available, renew_integration_development_budget,
    require_integration_development_budget,
)
from agent_run.resume_intent import bind_resume_intent


def test_cumulative_allowance_resumes_charged_attempt_without_reset() -> None:
    policy = DeliveryPolicy(ticket_integration_development_limit=2).snapshot()
    job = {"policy_snapshot": policy}
    ensure_integration_development_available(job, policy_snapshot=policy)
    assert acquire_integration_development(job, attempt_id="first", policy_snapshot=policy)
    assert not acquire_integration_development(job, attempt_id="first", policy_snapshot=policy)
    assert acquire_integration_development(job, attempt_id="second", policy_snapshot=policy)
    recovered = deepcopy(job)
    recovered["pending_semantic_attempt"] = {"attempt_id": "second"}
    assert not acquire_integration_development(recovered, attempt_id="second", policy_snapshot=policy)
    recovered.pop("pending_semantic_attempt")
    with pytest.raises(IntegrationDevelopmentExhausted):
        ensure_integration_development_available(recovered, policy_snapshot=policy)
    assert recovered["integration_development_budget"]["used"] == 2
    assert renew_integration_development_budget(recovered, policy_snapshot=policy)
    assert not renew_integration_development_budget(recovered, policy_snapshot=policy)
    assert recovered["integration_development_budget"]["used"] == 2
    assert recovered["integration_development_budget"]["limit"] == 4
    assert acquire_integration_development(recovered, attempt_id="third", policy_snapshot=policy)
    require_integration_development_budget(recovered["integration_development_budget"], policy_snapshot=policy)


def test_budget_rejects_counter_reset_or_duplicate_identity() -> None:
    policy = DeliveryPolicy().snapshot()
    job = {"policy_snapshot": policy}
    acquire_integration_development(job, attempt_id="first", policy_snapshot=policy)
    bad = deepcopy(job["integration_development_budget"])
    bad["used"] = 0
    with pytest.raises(ValueError, match="cumulative usage"):
        require_integration_development_budget(bad, policy_snapshot=policy)
    bad = deepcopy(job["integration_development_budget"])
    bad["attempt_ids"].append("first")
    bad["used"] = 2
    with pytest.raises(ValueError, match="cumulative usage"):
        require_integration_development_budget(bad, policy_snapshot=policy)


def test_resume_intent_binds_only_integration_allowance() -> None:
    policy = DeliveryPolicy(ticket_integration_development_limit=1).snapshot()
    job = {"ticket_number": 400, "ticket_branch_generation": 1,
           "policy_snapshot": policy, "phase": "blocked",
           "blocked_reason": "integration_development_exhausted"}
    acquire_integration_development(job, attempt_id="first", policy_snapshot=policy)
    state = {"status": "ready_for_human", "ticket_jobs": {"400": job}, "run_id": "run"}
    intent = bind_resume_intent(state, ticket_number=400)
    assert intent["authorization"] == "renew_integration_development"
    assert intent["pause_reason"] == "integration_development"
    assert intent["ticket_number"] == 400


def test_public_resume_renews_only_integration_and_preserves_other_budgets() -> None:
    from agent_run.controller import apply_resume_to_state
    policy = DeliveryPolicy(ticket_integration_development_limit=1)
    job = {"ticket_number": 400, "ticket_branch_generation": 1,
           "policy_snapshot": policy.snapshot(), "phase": "blocked",
           "blocked_reason": "integration_development_exhausted",
           "review_budget": {"window": 2, "development_attempts": 4},
           "integration_resync": {"used": 3}, "ci_recovery": {"rounds_used": 2}}
    acquire_integration_development(job, attempt_id="first", policy_snapshot=policy.snapshot())
    retained = deepcopy({key: job[key] for key in ("review_budget", "integration_resync", "ci_recovery")})
    state = {"status": "ready_for_human", "ticket_jobs": {"400": job},
             "ticket_integration_holder": None, "ticket_integration_queue": [],
             "ticket_agent_invocations": {}}
    apply_resume_to_state(state, explicit_resume=True, resume_human_blocker=False,
                          new_thread=False, human_response=None, resume_budget_checkpoint=False,
                          budget_policy=policy, ticket_number=400, source_status="ready_for_human")
    assert job["phase"] == "accepted"
    assert "blocked_reason" not in job
    assert job["integration_development_budget"]["used"] == 1
    assert job["integration_development_budget"]["limit"] == 2
    assert {key: job[key] for key in retained} == retained
    apply_resume_to_state(state, explicit_resume=True, resume_human_blocker=False,
                          new_thread=False, human_response=None, resume_budget_checkpoint=False,
                          budget_policy=policy, ticket_number=400, source_status="ticket_delivery_pending")
    assert job["integration_development_budget"]["limit"] == 2


def test_attempt_records_and_history_use_independent_integration_window() -> None:
    from agent_run.semantic_attempt import allocate_semantic_attempt
    from agent_run.attempt_evidence import _budget_facts_for_attempt
    from agent_run.delivery_history import _budget_line
    policy = DeliveryPolicy(ticket_integration_development_limit=1).snapshot()
    job = {"policy_snapshot": policy, "integration_conflict": {"stage": "git_check"},
           "review_budget": {"window": 1, "development_attempts": 4},
           "ticket_number": 400}
    acquire_integration_development(job, attempt_id="first", policy_snapshot=policy)
    renew_integration_development_budget(job, policy_snapshot=policy)
    attempt = allocate_semantic_attempt(job, role="development", work_subject="ticket:400",
                                        generation=1, currentness_boundary={"head": "candidate"},
                                        ordinal=2, budget_window=2)
    acquire_integration_development(job, attempt_id=attempt["attempt_id"], policy_snapshot=policy)
    facts = _budget_facts_for_attempt({"ticket_jobs": {"400": job}}, attempt)
    assert facts == {"window": 2, "integration_development_used": 2,
                     "integration_development_limit": 2}
    assert "2/2" in _budget_line({"role": "development", "budget_facts": facts})
    assert job["review_budget"]["window"] == 1
