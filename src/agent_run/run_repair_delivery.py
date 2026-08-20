from __future__ import annotations

"""Concrete Change Delivery adapters for the Run Repair consumer."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.change_delivery import (
    ChangeDeliveryAdapter,
    ChangeDeliveryEngine,
    ChangeDeliveryPublisher,
    StaleDisposition,
)
from agent_run.git import GitRepository
from agent_run.run_candidate_acceptance import CandidateRunAcceptance
from agent_run.run_currentness import RunCurrentnessReader, refresh_run_currentness
from agent_run.run_repair_currentness import RunRepairCurrentness
from agent_run.run_repair_requests import RunRepairRequests

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine


class RunRepairJobRotationRequired(Exception):
    """A new Candidate needs a fresh Run Repair Job and PR identity."""

    def __init__(
        self,
        *,
        candidate_sha: str,
        base_sha: str,
        repair_branch: str,
        repair_job_attempt: int,
        modification_attempt: int,
    ) -> None:
        super().__init__("integrated Run Repair publication cannot carry a new Candidate")
        self.candidate_sha = candidate_sha
        self.base_sha = base_sha
        self.repair_branch = repair_branch
        self.repair_job_attempt = repair_job_attempt
        self.modification_attempt = modification_attempt


class RunRepairAdapter(ChangeDeliveryAdapter):
    """Run Repair semantics without Git, GitHub, or StateStore writes."""

    stale_disposition = StaleDisposition.FRESH_RUN_ACCEPTANCE

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
        return thread_id not in _all_prior_threads(
            state, _mapping(state, "run_acceptance")
        )

    def base_is_current(self, current_base: str, job: dict[str, Any]) -> bool:
        return current_base in {job.get("base_sha"), job.get("integrated_sha")}

    def development_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return self.requests.development(state, job, checkout)

    def publication_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return self.requests.publication(state, job, checkout)

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
    ) -> dict[str, Any]:
        return self.candidate_acceptance.record(job, reviewer_thread_id, artifact)

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


class RunRepairPublisher(ChangeDeliveryPublisher):
    """Run Repair mutations behind the shared Publisher seam."""

    def __init__(self, owner: RunAcceptanceEngine) -> None:
        self.owner = owner

    def commit_candidate(
        self, checkout: Path, job: dict[str, Any], attempt: int
    ) -> str | None:
        candidate = self.owner.git.commit_run_repair_candidate(
            checkout, attempt=attempt
        )
        if candidate is None:
            return None
        integrated = job.get("integrated_sha")
        publication_sha = job.get("publication_sha")
        if not (
            isinstance(integrated, str)
            and isinstance(publication_sha, str)
            and job.get("integrated_publication_sha") == publication_sha
            and self.owner.git.resolve(f"{candidate}^{{tree}}")
            != self.owner.git.resolve(f"{publication_sha}^{{tree}}")
        ):
            return candidate
        repair_job_attempt = int(job.get("repair_job_attempt", 1)) + 1
        repair_branch = (
            f"agent-run-repair/{job['run_id']}/{job['repair_generation']}"
            f"-job-{repair_job_attempt}"
        )
        raise RunRepairJobRotationRequired(
            candidate_sha=candidate,
            base_sha=integrated,
            repair_branch=repair_branch,
            repair_job_attempt=repair_job_attempt,
            modification_attempt=attempt,
        )

    def rotate_job_checkout(
        self,
        *,
        checkout: Path,
        current_branch: str,
        next_branch: str,
        candidate_sha: str,
    ) -> None:
        self.owner.git.rotate_run_repair_checkout(
            checkout=checkout,
            current_branch=current_branch,
            next_branch=next_branch,
            candidate_sha=candidate_sha,
        )

    def prepare_validation(
        self, checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        self.owner._prepare_repair_validation(checkout, job, validation)

    def ensure_pr(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        publication: dict[str, Any],
    ) -> int:
        github = self.owner.github
        if github is None:
            raise ValueError("Run Repair requires the Publisher")
        return github.ensure_change_pr(
            branch=str(job["repair_branch"]),
            base_branch=str(state["run_branch"]),
            title=str(publication["pr_title"]),
            body=self.owner._render_run_repair_pr_body(state, publication),
            expected_head_sha=str(job["publication_sha"]),
            expected_base_sha=str(job["base_sha"]),
        )

    def live_pull_request(
        self, state: dict[str, Any], _job: dict[str, Any], pr_number: int
    ) -> dict[str, Any]:
        currentness = self.owner.repair_currentness
        if currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        return currentness.live_pull_request(
            state,
            pr_number,
            waiting_for=f"Run Repair PR #{pr_number} readback",
        )

    def invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.owner._invalidate_stale_repair(state, job, checkout)

    def after_merge(
        self, state: dict[str, Any], job: dict[str, Any], live: dict[str, Any]
    ) -> bool:
        return self.owner._after_repair_merge(state, job, live)

    def escalate(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        self.owner._escalate_repair(state, job, code)

    def sync_attempts(self, state: dict[str, Any], job: dict[str, Any]) -> None:
        self.owner._sync_repair_cycle_counters(
            self.owner._run_state(state), job
        )


def _all_prior_threads(
    state: dict[str, Any], run: dict[str, Any]
) -> set[str]:
    values = set(_string_list(run, "reviewer_thread_ids"))
    development_thread = run.get("development_thread_id")
    if isinstance(development_thread, str):
        values.add(development_thread)
    values.update(_string_list(run, "development_thread_history"))
    if run.get("discarded_repair_thread_ids") is not None:
        values.update(_string_list(run, "discarded_repair_thread_ids"))
    for job in _mapping(state, "ticket_jobs").values():
        if not isinstance(job, dict):
            continue
        development_thread = job.get("development_thread_id")
        if isinstance(development_thread, str):
            values.add(development_thread)
        for key in ("development_thread_history", "reviewer_thread_ids"):
            raw_threads = job.get(key, [])
            if isinstance(raw_threads, list):
                values.update(
                    thread for thread in raw_threads if isinstance(thread, str)
                )
    return values


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"{key} must contain strings")
    return list(value)
