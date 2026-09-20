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
        'development/repair-acceptance',
    ),
    "git_integrity": (
        "git_integrity_evidence",
        "Git Integrity Repair",
        'development/repair-git-integrity',
    ),
    "required_checks": (
        "ci_evidence",
        "Required-Checks Repair",
        'development/repair-required-checks',
    ),
    "human_revision": (
        "human_feedback",
        "Human Revision",
        'development/repair-human-revision',
    ),
    "merge_conflict": (
        "merge_conflict_evidence",
        "Merge Conflict Repair",
        'development/repair-merge-conflict',
    ),
}


def _repair_evidence(request: dict[str, Any]) -> str:
    source = request.get("repair_source")
    if source is None:
        return ""
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
    return resource(request, instruction).format(field, raw)


def development_prompt(
    request: dict[str, Any], *, force_continuation: bool = False
) -> str:
    request = bind_resources(request)
    repair = request.get("repair_source") is not None
    evidence = _repair_evidence(request)
    if force_continuation or uses_short_role_prompt(request):
        return "\n\n".join(
            block
            for block in (
                resource(request, 'development/repair-resume' if repair else 'development/resume'),
                task_brief(request, read_issues=False, development=True),
                evidence,
                review_budget(request, reviewer=False) if repair else "",
                human_continuation(request),
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
    blocks = [
        resource(request, 'methods/repair' if repair else 'methods/development'),
        task_brief(request, read_issues=not compact, development=True),
        (
            resource(request, "context/requirement-baseline")
            if not compact else ""
        ),
        evidence,
        (
            resource(request, "development/repair-requirement-refresh")
            if compact else ""
        ),
        review_budget(request, reviewer=False) if repair else "",
        human_continuation(request),
        resource(request, "development/output"),
    ]
    return "\n\n".join(block for block in blocks if block)
