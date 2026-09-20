"""Shared facts and role routing for model-visible worker instructions."""

from __future__ import annotations

import json
from typing import Any

from agent_run.prompt_resources import bind_resources, resource


def pretty(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def prompt_context(request: dict[str, Any], *fields: str) -> dict[str, Any]:
    context = {
        field: request[field]
        for field in fields
        if field != "human_response_history"
        and field in request
        and request[field] is not None
    }
    if "human_response_history" in fields:
        response = _latest_maintainer_response(request)
        if response is not None:
            context["latest_maintainer_response"] = response
    return context


def _latest_maintainer_response(request: dict[str, Any]) -> str | None:
    history = request.get("human_response_history")
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        if isinstance(item, dict):
            response = item.get("response")
            if isinstance(response, str) and response.strip():
                return response
    return None


def uses_short_role_prompt(
    request: dict[str, Any], *, reviewer: bool = False
) -> bool:
    if request.get("_invocation_mode") == "new-thread":
        return False
    thread_id = request.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return False
    if reviewer and not isinstance(request.get("current_review_identity"), dict):
        return False
    if request.get("_invocation_mode") == "resume" or reviewer:
        return True
    blockers = request.get("prior_human_blockers")
    return isinstance(blockers, list) and bool(blockers)


def task_brief(
    request: dict[str, Any], *, read_issues: bool, development: bool = False
) -> str:
    """Label requirement URLs without copying request bodies or controller state."""
    scope = request.get("acceptance_scope")
    parent = request.get("parent_issue_url")
    task = request.get("task_issue_url")
    lines: list[str] = []
    if scope in ("parent_only", "run"):
        if isinstance(parent, str) and parent.strip():
            lines.append(resource(request, "context/full-requirement-url").format(parent))
        lines.append(resource(request, "context/integrated-run" if scope == "run" else "context/full-requirement"))
        if scope == "run":
            if read_issues:
                lines.append(resource(request, "context/requirements-read-dependencies"))
    else:
        if isinstance(task, str) and task.strip():
            lines.append(resource(request, "context/task-url").format(task))
        if isinstance(parent, str) and parent.strip():
            lines.append(resource(request, "context/parent-url").format(parent))
        lines.append(
            resource(request, "context/child-task")
        )
        if development:
            lines.append(
                resource(request, "context/scope-existing-work")
            )
    checkout = request.get("checkout")
    if isinstance(checkout, str) and checkout.strip():
        lines.append(resource(request, "context/checkout").format(checkout))
    if read_issues:
        lines.extend(
            [
                "",
                resource(request, "context/requirements-read"),
            ]
        )
    return "\n".join(lines)


def human_continuation(request: dict[str, Any]) -> str:
    facts = prompt_context(request, "prior_human_blockers", "human_response_history")
    if not facts:
        return ""
    if "prior_human_blockers" in facts:
        facts["current_human_blockers"] = facts.pop("prior_human_blockers")
    return (
        resource(request, "context/human-response").format(pretty(facts))
    )


def review_budget(request: dict[str, Any], *, reviewer: bool) -> str:
    context = request.get("review_budget_context")
    if context is None:
        return ""
    if not isinstance(context, dict):
        raise ValueError("review_budget_context must be an object")
    remaining = context.get("remaining_review_attempts")
    if type(remaining) is not int or remaining < 0:
        raise ValueError("remaining_review_attempts must be a non-negative integer")
    if reviewer:
        current = context.get("current_review_attempt")
        if type(current) is not int or current < 1:
            raise ValueError("current_review_attempt must be a positive integer")
        text = (
            resource(request, "review/attempt-budget").format(current, remaining)
        )
    else:
        completed = context.get("completed_review_attempts")
        if type(completed) is not int or completed < 0:
            raise ValueError("completed_review_attempts must be a non-negative integer")
        text = resource(request, "development/review-budget").format(completed, remaining)
    return text


def structured_output_repair_prompt(output_name: str, contract_error: str, *, request: dict[str, Any] | None = None) -> str:
    request = bind_resources(request or {})
    roles = {
        "Development result": "development/output-repair",
        "Acceptance Artifact": "review/output-repair",
        "Publication Artifact": "publication/output-repair",
    }
    if output_name not in roles:
        raise ValueError(f"unknown structured output role: {output_name}")
    return resource(request, roles[output_name]).format(contract_error)
