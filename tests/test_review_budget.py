from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.controller import _resume_review_budget_window
from agent_run.ci_recovery_budget import (
    mark_ci_repair,
    new_ci_recovery_budget,
    observe_repairable_ci_failure,
)
from agent_run.delivery_policy import (
    DeliveryPolicy,
    parent_only_budget_policy,
    resolve_delivery_policy,
)
from agent_run.review_budget import (
    RUN_POLICY,
    TICKET_POLICY,
    can_start_development,
    can_start_review,
    ensure_budget,
    mark_development,
    mark_review,
    previous_review_context,
    repair_budget_context,
    reset_budget,
    reviewer_budget_context,
    remove_repair_job,
    review_budget_owner,
)
from agent_run.change_delivery_required_checks import observe_required_checks


def _job_with_budget(
    *,
    review_budget: dict[str, object] | None = None,
    **fields: object,
) -> dict[str, object]:
    job: dict[str, object] = {
        "modification_attempts": 0,
        "validation_attempts": 0,
        "review_budget": review_budget
        or {
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 0,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }
    job.update(fields)
    return job


def test_ticket_budget_has_bounded_attempts_reviews_and_final_ci_fix() -> None:
    job = _job_with_budget()

    budget = ensure_budget(job, TICKET_POLICY)

    assert budget == {
        "window": 1,
        "development_attempts": 0,
        "reviewer_invocations": 0,
        "final_ci_fix_used": False,
        "review_artifacts": [],
        "checkpoint_reason": None,
    }
    for _ in range(4):
        mark_development(job, TICKET_POLICY, attempt_kind="ordinary")
    assert not can_start_development(job, TICKET_POLICY)
    assert can_start_development(job, TICKET_POLICY, attempt_kind="final_ci_fix")
    mark_development(job, TICKET_POLICY, attempt_kind="final_ci_fix")
    assert not can_start_development(job, TICKET_POLICY, attempt_kind="final_ci_fix")
    assert job["review_budget"]["development_attempts"] == 4
    assert job["review_budget"]["final_ci_fix_used"] is True

    for _ in range(3):
        mark_review(job, TICKET_POLICY)
    assert not can_start_review(job, TICKET_POLICY)


def test_agent_facing_review_budget_context_handles_fresh_and_resumed_attempts() -> None:
    job = _job_with_budget()
    mark_review(job, TICKET_POLICY)

    assert reviewer_budget_context(
        job, TICKET_POLICY, current_attempt_consumed=False
    ) == {
        "current_review_attempt": 2,
        "remaining_review_attempts": 1,
    }
    assert reviewer_budget_context(
        job, TICKET_POLICY, current_attempt_consumed=True
    ) == {
        "current_review_attempt": 1,
        "remaining_review_attempts": 2,
    }
    assert repair_budget_context(job, TICKET_POLICY) == {
        "completed_review_attempts": 1,
        "remaining_review_attempts": 2,
    }


def test_reset_budget_opens_numbered_window_without_old_attempts() -> None:
    job = _job_with_budget(
        review_budget={
            "window": 1,
            "development_attempts": 4,
            "reviewer_invocations": 3,
            "final_ci_fix_used": True,
            "review_artifacts": [{"id": "old"}],
            "checkpoint_reason": "modification_budget_exhausted",
        },
        modification_attempts=4,
        validation_attempts=3,
        acceptance_artifact={"finding": "old"},
        publication_authority="fallback",
        fallback_publication_receipt={"receipt": "old"},
    )

    budget = reset_budget(job, TICKET_POLICY)

    assert budget["window"] == 2
    assert budget["development_attempts"] == 0
    assert budget["reviewer_invocations"] == 0
    assert budget["final_ci_fix_used"] is False
    assert budget["review_artifacts"] == []
    assert job["modification_attempts"] == 0
    assert job["validation_attempts"] == 0
    assert job["review_budget_history"][0]["review_budget"][
        "reviewer_invocations"
    ] == 3
    assert job["review_budget_history"][0]["acceptance_artifact"] == {
        "finding": "old"
    }
    assert "review_budget_history" not in job["review_budget_history"][0]
    assert "publication_authority" not in job
    assert "fallback_publication_receipt" not in job


def test_run_policy_allows_three_developments_and_four_reviews() -> None:
    job = _job_with_budget()

    for _ in range(4):
        mark_review(job, RUN_POLICY)
    assert not can_start_review(job, RUN_POLICY)
    assert can_start_development(job, RUN_POLICY)


def test_default_parent_policy_allows_exactly_ten_paired_rounds() -> None:
    policy = parent_only_budget_policy(resolve_delivery_policy())
    job = _job_with_budget()

    assert policy.development_limit == policy.review_limit == 10
    assert policy.fallback is False
    assert policy.final_ci_fix_limit == 0
    for ordinal in range(1, 11):
        assert can_start_development(job, policy)
        assert mark_development(job, policy, attempt_kind="ordinary") == ordinal
        assert can_start_review(job, policy)
        assert mark_review(job, policy) == ordinal

    assert not can_start_development(job, policy)
    assert not can_start_development(job, policy, attempt_kind="final_ci_fix")
    assert not can_start_review(job, policy)
    with pytest.raises(ValueError, match="development budget is exhausted"):
        mark_development(job, policy, attempt_kind="ordinary")
    with pytest.raises(ValueError, match="review budget is exhausted"):
        mark_review(job, policy)
    assert job["review_budget"]["development_attempts"] == 10
    assert job["review_budget"]["reviewer_invocations"] == 10


def test_parent_budget_resume_validates_old_window_before_applying_new_policy() -> None:
    old_policy = DeliveryPolicy(parent_only_paired_rounds=10)
    new_policy = DeliveryPolicy(parent_only_paired_rounds=2)
    job = _job_with_budget(
        review_budget={
            "window": 1,
            "development_attempts": 10,
            "reviewer_invocations": 10,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": "review_budget_exhausted",
        },
        parent_branch="parent-branch",
        policy_snapshot=old_policy.snapshot(),
        blocked_reason="review_budget_exhausted",
        phase="blocked",
    )
    state = {
        "parent_job": job,
        "policy_snapshot": old_policy.snapshot(),
        "status": "blocked",
    }

    assert _resume_review_budget_window(state, new_policy)
    assert job["review_budget"]["window"] == 2
    assert job["policy_snapshot"] == new_policy.snapshot()
    assert job["review_budget_history"][0]["policy_snapshot"] == old_policy.snapshot()
    assert state["policy_snapshot"] == new_policy.snapshot()


@pytest.mark.parametrize("value", [None, [], {"window": 0}])
def test_invalid_budget_shape_fails_closed(value: object) -> None:
    job: dict[str, object] = {"review_budget": value}
    with pytest.raises(ValueError):
        ensure_budget(job, TICKET_POLICY)


def test_legacy_attempt_counters_are_not_migrated_into_a_budget() -> None:
    with pytest.raises(ValueError, match="missing canonical review_budget"):
        ensure_budget(
            {"modification_attempts": 4, "validation_attempts": 3}, TICKET_POLICY
        )


def test_policy_limits_are_enforced_when_loading_a_budget() -> None:
    job = _job_with_budget(
        review_budget={
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 4,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        }
    )

    with pytest.raises(ValueError, match="reviewer_invocations exceeds"):
        ensure_budget(job, TICKET_POLICY)

    job = _job_with_budget(
        review_budget={
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 0,
            "final_ci_fix_used": True,
            "review_artifacts": [],
            "checkpoint_reason": None,
        }
    )
    with pytest.raises(ValueError, match="final_ci_fix_used is not allowed"):
        ensure_budget(job, RUN_POLICY)


@pytest.mark.parametrize(
    "history",
    [
        [],
        [{}],
        [
            {
                "phase": "blocked",
                "modification_attempts": 1,
                "validation_attempts": 1,
                "review_budget": {
                    "window": 2,
                    "development_attempts": 1,
                    "reviewer_invocations": 1,
                    "final_ci_fix_used": False,
                    "review_artifacts": [],
                    "checkpoint_reason": None,
                },
            }
        ],
    ],
)
def test_budget_history_must_preserve_consecutive_complete_windows(
    history: list[dict[str, object]],
) -> None:
    job = _job_with_budget(
        review_budget={
            "window": 2,
            "development_attempts": 0,
            "reviewer_invocations": 0,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
    )
    job["review_budget_history"] = history

    with pytest.raises(ValueError, match="review_budget_history"):
        ensure_budget(job, TICKET_POLICY)


def test_budget_history_requires_old_attempt_audit_facts() -> None:
    job = _job_with_budget(
        review_budget={
            "window": 2,
            "development_attempts": 0,
            "reviewer_invocations": 0,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        review_budget_history=[
            {
                "phase": "blocked",
                "review_budget": {
                    "window": 1,
                    "development_attempts": 1,
                    "reviewer_invocations": 1,
                    "final_ci_fix_used": False,
                    "review_artifacts": [],
                    "checkpoint_reason": None,
                },
            }
        ],
    )

    with pytest.raises(ValueError, match="audit snapshot"):
        ensure_budget(job, TICKET_POLICY)


def test_previous_review_context_keeps_the_previous_candidate_identity() -> None:
    previous_identity = {
        "reviewed_base_sha": "base-c1",
        "reviewed_candidate_sha": "candidate-c1",
        "reviewed_candidate_tree": "tree-c1",
    }
    job = _job_with_budget(
        review_budget={
            "window": 1,
            "development_attempts": 1,
            "reviewer_invocations": 1,
            "final_ci_fix_used": False,
            "review_artifacts": [
                {
                    "artifact": {"finding": "fixed in C2"},
                    "review_identity": previous_identity,
                }
            ],
            "checkpoint_reason": None,
        },
        candidate_sha="candidate-c2",
    )

    context = previous_review_context(job)

    assert context == {
        "artifact": {"finding": "fixed in C2"},
        "identity": previous_identity,
    }


def test_ticket_budget_resume_opens_new_window_and_preserves_repair_evidence() -> None:
    job: dict[str, object] = {
            "phase": "blocked",
            "blocked_reason": "modification_budget_exhausted",
            "repair_source": "acceptance",
            "acceptance_artifact": {"checks": "previous"},
        "review_budget": {
            "window": 1,
            "development_attempts": 4,
            "reviewer_invocations": 3,
            "final_ci_fix_used": True,
            "review_artifacts": [{"artifact": "previous"}],
            "checkpoint_reason": "modification_budget_exhausted",
        },
        "review_budget_history": [],
    }
    state = {"active_ticket_job": job}

    assert _resume_review_budget_window(state)
    assert job["phase"] == "repairing"
    assert job["review_budget"] == {
        "window": 2,
        "development_attempts": 0,
        "reviewer_invocations": 0,
        "final_ci_fix_used": False,
        "review_artifacts": [],
        "checkpoint_reason": None,
    }
    assert job["repair_source"] == "acceptance"
    assert job["review_budget_history"][0]["review_budget"][
        "reviewer_invocations"
    ] == 3


def test_run_repair_owns_one_persisted_run_window() -> None:
    import json

    from agent_run.run_repair_cycle import sync_repair_cycle_counters

    job = _job_with_budget(
        policy_snapshot=DeliveryPolicy().snapshot(), phase="reviewing",
    )
    job["review_budget"]["reviewer_invocations"] = 3
    run = {
        "policy_snapshot": DeliveryPolicy().snapshot(),
        "repair_job": job,
        "repair_cycle": {"generation": 1},
    }
    run = json.loads(json.dumps(run))
    job = run["repair_job"]
    mark_review(job, RUN_POLICY)
    sync_repair_cycle_counters(run, job)
    assert ensure_budget(run, RUN_POLICY)["reviewer_invocations"] == 4
    assert not can_start_review(run, RUN_POLICY)
    assert "review_budget" not in run
    assert "review_budget_history" not in run
    budget = job["review_budget"]
    history = job["review_budget_history"]
    remove_repair_job(run)
    assert run["review_budget"] is budget
    assert run["review_budget_history"] is history
    assert "repair_job" not in run
    assert not can_start_review(run, RUN_POLICY)


def test_run_repair_budget_uses_frozen_limits_and_retains_one_history() -> None:
    from agent_run.delivery_policy import run_repair_budget_policy
    from agent_run.run_repair_cycle import sync_repair_cycle_counters

    policy = DeliveryPolicy(run_repair_rounds=2)
    job = _job_with_budget(policy_snapshot=policy.snapshot(), phase="blocked")
    job["review_budget"].update({
        "development_attempts": 2,
        "reviewer_invocations": 3,
        "review_artifacts": [
            {"review_identity": {"attempt_id": f"r{ordinal}"}}
            for ordinal in range(1, 4)
        ],
        "checkpoint_reason": "review_budget_exhausted",
    })
    run = {
        "policy_snapshot": policy.snapshot(),
        "repair_job": job,
        "repair_cycle": {"generation": 1},
        "validation_attempts": 1,
    }
    sync_repair_cycle_counters(run, job)
    budget_policy = run_repair_budget_policy(policy)
    assert not can_start_development(run, budget_policy)
    assert not can_start_review(run, budget_policy)
    reset_budget(run, budget_policy)
    assert review_budget_owner(run) is job
    assert run["validation_attempts"] == 0
    assert job["review_budget"]["window"] == 2
    assert len(job["review_budget_history"]) == 1
    archived = job["review_budget_history"][0]["review_budget"]
    assert archived["development_attempts"] == 2
    assert archived["reviewer_invocations"] == 3
    assert len(archived["review_artifacts"]) == 3
    assert archived["checkpoint_reason"] == "review_budget_exhausted"
    assert "review_budget_history" not in run


def test_exhausted_ticket_demand_budget_keeps_independent_ci_repair_capacity() -> None:
    job = _job_with_budget(
        policy_snapshot=DeliveryPolicy().snapshot(),
        ci_recovery=new_ci_recovery_budget(),
    )
    budget = job["review_budget"]
    budget["development_attempts"] = TICKET_POLICY.development_limit
    budget["reviewer_invocations"] = TICKET_POLICY.review_limit
    assert not can_start_development(job, TICKET_POLICY)
    assert not can_start_review(job, TICKET_POLICY)

    assert observe_repairable_ci_failure(
        job, head_sha="candidate-head", outer_limit=3, inner_limit=3,
        is_ticket=True,
    ) == "repair"
    assert mark_ci_repair(
        job, outer_limit=3, inner_limit=3, is_ticket=True,
    ) == 1
    assert job["ci_recovery"]["rounds_used"] == 1
    assert job["ci_recovery"]["repairs_used"] == 1
    assert budget["development_attempts"] == TICKET_POLICY.development_limit
    assert budget["reviewer_invocations"] == TICKET_POLICY.review_limit


@pytest.mark.parametrize(
    "error",
    [OSError("snapshot transport unavailable"), TimeoutError("snapshot timed out")],
)
def test_success_required_checks_snapshot_read_is_supervised(error: BaseException) -> None:
    job: dict[str, object] = {
        "phase": "published",
        "publication_sha": "candidate-head",
    }
    stage = SimpleNamespace(
        github=SimpleNamespace(
            required_checks_snapshot=lambda _pr, expected_head_sha: (_ for _ in ()).throw(
                error
            ),
        ),
        _record_agent_run_status=lambda *_args, **_kwargs: None,
        _reject_stale=lambda *_args, **_kwargs: None,
        save=lambda state: state,
    )
    state = {"status": "active"}

    outcome, checks = observe_required_checks(stage, state, job, Path("."), 11)

    assert outcome is True
    assert checks == "unavailable"
    assert state["status"] == "waiting_external"
    assert state["diagnostics"][0]["code"] == (
        "github_checks_observation_pending"
    )
    assert "publication_operation_retry" not in job
    assert "last_publication_error" not in job
