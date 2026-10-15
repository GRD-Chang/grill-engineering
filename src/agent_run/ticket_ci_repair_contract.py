from __future__ import annotations

"""Exact-version authority for unreviewed Ticket CI repairs without a Reviewer."""

from typing import Any, Callable, NoReturn

from agent_run.required_checks_observation import (
    failure_evidence_matches_observation,
    require_required_checks_observation,
)
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.ticket_acceptance_contract import (
    require_active_accepted_publication_authorization,
)
from agent_run.ticket_fallback_contract import (
    _require_previous_publication_authorization,
)
from agent_run.review_budget import archived_budget_windows, scoped_budget_alias


def require_ci_repair_publication_authorization(
    receipt: object,
    job: dict[str, Any],
    *,
    location: str,
    candidate_tree: str | None = None,
    require_publication_facts: bool = False,
) -> None:
    """Reject a repair unless a previously authorized PR head failed CI.

    The old Acceptance or fallback is evidence about the *old* head. This
    receipt authorizes only the new candidate and never represents a Reviewer
    pass for it. The new head still needs its own Required Checks observation.
    """

    def invalid(detail: str) -> NoReturn:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.ci_repair_publication_receipt {detail}"
        )

    if not isinstance(receipt, dict) or receipt.get("kind") != "ticket_ci_repair_publication_receipt":
        invalid("is invalid")
    if receipt.get("policy_snapshot") != job.get("policy_snapshot"):
        invalid("policy snapshot is not bound")
    if job.get("acceptance_record") is not None or job.get("fallback_publication_receipt") is not None:
        invalid("mixes CI repair with Reviewer or fallback authority")
    for key in ("base_sha", "candidate_sha", "effective_revision"):
        if not isinstance(job.get(key), str) or not job[key].strip() or receipt.get(key) != job[key]:
            invalid(f"{key} is not bound to the active candidate")
    tree = receipt.get("candidate_tree")
    if not isinstance(tree, str) or not tree.strip() or (candidate_tree is not None and tree != candidate_tree):
        invalid("candidate tree is invalid or stale")
    recovery = job.get("ci_recovery")
    recovery_identity = receipt.get("ci_recovery_identity")
    if not isinstance(recovery, dict) or not isinstance(recovery_identity, dict):
        invalid("CI recovery state is missing")
    expected_recovery = {
        key: recovery.get(key)
        for key in ("window", "developments_used", "failure_head", "repair_pending", "review_required")
    }
    if (
        recovery_identity != expected_recovery
        or type(expected_recovery["window"]) is not int
        or expected_recovery["window"] < 1
        or type(expected_recovery["developments_used"]) is not int
        or expected_recovery["developments_used"] < 1
        or expected_recovery["repair_pending"] is not True
        or expected_recovery["review_required"] is not False
        or recovery.get("checkpoint_reason") is not None
    ):
        invalid("is outside an unreviewed CI repair")
    failure_head = receipt.get("failure_head")
    previous = receipt.get("previous_publication_authorization")
    if (
        not isinstance(failure_head, str)
        or not failure_head.strip()
        or recovery.get("failure_head") != failure_head
        or recovery.get("previous_publication_authorization") != previous
        or not isinstance(previous, dict)
    ):
        invalid("failed head or previous authority is not bound")
    authority = previous.get("authority")
    if authority not in {"acceptance", "fallback", "ci_repair"}:
        invalid("previous authority is invalid")
    if authority == "ci_repair":
        _require_previous_ci_authorization(previous, job, failure_head, invalid)
    else:
        _require_previous_publication_authorization(
            previous,
            previous_authority=authority,
            failure_head=failure_head,
            base_sha=job["base_sha"],
            effective_revision=job["effective_revision"],
            location=location,
        )
    previous_budget = receipt.get("previous_review_budget")
    if previous_budget != job.get("review_budget"):
        invalid("previous Reviewer budget differs from the active Ticket")
    history = job.get("review_budget_history")
    if not isinstance(history, list) and archived_budget_windows(job) is None:
        invalid("previous Reviewer budget history is missing")
    if authority == "acceptance":
        if not isinstance(previous_budget, dict):
            invalid("previous Reviewer budget is missing")
        prior_job = {
            "review_budget": previous_budget,
            "policy_snapshot": job.get("policy_snapshot"),
        }
        if isinstance(history, list):
            prior_job["review_budget_history"] = history
        with scoped_budget_alias(job, prior_job):
            require_active_accepted_publication_authorization(
                previous["acceptance_record"],
                prior_job,
                {
                    "base_sha": previous["base_sha"],
                    "candidate_sha": previous["candidate_sha"],
                    "candidate_tree": previous["candidate_tree"],
                    "effective_revision": previous["effective_revision"],
                },
                location,
            )
    elif authority == "fallback" and previous_budget != previous["fallback_receipt"].get("review_budget"):
        invalid("previous fallback budget is not bound")
    if previous.get("candidate_sha") == job["candidate_sha"] or previous.get("candidate_tree") == tree:
        invalid("has no new candidate code")
    observation = require_required_checks_observation(
        receipt.get("failure_observation"),
        location=f"{location}.ci_repair_publication_receipt.failure_observation",
        expected_pr_number=previous["pr_number"],
        expected_head_sha=failure_head,
        allowed_results=frozenset({"fail"}),
    )
    evidence = receipt.get("failure_evidence")
    if (
        not failure_evidence_matches_observation(
            observation, evidence,
            pr_number=previous["pr_number"], head_sha=failure_head,
        )
        or evidence != job.get("ci_evidence")
        or observation != job.get("required_checks_origin")
    ):
        invalid("failure evidence is invalid")
    integrity = receipt.get("git_integrity")
    if not isinstance(integrity, dict) or any(
        integrity.get(key) != value for key, value in {
            "status": "pass", "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "candidate_tree": tree, "base_is_ancestor": "true",
            "failed_head_is_ancestor": "true",
        }.items()
    ):
        invalid("Git integrity is invalid")
    delta = receipt.get("repair_delta")
    if (
        receipt.get("repair_delta_base_sha") != failure_head
        or not isinstance(delta, list)
        or not delta
        or not all(
            isinstance(item, dict)
            and isinstance(item.get("status"), str) and item["status"]
            and isinstance(item.get("path"), str) and item["path"]
            for item in delta
        )
        or not isinstance(receipt.get("development_summary"), str)
        or not receipt["development_summary"].strip()
    ):
        invalid("repair facts are invalid")
    publication_sha = job.get("publication_sha")
    pr_number = job.get("pr_number")
    for key, value in (("publication_sha", publication_sha), ("pr_number", pr_number)):
        if key in receipt and receipt[key] != value:
            invalid(f"{key} is not bound to the active publication")
    if require_publication_facts:
        if not isinstance(publication_sha, str) or not publication_sha.strip() or type(pr_number) is not int or pr_number < 1:
            invalid("published PR identity is missing")
        if receipt.get("publication_sha") != publication_sha or receipt.get("pr_number") != pr_number:
            invalid("published PR identity is invalid")
        observation = require_required_checks_observation(
            job.get("required_checks_evidence"),
            location=f"{location}.required_checks_evidence",
            expected_pr_number=pr_number,
            expected_head_sha=publication_sha,
        )
        if receipt.get("required_checks_evidence") != observation:
            invalid("new head Required Checks are not bound")
    elif receipt.get("required_checks_evidence") is not None:
        if receipt["required_checks_evidence"] != job.get("required_checks_evidence"):
            invalid("new head Required Checks are not bound")


