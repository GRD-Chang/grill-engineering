from __future__ import annotations

"""Development, Candidate and Review work for the existing delivery objects.

The scoped store and durable Job remain authoritative. This module owns role
allocation, allowances, invocation and returned-result consumption; delivery
callers retain integration, publication, approval and Repair promotion.
"""

import json
from copy import deepcopy
from enum import Enum
from pathlib import Path
import sys
from typing import Any, Literal, cast
from uuid import uuid4

from agent_run.agent_execution import execute_agent_work, select_development_thread
from agent_run.agent_invocation import fail_interrupted_invocation
from agent_run.agent_return import restore_agent_return
from agent_run.agent_work import AgentWorkScope
from agent_run.agents import AgentBackend, DevelopmentResult, HumanBlockerResult, ReviewResult
from agent_run.artifacts import (
    AcceptanceArtifact,
    clear_current_human_blocker,
    record_human_blocker,
)
from agent_run.change_delivery_contracts import ChangeJobContract, ChangeJobStopped
from agent_run.change_delivery_fallback import preserve_required_checks_publication_authorization
from agent_run.change_delivery_review import record_change_status
from agent_run.change_delivery_publication_agent import invocation_boundary
from agent_run.change_delivery_state import (
    ChangeDeliveryStateStore,
    save_change_job,
    block_change_job,
    require_mapping as _mapping,
)
from agent_run.change_delivery_threads import (
    record_development_thread as _record_development_thread,
    record_reviewer as _record_reviewer,
    require_development_thread,
    latest_reviewer_thread,
)
from agent_run.ci_recovery_budget import (
    checkpoint_after_reviewer_failure,
    ci_limit_for_job,
    ci_repair_budget_context,
    ci_reviewer_budget_context,
    ci_review_required,
    can_start_ci_repair,
    mark_ci_repair,
    refund_ci_repair_without_candidate,
    observe_repairable_ci_failure,
)
from agent_run.credential_availability import (
    clear_initial_credential_wait,
    resume_initial_credential_wait,
    wait_for_initial_credential,
)
from agent_run.credential_lifecycle import InitialCredentialUnavailable
from agent_run.delivery_budget import delivery_budget_owner
from agent_run.delivery_policy import (
    change_review_budget_policy,
    ticket_budget_policy_for_job,
    parent_only_budget_policy_for_job,
    run_repair_budget_policy_for_job,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.delivery_step import AgentWorkRequired, DeliveryStep, DeliveryStepKind
from agent_run.external_supervision import (
    ensure_supervision_window,
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.git import GitRepository, MergeConflictError
from agent_run.git_errors import GitError, GitIntegrityError
from agent_run.github import GitHubReadError
from agent_run.human_responses import current_human_response_history
from agent_run.integration_development_budget import (
    IntegrationDevelopmentExhausted,
    ensure_integration_development_available,
    acquire_integration_development,
)
from agent_run.integration_record_contract import require_completed_ticket_integration_records
from agent_run.required_checks_observation import clear_required_checks_observation
from agent_run.review_attempt import (
    begin_review,
    consume_review_result,
    finish_review_result,
    remember_review,
    review_ordinal,
)
from agent_run.review_budget import (
    ReviewBudgetPolicy,
    can_start_development,
    can_start_review,
    ensure_budget,
    mark_development,
    repair_budget_context,
    reviewer_budget_context,
    previous_review_context,
)
from agent_run.review_context import (
    prepare_review_context,
    review_context_for_result,
    review_record_identity,
    run_acceptance_record,
    review_context_is_current,
    candidate_review_identity,
)
from agent_run.review_evidence import (
    review_record_matches,
    run_review_identity,
    accepted_review_matches,
)
from agent_run.run_candidate_acceptance import CandidateRunAcceptance
from agent_run.run_currentness import (
    RunCurrentnessReader,
    refresh_run_currentness,
    run_currentness_boundary,
    ticket_completion_records,
    ticket_fallback_records,
    ticket_ci_repair_records,
    ticket_integration_records,
)
from agent_run.run_repair_currentness import RunRepairCurrentness
from agent_run.run_thread_identity import prior_thread_identities
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
    release_semantic_attempt,
    require_controller_reprepare_intent,
)
from agent_run.state import StateStore

_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT = "run-acceptance"
ChangeScope = Literal["ticket", "parent-only", "repair"]

class ReviewDecision(str, Enum):
    ACCEPT = "accepted"
    HUMAN = "reviewer_requires_human"
    REPAIR = "repairing"
    REVIEW_EXHAUSTED = "review_budget_exhausted"
    DEVELOPMENT_EXHAUSTED = "modification_budget_exhausted"
    CI_EXHAUSTED = "ci_development_budget_exhausted"


def review_is_due(
    job: dict[str, Any], policy: ReviewBudgetPolicy,
    *, budget_owner: dict[str, Any] | None = None,
) -> bool:
    """An interrupted semantic review may finish even at the window ceiling."""
    owner = budget_owner if budget_owner is not None else job
    return (
        pending_semantic_attempt(job, role="reviewer") is not None
        or job.get("parent_integration_stage") is True
        or job.get("attempt_kind") == "ci_repair"
        or can_start_review(owner, policy)
    )


def after_review(
    job: dict[str, Any], policy: ReviewBudgetPolicy, artifact: AcceptanceArtifact,
    *, budget_owner: dict[str, Any] | None = None,
) -> ReviewDecision:
    """Finite outcome rules shared by demand and full Parent acceptance.

    A finding never authorizes fallback. Only a later eligible Ticket Candidate
    can obtain that authority at the pre-review gate. CI keeps its own window.
    """
    owner = budget_owner if budget_owner is not None else job
    if artifact.is_accepted:
        return ReviewDecision.ACCEPT
    if artifact.requires_human:
        return ReviewDecision.HUMAN
    if job.get("parent_integration_stage") is True and job.get("attempt_kind") != "ci_repair":
        return ReviewDecision.REPAIR
    if job.get("attempt_kind") == "ci_repair":
        limit = ci_limit_for_job(job)
        if checkpoint_after_reviewer_failure(
            owner, limit=limit,
            is_ticket=policy.budget_scope == "ticket",
        ):
            return ReviewDecision.CI_EXHAUSTED
        return ReviewDecision.REPAIR
    if not policy.fallback and not can_start_review(owner, policy):
        return ReviewDecision.REVIEW_EXHAUSTED
    if not can_start_development(owner, policy):
        return ReviewDecision.DEVELOPMENT_EXHAUSTED
    return ReviewDecision.REPAIR


class DevelopmentReview:
    def __init__(
        self, *, git: GitRepository, states: StateStore, agents: AgentBackend,
        github: GitHubPublisher | None = None,
        currentness_reader: RunCurrentnessReader | None = None,
        default_head_sha: str | None = None,
        development_only: bool = False,
    ) -> None:
        self.git, self.states, self.agents, self.github = git, states, agents, github
        self.currentness_reader = currentness_reader
        self.default_head_sha = default_head_sha
        self.development_only = development_only
        self.candidate_acceptance = CandidateRunAcceptance(git, default_head_sha=default_head_sha)
        self.repair_currentness = RunRepairCurrentness(git, github, states) if github is not None else None
        from agent_run.ticket_state import TicketState
        from agent_run.ticket_work import TicketWorkAdmission
        admission = None
        if isinstance(states, TicketState):
            admission = TicketWorkAdmission(states.states, states.run_id)
        self.agent_work = AgentWorkScope(admission)
        self._scope: str = "run"
        self._current_job: dict[str, Any] | None = None

    def _bind_change(self, state: dict[str, Any], job: dict[str, Any], scope: ChangeScope) -> None:
        self._scope, self._current_job = scope, job
        run_id = str(state["run_id"])
        self.state_store = ChangeDeliveryStateStore(self.states, run_id)
        if scope == "ticket":
            from agent_run.ticket_state import TicketState
            from agent_run.ticket_work import TicketWorkAdmission
            assert self.github is not None
            self.contract = ChangeJobContract(
                label=f"ticket-{job['ticket_number']}",
                branch=str(job["ticket_branch"]),
                base_branch=str(state["run_branch"]),
                review_budget_policy=ticket_budget_policy_for_job(
                    job,
                    state_snapshot=state.get("policy_snapshot"),
                ),
            )
            if self.agent_work.admission is None:
                self.agent_work.admission = TicketWorkAdmission(
                    self.states.states if isinstance(self.states, TicketState) else self.states,
                    run_id,
                )
        elif scope == "parent-only":
            assert self.github is not None
            self.contract = ChangeJobContract(
                label="parent-only",
                branch=str(job["parent_branch"]),
                base_branch=str(state["base"]["branch"]),
                review_budget_policy=parent_only_budget_policy_for_job(
                    job,
                    state_snapshot=state.get("policy_snapshot"),
                ),
            )
        else:
            self.contract = ChangeJobContract(
                label=f"run-repair-{job['repair_attempt']}",
                branch=str(job["repair_branch"]),
                base_branch=str(state["run_branch"]),
                review_budget_policy=run_repair_budget_policy_for_job(
                    job,
                    state_snapshot=state.get("policy_snapshot"),
                ),
            )

    def integrate_repair_candidate(self, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> bool:
        from agent_run.run_repair_integration import needs_repair_integration, prepare_repair_integration
        self._bind_change(state, job, "repair")
        with self.agent_work.step(job, allow_agent=True):
            if not needs_repair_integration(self, job):
                return True
            return prepare_repair_integration(self, state, job, checkout)

    def integrate_parent_candidate(self, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> bool:
        from agent_run.parent_only_integration import prepare_parent_integration
        self._bind_change(state, job, "parent-only")
        with self.agent_work.step(job, allow_agent=True):
            return prepare_parent_integration(self, state, job, checkout)

    def resolve_ticket_integration(self, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> bool:
        from agent_run.integration_development import resolve_integration_work
        self._bind_change(state, job, "ticket")
        with self.agent_work.step(job, allow_agent=True):
            return resolve_integration_work(self, state, job, checkout)

    def wait_for_change_credential(
        self, state: dict[str, Any], job: dict[str, Any], *,
        scope: ChangeScope, http_status: int | None,
    ) -> None:
        self._bind_change(state, job, scope)
        self._refund_unstarted_credential_invocation(state, job)
        self._wait_for_initial_credential(state, job, http_status=http_status)
        self.agent_work.defer()

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        if self._scope == "run":
            self.states.save_run(str(state["run_id"]), state)
            return state
        return save_change_job(
            self.state_store,
            state,
            self._current_job,
            label=self.contract.label,
            development_only=self._scope == "repair" or self.development_only,
        )

    def review_budget_policy(self) -> ReviewBudgetPolicy:
        return change_review_budget_policy(
            self.contract.label,
            self.contract.review_budget_policy,
            self._current_job,
        )

    def _run_budget_policy(self, state: dict[str, Any], run: dict[str, Any]) -> ReviewBudgetPolicy:
        return run_repair_budget_policy_for_job(run, state_snapshot=state.get("policy_snapshot"))

    def _default_head(self, state: dict[str, Any]) -> str:
        return self.default_head_sha or str(state["base"]["sha"])

    def _refresh_run_currentness(self, state: dict[str, Any]) -> bool:
        require_completed_ticket_integration_records(state)
        if self.currentness_reader is None:
            return True
        default_head = refresh_run_currentness(state, reader=self.currentness_reader, git=self.git)
        if default_head is None:
            return False
        self.default_head_sha = default_head
        self.candidate_acceptance.update_default_head(default_head)
        return True

    def _agent_is_current(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        if self._scope != "repair":
            return not change_revision_changed(self.github, state, job)
        from agent_run.run_repair_integration import repair_run_branch_is_current
        if not repair_run_branch_is_current(self.git, job, self.git.resolve(str(state["run_branch"]))):
            return False
        if not self._refresh_run_currentness(state):
            return False
        default = self._default_head(state)
        assert self.repair_currentness is not None
        return (default == job.get("default_base_sha")
                and not self.repair_currentness.non_default_revision_changed(
                    state,
                    job,
                    default_head_sha=default,
                ))

    def _reject_stale(self, state: dict[str, Any], job: dict[str, Any], checkout: Path, message: str) -> None:
        if review_context_is_current(job) and self._agent_is_current(state, job):
            return
        attempt = pending_semantic_attempt(job)
        if attempt is not None:
            close_semantic_attempt(job, attempt, outcome="currentness_invalidated")
            detach_active_invocation(state, attempt)
        if self._scope == "repair":
            from agent_run.run_repair_currentness import RunRepairWorkStale
            raise RunRepairWorkStale()
        self._block(state, job, "effective_revision_mismatch", message)
        raise ChangeJobStopped()

    def _block(self, state: dict[str, Any], job: dict[str, Any], code: str, message: str) -> bool:
        return block_change_job(
            self.state_store,
            state,
            job,
            label=self.contract.label,
            development_only=self._scope == "repair" or self.development_only,
            code=code,
            message=message,
        )

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
            state,
            job,
            resume_phase=phase,
            blocked_phase="blocked",
            blockers=blockers,
            code=code,
        )
        self.save(state)



    def require_agent_work(self, role: str) -> None:
        self.agent_work.require(role)

    def release_agent_work(self) -> None:
        self.agent_work.release(error=sys.exception())

    def _invocation_boundary(self, job: dict[str, Any]) -> dict[str, Any]:
        candidate = job.get("candidate_sha")
        tree = self.git.resolve(f"{candidate}^{{tree}}") if isinstance(candidate, str) else None
        return invocation_boundary(job, candidate_tree=tree)

    def _sync_attempts(self, state: dict[str, Any], job: dict[str, Any]) -> None:
        if self._scope == "repair":
            from agent_run.run_repair_cycle import sync_repair_cycle_counters
            sync_repair_cycle_counters(state["run_acceptance"], job)

    def _identity(self, state: dict[str, Any], job: dict[str, Any]) -> tuple[str, int]:
        if self._scope == "repair":
            return f"run-repair:{state['run_id']}", int(job["repair_generation"])
        if self._scope == "ticket":
            return f"ticket:{job['ticket_number']}", int(job.get("ticket_branch_generation", 1))
        return f"parent-only:{state['run_id']}", int(job.get("parent_generation", 1))

    def _development_request(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any]:
        if self._scope == "repair":
            from agent_run.run_repair_requests import RunRepairRequests
            assert self.github is not None
            return RunRepairRequests(self.git, self.github).development(state, job, checkout)
        request = self._change_request(state, job, checkout)
        request.update(
            head_sha=self.git.checkout_head(checkout),
            thread_id=None if job.get("development_new_thread") else job.get("development_thread_id"),
        )
        if job.get("development_summary"):
            request["development_summary"] = str(job["development_summary"])
        source = job.get("repair_source")
        if self._scope == "parent-only" and job.get(
            "pending_attempt_kind",
            job.get("next_attempt_kind"),
        ) == "integration" and source == "acceptance":
            request.update(
                repair_source="integration",
                work_source="integration",
                integration_evidence=json.dumps(
                    {
                        "acceptance_artifact": _mapping(job, "acceptance_artifact"),
                        "parent_integration": _mapping(job, "parent_integration"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
            )
        elif source in {"git_integrity", "acceptance", "required_checks"}:
            evidence = {
                "git_integrity": "git_integrity_evidence",
                "acceptance": "acceptance_artifact",
                "required_checks": "ci_evidence",
            }[source]
            request.update(repair_source=source, **{evidence: _mapping(job, evidence)})
        return request

    def _change_request(self, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> dict[str, Any]:
        parent = dict(_mapping(state, "parent"))
        parent["url"] = f"https://github.com/{state['repository']}/issues/{parent['number']}"
        request = {
            "acceptance_scope": "ticket" if self._scope == "ticket" else "parent_only",
            "parent_issue_url": parent["url"],
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": parent,
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "checkout": str(checkout),
        }
        if self._scope == "ticket":
            ticket = dict(state["ticket_graph"]["tickets"][str(job["ticket_number"])])
            ticket["url"] = f"https://github.com/{state['repository']}/issues/{job['ticket_number']}"
            request.update(ticket=ticket, task_issue_url=ticket["url"])
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(job, generation=self._identity(state, job)[1]):
            request["human_response_history"] = history
        return request

    def _change_review_request(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any]:
        request = self._change_request(state, job, checkout)
        identity = (
            candidate_review_identity(job, self.git)
            if self._scope == "ticket"
            else parent_review_identity(self.git, state, job)
        )
        resumed = (
            job.get("review_human_blocker_resume") or job.get("review_resume_thread_id")
        ) and not job.get("review_new_thread")
        request.update(
            candidate_sha=job["candidate_sha"],
            current_review_identity=identity,
            thread_id=latest_reviewer_thread(job) if resumed else None,
        )
        if self._scope == "ticket":
            previous = previous_review_context(job)
            if previous is not None:
                request.update(
                    previous_acceptance_artifact=previous["artifact"],
                    previous_review_identity=previous["identity"],
                )
        else:
            previous = self._compatible_parent_baseline(state, job)
            baseline = self._compatible_parent_baseline(state, job, accepted_only=True)
            if baseline is not None:
                request["parent_acceptance_baseline"] = baseline
            if previous is not None:
                request.update(
                    previous_acceptance_artifact=previous["artifact"],
                    previous_review_identity={key: previous[key] for key in identity},
                )
            if isinstance(job.get("parent_integration"), dict):
                request.update(work_source="integration", integration_record=job["parent_integration"])
        return request

    def _compatible_parent_baseline(
        self, state: dict[str, Any], job: dict[str, Any], *, accepted_only: bool = False
    ) -> dict[str, Any] | None:
        for key in ("acceptance_record", "previous_parent_acceptance_record"):
            record = job.get(key)
            if not isinstance(record, dict) or (
                record.get("acceptance_scope") != "parent_only"
                or record.get("parent_number") != _mapping(state, "parent")["number"]
                or record.get("effective_revision") != job.get("effective_revision")
                or change_revision_changed(self.github, state, job)
            ):
                continue
            identity_fields = (
                "reviewed_base_sha", "reviewed_candidate_sha",
                "reviewed_candidate_tree", "expected_merge_tree", "reviewer_thread_id",
            )
            if not all(
                isinstance(record.get(field), str) and record[field]
                for field in identity_fields
            ):
                continue
            try:
                artifact = AcceptanceArtifact.parse(record.get("artifact"))
            except ValueError:
                continue
            if accepted_only and not artifact.is_accepted:
                continue
            if not any(
                check["status"] == "blocked" for check in artifact.checks.values()
            ):
                return record
        return None

    def _acceptance_is_current(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        acceptance: dict[str, Any],
    ) -> bool:
        if self._scope == "repair":
            return self.candidate_acceptance.is_current(
                state,
                job,
                acceptance,
            ) and self._agent_is_current(state, job)
        if change_revision_changed(self.github, state, job):
            return False
        try:
            identity = ({
                "acceptance_scope": "change_job",
                **candidate_review_identity(job, self.git),
                "effective_revision": job.get("effective_revision"),
            } if self._scope == "ticket" else parent_review_identity(self.git, state, job))
            return accepted_review_matches(acceptance, identity, job.get("acceptance_artifact"))
        except (GitError, ValueError, KeyError):
            return False

    def _thread_allowed(self, state: dict[str, Any], thread_id: str) -> bool:
        return self._scope != "repair" or thread_id not in prior_thread_identities(
            state,
            state["run_acceptance"],
        )

    def _commit_work_candidate(self, checkout: Path, job: dict[str, Any], attempt: int) -> str | None:
        if self._scope == "repair":
            from agent_run.run_repair_delivery import commit_repair_candidate
            return commit_repair_candidate(self.git, checkout, job, attempt)
        return self.git.commit_candidate(
            checkout,
            ticket_number=int(job["ticket_number"]) if self._scope == "ticket" else 0,
            attempt=attempt,
            expected_head=str(job.get("managed_checkout_head") or job.get("candidate_sha") or job["base_sha"]),
            candidate_intent=job.get("candidate_commit_intent") if isinstance(
                job.get("candidate_commit_intent"),
                dict,
            ) else None,
        )

    def _resume_after_initial_credential(self, state: dict[str, Any], job: dict[str, Any]) -> None:
        if resume_initial_credential_wait(state, work_subject=self.contract.label):
            self.save(state)

    def _wait_for_initial_credential(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        http_status: int | None,
    ) -> None:
        wait_for_initial_credential(
            state,
            work_subject=self.contract.label,
            phase=str(job["phase"]),
            resume_status=str(state.get("status", "active")),
            http_status=http_status,
        )
        ensure_supervision_window(state)
        self.save(state)

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
                limit = ci_limit_for_job(job)
                if not can_start_ci_repair(
                    delivery_budget_owner(state, job),
                    limit=limit,
                    is_ticket=self.contract.label.startswith("ticket-"),
                ):
                    self._checkpoint_ci_budget(
                        state, job,
                        "ci_development_budget_exhausted",
                    )
                    return
            elif not can_start_development(
                delivery_budget_owner(state, job), self.review_budget_policy(), attempt_kind=attempt_kind
            ):
                self._checkpoint_budget(
                    state,
                    job,
                    "modification_budget_exhausted",
                    "Development budget is exhausted for the current Review Budget Window",
                )
                return
        self.require_agent_work("development")
        self._develop_work(state, job, checkout)

    def mark_development_attempt(
        self, state: dict[str, Any], job: dict[str, Any], *, attempt_kind: str
    ) -> int:
        if attempt_kind == "ci_repair":
            limit = ci_limit_for_job(job)
            return mark_ci_repair(
                delivery_budget_owner(state, job),
                limit=limit,
                is_ticket=self.contract.label.startswith("ticket-"),
            )
        return mark_development(
            delivery_budget_owner(state, job), self.review_budget_policy(), attempt_kind=attempt_kind
        )

    def _checkpoint_ci_budget(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        job.update({"phase": "escalating", "escalation_code": code})
        delivery_budget_owner(state, job)["ci_recovery"]["checkpoint_reason"] = code
        self.save(state)

    def _checkpoint_budget(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        code: str,
        message: str,
    ) -> None:
        job.update({"phase": "escalating", "escalation_code": code})
        budget = ensure_budget(delivery_budget_owner(state, job), self.review_budget_policy())
        budget["checkpoint_reason"] = code
        self.save(state)

    def _refund_unstarted_credential_invocation(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        budget = ensure_budget(delivery_budget_owner(state, job), self.review_budget_policy())
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
                limit = ci_limit_for_job(job)
                refund_ci_repair_without_candidate(
                    delivery_budget_owner(state, job),
                    limit=limit,
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

    def _develop_work(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> None:
        self._resume_after_initial_credential(state, job)
        self._reject_stale(
            state,
            job,
            checkout,
            "Development did not start after requirements changed",
        )
        pending = job.get("pending_attempt")
        attempt_kind = str(job.get("next_attempt_kind", "ordinary"))
        attempt = (
            pending
            if isinstance(pending, int)
            and pending > int(job.get("modification_attempts", 0))
            else int(job.get("modification_attempts", 0)) + 1
        )
        pending_resume = (
            isinstance(pending, int)
            and pending > int(job.get("modification_attempts", 0))
        )
        if not pending_resume and attempt_kind != "integration":
            self.mark_development_attempt(state, job, attempt_kind=attempt_kind)
        # Persist the actual Worker attempt before invoking Codex.  This
        # makes a foreground status query truthful even while Codex is still
        # running, and leaves an observable recovery boundary on interruption.
        job["pending_attempt"] = attempt
        job["pending_attempt_kind"] = attempt_kind
        if pending_resume:
            if not isinstance(job.get("managed_checkout_head"), str):
                raise ValueError(
                    "incompatible_run_state: pending Development is missing its managed checkout head"
                )
        else:
            job["managed_checkout_head"] = self.git.checkout_head(checkout)
        work_subject, generation = self._identity(state, job)
        owner = delivery_budget_owner(state, job)
        budget = owner.get("integration_development_budget" if attempt_kind == "integration" else "review_budget")
        if not isinstance(budget, dict) or type(budget.get("window")) is not int:
            raise ValueError("Development is missing its Review Budget Window")
        new_semantic_attempt = pending_semantic_attempt(job, role="development") is None
        semantic_attempt = allocate_semantic_attempt(
            job,
            role="development",
            work_subject=work_subject,
            generation=generation,
            currentness_boundary=self._invocation_boundary(job),
            ordinal=attempt,
            budget_owner=owner,
            budget_window=int(
                delivery_budget_owner(state, job)["ci_recovery"]["window"]
                if attempt_kind == "ci_repair"
                else budget["window"]
            ),
        )
        if attempt_kind == "integration":
            acquire_integration_development(job, attempt_id=semantic_attempt["attempt_id"],
                policy_snapshot=job["policy_snapshot"])
        select_development_thread(state, job, new_attempt=new_semantic_attempt)
        intent = job.get("candidate_commit_intent")
        if not (
            isinstance(intent, dict)
            and intent.get("expected_head") == job["managed_checkout_head"]
            and intent.get("attempt") == attempt
            and isinstance(intent.get("token"), str)
            and intent.get("token")
        ):
            job["candidate_commit_intent"] = {
                "kind": "candidate_commit",
                "expected_head": str(job["managed_checkout_head"]),
                "attempt": attempt,
                "token": uuid4().hex,
            }
        self.save(state)
        request = self._development_request(state, job, checkout)
        if attempt_kind == "ci_repair":
            request["attempt_kind"] = "ci_repair"
            request["work_source"] = "ci"
            request["ci_evidence"] = job.get("ci_evidence")
            request["ci_failure_context"] = job.get("required_checks_origin")
        if request.get("repair_source") is not None and attempt_kind != "integration":
            if attempt_kind == "ci_repair" and (
                not self.contract.label.startswith("ticket-")
                or delivery_budget_owner(state, job)["ci_recovery"]["review_required"]
            ):
                limit = ci_limit_for_job(job)
                request["review_budget_context"] = ci_repair_budget_context(
                    delivery_budget_owner(state, job),
                    limit=limit,
                    is_ticket=self.contract.label.startswith("ticket-"),
                )
            elif attempt_kind != "ci_repair":
                request["review_budget_context"] = repair_budget_context(
                    delivery_budget_owner(state, job), self.review_budget_policy()
                )
        try:
            result = cast(
                DevelopmentResult | HumanBlockerResult,
                execute_agent_work(
                    state, job, request, semantic_attempt,
                    agents=self.agents, save=self.save, phase=str(job["phase"]),
                    boundary=self._invocation_boundary(job),
                    is_current=lambda: self._agent_is_current(state, job),
                ),
            )
        finally:
            self.release_agent_work()
        require_development_thread(
            job, result.thread_id, requested_thread=request.get("thread_id")
        )
        clear_initial_credential_wait(state, work_subject=self.contract.label)
        if not self._thread_allowed(state, result.thread_id):
            raise ValueError("Change Job Development Thread is not independent")
        _record_development_thread(job, result.thread_id, result.replaced_thread_id)
        job.pop("development_new_thread", None)
        job.pop("development_failure_resume", None)
        if isinstance(result, HumanBlockerResult):
            self._wait_for_human(
                state, job, phase=str(job["phase"]), blockers=result.human_blockers
            )
            return
        if not result.thread_id.strip() or not result.summary.strip():
            raise ValueError("Development result is incomplete")
        job["development_summary"] = result.summary
        clear_current_human_blocker(job)
        job["phase"] = "committing_candidate"
        self.save(state)
        self._reject_stale(
            state,
            job,
            checkout,
            "Development result was discarded after requirements changed",
        )

    def _commit_candidate(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> bool:
        attempt = int(job["pending_attempt"])
        controller_reprepared = "controller_candidate_reprepare" in job
        if controller_reprepared:
            require_controller_reprepare_intent(job)
        expected_head = str(
            job.get("managed_checkout_head")
            or job.get("candidate_sha")
            or job.get("base_sha")
        )
        intent = job.get("candidate_commit_intent")
        if not (
            isinstance(intent, dict)
            and intent.get("expected_head") == expected_head
            and intent.get("attempt") == attempt
            and isinstance(intent.get("token"), str)
            and intent.get("token")
        ):
            intent = {
                "kind": "candidate_commit",
                "expected_head": expected_head,
                "attempt": attempt,
                "token": uuid4().hex,
            }
            job["candidate_commit_intent"] = intent
            self.save(state)
        try:
            candidate = self._commit_work_candidate(checkout, job, attempt)
        except GitIntegrityError as error:
            evidence = dict(error.evidence)
            evidence.update(
                {
                    "expected_head": str(
                        expected_head
                    ),
                    "base_sha": str(job.get("base_sha", "")),
                    "previous_candidate_sha": str(job.get("candidate_sha", "")),
                }
            )
            return self._route_git_integrity_repair(state, job, checkout, evidence)
        if candidate is None:
            observed_head = self.git.checkout_head(checkout)
            if observed_head != expected_head:
                evidence = {
                    "kind": "git_integrity",
                    "status": "fail",
                    "reason": "clean checkout head differs from the managed Candidate boundary",
                    "expected_head": expected_head,
                    "observed_head": observed_head,
                    "base_sha": str(job.get("base_sha", "")),
                    "previous_candidate_sha": str(job.get("candidate_sha", "")),
                    "workspace_clean": "true",
                }
                return self._route_git_integrity_repair(state, job, checkout, evidence)
            semantic_attempt = pending_semantic_attempt(job, role="development")
            if semantic_attempt is None and not controller_reprepared:
                raise ValueError("Development closeout is missing its Semantic Attempt")
            if semantic_attempt is not None:
                close_semantic_attempt(
                    job,
                    semantic_attempt,
                    outcome="no_code_changes",
                    result=_development_attempt_result(job),
                )
            if job.get("pending_attempt_kind") == "ci_repair":
                job["modification_attempts"] = attempt
                limit = ci_limit_for_job(job)
                refund_ci_repair_without_candidate(
                    delivery_budget_owner(state, job),
                    limit=limit,
                    is_ticket=self.contract.label.startswith("ticket-"),
                )
            ticket_ci = job.get("pending_attempt_kind") == "ci_repair"
            job.pop("controller_candidate_reprepare", None)
            job.pop("pending_attempt", None)
            job.pop("pending_attempt_kind", None)
            job.pop("candidate_commit_intent", None)
            if ticket_ci:
                # The completed investigation stays charged. Explicit resume starts
                # new CI work, never reopens the closed semantic attempt.
                job["attempt_kind"] = "ci_repair"
                job["next_attempt_kind"] = "ci_repair"
                decision = observe_repairable_ci_failure(
                    delivery_budget_owner(
                        state,
                        job,
                    ), head_sha=delivery_budget_owner(state, job)["ci_recovery"]["failure_head"],
                    limit=limit, is_ticket=self.contract.label.startswith("ticket-"),
                )
                if decision != "exhausted":
                    job["phase"] = "repairing"
                    job["pause_resume_phase"] = "repairing"
                return self._block(
                    state, job,
                    "ci_development_budget_exhausted" if decision == "exhausted" else "ci_investigation_paused",
                    "CI investigation produced no code changes; explicit resume is required",
                )
            return self._block(
                state,
                job,
                "no_code_changes",
                "Development Attempt produced no code changes",
            )
        preserve_required_checks_publication_authorization(job)
        clear_required_checks_observation(job)
        job.update(
            {
                "candidate_sha": candidate,
                "modification_attempts": attempt,
                "code_modification_attempts": attempt,
                "attempt_kind": str(job.pop("pending_attempt_kind", "ordinary")),
                "phase": "candidate",
            }
        )
        job.pop("next_attempt_kind", None)
        if job["attempt_kind"] == "integration":
            # Integration findings go back to Git checks before any next review.
            job["phase"] = "accepted"
            if isinstance(
                job.get("acceptance_record"),
                dict,
            ) and AcceptanceArtifact.parse(job["acceptance_record"].get("artifact")).is_accepted:
                job["previous_parent_acceptance_record"] = job["acceptance_record"]
            job.pop("acceptance_record", None)
            job.pop("approval_grant", None)
        job.pop("candidate_commit_intent", None)
        self._sync_attempts(state, job)
        semantic_attempt = pending_semantic_attempt(job, role="development")
        if semantic_attempt is None and not controller_reprepared:
            raise ValueError("Candidate closeout is missing its Semantic Attempt")
        if semantic_attempt is not None:
            close_semantic_attempt(
                job,
                semantic_attempt,
                outcome="candidate",
                result=_development_attempt_result(job),
            )
        job.pop("controller_candidate_reprepare", None)
        job.pop("pending_attempt", None)
        self.save(state)
        return True

    def _route_git_integrity_repair(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
        evidence: dict[str, str],
    ) -> bool:
        """Return an integrity finding to the same Development Thread."""

        integration_work = job.get("pending_attempt_kind") == "integration"

        expected_head = evidence.get("expected_head") or job.get("managed_checkout_head")
        if not isinstance(expected_head, str) or not expected_head:
            raise GitError("Git Integrity Repair is missing its legal checkout boundary")
        try:
            self.git.restore_managed_checkout(checkout, expected_head=expected_head)
        except GitError as error:
            evidence["recovery_error"] = str(error)
            job["git_integrity_evidence"] = evidence
            job.update(
                {
                    "phase": "escalating",
                    "escalation_code": "git_integrity_boundary_restore_failed",
                }
            )
            self.save(state)
            return True

        evidence["recovery_head"] = expected_head
        evidence["recovery_action"] = "controller_reset_and_clean"
        ticket_ci_work = job.get("pending_attempt_kind") == "ci_repair"
        job["git_integrity_evidence"] = evidence
        job["repair_source"] = "git_integrity"
        if not ticket_ci_work and self.contract.label.startswith("ticket-"):
            job.pop("ci_evidence", None)
        clear_required_checks_observation(job)
        if not ticket_ci_work and self.contract.label.startswith("ticket-"):
            job.pop("final_ci_fix_failure_head", None)
        job["phase"] = "repairing"
        job["managed_checkout_head"] = expected_head
        job["modification_attempts"] = int(job["pending_attempt"])
        job["code_modification_attempts"] = int(job["pending_attempt"])
        job["attempt_kind"] = "ci_repair" if ticket_ci_work else "ordinary"
        job.pop("candidate_sha", None)
        job.pop("acceptance_record", None)
        job.pop("acceptance_artifact", None)
        job.pop("pending_attempt", None)
        job.pop("pending_attempt_kind", None)
        if ticket_ci_work:
            job["next_attempt_kind"] = "ci_repair"
        elif integration_work:
            job["next_attempt_kind"] = "integration"
        else:
            job.pop("next_attempt_kind", None)
        job.pop("candidate_commit_intent", None)
        self._sync_attempts(state, job)
        semantic_attempt = pending_semantic_attempt(job, role="development")
        if semantic_attempt is None:
            raise ValueError("Git Integrity closeout is missing its Semantic Attempt")
        close_semantic_attempt(
            job,
            semantic_attempt,
            outcome="git_integrity_repair",
            result=_development_attempt_result(job),
        )
        self.save(state)
        return True


    def advance_change(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
        *,
        scope: ChangeScope,
        allow_agent: bool = False,
    ) -> DeliveryStep:
        self._bind_change(state, job, scope)
        # Change delivery has already checked active Ticket, budget, pause and
        # unknown PR writes, in that order. Repair enters directly and keeps
        # its own canonical budget gate here.
        with self.agent_work.step(job, allow_agent=allow_agent):
            try:
                if scope == "repair":
                    ensure_budget(delivery_budget_owner(state, job), self.review_budget_policy())
                phase = job["phase"]
                if phase in {"developing", "repairing"}:
                    self._develop(state, job, checkout)
                elif phase == "committing_candidate":
                    self._reject_stale(
                        state,
                        job,
                        checkout,
                        "Candidate was not created after requirements changed",
                    )
                    if not self._commit_candidate(state, job, checkout):
                        return self._step_result(state, job, waiting=True)
                elif phase in {"candidate", "reviewing"}:
                    pending = pending_semantic_attempt(job, role="reviewer")
                    if pending is not None and "returned_result" in pending and "pending_review_result" not in job:
                        self._retain_review_return(state, job)
                    if isinstance(job.get("pending_review_result"), dict):
                        self._complete_change_review(state, job, checkout)
                    elif phase == "reviewing":
                        job["phase"] = "candidate"
                        self.save(state)
                    else:
                        owner = delivery_budget_owner(state, job)
                        if job.get("attempt_kind") == "ci_repair" and not ci_review_required(
                            owner,
                            is_ticket=scope == "ticket",
                        ):
                            from agent_run.change_delivery_fallback import prepare_ticket_ci_publication_authorization
                            if not prepare_ticket_ci_publication_authorization(self, state, job):
                                raise ValueError("Ticket CI repair has no valid publication authority")
                        elif not review_is_due(job, self.review_budget_policy(), budget_owner=owner):
                            from agent_run.change_delivery_fallback import prepare_ticket_fallback
                            if not prepare_ticket_fallback(self, state, job):
                                policy = self.review_budget_policy()
                                self._checkpoint_budget(
                                    state,
                                    job,
                                    "modification_budget_exhausted" if policy.fallback else "review_budget_exhausted",
                                    "Review budget is exhausted and the current Candidate still needs modification",
                                )
                        else:
                            self._review(state, job, checkout)
                elif phase == "escalating" and scope == "repair":
                    from agent_run.run_repair_cycle import escalate_repair
                    escalate_repair(state, state["run_acceptance"], job, str(job["escalation_code"]))
                    job["phase"] = "blocked"
                    self.save(state)
                return self._step_result(state, job)
            except AgentWorkRequired as work:
                return DeliveryStep(
                    DeliveryStepKind.WAITING if self.agent_work.capacity_wait else DeliveryStepKind.NEEDS_AGENT,
                    str(job["phase"]),
                    "work_capacity" if self.agent_work.capacity_wait else work.role,
                )
            except InitialCredentialUnavailable as error:
                if scope != "repair":
                    raise
                self.wait_for_change_credential(state, job, scope=scope, http_status=error.http_status)
                return self._step_result(state, job, waiting=True)
            except GitHubReadError as error:
                if scope != "repair" or not is_github_convergence_error(error.code):
                    raise
                wait_for_github_convergence(
                    state,
                    code=error.code,
                    message=str(error),
                    waiting_for=self.contract.label,
                )
                ensure_supervision_window(state)
                self.save(state)
                self.agent_work.defer()
                return self._step_result(state, job, waiting=True)

    def advance_run(self, state: dict[str, Any], run: dict[str, Any]) -> DeliveryStep:
        self._scope, self._current_job = "run", run
        with self.agent_work.step(run, allow_agent=True):
            phase = run["phase"]
            if phase not in {"pending", "reviewing"}:
                return self._step_result(state, run)
            self.retain_run_review_return(state, run)
            if isinstance(run.get("pending_review_result"), dict):
                progressing = self._complete_run_review(state, run)
            elif phase == "reviewing":
                if fail_interrupted_invocation(state, role="reviewer", save=self.save):
                    return self._step_result(state, run, waiting=True)
                run["phase"] = "pending"
                self.save(state)
                progressing = True
            elif not review_is_due(run, self._run_budget_policy(state, run)):
                self.checkpoint_run(state, run, "review_budget_exhausted")
                progressing = True
            else:
                progressing = self._review(state, run)
            return self._step_result(state, run, waiting=not progressing)

    def retain_run_review_return(self, state: dict[str, Any], run: dict[str, Any]) -> None:
        self._scope, self._current_job = "run", run
        pending = pending_semantic_attempt(run, role="reviewer")
        if pending is not None and "returned_result" in pending and "pending_review_result" not in run:
            self._retain_review_return(state, run)

    def _review(self, state: dict[str, Any], job: dict[str, Any], checkout: Path | None = None) -> bool:
        from agent_run.run_parent_integration import run_integration_record_is_valid
        run_scope = self._scope == "run"
        self.agent_work.require("reviewer")
        ordinal = review_ordinal(job)
        previous_attempt = pending_semantic_attempt(job, role="reviewer")
        if run_scope:
            validation = (
                self.states.root / "worktrees" / str(state["run_id"])
                / f"validation-run-{ordinal}"
            )
        else:
            assert checkout is not None
            validation = checkout.parent / f"validation-{self.contract.label}-{ordinal}"
        semantic: dict[str, Any] | None = None
        if run_scope:
            if isinstance(
                job.get("run_integration"),
                dict,
            ) and not run_integration_record_is_valid(self, state):
                raise ValueError("Run Acceptance requires the current integrated Run Branch")
            resume_initial_credential_wait(state, work_subject=_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT)
        else:
            job.pop("pending_review_result", None)
            job["phase"] = "reviewing"
            semantic = self._allocate_review(state, job, self._invocation_boundary(job))
            self._sync_attempts(state, job)
            self.save(state)
        try:
            if run_scope:
                run_head, default_head = self.git.resolve(str(state["run_branch"])), self._default_head(state)
                try:
                    self.git.prepare_expected_merge_checkout(
                        default_head_sha=default_head,
                        run_head_sha=run_head,
                        checkout=validation,
                    )
                except MergeConflictError as error:
                    job.update(
                        phase="repairing",
                        repair_request={
                            "repair_source": "merge_conflict",
                            "merge_conflict_evidence": str(error),
                        },
                    )
                    state.update(
                        status="run_acceptance_pending",
                        terminal_kind="run_repair_pending",
                        diagnostics=[],
                    )
                    self.save(state)
                    return True
                expected = self.git.expected_merge_tree(default_head_sha=default_head, run_head_sha=run_head)
                request = self._review_request(state, job, validation, run_head, default_head, expected)
                boundary = run_currentness_boundary(
                    state,
                    reviewed_head_sha=run_head,
                    reviewed_default_base_sha=default_head,
                    expected_merge_tree=expected,
                )
                semantic = self._allocate_review(state, job, boundary)
            else:
                if self._scope == "repair":
                    from agent_run.run_repair_requests import RunRepairRequests
                    self.candidate_acceptance.prepare_validation(job, validation)
                    assert self.github is not None
                    request = RunRepairRequests(self.git, self.github).review(state, job, validation)
                else:
                    self.git.prepare_validation_checkout(
                        head_sha=str(job["candidate_sha"]),
                        checkout=validation,
                    )
                    request = self._change_review_request(state, job, validation)
                boundary = self._invocation_boundary(job)
            assert semantic is not None
            self._review_budget_input(state, job, request, semantic)
            prepare_review_context(request, semantic, job, self.git, state)
            job["phase"] = "reviewing"
            self._save_review(state)
            try:
                execute_agent_work(
                    state,
                    job,
                    request,
                    semantic,
                    agents=self.agents,
                    save=self._save_review,
                    phase="run_acceptance" if run_scope else "reviewing",
                    boundary=boundary,
                    is_current=lambda: self._review_is_current(state, job, request, boundary),
                )
            except InitialCredentialUnavailable as error:
                if not run_scope:
                    raise
                job["phase"] = "pending"
                if previous_attempt is None:
                    job["validation_attempts"] = ordinal - 1
                    release_semantic_attempt(job)
                wait_for_initial_credential(
                    state,
                    work_subject=_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT,
                    phase="run_acceptance",
                    resume_status="run_acceptance_pending",
                    http_status=error.http_status,
                )
                self.save(state)
                return False
            finally:
                self.release_agent_work()
            if run_scope:
                clear_initial_credential_wait(state, work_subject=_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT)
                self._retain_review_return(state, job)
        finally:
            self.git.remove_worktree(validation)
        if run_scope:
            return self._complete_run_review(state, job)
        clear_initial_credential_wait(state, work_subject=self.contract.label)
        self._retain_review_return(state, job)
        self._complete_change_review(state, job, cast(Path, checkout))
        return True

    def _allocate_review(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any]:
        owner = delivery_budget_owner(state, job)
        policy = self._run_budget_policy(state, job) if self._scope == "run" else self.review_budget_policy()
        budget = ensure_budget(owner, policy)
        identity = (
            f"run-acceptance:{state['run_id']}",
            int(job["acceptance_generation"]),
        ) if self._scope == "run" else self._identity(state, job)
        return begin_review(
            job,
            work_subject=identity[0],
            generation=identity[1],
            boundary=boundary,
            budget_owner=owner,
            budget_window=int(owner["ci_recovery"]["window"] if job.get("attempt_kind") == "ci_repair" else budget["window"]),
        )

    def _review_budget_input(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        request: dict[str, Any],
        attempt: dict[str, Any],
    ) -> None:
        owner = delivery_budget_owner(state, job)
        if job.get("attempt_kind") == "ci_repair" and self._scope != "run":
            recovery = owner.get("ci_recovery")
            records = recovery.get("review_artifacts") if isinstance(recovery, dict) else None
            if isinstance(records, list) and records and isinstance(records[-1], dict):
                request["previous_acceptance_artifact"] = records[-1].get("artifact")
                request["previous_review_identity"] = records[-1].get("review_identity")
            request.update(
                ci_evidence=deepcopy(job.get("ci_evidence")),
                ci_failure_context=deepcopy(job.get("required_checks_origin")),
                attempt_kind="ci_repair",
                work_source="ci",
            )
        if job.get("parent_integration_stage") is True and job.get("attempt_kind") != "ci_repair":
            request.pop("review_budget_context", None)
        elif job.get("attempt_kind") == "ci_repair":
            request["review_budget_context"] = ci_reviewer_budget_context(
                owner,
                limit=ci_limit_for_job(job),
                is_ticket=self._scope == "ticket",
            )
        else:
            policy = self._run_budget_policy(
                state,
                job,
            ) if self._scope == "run" else self.review_budget_policy()
            request["review_budget_context"] = reviewer_budget_context(
                owner,
                policy,
                current_attempt_consumed=attempt.get("budget_consumed") is True,
            )

    def _save_review(self, state: dict[str, Any]) -> dict[str, Any]:
        job = self._current_job
        if self._scope == "run" and job is not None:
            active = state.get("active_agent_invocation")
            bound = active.get("semantic_attempt") if isinstance(active, dict) else None
            attempt = pending_semantic_attempt(job, role="reviewer")
            if job.get("phase") in {
                "pending",
                "reviewing",
            } and isinstance(
                active,
                dict,
            ) and isinstance(
                bound,
                dict,
            ) and attempt is not None and bound.get("attempt_id") == attempt["attempt_id"]:
                job["phase"] = "pending" if active["status"] == "resuming" else "reviewing"
        return self.save(state)

    def _review_is_current(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        request: dict[str, Any],
        boundary: dict[str, Any],
    ) -> bool:
        if self._scope != "run":
            return self._agent_is_current(state, job)
        from agent_run.run_parent_integration import run_integration_remote_is_current
        return (self._refresh_run_currentness(state)
                and (not isinstance(
                    job.get("run_integration"),
                    dict,
                ) or run_integration_remote_is_current(self, state))
                and self.git.resolve(str(state["run_branch"])) == boundary["reviewed_head_sha"]
                and self._default_head(state) == boundary["reviewed_default_base_sha"]
                and state["parent"]["revision"] == request["parent"]["revision"]
                and state["ticket_graph"]["revision"] == request["ticket_graph"]["revision"]
                and ticket_completion_records(state) == request["ticket_completion_records"])

    def _retain_review_return(self, state: dict[str, Any], job: dict[str, Any]) -> None:
        pending = pending_semantic_attempt(job, role="reviewer")
        if pending is None:
            raise ValueError("Reviewer result is missing its Semantic Attempt")
        review = cast(ReviewResult, restore_agent_return(pending["returned_result"], pending))
        run_scope = self._scope == "run"
        human_resume = bool(job.get("prior_human_blockers")) if run_scope else job.get("review_human_blocker_resume") is True
        if run_scope:
            self._record_reviewer(state, job, review.thread_id)
        else:
            _record_reviewer(job, review.thread_id, new_thread=job.get("review_new_thread") is True)
            for key in (
                "review_new_thread",
                "review_failure_resume",
                "review_resume_thread_id",
                "review_human_blocker_resume",
            ):
                job.pop(key, None)
        policy = self._run_budget_policy(state, job) if run_scope else self.review_budget_policy()
        artifact = consume_review_result(
            job,
            policy,
            review.artifact,
            budget_owner=delivery_budget_owner(state, job),
        )
        job["pending_review_result"] = ({
            "record": run_acceptance_record(review_context_for_result(job), review.thread_id, artifact.raw),
            "human_blocker_resume": human_resume,
        } if run_scope else {
            "reviewer_thread_id": review.thread_id,
            "artifact": artifact.raw,
            "human_blocker_resume": human_resume,
        })
        job["phase"] = "reviewing"
        self.save(state)

    def _finish_applicable_review(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        artifact: AcceptanceArtifact,
        *,
        history_facts: dict[str, Any] | None = None,
    ) -> None:
        run_scope = self._scope == "run"
        finish_review_result(
            state,
            job,
            artifact,
            resume_phase="pending" if run_scope else "candidate",
            blocked_phase="ready_for_human" if run_scope else "blocked",
            history_facts=history_facts,
        )
        policy = self._run_budget_policy(state, job) if run_scope else self.review_budget_policy()
        decision = after_review(job, policy, artifact, budget_owner=delivery_budget_owner(state, job))
        if decision == ReviewDecision.ACCEPT:
            job["phase"] = "accepted"
        elif decision == ReviewDecision.HUMAN:
            pass
        elif run_scope and decision in {
            ReviewDecision.REVIEW_EXHAUSTED,
            ReviewDecision.DEVELOPMENT_EXHAUSTED,
        }:
            self.checkpoint_run(state, job, decision.value)
        elif not run_scope and decision in {
            ReviewDecision.REVIEW_EXHAUSTED,
            ReviewDecision.CI_EXHAUSTED,
            ReviewDecision.DEVELOPMENT_EXHAUSTED,
        }:
            job.update(phase="escalating", escalation_code=decision.value)
            if decision == ReviewDecision.REVIEW_EXHAUSTED:
                ensure_budget(delivery_budget_owner(state, job), policy)["checkpoint_reason"] = decision.value
        else:
            job["phase"] = "repairing"
            if job.get("attempt_kind") == "ci_repair":
                job["next_attempt_kind"] = "ci_repair"
        self.save(state)

    def checkpoint_run(self, state: dict[str, Any], job: dict[str, Any], code: str) -> None:
        self._scope, self._current_job = "run", job
        job.update(phase="ready_for_human", blocked_reason=code)
        ensure_budget(job, self._run_budget_policy(state, job))["checkpoint_reason"] = code
        state.update(
            status="ready_for_human",
            terminal_kind="waiting_human",
            diagnostics=[{"code": code, "message": "Run Acceptance budget is exhausted; resume is required"}],
        )
        self.save(state)

    def _complete_change_review(self, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> None:
        pending = _mapping(job, "pending_review_result")
        reviewer_thread_id = pending.get("reviewer_thread_id")
        if not isinstance(reviewer_thread_id, str) or not reviewer_thread_id.strip():
            raise ValueError("pending review result is missing its Reviewer Thread")
        artifact = AcceptanceArtifact.parse(pending.get("artifact"))
        if pending_semantic_attempt(job, role="reviewer") is None:
            raise ValueError("Reviewer closeout is missing its Semantic Attempt")
        self._reject_stale(state, job, checkout, "Fresh Acceptance was discarded after requirements changed")
        job["acceptance_artifact"] = artifact.raw
        previous_repair_source = job.get("repair_source")
        # A new valid Acceptance Artifact supersedes any earlier Required-Checks
        # repair input when it is itself an Acceptance Repair.  A passing review
        # after CI repair still needs the exact CI failure as Publication context;
        # Ticket CI findings retain that original failure for the next CI Worker.
        ticket_ci = job.get("attempt_kind") == "ci_repair"
        if not artifact.is_accepted and not ticket_ci and self.contract.label.startswith("ticket-"):
            job.pop("ci_evidence", None)
            job.pop("final_ci_fix_failure_head", None)
        elif not ticket_ci and previous_repair_source != "required_checks" and self.contract.label.startswith("ticket-"):
            job.pop("ci_evidence", None)
            job.pop("final_ci_fix_failure_head", None)
        clear_required_checks_observation(job)
        budget = (
            delivery_budget_owner(state, job).get("ci_recovery")
            if job.get("attempt_kind") == "ci_repair"
            else delivery_budget_owner(state, job).get("review_budget")
        )
        review_facts: dict[str, Any] = {}
        if isinstance(budget, dict):
            context = review_context_for_result(job)
            candidate_sha = str(context["binding"]["candidate_sha"])
            reviewed_base_sha = str(context["binding"]["base_sha"])
            review_identity = review_record_identity(context)
            review_record = {
                "reviewer_thread_id": reviewer_thread_id,
                "candidate_sha": candidate_sha,
                "reviewed_base_sha": reviewed_base_sha,
                "review_identity": review_identity,
                "artifact": artifact.raw,
            }
            review_facts = {
                key: deepcopy(review_record[key])
                for key in ("reviewer_thread_id", "candidate_sha", "review_identity")
            }
            if job.get("parent_integration_stage") is not True or job.get("attempt_kind") == "ci_repair":
                remember_review(
                    budget, review_record, limit=self.review_budget_policy().review_limit,
                    human_blocker_resume=pending.get("human_blocker_resume") is True,
                )
        context = review_context_for_result(job)
        job["last_review_candidate_sha"] = str(context["binding"]["candidate_sha"])
        prior_acceptance = job.get("acceptance_record")
        if isinstance(
            prior_acceptance,
            dict,
        ) and AcceptanceArtifact.parse(prior_acceptance.get("artifact")).is_accepted:
            job["previous_parent_acceptance_record"] = deepcopy(prior_acceptance)
        if self._scope == "repair":
            job["acceptance_record"] = self.candidate_acceptance.record(
                job,
                reviewer_thread_id,
                artifact.raw,
                review_context=context,
            )
            if artifact.is_accepted:
                job.pop("unresolved_acceptance_artifact", None)
            elif artifact.has_failures:
                job["unresolved_acceptance_artifact"] = deepcopy(artifact.raw)
        else:
            job["acceptance_record"] = {
                **context["facts"]["current_review_identity"],
                "reviewer_thread_id": reviewer_thread_id,
                "artifact": artifact.raw,
            }
            if self._scope == "ticket":
                job["acceptance_record"].update(
                    acceptance_scope="change_job",
                    effective_revision=str(context["binding"]["effective_revision"]),
                )
        if artifact.is_accepted and job.get("attempt_kind") == "ci_repair":
            job["publication_authority"] = "accepted"
            job.pop("fallback_publication_receipt", None)
            job.pop("ci_repair_publication_receipt", None)
        if not artifact.is_accepted:
            job["repair_source"] = "acceptance"
            if job.get("parent_integration_stage") is True and job.get("attempt_kind") != "ci_repair":
                job["next_attempt_kind"] = "integration"
        # A first rejection has no PR to expose.  For a repair after a PR
        # already exists, update that PR's one status comment immediately so
        # it cannot keep advertising an obsolete passing Candidate.
        existing_pr = job.get("pr_number")
        if not artifact.requires_human and isinstance(existing_pr, int):
            assert self.github is not None
            record_change_status(
                self.github, self.contract.label, existing_pr,
                job,
                "not_checked",
                next_action=(
                    "repair Fresh Validation findings"
                    if artifact.has_failures
                    else "generate publication narrative"
                ),
            )
        self._finish_applicable_review(state, job, artifact, history_facts=review_facts)

    def _complete_run_review(self, state: dict[str, Any], run: dict[str, Any]) -> bool:
        from agent_run.run_parent_integration import run_integration_record_is_valid, run_integration_remote_is_current
        pending = _mapping(run, "pending_review_result")
        record = _mapping(pending, "record")
        artifact = AcceptanceArtifact.parse(record["artifact"])
        semantic_attempt = pending_semantic_attempt(run, role="reviewer")
        if semantic_attempt is None:
            raise ValueError("Run Reviewer closeout is missing its Semantic Attempt")
        run_head = str(record["reviewed_head_sha"])
        default_head = str(record["reviewed_default_base_sha"])
        expected_merge_tree = str(record["expected_merge_tree"])
        budget_policy = self._run_budget_policy(state, run)
        budget = ensure_budget(run, budget_policy)
        remember_review(budget, {
            "reviewer_thread_id": record["reviewer_thread_id"],
            "candidate_sha": run_head,
            "reviewed_base_sha": default_head,
            "expected_merge_tree": expected_merge_tree,
            "review_identity": {
                "default_base_sha": default_head, "run_head_sha": run_head,
                "expected_merge_tree": expected_merge_tree,
            },
            "artifact": artifact.raw,
        }, limit=budget_policy.review_limit,
            human_blocker_resume=pending.get("human_blocker_resume") is True)
        refreshed = self._refresh_run_currentness(state)
        integration = run.get("run_integration")
        integration_current = not isinstance(integration, dict) or (
            run_integration_record_is_valid(self, state)
            and run_integration_remote_is_current(self, state)
        )
        if (
            refreshed and integration_current and isinstance(integration, dict)
            and self.default_head_sha is None
        ):
            # A new Engine can resume a previously integrated default boundary
            # without rebuilding its checkout. Live refs above prove it still
            # applies; an explicitly refreshed head is never overwritten.
            self.default_head_sha = str(integration["target_sha"])
            self.candidate_acceptance.update_default_head(self.default_head_sha)
        if not (
            refreshed and integration_current
            # Different refs invalidate the old audit before Git previews a
            # potentially conflicting new merge. Matching refs still require
            # the complete evidence identity, including its expected tree.
            and self.git.resolve(str(state["run_branch"])) == run_head
            and self._default_head(state) == default_head
            and review_record_matches(
                record, run_review_identity(
                    state, self.git, self._default_head(state),
                    self.git.resolve(str(state["run_branch"])),
                ),
            )
        ):
            close_semantic_attempt(run, semantic_attempt, outcome="currentness_invalidated")
            detach_active_invocation(state, semantic_attempt)
            run.pop("pending_review_result", None)
            run["phase"] = "pending"
            self.save(state)
            return False
        run.update({"reviewed_head_sha": run_head,
                    "acceptance_artifact": artifact.raw, "acceptance_record": record})
        if not artifact.requires_human:
            self._record_final_pr_status(state, artifact.raw, run_head)
        self._finish_applicable_review(state, run, artifact)
        return True

    def _review_request(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        checkout: Path,
        run_head: str,
        default_head: str,
        expected_merge_tree: str,
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "run",
            "parent_issue_url": str(_mapping(
                state,
                "parent",
            ).get("url") or f"https://github.com/{state['repository']}/issues/{state['parent']['number']}"),
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": dict(_mapping(state, "parent")),
            "ticket_graph": deepcopy(_mapping(state, "ticket_graph")),
            "ticket_completion_records": ticket_completion_records(state),
            "run_review_facts": {
                "parent_revision": state["parent"]["revision"],
                "ticket_graph_revision": state["ticket_graph"]["revision"],
                "ticket_set": state["ticket_graph"]["ordered_ticket_numbers"],
                "ticket_completion_records": ticket_completion_records(state),
            },
            "base_sha": default_head,
            "run_head_sha": run_head,
            "current_review_identity": {
                "default_base_sha": default_head,
                "run_head_sha": run_head,
                "expected_merge_tree": expected_merge_tree,
            },
            "expected_merge_result": {
                "default_base_sha": default_head,
                "run_branch_head_sha": run_head,
                "inspection_command": f"git diff {default_head} {run_head}",
                "checkout_state": "merged working tree; HEAD remains default base",
            },
            "checkout": str(checkout),
            "thread_id": (
                _reviewer_resume_thread(run)
                if (
                    run.get("prior_human_blockers")
                    or run.get("reviewer_resume_thread_id")
                )
                and not run.get("reviewer_new_thread")
                else None
            ),
            **(
                {"prior_human_blockers": run["prior_human_blockers"]}
                if run.get("prior_human_blockers")
                else {}
            ),
            **(
                {"human_response_history": history}
                if (
                    history := current_human_response_history(
                        run,
                        generation=int(run.get("acceptance_generation", 1)),
                    )
                )
                else {}
            ),
        }
        fallbacks = ticket_fallback_records(state)
        if fallbacks:
            request["fallback_ticket_records"] = fallbacks
        ci_repairs = ticket_ci_repair_records(state)
        if ci_repairs:
            request["ci_repair_ticket_records"] = ci_repairs
        integrations = ticket_integration_records(state)
        if integrations:
            request["ticket_integration_records"] = integrations
        run_integration = run.get("run_integration")
        if isinstance(run_integration, dict):
            request["run_integration_record"] = dict(run_integration)
        previous = previous_review_context(run)
        if previous is not None:
            request["previous_acceptance_artifact"] = previous["artifact"]
            request["previous_review_identity"] = previous["identity"]
        return request

    def _record_reviewer(
        self, state: dict[str, Any], run: dict[str, Any], thread_id: str
    ) -> None:
        resumed_thread = run.get("reviewer_resume_thread_id")
        resumed = not run.get("reviewer_new_thread") and (
            bool(run.get("prior_human_blockers"))
            or isinstance(resumed_thread, str)
        )
        expected_thread = (
            resumed_thread
            if isinstance(resumed_thread, str)
            else latest_reviewer_thread(run)
        )
        if resumed and thread_id != expected_thread:
            raise ValueError(
                "Human Blocker resume requires the latest Reviewer Thread"
            )
        if not thread_id.strip() or (
            thread_id in prior_thread_identities(state, run) and not resumed
        ):
            raise ValueError("Run Acceptance requires a new Reviewer Thread")
        reviewers = _string_list(run, "reviewer_thread_ids")
        if thread_id not in reviewers:
            reviewers.append(thread_id)
        run["reviewer_thread_ids"] = reviewers
        run.pop("reviewer_resume_thread_id", None)
        run.pop("reviewer_new_thread", None)

    def _record_final_pr_status(
        self, state: dict[str, Any], artifact: dict[str, Any], run_head: str
    ) -> None:
        if self.github is None:
            return
        publication = state.get("run_publication")
        if not isinstance(publication, dict):
            return
        pr_number = publication.get("pr_number")
        if not isinstance(pr_number, int):
            return
        checks = _mapping(artifact, "checks")
        outcome = AcceptanceArtifact.parse(artifact).outcome
        self.github.record_agent_run_status(
            pr_number,
            {
                "scope": "final-run",
                "base_sha": self._default_head(state),
                "candidate_sha": run_head,
                "validation_outcome": outcome,
                "lane_statuses": {
                    lane: str(_mapping(checks, lane)["status"])
                    for lane in ("e2e", "standards", "spec")
                },
                "required_checks": "not_checked",
                "next_action": {
                    "pass": "generate refreshed final Run publication",
                    "findings": "repair Fresh Validation findings",
                    "blocked": "await human decision",
                }[outcome],
            },
        )

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
        elif phase in {"blocked", "stale", "publication_pending", "ready_for_approval", "ready_for_human"} or (
            waiting and state.get("terminal_kind") == "waiting_human"
        ):
            kind = DeliveryStepKind.PAUSED
        elif phase == "ready_for_integration" or waiting:
            kind = DeliveryStepKind.WAITING
        else:
            kind = DeliveryStepKind.CONTINUE
        return DeliveryStep(kind, phase, str(reason) if reason else None)

def _development_attempt_result(job: dict[str, Any]) -> dict[str, Any]:
    summary = job.get("development_summary")
    return {"development_summary": summary} if isinstance(summary, str) else {}


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    from agent_run.change_delivery_state import require_string_list
    return require_string_list(data, key)


def _reviewer_resume_thread(run: dict[str, Any]) -> str | None:
    value = run.get("reviewer_resume_thread_id")
    return value if isinstance(value, str) else latest_reviewer_thread(run)


def change_revision_changed(
    github: GitHubPublisher | None,
    state: dict[str, Any],
    job: dict[str, Any],
) -> bool:
    if isinstance(job.get("ticket_number"), int):
        assert github is not None
        return github.current_effective_revision(
            parent_number=int(state["parent"]["number"]),
            ticket_number=int(job["ticket_number"]),
            expected_revision=str(job["effective_revision"]),
        ) != str(job["effective_revision"])
    return bool(state["parent"].get("revision") != job.get("effective_revision"))


def parent_review_identity(git: GitRepository, state: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    return {
        "acceptance_scope": "parent_only",
        "parent_number": state["parent"]["number"],
        "effective_revision": job["effective_revision"],
        "reviewed_base_sha": job["base_sha"],
        "reviewed_candidate_sha": job["candidate_sha"],
        "reviewed_candidate_tree": git.resolve(f"{job['candidate_sha']}^{{tree}}"),
        "expected_merge_tree": git.expected_merge_tree(
            default_head_sha=str(job["base_sha"]),
            run_head_sha=str(job["candidate_sha"]),
        ),
    }
