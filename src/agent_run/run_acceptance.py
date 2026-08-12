from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.agent_invocation import select_publication_thread
from agent_run.artifacts import (
    AcceptanceArtifact,
    append_human_blocker_history,
    clear_current_human_blocker,
)
from agent_run.change_delivery import (
    MAX_MODIFICATION_ATTEMPTS,
    MAX_PUBLICATION_CONTEXT_ATTEMPTS,
    ChangeDeliveryEngine,
    ChangeJobContract,
    latest_reviewer_thread,
)
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.human_responses import current_human_response_history
from agent_run.revisions import effective_revision
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
    ) -> None:
        self.git = git
        self.states = states
        self.agents = agents
        self.default_head_sha = default_head_sha
        self.github = github

    def accept(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self.states.load_run(run_id)
            if state is None:
                raise ValueError(f"unknown Delivery Run: {run_id}")
            if not self._all_tickets_completed(state):
                raise ValueError("Run Acceptance requires every Ticket to be completed")
            run = self._run_state(state)
            self._invalidate_stale_acceptance(state, run)
            while True:
                phase = str(run["phase"])
                if phase == "reviewing":
                    # A process can die after persisting the attempt marker but
                    # before the reviewer returns. No verdict exists yet, so a
                    # later command must start a fresh attempt rather than get
                    # stuck on an in-flight transient state.
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
                        self._save(state)
                        continue
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
                    run["phase"] = "pending"
                    run.pop("acceptance_record", None)
                    run.pop("acceptance_artifact", None)
                    self._save(state)
                    continue
                if phase != "pending":
                    raise ValueError(f"unknown Run Acceptance phase: {phase}")
                self._review(state, run)

    def _review(self, state: dict[str, Any], run: dict[str, Any]) -> None:
        run_head = self.git.resolve(str(state["run_branch"]))
        validation_attempt = int(run.get("validation_attempts", 0)) + 1
        run["validation_attempts"] = validation_attempt
        run["phase"] = "reviewing"
        self._save(state)
        checkout = self._validation_checkout(state, validation_attempt)
        try:
            default_head = self._default_head(state)
            self.git.prepare_expected_merge_checkout(
                default_head_sha=default_head,
                run_head_sha=run_head,
                checkout=checkout,
            )
            expected_merge_tree = self.git.expected_merge_tree(
                default_head_sha=default_head,
                run_head_sha=run_head,
            )
            review = self.agents.review(
                self._review_request(state, run, checkout, run_head, default_head)
            )
        finally:
            self.git.remove_worktree(checkout)
        self._record_reviewer(state, run, review.thread_id)
        artifact = AcceptanceArtifact.parse(review.artifact)
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
        if artifact.verdict == "pass":
            clear_current_human_blocker(run)
            run["phase"] = "accepted"
        elif artifact.verdict == "human":
            append_human_blocker_history(
                run, phase="pending", blockers=artifact.human_blockers
            )
            run.update(
                {
                    "phase": "ready_for_human",
                    "blocked_reason": "reviewer_requires_human",
                    "human_blockers": list(artifact.human_blockers),
                    "human_blocker_phase": "pending",
                }
            )
            state.update(
                {
                    "status": "ready_for_human",
                    "terminal_kind": "waiting_human",
                    "diagnostics": [
                        {"code": "reviewer_requires_human", "message": blocker}
                        for blocker in artifact.human_blockers
                    ],
                }
            )
        elif int(run["modification_attempts"]) >= MAX_MODIFICATION_ATTEMPTS:
            clear_current_human_blocker(run)
            run["phase"] = "ready_for_human"
            run["blocked_reason"] = "modification_budget_exhausted"
        else:
            clear_current_human_blocker(run)
            run["phase"] = "repairing"
        if artifact.verdict != "human":
            self._record_final_pr_status(state, artifact.raw, run_head)
        self._save(state)

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
        verdict = str(artifact["verdict"])
        self.github.record_agent_run_status(
            pr_number,
            {
                "scope": "final-run",
                "base_sha": self._default_head(state),
                "candidate_sha": run_head,
                "validation_verdict": verdict,
                "lane_statuses": {
                    lane: str(self._mapping(checks, lane)["status"])
                    for lane in ("e2e", "standards", "spec")
                },
                "required_checks": "not_checked",
                "next_action": {
                    "pass": "generate refreshed final Run publication",
                    "request_changes": "repair Fresh Validation findings",
                    "human": "await human decision",
                }[verdict],
            },
        )

    def _repair(self, state: dict[str, Any], run: dict[str, Any]) -> str:
        """Deliver a Run Repair through the same Change Job lifecycle as Tickets.

        The repair remains deliberately separate from a Ticket: it receives a
        Run-Repair branch and PR, and its completion callback only returns the
        Run to a wholly fresh overall acceptance.  The shared engine owns every
        candidate, publication, fresh-review, checks, exact-head, and squash
        transition in between.
        """
        if self.github is None:
            raise ValueError("Run Repair requires the Publisher")
        job = self._repair_job(state, run)
        branch = str(job["repair_branch"])
        self.github.ensure_run_repair_branch(
            branch=branch, base_branch=str(state["run_branch"])
        )
        checkout = self._repair_checkout(state)
        preserve_checkout = False
        try:
            self.git.prepare_ticket_checkout(
                branch=branch, base_sha=str(job["base_sha"]), checkout=checkout
            )
            self._repair_engine().run(state, job, checkout)
            preserve_checkout = (
                job.get("blocked_reason") == "agent_requires_human"
                and job.get("human_blocker_phase") in {"developing", "repairing"}
            )
        finally:
            if not preserve_checkout:
                self.git.remove_worktree(checkout)
                self._remove_empty_directories(checkout)
        if job["phase"] == "completed":
            DeliveryCleanupEngine(
                git=self.git, states=self.states, github=self.github
            ).complete_run_repair(state, job)
            return "merged"
        if job["phase"] == "blocked":
            return (
                "no_code_changes"
                if job.get("blocked_reason") == "no_code_changes"
                else "blocked"
            )
        if job["phase"] == "stale":
            return "stale"
        return "waiting"

    def _repair_engine(self) -> ChangeDeliveryEngine:
        github = self.github
        if github is None:
            raise ValueError("Run Repair requires the Publisher")
        return ChangeDeliveryEngine(
            git=self.git,
            github=github,
            agents=self.agents,
            contract=ChangeJobContract(
                label=lambda job: f"run-repair-{job['repair_attempt']}",
                branch=lambda job: str(job["repair_branch"]),
                base_branch=lambda state: str(state["run_branch"]),
                candidate=lambda checkout, _job, attempt: self.git.commit_run_repair_candidate(
                    checkout, attempt=attempt
                ),
                development_thread_is_allowed=lambda _state, thread_id: thread_id
                not in self._all_prior_threads(_state, self._run_state(_state)),
                development_request=self._development_request,
                publication_request=self._publication_request,
                review_request=self._repair_review_request,
                prepare_validation=self._prepare_repair_validation,
                ensure_pr=lambda state, job, publication: github.ensure_run_repair_pr(
                    branch=str(job["repair_branch"]),
                    base_branch=str(state["run_branch"]),
                    title=str(publication["pr_title"]),
                    body=self._render_run_repair_pr_body(state, publication),
                ),
                acceptance_record=self._repair_acceptance_record,
                acceptance_is_current=self._repair_acceptance_is_current,
                invalidate_stale_publication=self._invalidate_stale_repair_publication,
                revision_changed=self._repair_revision_changed,
                requires_explicit_approval=lambda _state, _job: False,
                after_merge=self._after_repair_merge,
                escalate=self._escalate_repair,
                save=self._save,
            ),
        )

    def _repair_job(self, state: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        existing = run.get("repair_job")
        if isinstance(existing, dict):
            return existing
        base_sha = self.git.resolve(str(state["run_branch"]))
        attempt = int(run["modification_attempts"]) + 1
        generation = int(run.get("repair_generation", 0)) + 1
        run["repair_generation"] = generation
        # The generic engine only knows a job's own thread history.  Seed it
        # with every prior Ticket and Run identity so a repair reviewer cannot
        # accidentally reuse any of them.
        prior_threads = sorted(self._all_prior_threads(state, run))
        repair_request = run.pop("repair_request", {})
        if not isinstance(repair_request, dict):
            raise ValueError("repair_request must be an object")
        repair_source = str(repair_request.get("repair_source", "acceptance"))
        if repair_source not in {
            "acceptance",
            "human_revision",
            "required_checks",
            "merge_conflict",
        }:
            raise ValueError("invalid Run Repair source")
        job = {
            "run_id": state["run_id"],
            "phase": "developing",
            "repair_attempt": attempt,
            "repair_generation": generation,
            "repair_branch": (
                f"agent-run-repair/{state['run_id']}/{attempt}"
                if generation == 1
                else f"agent-run-repair/{state['run_id']}/{attempt}-generation-{generation}"
            ),
            "base_sha": base_sha,
            "parent_revision": self._mapping(state, "parent")["revision"],
            "ticket_graph_revision": self._mapping(state, "ticket_graph")["revision"],
            "ticket_completion_records": self._ticket_completion_records(state),
            "repair_source": repair_source,
            "acceptance_artifact": self._mapping(run, "acceptance_artifact"),
            "modification_attempts": int(run["modification_attempts"]),
            "validation_attempts": 0,
            "development_thread_id": None,
            "development_thread_history": [],
            "reviewer_thread_ids": prior_threads,
            "prior_reviewer_thread_ids": prior_threads,
        }
        if repair_source == "human_revision":
            feedback = repair_request.get("human_feedback")
            if not isinstance(feedback, str) or not feedback.strip():
                raise ValueError("human revision feedback must be non-empty")
            job["human_feedback"] = feedback
        if repair_source == "required_checks":
            evidence = repair_request.get("ci_evidence")
            if not isinstance(evidence, dict):
                raise ValueError("required-check repair evidence must be an object")
            job["ci_evidence"] = evidence
        if repair_source == "merge_conflict":
            evidence = repair_request.get("merge_conflict_evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise ValueError("merge-conflict repair evidence must be non-empty")
            job["merge_conflict_evidence"] = evidence
        run["repair_job"] = job
        self._save(state)
        return job

    def _run_state(self, state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get("run_acceptance")
        if isinstance(existing, dict):
            return existing
        run = {
            "phase": "pending",
            "modification_attempts": 0,
            "validation_attempts": 0,
            "development_thread_id": None,
            "development_thread_history": [],
            "reviewer_thread_ids": [],
        }
        state["run_acceptance"] = run
        return run

    def _invalidate_stale_acceptance(
        self, state: dict[str, Any], run: dict[str, Any]
    ) -> None:
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
            == self._ticket_completion_records(state)
        ):
            return
        for key in ("acceptance_record", "acceptance_artifact", "reviewed_head_sha"):
            run.pop(key, None)
        run["phase"] = "pending"

    def _review_request(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        checkout: Path,
        run_head: str,
        default_head: str,
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "run",
            "parent_issue_url": self._issue_url(
                state, int(self._mapping(state, "parent")["number"])
            ),
            "run_id": state["run_id"],
            "parent": dict(self._mapping(state, "parent")),
            "ticket_graph": self._mapping(state, "ticket_graph"),
            "ticket_completion_records": self._ticket_completion_records(state),
            "base_sha": default_head,
            "run_head_sha": run_head,
            "expected_merge_result": {
                "default_base_sha": default_head,
                "run_branch_head_sha": run_head,
                "inspection_command": f"git diff {default_head} {run_head}",
                "checkout_state": "merged working tree; HEAD remains default base",
            },
            "checkout": str(checkout),
            "thread_id": latest_reviewer_thread(run)
            if run.get("prior_human_blockers")
            else None,
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
                        generation=int(run.get("human_response_generation", 1)),
                    )
                )
                else {}
            ),
        }

    def _development_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "run",
            "repair_source": job.get("repair_source", "acceptance"),
            "parent_issue_url": self._issue_url(
                state, int(self._mapping(state, "parent")["number"])
            ),
            "run_id": state["run_id"],
            "parent": dict(self._mapping(state, "parent")),
            "ticket_graph": self._mapping(state, "ticket_graph"),
            "ticket_completion_records": self._ticket_completion_records(state),
            "base_sha": job["base_sha"],
            "head_sha": self.git.checkout_head(checkout),
            "checkout": str(checkout),
            "thread_id": None
            if job.get("development_new_thread")
            else job.get("development_thread_id"),
            "development_summary": job.get("development_summary"),
        }
        source = str(request["repair_source"])
        if source == "acceptance":
            request["acceptance_artifact"] = self._mapping(job, "acceptance_artifact")
        elif source == "required_checks":
            request["ci_evidence"] = self._mapping(job, "ci_evidence")
        elif source == "human_revision":
            request["human_feedback"] = str(job["human_feedback"])
        elif source == "merge_conflict":
            request["merge_conflict_evidence"] = str(job["merge_conflict_evidence"])
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job, generation=int(job.get("human_response_generation", job.get("repair_generation", 1)))
        ):
            request["human_response_history"] = history
        return request

    def _publication_request(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "run",
            "parent_issue_url": self._issue_url(
                state, int(self._mapping(state, "parent")["number"])
            ),
            "run_id": state["run_id"],
            "parent": self._mapping(state, "parent"),
            "ticket_graph": self._mapping(state, "ticket_graph"),
            "ticket_completion_records": self._ticket_completion_records(state),
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "checkout": str(checkout),
            "thread_id": select_publication_thread(
                job, max_context_attempts=MAX_PUBLICATION_CONTEXT_ATTEMPTS
            ),
            "acceptance_artifact": self._mapping(job, "acceptance_artifact"),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job,
            generation=int(
                job.get("human_response_generation", job.get("repair_generation", 1))
            ),
        ):
            request["human_response_history"] = history
        return request

    def _invalidate_stale_repair_publication(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        del checkout
        run = self._run_state(state)
        run.pop("repair_job", None)
        job["phase"] = "stale"
        state["status"] = "run_acceptance_pending"
        state["diagnostics"] = []

    def _render_run_repair_pr_body(
        self, state: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        parent = self._mapping(state, "parent")
        narrative = str(publication["pr_body_markdown"]).strip()
        return (
            f"Parent Issue: #{int(parent['number'])}\n"
            "Delivery Type: Run Repair\n\n"
            f"{narrative}"
        )

    def _prepare_repair_validation(
        self, _checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        # The repair reviewer must inspect the Run Branch with the repair
        # publication merged into it, not merely the repair branch by itself.
        self.git.prepare_expected_merge_checkout(
            default_head_sha=str(job["base_sha"]),
            run_head_sha=str(job["candidate_sha"]),
            checkout=validation,
        )

    def _repair_review_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "run",
            "parent_issue_url": self._issue_url(
                state, int(self._mapping(state, "parent")["number"])
            ),
            "repair_scope": "run_repair",
            "run_id": state["run_id"],
            "parent": dict(self._mapping(state, "parent")),
            "ticket_graph": self._mapping(state, "ticket_graph"),
            "ticket_completion_records": self._ticket_completion_records(state),
            "base_sha": job["base_sha"],
            "run_head_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "expected_merge_result": {
                "run_branch_head_sha": job["base_sha"],
                "repair_candidate_sha": job["candidate_sha"],
                "inspection_command": (
                    f"git diff {job['base_sha']} {job['candidate_sha']}"
                ),
                "checkout_state": "Run Branch plus repair publication merge preview; HEAD remains Run Branch base",
            },
            "checkout": str(checkout),
            "thread_id": latest_reviewer_thread(job)
            if (
                (job.get("prior_human_blockers") or job.get("review_resume_thread_id"))
                and not job.get("review_new_thread")
            )
            else None,
            **(
                {"prior_human_blockers": job["prior_human_blockers"]}
                if job.get("prior_human_blockers")
                else {}
            ),
            **(
                {"human_response_history": history}
                if (
                    history := current_human_response_history(
                        job,
                        generation=int(
                            job.get(
                                "human_response_generation",
                                job.get("repair_generation", 1),
                            )
                        ),
                    )
                )
                else {}
            ),
        }

    def _repair_acceptance_record(
        self,
        _state: dict[str, Any],
        job: dict[str, Any],
        reviewer_thread_id: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "run_repair",
            "reviewed_base_sha": job["base_sha"],
            "reviewed_candidate_sha": job["candidate_sha"],
            "reviewed_candidate_tree": self.git.resolve(
                f"{job['candidate_sha']}^{{tree}}"
            ),
            "parent_revision": job["parent_revision"],
            "ticket_graph_revision": job["ticket_graph_revision"],
            "ticket_completion_records": job["ticket_completion_records"],
            "reviewer_thread_id": reviewer_thread_id,
            "repair_source": self._mapping(job, "acceptance_artifact"),
            "artifact": artifact,
        }

    def _repair_acceptance_is_current(
        self, state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool:
        return (
            acceptance.get("reviewed_base_sha") == job.get("base_sha")
            and acceptance.get("reviewed_candidate_sha") == job.get("candidate_sha")
            and acceptance.get("reviewed_candidate_tree")
            == self.git.resolve(f"{job['candidate_sha']}^{{tree}}")
            and not self._repair_revision_changed(state, job)
        )

    def _repair_revision_changed(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        return (
            self.git.resolve(str(state["run_branch"])) != job.get("base_sha")
            or self._mapping(state, "parent").get("revision")
            != job.get("parent_revision")
            or self._mapping(state, "ticket_graph").get("revision")
            != job.get("ticket_graph_revision")
            or self._ticket_completion_records(state)
            != job.get("ticket_completion_records")
        )

    def _after_repair_merge(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        _live_before_merge: dict[str, Any],
    ) -> bool:
        if self.github is None:
            raise ValueError("Run Repair requires the Publisher")
        live = self.github.live_pull_request(int(job["pr_number"]))
        integrated = job.get("integrated_sha")
        publication = self._mapping(job, "publication")
        if (
            live.get("state") != "MERGED"
            or not isinstance(integrated, str)
            or live.get("integrated_sha") != integrated
            or live.get("head_sha") != job.get("publication_sha")
            or live.get("base_branch") != state.get("run_branch")
            or live.get("head_tree") != live.get("integrated_tree")
            or live.get("integrated_message") != publication.get("commit_message")
            or live.get("integrated_parents") != [job.get("base_sha")]
        ):
            return self._block_repair(
                state,
                job,
                "merged_result_mismatch",
                "Merged Run Repair PR does not match Publisher merge intent",
            )
        run = self._run_state(state)
        prior = set(self._string_list(job, "prior_reviewer_thread_ids"))
        reviewers = self._string_list(run, "reviewer_thread_ids")
        reviewers.extend(
            thread_id
            for thread_id in self._string_list(job, "reviewer_thread_ids")
            if thread_id not in prior and thread_id not in reviewers
        )
        run["reviewer_thread_ids"] = reviewers
        history = self._string_list(run, "development_thread_history")
        development_ids = [
            *self._string_list(job, "development_thread_history"),
            str(job.get("development_thread_id", "")),
        ]
        for thread_id in development_ids:
            if thread_id and thread_id not in history:
                history.append(thread_id)
        run["development_thread_history"] = history
        run.update(
            {
                "modification_attempts": int(job["modification_attempts"]),
                "candidate_sha": job["candidate_sha"],
                "publication_sha": job["publication_sha"],
                "repair_pr_number": job["pr_number"],
                "integrated_sha": integrated,
            }
        )
        completed_repairs = run.setdefault("completed_repair_jobs", [])
        if not isinstance(completed_repairs, list):
            raise ValueError("completed_repair_jobs must be a list")
        completed_repairs.append(
            {
                "phase": "completed",
                "repair_branch": job["repair_branch"],
                "integrated_sha": integrated,
            }
        )
        run.pop("repair_job", None)
        state["status"] = "run_acceptance_pending"
        state["diagnostics"] = []
        return True

    def _escalate_repair(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        run = self._run_state(state)
        run.update({"phase": "ready_for_human", "blocked_reason": code})
        state["status"] = "ready_for_human"
        state["terminal_kind"] = "waiting_human"
        state["diagnostics"] = [
            {
                "code": code,
                "message": "Run Repair requires explicit human intervention",
                "change_job": f"run-repair-{job['repair_attempt']}",
            }
        ]

    def _block_repair(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        code: str,
        message: str,
    ) -> bool:
        job.update({"phase": "blocked", "blocked_reason": code})
        state["status"] = "blocked"
        state["diagnostics"] = [
            {"code": code, "message": message, "change_job": f"run-repair-{job['repair_attempt']}"}
        ]
        self._save(state)
        return False

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
            "ticket_completion_records": self._ticket_completion_records(state),
            "reviewer_thread_id": reviewer_thread_id,
            "artifact": artifact,
        }

    def _record_reviewer(
        self, state: dict[str, Any], run: dict[str, Any], thread_id: str
    ) -> None:
        resumed = bool(run.get("prior_human_blockers"))
        if resumed and thread_id != latest_reviewer_thread(run):
            raise ValueError(
                "Human Blocker resume requires the latest Reviewer Thread"
            )
        if not thread_id.strip() or (
            thread_id in self._all_prior_threads(state, run) and not resumed
        ):
            raise ValueError("Run Acceptance requires a new Reviewer Thread")
        reviewers = self._string_list(run, "reviewer_thread_ids")
        if thread_id not in reviewers:
            reviewers.append(thread_id)
        run["reviewer_thread_ids"] = reviewers
        self._save(state)

    def _all_prior_threads(
        self, state: dict[str, Any], run: dict[str, Any]
    ) -> set[str]:
        values = set(self._string_list(run, "reviewer_thread_ids"))
        for key in ("development_thread_id",):
            value = run.get(key)
            if isinstance(value, str):
                values.add(value)
        values.update(self._string_list(run, "development_thread_history"))
        for job in self._mapping(state, "ticket_jobs").values():
            if not isinstance(job, dict):
                continue
            for key in ("development_thread_id",):
                value = job.get(key)
                if isinstance(value, str):
                    values.add(value)
            for key in ("development_thread_history", "reviewer_thread_ids"):
                value = job.get(key, [])
                if isinstance(value, list):
                    values.update(item for item in value if isinstance(item, str))
        return values

    def _ticket_completion_records(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        tickets = self._mapping(self._mapping(state, "ticket_graph"), "tickets")
        parent_revision = str(self._mapping(state, "parent")["revision"])
        graph_revision = str(self._mapping(state, "ticket_graph")["revision"])
        for key, job in sorted(self._mapping(state, "ticket_jobs").items()):
            if not isinstance(job, dict) or job.get("phase") != "completed":
                continue
            ticket = self._mapping(tickets, key)
            records.append(
                {
                    "ticket_number": int(key),
                    "integrated_sha": job.get("integrated_sha"),
                    "effective_revision": effective_revision(
                        ticket_revision=str(ticket["content_revision"]),
                        parent_revision=parent_revision,
                        graph_revision=graph_revision,
                    ),
                    "acceptance_record": job.get("acceptance_record"),
                }
            )
        return records

    def _all_tickets_completed(self, state: dict[str, Any]) -> bool:
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
