from __future__ import annotations

"""Shared, deterministic delivery lifecycle for a change job.

The controller owns this lifecycle.  A typed job descriptor supplies only
deterministic identity and branch facts; a semantic adapter supplies request
construction and currentness decisions.  Controller/Publisher side effects
and StateStore persistence are kept in their own seam.  In particular,
neither layer gets a shortcut from development to a Run Branch write.
"""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from agent_run.agents import AgentBackend
from agent_run.artifacts import AcceptanceArtifact
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitError, GitRepository
from agent_run.worker_credentials import InitialCredentialUnavailable
from agent_run.review_budget import (
    ReviewBudgetPolicy,
    can_start_development,
    can_start_review,
    ensure_budget,
    mark_development,
    mark_review,
    policy_for_subject,
)

MAX_MODIFICATION_ATTEMPTS = 10
MAX_PUBLICATION_CONTEXT_ATTEMPTS = 4
MAX_PUBLICATION_ATTEMPTS = MAX_PUBLICATION_CONTEXT_ATTEMPTS + 1


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


class ChangeDeliveryEngine:
    """One Development -> Candidate -> Review -> Publication -> Merge loop.

    ``job`` uses the existing durable phase values so Ticket records remain
    backward compatible.  Run Repair records use the same fields but have no
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
    ) -> None:
        self.git = git
        self.github = github
        self.agents = agents
        self.contract = contract
        self.adapter = adapter
        self.publisher = publisher
        self.state_store = state_store

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.state_store.save(state)

    def run(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        try:
            ensure_budget(job, self.review_budget_policy())
            while True:
                self._require_ticket_publication_authorization(job)
                phase = str(job["phase"])
                if phase in {"completed", "blocked"}:
                    return state
                if phase == "publication_pending":
                    # A new explicit delivery attempt is allowed to retry
                    # publication only; the accepted Candidate and Fresh
                    # Acceptance remain the durable boundary.
                    job["phase"] = "accepted"
                    job["publication_attempts"] = 0
                    job.pop("last_publication_error", None)
                    state["status"] = "active"
                    state["diagnostics"] = []
                    self.save(state)
                    continue
                if phase == "merged":
                    live = self.publisher.live_pull_request(
                        state, job, int(job["pr_number"])
                    )
                    if not self.publisher.after_merge(state, job, live):
                        return state
                    job["phase"] = "completed"
                    return self.save(state)
                if phase == "escalating":
                    self.publisher.escalate(state, job, str(job["escalation_code"]))
                    job["phase"] = "blocked"
                    return self.save(state)
                if phase in {"developing", "repairing"}:
                    self._develop(state, job, checkout)
                    if job["phase"] in {"blocked", "escalating"}:
                        continue
                if job["phase"] == "committing_candidate":
                    self._reject_stale(
                        state,
                        job,
                        checkout,
                        "Candidate was not created after requirements changed",
                    )
                    if not self._commit_candidate(state, job, checkout):
                        return state
                if job["phase"] == "candidate":
                    if self.review_budget_exhausted_for_review(job):
                        if self._prepare_ticket_fallback(state, job):
                            continue
                        checkpoint_code = (
                            "modification_budget_exhausted"
                            if self.review_budget_policy().fallback
                            else "review_budget_exhausted"
                        )
                        self._checkpoint_budget(
                            state,
                            job,
                            checkpoint_code,
                            "Review budget is exhausted and the current Candidate still needs modification",
                        )
                        continue
                    self._review(state, job, checkout)
                    if job["phase"] == "escalating":
                        # Budget exhaustion is a durable terminal boundary;
                        # process it before the Run Repair adapter asks the
                        # outer Run Acceptance loop to re-enter.
                        continue
                if job["phase"] == "reviewing":
                    if isinstance(job.get("pending_review_result"), dict):
                        self._complete_review(state, job, checkout)
                    else:
                        # The reviewer may have been interrupted before returning
                        # a result. Keep that attempt in the timeline, then request
                        # a fresh independent reviewer on resume.
                        job["phase"] = "candidate"
                        self.save(state)
                    continue
                if job["phase"] == "accepted":
                    if self._can_resume_integrated_publication(job):
                        job["phase"] = "merged"
                        self.save(state)
                        continue
                    self._publication(state, job, checkout)
                    # Publication can discover a moving base and let the
                    # consumer's stale boundary choose the next phase.
                    # Continue with an active phase instead of falling
                    # through to the unknown-phase guard.
                    if job["phase"] in {"candidate", "developing", "repairing"}:
                        continue
                if job["phase"] == "escalating":
                    continue
                if job["phase"] in {
                    "publishing",
                    "waiting_checks",
                    "waiting_merge",
                    "merging",
                }:
                    if job["phase"] == "waiting_merge":
                        job["phase"] = "merging"
                        self.save(state)
                    terminal = self._publish_and_merge(state, job, checkout)
                    if terminal:
                        return state
                    continue
                if job["phase"] == "publication_pending":
                    return state
                if job["phase"] == "stale":
                    return state
                if job["phase"] in {"developing", "repairing"}:
                    continue
                if job["phase"] in {"completed", "blocked"}:
                    return state
                raise ValueError(f"unknown Ticket phase: {job['phase']}")
        except _TerminalChangeJob:
            return state
        except InitialCredentialUnavailable as error:
            self._refund_unstarted_credential_invocation(job)
            self._wait_for_initial_credential(
                state, job, http_status=error.http_status
            )
            return state

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
        if attempt_kind not in {"ordinary", "final_ci_fix"}:
            raise ValueError(f"unknown Development attempt kind: {attempt_kind}")
        if not can_start_development(
            job, self.review_budget_policy(), attempt_kind=attempt_kind
        ):
            self._checkpoint_budget(
                state,
                job,
                "modification_budget_exhausted",
                "Development budget is exhausted for the current Review Budget Window",
            )
            return
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
    ) -> Callable[..., None]:
        return invocation_events(
            self, state, job, request, role=role, phase=phase
        )

    @staticmethod
    def _invocation_identity(
        state: dict[str, Any], job: dict[str, Any]
    ) -> tuple[str, int]:
        return invocation_identity(state, job)

    def _review(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
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

    def _refund_unstarted_credential_invocation(self, job: dict[str, Any]) -> None:
        budget = ensure_budget(job, self.review_budget_policy())
        if (
            job.get("phase") in {"developing", "repairing"}
            and isinstance(job.get("pending_attempt"), int)
            and job["pending_attempt"] > int(job.get("modification_attempts", 0))
        ):
            if job.get("pending_attempt_kind") == "final_ci_fix":
                budget["final_ci_fix_used"] = False
            elif budget["development_attempts"] > 0:
                budget["development_attempts"] -= 1
            job.pop("pending_attempt", None)
            job.pop("pending_attempt_kind", None)

    def review_budget_policy(self) -> ReviewBudgetPolicy:
        policy = policy_for_subject(self.contract.label)
        if policy.fallback and MAX_MODIFICATION_ATTEMPTS != 10:
            return replace(policy, development_limit=MAX_MODIFICATION_ATTEMPTS)
        return policy

    def review_budget_exhausted_for_review(self, job: dict[str, Any]) -> bool:
        return not can_start_review(job, self.review_budget_policy())

    def mark_development_attempt(
        self, job: dict[str, Any], *, attempt_kind: str
    ) -> int:
        return mark_development(
            job, self.review_budget_policy(), attempt_kind=attempt_kind
        )

    def mark_review_invocation(self, job: dict[str, Any]) -> int:
        return mark_review(job, self.review_budget_policy())

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

    @staticmethod
    def _fallback_candidate_is_eligible(job: dict[str, Any]) -> bool:
        return fallback_candidate_is_eligible(job)

    def publication_budget_exhausted(self, attempts: int) -> bool:
        return attempts >= MAX_PUBLICATION_ATTEMPTS

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
        if job.get("publication_authority") == "fallback":
            validation_outcome = "fallback"
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
