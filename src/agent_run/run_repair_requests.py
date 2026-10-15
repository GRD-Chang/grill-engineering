from __future__ import annotations

"""Parent repair Development and Review requests."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_run.delivery_budget import delivery_budget_owner

from agent_run.artifacts import AcceptanceArtifact
from agent_run.change_delivery import (
    latest_reviewer_thread,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.human_responses import current_human_response_history
from agent_run.review_budget import previous_review_context
from agent_run.run_currentness import (
    ticket_completion_records,
    ticket_fallback_records,
    ticket_integration_records,
)


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
            "repository": str(state["repository"]),
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
        if source == "integration":
            request["integration_evidence"] = str(job["integration_evidence"])
        elif source == "git_integrity":
            request["git_integrity_evidence"] = _mapping(
                job, "git_integrity_evidence"
            )
        elif source == "acceptance":
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
        integration = job.get("run_repair_integration")
        if isinstance(integration, dict) and source == "acceptance" and job.get("pending_attempt_kind") != "ci_repair":
            request["repair_source"] = "integration"
            request["work_source"] = "integration"
            request["integration_evidence"] = json.dumps({
                "run_repair_integration": integration,
                "acceptance_artifact": request.get("acceptance_artifact"),
            }, ensure_ascii=False, sort_keys=True)
        _add_human_resume_fields(request, job)
        return request

    def review(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "acceptance_scope": "run",
            "candidate_acceptance": True,
            "run_review_facts": {
                "parent_revision": state["parent"]["revision"],
                "ticket_graph_revision": state["ticket_graph"]["revision"],
                "ticket_set": state["ticket_graph"]["ordered_ticket_numbers"],
                "ticket_completion_records": ticket_completion_records(state),
            },
            "repair_scope": "run_repair",
            "parent_issue_url": _issue_url(
                state, int(_mapping(state, "parent")["number"])
            ),
            "repository": str(state["repository"]),
            "current_review_identity": {
                "run_base_sha": str(job["base_sha"]),
                "repair_candidate_sha": str(job["candidate_sha"]),
                "expected_merge_tree": self.git.expected_merge_tree(
                    default_head_sha=str(job["default_base_sha"]),
                    run_head_sha=str(job["candidate_sha"]),
                ),
            },
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
        integration = job.get("run_repair_integration")
        if isinstance(integration, dict) and job.get("attempt_kind") != "ci_repair":
            request["work_source"] = "integration"
            request["run_integration_record"] = integration
        _add_human_resume_fields(request, job)
        fallbacks = ticket_fallback_records(state)
        if fallbacks:
            request["fallback_ticket_records"] = fallbacks
        integrations = ticket_integration_records(state)
        if integrations:
            request["ticket_integration_records"] = integrations
        baseline = job.get("previous_parent_acceptance_record")
        if isinstance(baseline, dict) and (
            baseline.get("acceptance_scope") == "run"
            and baseline.get("parent_revision") == state["parent"]["revision"]
            and baseline.get("ticket_graph_revision") == state["ticket_graph"]["revision"]
            and baseline.get("ticket_completion_records") == ticket_completion_records(state)
            and all(isinstance(baseline.get(field), str) and baseline[field].strip()
                    for field in ("reviewer_thread_id", "reviewed_default_base_sha", "expected_merge_tree"))
            and isinstance(baseline.get("reviewed_head_sha", baseline.get("reviewed_candidate_sha")), str)
        ):
            try:
                accepted = AcceptanceArtifact.parse(baseline.get("artifact")).is_accepted
            except ValueError:
                accepted = False
            if accepted:
                request["parent_acceptance_baseline"] = baseline
                request["previous_acceptance_artifact"] = baseline["artifact"]
                request["previous_review_identity"] = {
                    "run_head_sha": baseline.get("reviewed_head_sha", baseline.get("reviewed_candidate_sha")),
                    "default_base_sha": baseline.get("reviewed_default_base_sha"),
                    "expected_merge_tree": baseline.get("expected_merge_tree"),
                }
        previous = previous_review_context(delivery_budget_owner(state, job))
        if previous is not None:
            request["previous_acceptance_artifact"] = previous["artifact"]
            request["previous_review_identity"] = previous["identity"]
        return request

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
