"""提交说明与 PR 文案的只读角色合同。"""

from __future__ import annotations

from typing import Any

from agent_run.prompt_resources import bind_resources, resource

from agent_run.prompt_context import (
    human_continuation,
    pretty,
    task_brief,
    uses_short_role_prompt,
)


def publication_prompt(
    request: dict[str, Any], *, final_run: bool = False
) -> str:
    request = bind_resources(request)
    if final_run:
        request = _final_request(request)
    evidence = _publication_evidence(request)
    if uses_short_role_prompt(request):
        return publication_continuation_prompt(request)
    blocks = (
        resource(request, "internal/publication-role"),
        task_brief(request, read_issues=True),
        evidence,
        human_continuation(request),
        resource(request, "internal/publication-boundary"),
        resource(request, "methods/publication"),
        resource(request, "internal/publication-output"),
    )
    return "\n\n".join(block for block in blocks if block)


def publication_continuation_prompt(
    request: dict[str, Any], *, final_run: bool = False
) -> str:
    request = bind_resources(request)
    if final_run:
        request = _final_request(request)
    blocks = (
        resource(request, "internal/publication-resume"),
        task_brief(request, read_issues=False),
        _publication_evidence(request),
        human_continuation(request),
        resource(request, "internal/publication-resume-output"),
    )
    return "\n\n".join(block for block in blocks if block)


def _final_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request.get("acceptance_artifact"), dict):
        raise ValueError("Final Run Publication requires acceptance_artifact")
    return {
        **{key: value for key, value in request.items() if key != "fallback_publication_context"},
        "acceptance_scope": "run",
    }


def _publication_evidence(request: dict[str, Any]) -> str:
    fallback = request.get("fallback_publication_context")
    artifact = request.get("acceptance_artifact")
    if isinstance(fallback, dict):
        return (
            resource(request, "internal/publication-fallback-evidence")
            + pretty(fallback)
            + resource(request, "internal/publication-fallback-boundary")
        )
    if isinstance(artifact, dict):
        return (
            resource(request, "internal/publication-acceptance-evidence")
            + pretty(artifact)
        )
    raise ValueError(
        "Publication requires acceptance_artifact or fallback_publication_context"
    )
