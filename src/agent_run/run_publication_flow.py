from __future__ import annotations

from typing import Any, Callable

from agent_run.agent_invocation import (
    fail_interrupted_invocation,
    invocation_event_recorder,
)
from agent_run.artifacts import (
    PublicationArtifact,
    append_human_blocker_history,
    clear_current_human_blocker,
    parse_publication_wire_result,
)
from agent_run.change_delivery import ensure_linked_branch_display
from agent_run.delivery_policy import invocation_deadline_for_state
from agent_run.credential_availability import (
    clear_initial_credential_wait,
    resume_initial_credential_wait,
    wait_for_initial_credential,
)
from agent_run.error_safety import bounded_error
from agent_run.external_supervision import (
    ensure_supervision_window,
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.git import GitError
from agent_run.github import GitHubReadError
from agent_run.required_checks import (
    is_explicitly_repairable_code_failure,
    supervise_unrepairable_check_failure,
)
from agent_run.required_checks_observation import failure_evidence_matches_observation
from agent_run.publication_pending import publication_pending_diagnostic
from agent_run.publication_operation_retry import begin_publication_operation_attempt
from agent_run.run_publication_shared import RunPublicationShared
from agent_run.run_currentness import run_currentness_boundary
from agent_run.worker_credentials import InitialCredentialUnavailable
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
    close_semantic_attempt,
    detach_active_invocation,
    pending_semantic_attempt,
    release_semantic_attempt,
)


_RUN_PUBLICATION_CREDENTIAL_SUBJECT = "run-publication"


