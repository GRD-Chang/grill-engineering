from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend, PublicationResult
from agent_run.artifacts import AcceptanceArtifact, PublicationArtifact
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.state import StateStore
from agent_run.ticket_phase import (
    TicketPhase,
    parse_ticket_phase,
    sync_active_ticket_job,
)


MAX_MODIFICATION_ATTEMPTS = 10


def _record_superseded_integration(
    job: dict[str, Any], integrated_sha: str
) -> None:
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

    def run(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any]:
        while True:
            phase = parse_ticket_phase(job["phase"])
            if phase is TicketPhase.MERGED:
                return self._finish_close(state, job)
            if phase is TicketPhase.ESCALATING:
                return self._complete_escalation(state, job)
            if phase in {TicketPhase.DEVELOPING, TicketPhase.REPAIRING}:
                self._develop(state, job, checkout)
            if job["phase"] == TicketPhase.COMMITTING_CANDIDATE.value:
                blocked = self._commit_candidate(state, job, checkout)
                if blocked is not None:
                    return blocked
            if job["phase"] == TicketPhase.CANDIDATE.value:
                self._create_publication(state, job, checkout)
            if job["phase"] == TicketPhase.REVIEWING.value:
                terminal = self._review(state, job, checkout)
                if terminal is not None:
                    return terminal
                if job["phase"] == TicketPhase.REPAIRING.value:
                    continue
            if job["phase"] in {
                TicketPhase.ACCEPTED.value,
                TicketPhase.WAITING_CHECKS.value,
                TicketPhase.MERGING.value,
            }:
                terminal = self._publish_and_merge(state, job)
                if terminal is not None:
                    return terminal
                continue
            if job["phase"] in {
                TicketPhase.COMPLETED.value,
                TicketPhase.BLOCKED.value,
            }:
                return state

    def _develop(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> None:
        result = self.agents.develop(
            self._development_request(state, job, checkout)
        )
        if not result.thread_id.strip() or not result.summary.strip():
            raise ValueError("Development result is incomplete")
        self._record_development_thread(
            job,
            thread_id=result.thread_id,
            replaced_thread_id=result.replaced_thread_id,
        )
        job["development_summary"] = result.summary
        job["pending_attempt"] = int(job["modification_attempts"]) + 1
        job["phase"] = TicketPhase.COMMITTING_CANDIDATE.value
        self._save(state)

    def _commit_candidate(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any] | None:
        next_attempt = int(job["pending_attempt"])
        candidate = self.git.commit_candidate(
            checkout,
            ticket_number=int(job["ticket_number"]),
            attempt=next_attempt,
        )
        if candidate is None:
            job["phase"] = TicketPhase.BLOCKED.value
            job["blocked_reason"] = "no_code_changes"
            job.pop("pending_attempt", None)
            state["status"] = "blocked"
            state["diagnostics"] = [
                {
                    "code": "no_code_changes",
                    "message": (
                        "Development Attempt produced no code changes; "
                        "the modification budget was not consumed"
                    ),
                    "ticket_number": job["ticket_number"],
                }
            ]
            return self._save(state)
        job["modification_attempts"] = next_attempt
        job["candidate_sha"] = candidate
        job.pop("pending_attempt", None)
        job["phase"] = TicketPhase.CANDIDATE.value
        self._save(state)
        return None

    def _create_publication(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> None:
        candidate = str(job["candidate_sha"])
        raw_publication = self.agents.publication(
            self._publication_request(state, job, checkout)
        )
        if isinstance(raw_publication, PublicationResult):
            self._record_development_thread(
                job,
                thread_id=raw_publication.thread_id,
                replaced_thread_id=raw_publication.replaced_thread_id,
            )
            self._save(state)
            artifact_data = raw_publication.artifact
        else:
            artifact_data = raw_publication
        publication = PublicationArtifact.parse(
            artifact_data,
            primary_ticket=int(job["ticket_number"]),
        )
        publication_sha = self.git.create_publication_commit(
            checkout,
            candidate_sha=candidate,
            base_sha=str(job["base_sha"]),
            message=publication.commit_message,
        )
        job.update(
            {
                "publication": {
                    "commit_message": publication.commit_message,
                    "pr_title": publication.pr_title,
                    "pr_body_markdown": publication.pr_body_markdown,
                },
                "publication_sha": publication_sha,
                "phase": TicketPhase.REVIEWING.value,
            }
        )
        self._save(state)

    def _review(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any] | None:
        validation_attempt = int(job.get("validation_attempts", 0)) + 1
        job["validation_attempts"] = validation_attempt
        self._save(state)
        validation_checkout = (
            checkout.parent
            / f"validation-ticket-{job['ticket_number']}-{validation_attempt}"
        )
        try:
            self.git.prepare_validation_checkout(
                head_sha=str(job["publication_sha"]),
                checkout=validation_checkout,
            )
            review = self.agents.review(
                self._review_request(
                    state, job, validation_checkout
                )
            )
        finally:
            self.git.remove_worktree(validation_checkout)
        reviewer_ids = _string_list(job, "reviewer_thread_ids")
        development_history = (
            _string_list(job, "development_thread_history")
            if "development_thread_history" in job
            else []
        )
        development_ids = {
            str(job.get("development_thread_id", "")),
            *development_history,
        }
        if review.thread_id in development_ids:
            raise ValueError(
                "Fresh Acceptance cannot reuse the Development Thread"
            )
        if not review.thread_id.strip() or review.thread_id in reviewer_ids:
            raise ValueError("Fresh Acceptance requires a new Reviewer Thread")
        reviewer_ids.append(review.thread_id)
        job["reviewer_thread_ids"] = reviewer_ids
        self._save(state)
        artifact = AcceptanceArtifact.parse(review.artifact)
        job["acceptance_artifact"] = artifact.raw
        job["acceptance_record"] = {
            "acceptance_scope": "change_job",
            "reviewed_base_sha": str(job["base_sha"]),
            "reviewed_head_sha": str(job["publication_sha"]),
            "effective_revision": str(job["effective_revision"]),
            "reviewer_thread_id": review.thread_id,
            "artifact": artifact.raw,
        }
        if artifact.verdict == "human":
            return self._escalate(state, job, "reviewer_requires_human")
        if artifact.verdict == "request_changes":
            if int(job["modification_attempts"]) >= MAX_MODIFICATION_ATTEMPTS:
                return self._escalate(
                    state, job, "modification_budget_exhausted"
                )
            job["repair_source"] = "acceptance"
            job["phase"] = TicketPhase.REPAIRING.value
        else:
            job.pop("repair_source", None)
            job["phase"] = TicketPhase.ACCEPTED.value
        self._save(state)
        return None

    def _publish_and_merge(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any] | None:
        phase = parse_ticket_phase(job["phase"])
        if isinstance(job.get("pr_number"), int):
            live = self.github.live_pull_request(int(job["pr_number"]))
            if live.get("state") == "MERGED":
                if phase is TicketPhase.MERGING:
                    return self._reconcile_merged(state, job, live)
                return self._block(
                    state,
                    job,
                    "unexpected_external_merge",
                    "Ticket PR merged without a persisted Publisher merge intent",
                )
            if live.get("state") not in {None, "OPEN"}:
                return self._block(
                    state,
                    job,
                    "ticket_pr_closed_unmerged",
                    "Current Ticket PR was closed without merging",
                )
        if phase is TicketPhase.MERGING:
            _mapping(job, "merge_intent")
            integrated_sha = job.get("integrated_sha")
            if isinstance(integrated_sha, str) and integrated_sha:
                state["status"] = "waiting_merge"
                return self._save(state)
        publication = _mapping(job, "publication")
        self.github.publish_branch(
            str(job["ticket_branch"]),
            str(job["publication_sha"]),
            expected_remote_sha=str(
                job.get("published_sha", job["base_sha"])
            ),
        )
        job["published_sha"] = str(job["publication_sha"])
        self._save(state)
        pr_number = self.github.ensure_ticket_pr(
            branch=str(job["ticket_branch"]),
            base_branch=str(state["run_branch"]),
            title=str(publication["pr_title"]),
            body=str(publication["pr_body_markdown"]),
            primary_ticket=int(job["ticket_number"]),
        )
        job["pr_number"] = pr_number
        self._save(state)
        live = self.github.live_pull_request(pr_number)
        if live.get("state") == "MERGED":
            if parse_ticket_phase(job["phase"]) is not TicketPhase.MERGING:
                return self._block(
                    state,
                    job,
                    "unexpected_external_merge",
                    "Ticket PR merged without a persisted Publisher merge intent",
                )
            return self._reconcile_merged(state, job, live)
        if live.get("state") not in {None, "OPEN"}:
            return self._block(
                state,
                job,
                "ticket_pr_closed_unmerged",
                "Current Ticket PR was closed without merging",
            )
        checks = self.github.required_checks(pr_number)
        if checks == "pending":
            job["phase"] = TicketPhase.WAITING_CHECKS.value
            state["status"] = "waiting_checks"
            return self._save(state)
        if checks == "fail":
            return self._handle_failed_checks(
                state,
                job,
                self.github.required_check_evidence(pr_number),
            )
        if checks not in {"none", "pass"}:
            raise ValueError(f"unknown Required Checks state: {checks}")
        if self._live_revision_changed(state, job):
            return self._block(
                state,
                job,
                "effective_revision_mismatch",
                "Published-Head Gate rejected stale requirements",
            )
        live = self.github.live_pull_request(pr_number)
        return self._pass_published_head_gate(state, job, publication, live)

    def _handle_failed_checks(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        if int(job["modification_attempts"]) >= MAX_MODIFICATION_ATTEMPTS:
            return self._escalate(
                state, job, "modification_budget_exhausted"
            )
        job["repair_source"] = "required_checks"
        job["ci_evidence"] = evidence
        job["phase"] = TicketPhase.REPAIRING.value
        self._save(state)
        return None

    def _live_revision_changed(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool:
        current = self.github.current_effective_revision(
            parent_number=int(_mapping(state, "parent")["number"]),
            ticket_number=int(job["ticket_number"]),
            expected_revision=str(job["effective_revision"]),
        )
        return current != str(job["effective_revision"])

    def _pass_published_head_gate(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        publication: dict[str, Any],
        live: dict[str, Any],
    ) -> dict[str, Any]:
        acceptance = _mapping(job, "acceptance_record")
        if (
            live.get("head_sha") != job["publication_sha"]
            or live.get("base_branch") != state["run_branch"]
            or live.get("base_sha") != acceptance.get("reviewed_base_sha")
            or live.get("mergeable") is not True
        ):
            job["phase"] = TicketPhase.BLOCKED.value
            job["blocked_reason"] = "published_head_mismatch"
            state["status"] = "blocked"
            state["diagnostics"] = [
                {
                    "code": "published_head_mismatch",
                    "message": "Published-Head Gate rejected live PR state",
                    "ticket_number": job["ticket_number"],
                }
            ]
            return self._save(state)
        if (
            acceptance.get("reviewed_base_sha") != job["base_sha"]
            or acceptance.get("reviewed_head_sha") != job["publication_sha"]
            or acceptance.get("effective_revision") != job["effective_revision"]
        ):
            return self._block(
                state,
                job,
                "acceptance_record_mismatch",
                "Published-Head Gate rejected a stale Acceptance Record",
            )
        pr_number = int(job["pr_number"])
        job["merge_intent"] = {
            "head_sha": str(job["publication_sha"]),
            "base_branch": str(state["run_branch"]),
            "base_sha": str(acceptance["reviewed_base_sha"]),
            "effective_revision": str(job["effective_revision"]),
            "commit_message": str(publication["commit_message"]),
        }
        job["phase"] = TicketPhase.MERGING.value
        self._save(state)
        self.github.record_acceptance(pr_number, acceptance)
        integrated = self.github.squash_merge(
            pr_number=pr_number,
            expected_head_sha=str(job["publication_sha"]),
            run_branch=str(state["run_branch"]),
            commit_message=str(publication["commit_message"]),
        )
        job["integrated_sha"] = integrated
        self._save(state)
        live_after_merge = self.github.live_pull_request(pr_number)
        if live_after_merge.get("state") != "MERGED":
            state["status"] = "waiting_merge"
            return self._save(state)
        return self._reconcile_merged(state, job, live_after_merge)

    def _reconcile_merged(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        live: dict[str, Any],
    ) -> dict[str, Any]:
        intent = _mapping(job, "merge_intent")
        integrated = live.get("integrated_sha")
        if not isinstance(integrated, str) or not integrated:
            raise ValueError("merged Ticket PR is missing integrated SHA")
        if (
            live.get("head_sha") != intent.get("head_sha")
            or live.get("base_branch") != intent.get("base_branch")
            or live.get("head_tree") != live.get("integrated_tree")
            or live.get("integrated_message") != intent.get("commit_message")
            or live.get("integrated_parents") != [intent.get("base_sha")]
        ):
            return self._block(
                state,
                job,
                "merged_result_mismatch",
                "Merged Ticket PR does not match Publisher merge intent",
            )
        job["integrated_sha"] = integrated
        self._save(state)
        self.github.sync_run_branch(
            run_branch=str(intent["base_branch"]),
            integrated_sha=integrated,
        )
        pending_revision = job.get("pending_effective_revision")
        if (
            isinstance(pending_revision, str)
            and pending_revision != job["effective_revision"]
        ) or self._live_revision_changed(state, job):
            _record_superseded_integration(job, integrated)
            return self._block(
                state,
                job,
                "merged_revision_mismatch",
                "Merged Ticket PR was integrated, but no longer matches "
                "the current revision",
            )
        job["phase"] = TicketPhase.MERGED.value
        self._save(state)
        return self._finish_close(state, job)

    def _block(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        code: str,
        message: str,
    ) -> dict[str, Any]:
        job["phase"] = TicketPhase.BLOCKED.value
        job["blocked_reason"] = code
        state["status"] = "blocked"
        state["diagnostics"] = [
            {
                "code": code,
                "message": message,
                "ticket_number": job["ticket_number"],
            }
        ]
        return self._save(state)

    def _development_request(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any]:
        head_sha = self.git.checkout_head(checkout)
        request = {
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "ticket": self._ticket(state, job),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "head_sha": head_sha,
            "checkout": str(checkout),
            "thread_id": job.get("development_thread_id"),
        }
        if job.get("development_summary"):
            request["development_summary"] = str(
                job["development_summary"]
            )
        repair_source = job.get("repair_source")
        if repair_source == "acceptance":
            request["repair_source"] = repair_source
            request["acceptance_artifact"] = _mapping(
                job, "acceptance_artifact"
            )
        elif repair_source == "required_checks":
            request["repair_source"] = repair_source
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    @staticmethod
    def _record_development_thread(
        job: dict[str, Any],
        *,
        thread_id: str,
        replaced_thread_id: str | None,
    ) -> None:
        if not thread_id.strip():
            raise ValueError("Development Thread ID is empty")
        previous_thread = job.get("development_thread_id")
        if previous_thread is not None and previous_thread != thread_id:
            if replaced_thread_id != previous_thread:
                raise ValueError(
                    "Development Thread changed without a verified replacement"
                )
            history = (
                _string_list(job, "development_thread_history")
                if "development_thread_history" in job
                else []
            )
            history.append(str(previous_thread))
            job["development_thread_history"] = history
        job["development_thread_id"] = thread_id

    def _publication_request(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any]:
        request = {
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "ticket": self._ticket(state, job),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "checkout": str(checkout),
            "thread_id": job["development_thread_id"],
        }
        if job.get("development_summary"):
            request["development_summary"] = str(
                job["development_summary"]
            )
        if job.get("repair_source") == "acceptance":
            request["acceptance_artifact"] = _mapping(
                job, "acceptance_artifact"
            )
        elif job.get("repair_source") == "required_checks":
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    def _review_request(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
    ) -> dict[str, Any]:
        return {
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "ticket": self._ticket(state, job),
            "base_sha": job["base_sha"],
            "publication_sha": job["publication_sha"],
            "effective_revision": job["effective_revision"],
            "publication": _mapping(job, "publication"),
            "checkout": str(checkout),
        }

    @staticmethod
    def _parent(state: dict[str, Any]) -> dict[str, Any]:
        parent = dict(_mapping(state, "parent"))
        parent["url"] = _issue_url(state, int(parent["number"]))
        return parent

    @staticmethod
    def _ticket(
        state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any]:
        tickets = _mapping(_mapping(state, "ticket_graph"), "tickets")
        ticket = dict(_mapping(tickets, str(job["ticket_number"])))
        ticket["url"] = _issue_url(state, int(job["ticket_number"]))
        return ticket

    def _escalate(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> dict[str, Any]:
        job["phase"] = TicketPhase.ESCALATING.value
        job["escalation_code"] = code
        job["blocked_reason"] = code
        state["status"] = "escalating"
        state["diagnostics"] = [
            {
                "code": code,
                "message": "Ticket requires explicit human intervention",
                "ticket_number": job["ticket_number"],
            }
        ]
        self._save(state)
        return self._complete_escalation(state, job)

    def _complete_escalation(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any]:
        self.github.mark_ready_for_human(int(job["ticket_number"]))
        job["phase"] = TicketPhase.BLOCKED.value
        job["blocked_reason"] = str(job["escalation_code"])
        state["status"] = "blocked"
        return self._save(state)

    def _finish_close(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any]:
        self.github.close_primary_ticket(
            ticket_number=int(job["ticket_number"]),
            run_id=str(state["run_id"]),
            pr_number=int(job["pr_number"]),
            integrated_sha=str(job["integrated_sha"]),
        )
        job["phase"] = TicketPhase.COMPLETED.value
        job.pop("blocked_reason", None)
        state["status"] = "ticket_completed"
        state["diagnostics"] = []
        return self._save(state)

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        sync_active_ticket_job(state)
        self.states.save_run(str(state["run_id"]), state)
        return state


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"{key} must contain strings")
    return list(value)


def _issue_url(state: dict[str, Any], number: int) -> str:
    repository = state.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("repository must be a non-empty string")
    return f"https://github.com/{repository}/issues/{number}"
