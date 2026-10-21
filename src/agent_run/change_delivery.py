from __future__ import annotations

"""Shared, deterministic delivery lifecycle for a change job.

Delivery stages own this lifecycle. A typed job descriptor supplies only
deterministic identity and branch facts; a semantic adapter supplies request
construction and currentness decisions.  Controller/Publisher side effects
and StateStore persistence are kept in their own seam.  In particular,
neither layer gets a shortcut from development to a Run Branch write.
"""

from copy import deepcopy
from pathlib import Path
import sys
from typing import Any

from agent_run.delivery_budget import delivery_budget_owner
from agent_run.agents import AgentBackend
from agent_run.delivery_policy import change_review_budget_policy
from agent_run.change_currentness import change_agent_is_current, reject_stale_change
from agent_run.development_review import DevelopmentReview
from agent_run.integration_development_budget import (
    IntegrationDevelopmentExhausted, ensure_integration_development_available,
)
from agent_run.delivery_step import AgentWorkRequired, DeliveryStep, DeliveryStepKind
from agent_run.change_delivery_integration import prepare_delivery_integration
from agent_run.ci_recovery_budget import (
    can_start_ci_repair,
    ci_limit_for_job,
    mark_ci_repair,
    refund_ci_repair_without_candidate,
)
from agent_run.artifacts import record_human_blocker
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError
from agent_run.external_supervision import (
    ensure_supervision_window,
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.publication_operation_retry import record_publication_operation_failure
from agent_run.publication_pending import publication_pending_diagnostic
from agent_run.credential_lifecycle import InitialCredentialUnavailable
from agent_run.semantic_attempt import (
    pending_semantic_attempt,
    release_semantic_attempt,
)
from agent_run.review_budget import (
    ReviewBudgetPolicy,
    can_start_development,
    can_start_review,
    ensure_budget,
    mark_development,
)

from agent_run.agent_work import AgentWorkScope
from agent_run.state_contract import require_current_run_state

MAX_PUBLICATION_CONTEXT_ATTEMPTS = 4


from agent_run.change_delivery_branches import (
    ensure_change_branch_authority as ensure_change_branch_authority,
    ensure_linked_branch_display as ensure_linked_branch_display,
)
from agent_run.change_delivery_contracts import (
    ChangeDeliveryAdapter as ChangeDeliveryAdapter,
    ChangeDeliveryPublisher as ChangeDeliveryPublisher,
    ChangeJobContract as ChangeJobContract,
    StaleDisposition as StaleDisposition,
    ChangeJobStopped,
)
from agent_run.change_delivery_fallback import (
    fallback_candidate_is_eligible,
    fallback_publication_context,
    prepare_ticket_fallback,
    previous_publication_authorization,
)
from agent_run.change_delivery_publication_agent import (
    invocation_boundary,
    invocation_identity,
    publication,
)
from agent_run.change_delivery_published_head import publish_and_merge
from agent_run.change_delivery_review import record_change_status
from agent_run.change_delivery_state import (
    block_change_job,
    save_change_job,
    ChangeDeliveryStateStore as ChangeDeliveryStateStore,
    require_mapping as _mapping,
)
from agent_run.change_delivery_threads import (
    latest_reviewer_thread as latest_reviewer_thread,
)
from agent_run.ticket_publication_contract import (
    require_active_ticket_publication_authorization,
)
from agent_run.required_checks_observation import (
    sync_fallback_receipt_observation,
)
from agent_run.ticket_integration import (
    integration_record_is_valid,
)
from agent_run.ticket_phase import sync_active_ticket_job
from agent_run.ticket_queue import (
    acquire as acquire_ticket_slot,
    has_other_ticket_work,
)
from agent_run.parent_integration import (
    parent_integration_record_is_valid,
)


class ChangeDeliveryEngine:
    """One Development -> Candidate -> Review -> Publication -> Merge loop.

    ``job`` retains the canonical durable phases without a second step state.
    Run Repair records use the same fields but have no
    Ticket number and their ``after_merge`` callback never closes an Issue.
    """

    def __init__(
        self,
        *,
        git: GitRepository,
        github: GitHubPublisher,
        agents: AgentBackend,
        contract: ChangeJobContract,
        adapter: ChangeDeliveryAdapter,
        publisher: ChangeDeliveryPublisher,
        state_store: ChangeDeliveryStateStore,
        development_only: bool = False,
        work: DevelopmentReview,
    ) -> None:
        self.git = git
        self.github = github
        self.agents = agents
        self.contract = contract
        self.adapter = adapter
        self.publisher = publisher
        self.state_store = state_store
        self.development_only = development_only
        self._current_job: dict[str, Any] | None = None
        self.work = work
        self.agent_work = work.agent_work

    def _pause_ticket_if_requested(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        from agent_run.ticket_pause import can_pause_ticket, pause_ticket

        if not self.contract.label.startswith("ticket-") or not can_pause_ticket(job):
            return False
        if not self._ticket_stop_requested(state, job):
            return False
        pause_ticket(state, job)
        self.save(state)
        return True

    def _ticket_stop_requested(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        if not self.contract.label.startswith("ticket-"):
            return False
        return self.state_store.states.ticket_pause_requested(
            str(state["run_id"]), int(job["ticket_number"]),
        )

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        return save_change_job(self.state_store, state, self._current_job,
            label=self.contract.label, development_only=self.development_only)

    def commit_required_checks_observation(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        pr_number: int,
    ) -> dict[str, Any]:
        """Validate and atomically persist one complete Observation commit."""

        sync_fallback_receipt_observation(job, pr_number)
        if self.contract.label.startswith("ticket-"):
            self.state_store.states.validate_work_projection(state)
        else:
            require_current_run_state(state, parent_scope=True)
        return self.save(state)

    def run(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        """Complete the existing entry point using the same resumable steps."""
        approved = isinstance(job.get("approval_grant"), dict)
        while self.advance(state, job, checkout, allow_agent=True).kind in {
            DeliveryStepKind.CONTINUE, DeliveryStepKind.NEEDS_AGENT,
        }:
            if approved and not isinstance(job.get("approval_grant"), dict):
                # Yield to the lifecycle owner before work on a new result.
                # It must end the old noncancellable Action first.
                break
        return state

    def advance(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path,
        *, allow_agent: bool = False,
    ) -> DeliveryStep:
        """Advance one stage, yielding before Agent work without a work slot.

        Waiting CI and merge reconciliation remain executable without an Agent.
        No shadow phase or budget is persisted: the existing Job is resumable
        through either this method or the complete ``run`` entry point.
        """
        if self.contract.label.startswith("ticket-"):
            if state.get("active_ticket_job") is not job:
                raise ValueError("Delivery step requires the active Ticket Job")
            sync_active_ticket_job(state)
        self._current_job = job
        with self.agent_work.step(job, allow_agent=allow_agent):
            return self._advance_stage(state, job, checkout)

    def _advance_stage(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path,
    ) -> DeliveryStep:
        try:
            ensure_budget(delivery_budget_owner(state, job), self.review_budget_policy())
            if self._pause_ticket_if_requested(state, job):
                return self._step_result(state, job)
            self._require_ticket_publication_authorization(job)
            phase = str(job["phase"])
            if phase in {"completed", "blocked", "stale", "publication_pending", "ready_for_approval"}:
                return self._step_result(state, job)
            if not self.reconcile_pending_pr_write(state, job):
                return self._step_result(state, job)
            if phase == "ready_for_integration":
                if self.development_only or not acquire_ticket_slot(state, job):
                    return DeliveryStep(DeliveryStepKind.WAITING, str(job["phase"]), "integration_queue")
                self.save(state)
                return self._step_result(state, job)
            if phase == "escalating":
                self.publisher.escalate(state, job, str(job["escalation_code"]))
                job["phase"] = "blocked"
                self.save(state)
                return self._step_result(state, job)
            if phase in {"developing", "repairing", "committing_candidate", "candidate", "reviewing"}:
                if (phase == "candidate" and self.contract.label == "parent-only"
                    and not isinstance(job.get("pending_review_result"), dict)
                    and not (isinstance(job.get("pending_semantic_attempt"), dict) and "returned_result" in job["pending_semantic_attempt"])):
                    if not prepare_delivery_integration(self, state, job, checkout, before_review=True):
                        return self._step_result(state, job, waiting=job.get("phase") == "ready_for_integration")
                return self.work.advance_change(state, job, checkout, scope="ticket" if self.contract.label.startswith("ticket-") else "parent-only", allow_agent=True)
            if phase == "accepted":
                if self.development_only:
                    return DeliveryStep(DeliveryStepKind.WAITING, phase, "development_complete")
                if self._can_resume_integrated_publication(job):
                    job["phase"] = "merged"
                    self.save(state)
                    return self._step_result(state, job)
                if not prepare_delivery_integration(self, state, job, checkout):
                    return self._step_result(state, job)
                self._publication(state, job, checkout)
                return self._step_result(state, job)
            if phase in {"publishing", "waiting_checks", "waiting_merge", "merging", "merged"}:
                if phase == "waiting_merge":
                    job["phase"] = "merging"
                    self.save(state)
                terminal = self._publish_and_merge(state, job, checkout)
                if (
                    self.contract.label.startswith("ticket-")
                    and job.get("phase") == "repairing"
                    and state.get("ticket_integration_holder") != job.get("ticket_number")
                    and has_other_ticket_work(state, int(job["ticket_number"]))
                ):
                    terminal = True
                return self._step_result(state, job, waiting=terminal)
            raise ValueError(f"unknown Ticket phase: {job['phase']}")
        except AgentWorkRequired as work:
            if self.agent_work.capacity_wait:
                return DeliveryStep(DeliveryStepKind.WAITING, str(job["phase"]), "work_capacity")
            return DeliveryStep(DeliveryStepKind.NEEDS_AGENT, str(job["phase"]), work.role)
        except ChangeJobStopped:
            return self._step_result(state, job, waiting=True)
        except GitHubReadError as error:
            if not is_github_convergence_error(error.code):
                raise
            wait_for_github_convergence(
                state, code=error.code, message=str(error), waiting_for=self.contract.label,
            )
            ensure_supervision_window(state)
            self.save(state)
            self.agent_work.defer()
            return self._step_result(state, job, waiting=True)
        except InitialCredentialUnavailable as error:
            self.work._refund_unstarted_credential_invocation(state, job)
            self.work._wait_for_initial_credential(state, job, http_status=error.http_status)
            self.agent_work.defer()
            return self._step_result(state, job, waiting=True)

    def reconcile_pending_pr_write(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        from agent_run.change_pr_reconciliation import reconcile_pending_pr_write

        return reconcile_pending_pr_write(self, state, job)

    def release_agent_work(self) -> None:
        self.agent_work.release(error=sys.exception())

    def require_agent_work(self, role: str) -> None:
        self.agent_work.require(role)

    @staticmethod
    def _step_result(
        state: dict[str, Any], job: dict[str, Any], *, waiting: bool = False,
    ) -> DeliveryStep:
        phase = str(job["phase"])
        reason = (
            "awaiting_approval" if phase == "ready_for_approval"
            else job.get("blocked_reason") or job.get("escalation_code")
        )
        if phase == "completed":
            kind = DeliveryStepKind.COMPLETED
        elif phase in {"blocked", "stale", "publication_pending", "ready_for_approval"} or (
            waiting and state.get("terminal_kind") == "waiting_human"
        ):
            kind = DeliveryStepKind.PAUSED
        elif phase == "ready_for_integration" or waiting:
            kind = DeliveryStepKind.WAITING
        else:
            kind = DeliveryStepKind.CONTINUE
        return DeliveryStep(kind, phase, str(reason) if reason else None)

    def _can_resume_integrated_publication(self, job: dict[str, Any]) -> bool:
        """Return whether an already merged publication can resume closeout."""

        publication = job.get("publication")
        candidate_sha = job.get("candidate_sha")
        publication_sha = job.get("publication_sha")
        if not (
            isinstance(job.get("integrated_sha"), str)
            and isinstance(job.get("pr_number"), int)
            and isinstance(publication, dict)
            and isinstance(candidate_sha, str)
            and isinstance(publication_sha, str)
            and job.get("published_sha") == publication_sha
            and job.get("integrated_publication_sha") == publication_sha
        ):
            return False
        try:
            return self.git.resolve(f"{candidate_sha}^{{tree}}") == self.git.resolve(
                f"{publication_sha}^{{tree}}"
            )
        except (GitError, ValueError):
            return False





    def _publication(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        publication(self, state, job, checkout)

    def _require_ticket_publication_authorization(
        self, job: dict[str, Any]
    ) -> None:
        if not self.contract.label.startswith("ticket-") or job.get("phase") not in {
            "accepted",
            "publication_pending",
            "publishing",
            "waiting_checks",
            "waiting_merge",
            "merging",
        }:
            return
        candidate_sha = job.get("candidate_sha")
        if not isinstance(candidate_sha, str) or not candidate_sha.strip():
            raise ValueError("active Ticket publication is missing candidate_sha")
        candidate_tree = self.git.resolve(f"{candidate_sha}^{{tree}}")
        require_active_ticket_publication_authorization(
            job,
            candidate_tree=candidate_tree,
            location=self.contract.label.replace("ticket-", "ticket_jobs[", 1)
            + "]",
        )

    def _invocation_boundary(self, job: dict[str, Any]) -> dict[str, Any]:
        candidate_sha = job.get("candidate_sha")
        candidate_tree = (
            self.git.resolve(f"{candidate_sha}^{{tree}}")
            if isinstance(candidate_sha, str)
            else None
        )
        return invocation_boundary(job, candidate_tree=candidate_tree)

    @staticmethod
    def _invocation_identity(
        state: dict[str, Any], job: dict[str, Any]
    ) -> tuple[str, int]:
        return invocation_identity(state, job)

    def _wait_for_human(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        phase: str,
        blockers: tuple[str, ...],
        code: str = "agent_requires_human",
    ) -> None:
        record_human_blocker(
            state, job, resume_phase=phase, blocked_phase="blocked", blockers=blockers, code=code,
        )
        self.save(state)

    def _publish_and_merge(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> bool:
        return publish_and_merge(self, state, job, checkout)



    def review_budget_policy(self) -> ReviewBudgetPolicy:
        return change_review_budget_policy(
            self.contract.label, self.contract.review_budget_policy, self._current_job,
        )









    def _record_publication_operation_failure(
        self, state: dict[str, Any], job: dict[str, Any], error: Exception
    ) -> bool:
        exhausted = self._record_publication_operation_failure_in_memory(
            state, job, error
        )
        self.save(state)
        return exhausted

    def _record_publication_operation_failure_in_memory(
        self, state: dict[str, Any], job: dict[str, Any], error: Exception
    ) -> bool:
        exhausted = record_publication_operation_failure(job, error)
        if exhausted:
            if job.get("phase") in {"publishing", "merging", "merged"}:
                job["execution_resume_phase"] = job["phase"]
            job["phase"] = "publication_pending"
            state.update(
                {
                    "status": "publication_pending",
                    "terminal_kind": "publication_pending",
                    "diagnostics": [
                        publication_pending_diagnostic(
                            subject_key="change_job", subject=self.contract.label
                        )
                    ],
                }
            )
        return exhausted

    def _wait_for_merge_reconciliation(
        self, state: dict[str, Any], job: dict[str, Any], message: str | None = None
    ) -> bool:
        state.update(
            {
                "status": "waiting_external",
                "terminal_kind": "waiting_external",
                "diagnostics": [
                    {
                        "code": "merge_reconciliation_pending",
                        "message": message
                        or "Merge intent reached its retry limit; waiting for GitHub reconciliation",
                        "waiting_for": f"{self.publisher.merge_description(job)} outcome",
                    }
                ],
            }
        )
        self.save(state)
        return True

    def _record_agent_run_status(
        self,
        pr_number: int,
        job: dict[str, Any],
        checks: str,
        *,
        next_action: str | None = None,
    ) -> None:
        record_change_status(self.github, self.contract.label, pr_number, job,
            checks, next_action=next_action)

    def _publication_is_current(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        # Candidate/Review and integration authority are independent of the
        # narrative returned for a previous candidate. Publication validates
        # its own Attempt before using a pending result or creating a commit.
        if self.contract.label.startswith("ticket-"):
            # The original Review/fallback remains bound to the fixed
            # development Candidate. A separate deterministic integration
            # record binds the delivery tree and live target before publish.
            return (
                integration_record_is_valid(self.git, job)
                and not self.adapter.revision_changed(state, job)
            )
        if self.contract.label == "parent-only":
            acceptance = job.get("acceptance_record")
            return (
                parent_integration_record_is_valid(self.git, job)
                and isinstance(acceptance, dict)
                and self.adapter.acceptance_is_current(state, job, acceptance)
                and not self.adapter.revision_changed(state, job)
            )
        if job.get("publication_authority") == "fallback":
            receipt = job.get("fallback_publication_receipt")
            if not isinstance(receipt, dict):
                return False
            candidate = job.get("candidate_sha")
            if not isinstance(candidate, str):
                return False
            try:
                candidate_tree = self.git.resolve(f"{candidate}^{{tree}}")
            except (GitError, ValueError):
                return False
            return (
                receipt.get("base_sha") == job.get("base_sha")
                and receipt.get("candidate_sha") == candidate
                and receipt.get("candidate_tree") == candidate_tree
                and receipt.get("effective_revision") == job.get("effective_revision")
                and self.adapter.base_is_current(
                    self.git.resolve(self.contract.base_branch), job
                )
                and not self.adapter.revision_changed(state, job)
            )
        acceptance = job.get("acceptance_record")
        return (
            isinstance(acceptance, dict)
            and self.adapter.base_is_current(
                self.git.resolve(self.contract.base_branch), job
            )
            and self.adapter.acceptance_is_current(state, job, acceptance)
            and not self.adapter.revision_changed(state, job)
        )

    def _agent_is_current(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        return change_agent_is_current(self.git, self.contract, self.adapter, state, job)

    def _reject_stale(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
        message: str,
    ) -> None:
        reject_stale_change(self.git, self.contract, self.adapter, self.publisher,
            self.state_store, state, job, checkout,
            development_only=self.development_only, message=message)

    def _invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.publisher.invalidate_stale(state, job, checkout)

    def _block(
        self, state: dict[str, Any], job: dict[str, Any], code: str, message: str
    ) -> bool:
        return block_change_job(self.state_store, state, job,
            label=self.contract.label, development_only=self.development_only, code=code, message=message)
