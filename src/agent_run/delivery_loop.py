from __future__ import annotations

"""Ticket-specific contract for the shared Change Delivery Engine."""

from pathlib import Path
from typing import Any

from agent_run.commit_messages import commit_messages_match
from agent_run.agents import AgentBackend
from agent_run.agent_invocation import select_publication_thread
from agent_run.change_delivery import (
    MAX_MODIFICATION_ATTEMPTS,
    ChangeDeliveryAdapter,
    ChangeDeliveryEngine,
    ChangeDeliveryPublisher,
    ChangeDeliveryStateStore,
    ChangeJobContract,
    MAX_PUBLICATION_CONTEXT_ATTEMPTS,
    latest_reviewer_thread,
    StaleDisposition,
)
from agent_run.change_delivery_fallback import fallback_publication_context
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.external_supervision import (
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.human_responses import current_human_response_history
from agent_run.state import StateStore
from agent_run.ticket_phase import TicketPhase, sync_active_ticket_job
from agent_run.review_budget import previous_review_context
from agent_run.delivery_policy import ticket_budget_policy_for_job
from agent_run.required_checks_observation import clear_required_checks_observation


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


class TicketDeliveryAdapter(ChangeDeliveryAdapter):
    """Semantic Adapter for the Ticket delivery consumer."""

    stale_disposition = StaleDisposition.BLOCK
    classify_required_check_failures = True

    def __init__(self, git: GitRepository, github: GitHubPublisher) -> None:
        self.git = git
        self.github = github

    def development_thread_is_allowed(
        self, _state: dict[str, Any], _thread_id: str
    ) -> bool:
        return True

    def base_is_current(self, current_base: str, job: dict[str, Any]) -> bool:
        return current_base == job.get("base_sha")

    def development_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "ticket",
            "parent_issue_url": _parent(state)["url"],
            "task_issue_url": _ticket(state, job)["url"],
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": _parent(state),
            "ticket": _ticket(state, job),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "head_sha": self.git.checkout_head(checkout),
            "checkout": str(checkout),
            "thread_id": (
                None
                if job.get("development_new_thread")
                else job.get("development_thread_id")
            ),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job, generation=int(job.get("ticket_branch_generation", 1))
        ):
            request["human_response_history"] = history
        if job.get("development_summary"):
            request["development_summary"] = str(job["development_summary"])
        if job.get("repair_source") == "git_integrity":
            request["repair_source"] = "git_integrity"
            request["git_integrity_evidence"] = _mapping(
                job, "git_integrity_evidence"
            )
        elif job.get("repair_source") == "acceptance":
            request["repair_source"] = "acceptance"
            request["acceptance_artifact"] = _mapping(job, "acceptance_artifact")
        elif job.get("repair_source") == "required_checks":
            request["repair_source"] = "required_checks"
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    def publication_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "ticket",
            "parent_issue_url": _parent(state)["url"],
            "task_issue_url": _ticket(state, job)["url"],
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": _parent(state),
            "ticket": _ticket(state, job),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "checkout": str(checkout),
            "thread_id": select_publication_thread(
                job, max_context_attempts=MAX_PUBLICATION_CONTEXT_ATTEMPTS
            ),
        }
        if job.get("publication_authority") == "fallback":
            request["fallback_publication_context"] = fallback_publication_context(
                _mapping(job, "fallback_publication_receipt")
            )
        else:
            request["acceptance_artifact"] = _mapping(job, "acceptance_artifact")
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
        if (
            job.get("publication_authority") != "fallback"
            and job.get("repair_source") == "acceptance"
        ):
            request["acceptance_artifact"] = _mapping(job, "acceptance_artifact")
        elif (
            job.get("publication_authority") != "fallback"
            and job.get("repair_source") == "required_checks"
        ):
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    def review_request(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "ticket",
            "parent_issue_url": _parent(state)["url"],
            "task_issue_url": _ticket(state, job)["url"],
            "run_id": state["run_id"],
            "repository": str(state["repository"]),
            "parent": _parent(state),
            "ticket": _ticket(state, job),
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "current_review_identity": {
                "reviewed_base_sha": str(job["base_sha"]),
                "reviewed_candidate_sha": str(job["candidate_sha"]),
                "reviewed_candidate_tree": self.git.resolve(
                    f"{job['candidate_sha']}^{{tree}}"
                ),
            },
            "effective_revision": job["effective_revision"],
            "checkout": str(checkout),
            "thread_id": (
                latest_reviewer_thread(job)
                if (
                    (
                        job.get("review_human_blocker_resume")
                        or job.get("review_resume_thread_id")
                    )
                    and not job.get("review_new_thread")
                )
                else None
            ),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if history := current_human_response_history(
            job, generation=int(job.get("ticket_branch_generation", 1))
        ):
            request["human_response_history"] = history
        previous = previous_review_context(job)
        if previous is not None:
            request["previous_acceptance_artifact"] = previous["artifact"]
            request["previous_review_identity"] = previous["identity"]
        return request

    def acceptance_record(
        self,
        state: dict[str, Any],
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

    def acceptance_is_current(
        self, state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool:
        return (
            acceptance.get("reviewed_base_sha") == job.get("base_sha")
            and acceptance.get("reviewed_candidate_sha") == job.get("candidate_sha")
            and acceptance.get("reviewed_candidate_tree")
            == self.git.resolve(f"{job['candidate_sha']}^{{tree}}")
            and acceptance.get("effective_revision") == job.get("effective_revision")
        )

    def revision_changed(self, state: dict[str, Any], job: dict[str, Any]) -> bool:
        current = self.github.current_effective_revision(
            parent_number=int(_mapping(state, "parent")["number"]),
            ticket_number=int(job["ticket_number"]),
            expected_revision=str(job["effective_revision"]),
        )
        return current != str(job["effective_revision"])

    def requires_explicit_approval(
        self, _state: dict[str, Any], _job: dict[str, Any]
    ) -> bool:
        return False

    def resume_after_required_checks_failure(
        self, state: dict[str, Any], _job: dict[str, Any]
    ) -> bool:
        state.update(
            {
                "status": "ticket_delivery_pending",
                "terminal_kind": None,
                "diagnostics": [],
            }
        )
        return False

    def linked_issue_number(
        self, _state: dict[str, Any], job: dict[str, Any]
    ) -> int:
        return int(job["ticket_number"])

    def invocation_identity(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> tuple[str, int]:
        return ChangeDeliveryEngine._invocation_identity(state, job)


class TicketDeliveryPublisher(ChangeDeliveryPublisher):
    """Ticket mutations behind the shared Publisher seam."""

    def __init__(self, owner: TicketDeliveryLoop) -> None:
        self.owner = owner

    def commit_candidate(
        self, checkout: Path, job: dict[str, Any], attempt: int
    ) -> str | None:
        return self.owner.git.commit_candidate(
            checkout,
            ticket_number=int(job["ticket_number"]),
            attempt=attempt,
            expected_head=str(
                job.get("managed_checkout_head")
                or job.get("candidate_sha")
                or job["base_sha"]
            ),
            candidate_intent=(
                job.get("candidate_commit_intent")
                if isinstance(job.get("candidate_commit_intent"), dict)
                else None
            ),
        )

    def create_publication_commit(
        self, checkout: Path, job: dict[str, Any], message: str
    ) -> str:
        return self.owner.git.create_publication_commit(
            checkout,
            candidate_sha=str(job["candidate_sha"]),
            base_sha=str(job["base_sha"]),
            message=message,
        )

    def prepare_validation(
        self, _checkout: Path, job: dict[str, Any], validation: Path
    ) -> None:
        self.owner.git.prepare_validation_checkout(
            head_sha=str(job["candidate_sha"]), checkout=validation
        )

    def ensure_pr(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        publication: dict[str, Any],
    ) -> int:
        return self.owner.github.ensure_ticket_pr(
            branch=str(job["ticket_branch"]),
            base_branch=str(state["run_branch"]),
            title=str(publication["pr_title"]),
            body=self.owner._render_ticket_pr_body(state, job, publication),
            primary_ticket=int(job["ticket_number"]),
            expected_head_sha=str(job["publication_sha"]),
            expected_base_sha=str(job["base_sha"]),
        )

    def live_pull_request(
        self, _state: dict[str, Any], _job: dict[str, Any], pr_number: int
    ) -> dict[str, Any]:
        return self.owner.github.live_pull_request(pr_number)

    def invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.owner._invalidate_stale(state, job, checkout)

    def after_merge(
        self, state: dict[str, Any], job: dict[str, Any], live: dict[str, Any]
    ) -> bool:
        return self.owner._after_merge(state, job, live)

    def merge(
        self, state: dict[str, Any], job: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        return self.owner.github.squash_merge(
            pr_number=int(job["pr_number"]),
            expected_head_sha=str(job["publication_sha"]),
            run_branch=str(state["run_branch"]),
            commit_message=str(publication["commit_message"]),
        )

    def merge_description(self, _job: dict[str, Any]) -> str:
        return "squash merge"

    def escalate(
        self, state: dict[str, Any], job: dict[str, Any], code: str
    ) -> None:
        self.owner._escalate(state, job, code)

    def sync_attempts(self, _state: dict[str, Any], _job: dict[str, Any]) -> None:
        return None


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
        self.adapter = TicketDeliveryAdapter(git, github)
        self.publisher = TicketDeliveryPublisher(self)

    def run(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return self._engine(state, job).run(state, job, checkout)

    def _engine(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> ChangeDeliveryEngine:
        return ChangeDeliveryEngine(
            git=self.git,
            github=self.github,
            agents=self.agents,
            contract=ChangeJobContract(
                label=f"ticket-{int(job['ticket_number'])}",
                branch=str(job["ticket_branch"]),
                base_branch=str(state["run_branch"]),
                review_budget_policy=ticket_budget_policy_for_job(
                    job, state_snapshot=state.get("policy_snapshot")
                ),
            ),
            adapter=self.adapter,
            publisher=self.publisher,
            state_store=ChangeDeliveryStateStore(self.states, str(state["run_id"])),
        )

    def _invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        self.git.reset_checkout_to_base(checkout, str(state["run_branch"]))
        clear_required_checks_observation(job)
        for key in (
            "candidate_sha",
            "publication",
            "publication_sha",
            "acceptance_artifact",
            "acceptance_record",
            "publication_attempts",
            "publication_thread_id",
            "publication_operation_retry",
            "last_publication_error",
            "repair_source",
            "ci_evidence",
            "required_checks_origin",
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
            or not commit_messages_match(
                live.get("integrated_message"), publication.get("commit_message")
            )
            or live.get("integrated_parents") != [job.get("base_sha")]
        ):
            return self._block(
                state,
                job,
                "merged_result_mismatch",
                "Merged Ticket PR does not match Publisher merge intent",
            )
        if self.adapter.revision_changed(state, job):
            _record_superseded_integration(job, integrated)
            return self._block(
                state,
                job,
                "merged_revision_mismatch",
                "Merged Ticket PR was integrated, but no longer matches the current revision",
            )
        integration_record = job.get("deterministic_integration_record")
        integrated_tree = live.get("integrated_tree")
        integrated_message = live.get("integrated_message")
        integrated_parents = live.get("integrated_parents")
        if (
            not isinstance(integration_record, dict)
            or not isinstance(integrated_tree, str)
            or not integrated_tree
            or not isinstance(integrated_message, str)
            or not integrated_message
            or not isinstance(integrated_parents, list)
            or not all(
                isinstance(parent, str) and parent for parent in integrated_parents
            )
        ):
            return self._block(
                state,
                job,
                "merged_result_mismatch",
                "Merged Ticket PR is missing its canonical integrated commit evidence",
            )
        record_pr = integration_record.get("pr")
        if not isinstance(record_pr, dict):
            return self._block(
                state,
                job,
                "merged_result_mismatch",
                "Merged Ticket PR is missing its canonical PR evidence",
            )
        # The Integration Record is created before merge so publication can
        # persist its authority boundary.  Only the live post-merge facts can
        # establish the commit that actually entered the Run Branch.
        integrated_facts = {
            "integrated_sha": integrated,
            "integrated_publication_sha": str(job["publication_sha"]),
            "integrated_tree": integrated_tree,
            "integrated_message": integrated_message,
            "integrated_parents": list(integrated_parents),
        }
        integration_record.update(integrated_facts)
        record_pr.update({"state": "MERGED", "merge_commit_sha": integrated})
        job.update(integrated_facts)
        # Persist the integrated boundary before the external Issue mutation.
        job["phase"] = TicketPhase.MERGED.value
        self._save(state)
        try:
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
                        "ticket_close_intent_missing",
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
                    "ticket_close_ownership_missing",
                    "Ticket close dispatch did not establish exact ownership",
                )
        except GitHubReadError as error:
            if error.code == "ticket_close_external_conflict":
                job.update(
                    {
                        "phase": TicketPhase.BLOCKED.value,
                        "blocked_reason": "ticket_close_external_conflict",
                    }
                )
                state.update(
                    {
                        "status": "ready_for_human",
                        "terminal_kind": "waiting_human",
                        "diagnostics": [
                            {
                                "code": error.code,
                                "message": error.message,
                                "ticket_number": job["ticket_number"],
                            }
                        ],
                    }
                )
                self._save(state)
                return False
            if not is_github_convergence_error(error.code):
                raise
            wait_for_github_convergence(
                state,
                code=error.code,
                message=error.message,
                waiting_for="Ticket close ownership",
            )
            self._save(state)
            return False
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
        job["blocked_reason"] = code
        state["status"] = "blocked"
        state["diagnostics"] = [
            {
                "code": code,
                "message": "Ticket requires explicit human intervention",
                "ticket_number": job["ticket_number"],
            }
        ]

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


def _parent(state: dict[str, Any]) -> dict[str, Any]:
    parent = dict(_mapping(state, "parent"))
    parent["url"] = _issue_url(state, int(parent["number"]))
    return parent


def _ticket(state: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    ticket = dict(
        _mapping(
            _mapping(_mapping(state, "ticket_graph"), "tickets"),
            str(job["ticket_number"]),
        )
    )
    ticket["url"] = _issue_url(state, int(job["ticket_number"]))
    return ticket
