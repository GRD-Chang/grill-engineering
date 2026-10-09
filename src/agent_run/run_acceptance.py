from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from agent_run.review_context import prepare_review_context, run_acceptance_record
from agent_run.review_evidence import review_record_matches, run_review_identity

from agent_run.agents import AgentBackend, ReviewResult
from agent_run.agent_execution import execute_agent_work
from agent_run.agent_return import restore_agent_return
from agent_run.development_review import advance_development_review, after_review, ReviewDecision
from agent_run.run_development_review import RunDevelopmentReview
from agent_run.artifacts import AcceptanceArtifact
from agent_run.change_delivery import latest_reviewer_thread
from agent_run.ci_recovery_budget import new_ci_recovery_budget
from agent_run.delivery_policy import run_repair_budget_policy_for_job
from agent_run.credential_availability import (
    clear_initial_credential_wait,
    resume_initial_credential_wait,
    wait_for_initial_credential,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository, MergeConflictError
from agent_run.human_responses import current_human_response_history
from agent_run.run_currentness import (
    RunCurrentnessReader,
    invalidate_run_acceptance,
    refresh_run_currentness,
    run_currentness_boundary,
    ticket_completion_records,
    ticket_fallback_records,
    ticket_ci_repair_records,
    ticket_integration_records,
)
from agent_run.run_parent_integration import (
    prepare_run_parent_integration,
    run_integration_record_is_valid,
    run_integration_remote_is_current,
)
from agent_run.run_candidate_acceptance import CandidateRunAcceptance
from agent_run.run_repair_lifecycle import RunRepairLifecycle
from agent_run.run_repair_promotion import RunRepairPromotion
from agent_run.run_repair_currentness import RunRepairCurrentness
from agent_run.run_repair_requests import RunRepairRequests
from agent_run.run_thread_identity import prior_thread_identities
from agent_run.run_pr_reconciliation import reconcile_run_pr_write
from agent_run.state import StateStore
from agent_run.integration_record_contract import (
    require_completed_ticket_integration_records,
)
from agent_run.credential_lifecycle import InitialCredentialUnavailable
from agent_run.review_attempt import (
    begin_review, consume_review_result, finish_review_result, remember_review,
    review_ordinal,
)
from agent_run.review_budget import (
    ReviewBudgetPolicy,
    ensure_budget,
    new_budget,
    previous_review_context,
    reviewer_budget_context,
)
from agent_run.semantic_attempt import (
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
    release_semantic_attempt,
)


_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT = "run-acceptance"


def _reviewer_resume_thread(run: dict[str, Any]) -> str | None:
    value = run.get("reviewer_resume_thread_id")
    return value if isinstance(value, str) else latest_reviewer_thread(run)


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
        self.states = states
        self.agents = agents
        self.default_head_sha = default_head_sha
        self.github = github
        self.currentness_reader = currentness_reader
        self.candidate_acceptance = CandidateRunAcceptance(
            git, default_head_sha=default_head_sha
        )
        self.repair_requests = (
            RunRepairRequests(git, github) if github is not None else None
        )
        self.repair_currentness = (
            RunRepairCurrentness(git, github, states)
            if github is not None
            else None
        )
        self._repair_promotion = RunRepairPromotion(self)
        self._repair_lifecycle = RunRepairLifecycle(self, self._repair_promotion)

    def accept(self, run_id: str) -> dict[str, Any]:
        state = self.states.load_current_run(run_id)
        if state is None:
            raise ValueError(f"unknown Delivery Run: {run_id}")
        if not reconcile_run_pr_write(
            state, self.github, self._save,
            current_head_sha=self.git.resolve(str(state["run_branch"])),
        ):
            return state
        if not self._refresh_run_currentness(state):
            return self._save(state)
        if not self._all_tickets_completed(state):
            raise ValueError("Run Acceptance requires every Ticket to be completed")
        run = self._run_state(state)
        budget_policy = self._run_budget_policy(state, run)
        ensure_budget(run, budget_policy)
        pending = pending_semantic_attempt(run, role="reviewer")
        if pending is not None and "returned_result" in pending and "pending_review_result" not in run:
            # Recover the dispatched snapshot before preparing any new Git
            # boundary. The saved result may already be stale, but was paid for.
            review = cast(ReviewResult, restore_agent_return(pending["returned_result"], pending))
            self._retain_review_return(state, run, review, self._mapping(pending, "review_context"))
        # A returned Reviewer still owns its pending Semantic Attempt. Finish
        # or invalidate that result before integration can allocate conflict
        # work, renew a window, or persist a different pause phase.
        if "pending_review_result" not in run:
            if not prepare_run_parent_integration(self, state, run):
                return self._save(state)
            self._invalidate_stale_acceptance(state, run)
        advance_development_review(RunDevelopmentReview(self, state, run))
        if run["phase"] == "accepted":
            state.update({"status": "run_publication_pending",
                          "terminal_kind": "run_acceptance_passed", "diagnostics": []})
        elif run["phase"] == "ready_for_human":
            state.update({"status": "ready_for_human", "terminal_kind": "waiting_human"})
        return self._save(state)

    def _review(self, state: dict[str, Any], run: dict[str, Any]) -> bool:
        budget_policy = self._run_budget_policy(state, run)
        if isinstance(run.get("run_integration"), dict) and not run_integration_record_is_valid(self, state):
            raise ValueError("Run Acceptance requires the current integrated Run Branch")
        resume_initial_credential_wait(
            state, work_subject=_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT
        )
        run_head = self.git.resolve(str(state["run_branch"]))
        pending_attempt = pending_semantic_attempt(run, role="reviewer")
        validation_attempt = review_ordinal(run)
        checkout = self._validation_checkout(state, validation_attempt)
        try:
            default_head = self._default_head(state)
            try:
                self.git.prepare_expected_merge_checkout(
                    default_head_sha=default_head,
                    run_head_sha=run_head,
                    checkout=checkout,
                )
            except MergeConflictError as error:
                run["phase"] = "repairing"
                run["repair_request"] = {
                    "repair_source": "merge_conflict",
                    "merge_conflict_evidence": str(error),
                }
                state.update(
                    {
                        "status": "run_acceptance_pending",
                        "terminal_kind": "run_repair_pending",
                        "diagnostics": [],
                    }
                )
                self._save(state)
                return True
            expected_merge_tree = self.git.expected_merge_tree(
                default_head_sha=default_head,
                run_head_sha=run_head,
            )
            request = self._review_request(
                state, run, checkout, run_head, default_head, expected_merge_tree
            )
            boundary = run_currentness_boundary(
                state,
                reviewed_head_sha=run_head,
                reviewed_default_base_sha=default_head,
                expected_merge_tree=expected_merge_tree,
            )
            semantic_attempt = begin_review(
                run,
                work_subject=f"run-acceptance:{state['run_id']}",
                generation=int(run["acceptance_generation"]),
                boundary=boundary,
                budget_window=int(ensure_budget(run, budget_policy)["window"]),
            )
            request["review_budget_context"] = reviewer_budget_context(
                run,
                budget_policy,
                current_attempt_consumed=(
                    semantic_attempt.get("budget_consumed") is True
                ),
            )
            context = prepare_review_context(request, semantic_attempt, run, self.git, state)
            run["phase"] = "reviewing"
            self._save(state)
            is_current = lambda: (
                self._refresh_run_currentness(state)
                and
                (not isinstance(run.get("run_integration"), dict) or run_integration_remote_is_current(self, state))
                and
                self.git.resolve(str(state["run_branch"])) == run_head
                and self._default_head(state) == default_head
                and self._mapping(state, "parent")["revision"]
                == request["parent"]["revision"]
                and self._mapping(state, "ticket_graph")["revision"]
                == request["ticket_graph"]["revision"]
                and ticket_completion_records(state)
                == request["ticket_completion_records"]
            )
            try:
                review = cast(ReviewResult, execute_agent_work(
                    state, run, request, semantic_attempt,
                    agents=self.agents, save=self._save, phase="run_acceptance",
                    boundary=boundary, is_current=is_current,
                ))
            except InitialCredentialUnavailable as error:
                # This reviewer has not started: keep the same Acceptance
                # generation and make the next Driver pass retry only its
                # first read credential, not an interrupted Worker attempt.
                run["phase"] = "pending"
                if pending_attempt is None:
                    run["validation_attempts"] = validation_attempt - 1
                    release_semantic_attempt(run)
                wait_for_initial_credential(
                    state,
                    work_subject=_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT,
                    phase="run_acceptance",
                    resume_status="run_acceptance_pending",
                    http_status=error.http_status,
                )
                self._save(state)
                return False
            clear_initial_credential_wait(
                state, work_subject=_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT
            )
            self._retain_review_return(state, run, review, context)
        finally:
            self.git.remove_worktree(checkout)
        return self._complete_review(state, run)

    def _retain_review_return(
        self, state: dict[str, Any], run: dict[str, Any],
        review: ReviewResult, context: dict[str, Any],
    ) -> None:
        # Count a valid audit exactly once, even if live authority later
        # invalidates its verdict. This context belongs to the original call.
        policy = self._run_budget_policy(state, run)
        artifact = consume_review_result(run, policy, review.artifact)
        run["pending_review_result"] = {
            "record": run_acceptance_record(context, review.thread_id, artifact.raw),
            "human_blocker_resume": bool(run.get("prior_human_blockers")),
        }
        run["phase"] = "reviewing"
        self._record_reviewer(state, run, review.thread_id)

    def _complete_review(self, state: dict[str, Any], run: dict[str, Any]) -> bool:
        pending = self._mapping(run, "pending_review_result")
        record = self._mapping(pending, "record")
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
            self._save(state)
            return False
        run.update({"reviewed_head_sha": run_head,
                    "acceptance_artifact": artifact.raw, "acceptance_record": record})
        if not artifact.requires_human:
            self._record_final_pr_status(state, artifact.raw, run_head)
        finish_review_result(
            state, run, artifact, resume_phase="pending", blocked_phase="ready_for_human",
        )
        decision = after_review(run, budget_policy, artifact)
        if artifact.is_accepted:
            run["phase"] = "accepted"
        elif not artifact.requires_human:
            if decision in {ReviewDecision.REVIEW_EXHAUSTED, ReviewDecision.DEVELOPMENT_EXHAUSTED}:
                RunDevelopmentReview(self, state, run).checkpoint(decision.value)
            else:
                run["phase"] = "repairing"
        self._save(state)
        return True

    def _refresh_run_currentness(self, state: dict[str, Any]) -> bool:
        """Re-read GitHub before applying a Reviewer result when configured."""
        require_completed_ticket_integration_records(state)
        if self.currentness_reader is None:
            return True
        default_head = refresh_run_currentness(
            state, reader=self.currentness_reader, git=self.git
        )
        if default_head is None:
            return False
        self.default_head_sha = default_head
        self.candidate_acceptance.update_default_head(default_head)
        return True

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
        checks = self._mapping(artifact, "checks")
        outcome = AcceptanceArtifact.parse(artifact).outcome
        self.github.record_agent_run_status(
            pr_number,
            {
                "scope": "final-run",
                "base_sha": self._default_head(state),
                "candidate_sha": run_head,
                "validation_outcome": outcome,
                "lane_statuses": {
                    lane: str(self._mapping(checks, lane)["status"])
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

    def _repair(self, state: dict[str, Any], run: dict[str, Any]) -> str:
        return self._repair_lifecycle._repair(state, run)

    def _render_run_repair_pr_body(
        self, state: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        return self._repair_lifecycle._render_run_repair_pr_body(state, publication)

    def _prepare_repair_validation(
        self, checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        self._repair_lifecycle._prepare_repair_validation(checkout, job, validation)

    def _escalate_repair(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        self._repair_promotion._escalate_repair(state, job, code)

    def _sync_repair_cycle_counters(
        self, run: dict[str, Any], job: dict[str, Any]
    ) -> None:
        self._repair_lifecycle._sync_repair_cycle_counters(run, job)

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

    @staticmethod
    def _run_budget_policy(
        state: dict[str, Any], run: dict[str, Any]
    ) -> ReviewBudgetPolicy:
        return run_repair_budget_policy_for_job(
            run, state_snapshot=state.get("policy_snapshot")
        )

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
            record, run_review_identity(state, self.git, self._default_head(state), current_head),
        ):
            return
        invalidate_run_acceptance(state)

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
            "parent_issue_url": self._issue_url(
                state, int(self._mapping(state, "parent")["number"])
            ),
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": dict(self._mapping(state, "parent")),
            "ticket_graph": deepcopy(self._mapping(state, "ticket_graph")),
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
        if previous is None:
            history = run.get("review_budget_history")
            if isinstance(history, list) and history and isinstance(history[-1], dict):
                previous = previous_review_context(history[-1])
        if previous is not None:
            request["previous_acceptance_artifact"] = previous["artifact"]
            request["previous_review_identity"] = previous["identity"]
        return request

    def _candidate_promotion_record(
        self, state: dict[str, Any], job: dict[str, Any], integrated: str
    ) -> dict[str, Any] | None:
        return self.candidate_acceptance.promotion_record(state, job, integrated)

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
        reviewers = self._string_list(run, "reviewer_thread_ids")
        if thread_id not in reviewers:
            reviewers.append(thread_id)
        run["reviewer_thread_ids"] = reviewers
        run.pop("reviewer_resume_thread_id", None)
        run.pop("reviewer_new_thread", None)
        self._save(state)

    def _all_tickets_completed(self, state: dict[str, Any]) -> bool:
        require_completed_ticket_integration_records(state)
        order = self._mapping(state, "ticket_graph").get("ordered_ticket_numbers")
        jobs = self._mapping(state, "ticket_jobs")
        return isinstance(order, list) and bool(order) and all(
            isinstance(jobs.get(str(number)), dict)
            and jobs[str(number)].get("phase") == "completed"
            for number in order
        )

    def _validation_checkout(self, state: dict[str, Any], attempt: int) -> Path:
        return self.states.root / "worktrees" / str(state["run_id"]) / f"validation-run-{attempt}"

    def _repair_checkout(self, state: dict[str, Any]) -> Path:
        return self.states.root / "worktrees" / str(state["run_id"]) / "run-repair"

    def _default_head(self, state: dict[str, Any]) -> str:
        return self.default_head_sha or str(self._mapping(state, "base")["sha"])

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
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key} must contain strings")
        return list(value)

    @staticmethod
    def _issue_url(state: dict[str, Any], number: int) -> str:
        repository = state.get("repository")
        if not isinstance(repository, str) or not repository:
            raise ValueError("repository must be a non-empty string")
        return f"https://github.com/{repository}/issues/{number}"

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(str(state["run_id"]), state)
        return state
