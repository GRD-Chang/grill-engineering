from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.controller import _resume_review_budget_window
from agent_run.review_budget import (
    RUN_POLICY,
    TICKET_POLICY,
    can_start_development,
    can_start_review,
    ensure_budget,
    mark_development,
    mark_review,
    previous_review_context,
    reset_budget,
)
from agent_run.change_delivery_required_checks import observe_required_checks
from agent_run.publication_operation_retry import record_publication_operation_failure


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


def test_run_policy_keeps_existing_development_capacity_but_caps_reviews() -> None:
    job = _job_with_budget()

    for _ in range(5):
        mark_review(job, RUN_POLICY)
    assert not can_start_review(job, RUN_POLICY)
    assert can_start_development(job, RUN_POLICY)


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


def test_run_repair_review_counts_share_the_run_window() -> None:
    from agent_run.run_repair_cycle import sync_repair_cycle_counters

    run: dict[str, object] = {
        "review_budget": {
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 4,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
        "repair_cycle": {"generation": 1},
    }
    job: dict[str, object] = {
        "modification_attempts": 0,
        "validation_attempts": 0,
        "review_budget": {
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 4,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }
    mark_review(job, RUN_POLICY)
    sync_repair_cycle_counters(run, job)
    assert run["review_budget"]["reviewer_invocations"] == 5


def test_exhausted_ticket_development_allows_one_exact_head_final_ci_fix() -> None:
    evidence = {
        "pr_number": 11,
        "checks": [
            {
                "bucket": "fail",
                "state": "FAILURE",
                "name": "tests",
                "workflow": "ci",
                "link": "https://example.invalid/check",
                "repairability": "code_failure",
            }
        ],
    }
    job: dict[str, object] = {
        "phase": "waiting_checks",
        "base_sha": "base-head",
        "publication_sha": "candidate-head",
        "acceptance_record": {"reviewed_base_sha": "base-head"},
        "modification_attempts": 4,
        "validation_attempts": 2,
        "review_budget": {
            "window": 1,
            "development_attempts": 4,
            "reviewer_invocations": 2,
            "final_ci_fix_used": False,
            "review_artifacts": [],
            "checkpoint_reason": None,
        },
        "review_budget_history": [],
    }
    repair_resumes: list[tuple[dict[str, object], dict[str, object]]] = []

    def resume_after_required_checks_failure(
        state: dict[str, object], repair_job: dict[str, object]
    ) -> bool:
        repair_resumes.append((state, repair_job))
        state.update(
            {"status": "adapter_resumed", "terminal_kind": None, "diagnostics": []}
        )
        return False

    stage = SimpleNamespace(
        contract=SimpleNamespace(
            label="ticket-1", branch="ticket-1", base_branch="run-1"
        ),
        github=SimpleNamespace(
            required_checks_snapshot=lambda _pr, expected_head_sha: {
                "pr_number": 11,
                "head_sha": expected_head_sha,
                "result": "fail",
                "checks": [dict(check) for check in evidence["checks"]],
            },
            required_check_evidence=lambda _pr, expected_head_sha: evidence,
        ),
        publisher=SimpleNamespace(
            live_pull_request=lambda _state, _job, _pr: {
                "state": "OPEN",
                "head_branch": "ticket-1",
                "head_sha": "candidate-head",
                "head_repository": "example/project",
                "base_branch": "run-1",
                "base_sha": "base-head",
                "base_repository": "example/project",
            }
        ),
        adapter=SimpleNamespace(
            classify_required_check_failures=True,
            resume_after_required_checks_failure=(
                resume_after_required_checks_failure
            ),
        ),
        modification_budget_exhausted=lambda _job: True,
        review_budget_policy=lambda: TICKET_POLICY,
        _record_agent_run_status=lambda *_args, **_kwargs: None,
        _record_publication_operation_failure_in_memory=(
            lambda _state, owner, failure: record_publication_operation_failure(
                owner, failure
            )
        ),
        _reject_stale=lambda *_args, **_kwargs: None,
        commit_required_checks_observation=lambda state, _job, _pr: state,
        save=lambda state: state,
    )

    state: dict[str, object] = {
        "status": "active",
        "repository": "example/project",
    }
    outcome, checks = observe_required_checks(
        stage,
        state,
        job,
        Path("."),
        11,
    )

    assert outcome is False
    assert checks == "fail"
    assert job["phase"] == "repairing"
    assert job["next_attempt_kind"] == "final_ci_fix"
    assert job["final_ci_fix_failure_head"] == "candidate-head"
    assert repair_resumes == [(state, job)]
    assert state == {
        "status": "adapter_resumed",
        "repository": "example/project",
        "terminal_kind": None,
        "diagnostics": [],
    }


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
        _record_publication_operation_failure_in_memory=(
            lambda _state, owner, failure: record_publication_operation_failure(
                owner, failure
            )
        ),
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
    assert job["publication_operation_retry"] == {"attempts": 1, "limit": 5}
