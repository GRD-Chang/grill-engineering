"""Durable, fact-bound maintainer approval for a final pull request."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_run.agent_invocation import canonical_fingerprint


def acceptance_fingerprint(record: dict[str, Any], artifact: dict[str, Any]) -> str:
    """Fingerprint the exact independent acceptance facts a maintainer approved."""
    return canonical_fingerprint(
        {"acceptance_record": record, "acceptance_artifact": artifact}
    )


def grant_authority(
    *,
    repository: str,
    pr_number: int,
    head_branch: str,
    head_sha: str,
    base_branch: str,
    base_sha: str,
    acceptance_fingerprint: str,
) -> dict[str, object]:
    """Return the immutable facts which an approval is permitted to authorize."""
    return {
        "repository": repository,
        "pr_number": pr_number,
        "head_branch": head_branch,
        "head_sha": head_sha,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "acceptance_fingerprint": acceptance_fingerprint,
    }


def grant_matches(grant: object, authority: dict[str, object]) -> bool:
    """Require every authorized fact to match before an automatic continuation."""
    return isinstance(grant, dict) and all(
        grant.get(key) == value for key, value in authority.items()
    )


def create_grant(authority: dict[str, object]) -> dict[str, object]:
    """Persist the one human approval without retaining mutable approval input."""
    return {**authority, "granted_at": datetime.now(UTC).isoformat()}
