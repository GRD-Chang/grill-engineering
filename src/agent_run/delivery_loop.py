from __future__ import annotations

"""Ticket-specific contract for the shared Change Delivery Engine."""

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.agent_invocation import select_publication_thread
from agent_run.change_delivery import (
    MAX_MODIFICATION_ATTEMPTS,
    ChangeDeliveryEngine,
    ChangeJobContract,
    MAX_PUBLICATION_CONTEXT_ATTEMPTS,
    latest_reviewer_thread,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.human_responses import current_human_response_history
from agent_run.state import StateStore
from agent_run.ticket_phase import TicketPhase, sync_active_ticket_job


def _record_superseded_integration(job: dict[str, Any], integrated_sha: str) -> None:
    raw_records = job.get("superseded_integrations", [])
    if not isinstance(raw_records, list) or not all(
        isinstance(record, dict) for record in raw_records
    ):
        raise ValueError("superseded_integrations must contain objects")
    record = {
        "pr_number": int(job["pr_number"]),
        "integrated_sha": integrated_sha,
        "effective_revision": str(job["effective_revision"]),
    }
    records = [dict(existing) for existing in raw_records]
    if record not in records:
        records.append(record)
    job["superseded_integrations"] = records


class TicketDeliveryLoop:
    """Run a Ticket Change Job through the shared delivery lifecycle."""

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
        self.engine = ChangeDeliveryEngine(
            git=git,
            github=github,
            agents=agents,
            contract=ChangeJobContract(
                label=lambda job: f"ticket-{job['ticket_number']}",
                branch=lambda job: str(job["ticket_branch"]),
                base_branch=lambda state: str(state["run_branch"]),
                candidate=lambda checkout, job, attempt: self.git.commit_candidate(
                    checkout, ticket_number=int(job["ticket_number"]), attempt=attempt
                ),
                development_thread_is_allowed=lambda _state, _thread_id: True,
                development_request=self._development_request,
                publication_request=self._publication_request,
                review_request=self._review_request,
                prepare_validation=lambda _checkout, job, validation: self.git.prepare_validation_checkout(
                    head_sha=str(job["candidate_sha"]), checkout=validation
                ),
                ensure_pr=lambda state, job, publication: self.github.ensure_ticket_pr(
                    branch=str(job["ticket_branch"]),
                    base_branch=str(state["run_branch"]),
                    title=str(publication["pr_title"]),
                    body=self._render_ticket_pr_body(state, job, publication),
                    primary_ticket=int(job["ticket_number"]),
                ),
                acceptance_record=self._acceptance_record,
                acceptance_is_current=self._acceptance_is_current,
                invalidate_stale_publication=self._invalidate_stale_publication,
                revision_changed=self._live_revision_changed,
                requires_explicit_approval=lambda _state, _job: False,
                after_merge=self._after_merge,
                escalate=self._escalate,
                save=self._save,
            ),
        )

    def run(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return self.engine.run(state, job, checkout)

    def _acceptance_record(
        self,
        _state: dict[str, Any],
        job: dict[str, Any],
        reviewer_thread_id: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "change_job",
            "reviewed_base_sha": str(job["base_sha"]),
            "reviewed_candidate_sha": str(job["candidate_sha"]),
            "reviewed_candidate_tree": self.git.resolve(
                f"{job['candidate_sha']}^{{tree}}"
            ),
            "effective_revision": str(job["effective_revision"]),
            "reviewer_thread_id": reviewer_thread_id,
            "artifact": artifact,
        }

    def _acceptance_is_current(
        self, _state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool:
        return (
            acceptance.get("reviewed_base_sha") == job.get("base_sha")
            and acceptance.get("reviewed_candidate_sha") == job.get("candidate_sha")
            and acceptance.get("reviewed_candidate_tree")
            == self.git.resolve(f"{job['candidate_sha']}^{{tree}}")
            and acceptance.get("effective_revision") == job.get("effective_revision")
        )

    def _invalidate_stale_publication(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.git.reset_checkout_to_base(checkout, str(state["run_branch"]))
        for key in (
            "candidate_sha",
            "publication",
            "publication_sha",
            "acceptance_artifact",
            "acceptance_record",
            "publication_attempts",
            "publication_thread_id",
            "last_publication_error",
            "repair_source",
            "ci_evidence",
        ):
            job.pop(key, None)
        job.update(
            {
                "base_sha": self.git.resolve(str(state["run_branch"])),
                "phase": TicketPhase.DEVELOPING.value,
                "validation_attempts": 0,
            }
        )
        state["status"] = "active"
        state["diagnostics"] = []

    def _after_merge(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        _live_before_merge: dict[str, Any],
    ) -> bool:
        live = self.github.live_pull_request(int(job["pr_number"]))
        integrated = job.get("integrated_sha")
        publication = _mapping(job, "publication")
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
            return self._block(
                state,
                job,
                "merged_result_mismatch",
                "Merged Ticket PR does not match Publisher merge intent",
            )
        if self._live_revision_changed(state, job):
            _record_superseded_integration(job, integrated)
            return self._block(
                state,
                job,
                "merged_revision_mismatch",
                "Merged Ticket PR was integrated, but no longer matches the current revision",
            )
        # Persist the integrated boundary before the external Issue mutation.
        job["phase"] = TicketPhase.MERGED.value
        self._save(state)
        close_intent = job.get("ticket_close_intent")
        if not isinstance(close_intent, dict):
            close_intent = self.github.prepare_primary_ticket_close(
                ticket_number=int(job["ticket_number"]),
                run_id=str(state["run_id"]),
                pr_number=int(job["pr_number"]),
                integrated_sha=integrated,
            )
            job["ticket_close_intent"] = close_intent
            self._save(state)
        if not isinstance(close_intent, dict):
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "Ticket close preparation did not establish current ownership",
            )
        dispatch_intent = close_intent

        def record_dispatch_boundary() -> None:
            nonlocal dispatch_intent
            if dispatch_intent.get("dispatch_attempted") is True:
                return
            dispatch_intent = {**dispatch_intent, "dispatch_attempted": True}
            job["ticket_close_intent"] = dispatch_intent
            self._save(state)

        close_ownership = self.github.close_primary_ticket(
            ticket_number=int(job["ticket_number"]),
            run_id=str(state["run_id"]),
            pr_number=int(job["pr_number"]),
            integrated_sha=integrated,
            close_intent=dispatch_intent,
            before_dispatch=record_dispatch_boundary,
        )
        if close_ownership is None:
            raise GitHubReadError(
                "ticket_close_ownership_pending",
                "Ticket close dispatch did not establish exact ownership",
            )
        job["ticket_close_ownership"] = close_ownership
        job["ticket_closed_by_run"] = close_ownership is not None
        job.pop("blocked_reason", None)
        state["status"] = "ticket_completed"
        state["diagnostics"] = []
        self._save(state)
        return True

    def _block(
        self, state: dict[str, Any], job: dict[str, Any], code: str, message: str
    ) -> bool:
        job.update({"phase": TicketPhase.BLOCKED.value, "blocked_reason": code})
        state["status"] = "blocked"
        state["diagnostics"] = [
            {"code": code, "message": message, "ticket_number": job["ticket_number"]}
        ]
        self._save(state)
        return False

    def _escalate(self, state: dict[str, Any], job: dict[str, Any], code: str) -> None:
        self.github.mark_ready_for_human(int(job["ticket_number"]))
        job["blocked_reason"] = code
        state["status"] = "blocked"
        state["diagnostics"] = [
            {
                "code": code,
                "message": "Ticket requires explicit human intervention",
                "ticket_number": job["ticket_number"],
            }
        ]

    def _live_revision_changed(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        current = self.github.current_effective_revision(
            parent_number=int(_mapping(state, "parent")["number"]),
            ticket_number=int(job["ticket_number"]),
            expected_revision=str(job["effective_revision"]),
        )
        return current != str(job["effective_revision"])

    def _development_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "ticket",
            "parent_issue_url": self._parent(state)["url"],
            "task_issue_url": self._ticket(state, job)["url"],
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "ticket": self._ticket(state, job),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "head_sha": self.git.checkout_head(checkout),
            "checkout": str(checkout),
            "thread_id": None
            if job.get("development_new_thread")
            else job.get("development_thread_id"),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job, generation=int(job.get("ticket_branch_generation", 1))
        ):
            request["human_response_history"] = history
        if job.get("development_summary"):
            request["development_summary"] = str(job["development_summary"])
        if job.get("repair_source") == "acceptance":
            request["repair_source"] = "acceptance"
            request["acceptance_artifact"] = _mapping(job, "acceptance_artifact")
        elif job.get("repair_source") == "required_checks":
            request["repair_source"] = "required_checks"
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    def _publication_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "ticket",
            "parent_issue_url": self._parent(state)["url"],
            "task_issue_url": self._ticket(state, job)["url"],
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "ticket": self._ticket(state, job),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "checkout": str(checkout),
            "thread_id": select_publication_thread(
                job, max_context_attempts=MAX_PUBLICATION_CONTEXT_ATTEMPTS
            ),
            "acceptance_artifact": _mapping(job, "acceptance_artifact"),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job, generation=int(job.get("ticket_branch_generation", 1))
        ):
            request["human_response_history"] = history
        if job.get("development_summary"):
            request["development_summary"] = str(job["development_summary"])
        existing_pr = job.get("pr_number")
        if isinstance(existing_pr, int):
            request["existing_pr"] = self.github.publication_context(existing_pr)
        if job.get("repair_source") == "acceptance":
            request["acceptance_artifact"] = _mapping(job, "acceptance_artifact")
        elif job.get("repair_source") == "required_checks":
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    def _review_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "ticket",
            "parent_issue_url": self._parent(state)["url"],
            "task_issue_url": self._ticket(state, job)["url"],
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "ticket": self._ticket(state, job),
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "effective_revision": job["effective_revision"],
            "checkout": str(checkout),
            "thread_id": latest_reviewer_thread(job)
            if (
                (
                    job.get("review_human_blocker_resume")
                    or job.get("review_resume_thread_id")
                )
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
                        generation=int(job.get("ticket_branch_generation", 1)),
                    )
                )
                else {}
            ),
        }

    @staticmethod
    def _render_ticket_pr_body(
        state: dict[str, Any], job: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        parent = _mapping(state, "parent")
        narrative = str(publication["pr_body_markdown"]).strip()
        return (
            f"Parent Issue: #{int(parent['number'])}\n"
            f"Primary Ticket: #{int(job['ticket_number'])}\n"
            "Delivery Type: Ticket\n\n"
            f"{narrative}"
        )

    @staticmethod
    def _parent(state: dict[str, Any]) -> dict[str, Any]:
        parent = dict(_mapping(state, "parent"))
        parent["url"] = _issue_url(state, int(parent["number"]))
        return parent

    @staticmethod
    def _ticket(state: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        ticket = dict(
            _mapping(
                _mapping(_mapping(state, "ticket_graph"), "tickets"),
                str(job["ticket_number"]),
            )
        )
        ticket["url"] = _issue_url(state, int(job["ticket_number"]))
        return ticket

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        sync_active_ticket_job(state)
        self.states.save_run(str(state["run_id"]), state)
        return state


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _issue_url(state: dict[str, Any], number: int) -> str:
    repository = state.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("repository must be a non-empty string")
    return f"https://github.com/{repository}/issues/{number}"
