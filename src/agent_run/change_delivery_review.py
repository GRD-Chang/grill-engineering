from __future__ import annotations

"""PR status projection for the Change delivery stages."""

from typing import Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.change_delivery_state import require_mapping as _mapping
from agent_run.delivery_protocol import GitHubPublisher

def record_change_status(
    github: GitHubPublisher, label: str, pr_number: int, job: dict[str, Any],
    checks: str, *, next_action: str | None = None,
) -> None:
    if job.get("publication_authority") in {"fallback", "ci_repair"}:
        validation_outcome = (
            "ci_repair_unreviewed"
            if job.get("publication_authority") == "ci_repair"
            else "fallback"
        )
        lane_statuses = {
            lane: "not_run" for lane in ("e2e", "standards", "spec")
        }
    else:
        artifact = _mapping(job, "acceptance_artifact")
        raw_checks = _mapping(artifact, "checks")
        validation_outcome = AcceptanceArtifact.parse(artifact).outcome
        lane_statuses = {
            lane: str(_mapping(raw_checks, lane)["status"])
            for lane in ("e2e", "standards", "spec")
        }
    if next_action is None:
        if checks == "pending":
            next_action = "wait for Required Checks"
        elif checks == "fail":
            next_action = "repair failed Required Checks"
        elif checks == "not_checked":
            next_action = "verify Published-Head Gate"
        else:
            next_action = "verify Published-Head Gate"
    github.record_agent_run_status(
        pr_number,
        {
            "scope": label,
            "base_sha": str(job["base_sha"]),
            "candidate_sha": str(job["candidate_sha"]),
            "validation_outcome": validation_outcome,
            "lane_statuses": lane_statuses,
            "required_checks": checks,
            "next_action": next_action,
        },
    )
