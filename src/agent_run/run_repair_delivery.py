from __future__ import annotations

"""Concrete Change Delivery adapters for the Run Repair consumer."""

from copy import deepcopy
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.change_delivery import (
    ChangeDeliveryAdapter,
    StaleDisposition,
)
from agent_run.git import GitRepository
from agent_run.run_candidate_acceptance import CandidateRunAcceptance
from agent_run.run_currentness import RunCurrentnessReader, refresh_run_currentness
from agent_run.run_repair_cycle import uses_merge_resolution
from agent_run.run_repair_currentness import RunRepairCurrentness
from agent_run.run_repair_integration import repair_run_branch_is_current
from agent_run.run_repair_requests import RunRepairRequests
from agent_run.run_thread_identity import prior_thread_identities

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine
    from agent_run.run_repair_promotion import RunRepairPromotion


class RunRepairAdapter(ChangeDeliveryAdapter):
    """Run Repair semantics without Git, GitHub, or StateStore writes."""

    stale_disposition = StaleDisposition.FRESH_RUN_ACCEPTANCE
    classify_required_check_failures = True

    def __init__(
        self,
        *,
        git: GitRepository,
        requests: RunRepairRequests,
        candidate_acceptance: CandidateRunAcceptance,
        currentness: RunRepairCurrentness,
        currentness_reader: RunCurrentnessReader | None,
        default_head_sha: str | None,
    ) -> None:
        self.git = git
        self.requests = requests
        self.candidate_acceptance = candidate_acceptance
        self.currentness = currentness
        self.currentness_reader = currentness_reader
        self.default_head_sha = default_head_sha

    def development_thread_is_allowed(
        self, state: dict[str, Any], thread_id: str
    ) -> bool:
        return thread_id not in prior_thread_identities(
            state, _mapping(state, "run_acceptance")
        )

    def base_is_current(self, current_base: str, job: dict[str, Any]) -> bool:
        return repair_run_branch_is_current(self.git, job, current_base)

    def development_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return self.requests.development(state, job, checkout)

    def publication_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        raise ValueError("Parent repair does not have an internal publication stage")

    def review_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return self.requests.review(state, job, checkout)

    def acceptance_record(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        reviewer_thread_id: str,
        artifact: dict[str, Any],
        *, review_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self.candidate_acceptance.record(
            job, reviewer_thread_id, artifact, review_context=review_context,
        )
        parsed = AcceptanceArtifact.parse(artifact)
        if parsed.is_accepted:
            job.pop("unresolved_acceptance_artifact", None)
        elif parsed.has_failures:
            job["unresolved_acceptance_artifact"] = deepcopy(artifact)
        return record

    def acceptance_is_current(
        self, state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool:
        return (
            self.candidate_acceptance.is_current(state, job, acceptance)
            and not self.revision_changed(state, job)
        )

    def revision_changed(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        if not self._refresh_run_currentness(state):
            return True
        default_head = self._default_head(state)
        return default_head != job.get(
            "default_base_sha"
        ) or self.currentness.non_default_revision_changed(
            state, job, default_head_sha=default_head
        )

    def requires_explicit_approval(
        self, _state: dict[str, Any], _job: dict[str, Any]
    ) -> bool:
        return False

    def resume_after_required_checks_failure(
        self, state: dict[str, Any], _job: dict[str, Any]
    ) -> bool:
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": None,
                "diagnostics": [],
            }
        )
        return False

    def linked_issue_number(
        self, state: dict[str, Any], _job: dict[str, Any]
    ) -> int:
        return int(_mapping(state, "parent")["number"])

    def invocation_identity(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> tuple[str, int]:
        return f"run-repair:{state['run_id']}", int(job["repair_generation"])

    def _refresh_run_currentness(self, state: dict[str, Any]) -> bool:
        if self.currentness_reader is None:
            return True
        default_head = refresh_run_currentness(
            state, reader=self.currentness_reader, git=self.git
        )
        if default_head is None:
            return False
        self.default_head_sha = default_head
        self.candidate_acceptance.update_default_head(default_head)
        return True

    def _default_head(self, state: dict[str, Any]) -> str:
        return self.default_head_sha or str(_mapping(state, "base")["sha"])


class RunRepairPublisher:
    """Run Repair mutations behind the shared Publisher seam."""

    def __init__(
        self, owner: RunAcceptanceEngine, promotion: RunRepairPromotion
    ) -> None:
        self.owner = owner
        self.promotion = promotion

    def commit_candidate(
        self, checkout: Path, job: dict[str, Any], attempt: int
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
            candidate = self.owner.git.commit_merge_resolution_candidate(
                checkout,
                run_head_sha=str(job["base_sha"]),
                default_head_sha=str(job["default_base_sha"]),
                attempt=attempt,
                published_sha=job.get("published_sha"),
                **commit_options,
            )
        else:
            candidate = self.owner.git.commit_run_repair_candidate(
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

    def prepare_validation(
        self, checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        self.owner._prepare_repair_validation(checkout, job, validation)

    def invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.promotion.invalidate_stale(state, job, checkout)

    def create_publication_commit(
        self, checkout: Path, job: dict[str, Any], message: str,
        *, save_intent: Callable[[], None],
    ) -> str:
        raise ValueError("Parent repair has no internal publication")

    def ensure_pr(
        self, state: dict[str, Any], job: dict[str, Any], publication: dict[str, Any],
        *, save_intent: Callable[[], None],
    ) -> int:
        raise ValueError("Parent repair has no internal PR")

    def live_pull_request(
        self, state: dict[str, Any], job: dict[str, Any], pr_number: int,
    ) -> dict[str, Any]:
        raise ValueError("Parent repair has no internal PR")

    def after_merge(
        self, state: dict[str, Any], job: dict[str, Any], live: dict[str, Any],
    ) -> bool:
        raise ValueError("Parent repair updates the Run Branch directly")

    def merge(
        self, state: dict[str, Any], job: dict[str, Any], publication: dict[str, Any],
    ) -> str:
        raise ValueError("Parent repair has no internal merge")

    def merge_description(self, job: dict[str, Any]) -> str:
        raise ValueError("Parent repair has no internal merge")

    def escalate(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        self.owner._escalate_repair(state, job, code)

    def sync_attempts(self, state: dict[str, Any], job: dict[str, Any]) -> None:
        self.owner._sync_repair_cycle_counters(
            self.owner._run_state(state), job
        )


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
