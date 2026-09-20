"""独立验收的完整角色合同与同次执行续接。"""

from __future__ import annotations

from typing import Any

from agent_run.prompt_resources import bind_resources, resource

from agent_run.prompt_context import (
    human_continuation,
    pretty,
    prompt_context,
    review_budget,
    task_brief,
    uses_short_role_prompt,
)


def review_prompt(request: dict[str, Any]) -> str:
    request = bind_resources(request)
    if uses_short_role_prompt(request, reviewer=True):
        return review_continuation_prompt(request)
    blocks = (
        resource(request, "methods/acceptance"),
        task_brief(request, read_issues=True),
        _review_object(request),
        resource(request, "review/read-only-validation"),
        _integration_evidence(request),
        _previous_review(request),
        review_budget(request, reviewer=True),
        human_continuation(request),
        resource(request, "review/output"),
    )
    return "\n\n".join(block for block in blocks if block)


def review_continuation_prompt(request: dict[str, Any]) -> str:
    request = bind_resources(request)
    blocks = (
        resource(request, "review/resume"),
        task_brief(request, read_issues=False),
        _review_object(request),
        _integration_evidence(request),
        review_budget(request, reviewer=True),
        human_continuation(request),
    )
    return "\n\n".join(block for block in blocks if block)


def _review_object(request: dict[str, Any]) -> str:
    identity = request.get("current_review_identity")
    facts = identity if isinstance(identity, dict) else {}
    if request.get("acceptance_scope") != "run":
        description = resource(request, "review/commit-object")
    elif request.get("repair_scope") == "run_repair":
        description = (
            resource(request, "review/run-repair-object")
        )
    elif request.get("candidate_acceptance") is True:
        description = (
            resource(request, "review/candidate-object")
        )
    else:
        description = (
            resource(request, "review/run-object")
        )
    return "\n".join(
        [description, *_identity_lines(facts, request)]
    )


def _identity_lines(identity: dict[str, Any], request: dict[str, Any]) -> list[str]:
    labels = (
        (resource(request, "review/identity-run-base"), "run_base_sha"),
        (resource(request, "review/identity-repair-candidate"), "repair_candidate_sha"),
        (resource(request, "review/identity-default-base"), "default_base_sha"),
        (resource(request, "review/identity-run-head"), "run_head_sha"),
        (resource(request, "review/identity-candidate"), "candidate_sha"),
        (resource(request, "review/identity-reviewed-base"), "reviewed_base_sha"),
        (resource(request, "review/identity-reviewed-candidate"), "reviewed_candidate_sha"),
        (resource(request, "review/identity-reviewed-tree"), "reviewed_candidate_tree"),
        (resource(request, "review/identity-expected-tree"), "expected_merge_tree"),
    )
    return [
        f"{label}（{key}）：{identity[key]}"
        for label, key in labels
        if key in identity
    ]


def _previous_review(request: dict[str, Any]) -> str:
    artifact = request.get("previous_acceptance_artifact")
    if not isinstance(artifact, dict):
        return resource(request, "review/initial-baseline")
    identity = request.get("previous_review_identity")
    facts = identity if isinstance(identity, dict) else {}
    return resource(request, "review/previous-result").format(
        "\n".join(_identity_lines(facts, request)), pretty(artifact)
    )


def _integration_evidence(request: dict[str, Any]) -> str:
    if request.get("acceptance_scope") != "run":
        return ""
    evidence = prompt_context(
        request, "fallback_ticket_records", "ticket_integration_records"
    )
    if not evidence:
        return ""
    return (
        resource(request, "review/integration-evidence").format(pretty(evidence))
    )
