"""提交说明与 PR 文案的只读角色合同。"""

from __future__ import annotations

from typing import Any

from agent_run.config.resources import bind_resources, resource

from agent_run.core.prompts.context import (
    human_continuation,
    pretty,
    prompt_context,
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
        resource(request, "methods/publishing"),
        task_brief(request, read_issues=True),
        evidence,
        human_continuation(request),
        resource(request, "publication/output"),
    )
    return "\n\n".join(block for block in blocks if block)


def publication_continuation_prompt(
    request: dict[str, Any], *, final_run: bool = False
) -> str:
    request = bind_resources(request)
    if final_run:
        request = _final_request(request)
    blocks = (
        resource(request, "publication/resume"),
        task_brief(request, read_issues=False),
        _publication_evidence(request),
        human_continuation(request),
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
    ci_repair = request.get("ci_repair_publication_context")
    artifact = request.get("acceptance_artifact")
    if isinstance(ci_repair, dict):
        evidence = resource(request, "publication/ci-repair-evidence").format(
            pretty(ci_repair)
        )
    elif isinstance(fallback, dict):
        evidence = (
            resource(request, "publication/fallback-evidence").format(pretty(fallback))
        )
    elif isinstance(artifact, dict):
        evidence = (
            resource(request, "publication/accepted-evidence")
            + "\n" + pretty(artifact)
        )
    else:
        raise ValueError(
            "Publication requires acceptance_artifact, fallback_publication_context, or ci_repair_publication_context"
        )
    scope = request.get("publication_context")
    if scope is not None:
        if not isinstance(scope, dict) or (scope.get("commit_range") is None) != (request.get("acceptance_scope") == "run"):
            raise ValueError("Publication scope must distinguish an actual commit from Final Run PR copy")
        boundaries = [scope.get("pr_range")]
        if scope.get("commit_range") is not None:
            boundaries.append(scope["commit_range"])
        for boundary in boundaries:
            if (not isinstance(boundary, dict)
                    or any(not isinstance(boundary.get(key), str) or not boundary[key] for key in ("base_sha", "head_sha"))
                    or boundary["head_sha"] != request.get("reviewed_commit")):
                raise ValueError("Publication scope must bind the fixed delivery result")
        evidence += "\n\n" + pretty({"publication_context": scope})
    facts = prompt_context(request, "acceptance_identity", "ticket_completions", "ci_evidence")
    if facts:
        evidence += "\n\n" + pretty(facts)
    feedback = request.get("human_feedback")
    if feedback is not None:
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValueError("Publication human_feedback must retain the original maintainer message")
        evidence += "\n\n" + resource(request, "context/human-response").format(pretty({"human_feedback": feedback}))
    return evidence
