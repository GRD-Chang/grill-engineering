from __future__ import annotations

from typing import Any

from agent_run.state_errors import IncompatibleRunStateError
from agent_run.required_checks_observation import (
    require_required_checks_observation,
    validate_legacy_required_checks_projection,
)
from agent_run.ticket_acceptance_contract import (
    require_completed_accepted_ticket_authorization,
)
from agent_run.ticket_fallback_contract import (
    _require_fallback_integration_authorization,
)


def require_completed_ticket_integration_records(state: dict[str, Any]) -> None:
    """Require canonical Integration Records for every completed Ticket."""

    jobs = state.get("ticket_jobs")
    if not isinstance(jobs, dict):
        raise IncompatibleRunStateError(
            "incompatible_run_state: ticket_jobs must be an object"
        )
    for key, job in jobs.items():
        if not isinstance(job, dict) or job.get("phase") != "completed":
            continue
        location = f"ticket_jobs[{key}].deterministic_integration_record"
        record = job.get("deterministic_integration_record")
        if not isinstance(record, dict):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: completed Ticket {key} is missing {location}"
            )
        integrated_sha = job.get("integrated_sha")
        if not isinstance(integrated_sha, str) or not integrated_sha.strip():
            raise IncompatibleRunStateError(
                f"incompatible_run_state: completed Ticket {key}.integrated_sha is invalid"
            )
        _require_deterministic_integration_record(
            record, location, integrated_sha=integrated_sha, completed_job=job
        )


def _require_deterministic_integration_record(
    record: dict[str, Any],
    location: str,
    *,
    integrated_sha: str,
    completed_job: dict[str, Any],
) -> None:
    source = record.get("source")
    if source not in {"accepted", "fallback"}:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.source must be accepted or fallback"
        )
    for key in (
        "base_sha",
        "candidate_sha",
        "candidate_tree",
        "publication_sha",
        "integrated_sha",
        "integrated_tree",
        "integrated_message",
    ):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.{key} is invalid"
            )
    if record["integrated_sha"] != integrated_sha:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.integrated_sha is not associated with the completed Ticket"
        )
    if record.get("integrated_publication_sha") != record["publication_sha"]:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.integrated_publication_sha is invalid"
        )
    integrated_parents = record.get("integrated_parents")
    if integrated_parents != [record["base_sha"]]:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.integrated_parents is invalid"
        )
    effective_revision = record.get("effective_revision")
    if not isinstance(effective_revision, str) or not effective_revision.strip():
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.effective_revision is invalid"
        )
    pr_number = record.get("pr_number")
    if type(pr_number) is not int or pr_number < 1:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.pr_number is invalid"
        )
    window = record.get("window")
    if type(window) is not int or window < 1:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.window is invalid"
        )
    if type(record.get("final_ci_fix_used")) is not bool:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.final_ci_fix_used is invalid"
        )
    evidence = record.get("required_checks_evidence")
    publication_sha = record["publication_sha"]
    require_required_checks_observation(
        evidence,
        location=f"{location}.required_checks_evidence",
        expected_pr_number=pr_number,
        expected_head_sha=publication_sha,
        allowed_results=frozenset({"none", "pass"}),
    )
    validate_legacy_required_checks_projection(
        record,
        location=location,
        observation=evidence,
    )
    pr = record.get("pr")
    if not isinstance(pr, dict):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.pr is invalid"
        )
    if (
        pr.get("number") != pr_number
        or pr.get("head_sha") != publication_sha
        or pr.get("base_sha") != record["base_sha"]
        or pr.get("state") != "MERGED"
        or pr.get("merge_commit_sha") != integrated_sha
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.pr/head is invalid"
        )
    evidence_key = (
        "acceptance_record" if source == "accepted" else "fallback_receipt"
    )
    authorization = record.get(evidence_key)
    if not isinstance(authorization, dict) or not authorization:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.{evidence_key} is invalid"
        )
    if source == "accepted":
        require_completed_accepted_ticket_authorization(
            completed_job, authorization, record, location
        )
    else:
        _require_fallback_integration_authorization(
            authorization, record, location
        )
