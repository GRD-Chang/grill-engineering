"""Implementation and directed-repair instructions selected by the caller."""

from __future__ import annotations

from typing import Any

from agent_run.prompt_resources import bind_resources, resource

from agent_run.prompt_context import (
    human_continuation,
    pretty,
    review_budget,
    task_brief,
    uses_short_role_prompt,
)


_REPAIR_SOURCES = {
    "acceptance": (
        "acceptance_artifact",
        "Acceptance Repair",
        'internal/repair-acceptance',
    ),
    "git_integrity": (
        "git_integrity_evidence",
        "Git Integrity Repair",
        'internal/repair-git-integrity',
    ),
    "required_checks": (
        "ci_evidence",
        "Required-Checks Repair",
        'internal/repair-required-checks',
    ),
    "human_revision": (
        "human_feedback",
        "Human Revision",
        'internal/repair-human-revision',
    ),
    "merge_conflict": (
        "merge_conflict_evidence",
        "Merge Conflict Repair",
        'internal/repair-merge-conflict',
    ),
}


def _repair_evidence(request: dict[str, Any]) -> tuple[str, str]:
    source = request.get("repair_source")
    if source is None:
        return "", ""
    if not isinstance(source, str) or source not in _REPAIR_SOURCES:
        raise ValueError(f"unknown repair_source: {source}")
    field, error_role, instruction = _REPAIR_SOURCES[source]
    evidence = request.get(field)
    if source in {"human_revision", "merge_conflict"}:
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"{error_role} requires {field}")
        raw = evidence
    else:
        if not isinstance(evidence, dict):
            raise ValueError(f"{error_role} requires {field}")
        raw = pretty(evidence)
    return resource(request, instruction), resource(request, "internal/repair-evidence").format(field, raw)


def development_prompt(
    request: dict[str, Any], *, force_continuation: bool = False
) -> str:
    request = bind_resources(request)
    repair = request.get("repair_source") is not None
    source_instruction, evidence = _repair_evidence(request)
    if force_continuation or uses_short_role_prompt(request):
        return "\n\n".join(
            block
            for block in (
                resource(request, 'internal/repair-resume' if repair else 'internal/development-resume'),
                task_brief(request, read_issues=False, development=True),
                source_instruction,
                evidence,
                review_budget(request, reviewer=False) if repair else "",
                human_continuation(request),
                resource(request, "internal/development-resume-output"),
            )
            if block
        )

    thread = request.get("thread_id")
    compact = (
        repair
        and isinstance(thread, str)
        and bool(thread.strip())
        and request.get("_invocation_mode") != "new-thread"
    )
    completion = (
        resource(request, "internal/repair-completion")
        if repair
        else resource(request, "internal/development-completion")
    )
    blocks = [
        resource(request, 'internal/repair-role' if repair else 'internal/development-role'),
        task_brief(request, read_issues=not compact, development=True),
        (
            resource(request, "internal/checkout").format(request['checkout'])
            if request.get("checkout") and not compact else ""
        ),
        (
            resource(request, "internal/requirement-baseline")
            if not compact else ""
        ),
        source_instruction,
        evidence,
        (
            resource(request, "internal/repair-requirement-refresh")
            if compact else ""
        ),
        resource(request, "methods/development-common"),
        resource(request, "methods/development-repair" if repair else "methods/development-initial"),
        resource(request, "internal/git"),
        resource(request, "internal/development-delivery-boundary"),
        completion,
        review_budget(request, reviewer=False) if repair else "",
        human_continuation(request),
        resource(request, "internal/output"),
    ]
    return "\n\n".join(block for block in blocks if block)
