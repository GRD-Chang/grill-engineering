from __future__ import annotations

import pytest
from typing import Any

from agent_run.ci_recovery_budget import (
    can_start_ci_repair,
    checkpoint_after_reviewer_failure,
    ci_review_required,
    ensure_ci_recovery_budget,
    mark_ci_repair,
    new_ci_recovery_budget,
    new_ticket_ci_recovery_budget,
    observe_repairable_ci_failure,
    refund_ci_repair_without_candidate,
    reset_exhausted_ci_budget,
)


def _job() -> dict[str, Any]:
    return {"ci_recovery": new_ci_recovery_budget()}


@pytest.mark.parametrize("parent_kind", ["parent_only", "run_acceptance"])
def test_parent_ci_failure_versions_and_review_share_cumulative_allowance(parent_kind: str) -> None:
    job = _job()
    job["kind"] = parent_kind
    job["review_budget"] = {"development_used": 100}
    job["integration_recovery"] = {"developments_used": 100}
    for number in range(1, 4):
        assert observe_repairable_ci_failure(job, head_sha=f"failure-{number}", limit=3, is_ticket=False) == "repair"
        assert ci_review_required(job, is_ticket=False)
        assert mark_ci_repair(job, limit=3, is_ticket=False, semantic_work_id=f"work-{number}") == number
        assert mark_ci_repair(job, limit=3, is_ticket=False, semantic_work_id=f"work-{number}") == number
        assert checkpoint_after_reviewer_failure(job, limit=3, is_ticket=False) is (number == 3)
    assert observe_repairable_ci_failure(job, head_sha="next-pr-failure", limit=3, is_ticket=False) == "exhausted"
    assert job["ci_recovery"]["developments_used"] == 3
    assert reset_exhausted_ci_budget(job, limit=3, is_ticket=False) == "ci_development_budget_exhausted"
    assert reset_exhausted_ci_budget(job, limit=3, is_ticket=False) == ""
    assert job["ci_recovery"]["window"] == 2
    assert len([item for item in job["ci_recovery"]["history"] if item["kind"] == "resume"]) == 1
    assert job["review_budget"] == {"development_used": 100}
    assert job["integration_recovery"] == {"developments_used": 100}
    assert can_start_ci_repair(job, limit=3, is_ticket=False)


@pytest.mark.parametrize("review_enabled", [False, True])
def test_ticket_ci_uses_one_cumulative_budget_and_stable_review(review_enabled: bool) -> None:
    job = {"ci_recovery": new_ticket_ci_recovery_budget(review_required=review_enabled)}
    for number in range(1, 4):
        assert observe_repairable_ci_failure(
            job, head_sha=f"failure-{number}", limit=3, is_ticket=True
        ) == "repair"
        assert ci_review_required(job, is_ticket=True) is review_enabled
        # Re-observing facts never charges work.
        assert observe_repairable_ci_failure(
            job, head_sha=f"failure-{number}", limit=3, is_ticket=True
        ) == "repair"
        assert mark_ci_repair(
            job, limit=3, is_ticket=True,
            semantic_work_id=f"work-{number}",
        ) == number
        # Interrupted work resumes its semantic identity, even at the limit.
        assert mark_ci_repair(
            job, limit=3, is_ticket=True,
            semantic_work_id=f"work-{number}",
        ) == number
    assert observe_repairable_ci_failure(
        job, head_sha="failure-4", limit=3, is_ticket=True
    ) == "exhausted"
    assert job["ci_recovery"]["developments_used"] == 3
    assert reset_exhausted_ci_budget(
        job, limit=3, is_ticket=True
    ) == "ci_development_budget_exhausted"
    assert job["ci_recovery"]["window"] == 2
    assert reset_exhausted_ci_budget(
        job, limit=3, is_ticket=True
    ) == ""
    assert job["ci_recovery"]["window"] == 2
    assert ci_review_required(job, is_ticket=True) is review_enabled
    assert mark_ci_repair(job, limit=3, is_ticket=True) == 1


def test_ticket_review_findings_consume_the_same_ci_allowance() -> None:
    job = {"ci_recovery": new_ticket_ci_recovery_budget(review_required=True),
           "review_budget": {"development_used": 100},
           "integration_recovery": {"developments_used": 100}}
    observe_repairable_ci_failure(job, head_sha="first", limit=2, is_ticket=True)
    mark_ci_repair(job, limit=2, is_ticket=True)
    assert not checkpoint_after_reviewer_failure(job, limit=2, is_ticket=True)
    mark_ci_repair(job, limit=2, is_ticket=True)
    assert checkpoint_after_reviewer_failure(job, limit=2, is_ticket=True)
    assert job["ci_recovery"]["checkpoint_reason"] == "ci_development_budget_exhausted"
    reset_exhausted_ci_budget(job, limit=2, is_ticket=True)
    assert job["review_budget"] == {"development_used": 100}
    assert job["integration_recovery"] == {"developments_used": 100}


def test_parent_ci_investigation_without_code_consumes_allowance() -> None:
    job = _job()
    observe_repairable_ci_failure(job, head_sha="first", limit=3, is_ticket=False)
    mark_ci_repair(job, limit=3, is_ticket=False)
    refund_ci_repair_without_candidate(job, limit=3, is_ticket=False)
    assert job["ci_recovery"]["developments_used"] == 1
    assert observe_repairable_ci_failure(job, head_sha="new-pr", limit=3, is_ticket=False) == "repair"
    assert job["ci_recovery"]["developments_used"] == 1


def test_missing_or_incompatible_ci_budget_fails_closed() -> None:
    with pytest.raises(ValueError, match="missing canonical"):
        ensure_ci_recovery_budget(
            {}, limit=3, is_ticket=True
        )
    job = _job()
    job["ci_recovery"]["developments_used"] = 1
    with pytest.raises(ValueError, match="missing its failure head"):
        ensure_ci_recovery_budget(
            job, limit=3, is_ticket=False
        )


def test_ticket_ci_investigation_without_code_still_consumes_development() -> None:
    job = {"ci_recovery": new_ticket_ci_recovery_budget()}
    observe_repairable_ci_failure(job, head_sha="failed", limit=1, is_ticket=True)
    mark_ci_repair(job, limit=1, is_ticket=True)
    refund_ci_repair_without_candidate(job, limit=1, is_ticket=True)
    assert job["ci_recovery"]["developments_used"] == 1
    assert not job["ci_recovery"]["repair_pending"]
    assert observe_repairable_ci_failure(job, head_sha="failed", limit=1, is_ticket=True) == "exhausted"


def test_parent_limit_does_not_inherit_ticket_policy_from_shared_budget_shape() -> None:
    from agent_run.ci_recovery_budget import ci_limit_for_job
    from agent_run.delivery_policy import DeliveryPolicy

    snapshot = DeliveryPolicy(
        ticket_ci_development_limit=2, parent_ci_development_limit=5,
        ticket_ci_review=False,
    ).snapshot()
    parent = {"policy_snapshot": snapshot, "ci_recovery": new_ci_recovery_budget()}
    ticket = {
        "ticket_number": 404, "policy_snapshot": snapshot,
        "ci_recovery": new_ticket_ci_recovery_budget(),
    }
    assert ci_limit_for_job(parent) == 5
    assert ci_limit_for_job(ticket) == 2
    assert ci_review_required(parent, is_ticket=False)
    assert not ci_review_required(ticket, is_ticket=True)
