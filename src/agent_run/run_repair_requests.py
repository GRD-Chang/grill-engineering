from __future__ import annotations

"""Run Repair worker request construction and PR narrative framing."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_run.agent_invocation import select_publication_thread
from agent_run.change_delivery import (
    MAX_PUBLICATION_CONTEXT_ATTEMPTS,
    latest_reviewer_thread,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.human_responses import current_human_response_history
from agent_run.run_currentness import ticket_completion_records


@dataclass(frozen=True)
class RunRepairRequests:
    git: GitRepository
    github: GitHubPublisher

    def development(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "run",
            "repair_scope": "run_repair",
            "repair_source": job.get("repair_source", "acceptance"),
            "parent_issue_url": _issue_url(
                state, int(_mapping(state, "parent")["number"])
            ),
            "run_id": state["run_id"],
            "parent": dict(_mapping(state, "parent")),
            "ticket_graph": _mapping(state, "ticket_graph"),
            "ticket_completion_records": ticket_completion_records(state),
            "base_sha": job["base_sha"],
            "head_sha": self.git.checkout_head(checkout),
            "checkout": str(checkout),
            "thread_id": (
                None
                if job.get("development_new_thread")
                else job.get("development_thread_id")
            ),
            "development_summary": job.get("development_summary"),
        }
        source = str(request["repair_source"])
        if source == "acceptance":
            artifact_key = (
                "acceptance_artifact"
                if isinstance(job.get("acceptance_artifact"), dict)
                else "repair_input_artifact"
            )
            request["acceptance_artifact"] = _mapping(job, artifact_key)
        elif source == "required_checks":
            request["ci_evidence"] = _mapping(job, "ci_evidence")
        elif source == "human_revision":
            request["human_feedback"] = str(job["human_feedback"])
        elif source == "merge_conflict":
            request["merge_conflict_evidence"] = str(job["merge_conflict_evidence"])
        _add_human_resume_fields(request, job)
        return request

    def publication(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request = {
            "acceptance_scope": "run",
            "parent_issue_url": _issue_url(
                state, int(_mapping(state, "parent")["number"])
            ),
            "run_id": state["run_id"],
            "parent": _mapping(state, "parent"),
            "ticket_graph": _mapping(state, "ticket_graph"),
            "ticket_completion_records": ticket_completion_records(state),
            "base_sha": job["base_sha"],
            "candidate_sha": job["candidate_sha"],
            "checkout": str(checkout),
            "thread_id": select_publication_thread(
                job, max_context_attempts=MAX_PUBLICATION_CONTEXT_ATTEMPTS
            ),
            "acceptance_artifact": _mapping(job, "acceptance_artifact"),
        }
        _add_human_resume_fields(request, job)
        return request

    def review(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "acceptance_scope": "run",
            "candidate_acceptance": True,
            "parent_issue_url": _issue_url(
                state, int(_mapping(state, "parent")["number"])
            ),
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
        _add_human_resume_fields(request, job)
        return request

    @staticmethod
    def render_pr_body(
        state: dict[str, Any], publication: dict[str, Any]
    ) -> str:
        parent = _mapping(state, "parent")
        narrative = str(publication["pr_body_markdown"]).strip()
        return (
            f"Parent Issue: #{int(parent['number'])}\n"
            "Delivery Type: Run Repair\n\n"
            f"{narrative}"
        )


def _add_human_resume_fields(
    request: dict[str, Any], job: dict[str, Any]
) -> None:
    if job.get("prior_human_blockers"):
        request["prior_human_blockers"] = job["prior_human_blockers"]
    history = current_human_response_history(
        job,
        generation=int(
            job.get("human_response_generation", job.get("repair_generation", 1))
        ),
    )
    if history:
        request["human_response_history"] = history


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
