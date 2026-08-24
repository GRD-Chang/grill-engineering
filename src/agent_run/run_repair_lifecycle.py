from __future__ import annotations

"""Run Repair job construction and Change Delivery lifecycle orchestration."""

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.change_delivery import (
    ChangeDeliveryEngine,
    ChangeDeliveryStateStore,
    ChangeJobContract,
    ensure_change_branch_authority,
)
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.git import MergeConflictError
from agent_run.run_currentness import ticket_completion_records
from agent_run.run_repair_cycle import (
    pause_human_blocked_repair_cycle,
    repair_checkout_is_active,
    rotate_repair_job,
    start_repair_cycle,
    sync_repair_cycle_counters,
    uses_merge_resolution,
)
from agent_run.review_budget import RUN_POLICY, ensure_budget, new_budget
from agent_run.run_repair_currentness import RunRepairObservationPending
from agent_run.run_repair_delivery import (
    RunRepairAdapter,
    RunRepairJobRotationRequired,
    RunRepairPublisher,
)
from agent_run.run_thread_identity import prior_thread_identities
from agent_run.semantic_attempt import (
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
)

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine


class RunRepairLifecycle:
    """Own one Run Repair Job lifecycle while the engine remains its façade."""

    def __init__(self, owner: RunAcceptanceEngine) -> None:
        self.owner = owner

    def _repair(self, state: dict[str, Any], run: dict[str, Any]) -> str:
        """Deliver a Run Repair through the same Change Job lifecycle as Tickets.

        The repair remains deliberately separate from a Ticket: it receives a
        Run-Repair branch and PR.  Its completion callback promotes a strictly
        matching Candidate Acceptance directly to Run Publication; any mismatch
        instead discards that Candidate and starts a fresh overall acceptance.
        The shared engine owns every candidate, publication, fresh-review,
        checks, exact-head, and squash transition in between.
        """
        if self.owner.github is None:
            raise ValueError("Run Repair requires the Publisher")
        checkout = self.owner._repair_checkout(state)
        job: dict[str, Any] | None = None
        preserve_checkout = False
        try:
            while True:
                job = self._repair_job(state, run)
                self._complete_pending_repair_job_rotation(state, job, checkout)
                branch = str(job["repair_branch"])
                repair_is_integrated = isinstance(job.get("integrated_sha"), str)
                if not repair_is_integrated:
                    ensure_change_branch_authority(
                        github=self.owner.github,
                        state=state,
                        job=job,
                        branch=branch,
                        base_branch=str(state["run_branch"]),
                        save=self.owner._save,
                    )
                job["repair_checkout"] = str(checkout)
                self._sync_repair_cycle_counters(run, job)
                self.owner.git.prepare_ticket_checkout(
                    branch=branch, base_sha=str(job["base_sha"]), checkout=checkout
                )
                preserved_candidate = job.pop("repair_seed_candidate_sha", None)
                if preserved_candidate is not None:
                    self.owner.git.seed_managed_checkout(
                        checkout, expected_head=str(preserved_candidate)
                    )
                    self.owner._save(state)
                if job.get("integration_reprepare_required") is True:
                    self.owner._complete_integration_reprepare(state, job, checkout)
                elif job.get("integration_squash_conversion_required") is True:
                    self._complete_squash_repair_conflict_conversion(
                        state, job, checkout
                    )
                elif uses_merge_resolution(job) and not isinstance(
                    job.get("candidate_sha"), str
                ):
                    evidence = self.owner.git.prepare_integration_repair_checkout(
                        checkout,
                        run_head_sha=str(job["base_sha"]),
                        default_head_sha=str(job["default_base_sha"]),
                        squash_candidate_sha=(
                            str(job["integration_squash_candidate_sha"])
                            if isinstance(
                                job.get("integration_squash_candidate_sha"), str
                            )
                            else None
                        ),
                        allow_clean_merge=job.get("phase") == "committing_candidate",
                        allow_staged_resolution=(
                            job.get("development_failure_resume") is True
                        ),
                    )
                    if evidence:
                        job["merge_conflict_evidence"] = evidence
                        job["integration_conflict_paths"] = list(
                            self.owner.git.integration_conflict_paths(checkout)
                        )
                try:
                    self._repair_engine(state, job).run(state, job, checkout)
                except MergeConflictError as error:
                    if not self._convert_squash_repair_conflict(
                        state, job, checkout, error
                    ):
                        raise
                    preserve_checkout = True
                    continue
                except RunRepairJobRotationRequired as rotation:
                    self._close_rebound_attempt(state, job)
                    job = rotate_repair_job(
                        run,
                        job,
                        candidate_sha=rotation.candidate_sha,
                        base_sha=rotation.base_sha,
                        repair_branch=rotation.repair_branch,
                        repair_job_attempt=rotation.repair_job_attempt,
                        modification_attempt=rotation.modification_attempt,
                        repair_checkout=str(checkout),
                    )
                    self.owner._save(state)
                    preserve_checkout = True
                    continue
                self._sync_repair_cycle_counters(run, job)
                pause_human_blocked_repair_cycle(run, job)
                self.owner._save(state)
                preserve_checkout = self._repair_checkout_is_active(job)
                break
        except RunRepairObservationPending:
            preserve_checkout = True
            return "waiting"
        finally:
            if job is not None and self._repair_checkout_is_active(job):
                preserve_checkout = True
            if (
                job is not None
                and job.get("phase") == "stale"
                and self.owner.git.managed_checkout_dirty_reason(checkout) is not None
            ):
                preserve_checkout = True
            if not preserve_checkout:
                self.owner.git.remove_worktree(checkout)
                self.owner._remove_empty_directories(checkout)
        assert job is not None
        if job["phase"] == "completed":
            completed_repairs = run.get("completed_repair_jobs", [])
            if not isinstance(completed_repairs, list) or not all(
                isinstance(completed, dict) for completed in completed_repairs
            ):
                raise ValueError("completed_repair_jobs must contain objects")
            DeliveryCleanupEngine(
                git=self.owner.git, states=self.owner.states, github=self.owner.github
            ).complete_run_repairs(state, completed_repairs)
            return "promoted"
        if job["phase"] == "blocked":
            return (
                "no_code_changes"
                if job.get("blocked_reason") == "no_code_changes"
                else "blocked"
            )
        if job["phase"] == "stale":
            return "stale"
        return "waiting"

    def _convert_squash_repair_conflict(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
        error: MergeConflictError,
    ) -> bool:
        """Convert a stale squash Candidate preview into the real conflict scene."""

        candidate = job.get("candidate_sha")
        if uses_merge_resolution(job) or not isinstance(candidate, str):
            return False
        self._close_rebound_attempt(state, job)
        job.update(
            {
                "repair_mode": "merge_resolution",
                "repair_source": "merge_conflict",
                "merge_conflict_evidence": str(error),
                "integration_squash_candidate_sha": candidate,
                "integration_squash_conversion_required": True,
                "phase": "repairing",
            }
        )
        job.pop("candidate_sha", None)
        job.pop("acceptance_record", None)
        job.pop("acceptance_artifact", None)
        job.pop("pending_review_result", None)
        run = self.owner._run_state(state)
        run["phase"] = "repairing"
        self._sync_repair_cycle_counters(run, job)
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_repair_pending",
                "diagnostics": [],
            }
        )
        self.owner._save(state)
        return True

    @staticmethod
    def _close_rebound_attempt(
        state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        attempt = pending_semantic_attempt(job)
        if attempt is None:
            return
        close_semantic_attempt(job, attempt, outcome="currentness_invalidated")
        detach_active_invocation(state, attempt)

    def _complete_squash_repair_conflict_conversion(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        candidate = job.get("integration_squash_candidate_sha")
        if not isinstance(candidate, str):
            raise ValueError("squash conflict conversion requires its Candidate")
        evidence = self.owner.git.convert_squash_candidate_to_integration_repair(
            checkout,
            run_head_sha=str(job["base_sha"]),
            default_head_sha=str(job["default_base_sha"]),
            candidate_sha=candidate,
        )
        job.update(
            {
                "repair_mode": "merge_resolution",
                "repair_source": "merge_conflict",
                "merge_conflict_evidence": evidence,
                "integration_conflict_paths": list(
                    self.owner.git.integration_conflict_paths(checkout)
                ),
                "phase": "repairing",
            }
        )
        job.pop("integration_squash_conversion_required", None)
        self.owner._save(state)

    def _repair_engine(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> ChangeDeliveryEngine:
        github = self.owner.github
        requests = self.owner.repair_requests
        currentness = self.owner.repair_currentness
        if github is None or requests is None or currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        return ChangeDeliveryEngine(
            git=self.owner.git,
            github=github,
            agents=self.owner.agents,
            contract=ChangeJobContract(
                label=f"run-repair-{job['repair_attempt']}",
                branch=str(job["repair_branch"]),
                base_branch=str(state["run_branch"]),
            ),
            adapter=RunRepairAdapter(
                git=self.owner.git,
                requests=requests,
                candidate_acceptance=self.owner.candidate_acceptance,
                currentness=currentness,
                currentness_reader=self.owner.currentness_reader,
                default_head_sha=self.owner.default_head_sha,
            ),
            publisher=RunRepairPublisher(self.owner),
            state_store=ChangeDeliveryStateStore(self.owner.states, str(state["run_id"])),
        )

    def _repair_job(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
    ) -> dict[str, Any]:
        existing = run.get("repair_job")
        if isinstance(existing, dict):
            self._complete_pending_repair_trigger(state, existing)
            return existing
        base_sha = self.owner.git.resolve(str(state["run_branch"]))
        generation = int(run.get("repair_generation", 0)) + 1
        run["repair_generation"] = generation
        initial_modifications = 0
        initial_validations = 0
        start_repair_cycle(run, generation)
        # The generic engine only knows a job's own thread history.  Seed it
        # with every prior Ticket and Run identity so a repair reviewer cannot
        # accidentally reuse any of them.
        prior_thread_set = prior_thread_identities(state, run)
        development_thread_id = None
        development_thread_history: list[str] = []
        prior_threads = sorted(prior_thread_set)
        repair_request = run.pop("repair_request", {})
        if not isinstance(repair_request, dict):
            raise ValueError("repair_request must be an object")
        requested_thread_id = repair_request.get("development_thread_id")
        if requested_thread_id is not None and (
            not isinstance(requested_thread_id, str) or not requested_thread_id.strip()
        ):
            raise ValueError("repair_request has an invalid Development Thread ID")
        requested_thread_history = repair_request.get("development_thread_history", [])
        if not isinstance(requested_thread_history, list) or not all(
            isinstance(item, str) and item.strip() for item in requested_thread_history
        ):
            raise ValueError("repair_request has invalid Development Thread history")
        development_thread_id = requested_thread_id
        development_thread_history = list(requested_thread_history)
        preserved_candidate = repair_request.get("repair_candidate_sha")
        if preserved_candidate is not None and (
            not isinstance(preserved_candidate, str)
            or not preserved_candidate.strip()
        ):
            raise ValueError("repair_request has an invalid preserved Candidate")
        if isinstance(preserved_candidate, str):
            if not self.owner.git.is_ancestor(base_sha, preserved_candidate):
                raise ValueError(
                    "incompatible_run_state: preserved Run Repair Candidate is not based on the current Run Branch"
                )
            self.owner.git.resolve(f"{preserved_candidate}^{{tree}}")
        run_state = self.owner._run_state(state)
        run_budget = ensure_budget(run_state, RUN_POLICY)
        run_budget_history = run_state.get("review_budget_history")
        if not isinstance(run_budget_history, list):
            raise ValueError("run review_budget_history must be an array")
        prior_review_artifacts = deepcopy(run_budget["review_artifacts"][-1:])
        repair_source = str(repair_request.get("repair_source", "acceptance"))
        if repair_source not in {
            "acceptance",
            "git_integrity",
            "human_revision",
            "required_checks",
            "merge_conflict",
        }:
            raise ValueError("invalid Run Repair source")
        requested_repair_mode = repair_request.get("repair_mode")
        if requested_repair_mode is not None and requested_repair_mode not in {
            "squash",
            "merge_resolution",
        }:
            raise ValueError("invalid Run Repair mode")
        job = {
            "run_id": state["run_id"],
            "phase": "developing",
            "repair_attempt": generation,
            "repair_job_attempt": 1,
            "repair_generation": generation,
            "repair_branch": (
                f"agent-run-repair/{state['run_id']}/{generation}"
                if generation == 1
                else f"agent-run-repair/{state['run_id']}/{generation}-generation-{generation}"
            ),
            "base_sha": base_sha,
            "default_base_sha": self.owner._default_head(state),
            "repair_base_run_head_sha": base_sha,
            "parent_revision": self.owner._mapping(state, "parent")["revision"],
            "ticket_graph_revision": self.owner._mapping(state, "ticket_graph")["revision"],
            "ticket_completion_records": ticket_completion_records(state),
            "repair_source": repair_source,
            "repair_mode": (
                str(requested_repair_mode)
                if requested_repair_mode is not None
                else (
                    "merge_resolution"
                    if repair_source == "merge_conflict"
                    else "squash"
                )
            ),
            "modification_attempts": initial_modifications,
            "code_modification_attempts": initial_modifications,
            "validation_attempts": initial_validations,
            "acceptance_generation": int(run.get("acceptance_generation", 1)),
            "development_thread_id": development_thread_id,
            "development_thread_history": development_thread_history,
            "repair_seed_candidate_sha": preserved_candidate,
            "reviewer_thread_ids": prior_threads,
            "prior_reviewer_thread_ids": prior_threads,
            "candidate_acceptance_history": [],
            "review_budget": new_budget(
                window=run_budget["window"],
                reviewer_invocations=run_budget["reviewer_invocations"],
                review_artifacts=prior_review_artifacts,
            ),
            "review_budget_history": deepcopy(run_budget_history),
            "repair_checkout": str(self.owner._repair_checkout(state)),
        }
        for key in (
            "prior_human_blockers",
            "human_response_history",
            "human_response_generation",
        ):
            if key in repair_request:
                job[key] = deepcopy(repair_request[key])
        if repair_source == "acceptance":
            artifact = repair_request.get("acceptance_artifact")
            if not isinstance(artifact, dict):
                artifact = self.owner._mapping(run, "acceptance_artifact")
            job["repair_input_artifact"] = artifact
            job["acceptance_artifact"] = artifact
            job["unresolved_acceptance_artifact"] = deepcopy(artifact)
        if repair_source == "git_integrity":
            evidence = repair_request.get("git_integrity_evidence")
            if not isinstance(evidence, dict):
                raise ValueError("Git Integrity repair evidence must be an object")
            job["git_integrity_evidence"] = evidence
        if repair_source == "human_revision":
            feedback = repair_request.get("human_feedback")
            if not isinstance(feedback, str) or not feedback.strip():
                raise ValueError("human revision feedback must be non-empty")
            job["human_feedback"] = feedback
        if repair_source == "required_checks":
            evidence = repair_request.get("ci_evidence")
            if not isinstance(evidence, dict):
                raise ValueError("required-check repair evidence must be an object")
            job["ci_evidence"] = evidence
        if repair_source == "merge_conflict":
            evidence = repair_request.get("merge_conflict_evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError("merge-conflict repair evidence must be non-empty")
            job["merge_conflict_evidence"] = evidence
        job["repair_trigger_pending"] = True
        run["repair_job"] = job
        self._sync_repair_cycle_counters(run, job)
        self.owner._save(state)
        self._complete_pending_repair_trigger(state, job)
        return job

    def _complete_pending_repair_job_rotation(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        raw_rotation = job.get("repair_job_rotation")
        if raw_rotation is None:
            return
        rotation = self.owner._mapping(job, "repair_job_rotation")
        current_branch = rotation.get("current_branch")
        next_branch = rotation.get("next_branch")
        candidate_sha = rotation.get("candidate_sha")
        if (
            not isinstance(current_branch, str)
            or not current_branch
            or not isinstance(next_branch, str)
            or not next_branch
            or not isinstance(candidate_sha, str)
            or not candidate_sha
        ):
            raise ValueError("repair_job_rotation has invalid identity")
        if (
            next_branch != job.get("repair_branch")
            or candidate_sha != job.get("candidate_sha")
        ):
            raise ValueError("repair_job_rotation does not match the active Job")
        RunRepairPublisher(self.owner).rotate_job_checkout(
            checkout=checkout,
            current_branch=current_branch,
            next_branch=next_branch,
            candidate_sha=candidate_sha,
        )
        job.pop("repair_job_rotation", None)
        self.owner._save(state)

    def _complete_pending_repair_trigger(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        if job.get("repair_trigger_pending") is not True:
            return
        repair_source = str(job["repair_source"])
        repair_request: dict[str, Any] = {}
        if repair_source == "required_checks":
            repair_request["ci_evidence"] = self.owner._mapping(job, "ci_evidence")
        trigger = self._repair_trigger(state, repair_source, repair_request)
        if trigger is not None:
            job["repair_trigger"] = trigger
        job.pop("repair_trigger_pending", None)
        self.owner._save(state)

    @staticmethod
    def _repair_checkout_is_active(job: dict[str, Any]) -> bool:
        return repair_checkout_is_active(job)

    @staticmethod
    def _sync_repair_cycle_counters(
        run: dict[str, Any], job: dict[str, Any]
    ) -> None:
        sync_repair_cycle_counters(run, job)

    def _render_run_repair_pr_body(
        self, state: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        requests = self.owner.repair_requests
        if requests is None:
            raise ValueError("Run Repair requires the Publisher")
        return requests.render_pr_body(state, publication)

    def _prepare_repair_validation(
        self, _checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        self.owner.candidate_acceptance.prepare_validation(job, validation)

    def _repair_trigger(
        self,
        state: dict[str, Any],
        repair_source: str,
        repair_request: dict[str, Any],
    ) -> dict[str, Any] | None:
        currentness = self.owner.repair_currentness
        if currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        return currentness.create_trigger(state, repair_source, repair_request)
