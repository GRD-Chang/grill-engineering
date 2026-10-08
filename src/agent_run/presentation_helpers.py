from __future__ import annotations

from agent_run.messages import display_text, display_language

import re
from datetime import UTC, datetime
from typing import Any

from agent_run.messages import text


_STATUS_TERMS = {
    'abandoned',
    'abandonment_pending',
    'acceptance_artifact',
    'accepted',
    'action_required',
    'active',
    'blocked',
    'cancel',
    'canceled',
    'cancelled',
    'candidate',
    'cleanup_pending',
    'closed',
    'committing_candidate',
    'completed',
    'currentness_invalidated',
    'deterministic_contradiction',
    'developing',
    'execution_failed',
    'fail',
    'failed',
    'failure',
    'git_integrity_repair',
    'github_convergence',
    'in_progress',
    'incompatible_run_state',
    'interrupted',
    'merged',
    'neutral',
    'no_code_changes',
    'none',
    'not_running',
    'open',
    'operator_stopped',
    'parent_approval_pending',
    'parent_closeout_pending',
    'parent_delivery_pending',
    'parent_phase',
    'pass',
    'pending',
    'progress_exhausted',
    'publication_artifact',
    'publication_pending',
    'publishing',
    'queued',
    'ready_for_integration',
    'ready_for_approval',
    'ready_for_human',
    'repairing',
    'requeue_required',
    'required_checks',
    'review_blocked',
    'review_failed',
    'review_passed',
    'reviewing',
    'run_acceptance',
    'run_acceptance_pending',
    'run_approval_pending',
    'run_publication',
    'run_publication_pending',
    'run_status',
    'running',
    'skipped',
    'skipping',
    'stale',
    'stale_result',
    'starting',
    'startup_failure',
    'success',
    'supervision_timeout',
    'ticket_completed',
    'ticket_phase',
    'timed_out',
    'timeline_capacity',
    'unavailable',
    'unknown',
    'unreviewed',
    'unsupported_scope_change',
    'validating',
    'waiting_checks',
    'waiting_external',
    'waiting_merge',
    '当前有效通过',
}
_PAUSE_REASONS = {
    'Change PR changed outside the current Generation',
    'Parent Issue requires explicit human intervention',
    'Run abandonment recovery is incomplete.',
    'acceptance_record_mismatch',
    'agent_requires_human',
    'candidate_or_acceptance_inconsistent',
    'initial Worker read credential is temporarily unavailable',
    'merged_result_mismatch',
    'modification_budget_exhausted',
    'published_head_mismatch',
    'review_budget_exhausted',
    'ci_development_budget_exhausted',
    'ci_investigation_paused',
    'ci_inner_budget_exhausted',
    'ci_outer_budget_exhausted',
    'reviewer_requires_human',
    'unexpected_external_merge',
    '外部状态在本次监督窗口内未收敛',
}
_PAUSE_STATUS_TERMS = {
    'abandonment_pending',
    'deterministic_contradiction',
    'execution_failed',
    'operator_stopped',
    'parent_approval_pending',
    'progress_exhausted',
    'publication_pending',
    'ready_for_human',
    'requeue_required',
    'run_approval_pending',
    'supervision_timeout',
    'unsupported_scope_change',
}

_UNSAFE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def terminal_safe(value: object) -> str:
    """Render persisted or user-provided text without terminal controls."""

    text = str(value)
    # Remove control bytes, but keep their printable payload.  For example,
    # ESC[2J becomes the harmless, copyable text [2J instead of silently
    # erasing the user's finding content.
    return _UNSAFE_CONTROL.sub("", text)


def human_ci_fix_usage(used: object, limit: object) -> str:
    """Describe the extra CI allowance without exposing its boolean encoding."""
    if limit == 0:
        return display_text('presentation.status.not_applicable')
    count = int(used) if isinstance(used, bool) else used
    return display_text('presentation.status.used_v1_v3_times', v1=count if count is not None else display_text('presentation.status.unknown'), v3=limit if limit is not None else display_text('presentation.status.unknown'))


