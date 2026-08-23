from __future__ import annotations

"""Parent-only delivery using the shared candidate-first change lifecycle."""

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.approval_grant import (
    acceptance_fingerprint,
    create_grant,
    grant_authority,
    grant_matches,
)
from agent_run.artifacts import AcceptanceArtifact
from agent_run.change_delivery import ensure_change_branch_authority
from agent_run.delivery_cleanup import DeliveryCleanupEngine, remove_run_worktrees
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.external_supervision import (
    is_github_convergence_error,
    ensure_supervision_window,
    wait_for_github_convergence,
)
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError, MergeOutcomeUnknownError
from agent_run.parent_delivery_loop import ParentDeliveryLoop
from agent_run.state import StateStore
from agent_run.review_budget import RUN_POLICY, new_budget, reset_budget


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
            if job["phase"] == "merging":
                try:
                    live = self.github.live_pull_request(int(job["pr_number"]))
                except (OSError, GitHubReadError) as error:
                    if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                        error.code
                    ):
                        raise
                    return self._wait_for_merge_convergence(state, job, str(error))
                if live.get("state") == "MERGED":
                    return self._complete_after_merge(state, job, live)
                job["phase"] = "ready_for_approval"
                state["status"] = "parent_approval_pending"
                state["diagnostics"] = []
                self._save(state)
                return state
            if job["phase"] in {"completed", "ready_for_approval"}:
                return state
            checkout = self.states.root / "worktrees" / run_id / "parent"
            preserve_checkout = False
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
                preserve_checkout = result.get("status") == "waiting_checks" or (
                    job.get("blocked_reason") == "agent_requires_human"
                    and job.get("human_blocker_phase")
                    in {"developing", "repairing"}
                )
                return result
            except KeyboardInterrupt:
                raise
            except BaseException:
                preserve_checkout = prepared
                raise
            finally:
                if not preserve_checkout:
                    self.git.remove_worktree(checkout)
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
            base = _mapping(state, "base")
            pr_number = int(job["pr_number"])
            live = self.github.live_pull_request(pr_number)
            if live.get("state") == "OPEN":
                if not self._acceptance_is_current(state, job):
                    self._reset_for_revision(
                        state, job, str(_mapping(state, "parent")["revision"])
                    )
                    self._save(state)
                    return state
                if self.git.resolve(str(base["branch"])) != job.get("base_sha"):
                    return self._block(
                        state,
                        job,
                        "parent_default_branch_drift",
                        "Default branch advanced after Parent Fresh Validation",
                    )
                if (
                    live.get("head_sha") != job.get("publication_sha")
                    or live.get("base_branch") != base.get("branch")
                    or live.get("base_sha") != job.get("base_sha")
                    or live.get("mergeable") is not True
                ):
                    return self._block(
                        state,
                        job,
                        "parent_published_head_mismatch",
                        "Parent PR no longer matches the accepted publication",
                    )
                authority = self._approval_grant_authority(state, job)
                if not grant_matches(job.get("approval_grant"), authority):
                    job["approval_grant"] = create_grant(authority)
                    self._save(state)
                checks = self.github.required_checks(pr_number)
                if checks == "pending":
                    job["phase"] = "waiting_checks"
                    state["status"] = "waiting_checks"
                    state["diagnostics"] = []
                    ensure_supervision_window(state)
                    self._save(state)
                    self._record_status(pr_number, job, checks, "wait for Required Checks")
                    return state
                if checks == "fail":
                    job.pop("approval_grant", None)
                    job.update(
                        {
                            "phase": "repairing",
                            "repair_source": "required_checks",
                            "ci_evidence": self.github.required_check_evidence(pr_number),
                        }
                    )
                    state["status"] = "parent_delivery_pending"
                    state["diagnostics"] = []
                    self._save(state)
                    self._record_status(pr_number, job, checks, "repair failed Required Checks")
                    return state
                if checks not in {"none", "pass"}:
                    raise ValueError(f"unknown Required Checks state: {checks}")
                if not grant_matches(job.get("approval_grant"), authority):
                    return self._block(
                        state,
                        job,
                        "approval_grant_mismatch",
                        "Parent approval no longer matches the accepted publication",
                    )
                job["phase"] = "merging"
                self._save(state)
                try:
                    integrated = self.github.normal_merge(
                        pr_number=pr_number,
                        expected_head_sha=str(job["publication_sha"]),
                    )
                except (MergeOutcomeUnknownError, OSError, GitHubReadError) as error:
                    if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                        error.code
                    ):
                        raise
                    return self._wait_for_merge_convergence(state, job, str(error))
                job["integrated_sha"] = integrated
                self._save(state)
                try:
                    live = self.github.live_pull_request(pr_number)
                except (OSError, GitHubReadError) as error:
                    if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                        error.code
                    ):
                        raise
                    return self._wait_for_merge_convergence(state, job, str(error))
            if live.get("state") != "MERGED":
                raise ValueError("Parent PR was not merged after explicit approval")
            return self._complete_after_merge(state, job, live)

    def recover_closeout(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            job = _mapping(state, "parent_job")
            if job.get("phase") != "merging":
                return state
            live = self.github.live_pull_request(int(job["pr_number"]))
            if live.get("state") == "MERGED":
                return self._complete_after_merge(state, job, live)
            if live.get("state") == "OPEN":
                job["phase"] = "ready_for_approval"
                state["status"] = "parent_approval_pending"
                state["diagnostics"] = []
                self._save(state)
            return state

    def abandon(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            job = _mapping(state, "parent_job")
            if job.get("phase") == "completed":
                return state
            if job.get("phase") == "abandoned":
                return state
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
            remove_run_worktrees(self.git, self.states, run_id)
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
        reset_budget(job, RUN_POLICY)
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
            "required_checks",
            "required_checks_mode",
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

    def _complete_after_merge(
        self, state: dict[str, Any], job: dict[str, Any], live: dict[str, Any]
    ) -> dict[str, Any]:
        base_sha = str(job["base_sha"])
        publication_sha = str(job["publication_sha"])
        integrated = live.get("integrated_sha")
        if (
            not isinstance(integrated, str)
            or live.get("head_sha") != publication_sha
            or live.get("base_branch") != _mapping(state, "base").get("branch")
            or live.get("integrated_parents") != [base_sha, publication_sha]
        ):
            return self._block(
                state,
                job,
                "parent_merged_result_mismatch",
                "Merged Parent PR does not match the approved publication",
            )
        job["integrated_sha"] = integrated
        self._save(state)
        self.github.close_parent_issue(
            parent_number=int(_mapping(state, "parent")["number"]),
            run_id=str(state["run_id"]),
            pr_number=int(job["pr_number"]),
            integrated_sha=integrated,
            delivery_type="Parent-only",
        )
        job["phase"] = "completed"
        state["status"] = "completed"
        state["terminal_kind"] = "completed"
        state["diagnostics"] = []
        self._save(state)
        return DeliveryCleanupEngine(
            git=self.git, states=self.states, github=self.github
        ).complete_parent(state)

    def _acceptance_is_current(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        acceptance = job.get("acceptance_record")
        candidate = job.get("candidate_sha")
        if not isinstance(acceptance, dict) or not isinstance(candidate, str):
            return False
        return (
            acceptance.get("reviewed_base_sha") == job.get("base_sha")
            and acceptance.get("reviewed_candidate_sha") == candidate
            and acceptance.get("reviewed_candidate_tree")
            == self.git.resolve(f"{candidate}^{{tree}}")
            and acceptance.get("effective_revision") == job.get("effective_revision")
            and _mapping(state, "parent").get("revision")
            == job.get("effective_revision")
        )

    def _approval_grant_authority(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, object]:
        return grant_authority(
            repository=str(state["repository"]),
            pr_number=int(job["pr_number"]),
            head_branch=str(job["parent_branch"]),
            head_sha=str(job["publication_sha"]),
            base_branch=str(_mapping(state, "base")["branch"]),
            base_sha=str(job["base_sha"]),
            acceptance_fingerprint=acceptance_fingerprint(
                _mapping(job, "acceptance_record"),
                _mapping(job, "acceptance_artifact"),
            ),
        )

    def has_current_approval_grant(self, run_id: str) -> bool:
        with self.states.locked():
            state = self._load(run_id)
            job = _mapping(state, "parent_job")
            try:
                authority = self._approval_grant_authority(state, job)
            except (KeyError, TypeError, ValueError):
                return False
            return grant_matches(job.get("approval_grant"), authority)

    def _record_status(
        self, pr_number: int, job: dict[str, Any], checks: str, next_action: str
    ) -> None:
        artifact = _mapping(job, "acceptance_artifact")
        raw_checks = _mapping(artifact, "checks")
        self.github.record_agent_run_status(
            pr_number,
            {
                "scope": "parent-only",
                "base_sha": str(job["base_sha"]),
                "candidate_sha": str(job["candidate_sha"]),
                "validation_outcome": AcceptanceArtifact.parse(artifact).outcome,
                "lane_statuses": {
                    lane: str(_mapping(raw_checks, lane)["status"])
                    for lane in ("e2e", "standards", "spec")
                },
                "required_checks": checks,
                "next_action": next_action,
            },
        )

    def _block(
        self, state: dict[str, Any], job: dict[str, Any], code: str, message: str
    ) -> dict[str, Any]:
        job.pop("approval_grant", None)
        job.update({"phase": "blocked", "blocked_reason": code})
        state["status"] = "blocked"
        state["diagnostics"] = [{"code": code, "message": message}]
        self._save(state)
        return state

    def _wait_for_merge_convergence(
        self, state: dict[str, Any], job: dict[str, Any], message: str
    ) -> dict[str, Any]:
        job["phase"] = "merging"
        wait_for_github_convergence(
            state,
            code="merge_outcome_unknown",
            message=message,
            waiting_for="Parent-only merge/readback reconciliation",
        )
        ensure_supervision_window(state)
        return self._save(state)

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(str(state["run_id"]), state)
        return state


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
