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
    observe_repairable_ci_failure,
    refund_ci_repair_without_candidate,
    reset_exhausted_ci_budget,
)


def _job() -> dict[str, Any]:
    return {"ci_recovery": new_ci_recovery_budget()}


@pytest.mark.parametrize("is_ticket,expected_repairs", [(True, 7), (False, 9)])
def test_default_ci_recovery_repair_matrix(
    is_ticket: bool, expected_repairs: int
) -> None:
    job = _job()
    repairs = 0
    for round_number in range(1, 4):
        failed_head = f"failed-{round_number}"
        assert observe_repairable_ci_failure(
            job, head_sha=failed_head, outer_limit=3,
            inner_limit=3, is_ticket=is_ticket,
        ) == "repair"
        assert ci_review_required(job, is_ticket=is_ticket) is (
            not is_ticket or round_number > 1
        )
        round_repairs = 1 if is_ticket and round_number == 1 else 3
        for attempt in range(round_repairs):
            mark_ci_repair(job, outer_limit=3, inner_limit=3, is_ticket=is_ticket)
            repairs += 1
            if attempt + 1 < round_repairs:
                assert not checkpoint_after_reviewer_failure(
                    job, outer_limit=3, inner_limit=3, is_ticket=is_ticket
                )
        if round_number < 3:
            assert job["ci_recovery"]["rounds_used"] == round_number
    assert repairs == expected_repairs
    assert observe_repairable_ci_failure(
        job, head_sha="failed-4", outer_limit=3,
        inner_limit=3, is_ticket=is_ticket,
    ) == "outer_exhausted"
    assert job["ci_recovery"]["checkpoint_reason"] == "ci_outer_budget_exhausted"


def test_review_rejection_stays_in_round_and_inner_resume_preserves_outer() -> None:
    job = _job()
    observe_repairable_ci_failure(
        job, head_sha="first", outer_limit=2, inner_limit=2, is_ticket=False
    )
    assert mark_ci_repair(
        job, outer_limit=2, inner_limit=2, is_ticket=False
    ) == 1
    assert not checkpoint_after_reviewer_failure(
        job, outer_limit=2, inner_limit=2, is_ticket=False
    )
    assert job["ci_recovery"]["rounds_used"] == 1
    assert mark_ci_repair(
        job, outer_limit=2, inner_limit=2, is_ticket=False
    ) == 2
    assert checkpoint_after_reviewer_failure(
        job, outer_limit=2, inner_limit=2, is_ticket=False
    )
    assert job["ci_recovery"]["checkpoint_reason"] == "ci_inner_budget_exhausted"
    assert reset_exhausted_ci_budget(
        job, outer_limit=2, inner_limit=2, is_ticket=False
    ) == "ci_inner_budget_exhausted"
    assert job["ci_recovery"]["rounds_used"] == 1
    assert job["ci_recovery"]["inner_window"] == 2
    assert can_start_ci_repair(
        job, outer_limit=2, inner_limit=2, is_ticket=False
    )


def test_ticket_review_upgrade_survives_outer_resume() -> None:
    job = _job()
    observe_repairable_ci_failure(
        job, head_sha="first", outer_limit=2, inner_limit=3, is_ticket=True
    )
    mark_ci_repair(job, outer_limit=2, inner_limit=3, is_ticket=True)
    observe_repairable_ci_failure(
        job, head_sha="second", outer_limit=2, inner_limit=3, is_ticket=True
    )
    mark_ci_repair(job, outer_limit=2, inner_limit=3, is_ticket=True)
    assert observe_repairable_ci_failure(
        job, head_sha="third", outer_limit=2, inner_limit=3, is_ticket=True
    ) == "outer_exhausted"
    assert reset_exhausted_ci_budget(
        job, outer_limit=2, inner_limit=3, is_ticket=True
    ) == "ci_outer_budget_exhausted"
    assert job["ci_recovery"]["outer_window"] == 2
    assert job["ci_recovery"]["rounds_used"] == 1
    assert ci_review_required(job, is_ticket=True)
    assert mark_ci_repair(
        job, outer_limit=2, inner_limit=3, is_ticket=True
    ) == 1


def test_first_ticket_ci_round_outer_exhaustion_upgrades_review_before_resume() -> None:
    job = _job()
    assert observe_repairable_ci_failure(
        job, head_sha="first", outer_limit=1, inner_limit=3, is_ticket=True
    ) == "repair"
    mark_ci_repair(job, outer_limit=1, inner_limit=3, is_ticket=True)

    assert observe_repairable_ci_failure(
        job, head_sha="second", outer_limit=1, inner_limit=3, is_ticket=True
    ) == "outer_exhausted"
    budget = ensure_ci_recovery_budget(
        job, outer_limit=1, inner_limit=3, is_ticket=True
    )
    assert budget["failure_head"] == "second"
    assert budget["review_required"] is True
    assert budget["checkpoint_reason"] == "ci_outer_budget_exhausted"

    reset_exhausted_ci_budget(
        job, outer_limit=1, inner_limit=3, is_ticket=True
    )
    assert ci_review_required(job, is_ticket=True)
    assert mark_ci_repair(job, outer_limit=1, inner_limit=3, is_ticket=True) == 1


def test_same_head_replay_and_unstarted_refund_do_not_spend_a_round() -> None:
    job = _job()
    observe_repairable_ci_failure(
        job, head_sha="first", outer_limit=3, inner_limit=3, is_ticket=False
    )
    mark_ci_repair(job, outer_limit=3, inner_limit=3, is_ticket=False)
    refund_ci_repair_without_candidate(
        job, outer_limit=3, inner_limit=3, is_ticket=False
    )
    assert job["ci_recovery"]["repairs_used"] == 0
    assert observe_repairable_ci_failure(
        job, head_sha="first", outer_limit=3, inner_limit=3, is_ticket=False
    ) == "repair"
    assert job["ci_recovery"]["rounds_used"] == 1
    with pytest.raises(ValueError, match="no preceding repair"):
        observe_repairable_ci_failure(
            job, head_sha="unrelated", outer_limit=3,
            inner_limit=3, is_ticket=False,
        )


def test_missing_or_incompatible_ci_budget_fails_closed() -> None:
    with pytest.raises(ValueError, match="missing canonical"):
        ensure_ci_recovery_budget(
            {}, outer_limit=3, inner_limit=3, is_ticket=True
        )
    job = _job()
    job["ci_recovery"]["repairs_used"] = 1
    with pytest.raises(ValueError, match="unused CI recovery"):
        ensure_ci_recovery_budget(
            job, outer_limit=3, inner_limit=3, is_ticket=True
        )
