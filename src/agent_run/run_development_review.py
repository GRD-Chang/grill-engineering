from __future__ import annotations

"""Full Parent semantics at the shared Development–Review boundary."""

from typing import TYPE_CHECKING, Any

from agent_run.agent_invocation import fail_interrupted_invocation
from agent_run.development_review import review_is_due
from agent_run.review_budget import ensure_budget

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine


class RunDevelopmentReview:
    def __init__(
        self, engine: RunAcceptanceEngine, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        self.engine = engine
        self.state = state
        self.job = job

    def pause_requested(self) -> bool:
        if self.job.get("phase") not in {
            "pending", "reviewing", "repairing", "accepted", "ready_for_human",
        }:
            raise ValueError(f"unknown Run Acceptance phase: {self.job.get('phase')}")
        # Global control is checked at the existing invocation/currentness fence.
        return False

    def develop(self) -> bool:
        # A full Parent repair retains its local branch and integration work.
        # Its development uses the shared loop; direct promotion proves
        # the reviewed Candidate is exactly the integrated result.
        repair = self.engine._repair(self.state, self.job)
        if repair in {"waiting", "promoted"}:
            return False
        if repair == "no_code_changes":
            self.job.update({"phase": "ready_for_human", "blocked_reason": repair})
            self.state["diagnostics"] = [{
                "code": repair, "message": "Run Repair produced no code changes",
            }]
        elif repair == "blocked":
            self.job["phase"] = "ready_for_human"
        else:
            self.job["phase"] = "pending"
            self.job.pop("acceptance_record", None)
            self.job.pop("acceptance_artifact", None)
        self.engine._save(self.state)
        # A stale repair yields so the next call integrates a fresh snapshot.
        return repair != "stale"

    def commit_candidate(self) -> bool:
        raise ValueError("Full Parent Candidates belong to the Run Repair Job")

    def review(self) -> bool:
        policy = self.engine._run_budget_policy(self.state, self.job)
        if not review_is_due(self.job, policy):
            self.checkpoint("review_budget_exhausted")
            return True
        return self.engine._review(self.state, self.job)

    def resume_review(self) -> bool:
        if isinstance(self.job.get("pending_review_result"), dict):
            return self.engine._complete_review(self.state, self.job)
        if fail_interrupted_invocation(
            self.state, role="reviewer", save=self.engine._save
        ):
            return False
        self.job["phase"] = "pending"
        self.engine._save(self.state)
        return True

    def checkpoint(self, code: str) -> None:
        self.job.update({"phase": "ready_for_human", "blocked_reason": code})
        policy = self.engine._run_budget_policy(self.state, self.job)
        ensure_budget(self.job, policy)["checkpoint_reason"] = code
        self.state.update({
            "status": "ready_for_human", "terminal_kind": "waiting_human",
            "diagnostics": [{"code": code,
                             "message": "Run Acceptance budget is exhausted; resume is required"}],
        })
        self.engine._save(self.state)
