from __future__ import annotations

from copy import deepcopy
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
from agent_run.change_delivery import (
    ChangeDeliveryEngine,
    ChangeDeliveryStateStore,
    ChangeJobContract,
    ensure_change_branch_authority,
    latest_reviewer_thread,
)
from agent_run.credential_availability import (
    clear_initial_credential_wait,
    resume_initial_credential_wait,
    wait_for_initial_credential,
)
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.human_responses import current_human_response_history
from agent_run.revisions import effective_revision
from agent_run.run_currentness import (
    MAX_CANDIDATE_ACCEPTANCE_HISTORY,
    RunCurrentnessReader,
    invalidate_run_acceptance,
    invalidate_stale_run_repair,
    refresh_run_currentness,
    run_currentness_boundary,
    ticket_completion_records,
)
from agent_run.run_candidate_acceptance import CandidateRunAcceptance
from agent_run.run_repair_cycle import (
    escalate_repair,
    repair_checkout_is_active,
    rotate_repair_job,
    start_repair_cycle,
    sync_repair_cycle_counters,
)
from agent_run.run_repair_currentness import (
    RunRepairCurrentness,
    RunRepairObservationPending,
)
from agent_run.run_repair_delivery import (
    RunRepairAdapter,
    RunRepairJobRotationRequired,
    RunRepairPublisher,
)
from agent_run.run_repair_requests import RunRepairRequests
from agent_run.state import StateStore
from agent_run.state_contract import require_candidate_acceptance_history
from agent_run.worker_credentials import InitialCredentialUnavailable


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
                if not self._review(state, run):
                    return self._save(state)

    def _review(self, state: dict[str, Any], run: dict[str, Any]) -> bool:
        resume_initial_credential_wait(
            state, work_subject=_RUN_ACCEPTANCE_CREDENTIAL_SUBJECT
        )
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
            request = self._review_request(
                state, run, checkout, run_head, default_head
            )
            if run.get("reviewer_new_thread") is True:
                request["_invocation_mode"] = "new-thread"
            request["_invocation_event"] = invocation_event_recorder(
                state,
                role="reviewer",
                phase="run_acceptance",
                work_subject=f"run-acceptance:{state['run_id']}",
                generation=int(run["acceptance_generation"]),
                invocation_input=request,
                currentness_boundary=run_currentness_boundary(
                    state,
                    reviewed_head_sha=run_head,
                    reviewed_default_base_sha=default_head,
                    expected_merge_tree=expected_merge_tree,
                ),
                save=self._save,
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
                run["validation_attempts"] = validation_attempt - 1
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
            run.pop("reviewer_new_thread", None)
            # The reviewer has already consumed this identity even if a live
            # authority refresh discards its verdict.  Keep it unavailable to
            # the fresh Acceptance that follows a drift.
            self._record_reviewer(state, run, review.thread_id)
            if not request["_currentness_check"]():
                run["phase"] = "pending"
                self._save(state)
                return False
        finally:
            self.git.remove_worktree(checkout)
        run.pop("review_new_thread", None)
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
        if artifact.is_accepted:
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
            clear_current_human_blocker(run)
            run["phase"] = "repairing"
        if not artifact.requires_human:
            self._record_final_pr_status(state, artifact.raw, run_head)
        self._save(state)
        return True

    def _refresh_run_currentness(self, state: dict[str, Any]) -> bool:
        """Re-read GitHub before applying a Reviewer result when configured."""
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
        """Deliver a Run Repair through the same Change Job lifecycle as Tickets.

        The repair remains deliberately separate from a Ticket: it receives a
        Run-Repair branch and PR.  Its completion callback promotes a strictly
        matching Candidate Acceptance directly to Run Publication; any mismatch
        instead discards that Candidate and starts a fresh overall acceptance.
        The shared engine owns every candidate, publication, fresh-review,
        checks, exact-head, and squash transition in between.
        """
        if self.github is None:
            raise ValueError("Run Repair requires the Publisher")
        checkout = self._repair_checkout(state)
        job: dict[str, Any] | None = None
        preserve_checkout = False
        try:
            while True:
                job = self._repair_job(state, run)
                self._complete_pending_repair_job_rotation(state, job, checkout)
                branch = str(job["repair_branch"])
                repair_is_integrated = isinstance(job.get("integrated_sha"), str)
                if not repair_is_integrated:
                    ensure_change_branch_authority(
                        github=self.github,
                        state=state,
                        job=job,
                        branch=branch,
                        base_branch=str(state["run_branch"]),
                        save=self._save,
                    )
                job["repair_checkout"] = str(checkout)
                self._sync_repair_cycle_counters(run, job)
                self.git.prepare_ticket_checkout(
                    branch=branch, base_sha=str(job["base_sha"]), checkout=checkout
                )
                try:
                    self._repair_engine(state, job).run(state, job, checkout)
                except RunRepairJobRotationRequired as rotation:
                    job = rotate_repair_job(
                        run,
                        job,
                        candidate_sha=rotation.candidate_sha,
                        base_sha=rotation.base_sha,
                        repair_branch=rotation.repair_branch,
                        repair_job_attempt=rotation.repair_job_attempt,
                        modification_attempt=rotation.modification_attempt,
                        repair_checkout=str(checkout),
                    )
                    self._save(state)
                    preserve_checkout = True
                    continue
                self._sync_repair_cycle_counters(run, job)
                preserve_checkout = self._repair_checkout_is_active(job)
                break
        except RunRepairObservationPending:
            preserve_checkout = True
            return "waiting"
        finally:
            if job is not None and self._repair_checkout_is_active(job):
                preserve_checkout = True
            if not preserve_checkout:
                self.git.remove_worktree(checkout)
                self._remove_empty_directories(checkout)
        assert job is not None
        if job["phase"] == "completed":
            completed_repairs = run.get("completed_repair_jobs", [])
            if not isinstance(completed_repairs, list) or not all(
                isinstance(completed, dict) for completed in completed_repairs
            ):
                raise ValueError("completed_repair_jobs must contain objects")
            DeliveryCleanupEngine(
                git=self.git, states=self.states, github=self.github
            ).complete_run_repairs(state, completed_repairs)
            return "promoted"
        if job["phase"] == "blocked":
            return (
                "no_code_changes"
                if job.get("blocked_reason") == "no_code_changes"
                else "blocked"
            )
        if job["phase"] == "stale":
            return "stale"
        return "waiting"

    def _repair_engine(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> ChangeDeliveryEngine:
        github = self.github
        requests = self.repair_requests
        currentness = self.repair_currentness
        if github is None or requests is None or currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        return ChangeDeliveryEngine(
            git=self.git,
            github=github,
            agents=self.agents,
            contract=ChangeJobContract(
                label=f"run-repair-{job['repair_attempt']}",
                branch=str(job["repair_branch"]),
                base_branch=str(state["run_branch"]),
            ),
            adapter=RunRepairAdapter(
                git=self.git,
                requests=requests,
                candidate_acceptance=self.candidate_acceptance,
                currentness=currentness,
                currentness_reader=self.currentness_reader,
                default_head_sha=self.default_head_sha,
            ),
            publisher=RunRepairPublisher(self),
            state_store=ChangeDeliveryStateStore(self.states, str(state["run_id"])),
        )

    def _repair_job(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
    ) -> dict[str, Any]:
        existing = run.get("repair_job")
        if isinstance(existing, dict):
            self._complete_pending_repair_trigger(state, existing)
            return existing
        base_sha = self.git.resolve(str(state["run_branch"]))
        generation = int(run.get("repair_generation", 0)) + 1
        run["repair_generation"] = generation
        initial_modifications = 0
        initial_validations = 0
        start_repair_cycle(run, generation)
        # The generic engine only knows a job's own thread history.  Seed it
        # with every prior Ticket and Run identity so a repair reviewer cannot
        # accidentally reuse any of them.
        prior_thread_set = self._all_prior_threads(state, run)
        development_thread_id = None
        development_thread_history: list[str] = []
        prior_threads = sorted(prior_thread_set)
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
            "repair_attempt": generation,
            "repair_job_attempt": 1,
            "repair_generation": generation,
            "repair_branch": (
                f"agent-run-repair/{state['run_id']}/{generation}"
                if generation == 1
                else f"agent-run-repair/{state['run_id']}/{generation}-generation-{generation}"
            ),
            "base_sha": base_sha,
            "default_base_sha": self._default_head(state),
            "repair_base_run_head_sha": base_sha,
            "parent_revision": self._mapping(state, "parent")["revision"],
            "ticket_graph_revision": self._mapping(state, "ticket_graph")["revision"],
            "ticket_completion_records": ticket_completion_records(state),
            "repair_source": repair_source,
            "repair_input_artifact": self._mapping(run, "acceptance_artifact"),
            "acceptance_artifact": self._mapping(run, "acceptance_artifact"),
            "modification_attempts": initial_modifications,
            "code_modification_attempts": initial_modifications,
            "validation_attempts": initial_validations,
            "acceptance_generation": 1,
            "development_thread_id": development_thread_id,
            "development_thread_history": development_thread_history,
            "reviewer_thread_ids": prior_threads,
            "prior_reviewer_thread_ids": prior_threads,
            "candidate_acceptance_history": [],
            "repair_checkout": str(self._repair_checkout(state)),
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
        job["repair_trigger_pending"] = True
        run["repair_job"] = job
        self._sync_repair_cycle_counters(run, job)
        self._save(state)
        self._complete_pending_repair_trigger(state, job)
        return job

    def _complete_pending_repair_job_rotation(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        raw_rotation = job.get("repair_job_rotation")
        if raw_rotation is None:
            return
        rotation = self._mapping(job, "repair_job_rotation")
        current_branch = rotation.get("current_branch")
        next_branch = rotation.get("next_branch")
        candidate_sha = rotation.get("candidate_sha")
        if (
            not isinstance(current_branch, str)
            or not current_branch
            or not isinstance(next_branch, str)
            or not next_branch
            or not isinstance(candidate_sha, str)
            or not candidate_sha
        ):
            raise ValueError("repair_job_rotation has invalid identity")
        if (
            next_branch != job.get("repair_branch")
            or candidate_sha != job.get("candidate_sha")
        ):
            raise ValueError("repair_job_rotation does not match the active Job")
        RunRepairPublisher(self).rotate_job_checkout(
            checkout=checkout,
            current_branch=current_branch,
            next_branch=next_branch,
            candidate_sha=candidate_sha,
        )
        job.pop("repair_job_rotation", None)
        self._save(state)

    def _complete_pending_repair_trigger(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None:
        if job.get("repair_trigger_pending") is not True:
            return
        repair_source = str(job["repair_source"])
        repair_request: dict[str, Any] = {}
        if repair_source == "required_checks":
            repair_request["ci_evidence"] = self._mapping(job, "ci_evidence")
        trigger = self._repair_trigger(state, repair_source, repair_request)
        if trigger is not None:
            job["repair_trigger"] = trigger
        job.pop("repair_trigger_pending", None)
        self._save(state)

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
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "run",
            "parent_issue_url": self._issue_url(
                state, int(self._mapping(state, "parent")["number"])
            ),
            "run_id": state["run_id"],
            "parent": dict(self._mapping(state, "parent")),
            "ticket_graph": self._mapping(state, "ticket_graph"),
            "ticket_completion_records": ticket_completion_records(state),
            "base_sha": default_head,
            "run_head_sha": run_head,
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
                and not run.get("review_new_thread")
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

    def _invalidate_stale_repair(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        current_default = (
            self.candidate_acceptance.default_head_sha
            or str(self._mapping(state, "base")["sha"])
        )
        currentness = self.repair_currentness
        if (
            currentness is not None
            and current_default != job.get("default_base_sha")
            and not currentness.non_default_revision_changed(
                state, job, default_head_sha=current_default
            )
        ):
            self._rebind_repair_to_default(state, job, current_default)
            return
        self.git.remove_worktree(checkout)
        self._remove_empty_directories(checkout)
        invalidate_stale_run_repair(state)
        job["phase"] = "stale"

    def _rebind_repair_to_default(
        self, state: dict[str, Any], job: dict[str, Any], default_head: str
    ) -> None:
        """Freeze current work and revalidate it against an advanced default head."""

        prior_phase = str(job["phase"])
        publication_was_integrated = (
            isinstance(job.get("integrated_sha"), str)
            and job.get("integrated_publication_sha") == job.get("publication_sha")
        )
        job["default_base_sha"] = default_head
        self.default_head_sha = default_head
        self.candidate_acceptance.update_default_head(default_head)
        stale_keys = [
            "acceptance_record",
            "acceptance_artifact",
            "merge_intent",
            "ticket_write_intent",
            "review_resume_thread_id",
            "review_human_blocker_resume",
            "review_new_thread",
            "pending_review_result",
        ]
        if not publication_was_integrated:
            stale_keys.extend(("publication", "publication_sha"))
        for key in stale_keys:
            job.pop(key, None)
        if prior_phase != "committing_candidate" and isinstance(
            job.get("candidate_sha"), str
        ):
            job["phase"] = "candidate"
        run = self._run_state(state)
        run["phase"] = "repairing"
        self._sync_repair_cycle_counters(run, job)
        publication = state.get("run_publication")
        if isinstance(publication, dict) and publication.get("phase") not in {
            "merged",
            "abandoned",
        }:
            publication["phase"] = "stale"
            for key in ("artifact", "write_intent", "approval_grant"):
                publication.pop(key, None)
        state.update(
            {
                "status": "run_acceptance_pending",
                "terminal_kind": "run_repair_pending",
                "diagnostics": [],
            }
        )

    @staticmethod
    def _repair_checkout_is_active(job: dict[str, Any]) -> bool:
        return repair_checkout_is_active(job)

    @staticmethod
    def _sync_repair_cycle_counters(
        run: dict[str, Any], job: dict[str, Any]
    ) -> None:
        sync_repair_cycle_counters(run, job)

    def _render_run_repair_pr_body(
        self, state: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        requests = self.repair_requests
        if requests is None:
            raise ValueError("Run Repair requires the Publisher")
        return requests.render_pr_body(state, publication)
    def _prepare_repair_validation(
        self, _checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        self.candidate_acceptance.prepare_validation(job, validation)

    def _repair_trigger(
        self,
        state: dict[str, Any],
        repair_source: str,
        repair_request: dict[str, Any],
    ) -> dict[str, Any] | None:
        currentness = self.repair_currentness
        if currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        return currentness.create_trigger(state, repair_source, repair_request)
    def _repair_trigger_is_current(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        currentness = self.repair_currentness
        if currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        return currentness.trigger_is_current(
            state, job, default_head_sha=self._default_head(state)
        )
    def _after_repair_merge(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        _live_before_merge: dict[str, Any],
    ) -> bool:
        if self.github is None:
            raise ValueError("Run Repair requires the Publisher")
        candidate_history = require_candidate_acceptance_history(
            job.get("candidate_acceptance_history", []),
            "run_acceptance.repair_job.candidate_acceptance",
        )
        raw_run = state.get("run_acceptance")
        if not isinstance(raw_run, dict):
            raise ValueError("run_acceptance must be an object")
        run_history = require_candidate_acceptance_history(
            raw_run.get("candidate_acceptance_history", []),
            "run_acceptance.candidate_acceptance",
        )
        currentness = self.repair_currentness
        if currentness is None:
            raise ValueError("Run Repair requires the Publisher")
        live = currentness.live_pull_request(
            state,
            int(job["pr_number"]),
            waiting_for=f"merged Run Repair PR #{int(job['pr_number'])} promotion",
        )
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
            self._invalidate_stale_repair(state, job, self._repair_checkout(state))
            return False
        if not self._refresh_run_currentness(state):
            return False
        if not self._repair_trigger_is_current(state, job):
            self._invalidate_stale_repair(state, job, self._repair_checkout(state))
            return False
        promoted = self._candidate_promotion_record(state, job, integrated)
        if promoted is None:
            # A merged Candidate is not automatically an accepted Run.  Any
            # mismatch at this seam discards the Candidate and starts a fresh
            # Run Acceptance generation against the live authorities.
            self._invalidate_stale_repair(state, job, self._repair_checkout(state))
            return False
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
                "code_modification_attempts": int(
                    job.get("code_modification_attempts", job["modification_attempts"])
                ),
                "candidate_sha": job["candidate_sha"],
                "publication_sha": job["publication_sha"],
                "repair_pr_number": job["pr_number"],
                "integrated_sha": integrated,
                "reviewed_head_sha": integrated,
                "acceptance_record": promoted,
                "acceptance_artifact": promoted["artifact"],
                "phase": "accepted",
            }
        )
        cycle = run.get("repair_cycle")
        if isinstance(cycle, dict):
            cycle.update(
                {
                    "status": "promoted",
                    "promoted_candidate_sha": str(job["candidate_sha"]),
                    "integrated_sha": integrated,
                }
            )
        completed_repairs = run.setdefault("completed_repair_jobs", [])
        if not isinstance(completed_repairs, list):
            raise ValueError("completed_repair_jobs must be a list")
        completed = {
            "phase": "completed",
            "repair_branch": job["repair_branch"],
            "integrated_sha": integrated,
            "candidate_sha": job["candidate_sha"],
            "acceptance_state": "promoted",
        }
        run["candidate_acceptance_history"] = [
            *run_history,
            *deepcopy(candidate_history),
        ][-MAX_CANDIDATE_ACCEPTANCE_HISTORY:]
        display = job.get("linked_branch_display")
        if isinstance(display, dict):
            completed["linked_branch_display"] = dict(display)
        completed_repairs.append(completed)
        del completed_repairs[:-32]
        run.pop("repair_job", None)
        publication_state = state.get("run_publication")
        if isinstance(publication_state, dict) and publication_state.get("phase") not in {
            "merged",
            "abandoned",
        }:
            # The existing Final Run PR, if any, must receive a refreshed
            # narrative after the promoted boundary is durable.
            publication_state["phase"] = "stale"
            for key in ("artifact", "write_intent", "approval_grant"):
                publication_state.pop(key, None)
        state["status"] = "run_publication_pending"
        state["terminal_kind"] = "run_acceptance_passed"
        state["diagnostics"] = []
        return True

    def _candidate_promotion_record(
        self, state: dict[str, Any], job: dict[str, Any], integrated: str
    ) -> dict[str, Any] | None:
        return self.candidate_acceptance.promotion_record(state, job, integrated)

    def _escalate_repair(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        escalate_repair(state, self._run_state(state), job, code)

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
            thread_id in self._all_prior_threads(state, run) and not resumed
        ):
            raise ValueError("Run Acceptance requires a new Reviewer Thread")
        reviewers = self._string_list(run, "reviewer_thread_ids")
        if thread_id not in reviewers:
            reviewers.append(thread_id)
        run["reviewer_thread_ids"] = reviewers
        run.pop("reviewer_resume_thread_id", None)
        run.pop("reviewer_new_thread", None)
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
        discarded = run.get("discarded_repair_thread_ids")
        if discarded is not None:
            values.update(self._string_list(run, "discarded_repair_thread_ids"))
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
