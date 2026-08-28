from __future__ import annotations

"""Small, deterministic policy helpers for Review Budget Windows.

The engine owns the transitions; this module only defines the mechanical
limits and the durable shape shared by Ticket, Parent-only, and Run Repair
jobs.  Keeping this policy pure makes the cost boundary easy to test without
creating a second orchestration path.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, TypedDict, cast

from agent_run.required_checks_observation import clear_required_checks_observation


TICKET_DEVELOPMENT_LIMIT = 4
TICKET_REVIEW_LIMIT = 3
FINAL_CI_FIX_LIMIT = 1
RUN_REVIEW_LIMIT = 5
MAX_REVIEW_ARTIFACTS = RUN_REVIEW_LIMIT
_REVIEW_BUDGET_KEYS = frozenset(
    {
        "window",
        "development_attempts",
        "reviewer_invocations",
        "final_ci_fix_used",
        "review_artifacts",
        "checkpoint_reason",
    }
)


@dataclass(frozen=True)
class ReviewBudgetPolicy:
    """Mechanical limits for one budget window."""

    development_limit: int
    review_limit: int
    final_ci_fix_limit: int = 0
    fallback: bool = False


class ReviewBudget(TypedDict):
    window: int
    development_attempts: int
    reviewer_invocations: int
    final_ci_fix_used: bool
    review_artifacts: list[dict[str, Any]]
    checkpoint_reason: str | None


def new_budget(
    *,
    window: int = 1,
    development_attempts: int = 0,
    reviewer_invocations: int = 0,
    final_ci_fix_used: bool = False,
    review_artifacts: list[dict[str, Any]] | None = None,
    checkpoint_reason: str | None = None,
) -> ReviewBudget:
    """Build one canonical empty or seeded Review Budget projection."""

    return {
        "window": window,
        "development_attempts": development_attempts,
        "reviewer_invocations": reviewer_invocations,
        "final_ci_fix_used": final_ci_fix_used,
        "review_artifacts": deepcopy(review_artifacts or []),
        "checkpoint_reason": checkpoint_reason,
    }


TICKET_POLICY = ReviewBudgetPolicy(
    development_limit=TICKET_DEVELOPMENT_LIMIT,
    review_limit=TICKET_REVIEW_LIMIT,
    final_ci_fix_limit=FINAL_CI_FIX_LIMIT,
    fallback=True,
)
RUN_POLICY = ReviewBudgetPolicy(
    development_limit=10,
    review_limit=RUN_REVIEW_LIMIT,
)


def policy_for_subject(label: str) -> ReviewBudgetPolicy:
    """Return the policy for a shared-engine subject label."""

    return TICKET_POLICY if label.startswith("ticket-") else RUN_POLICY


def ensure_budget(job: dict[str, Any], policy: ReviewBudgetPolicy) -> ReviewBudget:
    """Validate the canonical, durable budget projection.

    A missing projection is an incompatible persisted Job, not an invitation
    to infer a new budget from legacy counters.
    """

    if "review_budget" not in job:
        raise ValueError("missing canonical review_budget")
    raw = job["review_budget"]
    if not isinstance(raw, dict):
        raise ValueError("review_budget must be an object")
    budget = cast(ReviewBudget, raw)
    _validate_budget_projection(budget, "review_budget", policy)
    history = job.get("review_budget_history")
    if not isinstance(history, list) or not all(
        isinstance(item, dict) for item in history
    ):
        raise ValueError("missing canonical review_budget_history")
    _validate_budget_history(history, budget["window"], policy)
    return budget


def _validate_budget_projection(
    budget: Mapping[str, Any], location: str, policy: ReviewBudgetPolicy
) -> None:
    if set(budget) != _REVIEW_BUDGET_KEYS:
        raise ValueError(f"{location} has an invalid canonical field set")
    if type(budget.get("window")) is not int or budget["window"] < 1:
        raise ValueError(f"{location}.window must be a positive integer")
    for key in ("development_attempts", "reviewer_invocations"):
        if type(budget.get(key)) is not int or budget[key] < 0:
            raise ValueError(f"{location}.{key} must be a non-negative integer")
    if type(budget.get("final_ci_fix_used")) is not bool:
        raise ValueError(f"{location}.final_ci_fix_used must be boolean")
    artifacts = budget.get("review_artifacts")
    if not isinstance(artifacts, list) or not all(
        isinstance(item, dict) for item in artifacts
    ):
        raise ValueError(f"{location}.review_artifacts must contain objects")
    if type(budget.get("checkpoint_reason")) not in {str, type(None)}:
        raise ValueError(f"{location}.checkpoint_reason must be string or null")
    if budget["development_attempts"] > policy.development_limit:
        raise ValueError(
            f"{location}.development_attempts exceeds the policy limit"
        )
    if budget["reviewer_invocations"] > policy.review_limit:
        raise ValueError(f"{location}.reviewer_invocations exceeds the policy limit")
    if len(artifacts) > policy.review_limit:
        raise ValueError(f"{location}.review_artifacts exceeds the policy limit")
    if budget["final_ci_fix_used"] and policy.final_ci_fix_limit < 1:
        raise ValueError(f"{location}.final_ci_fix_used is not allowed by policy")
    if (
        budget["final_ci_fix_used"]
        and budget["development_attempts"] != policy.development_limit
    ):
        raise ValueError(
            f"{location}.final_ci_fix_used requires the ordinary development limit"
        )


def _validate_budget_history(
    history: list[dict[str, Any]],
    current_window: int,
    policy: ReviewBudgetPolicy,
) -> None:
    expected_history_length = current_window - 1
    if len(history) != expected_history_length:
        raise ValueError(
            "review_budget_history must contain exactly one snapshot per prior window"
        )
    for index, snapshot in enumerate(history, start=1):
        location = f"review_budget_history[{index - 1}]"
        if not snapshot:
            raise ValueError(f"{location} must preserve a complete old Job snapshot")
        if "review_budget_history" in snapshot:
            raise ValueError(f"{location} must not contain a nested history container")
        old_budget = snapshot.get("review_budget")
        if not isinstance(old_budget, dict):
            raise ValueError(f"{location}.review_budget is missing")
        try:
            _validate_budget_projection(old_budget, f"{location}.review_budget", policy)
        except (KeyError, TypeError) as error:
            raise ValueError(f"{location}.review_budget is invalid") from error
        if old_budget["window"] != index:
            raise ValueError(
                f"{location}.review_budget.window must be the consecutive window {index}"
            )
        if not isinstance(snapshot.get("phase"), str) or not snapshot["phase"].strip():
            raise ValueError(f"{location}.phase is missing from the audit snapshot")
        for key in ("modification_attempts", "validation_attempts"):
            value = snapshot.get(key)
            if type(value) is not int or value < 0:
                raise ValueError(f"{location}.{key} is missing from the audit snapshot")


def reset_budget(job: dict[str, Any], policy: ReviewBudgetPolicy) -> ReviewBudget:
    """Open a new numbered window while retaining historical state elsewhere."""

    current = ensure_budget(job, policy)
    history = job["review_budget_history"]
    if not isinstance(history, list):
        raise ValueError("review_budget_history must be a list")
    # Keep the complete pre-reset Job projection except the history container
    # itself.  Excluding that one recursive field keeps storage linear in the
    # number of windows while retaining every finding, repair input, receipt,
    # thread, and checkpoint belonging to the old window.
    snapshot = deepcopy(
        {key: value for key, value in job.items() if key != "review_budget_history"}
    )
    history.append(snapshot)
    next_window = int(current["window"]) + 1
    job["review_budget"] = new_budget(window=next_window)
    clear_required_checks_observation(job)
    job["modification_attempts"] = 0
    job["validation_attempts"] = 0
    job.pop("last_review_candidate_sha", None)
    job.pop("publication_authority", None)
    job.pop("fallback_publication_receipt", None)
    job.pop("deterministic_integration_record", None)
    job.pop("final_ci_fix_failure_head", None)
    return cast(ReviewBudget, job["review_budget"])


def previous_review_context(job: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest saved Artifact and its own review identity.

    The current Job may already point at a newly-created Candidate while the
    previous Artifact still describes the prior Candidate.  The persisted
    review entry is therefore the only safe source for Reviewer 2+ context.
    """

    budget = job.get("review_budget")
    if not isinstance(budget, dict):
        return None
    artifacts = budget.get("review_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None
    latest = artifacts[-1]
    if not isinstance(latest, dict):
        return None
    artifact = latest.get("artifact")
    identity = latest.get("review_identity")
    if not isinstance(artifact, dict) or not isinstance(identity, dict):
        return None
    return {
        "artifact": deepcopy(artifact),
        "identity": deepcopy(identity),
    }


def budget_checkpoint_subjects(
    state: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Resolve the one resumable budget checkpoint using one stable ordering."""

    subjects: list[tuple[str, dict[str, Any]]] = []
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and _is_budget_checkpoint(active):
        subjects.append(("ticket", active))
    elif isinstance(state.get("ticket_jobs"), dict):
        for value in state["ticket_jobs"].values():
            if isinstance(value, dict) and _is_budget_checkpoint(value):
                subjects.append(("ticket", value))
    parent = state.get("parent_job")
    if isinstance(parent, dict) and _is_budget_checkpoint(parent):
        subjects.append(("parent", parent))
    run = state.get("run_acceptance")
    if isinstance(run, dict):
        repair = run.get("repair_job")
        if isinstance(repair, dict) and _is_budget_checkpoint(repair):
            subjects.append(("run_repair", repair))
        elif _is_budget_checkpoint(run):
            subjects.append(("run", run))
    return subjects


def _is_budget_checkpoint(value: dict[str, Any]) -> bool:
    return value.get("pending_semantic_attempt") is None and value.get(
        "blocked_reason"
    ) in {
        "modification_budget_exhausted",
        "review_budget_exhausted",
    }


def can_start_development(
    job: dict[str, Any], policy: ReviewBudgetPolicy, *, attempt_kind: str = "ordinary"
) -> bool:
    budget = ensure_budget(job, policy)
    if attempt_kind == "final_ci_fix":
        return (
            budget["development_attempts"] >= policy.development_limit
            and not budget["final_ci_fix_used"]
            and policy.final_ci_fix_limit > 0
        )
    return budget["development_attempts"] < policy.development_limit


def can_start_review(job: dict[str, Any], policy: ReviewBudgetPolicy) -> bool:
    return ensure_budget(job, policy)["reviewer_invocations"] < policy.review_limit


def mark_development(
    job: dict[str, Any], policy: ReviewBudgetPolicy, *, attempt_kind: str
) -> int:
    budget = ensure_budget(job, policy)
    if not can_start_development(job, policy, attempt_kind=attempt_kind):
        raise ValueError("development budget is exhausted")
    if attempt_kind == "final_ci_fix":
        budget["final_ci_fix_used"] = True
        # Final CI-fix is a separate, one-shot exception. Keep the ordinary
        # Development counter at its ceiling so the public projection cannot
        # report a misleading "5/4".
        return budget["development_attempts"]
    budget["development_attempts"] += 1
    return budget["development_attempts"]


def mark_review(job: dict[str, Any], policy: ReviewBudgetPolicy) -> int:
    budget = ensure_budget(job, policy)
    if not can_start_review(job, policy):
        raise ValueError("review budget is exhausted")
    budget["reviewer_invocations"] += 1
    return budget["reviewer_invocations"]
