from __future__ import annotations

from typing import Any

import pytest

from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
    close_semantic_attempt,
    controller_reprepare_intent,
    invocation_is_explicitly_resumable,
    pending_semantic_attempt,
    require_controller_reprepare_intent,
    require_semantic_attempt,
)


def _boundary() -> dict[str, str]:
    return {"base_sha": "base", "candidate_sha": "candidate"}


@pytest.mark.parametrize(
    ("owner_key", "status"),
    [
        ("active_ticket_job", "active"),
        ("parent_job", "parent_delivery_pending"),
        ("run_acceptance", "run_acceptance_pending"),
        ("run_publication", "run_publication_pending"),
    ],
)
def test_resuming_exact_attempt_is_role_independently_resumable(
    owner_key: str, status: str
) -> None:
    attempt = {"attempt_id": "sha256:" + "1" * 64}
    state = {
        "status": status,
        owner_key: {"pending_semantic_attempt": attempt},
        "active_agent_invocation": {
            "status": "resuming",
            "semantic_attempt": attempt,
        },
    }

    assert invocation_is_explicitly_resumable(state) is True


def test_pending_semantic_attempt_is_reused_without_advancing_ordinal() -> None:
    subject: dict[str, Any] = {}

    first = allocate_semantic_attempt(
        subject,
        role="development",
        work_subject="ticket:141",
        generation=2,
        currentness_boundary=_boundary(),
        ordinal=4,
        budget_window=1,
    )
    resumed = allocate_semantic_attempt(
        subject,
        role="development",
        work_subject="ticket:141",
        generation=2,
        currentness_boundary=_boundary(),
        ordinal=5,
        budget_window=1,
    )

    assert resumed == first
    assert resumed["ordinal"] == 4
    assert resumed["budget_window"] == 1
    assert pending_semantic_attempt(subject, role="development") == first


def test_semantic_attempt_identity_changes_only_after_canonical_closeout() -> None:
    subject: dict[str, Any] = {}
    first = allocate_semantic_attempt(
        subject,
        role="publication",
        work_subject="run-publication:run-141",
        generation=1,
        currentness_boundary=_boundary(),
        ordinal=1,
    )

    close_semantic_attempt(subject, first, outcome="completed")
    second = allocate_semantic_attempt(
        subject,
        role="publication",
        work_subject="run-publication:run-141",
        generation=1,
        currentness_boundary=_boundary(),
        ordinal=2,
    )

    assert first["attempt_id"] != second["attempt_id"]
    assert subject["semantic_attempt_history"] == [
        {**first, "status": "completed", "outcome": "completed"}
    ]
    assert second["budget_window"] is None


def test_pending_attempt_rejects_currentness_or_generation_drift() -> None:
    subject: dict[str, Any] = {}
    attempt = allocate_semantic_attempt(
        subject,
        role="reviewer",
        work_subject="ticket:141",
        generation=1,
        currentness_boundary=_boundary(),
        ordinal=3,
        budget_window=2,
    )

    with pytest.raises(ValueError, match="currentness boundary"):
        require_semantic_attempt(
            attempt,
            role="reviewer",
            work_subject="ticket:141",
            generation=1,
            currentness_boundary={"base_sha": "new-base"},
            budget_window=2,
        )
    with pytest.raises(ValueError, match="generation"):
        require_semantic_attempt(
            attempt,
            role="reviewer",
            work_subject="ticket:141",
            generation=2,
            currentness_boundary=_boundary(),
            budget_window=2,
        )


def test_controller_reprepare_intent_fails_closed_after_boundary_tampering() -> None:
    job: dict[str, Any] = {
        "run_id": "run-1",
        "repair_generation": 2,
        "phase": "committing_candidate",
        "base_sha": "base",
        "default_base_sha": "default",
        "managed_checkout_head": "head",
        "pending_attempt": 3,
    }
    job["controller_candidate_reprepare"] = controller_reprepare_intent(job)

    require_controller_reprepare_intent(job)
    job["default_base_sha"] = "changed"

    with pytest.raises(ValueError, match="intent is invalid"):
        require_controller_reprepare_intent(job)
