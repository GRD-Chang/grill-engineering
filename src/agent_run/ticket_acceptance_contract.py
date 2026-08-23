from __future__ import annotations

from typing import Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.review_budget import TICKET_POLICY, ensure_budget
from agent_run.state_errors import IncompatibleRunStateError


def _require_accepted_integration_authorization(
    authorization: dict[str, Any], record: dict[str, Any], location: str
) -> None:
    if authorization.get("acceptance_scope") != "change_job":
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record scope is invalid"
        )
    expected = {
        "reviewed_base_sha": record["base_sha"],
        "reviewed_candidate_sha": record["candidate_sha"],
        "reviewed_candidate_tree": record["candidate_tree"],
        "effective_revision": record["effective_revision"],
    }
    if any(authorization.get(key) != value for key, value in expected.items()):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record boundary is invalid"
        )
    try:
        artifact = AcceptanceArtifact.parse(authorization.get("artifact"))
    except (TypeError, ValueError):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record.artifact is invalid"
        ) from None
    if not artifact.is_accepted:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record.artifact must be pass"
        )
    reviewer_thread_id = authorization.get("reviewer_thread_id")
    if not isinstance(reviewer_thread_id, str) or not reviewer_thread_id.strip():
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record reviewer is invalid"
        )


def require_active_accepted_publication_authorization(
    authorization: dict[str, Any],
    job: dict[str, Any],
    record: dict[str, Any],
    location: str,
) -> None:
    """Validate an accepted Ticket publication against its budgeted review."""

    _require_accepted_ticket_review_authorization(
        authorization, job, record, location
    )


def require_completed_accepted_ticket_authorization(
    job: dict[str, Any],
    authorization: dict[str, Any],
    record: dict[str, Any],
    location: str,
) -> None:
    """Bind a completed accepted Ticket to its persisted budget projection."""

    record_budget = record.get("review_budget")
    if record_budget != job.get("review_budget"):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record review budget is not bound to the completed Ticket"
        )
    _require_accepted_ticket_review_authorization(
        authorization, job, record, location
    )


def _require_accepted_ticket_review_authorization(
    authorization: dict[str, Any],
    job: dict[str, Any],
    record: dict[str, Any],
    location: str,
) -> None:
    _require_accepted_integration_authorization(authorization, record, location)
    try:
        budget = ensure_budget(job, TICKET_POLICY)
    except (KeyError, TypeError, ValueError) as error:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.review_budget is invalid"
        ) from error
    if budget["reviewer_invocations"] < 1:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.accepted publication has no Reviewer invocation"
        )
    if budget["reviewer_invocations"] != len(budget["review_artifacts"]):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.review_budget invocation and Artifact counts differ"
        )
    artifacts = budget["review_artifacts"]
    expected_keys = {
        "reviewer_thread_id",
        "candidate_sha",
        "reviewed_base_sha",
        "review_identity",
        "artifact",
    }
    matching = False
    for index, item in enumerate(artifacts):
        item_location = f"{location}.review_budget.review_artifacts[{index}]"
        if not isinstance(item, dict) or set(item) != expected_keys:
            continue
        identity = item["review_identity"]
        if (
            item["reviewer_thread_id"] == authorization.get("reviewer_thread_id")
            and item["candidate_sha"] == authorization.get("reviewed_candidate_sha")
            and item["reviewed_base_sha"] == authorization.get("reviewed_base_sha")
            and item["artifact"] == authorization.get("artifact")
            and isinstance(identity, dict)
            and identity == {
                "reviewed_base_sha": authorization.get("reviewed_base_sha"),
                "reviewed_candidate_sha": authorization.get(
                    "reviewed_candidate_sha"
                ),
                "reviewed_candidate_tree": authorization.get(
                    "reviewed_candidate_tree"
                ),
            }
        ):
            try:
                matching_artifact = AcceptanceArtifact.parse(item["artifact"])
            except (TypeError, ValueError):
                raise IncompatibleRunStateError(
                    f"incompatible_run_state: {item_location}.artifact is invalid"
                ) from None
            if matching_artifact.is_accepted:
                matching = True
                break
    if not matching:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.accepted publication is not bound to a matching Review Artifact"
        )
