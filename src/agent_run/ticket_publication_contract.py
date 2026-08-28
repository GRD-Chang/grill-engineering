from __future__ import annotations

from typing import Any

from agent_run.state_errors import IncompatibleRunStateError
from agent_run.ticket_acceptance_contract import (
    require_active_accepted_publication_authorization,
)
from agent_run.ticket_fallback_contract import (
    _require_fallback_integration_authorization,
)
from agent_run.required_checks_observation import (
    require_required_checks_observation,
    validate_legacy_required_checks_projection,
)


_ACTIVE_TICKET_PUBLICATION_PHASES = frozenset(
    {
        "accepted",
        "publication_pending",
        "publishing",
        "waiting_checks",
        "waiting_merge",
        "merging",
    }
)
_PUBLISHED_TICKET_AUTHORITY_PHASES = frozenset(
    {"waiting_checks", "waiting_merge", "merging"}
)


def require_active_ticket_publication_authorization(
    job: dict[str, Any],
    *,
    candidate_tree: str | None = None,
    location: str = "active_ticket_job",
) -> None:
    """Validate the authority that may cross an active Ticket PR gate.

    Waiting/merging Ticket Jobs are already past local publication.  Their
    persisted Acceptance Artifact or fallback Receipt therefore has to be a
    complete authorization before a resume may prepare a checkout or touch
    GitHub.  ``candidate_tree`` is supplied by the delivery gate when the
    local Git object is available; state loading still validates the durable
    boundary without needing Git.
    """

    phase = job.get("phase")
    if phase not in _ACTIVE_TICKET_PUBLICATION_PHASES:
        return
    for key in ("base_sha", "candidate_sha", "effective_revision"):
        value = job.get(key)
        if not isinstance(value, str) or not value.strip():
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.{key} is invalid before publication"
            )
    authority = job.get("publication_authority")
    if authority == "fallback":
        if job.get("acceptance_record") is not None:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location} mixes fallback and accepted authority"
            )
        receipt = job.get("fallback_publication_receipt")
        if not isinstance(receipt, dict) or not receipt:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.fallback_publication_receipt is missing"
            )
        persisted_tree = receipt.get("candidate_tree")
        if not isinstance(persisted_tree, str) or not persisted_tree.strip():
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.fallback_publication_receipt candidate tree is invalid"
            )
        if candidate_tree is not None and persisted_tree != candidate_tree:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.fallback_publication_receipt candidate tree is stale"
            )
        publication_sha = job.get("publication_sha")
        pr_number = job.get("pr_number")
        evidence = job.get("required_checks_evidence")
        if phase in _PUBLISHED_TICKET_AUTHORITY_PHASES:
            if not isinstance(publication_sha, str) or not publication_sha.strip():
                raise IncompatibleRunStateError(
                    f"incompatible_run_state: {location}.publication_sha is invalid before publication"
                )
            if type(pr_number) is not int or pr_number < 1:
                raise IncompatibleRunStateError(
                    f"incompatible_run_state: {location}.pr_number is invalid before publication"
                )
            require_required_checks_observation(
                evidence,
                location=f"{location}.required_checks_evidence",
                expected_pr_number=pr_number,
                expected_head_sha=publication_sha,
            )
        elif publication_sha is not None and (
            not isinstance(publication_sha, str) or not publication_sha.strip()
        ):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.publication_sha is invalid before publication"
            )
        if pr_number is not None and (type(pr_number) is not int or pr_number < 1):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.pr_number is invalid before publication"
            )
        budget = job.get("review_budget")
        if not isinstance(budget, dict):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.review_budget is missing before publication"
            )
        active_record = {
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "candidate_tree": persisted_tree,
            "effective_revision": job["effective_revision"],
            "window": budget.get("window"),
            "final_ci_fix_used": budget.get("final_ci_fix_used"),
        }
        if isinstance(publication_sha, str) and publication_sha.strip():
            active_record["publication_sha"] = publication_sha
        if type(pr_number) is int:
            active_record["pr_number"] = pr_number
        if isinstance(evidence, dict):
            active_record["required_checks_evidence"] = evidence
        _require_fallback_integration_authorization(
            receipt,
            active_record,
            location,
            require_publication_facts=phase in _PUBLISHED_TICKET_AUTHORITY_PHASES,
        )
        validate_legacy_required_checks_projection(
            job,
            location=location,
            observation=evidence,
        )
        return
    if authority not in {None, "accepted"}:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.publication_authority is invalid"
        )
    if job.get("fallback_publication_receipt") is not None:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location} mixes accepted and fallback authority"
        )
    acceptance = job.get("acceptance_record")
    if not isinstance(acceptance, dict) or not acceptance:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record is missing"
        )
    if job.get("acceptance_artifact") != acceptance.get("artifact"):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_artifact is not bound to acceptance_record"
        )
    persisted_tree = acceptance.get("reviewed_candidate_tree")
    if candidate_tree is not None and persisted_tree != candidate_tree:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record candidate tree is stale"
        )
    if not isinstance(persisted_tree, str) or not persisted_tree.strip():
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.acceptance_record candidate tree is invalid"
        )
    require_active_accepted_publication_authorization(
        acceptance,
        job,
        {
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "candidate_tree": persisted_tree,
            "effective_revision": job["effective_revision"],
        },
        location,
    )
    evidence = job.get("required_checks_evidence")
    validate_legacy_required_checks_projection(
        job,
        location=location,
        observation=evidence,
    )
    if phase in _PUBLISHED_TICKET_AUTHORITY_PHASES:
        publication_sha = job.get("publication_sha")
        pr_number = job.get("pr_number")
        if not isinstance(publication_sha, str) or not publication_sha.strip():
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.publication_sha is invalid before publication"
            )
        if type(pr_number) is not int or pr_number < 1:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.pr_number is invalid before publication"
            )
        require_required_checks_observation(
            evidence,
            location=f"{location}.required_checks_evidence",
            expected_pr_number=pr_number,
            expected_head_sha=publication_sha,
        )
