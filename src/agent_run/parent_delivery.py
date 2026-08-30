from __future__ import annotations

"""Parent-only delivery using the shared candidate-first change lifecycle."""

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.approval_grant import create_grant, grant_matches
from agent_run.change_delivery import ensure_change_branch_authority
from agent_run.delivery_cleanup import (
    DeliveryCleanupEngine,
    remove_run_worktrees,
    require_clean_run_worktrees,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.parent_delivery_loop import (
    ParentDeliveryLoop,
    parent_approval_grant_authority,
)
from agent_run.delivery_policy import (
    parent_only_budget_policy_for_job,
    policy_snapshot_for_state,
)
from agent_run.state import StateStore
from agent_run.review_budget import new_budget, reset_budget
from agent_run.required_checks_observation import clear_required_checks_observation
from agent_run.semantic_attempt import (
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
)


class ParentDeliveryEngine:
    """Deliver a zero-Child-Ticket Parent Issue to its single Parent PR."""

    def __init__(
        self,
        *,
        git: GitRepository,
        states: StateStore,
        github: GitHubPublisher,
        agents: AgentBackend,
    ) -> None:
        self.git = git
        self.states = states
        self.github = github
        self.agents = agents

    def deliver(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            if state.get("status") in {
                "abandoned",
                "unsupported_scope_change",
            }:
                return state
            job = self._job(state)
            if job["phase"] in {"completed", "ready_for_approval"}:
                return state
            checkout = self.states.root / "worktrees" / run_id / "parent"
            preserve_checkout = False
            checkout_existed_before_attempt = checkout.exists()
            prepared = self.git.ticket_checkout_matches(
                checkout, str(job["parent_branch"])
            )
            try:
                ensure_change_branch_authority(
                    github=self.github,
                    state=state,
                    job=job,
                    branch=str(job["parent_branch"]),
                    base_branch=str(_mapping(state, "base")["branch"]),
                    save=self._save,
                )
                self.git.prepare_ticket_checkout(
                    branch=str(job["parent_branch"]),
                    base_sha=str(job["base_sha"]),
                    checkout=checkout,
                )
                prepared = True
                result = ParentDeliveryLoop(
                    git=self.git,
                    states=self.states,
                    github=self.github,
                    agents=self.agents,
                ).run(state, job, checkout)
                if result.get("status") == "completed":
                    preserve_checkout = True
                    return DeliveryCleanupEngine(
                        git=self.git, states=self.states, github=self.github
                    ).complete_parent(result)
                preserve_checkout = result.get("status") in {
                    "waiting_checks",
                    "requeue_required",
                    "blocked",
                    "ready_for_human",
                } or (
                    job.get("blocked_reason") == "agent_requires_human"
                    and job.get("human_blocker_phase")
                    in {"developing", "repairing"}
                )
                return result
            except KeyboardInterrupt:
                preserve_checkout = checkout_existed_before_attempt or prepared
                raise
            except BaseException:
                preserve_checkout = checkout_existed_before_attempt or prepared
                raise
            finally:
                if not preserve_checkout:
                    self.git.remove_worktree(
                        checkout,
                        discard_worktree=not (
                            checkout_existed_before_attempt or prepared
                        ),
                    )
                    self._remove_empty_worktree_directories(checkout)

    def approve(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            job = _mapping(state, "parent_job")
            if job.get("phase") == "completed":
                return state
            if job.get("phase") not in {"ready_for_approval", "merging"}:
                raise ValueError("Parent-only delivery is not awaiting approval")
            if state.get("status") == "unsupported_scope_change":
                return state
            if job.get("phase") == "ready_for_approval":
                authority = parent_approval_grant_authority(state, job)
                if not grant_matches(job.get("approval_grant"), authority):
                    job["approval_grant"] = create_grant(authority)
                job["phase"] = "waiting_checks"
                state.update(
                    {
                        "status": "parent_delivery_pending",
                        "terminal_kind": None,
                        "diagnostics": [],
                    }
                )
                self._save(state)
        return self.deliver(run_id)

    def recover_closeout(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            job = _mapping(state, "parent_job")
            if job.get("phase") != "merging":
                return state
        return self.deliver(run_id)

    def abandon(self, run_id: str, *, discard_worktree: bool = False) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            job = _mapping(state, "parent_job")
            if job.get("phase") == "completed":
                return state
            if job.get("phase") == "abandoned":
                return state
            if not discard_worktree:
                require_clean_run_worktrees(self.git, self.states, run_id)
            abandonment = state.get("run_abandonment")
            if not isinstance(abandonment, dict):
                pr_number = job.get("pr_number")
                abandonment = {
                    "phase": "pending",
                    "kind": "parent_only",
                    "parent_pr": (
                        {"pr_number": pr_number, "status": "pending"}
                        if isinstance(pr_number, int)
                        else None
                    ),
                }
                state.update(
                    {
                        "run_abandonment": abandonment,
                        "status": "abandonment_pending",
                        "terminal_kind": "abandonment_pending",
                        "diagnostics": [],
                    }
                )
                self._save(state)
            parent_pr = abandonment.get("parent_pr")
            if (
                isinstance(parent_pr, dict)
                and parent_pr.get("status") != "completed"
            ):
                self.github.abandon_parent_pr(int(parent_pr["pr_number"]))
                parent_pr["status"] = "completed"
                self._save(state)
            remove_run_worktrees(
                self.git,
                self.states,
                run_id,
                discard_worktree=discard_worktree,
            )
            abandonment["phase"] = "completed"
            job["phase"] = "abandoned"
            state.update(
                {
                    "status": "abandoned",
                    "terminal_kind": "abandoned",
                    "diagnostics": [],
                }
            )
            return self._save(state)

    def retire_for_child_flow(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self.states.load_current_run(run_id)
            if state is None:
                raise ValueError(f"unknown Delivery Run: {run_id}")
            job = state.get("parent_job")
            if not isinstance(job, dict):
                return state
            pr_number = job.get("pr_number")
            if isinstance(pr_number, int):
                self.github.abandon_parent_pr(pr_number)
            job["phase"] = "abandoned_for_structure_change"
            state["retired_parent_job"] = dict(job)
            state.pop("parent_job", None)
            self._save(state)
            return state

    def _load(self, run_id: str) -> dict[str, Any]:
        state = self.states.load_current_run(run_id)
        if state is None:
            raise ValueError(f"unknown Delivery Run: {run_id}")
        if state.get("delivery_type") != "parent_only":
            raise ValueError("Delivery Run is not Parent-only")
        return state

    def _job(self, state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("parent_job")
        parent = _mapping(state, "parent")
        if isinstance(existing, dict):
            if existing.get("effective_revision") != parent.get("revision"):
                self._reset_for_revision(state, existing, str(parent["revision"]))
                self._save(state)
            return existing
        branch = state.get("parent_branch")
        if not isinstance(branch, str) or not branch:
            raise ValueError("Parent-only Delivery Run is missing Parent Branch")
        job = {
            "run_id": state["run_id"],
            "parent_branch": branch,
            "base_sha": str(_mapping(state, "base")["sha"]),
            "effective_revision": str(parent["revision"]),
            "parent_generation": int(state.get("retired_parent_generation", 0)) + 1,
            "phase": "developing",
            "modification_attempts": 0,
            "development_thread_id": None,
            "development_thread_history": [],
            "reviewer_thread_ids": [],
            "validation_attempts": 0,
            "acceptance_artifact": None,
            "policy_snapshot": policy_snapshot_for_state(state),
            "review_budget": new_budget(),
            "review_budget_history": [],
        }
        state["parent_job"] = job
        self._save(state)
        return job

    @staticmethod
    def _reset_for_revision(
        state: dict[str, Any], job: dict[str, Any], revision: str
    ) -> None:
        pending = pending_semantic_attempt(job)
        if pending is not None:
            close_semantic_attempt(job, pending, outcome="currentness_invalidated")
            detach_active_invocation(state, pending)
        reset_budget(
            job,
            parent_only_budget_policy_for_job(
                job, state_snapshot=state.get("policy_snapshot")
            ),
        )
        clear_required_checks_observation(job)
        for key in (
            "candidate_sha",
            "publication",
            "publication_sha",
            "acceptance_artifact",
            "acceptance_record",
            "repair_source",
            "ci_evidence",
            "integrated_sha",
            "merge_intent",
            "approval_grant",
            "pr_number",
            "pending_attempt",
            "blocked_reason",
            "escalation_code",
            "human_response_history",
            "human_response_generation",
            "prior_human_blockers",
            "publication_authority",
            "fallback_publication_receipt",
            "deterministic_integration_record",
            "next_attempt_kind",
            "last_review_candidate_sha",
            "final_ci_fix_failure_head",
        ):
            job.pop(key, None)
        job.update(
            {
                "base_sha": str(_mapping(state, "base")["sha"]),
                "effective_revision": revision,
                "parent_generation": int(job.get("parent_generation", 1)) + 1,
                "phase": "developing",
                "modification_attempts": 0,
                "validation_attempts": 0,
            }
        )
        state["status"] = "parent_delivery_pending"
        state["terminal_kind"] = None
        state["diagnostics"] = []

    @staticmethod
    def _remove_empty_worktree_directories(checkout: Path) -> None:
        for directory in (checkout.parent, checkout.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass

    def has_current_approval_grant(self, run_id: str) -> bool:
        with self.states.locked():
            state = self._load(run_id)
            job = _mapping(state, "parent_job")
            try:
                authority = parent_approval_grant_authority(state, job)
            except (KeyError, TypeError, ValueError):
                return False
            return grant_matches(job.get("approval_grant"), authority)

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(str(state["run_id"]), state)
        return state


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
