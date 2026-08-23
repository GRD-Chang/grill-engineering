from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from agent_run.git import GitRepository
from agent_run.review_budget import ReviewBudgetPolicy, ensure_budget


class FallbackStage(Protocol):
    git: GitRepository

    def review_budget_policy(self) -> ReviewBudgetPolicy: ...

    def save(self, state: dict[str, Any]) -> dict[str, Any]: ...


def fallback_publication_context(receipt: dict[str, Any]) -> dict[str, Any]:
    """Project only narrative-safe fallback facts for the Publication Agent."""

    review_artifacts = receipt.get("review_artifacts")
    latest = (
        review_artifacts[-1]
        if isinstance(review_artifacts, list) and review_artifacts
        else {}
    )
    identity = latest.get("review_identity") if isinstance(latest, dict) else None
    if isinstance(identity, dict):
        identity = {
            key: deepcopy(value)
            for key, value in identity.items()
            if value is not None
        }
    else:
        identity = {
            key: deepcopy(latest[key])
            for key in ("reviewed_base_sha", "candidate_sha")
            if isinstance(latest, dict) and key in latest and latest[key] is not None
        }
    current_identity = {
        key: deepcopy(receipt[key])
        for key in ("base_sha", "candidate_sha", "candidate_tree")
        if key in receipt and receipt[key] is not None
    }
    previous_candidate = identity.get("reviewed_candidate_sha") or identity.get(
        "candidate_sha"
    )
    current_candidate = receipt.get("candidate_sha")
    context: dict[str, Any] = {}
    if identity:
        context["last_review_identity"] = identity
    if current_identity:
        context["current_candidate_identity"] = current_identity
    if isinstance(current_candidate, str) and isinstance(previous_candidate, str):
        context["development_delta"] = current_candidate != previous_candidate
    context["current_candidate_has_additional_review"] = False
    for key in (
        "candidate_delta",
        "repair_delta",
        "repair_source",
        "failure_evidence_source",
        "git_integrity",
    ):
        receipt_key = "code_delta" if key == "candidate_delta" else key
        value = receipt.get(receipt_key)
        if isinstance(value, (dict, list, str, int, float, bool)):
            context[key] = deepcopy(value)
    last_artifact = receipt.get("last_acceptance_artifact")
    if isinstance(last_artifact, dict):
        checks = last_artifact.get("checks")
        if isinstance(checks, dict):
            context["last_review_lane_statuses"] = {
                lane: check.get("status")
                for lane, check in checks.items()
                if isinstance(lane, str) and isinstance(check, dict)
            }
    return context


def previous_publication_authorization(
    job: dict[str, Any],
) -> dict[str, Any] | None:
    """Snapshot the exact publication authority behind a CI repair.

    A Required-Checks repair is only meaningful relative to the PR head
    whose checks failed.  Keep the nested Acceptance Record or fallback
    Receipt alongside that head so a later receipt cannot merely claim an
    authority kind while silently swapping the evidence.
    """

    authority = job.get("publication_authority")
    acceptance = job.get("acceptance_record")
    receipt = job.get("fallback_publication_receipt")
    if authority is None and isinstance(acceptance, dict):
        authority = "acceptance"
    if authority not in {"acceptance", "fallback"}:
        return None
    publication_sha = job.get("publication_sha")
    pr_number = job.get("pr_number")
    base_sha = job.get("base_sha")
    effective_revision = job.get("effective_revision")
    if (
        not isinstance(publication_sha, str)
        or not publication_sha.strip()
        or type(pr_number) is not int
        or pr_number < 1
        or not isinstance(base_sha, str)
        or not base_sha.strip()
        or not isinstance(effective_revision, str)
        or not effective_revision.strip()
    ):
        return None
    if authority == "acceptance":
        if not isinstance(acceptance, dict):
            return None
        candidate_sha = acceptance.get("reviewed_candidate_sha")
        candidate_tree = acceptance.get("reviewed_candidate_tree")
        nested_acceptance: dict[str, Any] | None = deepcopy(acceptance)
        nested_receipt = None
    else:
        if not isinstance(receipt, dict):
            return None
        candidate_sha = receipt.get("candidate_sha")
        candidate_tree = receipt.get("candidate_tree")
        nested_acceptance = None
        nested_receipt = deepcopy(receipt)
        nested_receipt.pop("previous_publication_authorization", None)
    if (
        not isinstance(candidate_sha, str)
        or not candidate_sha.strip()
        or not isinstance(candidate_tree, str)
        or not candidate_tree.strip()
    ):
        return None
    return {
        "authority": authority,
        "pr_number": pr_number,
        "publication_sha": publication_sha,
        "base_sha": base_sha,
        "effective_revision": effective_revision,
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "acceptance_record": nested_acceptance,
        "fallback_receipt": nested_receipt,
    }