def _require_previous_ci_authorization(
    previous: dict[str, Any], job: dict[str, Any], failure_head: str, invalid: Callable[[str], NoReturn],
) -> None:
    """Validate compact prior CI provenance without recursively growing receipts."""
    prior = previous.get("fallback_receipt")
    if (
        not isinstance(prior, dict)
        or prior.get("kind") != "ticket_ci_repair_publication_receipt"
        or previous.get("acceptance_record") is not None
        or previous.get("publication_sha") != failure_head
        or previous.get("base_sha") != job.get("base_sha")
        or previous.get("effective_revision") != job.get("effective_revision")
        or prior.get("policy_snapshot") != job.get("policy_snapshot")
        or any(prior.get(key) != previous.get(key) for key in (
            "base_sha", "candidate_sha", "candidate_tree", "effective_revision", "publication_sha", "pr_number"
        ))
    ):
        invalid("previous CI publication boundary is invalid")
    require_required_checks_observation(
        prior.get("required_checks_evidence"),
        location="previous_ci_publication.required_checks_evidence",
        expected_pr_number=previous.get("pr_number"), expected_head_sha=failure_head,
        allowed_results=frozenset({"fail"}),
    )
    integrity = prior.get("git_integrity")
    if not isinstance(integrity, dict) or any(integrity.get(key) != value for key, value in {
        "status": "pass", "base_sha": previous["base_sha"],
        "candidate_sha": previous["candidate_sha"], "candidate_tree": previous["candidate_tree"],
        "base_is_ancestor": "true", "failed_head_is_ancestor": "true",
    }.items()):
        invalid("previous CI publication integrity is invalid")
