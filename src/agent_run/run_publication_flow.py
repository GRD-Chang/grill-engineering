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
from agent_run.change_delivery import MAX_PUBLICATION_ATTEMPTS
from agent_run.git import GitError
from agent_run.github import GitHubReadError
from agent_run.publication_pending import publication_pending_diagnostic
from agent_run.run_publication_shared import RunPublicationShared
from agent_run.run_currentness import run_currentness_boundary


class RunPublicationFlow(RunPublicationShared):
    """Create and publish the PR for an already accepted Final Run."""

    def publish(self, run_id: str) -> dict[str, Any]:
        with self.states.locked():
            state = self._load(run_id)
            if not self._refresh_currentness(state):
                return self._save(state)
            run = self._mapping(state, "run_acceptance")
            publication = self._publication_state(state)
            if publication["phase"] in {"merged", "abandoned"}:
                return state
            if publication["phase"] == "stale":
                publication.clear()
                publication["phase"] = "pending"
            if publication["phase"] == "publication_pending":
                publication.pop("artifact", None)
                publication.pop("last_publication_error", None)
                publication["publication_attempts"] = 0
                publication["phase"] = "pending"
                state["terminal_kind"] = "run_publication_pending"
            if publication["phase"] == "publishing":
                if fail_interrupted_invocation(
                    state, role="final_publication", save=self._save
                ):
                    return state
                publication.pop("artifact", None)
                publication["phase"] = "pending"
            if not self._acceptance_is_current(state, run):
                return self._invalidate_for_fresh_acceptance(state)
            if publication["phase"] in {"waiting_checks", "ready_for_approval"}:
                return self._publish_accepted_run(state, run, publication)
            if publication["phase"] != "pending":
                raise ValueError("unknown Final Run Publication phase")
            while True:
                publication["publication_attempts"] = (
                    int(publication.get("publication_attempts", 0)) + 1
                )
                publication["phase"] = "publishing"
                publication.pop("artifact", None)
                self._save(state)
                artifact = self._create_publication_artifact(state, publication)
                if artifact is None:
                    return self._save(state)
                self._save(state)
                publication["artifact"] = {
                    "commit_message": artifact.commit_message,
                    "pr_title": artifact.pr_title,
                    "pr_body_markdown": artifact.pr_body_markdown,
                }
                try:
                    return self._publish_accepted_run(state, run, publication)
                except (GitError, GitHubReadError, OSError) as error:
                    if self._publication_failed(state, publication, error):
                        return state

    def _publication_failed(
        self,
        state: dict[str, Any],
        publication: dict[str, Any],
        error: Exception,
    ) -> bool:
        if int(publication["publication_attempts"]) >= MAX_PUBLICATION_ATTEMPTS:
            publication["phase"] = "publication_pending"
            publication["last_publication_error"] = str(error)
            state.update(
                {
                    "status": "publication_pending",
                    "terminal_kind": "publication_pending",
                    "diagnostics": [
                        publication_pending_diagnostic(
                            subject_key="delivery_run", subject=str(state["run_id"])
                        )
                    ],
                }
            )
            self._save(state)
            return True
        publication.pop("artifact", None)
        publication["last_publication_error"] = str(error)
        publication["phase"] = "pending"
        self._save(state)
        return False

    def _create_publication_artifact(
        self, state: dict[str, Any], publication: dict[str, Any]
    ) -> PublicationArtifact | None:
        checkout = self._publication_checkout(state)
        try:
            self.git.prepare_validation_checkout(
                head_sha=self.git.resolve(str(state["run_branch"])), checkout=checkout
            )
            request = self._publication_request(state, checkout)
            request["_invocation_event"] = self._invocation_events(state, request)
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
            raw = self.agents.run_publication(request)
            publication.pop("publication_failure_resume", None)
            publication.pop("publication_new_thread", None)
            if not self._refresh_currentness(state) or not self._acceptance_is_current(
                state, self._mapping(state, "run_acceptance")
            ):
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
        self, state: dict[str, Any], request: dict[str, Any]
    ) -> Callable[..., None]:
        run = self._mapping(state, "run_acceptance")
        acceptance = self._mapping(run, "acceptance_record")
        return invocation_event_recorder(
            state,
            role="final_publication",
            phase="run_publication",
            work_subject=f"run-publication:{state['run_id']}",
            generation=int(run.get("acceptance_generation", 1)),
            invocation_input=request,
            currentness_boundary=run_currentness_boundary(
                state,
                reviewed_head_sha=str(acceptance["reviewed_head_sha"]),
                reviewed_default_base_sha=str(acceptance["reviewed_default_base_sha"]),
                expected_merge_tree=str(acceptance["expected_merge_tree"]),
            ),
            save=self._save,
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
        known_pr = publication.get("pr_number")
        existing = (
            known_pr
            if isinstance(known_pr, int)
            else self.github.find_run_pr(branch=str(state["run_branch"]))
        )
        if not self._publication_is_current(state):
            return self._invalidate_for_fresh_acceptance(state)
        run = self._mapping(state, "run_acceptance")
        run_head = self.git.resolve(str(state["run_branch"]))
        if existing is not None and not self._final_pr_is_current(existing, run_head):
            return self._invalidate_for_fresh_acceptance(state)
        pr_number = existing
        if pr_number is None:
            pr_number = self.github.ensure_run_pr(
                branch=str(state["run_branch"]),
                base_branch=self.default_branch,
                title=artifact.pr_title,
                body=self._render_final_run_pr_body(state, artifact.pr_body_markdown),
            )
        else:
            self.github.refresh_run_pr_narrative(
                pr_number=pr_number,
                expected_head_sha=run_head,
                expected_base_branch=self.default_branch,
                expected_base_sha=self.default_head_sha,
                title=artifact.pr_title,
                body=self._render_final_run_pr_body(state, artifact.pr_body_markdown),
            )
        live = self.github.live_pull_request(pr_number)
        if not self._final_pr_is_current(pr_number, run_head):
            return self._invalidate_for_fresh_acceptance(state)
        record = self._record(state, run_head, str(live["head_sha"]))
        self.github.record_run_publication(pr_number, record)
        publication.update({"pr_number": pr_number, "record": record})
        publication.pop("last_publication_error", None)
        checks = self.github.required_checks(pr_number)
        self._record_agent_run_status(pr_number, run, run_head, checks)
        if checks == "fail":
            self._queue_repair(
                state,
                repair_source="required_checks",
                ci_evidence=self.github.required_check_evidence(pr_number),
            )
        elif checks == "pending":
            publication["phase"] = "waiting_checks"
            state["status"] = "waiting_checks"
            state["terminal_kind"] = "waiting_checks"
            state["diagnostics"] = []
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

    def _final_pr_is_current(self, pr_number: int, run_head: str) -> bool:
        live = self.github.live_pull_request(pr_number)
        return (
            live.get("state") == "OPEN"
            and live.get("head_sha") == run_head
            and live.get("base_branch") == self.default_branch
            and live.get("base_sha") == self.default_head_sha
        )
