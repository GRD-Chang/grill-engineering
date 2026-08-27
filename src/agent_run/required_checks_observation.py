from __future__ import annotations

"""Exact-head Required Checks observations shared by Change Delivery paths."""

from copy import deepcopy
from typing import Any

from agent_run.delivery_protocol import GitHubPublisher
from agent_run.github import GitHubReadError


REQUIRED_CHECK_RESULTS = frozenset({"none", "pass", "pending", "unknown", "fail"})


def read_required_checks_observation(
    github: GitHubPublisher,
    pr_number: int,
    *,
    expected_head_sha: str,
) -> dict[str, Any]:
    """Read and validate one complete Required Checks snapshot.

    The snapshot is the aggregate result and its supporting check detail in one
    exact-head read.  Callers must not combine it with a separately observed
    aggregate value.
    """

    snapshot = github.required_checks_snapshot(
        pr_number, expected_head_sha=expected_head_sha
    )
    if not isinstance(snapshot, dict):
        raise ValueError("Required Checks snapshot must be an object")
    if snapshot.get("pr_number") != pr_number:
        raise GitHubReadError(
            "change_pr_identity_mismatch",
            "Required Checks snapshot does not match the expected PR",
        )
    if snapshot.get("head_sha") != expected_head_sha:
        raise GitHubReadError(
            "change_pr_head_drift",
            "Required Checks snapshot does not match the expected PR head",
        )
    result = snapshot.get("result")
    if result not in REQUIRED_CHECK_RESULTS:
        raise ValueError("Required Checks snapshot result is invalid")
    checks = snapshot.get("checks")
    if not isinstance(checks, list) or not all(
        isinstance(check, dict) for check in checks
    ):
        raise ValueError("Required Checks snapshot checks must contain objects")
    return deepcopy(snapshot)


def sync_fallback_receipt_observation(
    job: dict[str, Any], pr_number: int
) -> None:
    """Copy one Observation into the active fallback publication receipt."""

    if job.get("publication_authority") != "fallback":
        return
    receipt = job.get("fallback_publication_receipt")
    evidence = job.get("required_checks_evidence")
    publication_sha = job.get("publication_sha")
    if (
        not isinstance(receipt, dict)
        or not isinstance(evidence, dict)
        or not isinstance(publication_sha, str)
        or not publication_sha.strip()
        or evidence.get("pr_number") != pr_number
        or evidence.get("head_sha") != publication_sha
        or evidence.get("result") not in REQUIRED_CHECK_RESULTS
    ):
        return
    current_evidence = receipt.get("required_checks_evidence")
    if (
        receipt.get("pr_number") == pr_number
        and receipt.get("publication_sha") == publication_sha
        and current_evidence == evidence
        and current_evidence is not evidence
    ):
        return
    receipt_evidence = deepcopy(evidence)
    receipt.update(
        {
            "pr_number": pr_number,
            "publication_sha": publication_sha,
            "required_checks_evidence": receipt_evidence,
        }
    )


def bind_new_publication_head(job: dict[str, Any], publication_sha: str) -> None:
    """Clear the old Observation while retaining CI failure provenance."""

    for key in (
        "required_checks_evidence",
        "required_checks",
        "required_checks_mode",
    ):
        job.pop(key, None)
    receipt = job.get("fallback_publication_receipt")
    if isinstance(receipt, dict):
        receipt["publication_sha"] = publication_sha
        receipt.pop("required_checks_evidence", None)
        if isinstance(job.get("pr_number"), int):
            receipt["pr_number"] = job["pr_number"]
