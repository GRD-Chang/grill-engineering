from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_run.agents import AgentBackend
from agent_run.change_delivery import (
    MAX_PUBLICATION_CONTEXT_ATTEMPTS,
    ChangeDeliveryEngine,
    ChangeJobContract,
    latest_reviewer_thread,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.state import StateStore


class ParentDeliveryLoop:
    """Adapt the shared change lifecycle to the Parent-only delivery boundary."""

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
                label=lambda _job: "parent-only",
                branch=lambda job: str(job["parent_branch"]),
                base_branch=lambda state: str(_mapping(state, "base")["branch"]),
                candidate=lambda checkout, _job, attempt: self.git.commit_candidate(
                    checkout, ticket_number=0, attempt=attempt
                ),
                development_thread_is_allowed=lambda _state, _thread_id: True,
                development_request=self._development_request,
                publication_request=self._publication_request,
                review_request=self._review_request,
                prepare_validation=lambda _checkout, job, validation: self.git.prepare_validation_checkout(
                    head_sha=str(job["candidate_sha"]), checkout=validation
                ),
                ensure_pr=lambda state, job, publication: self.github.ensure_parent_pr(
                    branch=str(job["parent_branch"]),
                    base_branch=str(_mapping(state, "base")["branch"]),
                    title=str(publication["pr_title"]),
                    body=self._render_pr_body(state, publication),
                ),
                acceptance_record=self._acceptance_record,
                acceptance_is_current=self._acceptance_is_current,
                invalidate_stale_publication=self._invalidate_stale_publication,
                revision_changed=self._revision_changed,
                requires_explicit_approval=lambda _state, _job: True,
                after_merge=lambda _state, _job, _live: True,
                escalate=self._escalate,
                save=self._save,
            ),
        )

    def run(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        return self.engine.run(state, job, checkout)

    def _acceptance_record(
        self, _state: dict[str, Any], job: dict[str, Any], reviewer: str, artifact: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "acceptance_scope": "parent_only",
            "reviewed_base_sha": job["base_sha"],
            "reviewed_candidate_sha": job["candidate_sha"],
            "reviewed_candidate_tree": self.git.resolve(f"{job['candidate_sha']}^{{tree}}"),
            "effective_revision": job["effective_revision"],
            "reviewer_thread_id": reviewer,
            "artifact": artifact,
        }

    def _acceptance_is_current(
        self, state: dict[str, Any], job: dict[str, Any], acceptance: dict[str, Any]
    ) -> bool:
        return (
            acceptance.get("reviewed_base_sha") == job.get("base_sha")
            and acceptance.get("reviewed_candidate_sha") == job.get("candidate_sha")
            and acceptance.get("reviewed_candidate_tree") == self.git.resolve(f"{job['candidate_sha']}^{{tree}}")
            and not self._revision_changed(state, job)
        )

    @staticmethod
    def _render_pr_body(state: dict[str, Any], publication: dict[str, Any]) -> str:
        parent = _mapping(state, "parent")
        return (
            f"Parent Issue: #{int(parent['number'])}\n"
            "Delivery Type: Parent-only\n\n"
            f"{str(publication['pr_body_markdown']).strip()}"
        )

    @staticmethod
    def _parent(state: dict[str, Any]) -> dict[str, Any]:
        parent = dict(_mapping(state, "parent"))
        parent["url"] = f"https://github.com/{state['repository']}/issues/{parent['number']}"
        return parent

    def _development_request(self, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> dict[str, Any]:
        request = {
            "acceptance_scope": "parent_only",
            "parent_issue_url": self._parent(state)["url"],
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "head_sha": self.git.checkout_head(checkout),
            "checkout": str(checkout),
            "thread_id": job.get("development_thread_id"),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        if job.get("repair_source") == "acceptance":
            request["repair_source"] = "acceptance"
            request["acceptance_artifact"] = _mapping(job, "acceptance_artifact")
        elif job.get("repair_source") == "required_checks":
            request["repair_source"] = "required_checks"
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    def _publication_request(self, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> dict[str, Any]:
        request = {
            "acceptance_scope": "parent_only",
            "parent_issue_url": self._parent(state)["url"],
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "effective_revision": job["effective_revision"],
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "checkout": str(checkout),
            "thread_id": (
                job.get("publication_thread_id")
                if job.get("prior_human_blockers")
                else job["development_thread_id"]
                if int(job.get("publication_attempts", 0))
                < MAX_PUBLICATION_CONTEXT_ATTEMPTS
                else None
            ),
            "acceptance_artifact": _mapping(job, "acceptance_artifact"),
        }
        if job.get("prior_human_blockers"):
            request["prior_human_blockers"] = job["prior_human_blockers"]
        existing_pr = job.get("pr_number")
        if isinstance(existing_pr, int):
            request["existing_pr"] = self.github.publication_context(existing_pr)
        if job.get("repair_source") == "required_checks":
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        return request

    def _invalidate_stale_publication(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None:
        base_branch = str(_mapping(state, "base")["branch"])
        base_sha = self.git.resolve(base_branch)
        self.git.reset_checkout_to_base(checkout, base_branch)
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
                "base_sha": base_sha,
                "phase": "developing",
                "validation_attempts": 0,
            }
        )
        state["status"] = "parent_delivery_pending"
        state["diagnostics"] = []

    def _review_request(self, state: dict[str, Any], job: dict[str, Any], checkout: Path) -> dict[str, Any]:
        return {
            "acceptance_scope": "parent_only",
            "parent_issue_url": self._parent(state)["url"],
            "run_id": state["run_id"],
            "parent": self._parent(state),
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "effective_revision": job["effective_revision"],
            "checkout": str(checkout),
            "thread_id": latest_reviewer_thread(job)
            if job.get("prior_human_blockers")
            else None,
            **(
                {"prior_human_blockers": job["prior_human_blockers"]}
                if job.get("prior_human_blockers")
                else {}
            ),
        }

    @staticmethod
    def _revision_changed(state: dict[str, Any], job: dict[str, Any]) -> bool:
        return _mapping(state, "parent").get("revision") != job.get("effective_revision")

    @staticmethod
    def _escalate(state: dict[str, Any], job: dict[str, Any], code: str) -> None:
        job["blocked_reason"] = code
        state["status"] = "blocked"
        state["diagnostics"] = [{"code": code, "message": "Parent Issue requires explicit human intervention"}]

    def _save(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.save_run(str(state["run_id"]), state)
        return state


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