def human_delivery_object(value: object) -> str:
    """Translate a display label, leaving machine work-subject identities intact."""
    text = str(value or "Delivery Run")
    labels = {
        "Delivery Run": display_text('presentation.status.overall_delivery'), "Run Acceptance": display_text('presentation.status.run_acceptance'),
        "Run Publication": display_text('presentation.status.overall_delivery'), "Run Repair": display_text('presentation.status.run_repair'),
    }
    if text in labels:
        return labels[text]
    match = re.fullmatch(r"Ticket (#\S+)", text)
    if match:
        return display_text('presentation.status.ticket_v1', v1=match[1])
    match = re.fullmatch(r"Parent Issue (#\S+)", text)
    if match:
        return display_text('presentation.status.parent_issue_v1', v1=match[1])
    match = re.fullmatch(r"Delivery Run（Parent Issue (#\S+)）", text)
    if match:
        return display_text('presentation.status.overall_delivery_requirement_v1', v1=match[1])
    return text


def human_agent_role(value: object) -> str:
    raw = str(value or "Agent")
    if "开发" in raw or raw in {"development", "Development Agent"}:
        return display_text('presentation.status.development_agent')
    if "验收" in raw or raw in {"review", "reviewer", "fresh_acceptance", "Review Agent"}:
        return display_text('presentation.status.review_agent')
    if "发布" in raw or raw in {"publication", "final_publication", "Publication Agent"}:
        return display_text('presentation.status.publication_agent')
    return "Agent" if raw != "Agent" else raw


def human_status_term(value: object, *, language: str | None = None) -> object:
    """One human vocabulary shared by status, history and notifications."""
    if not isinstance(value, str):
        return value
    key = value.lower()
    if key not in _STATUS_TERMS:
        key = "fallback"
    return text(f"guidance.status.{key}", language=language or display_language())


def status_diagnostic_command(state: dict[str, Any]) -> str:
    parent = state.get("parent")
    number = parent.get("number") if isinstance(parent, dict) else None
    repository = state.get("repository")
    if isinstance(repository, str) and type(number) is int:
        return f"agent-run status --repo {repository} --parent {number} --json"
    return "agent-run status --json"


def unknown_execution_guidance(state: dict[str, Any], *, include_command: bool = True) -> str:
    diagnostic = (
        display_text('presentation.status.ndiagnostic_command_v1', v1=status_diagnostic_command(state)) if include_command else ""
    )
    return (
        display_text('presentation.status.execution_status_cannot_be_confirmed_inspect_background_diagnosti', v1=diagnostic)
    )


def human_pause_reason(value: object, *, language: str | None = None) -> str:
    """Render controller reasons without rewriting user evidence."""
    raw = str(value)
    if raw in _PAUSE_STATUS_TERMS:
        return str(human_status_term(raw, language=language or display_language()))
    if raw in _PAUSE_REASONS:
        return text(f"guidance.pause.{raw}", language=language or display_language())
    return raw


def local_timestamp(value: object) -> str:
    """Render persisted UTC timestamps in the querying device's local timezone."""
    if not isinstance(value, str):
        return display_text('presentation.status.unknown')
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
    except ValueError:
        return display_text('presentation.status.unknown')
    fallback = False
    try:
        local = instant.astimezone()
    except (OSError, OverflowError, ValueError):
        local, fallback = instant.astimezone(UTC), True
    offset = local.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    suffix = display_text('presentation.status.local_timezone_unavailable_using_utc') if fallback else ""
    return f"{local:%Y-%m-%d %H:%M:%S} {zone}{suffix}"


def human_next_action(value: object, *, run_id: object = None) -> object:
    """Hide managed Run identifiers from ordinary operator instructions."""

    if not isinstance(value, str):
        return value
    if isinstance(run_id, str) and run_id:
        return re.sub(
            rf"(?<!\S){re.escape(run_id)}(?!\S)",
            "<run-id>",
            value,
        )
    return value


def elapsed_seconds_since(value: object, *, end: datetime | None = None) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        started = datetime.fromisoformat(value)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    finished = end or datetime.now(UTC)
    return max(0, int((finished - started).total_seconds()))


def execution_duration(seconds: object) -> str:
    """Format trusted execution seconds without dropping minute remainders."""
    if type(seconds) is not int or seconds < 0:
        return display_text('presentation.status.unknown')
    minutes, remainder = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return display_text('presentation.status.v0_hours_v2_minutes_v4_seconds', v0=hours, v2=minutes, v4=remainder)
    if minutes:
        return display_text('presentation.status.v0_minutes_v2_seconds', v0=minutes, v2=remainder)
    return display_text('presentation.status.v0_seconds', v0=seconds)
