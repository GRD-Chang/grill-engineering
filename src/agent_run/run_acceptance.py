from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.ci_recovery_budget import new_ci_recovery_budget
from agent_run.delivery_policy import run_repair_budget_policy_for_job
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.delivery_step import DeliveryStepKind
from agent_run.development_review import DevelopmentReview
from agent_run.git import GitRepository
from agent_run.integration_record_contract import require_completed_ticket_integration_records
from agent_run.object_state import object_store
from agent_run.review_budget import ensure_budget, new_budget
from agent_run.review_evidence import review_record_matches, run_review_identity
from agent_run.run_currentness import RunCurrentnessReader, invalidate_run_acceptance
from agent_run.run_parent_integration import prepare_run_parent_integration
from agent_run.run_pr_reconciliation import reconcile_run_pr_write
from agent_run.run_repair_lifecycle import RunRepairLifecycle
from agent_run.run_repair_promotion import RunRepairPromotion
from agent_run.state import StateStore


class RunAcceptanceEngine:
    """Independently validate a completed Run and repair only its Run Branch."""

    def __init__(
        self,
        *,
        git: GitRepository,
        states: StateStore,
        agents: AgentBackend,
        default_head_sha: str | None = None,
        github: GitHubPublisher | None = None,
        currentness_reader: RunCurrentnessReader | None = None,
    ) -> None:
        self.git = git
        self.states = object_store(states, parent_scope=True)
        self.agents = agents
        self.github = github
        self.work = DevelopmentReview(
            git=git, states=self.states, agents=agents, github=github,
            currentness_reader=currentness_reader, default_head_sha=default_head_sha,
        )
        self._repair_promotion = RunRepairPromotion(self)
        self._repair_lifecycle = RunRepairLifecycle(self, self._repair_promotion)

    def accept(self, run_id: str) -> dict[str, Any]:
        with self.states.work_session():
            return self._accept(run_id)

    def _accept(self, run_id: str) -> dict[str, Any]:
        state = self.states.load_current_run(run_id)
        if state is None:
            raise ValueError(f"unknown Delivery Run: {run_id}")
        state.update(self.states.load_delivery_evidence(run_id))  # type: ignore[attr-defined]
        run = state.get("run_acceptance")
        repair = run.get("repair_job") if isinstance(run, dict) else None
        if isinstance(run, dict) and isinstance(repair, dict) and isinstance(repair.get("run_branch_write_intent"), dict):
            # Resolve the original remote write before graph/currentness
            # refresh or integration can replace its authority or charge it
            # as an external source update.
            self._repair_lifecycle._repair(state, run)
            return self._save(state)
        if not reconcile_run_pr_write(
            state, self.github, self._save,
            current_head_sha=self.git.resolve(str(state["run_branch"])),
        ):
            return state
        if not self.work._refresh_run_currentness(state):
            return self._save(state)
        if not self._all_tickets_completed(state):
            raise ValueError("Run Acceptance requires every Ticket to be completed")
        run = self._run_state(state)
        budget_policy = run_repair_budget_policy_for_job(
            run, state_snapshot=state.get("policy_snapshot")
        )
        ensure_budget(run, budget_policy)
        self.work.retain_run_review_return(state, run)
        # A returned Reviewer still owns its pending Semantic Attempt. Finish
        # or invalidate that result before integration can allocate conflict
        # work, renew a window, or persist a different pause phase.
        if "pending_review_result" not in run:
            if not prepare_run_parent_integration(self, state, run):
                return self._save(state)
            self._invalidate_stale_acceptance(state, run)
        acceptance_already_complete = run["phase"] == "accepted"
        if run["phase"] not in {
            "pending", "reviewing", "repairing", "accepted", "ready_for_human",
        }:
            raise ValueError(f"unknown Run Acceptance phase: {run.get('phase')}")
        while run["phase"] in {"pending", "reviewing", "repairing"}:
            if run["phase"] == "repairing":
                repair = self._repair_lifecycle._repair(state, run)
                if repair in {"waiting", "promoted"}:
                    break
                if repair == "no_code_changes":
                    run.update({"phase": "ready_for_human", "blocked_reason": repair})
                    state["diagnostics"] = [{
                        "code": repair, "message": "Run Repair produced no code changes",
                    }]
                elif repair == "blocked":
                    run["phase"] = "ready_for_human"
                else:
                    run["phase"] = "pending"
                    run.pop("acceptance_record", None)
                    run.pop("acceptance_artifact", None)
                self._save(state)
                if repair == "stale":
                    break
            elif self.work.advance_run(state, run).kind is not DeliveryStepKind.CONTINUE:
                break
        if run["phase"] == "accepted" and (
            not acceptance_already_complete or state.get("status") == "run_acceptance_pending"
        ):
            # Reusing an applicable completed review must retain the current
            # Publication/approval gate; only Acceptance's own handoff advances
            # the Run to publication work.
            state.update({"status": "run_publication_pending",
                          "terminal_kind": "run_acceptance_passed", "diagnostics": []})
        elif run["phase"] == "ready_for_human":
            state.update({"status": "ready_for_human", "terminal_kind": "waiting_human"})
        return self._save(state)

    def _run_state(self, state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("run_acceptance")
        if isinstance(existing, dict):
            return existing
        run = {
            "phase": "pending",
            "acceptance_generation": 1,
            "modification_attempts": 0,
            "validation_attempts": 0,
            "development_thread_id": None,
            "development_thread_history": [],
            "reviewer_thread_ids": [],
            "candidate_acceptance_history": [],
            "review_budget": new_budget(),
            "review_budget_history": [],
            "ci_recovery": new_ci_recovery_budget(),
        }
        if isinstance(state.get("policy_snapshot"), dict):
            run["policy_snapshot"] = dict(state["policy_snapshot"])
        state["run_acceptance"] = run
        return run

    def _invalidate_stale_acceptance(
        self, state: dict[str, Any], run: dict[str, Any]
    ) -> None:
        # An active Repair Job owns its own currentness checks.  This guard only
        # invalidates a persisted, pre-repair Run Acceptance record before a
        # new Repair Cycle can reuse it.
        if isinstance(run.get("repair_job"), dict):
            return
        record = run.get("acceptance_record")
        if not isinstance(record, dict):
            return
        current_head = self.git.resolve(str(state["run_branch"]))
        if review_record_matches(
            record, run_review_identity(state, self.git, self.work._default_head(state), current_head),
        ):
            return
        invalidate_run_acceptance(state)

    def _all_tickets_completed(self, state: dict[str, Any]) -> bool:
        require_completed_ticket_integration_records(state)
        order = self._mapping(state, "ticket_graph").get("ordered_ticket_numbers")
        jobs = self._mapping(state, "ticket_jobs")
        return isinstance(order, list) and bool(order) and all(
            isinstance(jobs.get(str(number)), dict)
            and jobs[str(number)].get("phase") == "completed"
            for number in order
        )

    def _repair_checkout(self, state: dict[str, Any]) -> Path:
        return self.states.root / "worktrees" / str(state["run_id"]) / "run-repair"

    @staticmethod
    def _remove_empty_directories(checkout: Path) -> None:
        for directory in (checkout.parent, checkout.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
        value = data.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")
        return value

    @staticmethod
    def _string_list(data: dict[str, Any], key: str) -> list[str]:
        from agent_run.work_projection import materialize_history

        value = materialize_history(data, key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key} must contain strings")
        return list(value)

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(str(state["run_id"]), state)
        return state