class RunPublicationFlow(RunPublicationShared):
    """Create and publish the PR for an already accepted Final Run."""

    def publish(self, run_id: str) -> dict[str, Any]:
        state = self._load(run_id)
        publication = self._publication_state(state)
        try:
            return self._publish(state, publication)
        except GitHubReadError as error:
            return self._handle_github_error(state, publication, error)

    def _publish(
        self, state: dict[str, Any], publication: dict[str, Any]
    ) -> dict[str, Any]:
        if not self._refresh_currentness(state):
            return self._save(state)
        run = self._mapping(state, "run_acceptance")
        if publication["phase"] in {"merged", "abandoned"}:
            return state
        if publication["phase"] == "ready_for_human":
            return state
        if publication["phase"] == "stale":
            attempt_audit = {
                key: publication[key]
                for key in (
                    "publication_attempts",
                    "semantic_attempt_history",
                    "publication_operation_retry",
                    "last_publication_error",
                )
                if key in publication
            }
            publication.clear()
            publication.update(attempt_audit)
            publication["phase"] = "pending"
        if publication["phase"] == "publication_pending":
            return state
        if publication["phase"] == "publishing":
            if fail_interrupted_invocation(
                state, role="final_publication", save=self._save
            ):
                return state
            publication["phase"] = "pending"
        if not self._acceptance_is_current(state, run):
            return self._invalidate_for_fresh_acceptance(state)
        continue_accepted_publication = publication["phase"] in {
            "waiting_checks",
            "waiting_external",
            "ready_for_approval",
        }
        if not continue_accepted_publication and publication["phase"] != "pending":
            raise ValueError("unknown Final Run Publication phase")
        while True:
            if continue_accepted_publication:
                continue_accepted_publication = False
            else:
                publication["phase"] = "publishing"
                stored_artifact = publication.get("artifact")
                if isinstance(stored_artifact, dict):
                    artifact = PublicationArtifact.from_stored(
                        stored_artifact, delivery_run=str(state["run_id"])
                    )
                else:
                    semantic_attempt = pending_semantic_attempt(
                        publication, role="publication"
                    )
                    newly_allocated = semantic_attempt is None
                    if semantic_attempt is None:
                        begin_publication_operation_attempt(publication)
                        ordinal = int(publication.get("publication_attempts", 0)) + 1
                        publication["publication_attempts"] = ordinal
                        semantic_attempt = allocate_semantic_attempt(
                            publication,
                            role="publication",
                            work_subject=f"run-publication:{state['run_id']}",
                            generation=int(run.get("acceptance_generation", 1)),
                            currentness_boundary=self._publication_boundary(state),
                            ordinal=ordinal,
                        )
                    self._save(state)
                    created_artifact = self._create_publication_artifact(
                        state,
                        publication,
                        semantic_attempt,
                        newly_allocated=newly_allocated,
                    )
                    if created_artifact is None:
                        return self._save(state)
                    artifact = created_artifact
                    close_semantic_attempt(
                        publication,
                        semantic_attempt,
                        outcome="publication_artifact",
                    )
                    self._save(state)
                    publication["artifact"] = {
                        "commit_message": artifact.commit_message,
                        "pr_title": artifact.pr_title,
                        "pr_body_markdown": artifact.pr_body_markdown,
                    }
            result = self._continue_accepted_publication(state, run, publication)
            if result is not None:
                return result

    def _continue_accepted_publication(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        publication: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            return self._publish_accepted_run(state, run, publication)
        except (GitError, GitHubReadError, OSError) as error:
            if isinstance(error, GitHubReadError):
                return self._handle_github_error(state, publication, error)
            if self._final_run_ref_write_is_pending(publication):
                return self._handle_final_run_ref_error(state, publication, error)
            if self._publication_failed(state, publication, error):
                return state
            return None

    def _handle_github_error(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
        error: GitHubReadError,
    ) -> dict[str, Any]:
        if error.code == "github_write_failed" and isinstance(
            publication.get("write_intent"), dict
        ):
            return self._wait_for_github_convergence(state, publication, error)
        if error.code == "github_write_failed":
            return self._hold_unknown_write_outcome(state, publication, error)
        if not is_github_convergence_error(error.code):
            raise error
        return self._wait_for_github_convergence(state, publication, error)

    def _wait_for_github_convergence(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
        error: GitHubReadError,
    ) -> dict[str, Any]:
        if self._record_operation_failure(state, publication, error):
            return state
        publication["phase"] = "waiting_external"
        wait_for_github_convergence(
            state,
            code=error.code,
            message=error.message,
            waiting_for="Final Run PR GitHub reconciliation",
        )
        return self._save(state)

    def _hold_unknown_write_outcome(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
        error: GitHubReadError,
    ) -> dict[str, Any]:
        publication["phase"] = "ready_for_human"
        state.update(
            {
                "status": "ready_for_human",
                "terminal_kind": "waiting_human",
                "diagnostics": [
                    {
                        "code": "github_write_outcome_unknown",
                        "message": bounded_error(error.message),
                    }
                ],
            }
        )
        return self._save(state)

    def _persist_write_intent(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
        action: str,
        authority: dict[str, str] | None = None,
    ) -> None:
        intent: dict[str, object] = {"action": action}
        if authority is not None:
            intent["authority"] = authority
        publication["write_intent"] = intent
        publication["phase"] = "waiting_external"
        state.update(
            {
                "status": "waiting_external",
                "terminal_kind": "waiting_external",
                "diagnostics": [
                    {
                        "code": "github_write_pending",
                        "message": "正在确认 Final Run PR 写入结果",
                    }
                ],
            }
        )
        self._save(state)

    def _final_run_ref_write_is_pending(self, publication: dict[str, Any]) -> bool:
        intent = publication.get("write_intent")
        return isinstance(intent, dict) and intent.get("action") == "ensure_final_run_ref"

    def _handle_final_run_ref_error(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
        error: Exception,
    ) -> dict[str, Any]:
        if isinstance(error, OSError):
            if self._record_operation_failure(state, publication, error):
                return state
            # The ref may have been created after the response was lost.  Keep
            # its durable intent so the next call performs readback only.
            publication["phase"] = "waiting_external"
            state.update(
                {
                    "status": "waiting_external",
                    "terminal_kind": "waiting_external",
                    "diagnostics": [
                        {
                            "code": "final_run_ref_readback_pending",
                            "message": "正在确认 Final Run ref 写入结果",
                        }
                    ],
                }
            )
        else:
            # A known foreign ref or failed CAS must not trigger another
            # Publication Agent invocation or a PR write.
            publication["phase"] = "ready_for_human"
            state.update(
                {
                    "status": "ready_for_human",
                    "terminal_kind": "waiting_human",
                    "diagnostics": [
                        {
                            "code": "final_run_ref_authority_failed",
                            "message": bounded_error(str(error)),
                        }
                    ],
                }
            )
        return self._save(state)

    def _write_intent_matches(
        self, pr_number: int | None, expected_title: str, expected_body: str
    ) -> bool:
        if pr_number is None:
            return False
        return self.github.run_pr_narrative_matches(
            pr_number, title=expected_title, body=expected_body
        )

    def _publication_failed(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
        error: Exception,
    ) -> bool:
        if self._record_operation_failure(state, publication, error):
            return True
        publication.pop("write_intent", None)
        publication["last_publication_error"] = str(error)
        publication["phase"] = "pending"
        self._save(state)
        return False

    def _create_publication_artifact(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
        semantic_attempt: dict[str, Any],
        *,
        newly_allocated: bool,
    ) -> PublicationArtifact | None:
        resume_initial_credential_wait(
            state, work_subject=_RUN_PUBLICATION_CREDENTIAL_SUBJECT
        )
        checkout = self._publication_checkout(state)
        try:
            self.git.prepare_validation_checkout(
                head_sha=self.git.resolve(str(state["run_branch"])), checkout=checkout
            )
            request = self._publication_request(state, checkout)
            request["_invocation_event"] = self._invocation_events(
                state, request, semantic_attempt
            )
            request["_currentness_check"] = lambda: (
                self._refresh_currentness(state)
                and self._acceptance_is_current(
                    state, self._mapping(state, "run_acceptance")
                )
            )
            if publication.get("publication_new_thread") is True:
                request["_invocation_mode"] = "new-thread"
            elif publication.get("publication_failure_resume") is True:
                request["_invocation_mode"] = "resume"
            try:
                raw = self.agents.run_publication(request)
            except InitialCredentialUnavailable as error:
                # No final-publication Worker started, so this does not spend
                # a publication attempt or turn an availability outage into
                # an execution failure.
                publication["phase"] = "pending"
                if newly_allocated:
                    publication["publication_attempts"] = max(
                        0, int(publication["publication_attempts"]) - 1
                    )
                    release_semantic_attempt(publication)
                    publication.pop("publication_operation_retry", None)
                    publication.pop("last_publication_error", None)
                wait_for_initial_credential(
                    state,
                    work_subject=_RUN_PUBLICATION_CREDENTIAL_SUBJECT,
                    phase="run_publication",
                    resume_status="run_publication_pending",
                    http_status=error.http_status,
                )
                self._save(state)
                return None
            clear_initial_credential_wait(
                state, work_subject=_RUN_PUBLICATION_CREDENTIAL_SUBJECT
            )
            publication.pop("publication_failure_resume", None)
            publication.pop("publication_new_thread", None)
            if not self._refresh_currentness(state) or not self._acceptance_is_current(
                state, self._mapping(state, "run_acceptance")
            ):
                close_semantic_attempt(
                    publication,
                    semantic_attempt,
                    outcome="currentness_invalidated",
                )
                detach_active_invocation(state, semantic_attempt)
                self._invalidate_for_fresh_acceptance(state)
                return None
            thread_id = raw.pop("_thread_id", None)
            if isinstance(thread_id, str):
                publication["thread_id"] = thread_id
            normalized = parse_publication_wire_result(raw)
            blockers = (
                tuple(normalized["human_blockers"])
                if normalized["result_kind"] == "human_blocker"
                else None
            )
            if blockers is not None:
                append_human_blocker_history(
                    publication, phase="pending", blockers=blockers
                )
                publication.update(
                    {
                        "phase": "ready_for_human",
                        "blocked_reason": "agent_requires_human",
                        "human_blockers": list(blockers),
                        "human_blocker_phase": "pending",
                    }
                )
                state.update(
                    {
                        "status": "ready_for_human",
                        "terminal_kind": "waiting_human",
                        "diagnostics": [
                            {"code": "agent_requires_human", "message": blocker}
                            for blocker in blockers
                        ],
                    }
                )
                return None
            artifact = PublicationArtifact.parse(
                normalized,
                delivery_run=str(state["run_id"]),
            )
            clear_current_human_blocker(publication)
            return artifact
        finally:
            self.git.remove_worktree(checkout)
            self._remove_empty_directories(checkout)

    def _invocation_events(
        self,
        state: dict[str, Any],
        request: dict[str, Any],
        semantic_attempt: dict[str, Any],
    ) -> Callable[..., None]:
        run = self._mapping(state, "run_acceptance")
        acceptance = self._mapping(run, "acceptance_record")
        invocation_deadline_seconds = invocation_deadline_for_state(
            state, "final_publication"
        )
        return invocation_event_recorder(
            state,
            role="final_publication",
            phase="run_publication",
            work_subject=f"run-publication:{state['run_id']}",
            generation=int(run.get("acceptance_generation", 1)),
            invocation_input=request,
            currentness_boundary=self._publication_boundary(state),
            semantic_attempt=semantic_attempt,
            save=self._save,
            invocation_deadline_seconds=invocation_deadline_seconds,
        )

    def _publication_boundary(self, state: dict[str, Any]) -> dict[str, Any]:
        run = self._mapping(state, "run_acceptance")
        acceptance = self._mapping(run, "acceptance_record")
        return run_currentness_boundary(
            state,
            reviewed_head_sha=str(acceptance["reviewed_head_sha"]),
            reviewed_default_base_sha=str(acceptance["reviewed_default_base_sha"]),
            expected_merge_tree=str(acceptance["expected_merge_tree"]),
        )

    def _publish_accepted_run(
        self,
        state: dict[str, Any],
        run: dict[str, Any],
        publication: dict[str, Any],
    ) -> dict[str, Any]:
        artifact = PublicationArtifact.from_stored(
            self._mapping(publication, "artifact"), delivery_run=str(state["run_id"])
        )
        if not self._publication_is_current(state):
            return self._invalidate_for_fresh_acceptance(state)
        run = self._mapping(state, "run_acceptance")
        run_head = self.git.resolve(str(state["run_branch"]))
        narrative = self._render_final_run_pr_body(state, artifact.pr_body_markdown)
        known_pr = publication.get("pr_number")
        if not self._known_final_pr_is_authorized(state, publication, run_head):
            return self._invalidate_for_fresh_acceptance(state)
        initial_existing = (
            known_pr
            if isinstance(known_pr, int)
            else self.github.find_run_pr(
                branch=str(state["run_branch"]),
                expected_head_sha=run_head,
                expected_base_branch=self.default_branch,
                expected_base_sha=self.default_head_sha,
            )
        )
        if not self._publication_is_current(state):
            return self._invalidate_for_fresh_acceptance(state)
        run = self._mapping(state, "run_acceptance")
        run_head = self.git.resolve(str(state["run_branch"]))
        if initial_existing is not None and not self._final_pr_is_current(
            initial_existing, run_head, str(state["run_branch"]), str(state["repository"])
        ):
            return self._invalidate_for_fresh_acceptance(state)
        ref_authority = {
            "branch": str(state["run_branch"]),
            "base_branch": self.default_branch,
            "head_sha": run_head,
            "base_sha": self.default_head_sha,
        }
        pending_intent = publication.get("write_intent")
        if self._final_run_ref_write_is_pending(publication):
            assert isinstance(pending_intent, dict)
            if self._mapping(pending_intent, "authority") != ref_authority:
                raise ValueError("Final Run ref intent does not match durable authority")
            if not self.github.final_run_ref_matches(
                branch=ref_authority["branch"], expected_head_sha=run_head
            ):
                raise GitError("Final Run ref recovery did not match durable intent")
            publication.pop("write_intent", None)
            self._save(state)
        elif not isinstance(pending_intent, dict):
            self._persist_write_intent(
                state, publication, "ensure_final_run_ref", ref_authority
            )
            self.github.ensure_final_run_ref(
                branch=ref_authority["branch"], expected_head_sha=run_head
            )
            publication.pop("write_intent", None)
            self._save(state)
        elif pending_intent.get("action") == "create_final_pr":
            if pending_intent.get("authority") != ref_authority:
                raise ValueError("Final Run ref intent does not match durable authority")
            if not self.github.final_run_ref_matches(
                branch=ref_authority["branch"], expected_head_sha=run_head
            ):
                raise GitError("Final Run ref recovery did not match durable intent")
        existing = (
            known_pr
            if isinstance(known_pr, int)
            else self.github.find_run_pr(
                branch=str(state["run_branch"]),
                expected_head_sha=run_head,
                expected_base_branch=self.default_branch,
                expected_base_sha=self.default_head_sha,
            )
        )
        if not self._known_final_pr_is_authorized(state, publication, run_head):
            return self._invalidate_for_fresh_acceptance(state)
        if not self._publication_is_current(state):
            return self._invalidate_for_fresh_acceptance(state)
        run = self._mapping(state, "run_acceptance")
        run_head = self.git.resolve(str(state["run_branch"]))
        if existing is not None and not self._final_pr_is_current(
            existing, run_head, str(state["run_branch"]), str(state["repository"])
        ):
            return self._invalidate_for_fresh_acceptance(state)
        write_intent_resolved = False
        if isinstance(publication.get("write_intent"), dict):
            if not self._write_intent_matches(existing, artifact.pr_title, narrative):
                return self._save(state)
            publication.pop("write_intent", None)
            write_intent_resolved = True
        pr_number = existing
        created_pr = pr_number is None
        if pr_number is None:
            self._persist_write_intent(
                state, publication, "create_final_pr", ref_authority
            )
            pr_number = self.github.ensure_run_pr(
                branch=str(state["run_branch"]),
                base_branch=self.default_branch,
                expected_head_sha=run_head,
                expected_base_sha=self.default_head_sha,
                title=artifact.pr_title,
                body=narrative,
            )
            publication.pop("write_intent", None)
        live = self.github.live_pull_request(pr_number)
        if not self._final_pr_is_current(
            pr_number, run_head, str(state["run_branch"]), str(state["repository"])
        ):
            return self._invalidate_for_fresh_acceptance(state)
        record = self._record(state, run_head, str(live["head_sha"]))
        if (
            not created_pr
            and not write_intent_resolved
            and publication.get("record") != record
        ):
            self._persist_write_intent(state, publication, "refresh_final_pr_narrative")
            self.github.refresh_run_pr_narrative(
                pr_number=pr_number,
                expected_head_branch=str(state["run_branch"]),
                expected_head_sha=run_head,
                expected_base_branch=self.default_branch,
                expected_base_sha=self.default_head_sha,
                title=artifact.pr_title,
                body=narrative,
            )
            publication.pop("write_intent", None)
        try:
            ensure_linked_branch_display(
                github=self.github,
                state=state,
                job=publication,
                issue_number=int(self._mapping(state, "parent")["number"]),
                branch=str(state["run_branch"]),
                head_sha=run_head,
                save=self._save,
            )
        except (GitHubReadError, OSError, TimeoutError):
            # Display is intentionally outside the core ref/PR lifecycle.  Its
            # durable pre-call intent makes an interrupted call indeterminate
            # and prevents any recovery replay.
            self._save(state)
        self.github.record_run_publication(pr_number, record)
        publication.update({"pr_number": pr_number, "record": record})
        publication.pop("last_publication_error", None)
        observation = self._observe_required_checks(
            state, run, publication, pr_number, run_head
        )
        if observation is None:
            return state
        checks = str(observation["result"])
        self._record_agent_run_status(pr_number, run, run_head, checks)
        if checks == "fail":
            self._save(state)
            evidence = self._required_check_evidence(
                state, publication, pr_number, run_head
            )
            if evidence is None:
                return state
            if not self._revalidate_final_run_pr_before_repair(
                state, publication, pr_number, run_head
            ):
                return state
            if failure_evidence_matches_observation(
                observation,
                evidence,
                pr_number=pr_number,
                head_sha=run_head,
            ) and is_explicitly_repairable_code_failure(evidence):
                self._queue_repair(
                    state,
                    repair_source="required_checks",
                    ci_evidence=evidence,
                )
            else:
                supervise_unrepairable_check_failure(
                    state,
                    publication,
                    phase="waiting_external",
                    waiting_for=f"Run PR #{pr_number} Required Check repairability",
                )
        elif checks == "pending":
            publication["phase"] = "waiting_checks"
            state["status"] = "waiting_checks"
            state["terminal_kind"] = "waiting_checks"
            state["diagnostics"] = []
        elif checks == "unknown":
            publication["required_checks_observation_status"] = "unknown"
            publication["phase"] = "waiting_external"
            wait_for_github_convergence(
                state,
                code="github_checks_observation_unknown",
                message="GitHub Required Checks returned an unknown state",
                waiting_for=f"Run PR #{pr_number} Required Checks observation",
            )
            ensure_supervision_window(state)
        else:
            publication["phase"] = "ready_for_approval"
            state["status"] = "run_approval_pending"
            state["terminal_kind"] = "waiting_human"
            state["diagnostics"] = []
        return self._save(state)

    def _publication_is_current(self, state: dict[str, Any]) -> bool:
        if not self._refresh_currentness(state):
            return False
        return self._acceptance_is_current(
            state, self._mapping(state, "run_acceptance")
        )

    def _known_final_pr_is_authorized(
        self, state: dict[str, Any], publication: dict[str, Any], run_head: str
    ) -> bool:
        """Validate a persisted Final Run PR before any recovery write.

        A replacement PR after an approval grant belongs to a fresh Acceptance
        generation.  Other identity failures remain hard read errors, exactly
        as they did at each recovery boundary before this helper was shared.
        """

        known_pr = publication.get("pr_number")
        if not isinstance(known_pr, int):
            return True
        try:
            self._require_final_pr_identity(
                live=self.github.live_pull_request(known_pr),
                run_head=run_head,
                branch=str(state["run_branch"]),
                repository=str(state["repository"]),
                expected_base_sha=self.default_head_sha,
            )
        except GitHubReadError as error:
            if error.code == "foreign_run_pr" and isinstance(
                publication.get("approval_grant"), dict
            ):
                return False
            raise
        return True

    def _final_pr_is_current(
        self, pr_number: int, run_head: str, branch: str, repository: str
    ) -> bool:
        live = self.github.live_pull_request(pr_number)
        return (
            live.get("state") == "OPEN"
            and self._final_pr_has_expected_identity(
                live=live,
                run_head=run_head,
                branch=branch,
                repository=repository,
                expected_base_sha=self.default_head_sha,
            )
        )
