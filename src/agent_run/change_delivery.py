from __future__ import annotations

"""Shared, deterministic delivery lifecycle for a change job.

The controller owns this lifecycle.  A typed job descriptor supplies only
deterministic identity and branch facts; a semantic adapter supplies request
construction and currentness decisions.  Controller/Publisher side effects
and StateStore persistence are kept in their own seam.  In particular,
neither layer gets a shortcut from development to a Run Branch write.
"""

from contextvars import Token
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from agent_run.agents import AgentBackend
from agent_run.delivery_policy import parent_only_budget_policy_for_job
from agent_run.development_review import advance_development_review
from agent_run.integration_development_budget import (
    IntegrationDevelopmentExhausted, ensure_integration_development_available,
)
from agent_run.delivery_step import AgentWorkRequired, DeliveryStep, DeliveryStepKind
from agent_run.change_delivery_integration import prepare_delivery_integration
from agent_run.change_development_review import ChangeDevelopmentReview
from agent_run.ci_recovery_budget import (
    can_start_ci_repair,
    ci_limits_for_job,
    mark_ci_repair,
    refund_ci_repair_without_candidate,
)
from agent_run.artifacts import AcceptanceArtifact
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
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
    release_semantic_attempt,
)
from agent_run.review_budget import (
    ReviewBudgetPolicy,
    can_start_development,
    can_start_review,
    ensure_budget,
    mark_development,
    policy_for_subject,
)

