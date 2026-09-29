from __future__ import annotations

"""Change Job operations consumed by the shared Development–Review loop."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.ci_recovery_budget import ci_review_required
from agent_run.development_review import review_is_due
from agent_run.parent_only_integration import prepare_parent_integration
from agent_run.run_repair_integration import needs_repair_integration, prepare_repair_integration
from agent_run.ticket_integration import prepare_ticket_integration
from agent_run.ticket_queue import acquire as acquire_ticket_slot

if TYPE_CHECKING:
    from agent_run.change_delivery import ChangeDeliveryEngine


class ChangeDevelopmentReview:
    def __init__(
        self, engine: ChangeDeliveryEngine, state: dict[str, Any],
        job: dict[str, Any], checkout: Path,
    ) -> None:
        self.engine = engine
        self.state = state
        self.job = job
        self.checkout = checkout

    def pause_requested(self) -> bool:
        return self.engine._pause_ticket_if_requested(self.state, self.job)

    def develop(self) -> bool:
        self.engine._develop(self.state, self.job, self.checkout)
        return True

    def commit_candidate(self) -> bool:
        self.engine._reject_stale(
            self.state, self.job, self.checkout,
            "Candidate was not created after requirements changed",
        )
        return self.engine._commit_candidate(self.state, self.job, self.checkout)

    def review(self) -> bool:
        state, job, checkout = self.state, self.job, self.checkout
        if (
            self.engine.contract.label.startswith("ticket-")
            and job.get("attempt_kind") == "ci_repair"
        ):
            if not acquire_ticket_slot(state, job):
                self.engine.save(state)
                return False
            self.engine.save(state)
            if not prepare_ticket_integration(self.engine, state, job, checkout):
                return True
            self.engine._bind_ticket_ci_integration(state, job)
        if (
            self.engine.contract.label == "parent-only"
            # A no-change first integration reuses demand acceptance
            # and does not open the separate integration review budget.
            # Its record still makes every later CI Candidate cross
            # the live snapshot boundary before Reviewer starts.
            and isinstance(job.get("parent_integration"), dict)
            and not prepare_parent_integration(self.engine, state, job, checkout)
        ):
            return True
        if (
            self.engine.contract.label.startswith("run-repair-")
            and not isinstance(job.get("integrated_sha"), str)
            and needs_repair_integration(self.engine, job)
            and not prepare_repair_integration(self.engine, state, job, checkout)
        ):
            return True
        if (
            job.get("attempt_kind") == "ci_repair"
            and job.get("repair_source") == "required_checks"
            and not ci_review_required(
                job, is_ticket=self.engine.contract.label.startswith("ticket-")
            )
        ):
            if not self.engine._prepare_ticket_ci_first_round_authorization(
                state, job
            ):
                raise ValueError("Ticket CI repair has no valid first-round publication authority")
            return True
        policy = self.engine.review_budget_policy()
        if not review_is_due(job, policy):
            if self.engine._prepare_ticket_fallback(state, job):
                return True
            self.engine._checkpoint_budget(
                state, job,
                "modification_budget_exhausted" if policy.fallback else "review_budget_exhausted",
                "Review budget is exhausted and the current Candidate still needs modification",
            )
            return True
        self.engine._review(state, job, checkout)
        return True

    def resume_review(self) -> bool:
        if isinstance(self.job.get("pending_review_result"), dict):
            self.engine._complete_review(self.state, self.job, self.checkout)
        else:
            # Resume the existing semantic attempt, without charging a new one.
            self.job["phase"] = "candidate"
            self.engine.save(self.state)
        return True
