from __future__ import annotations

"""Exact-head Required Checks observations shared by Change Delivery paths."""

from copy import deepcopy
from typing import Any

from agent_run.delivery_protocol import GitHubPublisher
from agent_run.github import GitHubReadError
from agent_run.state_errors import IncompatibleRunStateError


REQUIRED_CHECK_RESULTS = frozenset({"none", "pass", "pending", "unknown", "fail"})
_KNOWN_CHECK_BUCKETS = frozenset(
    {"fail", "cancel", "pending", "pass", "skipping", "neutral"}
)


def derive_required_checks_result(checks: object) -> str:
    """Derive the aggregate result from the supporting check buckets."""

    if not isinstance(checks, list) or not all(
        isinstance(check, dict) for check in checks
    ):
        raise ValueError("Required Checks snapshot checks must contain objects")
    if not checks:
        return "none"
    buckets: set[str] = set()
    for check in checks:
        bucket = check.get("bucket")
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("Required Checks snapshot check bucket is invalid")
        buckets.add(bucket.lower())
    if buckets - _KNOWN_CHECK_BUCKETS:
        return "unknown"
    if buckets & {"fail", "cancel"}:
        return "fail"
    if "pending" in buckets:
        return "pending"
    return "pass"


def _result_matches_check_buckets(result: object, checks: list[dict[str, Any]]) -> bool:
    return result == derive_required_checks_result(checks)


def require_required_checks_observation(
    value: object,
    *,
    location: str,
    expected_pr_number: int | None = None,
    expected_head_sha: str | None = None,
    allowed_results: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Validate one durable, exact-head Required Checks Observation."""

    if not isinstance(value, dict):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location} must be an object"
        )
    pr_number = value.get("pr_number")
    if type(pr_number) is not int or pr_number < 1:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.pr_number is invalid"
        )
    if expected_pr_number is not None and pr_number != expected_pr_number:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.pr_number is not bound to the expected PR"
        )
    head_sha = value.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha.strip():
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.head_sha is invalid"
        )
    if expected_head_sha is not None and head_sha != expected_head_sha:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.head_sha is not bound to the expected head"
        )
    result = value.get("result")
    accepted_results = (
        REQUIRED_CHECK_RESULTS if allowed_results is None else allowed_results
    )
    if result not in accepted_results:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.result is invalid"
        )
    checks = value.get("checks")
    if not isinstance(checks, list) or not all(
        isinstance(check, dict) for check in checks
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.checks is invalid"
        )
    try:
        matches = _result_matches_check_buckets(result, checks)
    except ValueError as error:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.checks is invalid: {error}"
        ) from None
    if not matches:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.result conflicts with check buckets"
        )
    return value


def validate_legacy_required_checks_projection(
    owner: dict[str, Any],
    *,
    location: str,
    observation: object,
) -> None:
    """Accept a legacy projection only when its canonical Observation agrees."""

    has_result = "required_checks" in owner
    has_mode = "required_checks_mode" in owner
    if not has_result and not has_mode:
        return
    if not has_result or not has_mode:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location} has an incomplete Required Checks projection"
        )
    canonical = require_required_checks_observation(
        observation,
        location=f"{location}.required_checks_evidence",
    )
    expected_result = canonical["result"]
    expected_mode = "not_configured" if expected_result == "none" else "configured"
    if owner.get("required_checks") != expected_result:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.required_checks conflicts with Observation"
        )
    if owner.get("required_checks_mode") != expected_mode:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.required_checks_mode conflicts with Observation"
        )


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
    try:
        matches = _result_matches_check_buckets(result, checks)
    except ValueError:
        raise
    if not matches:
        raise ValueError("Required Checks snapshot result conflicts with check buckets")
    return deepcopy(snapshot)


def failure_evidence_matches_observation(
    observation: dict[str, Any],
    evidence: object,
    *,
    pr_number: int,
    head_sha: str,
) -> bool:
    """Require matching terminal failures from one exact-head Observation."""

    if not isinstance(evidence, dict):
        return False
    for key, expected in {
        "pr_number": pr_number,
        "head_sha": head_sha,
        "result": "fail",
    }.items():
        if key in evidence and evidence[key] != expected:
            return False
    observed_checks = observation.get("checks")
    evidence_checks = evidence.get("checks")
    if not isinstance(observed_checks, list) or not isinstance(evidence_checks, list):
        return False
    terminal_buckets = {"pass", "fail", "cancel", "skipping", "neutral"}
    if any(
        not isinstance(check, dict)
        or str(check.get("bucket", "")).lower() not in terminal_buckets
        for check in observed_checks
    ):
        return False

    def failed_identities(
        checks: list[object],
    ) -> list[tuple[str, str, str]] | None:
        identities: list[tuple[str, str, str]] = []
        for check in checks:
            if not isinstance(check, dict):
                return None
            if str(check.get("bucket", "")).lower() not in {"fail", "cancel"}:
                continue
            values = tuple(check.get(key) for key in ("workflow", "name", "link"))
            if not all(isinstance(value, str) and value.strip() for value in values):
                return None
            identities.append((str(values[0]), str(values[1]), str(values[2])))
        return sorted(identities)

    observed_failures = failed_identities(observed_checks)
    evidence_failures = failed_identities(evidence_checks)
    return bool(observed_failures) and observed_failures == evidence_failures


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

    clear_required_checks_observation(job)
    receipt = job.get("fallback_publication_receipt")
    if isinstance(receipt, dict):
        receipt["publication_sha"] = publication_sha
        if isinstance(job.get("pr_number"), int):
            receipt["pr_number"] = job["pr_number"]


def clear_required_checks_observation(job: dict[str, Any]) -> None:
    """Remove current Observation data without touching failure provenance."""

    for key in (
        "required_checks_evidence",
        "required_checks_observation_status",
        "required_checks",
        "required_checks_mode",
    ):
        job.pop(key, None)
    receipt = job.get("fallback_publication_receipt")
    if isinstance(receipt, dict):
        receipt.pop("required_checks_evidence", None)