from agent_run.worker_control import worker_control

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
)
from agent_run.change_delivery_development import (
    commit_candidate,
    develop,
    resume_after_initial_credential,
    wait_for_initial_credential as wait_for_initial_credential_stage,
)
from agent_run.change_delivery_fallback import (
    fallback_candidate_is_eligible,
    fallback_publication_context,
    prepare_ticket_fallback,
    previous_publication_authorization,
)
from agent_run.change_delivery_publication_agent import (
    invocation_boundary,
    invocation_events,
    invocation_identity,
    publication,
)
from agent_run.change_delivery_published_head import publish_and_merge
from agent_run.change_delivery_review import (
    complete_review,
    review,
    wait_for_human,
)
from agent_run.change_delivery_state import (
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
from agent_run.state_contract import require_current_run_state
from agent_run.ticket_integration import (
    integration_record_is_valid,
)
from agent_run.ticket_phase import sync_active_ticket_job
from agent_run.ticket_queue import (
    acquire as acquire_ticket_slot,
    has_other_ticket_work,
    reconcile as reconcile_ticket_slot,
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
        self._agent_work_allowed = True
        self._work_token: str | None = None
        self._control_token: Token[Callable[[], None] | None] | None = None
        self._work_admission = None
        if contract.label.startswith("ticket-"):
            from agent_run.ticket_state import TicketState
            from agent_run.ticket_work import TicketWorkAdmission

            store = state_store.states
            self._work_admission = TicketWorkAdmission(
                store.states if isinstance(store, TicketState) else store,
                state_store.run_id,
            )

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
        latest = self.state_store.states.load_current_run(str(state["run_id"]))
        if latest is None:
            raise ValueError("Delivery Run disappeared at Ticket pause boundary")
        return str(job["ticket_number"]) in latest["ticket_pause_requests"]

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.contract.label.startswith("ticket-") and self._current_job is not None:
            if self._current_job.get("phase") == "accepted":
                if self.development_only:
                    from agent_run.ticket_queue import enqueue

                    enqueue(state, self._current_job)
                else:
                    acquire_ticket_slot(state, self._current_job)
            reconcile_ticket_slot(state, self._current_job)
        return self.state_store.save(state)

    def commit_required_checks_observation(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        pr_number: int,
    ) -> dict[str, Any]:
        """Validate and atomically persist one complete Observation commit."""

        sync_fallback_receipt_observation(job, pr_number)
        require_current_run_state(state)
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
        self._agent_work_allowed = allow_agent
        try:
            ensure_budget(job, self.review_budget_policy())
            if self._pause_ticket_if_requested(state, job):
                return self._step_result(state, job)
            self._require_ticket_publication_authorization(job)
            phase = str(job["phase"])
            if phase in {"completed", "blocked", "stale", "publication_pending", "ready_for_approval"}:
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
                progressing = advance_development_review(
                    ChangeDevelopmentReview(self, state, job, checkout), single_step=True
                )
                return self._step_result(state, job, waiting=not progressing)
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
            if self._work_admission is not None and allow_agent and self._agent_work_allowed:
                return DeliveryStep(DeliveryStepKind.WAITING, str(job["phase"]), "work_capacity")
            return DeliveryStep(DeliveryStepKind.NEEDS_AGENT, str(job["phase"]), work.role)
        except _TerminalChangeJob:
            return self._step_result(state, job, waiting=True)
        except GitHubReadError as error:
            if not is_github_convergence_error(error.code):
                raise
            wait_for_github_convergence(
                state, code=error.code, message=str(error), waiting_for=self.contract.label,
            )
            ensure_supervision_window(state)
            self.save(state)
            if self._work_admission is not None:
                self._work_admission.cancel_waiter(int(job["ticket_number"]), int(job["ticket_branch_generation"]))
            return self._step_result(state, job, waiting=True)
        except InitialCredentialUnavailable as error:
            self._refund_unstarted_credential_invocation(state, job)
            self._wait_for_initial_credential(state, job, http_status=error.http_status)
            if self._work_admission is not None:
                self._work_admission.cancel_waiter(int(job["ticket_number"]), int(job["ticket_branch_generation"]))
            return self._step_result(state, job, waiting=True)
        finally:
            self.release_agent_work()
            self._agent_work_allowed = True

    def release_agent_work(self) -> None:
        if self._control_token is not None:
            worker_control.reset(self._control_token)
            self._control_token = None
        if self._work_token is not None:
            assert self._work_admission is not None
            self._work_admission.release(self._work_token)
            self._work_token = None

    def require_agent_work(self, role: str) -> None:
        if not self._agent_work_allowed and self._work_admission is None:
            raise AgentWorkRequired(role)
        if self._work_admission is not None:
            assert self._current_job is not None
            self._work_token = self._work_admission.acquire(
                int(self._current_job["ticket_number"]),
                int(self._current_job["ticket_branch_generation"]), role,
                allow_start=self._agent_work_allowed,
            )
            if self._work_token is None:
                raise AgentWorkRequired(role)
            admission, token = self._work_admission, self._work_token
            self._control_token = worker_control.set(lambda: admission.check_control(token))
        self._agent_work_allowed = False

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

    def _develop(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        attempt_kind = str(job.get("next_attempt_kind", "ordinary"))
        if attempt_kind not in {"ordinary", "ci_repair", "integration"}:
            raise ValueError(f"unknown Development attempt kind: {attempt_kind}")
        if pending_semantic_attempt(job, role="development") is None:
            if attempt_kind == "integration":
                try:
                    ensure_integration_development_available(job, policy_snapshot=job["policy_snapshot"])
                except IntegrationDevelopmentExhausted:
                    self._block(state, job, "integration_development_exhausted",
                        "Parent integration Development allowance is exhausted; resume explicitly")
                    state["terminal_kind"] = "waiting_human"
                    self.save(state)
                    return
            elif attempt_kind == "ci_repair":
                outer_limit, inner_limit = ci_limits_for_job(job)
                if not can_start_ci_repair(
                    job,
                    outer_limit=outer_limit,
                    inner_limit=inner_limit,
                    is_ticket=self.contract.label.startswith("ticket-"),
                ):
                    self._checkpoint_ci_budget(
                        state, job,
                        "ci_development_budget_exhausted" if self.contract.label.startswith("ticket-")
                        else "ci_inner_budget_exhausted",
                    )
                    return
            elif not can_start_development(
                job, self.review_budget_policy(), attempt_kind=attempt_kind
            ):
                self._checkpoint_budget(
                    state,
                    job,
                    "modification_budget_exhausted",
                    "Development budget is exhausted for the current Review Budget Window",
                )
                return
        self.require_agent_work("development")
        develop(self, state, job, checkout)

    def _wait_for_initial_credential(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        http_status: int | None,
    ) -> None:
        wait_for_initial_credential_stage(
            self, state, job, http_status=http_status
        )

    def _resume_after_initial_credential(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        resume_after_initial_credential(self, state, job)

    def _commit_candidate(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> bool:
        return commit_candidate(self, state, job, checkout)

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

    def _invocation_events(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        request: dict[str, Any],
        *,
        role: str = "publication",
        phase: str,
        semantic_attempt: dict[str, Any],
    ) -> Callable[..., None]:
        return invocation_events(
            self,
            state,
            job,
            request,
            role=role,
            phase=phase,
            semantic_attempt=semantic_attempt,
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

    def _review(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.require_agent_work("reviewer")
        review(self, state, job, checkout)

    def _complete_review(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        complete_review(self, state, job, checkout)

    def _wait_for_human(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        phase: str,
        blockers: tuple[str, ...],
        code: str = "agent_requires_human",
    ) -> None:
        wait_for_human(
            self, state, job, phase=phase, blockers=blockers, code=code
        )

    def _publish_and_merge(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> bool:
        return publish_and_merge(self, state, job, checkout)

    def modification_budget_exhausted(self, job: dict[str, Any]) -> bool:
        return not can_start_development(job, self.review_budget_policy())

    def _refund_unstarted_credential_invocation(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        budget = ensure_budget(job, self.review_budget_policy())
        semantic_attempt = pending_semantic_attempt(job)
        if semantic_attempt is None:
            return
        active = state.get("active_agent_invocation")
        bound_attempt = (
            active.get("semantic_attempt") if isinstance(active, dict) else None
        )
        if (
            isinstance(active, dict)
            and active.get("status") == "resuming"
            and isinstance(bound_attempt, dict)
            and bound_attempt.get("attempt_id") == semantic_attempt.get("attempt_id")
        ):
            return
        semantic_role = semantic_attempt.get("role")
        integration_budget = job.get("integration_development_budget")
        if (semantic_role == "development" and isinstance(integration_budget, dict)
            and semantic_attempt["attempt_id"] in integration_budget["attempt_ids"]):
            integration_budget["attempt_ids"].remove(semantic_attempt["attempt_id"])
            integration_budget["used"] -= 1
            job.pop("pending_attempt", None)
            job.pop("pending_attempt_kind", None)
            release_semantic_attempt(job)
            return
        if (
            semantic_role == "development"
            and job.get("phase") in {"developing", "repairing"}
            and isinstance(job.get("pending_attempt"), int)
            and job["pending_attempt"] > int(job.get("modification_attempts", 0))
        ):
            if job.get("pending_attempt_kind") == "ci_repair":
                outer_limit, inner_limit = ci_limits_for_job(job)
                refund_ci_repair_without_candidate(
                    job,
                    outer_limit=outer_limit,
                    inner_limit=inner_limit,
                    is_ticket=self.contract.label.startswith("ticket-"),
                )
            elif budget["development_attempts"] > 0:
                budget["development_attempts"] -= 1
            job.pop("pending_attempt", None)
            job.pop("pending_attempt_kind", None)
            release_semantic_attempt(job)
        elif semantic_role == "reviewer" and job.get("phase") == "reviewing":
            job["validation_attempts"] = max(
                0, int(job.get("validation_attempts", 0)) - 1
            )
            job["phase"] = "candidate"
            release_semantic_attempt(job)
        elif semantic_role == "publication" and job.get("phase") == "accepted":
            job["publication_attempts"] = max(
                0, int(job.get("publication_attempts", 0)) - 1
            )
            release_semantic_attempt(job)
            job.pop("publication_operation_retry", None)
            job.pop("last_publication_error", None)

    def review_budget_policy(self) -> ReviewBudgetPolicy:
        if self.contract.label == "parent-only" and self._current_job is not None:
            return parent_only_budget_policy_for_job(self._current_job)
        return self.contract.review_budget_policy or policy_for_subject(
            self.contract.label
        )

    def review_budget_exhausted_for_review(self, job: dict[str, Any]) -> bool:
        if job.get("attempt_kind") == "ci_repair":
            return False
        return not can_start_review(job, self.review_budget_policy())

    def mark_development_attempt(
        self, job: dict[str, Any], *, attempt_kind: str
    ) -> int:
        if attempt_kind == "ci_repair":
            outer_limit, inner_limit = ci_limits_for_job(job)
            return mark_ci_repair(
                job,
                outer_limit=outer_limit,
                inner_limit=inner_limit,
                is_ticket=self.contract.label.startswith("ticket-"),
            )
        return mark_development(
            job, self.review_budget_policy(), attempt_kind=attempt_kind
        )

    def _checkpoint_ci_budget(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        job.update({"phase": "escalating", "escalation_code": code})
        job["ci_recovery"]["checkpoint_reason"] = code
        self.save(state)

    def _prepare_ticket_ci_publication_authorization(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        from agent_run.change_delivery_fallback import (
            prepare_ticket_ci_publication_authorization,
        )

        return prepare_ticket_ci_publication_authorization(self, state, job)

    def _checkpoint_budget(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        code: str,
        message: str,
    ) -> None:
        job.update({"phase": "escalating", "escalation_code": code})
        budget = ensure_budget(job, self.review_budget_policy())
        budget["checkpoint_reason"] = code
        self.save(state)

    @staticmethod
    def _previous_publication_authorization(
        job: dict[str, Any],
    ) -> dict[str, Any] | None:
        return previous_publication_authorization(job)

    def _prepare_ticket_fallback(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        return prepare_ticket_fallback(self, state, job)

    def _fallback_candidate_is_eligible(self, job: dict[str, Any]) -> bool:
        return fallback_candidate_is_eligible(job, self.review_budget_policy())

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
            if self.contract.label.startswith("ticket-") and job.get("phase") in {"merging", "merged"}:
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
        self.github.record_agent_run_status(
            pr_number,
            {
                "scope": self.contract.label,
                "base_sha": str(job["base_sha"]),
                "candidate_sha": str(job["candidate_sha"]),
                "validation_outcome": validation_outcome,
                "lane_statuses": lane_statuses,
                "required_checks": checks,
                "next_action": next_action,
            },
        )

    def _publication_is_current(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
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
        # Ticket Development and its original Review intentionally use their
        # fixed checkout. Remote source/target additions belong to the later
        # integration boundary, never to an in-flight semantic attempt.
        if self.contract.label.startswith("ticket-") or self.contract.label == "parent-only":
            return not self.adapter.revision_changed(state, job)
        current_base = self.git.resolve(self.contract.base_branch)
        return self.adapter.base_is_current(
            current_base, job
        ) and not self.adapter.revision_changed(state, job)

    def _reject_stale(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
        message: str,
    ) -> None:
        if not self._agent_is_current(state, job):
            pending_attempt = pending_semantic_attempt(job)
            if pending_attempt is not None:
                close_semantic_attempt(
                    job, pending_attempt, outcome="currentness_invalidated"
                )
                detach_active_invocation(state, pending_attempt)
            if self.adapter.stale_disposition is StaleDisposition.FRESH_RUN_ACCEPTANCE:
                self._invalidate_stale(state, job, checkout)
                self.save(state)
                raise _TerminalChangeJob()
            self._block(state, job, "effective_revision_mismatch", message)
            raise _TerminalChangeJob()

    def _invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.publisher.invalidate_stale(state, job, checkout)

    def _block(
        self, state: dict[str, Any], job: dict[str, Any], code: str, message: str
    ) -> bool:
        job.update({"phase": "blocked", "blocked_reason": code})
        state["status"] = "blocked"
        state["diagnostics"] = [
            {"code": code, "message": message, "change_job": self.contract.label}
        ]
        self.save(state)
        return False

    def _sync_attempts(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        self.publisher.sync_attempts(state, job)


class _TerminalChangeJob(Exception):
    pass
