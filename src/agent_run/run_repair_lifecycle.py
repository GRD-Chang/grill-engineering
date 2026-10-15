from __future__ import annotations

"""Run Repair job construction and Change Delivery lifecycle orchestration."""

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.change_delivery import (
    ChangeDeliveryEngine,
    ChangeDeliveryStateStore,
    ChangeJobContract,
)
from agent_run.resume_materials import require_change_checkout
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.delivery_policy import run_repair_budget_policy_for_job
from agent_run.git import MergeConflictError
from agent_run.run_currentness import ticket_completion_records
from agent_run.run_repair_cycle import (
    pause_human_blocked_repair_cycle,
    repair_checkout_is_active,
    start_repair_cycle,
    sync_repair_cycle_counters,
    uses_merge_resolution,
)
from agent_run.review_budget import ensure_budget
from agent_run.run_repair_currentness import RunRepairObservationPending
from agent_run.run_repair_delivery import (
    RunRepairAdapter,
    RunRepairPublisher,
)
from agent_run.change_delivery_integration import prepare_delivery_integration
from agent_run.run_repair_write import update_run_branch
from agent_run.run_parent_integration import ensure_run_repair_base
from agent_run.run_thread_identity import prior_thread_identities
from agent_run.semantic_attempt import (
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
)

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine
    from agent_run.run_repair_promotion import RunRepairPromotion


class RunRepairLifecycle:
    """Own one Run Repair Job lifecycle while the engine remains its façade."""

    def __init__(
        self, owner: RunAcceptanceEngine, promotion: RunRepairPromotion
    ) -> None:
        self.owner = owner
        self.promotion = promotion

    def _repair(self, state: dict[str, Any], run: dict[str, Any]) -> str:
        """Develop, review and directly integrate the Parent repair."""
        if self.owner.github is None:
            raise ValueError("Run Repair requires the Publisher")
        checkout = self.owner._repair_checkout(state)
        job: dict[str, Any] | None = None
        preserve_checkout = False
        try:
            while True:
                job = self._repair_job(state, run)
                if isinstance(job.get("run_branch_write_intent"), dict):
                    if update_run_branch(self.owner, self.promotion, state, job):
                        break
                    if job.get("phase") == "candidate":
                        continue
                    preserve_checkout = True
                    break
                require_change_checkout(
                    state, job, f"run-repair:{state['run_id']}", self.owner.states.root, self.owner.git
                )
                branch = str(job["repair_branch"])
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
                    self.promotion.complete_integration_reprepare(state, job, checkout)
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
                if job.get("phase") == "accepted":
                    engine = self._repair_engine(state, job)
                    # This integration stage runs outside Change.advance, but
                    # may need conflict Development under the same admission.
                    with engine.agent_work.step(job, allow_agent=True):
                        prepared = prepare_delivery_integration(engine, state, job, checkout)
                    if not prepared:
                        if job.get("phase") == "candidate":
                            continue
                    elif update_run_branch(self.owner, self.promotion, state, job):
                        break
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
                and (job.get("phase") == "stale" or (
                    job.get("phase") == "blocked"
                    and job.get("blocked_reason") in {
                        "modification_budget_exhausted", "review_budget_exhausted",
                    }
                ))
                and self.owner.git.managed_checkout_dirty_reason(checkout) is not None
            ):
                preserve_checkout = True
            # Promotion cleanup below binds the approved source head; generic
            # worktree removal must not run first and bypass that protection.
            completed = job is not None and job.get("phase") == "completed"
            if not preserve_checkout and not completed:
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
        if candidate == job.get("base_sha"):
            # The original Candidate was already written. Its current Run
            # head is the new merge base; there is no unintegrated squash
            # delta to convert when the latest default now conflicts.
            job.pop("integration_squash_candidate_sha", None)
            job.pop("integration_squash_conversion_required", None)
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
        run_state = self.owner._run_state(state)
        policy = run_repair_budget_policy_for_job(
            job,
            state_snapshot=run_state.get("policy_snapshot")
            or state.get("policy_snapshot"),
        )
        return ChangeDeliveryEngine(
            git=self.owner.git,
            github=github,
            agents=self.owner.agents,
            contract=ChangeJobContract(
                label=f"run-repair-{job['repair_attempt']}",
                branch=str(job["repair_branch"]),
                base_branch=str(state["run_branch"]),
                review_budget_policy=policy,
            ),
            adapter=RunRepairAdapter(
                git=self.owner.git,
                requests=requests,
                candidate_acceptance=self.owner.candidate_acceptance,
                currentness=currentness,
                currentness_reader=self.owner.currentness_reader,
                default_head_sha=self.owner.default_head_sha,
            ),
            publisher=RunRepairPublisher(self.owner, self.promotion),
            state_store=ChangeDeliveryStateStore(self.owner.states, str(state["run_id"])),
            development_only=True,
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
        ensure_run_repair_base(self.owner, state, run)
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
        run_policy = run_repair_budget_policy_for_job(
            run_state, state_snapshot=state.get("policy_snapshot")
        )
        ensure_budget(run_state, run_policy)
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
            "policy_snapshot": deepcopy(
                run_state.get("policy_snapshot") or state.get("policy_snapshot")
            ),
            "repair_checkout": str(self.owner._repair_checkout(state)),
        }
        for key in (
            "prior_human_blockers",
            "human_response_history",
            "human_response_generation",
            "development_new_thread",
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
        baseline = run.get("acceptance_record")
        if isinstance(baseline, dict):
            job["previous_parent_acceptance_record"] = deepcopy(baseline)
        if repair_source == "required_checks":
            evidence = repair_request.get("ci_evidence")
            if not isinstance(evidence, dict):
                raise ValueError("required-check repair evidence must be an object")
            job["ci_evidence"] = deepcopy(evidence)
            origin = deepcopy(evidence)
            origin["head_sha"] = base_sha
            origin["result"] = "fail"
            job["required_checks_origin"] = origin
            job["next_attempt_kind"] = "ci_repair"
        if repair_source == "merge_conflict":
            evidence = repair_request.get("merge_conflict_evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError("merge-conflict repair evidence must be non-empty")
            job["merge_conflict_evidence"] = evidence
        job["repair_trigger_pending"] = True
        windows = run.setdefault("repair_integration_budgets", {})
        windows[str(generation)] = {"policy_snapshot": deepcopy(job["policy_snapshot"])}
        run["repair_job"] = job
        self._sync_repair_cycle_counters(run, job)
        self.owner._save(state)
        self._complete_pending_repair_trigger(state, job)
        return job

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
