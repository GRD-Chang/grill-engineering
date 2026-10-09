from __future__ import annotations

"""Change Job operations consumed by the shared Development–Review loop."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.delivery_budget import delivery_budget_owner

from agent_run.ci_recovery_budget import ci_review_required
from agent_run.development_review import review_is_due
from agent_run.change_delivery_integration import prepare_delivery_integration

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
        if not self.engine.contract.label.startswith("ticket-") and not prepare_delivery_integration(
            self.engine, state, job, checkout, before_review=True
        ):
            return job.get("phase") != "ready_for_integration"
        if (
            job.get("attempt_kind") == "ci_repair"
            and not ci_review_required(
                delivery_budget_owner(state, job), is_ticket=self.engine.contract.label.startswith("ticket-")
            )
        ):
            if not self.engine._prepare_ticket_ci_publication_authorization(
                state, job
            ):
                raise ValueError("Ticket CI repair has no valid publication authority")
            return True
        policy = self.engine.review_budget_policy()
        if not review_is_due(job, policy, budget_owner=delivery_budget_owner(state, job)):
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
