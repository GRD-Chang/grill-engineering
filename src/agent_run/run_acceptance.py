from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.agent_invocation import (
    fail_interrupted_invocation,
    invocation_event_recorder,
)
from agent_run.artifacts import (
    AcceptanceArtifact,
    append_human_blocker_history,
    clear_current_human_blocker,
)
from agent_run.change_delivery import latest_reviewer_thread
from agent_run.delivery_policy import invocation_deadline_for_state
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
    ticket_integration_records,
)
from agent_run.run_candidate_acceptance import CandidateRunAcceptance
from agent_run.run_repair_lifecycle import RunRepairLifecycle
from agent_run.run_repair_promotion import RunRepairPromotion
from agent_run.run_repair_currentness import RunRepairCurrentness
from agent_run.run_repair_requests import RunRepairRequests
from agent_run.run_thread_identity import prior_thread_identities
from agent_run.state import StateStore
from agent_run.integration_record_contract import (
    require_completed_ticket_integration_records,
)
from agent_run.worker_credentials import InitialCredentialUnavailable
from agent_run.review_budget import (
    RUN_POLICY,
    ensure_budget,
    mark_review,
    new_budget,
    previous_review_context,
)
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
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
        self._repair_lifecycle = RunRepairLifecycle(self)
        self._repair_promotion = RunRepairPromotion(self)

    def accept(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self.states.load_current_run(run_id)
            if state is None:
                raise ValueError(f"unknown Delivery Run: {run_id}")
            if not self._refresh_run_currentness(state):
                return self._save(state)
            if not self._all_tickets_completed(state):
                raise ValueError("Run Acceptance requires every Ticket to be completed")
            run = self._run_state(state)
            ensure_budget(run, RUN_POLICY)
            self._invalidate_stale_acceptance(state, run)
            while True:
                phase = str(run["phase"])
                if phase == "reviewing":
                    # A process can die after persisting the attempt marker but
                    # before the reviewer returns. No verdict exists yet, so a
                    # later command must start a fresh attempt rather than get
                    # stuck on an in-flight transient state.
                    if fail_interrupted_invocation(
                        state, role="reviewer", save=self._save
                    ):
                        return state
                    run["phase"] = "pending"
                    self._save(state)
                    continue
                if phase == "accepted":
                    state["status"] = "run_publication_pending"
                    state["terminal_kind"] = "run_acceptance_passed"
                    state["diagnostics"] = []
                    return self._save(state)
                if phase == "ready_for_human":
                    state["status"] = "ready_for_human"
                    state["terminal_kind"] = "waiting_human"
                    return self._save(state)
                if phase == "repairing":
                    repair = self._repair(state, run)
                    if repair == "waiting":
                        return self._save(state)
                    if repair == "stale":
                        run["phase"] = "pending"
                        run.pop("acceptance_record", None)
                        run.pop("acceptance_artifact", None)
                        # A discarded Repair establishes the fresh Acceptance
                        # boundary; it does not reuse this invocation to run it.
                        return self._save(state)
                    if repair == "no_code_changes":
                        run["phase"] = "ready_for_human"
                        run["blocked_reason"] = "no_code_changes"
                        state["diagnostics"] = [
                            {
                                "code": "no_code_changes",
                                "message": "Run Repair produced no code changes",
                            }
                        ]
                        continue
                    if repair == "blocked":
                        run["phase"] = "ready_for_human"
                        continue
                    if repair == "promoted":
                        # The Candidate Acceptance has already been promoted
                        # to the integrated Run boundary.  Run Publication is
                        # the next phase; do not ask a second full Run
                        # Reviewer to repeat the same three lanes.
                        return self._save(state)
                    run["phase"] = "pending"
                    run.pop("acceptance_record", None)
                    run.pop("acceptance_artifact", None)
                    self._save(state)
                    continue
                if phase != "pending":
                    raise ValueError(f"unknown Run Acceptance phase: {phase}")
                budget = ensure_budget(run, RUN_POLICY)
                if (
                    pending_semantic_attempt(run, role="reviewer") is None
                    and int(budget["reviewer_invocations"]) >= RUN_POLICY.review_limit
                ):
                    run["phase"] = "ready_for_human"
                    run["blocked_reason"] = "review_budget_exhausted"
                    budget["checkpoint_reason"] = "review_budget_exhausted"
                    state.update(
                        {
                            "status": "ready_for_human",
                            "terminal_kind": "waiting_human",
                            "diagnostics": [
                                {
                                    "code": "review_budget_exhausted",
                                    "message": "Run Acceptance review budget is exhausted; resume is required",
                                }
                            ],
                        }
                    )
                    return self._save(state)
                if not self._review(state, run):
                    return self._save(state)

    def _review(self, state: dict[str, Any], run: dict[str, Any]) -> bool:
        resume_initial_credential_wait(
            state, work_subject=_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT
        )
        run_head = self.git.resolve(str(state["run_branch"]))
        pending_attempt = pending_semantic_attempt(run, role="reviewer")
        validation_attempt = (
            int(pending_attempt["ordinal"])
            if pending_attempt is not None
            else int(run.get("validation_attempts", 0)) + 1
        )
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
            if pending_attempt is None:
                run["validation_attempts"] = validation_attempt
            semantic_attempt = allocate_semantic_attempt(
                run,
                role="reviewer",
                work_subject=f"run-acceptance:{state['run_id']}",
                generation=int(run["acceptance_generation"]),
                currentness_boundary=boundary,
                ordinal=validation_attempt,
                budget_window=int(ensure_budget(run, RUN_POLICY)["window"]),
            )
            run["phase"] = "reviewing"
            self._save(state)
            if run.get("reviewer_new_thread") is True:
                request["_invocation_mode"] = "new-thread"
            invocation_deadline_seconds = invocation_deadline_for_state(
                state, "reviewer"
            )
            request["_invocation_event"] = invocation_event_recorder(
                state,
                role="reviewer",
                phase="run_acceptance",
                work_subject=f"run-acceptance:{state['run_id']}",
                generation=int(run["acceptance_generation"]),
                invocation_input=request,
                currentness_boundary=boundary,
                semantic_attempt=semantic_attempt,
                save=self._save,
                invocation_deadline_seconds=invocation_deadline_seconds,
            )
            request["_currentness_check"] = lambda: (
                self._refresh_run_currentness(state)
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
                review = self.agents.review(request)
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
            # Only a returned Artifact that passes the strict schema parser
            # consumes a Reviewer invocation.  Count it before currentness
            # re-checking because the Reviewer did complete a valid audit even
            # when a live boundary refresh discards that verdict.
            artifact = AcceptanceArtifact.parse(review.artifact)
            if semantic_attempt.get("budget_consumed") is not True:
                mark_review(run, RUN_POLICY)
                semantic_attempt["budget_consumed"] = True
            # The reviewer has already consumed this identity even if a live
            # authority refresh discards its verdict.  Keep it unavailable to
            # the fresh Acceptance that follows a drift.
            self._record_reviewer(state, run, review.thread_id)
            if not request["_currentness_check"]():
                close_semantic_attempt(
                    run, semantic_attempt, outcome="currentness_invalidated"
                )
                detach_active_invocation(state, semantic_attempt)
                run["phase"] = "pending"
                self._save(state)
                return False
        finally:
            self.git.remove_worktree(checkout)
        run.pop("reviewer_new_thread", None)
        budget = ensure_budget(run, RUN_POLICY)
        budget["review_artifacts"].append(
            {
                "reviewer_thread_id": review.thread_id,
                "candidate_sha": run_head,
                "reviewed_base_sha": default_head,
                "expected_merge_tree": expected_merge_tree,
                "review_identity": {
                    "default_base_sha": default_head,
                    "run_head_sha": run_head,
                    "expected_merge_tree": expected_merge_tree,
                },
                "artifact": artifact.raw,
            }
        )
        del budget["review_artifacts"][:-5]
        record = self._acceptance_record(
            state,
            run_head,
            expected_merge_tree,
            review.thread_id,
            artifact.raw,
        )
        run.update(
            {
                "reviewed_head_sha": run_head,
                "acceptance_artifact": artifact.raw,
                "acceptance_record": record,
            }
        )
        if artifact.is_accepted:
            close_semantic_attempt(run, semantic_attempt, outcome="acceptance_artifact")
            clear_current_human_blocker(run)
            run["phase"] = "accepted"
        elif artifact.requires_human:
            append_human_blocker_history(
                run, phase="pending", blockers=artifact.blocker_evidence
            )
            run.update(
                {
                    "phase": "ready_for_human",
                    "blocked_reason": "reviewer_requires_human",
                    "human_blockers": list(artifact.blocker_evidence),
                    "human_blocker_phase": "pending",
                }
            )
            state.update(
                {
                    "status": "ready_for_human",
                    "terminal_kind": "waiting_human",
                    "diagnostics": [
                        {"code": "reviewer_requires_human", "message": blocker}
                        for blocker in artifact.blocker_evidence
                    ],
                }
            )
        else:
            close_semantic_attempt(run, semantic_attempt, outcome="acceptance_artifact")
            clear_current_human_blocker(run)
            budget = ensure_budget(run, RUN_POLICY)
            if int(budget["reviewer_invocations"]) >= RUN_POLICY.review_limit:
                run["phase"] = "ready_for_human"
                run["blocked_reason"] = "review_budget_exhausted"
                budget["checkpoint_reason"] = "review_budget_exhausted"
                state.update(
                    {
                        "status": "ready_for_human",
                        "terminal_kind": "waiting_human",
                        "diagnostics": [
                            {
                                "code": "review_budget_exhausted",
                                "message": "Run Acceptance review budget is exhausted; resume is required",
                            }
                        ],
                    }
                )
            else:
                run["phase"] = "repairing"
        if not artifact.requires_human:
            self._record_final_pr_status(state, artifact.raw, run_head)
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

    def _invalidate_stale_repair(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self._repair_promotion._invalidate_stale_repair(state, job, checkout)

    def _complete_integration_reprepare(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self._repair_promotion._complete_integration_reprepare(state, job, checkout)

    def _render_run_repair_pr_body(
        self, state: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        return self._repair_lifecycle._render_run_repair_pr_body(state, publication)

    def _prepare_repair_validation(
        self, checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        self._repair_lifecycle._prepare_repair_validation(checkout, job, validation)

    def _after_repair_merge(
        self, state: dict[str, Any], job: dict[str, Any], live: dict[str, Any]
    ) -> bool:
        return self._repair_promotion._after_repair_merge(state, job, live)

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
        }
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
        parent = self._mapping(state, "parent")
        graph = self._mapping(state, "ticket_graph")
        if (
            record.get("reviewed_head_sha") == current_head
            and record.get("reviewed_default_base_sha") == self._default_head(state)
            and record.get("parent_revision") == parent.get("revision")
            and record.get("ticket_graph_revision") == graph.get("revision")
            and record.get("ticket_completion_records")
            == ticket_completion_records(state)
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
            "ticket_graph": self._mapping(state, "ticket_graph"),
            "ticket_completion_records": ticket_completion_records(state),
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
        integrations = ticket_integration_records(state)
        if integrations:
            request["ticket_integration_records"] = integrations
        previous = previous_review_context(run)
        if previous is not None:
            request["previous_acceptance_artifact"] = previous["artifact"]
            request["previous_review_identity"] = previous["identity"]
        return request

    def _candidate_promotion_record(
        self, state: dict[str, Any], job: dict[str, Any], integrated: str
    ) -> dict[str, Any] | None:
        return self.candidate_acceptance.promotion_record(state, job, integrated)

    def _acceptance_record(
        self,
        state: dict[str, Any],
        run_head: str,
        expected_merge_tree: str,
        reviewer_thread_id: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "run",
            "reviewed_base_sha": self._default_head(state),
            "reviewed_default_base_sha": self._default_head(state),
            "reviewed_head_sha": run_head,
            "expected_merge_tree": expected_merge_tree,
            "parent_revision": self._mapping(state, "parent")["revision"],
            "ticket_graph_revision": self._mapping(state, "ticket_graph")["revision"],
            "ticket_completion_records": ticket_completion_records(state),
            "reviewer_thread_id": reviewer_thread_id,
            "artifact": artifact,
        }

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