def prepare_ticket_fallback(
    stage: FallbackStage, state: dict[str, Any], job: dict[str, Any]
) -> bool:
    policy = stage.review_budget_policy()
    if not policy.fallback or not fallback_candidate_is_eligible(job):
        return False
    candidate = job.get("candidate_sha")
    if not isinstance(candidate, str):
        return False
    artifact = job.get("acceptance_artifact")
    previous_receipt = job.get("fallback_publication_receipt")
    if not isinstance(artifact, dict) and isinstance(previous_receipt, dict):
        prior_artifact = previous_receipt.get("last_acceptance_artifact")
        if isinstance(prior_artifact, dict):
            artifact = deepcopy(prior_artifact)
    if not isinstance(artifact, dict):
        return False
    budget = ensure_budget(job, policy)
    git_integrity = stage.git.verify_candidate_integrity(
        str(job["base_sha"]), candidate
    )
    changed_files = stage.git.diff_name_status(str(job["base_sha"]), candidate)
    repair_source = str(job.get("repair_source", "acceptance"))
    if repair_source == "required_checks":
        repair_delta_base = job.get("final_ci_fix_failure_head") or job.get(
            "publication_sha"
        )
    elif repair_source == "git_integrity":
        evidence = job.get("git_integrity_evidence")
        repair_delta_base = (
            evidence.get("expected_head")
            if isinstance(evidence, dict)
            else None
        ) or job.get("last_review_candidate_sha")
    else:
        repair_delta_base = job.get("last_review_candidate_sha")
    if not isinstance(repair_delta_base, str) or not repair_delta_base:
        repair_delta_base = str(job["base_sha"])
    repair_delta = stage.git.diff_name_status(repair_delta_base, candidate)
    ci_evidence = (
        job.get("ci_evidence") if repair_source == "required_checks" else None
    )
    git_integrity_evidence = (
        job.get("git_integrity_evidence")
        if repair_source == "git_integrity"
        else None
    )
    failure_evidence = (
        deepcopy(ci_evidence)
        if isinstance(ci_evidence, dict)
        else deepcopy(git_integrity_evidence)
        if isinstance(git_integrity_evidence, dict)
        else deepcopy(artifact)
    )
    failure_evidence_source = (
        "required_checks"
        if isinstance(ci_evidence, dict)
        else "git_integrity"
        if isinstance(git_integrity_evidence, dict)
        else "acceptance"
    )
    previous_authority = job.get("publication_authority")
    if previous_authority is None and isinstance(
        job.get("acceptance_record"), dict
    ):
        previous_authority = "acceptance"
    previous_authorization_snapshot = (
        previous_publication_authorization(job)
        if repair_source == "required_checks"
        else None
    )
    if repair_source == "required_checks" and previous_authorization_snapshot is None:
        return False
    required_check_failure_head = (
        job.get("final_ci_fix_failure_head") or job.get("publication_sha")
        if isinstance(ci_evidence, dict)
        else None
    )
    receipt = {
        "kind": "ticket_fallback_publication_receipt",
        "window": budget["window"],
        "base_sha": str(job["base_sha"]),
        "candidate_sha": candidate,
        "candidate_tree": stage.git.resolve(f"{candidate}^{{tree}}"),
        "effective_revision": str(job["effective_revision"]),
        "reviewer_invocations": budget["reviewer_invocations"],
        "review_artifacts": [dict(item) for item in budget["review_artifacts"]],
        "last_acceptance_artifact": artifact,
        "last_review_candidate_sha": job.get("last_review_candidate_sha"),
        "validation_attempts": job.get("validation_attempts"),
        "development_attempts": budget["development_attempts"],
        "review_budget": deepcopy(budget),
        "final_ci_fix_used": budget["final_ci_fix_used"],
        "final_ci_fix_failure_head": job.get("final_ci_fix_failure_head"),
        "required_check_failure_evidence": deepcopy(ci_evidence)
        if isinstance(ci_evidence, dict)
        else None,
        "last_attempt_kind": job.get("attempt_kind", "ordinary"),
        "repair_source": repair_source,
        "failure_evidence_source": failure_evidence_source,
        "failure_evidence": failure_evidence,
        "git_integrity_evidence": deepcopy(git_integrity_evidence)
        if isinstance(git_integrity_evidence, dict)
        else None,
        "development_summary": (
            job["development_summary"]
            if isinstance(job.get("development_summary"), str)
            and job["development_summary"].strip()
            else ""
        ),
        "code_delta_base_sha": str(job["base_sha"]),
        "code_delta": changed_files,
        "repair_delta_base_sha": repair_delta_base,
        "repair_delta": repair_delta,
        "required_check_failure_head": required_check_failure_head,
        "previous_publication_authority": previous_authority,
        "previous_publication_authorization": previous_authorization_snapshot,
        "git_integrity": git_integrity,
    }
    job["fallback_publication_receipt"] = receipt
    job["publication_authority"] = "fallback"
    job.pop("acceptance_record", None)
    job.pop("acceptance_artifact", None)
    job["phase"] = "accepted"
    budget["checkpoint_reason"] = None
    stage.save(state)
    return True


def fallback_candidate_is_eligible(job: dict[str, Any]) -> bool:
    budget = job.get("review_budget")
    candidate = job.get("candidate_sha")
    last_review_candidate = job.get("last_review_candidate_sha")
    receipt = job.get("fallback_publication_receipt")
    has_fallback_artifact = isinstance(job.get("acceptance_artifact"), dict) or (
        isinstance(receipt, dict)
        and isinstance(receipt.get("last_acceptance_artifact"), dict)
    )
    return (
        isinstance(budget, dict)
        and budget.get("reviewer_invocations") == 3
        and isinstance(candidate, str)
        and candidate != last_review_candidate
        and has_fallback_artifact
    )
