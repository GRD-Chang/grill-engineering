from __future__ import annotations

"""Candidate Run Acceptance records and deterministic promotion checks."""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_run.agent_invocation import canonical_fingerprint
from agent_run.artifacts import AcceptanceArtifact
from agent_run.git import GitError, GitRepository
from agent_run.run_currentness import (
    MAX_CANDIDATE_ACCEPTANCE_HISTORY,
    ticket_completion_records,
)
from agent_run.run_repair_cycle import uses_merge_resolution
from agent_run.state_contract import require_candidate_acceptance_history


@dataclass
class CandidateRunAcceptance:
    """Own the one canonical Candidate record and its promotion proof."""

    git: GitRepository
    default_head_sha: str | None = None

    def update_default_head(self, default_head_sha: str) -> None:
        self.default_head_sha = default_head_sha

    def prepare_validation(
        self, job: dict[str, Any], validation: Path
    ) -> None:
        self.git.prepare_expected_merge_checkout(
            default_head_sha=str(job["default_base_sha"]),
            run_head_sha=str(job["candidate_sha"]),
            checkout=validation,
        )

    def record(
        self,
        job: dict[str, Any],
        reviewer_thread_id: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        default_base = str(job["default_base_sha"])
        candidate_sha = str(job["candidate_sha"])
        candidate_tree = self.git.resolve(f"{candidate_sha}^{{tree}}")
        self._require_merge_resolution_parents(job, candidate_sha)
        expected_merge_tree = self.git.expected_merge_tree(
            default_head_sha=default_base,
            run_head_sha=candidate_sha,
        )
        record = {
            "acceptance_scope": "run",
            "acceptance_state": "candidate",
            "reviewed_base_sha": job["base_sha"],
            "reviewed_default_base_sha": default_base,
            "reviewed_candidate_sha": candidate_sha,
            "reviewed_candidate_tree": candidate_tree,
            "repair_base_run_head_sha": job["base_sha"],
            "expected_merge_tree": expected_merge_tree,
            "parent_revision": job["parent_revision"],
            "ticket_graph_revision": job["ticket_graph_revision"],
            "ticket_completion_records": deepcopy(job["ticket_completion_records"]),
            "reviewer_thread_id": reviewer_thread_id,
            "repair_source": job["repair_source"],
            "artifact": deepcopy(artifact),
        }
        history = require_candidate_acceptance_history(
            job.get("candidate_acceptance_history", []),
            "run_acceptance.repair_job.candidate_acceptance",
        )
        artifact_outcome = AcceptanceArtifact.parse(artifact).outcome
        history_outcome = {
            "pass": "accepted",
            "findings": "finding",
            "blocked": "blocked",
        }[artifact_outcome]
        entry = {
            "candidate_sha": candidate_sha,
            "repair_base_run_head_sha": str(job["base_sha"]),
            "default_base_sha": default_base,
            "candidate_tree": candidate_tree,
            "expected_merge_tree": expected_merge_tree,
            "parent_revision": job["parent_revision"],
            "ticket_graph_revision": job["ticket_graph_revision"],
            "ticket_completion_records_fingerprint": canonical_fingerprint(
                job["ticket_completion_records"]
            ),
            "reviewer_thread_id": reviewer_thread_id,
            "development_thread_id": job.get("development_thread_id"),
            "pr_number": job.get("pr_number"),
            "integrated_sha": job.get("integrated_sha"),
            "repair_source": job["repair_source"],
            "outcome": history_outcome,
        }
        require_candidate_acceptance_history(
            [entry], "run_acceptance.repair_job.candidate_acceptance"
        )
        job["candidate_acceptance_history"] = [
            *history,
            entry,
        ][-MAX_CANDIDATE_ACCEPTANCE_HISTORY:]
        return record

    def is_current(
        self, state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool:
        default_base = job.get("default_base_sha")
        candidate_sha = job.get("candidate_sha")
        if not isinstance(default_base, str) or not isinstance(candidate_sha, str):
            return False
        try:
            candidate_tree = self.git.resolve(f"{candidate_sha}^{{tree}}")
            self._require_merge_resolution_parents(job, candidate_sha)
            expected_merge_tree = self.git.expected_merge_tree(
                default_head_sha=default_base,
                run_head_sha=candidate_sha,
            )
        except (GitError, ValueError):
            return False
        return (
            acceptance.get("acceptance_scope") == "run"
            and acceptance.get("acceptance_state") == "candidate"
            and acceptance.get("reviewed_base_sha") == job.get("base_sha")
            and acceptance.get("reviewed_default_base_sha") == default_base
            and acceptance.get("repair_base_run_head_sha") == job.get("base_sha")
            and acceptance.get("reviewed_candidate_sha") == candidate_sha
            and acceptance.get("reviewed_candidate_tree") == candidate_tree
            and acceptance.get("expected_merge_tree") == expected_merge_tree
            and self._default_head(state) == default_base
            and self.git.resolve(str(state["run_branch"]))
            in {job.get("base_sha"), job.get("integrated_sha")}
        )

    def promotion_record(
        self, state: dict[str, Any], job: dict[str, Any], integrated: str
    ) -> dict[str, Any] | None:
        record = job.get("acceptance_record")
        if not isinstance(record, dict):
            return None
        try:
            artifact = AcceptanceArtifact.parse(record.get("artifact"))
            default_base = str(job["default_base_sha"])
            candidate_sha = str(job["candidate_sha"])
            repair_base = str(job["base_sha"])
            candidate_tree = self.git.resolve(f"{candidate_sha}^{{tree}}")
            integrated_tree = self.git.resolve(f"{integrated}^{{tree}}")
            run_tree = self.git.resolve(f"{state['run_branch']}^{{tree}}")
            candidate_expected_merge_tree = self.git.expected_merge_tree(
                default_head_sha=default_base,
                run_head_sha=candidate_sha,
            )
            expected_merge_tree = self.git.expected_merge_tree(
                default_head_sha=default_base,
                run_head_sha=integrated,
            )
            self._require_merge_resolution_parents(job, candidate_sha)
        except (GitError, KeyError, TypeError, ValueError):
            return None
        parent_revision = _mapping(state, "parent").get("revision")
        graph_revision = _mapping(state, "ticket_graph").get("revision")
        completions = ticket_completion_records(state)
        if (
            not artifact.is_accepted
            or record.get("acceptance_scope") != "run"
            or record.get("acceptance_state") != "candidate"
            or record.get("reviewed_base_sha") != repair_base
            or record.get("repair_base_run_head_sha") != repair_base
            or record.get("reviewed_default_base_sha") != default_base
            or record.get("reviewed_candidate_sha") != candidate_sha
            or record.get("reviewed_candidate_tree") != candidate_tree
            or record.get("expected_merge_tree") != candidate_expected_merge_tree
            or expected_merge_tree != candidate_expected_merge_tree
            or self._default_head(state) != default_base
            or self.git.resolve(str(state["run_branch"])) != integrated
            or integrated_tree != candidate_tree
            or run_tree != candidate_tree
            or (
                uses_merge_resolution(job)
                and not self.git.is_ancestor(default_base, integrated)
            )
            or record.get("parent_revision") != parent_revision
            or record.get("ticket_graph_revision") != graph_revision
            or record.get("ticket_completion_records") != completions
        ):
            return None
        promoted = dict(record)
        promoted.update(
            {
                "acceptance_state": "integrated",
                "reviewed_base_sha": default_base,
                "reviewed_default_base_sha": default_base,
                "reviewed_head_sha": integrated,
                "expected_merge_tree": expected_merge_tree,
            }
        )
        return promoted

    def _require_merge_resolution_parents(
        self, job: dict[str, Any], candidate_sha: str
    ) -> None:
        if not uses_merge_resolution(job):
            return
        expected = [str(job["base_sha"]), str(job["default_base_sha"])]
        if self.git.commit_parents(candidate_sha) != expected:
            raise GitError("Merge-resolution Candidate parents do not match its boundary")

    def _default_head(self, state: dict[str, Any]) -> str:
        return self.default_head_sha or str(_mapping(state, "base")["sha"])


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
