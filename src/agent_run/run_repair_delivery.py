from __future__ import annotations

"""Create the exact Parent repair Candidate without a PR delivery lifecycle."""

from pathlib import Path
from typing import Any

from agent_run.git import GitRepository
from agent_run.run_repair_cycle import uses_merge_resolution


def commit_repair_candidate(
    git: GitRepository, checkout: Path, job: dict[str, Any], attempt: int,
) -> str | None:
    if uses_merge_resolution(job):
        squash_candidate = job.get("integration_squash_candidate_sha")
        raw_conflict_paths = job.get("integration_conflict_paths", [])
        if not isinstance(raw_conflict_paths, list) or not all(
            isinstance(path, str) for path in raw_conflict_paths
        ):
            raise ValueError("integration_conflict_paths must contain strings")
        conflict_paths = tuple(raw_conflict_paths)
        commit_options: dict[str, Any] = {
            "expected_conflict_paths": conflict_paths,
            "candidate_intent": (
                job.get("candidate_commit_intent")
                if isinstance(job.get("candidate_commit_intent"), dict)
                else None
            ),
        }
        if isinstance(squash_candidate, str):
            commit_options["squash_candidate_sha"] = squash_candidate
        candidate = git.commit_merge_resolution_candidate(
            checkout,
            run_head_sha=str(job["base_sha"]),
            default_head_sha=str(job["default_base_sha"]),
            attempt=attempt,
            published_sha=job.get("published_sha"),
            **commit_options,
        )
    else:
        candidate = git.commit_run_repair_candidate(
            checkout,
            attempt=attempt,
            expected_head=str(
                job.get("managed_checkout_head")
                or job.get("candidate_sha")
                or job["base_sha"]
            ),
            candidate_intent=(
                job.get("candidate_commit_intent")
                if isinstance(job.get("candidate_commit_intent"), dict)
                else None
            ),
        )
    if candidate is None:
        return None
    job.pop("integration_squash_candidate_sha", None)
    job.pop("integration_finding_snapshot_sha", None)
    return candidate
