"""Read-only semantic history projection shared by queries and notifications.

Retained Attempts, Invocation identity, and lifecycle snapshots are authoritative.
This module associates and classifies those facts without choosing display language.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from agent_run.delivery_facts import delivery_object_label, role_family as _role_family
from agent_run.execution_timing import (
    invocation_activity,
    invocation_execution_seconds,
    parse_timestamp as _parse_timestamp,
    parse_optional_timestamp as _parse_optional_timestamp,
)
from agent_run.acceptance_facts import artifact_findings as _artifact_findings
from agent_run.history_check_facts import CHECKS_HISTORY_FIELDS
from agent_run.history_publication import group_final_pr_creation
from agent_run.history_waits import collapse_check_waits
from agent_run.attempt_evidence import (
    artifact_outcome,
    _budget_facts_for_attempt,
    _development_summary_for_attempt,
    owners_for_attempt,
    _publication_for_attempt,
    review_artifact_for_attempt,
    _supporting_records,
)

def _history_turning_points(
    state: dict[str, Any], audit: dict[str, Any]
) -> list[dict[str, Any]]:
    """Project persisted lifecycle facts used as timeline turning points.

    Ordinary timeline snapshots are deliberately omitted.  They describe the
    same Attempt repeatedly and must not become extra human-facing rounds.
    Attempt identities associate lifecycle events with their semantic records;
    they are not added to the separate public JSON event projection.
    """

    points: list[dict[str, Any]] = []
    timeline_events: list[dict[str, Any]] = []
    order = 0
    for timeline in (audit.get("timeline"), audit.get("timeline_continuation")):
        if not isinstance(timeline, list):
            continue
        for raw in timeline:
            if not isinstance(raw, dict) or not isinstance(raw.get("at"), str):
                continue
            timestamp = utc_timestamp(raw["at"])
            if timestamp is None:
                continue
            blockers = raw.get("human_blockers")
            details = (
                [item for item in blockers if isinstance(item, str)]
                if isinstance(blockers, list)
                else []
            )
            kind = _human_timeline_kind(raw, details)
            if (
                kind == "publication" and raw.get("semantic_attempt_id")
                and raw.get("status") == "run_publication_pending"
                and raw.get("pr_number") is None
            ):
                continue
            scope_details = scope_change_details(raw)
            details.extend(scope_details)
            event = {
                "at": timestamp,
                "kind": kind,
                "object": (
                    subject_label(state, raw["history_work_subject"])
                    if isinstance(raw.get("history_work_subject"), str)
                    else timeline_object(state, raw)
                ),
                "phase": raw.get("phase"),
                "status": raw.get("status"),
                "role": raw.get("worker"),
                "round": raw.get("attempt"),
                "details": details,
                "semantic_attempt_id": raw.get("semantic_attempt_id"),
                "scope_ticket_changes": scope_ticket_changes(raw),
                "_order": order,
            }
            for key in (
                "pr_number",
                "commit_sha",
                "thread_id",
                "approval_granted_at",
                "required_checks_result",
                "required_checks_observed_at",
                "required_checks_evidence",
                "next_action",
                "result",
                "explicit_resume_sequence",
                "explicit_resume_kind",
                "explicit_resume_thread_id",
                "explicit_resume_attempt_id",
                *CHECKS_HISTORY_FIELDS,
            ):
                if raw.get(key) is not None:
                    event[key] = raw[key]
            timeline_events.append(event)
            order += 1

    timeline_events.sort(key=lambda event: (event["at"], event["_order"]))
    points.extend(_collapse_human_blocker_snapshots(timeline_events))

    responses = human_responses_by_subject(state)
    response_audit = state.get("human_response_audit")
    audited_responses = response_audit if isinstance(response_audit, dict) else {}
    resumes = audit.get("agent_resumes")
    if isinstance(resumes, list):
        for resume in resumes:
            if not isinstance(resume, dict):
                continue
            raw_at = resume.get("requested_at")
            timestamp = utc_timestamp(raw_at) if isinstance(raw_at, str) else None
            if timestamp is None:
                continue
            response_details: list[str] = []
            if resume.get("human_response_supplied") is True:
                audited_response = audited_responses.get(resume.get("resume_id"))
                if isinstance(audited_response, str):
                    response_details.append(audited_response)
                else:
                    subject_responses = responses.get(
                        (
                            str(resume.get("work_subject") or ""),
                            resume.get("generation"),
                        )
                    )
                    if subject_responses:
                        response_details.append(subject_responses.pop(0))
            points.append(
                {
                    "at": timestamp,
                    "kind": "resume",
                    "object": subject_label(
                        state, str(resume.get("work_subject") or "")
                    ),
                    "phase": resume.get("source_status"),
                    "status": "resumed",
                    "role": None,
                    "round": None,
                    "details": response_details,
                    "semantic_attempt_id": resume.get("semantic_attempt_id"),
                    "resume_id": resume.get("resume_id"),
                    "_order": order,
                }
            )
            order += 1
    points.sort(key=lambda event: (event["at"], event["_order"]))
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for point in points:
        identity = _history_turning_point_identity(point)
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(point)
    points = deduplicated
    for point in points:
        point.pop("_order", None)
        point.pop("_human_blocker_occurrence", None)
    return group_final_pr_creation(collapse_check_waits(points))

def _collapse_human_blocker_snapshots(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one human pause per blocked interval in the human timeline.

    Resume authorization is persisted before the old blocker is cleared. The
    resulting ready-for-human snapshot has a newer resume sequence but still
    belongs to the same blocked interval. A later pause is distinct only
    after a persisted non-ready snapshot proves that the interval ended.
    """

    active_scopes: set[tuple[str, str, str, str]] = set()
    occurrences: dict[tuple[str, str, str, str], int] = {}
    turning_points: list[dict[str, Any]] = []
    for event in events:
        scope = _human_blocker_scope(event)
        details = event.get("details")
        is_blocker = (
            isinstance(details, list)
            and bool(details)
            and _is_human_blocker_event(event)
        )
        if is_blocker:
            if scope in active_scopes:
                continue
            active_scopes.add(scope)
            event["_human_blocker_occurrence"] = occurrences.get(scope, 0)
            occurrences[scope] = occurrences.get(scope, 0) + 1
        elif str(event.get("status") or "") != "ready_for_human":
            active_scopes.discard(scope)
        if _is_history_turning_point(event):
            turning_points.append(event)
    return turning_points

def _human_blocker_scope(event: dict[str, Any]) -> tuple[str, str, str, str]:
    semantic_attempt_id = str(event.get("semantic_attempt_id") or "")
    return (
        str(event.get("object") or ""),
        semantic_attempt_id,
        "" if semantic_attempt_id else str(event.get("role") or ""),
        "" if semantic_attempt_id else str(event.get("round") or ""),
    )

def _history_turning_point_identity(point: dict[str, Any]) -> str:
    """Deduplicate repeated internal snapshots without hiding operations.

    Timeline snapshots can differ only because an invocation counter or its
    timestamp changed.  Those are not user-visible business transitions.  A
    concrete publication/check state remains distinct by its business fields;
    integration records with the same PR and commit are one integration even
    when persistence observes both the merged and completed phases. Human
    blockers are keyed by their proven blocked interval so repeated operator
    actions and later real pauses stay visible.
    """

    resume_id = point.get("resume_id")
    if point.get("kind") == "resume" and isinstance(resume_id, str) and resume_id:
        return json.dumps(
            {"kind": "resume", "resume_id": resume_id},
            ensure_ascii=False,
            sort_keys=True,
        )
    kind = point.get("kind")
    can_deduplicate_snapshot = (
        kind == "publication"
        and any(
            point.get(key) is not None
            for key in (
                "semantic_attempt_id",
                "round",
                "thread_id",
                "pr_number",
                "commit_sha",
            )
        )
    )
    if kind == "human_blocker":
        # A ready_for_human marker is persisted more than once while the
        # controller waits. The projection assigns an occurrence after a
        # proven non-ready boundary, so a later real pause remains visible
        # even when it has the same resume sequence and blocker text.
        return json.dumps(
            {
                "kind": kind,
                "object": point.get("object"),
                "semantic_attempt_id": point.get("semantic_attempt_id"),
                "round": point.get("round"),
                "role": point.get("role"),
                "phase": point.get("phase"),
                "status": point.get("status"),
                "details": point.get("details"),
                "human_blocker_occurrence": point.get(
                    "_human_blocker_occurrence"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    if can_deduplicate_snapshot:
        return json.dumps(
            {
                "kind": kind,
                "object": point.get("object"),
                "semantic_attempt_id": point.get("semantic_attempt_id"),
                "round": point.get("round"),
                "role": point.get("role"),
                "thread_id": point.get("thread_id"),
                "phase": point.get("phase"),
                "status": point.get("status"),
                "details": point.get("details"),
                "pr_number": point.get("pr_number"),
                "commit_sha": point.get("commit_sha"),
                "approval_granted_at": point.get("approval_granted_at"),
                "required_checks_result": point.get("required_checks_result"),
                "required_checks_observed_at": point.get(
                    "required_checks_observed_at"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    if kind == "integration":
        pr_number = point.get("pr_number")
        commit_sha = point.get("commit_sha")
        if pr_number is not None or commit_sha is not None:
            return json.dumps(
                {
                    "kind": "integration",
                    "object": point.get("object"),
                    "pr_number": pr_number,
                    "commit_sha": commit_sha,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
    return json.dumps(
        {key: value for key, value in point.items() if key != "_order"},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )

def history_records(
    state: dict[str, Any],
    audit: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate raw snapshots and invocations into one record per Attempt.

    This is a read-only projection.  Raw timeline events remain in the JSON
    audit output; History and notifications consume the same semantic records.
    """

    attempts = audit.get("semantic_agent_attempts")
    invocations = audit.get("agent_invocations")
    attempt_values = attempts if isinstance(attempts, list) else []
    invocation_values = invocations if isinstance(invocations, list) else []
    records_by_key: dict[str, dict[str, Any]] = {}
    for attempt in attempt_values:
        if not isinstance(attempt, dict):
            continue
        _ensure_history_record(records_by_key, attempt=attempt)
    for invocation in invocation_values:
        if not isinstance(invocation, dict):
            continue
        attempt = invocation.get("semantic_attempt")
        attempt_view = attempt if isinstance(attempt, dict) else {}
        _ensure_history_record(
            records_by_key,
            attempt=attempt_view,
            invocation=invocation,
        )["invocations"].append(invocation)

    raw_events = _history_turning_points(state, audit)
    if not raw_events and isinstance(events, list):
        # Keep malformed/legacy state visible when no persisted turning point
        # can be reconstructed from the raw audit fields. Invocation events
        # already belong to their Attempt and must not become duplicate
        # standalone records merely because their review details are long.
        raw_events = [
            deepcopy(event)
            for event in events
            if isinstance(event, dict)
            and event.get("details")
            and _role_family(str(event.get("kind") or "")) is None
        ]
    event_by_attempt: dict[str, list[dict[str, Any]]] = {}
    standalone: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        attempt_id = raw.get("semantic_attempt_id")
        if isinstance(attempt_id, str) and raw.get("kind") not in {
            "required_checks", "integration", "completion", "approval", "supervision", "abandonment", "pr_creation",
        }:
            event_by_attempt.setdefault(attempt_id, []).append(raw)
        elif _is_history_turning_point(raw):
            standalone.append(raw)
    for key, record in records_by_key.items():
        attempt_id = record.get("attempt_id")
        if isinstance(attempt_id, str):
            record["turning_points"] = event_by_attempt.get(attempt_id, [])
        else:
            record["turning_points"] = []
        _finalize_history_record(state, record)

    for index, raw in enumerate(standalone):
        record = _ensure_history_record(
            records_by_key,
            attempt={
                "attempt_id": (
                    f"event:{raw.get('at')}:{raw.get('kind')}:{index}"
                ),
                "work_subject": raw.get("object"),
                "role": "event",
                "ordinal": None,
                "event_record": True,
                "event_at": raw.get("at"),
            },
        )
        record.setdefault("turning_points", []).append(raw)
        _finalize_history_record(state, record)

    result = list(records_by_key.values())
    result.sort(key=lambda item: (_record_start(item), _record_order(item)))
    return result

def _ensure_history_record(
    records: dict[str, dict[str, Any]],
    *,
    attempt: dict[str, Any],
    invocation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = _attempt_key(attempt, invocation)
    record = records.get(key)
    if record is None:
        record = {
            "attempt_id": attempt.get("attempt_id"),
            "role": attempt.get("role")
            or (invocation or {}).get("role")
            or (invocation or {}).get("invocation_role"),
            "work_subject": attempt.get("work_subject")
            or (invocation or {}).get("work_subject"),
            "generation": attempt.get("generation")
            or (invocation or {}).get("generation"),
            "ordinal": attempt.get("ordinal"),
            "budget_window": attempt.get("budget_window"),
            "attempt": dict(attempt),
            "invocations": [],
            "turning_points": [],
            "event_record": attempt.get("event_record") is True,
            "event_at": attempt.get("event_at"),
        }
        records[key] = record
    elif record.get("attempt") is not attempt and attempt:
        # The persisted Attempt is authoritative. An Invocation contains a
        # startup mirror which may still say "pending" after the Attempt was
        # completed. Only fill genuinely absent identity fields from it.
        authoritative = record.get("attempt")
        if not isinstance(authoritative, dict) or not authoritative:
            record["attempt"] = dict(attempt)
        else:
            for key in (
                "attempt_id",
                "role",
                "work_subject",
                "generation",
                "currentness_boundary_fingerprint",
                "ordinal",
                "budget_window",
            ):
                if authoritative.get(key) is None and attempt.get(key) is not None:
                    authoritative[key] = attempt[key]
                    if record.get(key) is None:
                        record[key] = attempt[key]
    return record

def _attempt_key(
    attempt: dict[str, Any], invocation: dict[str, Any] | None = None
) -> str:
    attempt_id = attempt.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        return f"id:{attempt_id}"
    values = {
        key: attempt.get(key, (invocation or {}).get(key))
        for key in (
            "role",
            "work_subject",
            "generation",
            "currentness_boundary_fingerprint",
            "ordinal",
            "budget_window",
        )
    }
    return "identity:" + json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)

def _finalize_history_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    if record.get("event_record") is True:
        _finalize_event_record(state, record)
        return
    invocations = [
        invocation
        for invocation in record.get("invocations", [])
        if isinstance(invocation, dict)
    ]
    record["object"] = subject_label(
        state, str(record.get("work_subject") or "run")
    )
    starts = [
        timestamp
        for invocation in invocations
        if (timestamp := _parse_optional_timestamp(invocation.get("started_at"))) is not None
    ]
    ends = [
        timestamp
        for invocation in invocations
        if (timestamp := _parse_optional_timestamp(invocation.get("ended_at"))) is not None
    ]
    record["started_at"] = min(starts).isoformat() if starts else None
    has_unclosed_invocation = any(
        invocation.get("ended_at") is None for invocation in invocations
    )
    if has_unclosed_invocation:
        record["ended_at"] = None
    elif ends:
        record["ended_at"] = max(ends).isoformat()
    else:
        record["ended_at"] = None
    record["activity"] = _record_activity(state, record, invocations)
    record["span_seconds"] = _record_span_seconds(record)
    record["execution_seconds"] = _record_execution_seconds(record)
    record["resumption_count"] = sum(
        1 for invocation in invocations if invocation.get("resume_id") is not None
    )
    record["output_attempts"] = [
        invocation.get("attempt_count")
        for invocation in invocations
        if type(invocation.get("attempt_count")) is int
    ]
    record["configurations"] = _record_configurations(invocations)
    details = _history_record_details(state, record)
    record.update(details)
    record["status_code"] = history_record_status_code(record)

def _finalize_event_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    """Finalize a lifecycle event without inventing an Agent Attempt."""

    event = next(
        (
            point
            for point in record.get("turning_points", [])
            if isinstance(point, dict)
        ),
        {},
    )
    event_at = event.get("at") or record.get("event_at")
    record["object"] = str(record.get("work_subject") or "Delivery Run")
    record["started_at"] = event.get("wait_started_at", event_at)
    record["ended_at"] = event.get("wait_observed_until", event_at)
    record["activity"] = "not_running"
    start = _parse_optional_timestamp(record["started_at"])
    end = _parse_optional_timestamp(record["ended_at"])
    record["span_seconds"] = (
        max(0, int((end - start).total_seconds()))
        if event.get("kind") in {"required_checks", "approval", "pr_creation"} and start and end else None
    )
    record["execution_seconds"] = None
    record["resumption_count"] = 0
    record["output_attempts"] = []
    record["recovery_details"] = []
    record["configurations"] = []
    record["findings"] = []
    record["acceptance_artifact"] = None
    record["development_summary"] = None
    record["publication"] = None
    record["supporting_records"] = []

def _record_start(record: dict[str, Any]) -> datetime:
    timestamp = _parse_optional_timestamp(record.get("started_at"))
    if timestamp is not None:
        return timestamp
    timestamp = _parse_optional_timestamp(record.get("ended_at"))
    return timestamp or datetime.max.replace(tzinfo=UTC)

def _record_order(record: dict[str, Any]) -> tuple[str, str, int]:
    ordinal = record.get("ordinal")
    return (
        str(record.get("work_subject") or ""),
        str(record.get("role") or ""),
        ordinal if type(ordinal) is int else 0,
    )

def _record_span_seconds(record: dict[str, Any]) -> int | None:
    invocations = record.get("invocations")
    if isinstance(invocations, list) and any(
        isinstance(invocation, dict)
        and _parse_optional_timestamp(invocation.get("started_at")) is not None
        and _parse_optional_timestamp(invocation.get("ended_at")) is None
        for invocation in invocations
    ):
        # A current, trusted running observation is the one case where query
        # time is meaningful. Interrupted/unknown observations remain open.
        if record.get("activity") != "running":
            return None
        started = _parse_optional_timestamp(record.get("started_at"))
        return (
            max(0, int((datetime.now(UTC) - started).total_seconds()))
            if started is not None
            else None
        )
    started = _parse_optional_timestamp(record.get("started_at"))
    ended = _parse_optional_timestamp(record.get("ended_at"))
    if started is None:
        return None
    if ended is None:
        if record.get("activity") != "running":
            return None
        ended = datetime.now(UTC)
    return max(0, int((ended - started).total_seconds()))

def _record_execution_seconds(record: dict[str, Any]) -> int | None:
    invocations = record.get("invocations")
    if not isinstance(invocations, list) or not invocations:
        return None
    last_invocation_index = len(invocations) - 1
    open_invocation_indices = [
        index
        for index, invocation in enumerate(invocations)
        if isinstance(invocation, dict)
        and _parse_optional_timestamp(invocation.get("started_at")) is not None
        and _parse_optional_timestamp(invocation.get("ended_at")) is None
    ]
    if any(index != last_invocation_index for index in open_invocation_indices):
        # An older invocation without a persisted end is not made complete by
        # observing a later invocation running.  Its execution interval is
        # unknowable, so the aggregate must remain unknown.
        return None
    total = 0
    for index, invocation in enumerate(invocations):
        if not isinstance(invocation, dict):
            return None
        allow_open = (
            index == last_invocation_index
            and record.get("activity")
            in {"running", "capacity_wait", "recovery_wait"}
        )
        duration = invocation_execution_seconds(invocation, allow_open=allow_open)
        if duration is None:
            return None
        total += duration
    return total

def _record_configurations(invocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configurations: list[dict[str, Any]] = []
    for invocation in invocations:
        configuration = {
            "started_at": invocation.get("started_at"),
            "model": invocation.get("model"),
            "reasoning_effort": invocation.get("reasoning_effort"),
        }
        if configurations and (
            configurations[-1].get("model") == configuration.get("model")
            and configurations[-1].get("reasoning_effort")
            == configuration.get("reasoning_effort")
        ):
            continue
        configurations.append(configuration)
    return configurations

def history_record_status_code(record: dict[str, Any]) -> str:
    """Stable semantic result shared by History and notifications.

    This read-only projection uses the already associated attempt evidence;
    display wording is never an input to result classification.
    """
    invocations = record.get("invocations")
    if isinstance(invocations, list) and invocations:
        latest = invocations[-1]
        if isinstance(latest, dict) and latest.get("status") in {
            "failed",
            "execution_failed",
        }:
            return "execution_failed"
    attempt = record.get("attempt")
    if isinstance(record.get("findings"), list) and record["findings"]:
        return "review_failed"
    artifact = record.get("acceptance_artifact")
    if isinstance(artifact, dict):
        outcome = artifact_outcome(artifact)
        if outcome == "pass":
            return "review_passed"
        if outcome == "blocked":
            return "review_blocked"
    if isinstance(attempt, dict) and attempt.get("status") == "pending":
        if record.get("activity") == "running":
            return "running"
        if record.get("activity") in {"interrupted", "unknown"}:
            return str(record["activity"])
        return "pending"
    attempt_outcome: object = (
        attempt.get("outcome") if isinstance(attempt, dict) else None
    )
    outcome = str(attempt_outcome)
    return outcome if outcome in {
        "candidate", "publication_artifact", "acceptance_artifact",
        "currentness_invalidated", "no_code_changes", "git_integrity_repair",
    } else "completed"

def _history_record_details(
    state: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any]:
    attempt = record.get("attempt")
    attempt_view = dict(attempt) if isinstance(attempt, dict) else {}
    for key in (
        "work_subject",
        "role",
        "generation",
        "ordinal",
        "budget_window",
    ):
        if attempt_view.get(key) is None and record.get(key) is not None:
            attempt_view[key] = record[key]
    invocations = record.get("invocations")
    invocation_values = [
        item
        for item in (invocations if isinstance(invocations, list) else [])
        if isinstance(item, dict)
    ]
    record["activity"] = _record_activity(state, record, invocation_values)
    owners = owners_for_attempt(state, attempt_view)
    artifact = review_artifact_for_attempt(owners, attempt_view, invocation_values)
    if artifact is None and isinstance(attempt_view.get("acceptance_artifact"), dict):
        artifact = attempt_view["acceptance_artifact"]
    findings = _artifact_findings(artifact) if isinstance(artifact, dict) else []
    return {
        "findings": findings,
        "acceptance_artifact": artifact,
        "budget_facts": _budget_facts_for_attempt(state, attempt_view),
        "development_summary": _development_summary_for_attempt(
            attempt_view, owners
        ),
        "publication": _publication_for_attempt(attempt_view, owners),
        "supporting_records": _supporting_records(
            owners, attempt_view, invocation_values
        ),
    }

def _record_activity(
    state: dict[str, Any],
    record: dict[str, Any],
    invocations: list[dict[str, Any]],
) -> str:
    if not invocations:
        return "not_running"
    latest = invocations[-1]
    active = state.get("active_agent_invocation")
    if isinstance(active, dict) and (
        latest is active
        or (
            latest.get("started_at") == active.get("started_at")
            and latest.get("work_subject") == active.get("work_subject")
        )
    ):
        audit = {"executor_control": state.get("_executor_control")}
        return invocation_activity(active, audit)
    return "not_running"

def _is_history_turning_point(event: dict[str, Any]) -> bool:
    details = event.get("details")
    if isinstance(details, list) and details and _is_human_blocker_event(event):
        return True
    kind = str(event.get("kind") or "")
    status = str(event.get("status") or "")
    return kind in {
        "human_blocker",
        "supervision",
        "required_checks",
        "integration",
        "completion",
        "publication",
        "pr_creation",
        "approval",
        "resume",
        "unsupported_scope_change",
    } or status in {
        "completed",
        "abandoned",
        "abandonment_pending",
        "deterministic_contradiction",
        "parent_approval_pending",
        "parent_closeout_pending",
        "progress_exhausted",
        "requeue_required",
        "operator_stopped",
        "ready_for_human",
        "execution_failed",
        "supervision_timeout",
        "ticket_completed",
        "waiting_external",
        "waiting_checks",
        "waiting_merge",
    }

_HUMAN_BLOCKER_STATUSES = {"ready_for_human"}

def _is_human_blocker_event(event: dict[str, Any]) -> bool:
    return str(event.get("kind") or "") == "human_blocker" or str(
        event.get("status") or ""
    ) in _HUMAN_BLOCKER_STATUSES

def human_responses_by_subject(
    state: dict[str, Any],
) -> dict[tuple[str, object], list[str]]:
    responses: dict[tuple[str, object], list[str]] = {}
    subjects: list[tuple[str, dict[str, Any]]] = []
    run_id = state.get("run_id")
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        subjects.extend(
            (f"ticket:{number}", job)
            for number, job in jobs.items()
            if isinstance(job, dict)
        )
    elif isinstance(state.get("active_ticket_job"), dict):
        active = state["active_ticket_job"]
        subjects.append((f"ticket:{active.get('ticket_number')}", active))
    for name, locator in (
        ("parent_job", f"parent-only:{run_id}"),
        ("run_acceptance", f"run-acceptance:{run_id}"),
        ("run_publication", f"run-publication:{run_id}"),
    ):
        subject = state.get(name)
        if isinstance(subject, dict):
            subjects.append((locator, subject))
            repair = subject.get("repair_job")
            if isinstance(repair, dict):
                subjects.append((f"run-repair:{run_id}", repair))
    for locator, subject in subjects:
        history = subject.get("human_response_history")
        if not isinstance(history, list):
            continue
        for entry in history:
            response = entry.get("response") if isinstance(entry, dict) else None
            if isinstance(response, str):
                generation = entry.get("generation") if isinstance(entry, dict) else None
                responses.setdefault((locator, generation), []).append(response)
    return responses

def timeline_object(state: dict[str, Any], event: dict[str, Any]) -> str:
    if isinstance(event.get("ticket"), int):
        return delivery_object_label(
            state, f"ticket:{event['ticket']}", ticket_number=event["ticket"]
        )
    kind = str(event.get("kind") or "")
    if kind == "parent_phase":
        return delivery_object_label(state, "parent")
    if kind == "run_acceptance":
        return delivery_object_label(state, "run_acceptance")
    if kind == "run_publication":
        return delivery_object_label(state, "run_publication")
    return delivery_object_label(state, "run")

def _human_timeline_kind(event: dict[str, Any], details: list[str]) -> str:
    """Human lifecycle nodes are independent of the unchanged machine projection."""

    if _is_human_blocker_event(event):
        return "human_blocker"
    status, phase = event.get("status"), event.get("phase")
    if status == "abandonment_pending":
        return "abandonment"
    if status in {"operator_stopped", "execution_failed", "requeue_required"}:
        return "supervision"
    if status in {"completed", "abandoned"}:
        return "completion"
    if phase in {"merged", "completed"} and event.get("pr_number") is not None:
        return "integration"
    if status in {"run_approval_pending", "parent_approval_pending"} or (
        phase == "ready_for_approval" and status not in {"waiting_external", "supervision_timeout"}
    ):
        return "approval"
    return timeline_kind(event, details)

def timeline_kind(event: dict[str, Any], details: list[str]) -> str:
    if _is_human_blocker_event(event):
        return "human_blocker"
    status = str(event.get("status") or "")
    phase = str(event.get("phase") or "")
    kind = str(event.get("kind") or "delivery_state")
    if status in {"supervision_timeout", "waiting_external"}:
        return "supervision"
    if kind in {"publication", "required_checks", "integration", "completion"}:
        return kind
    if status in {"completed", "abandoned"}:
        return "completion"
    if status == "waiting_checks" or phase == "waiting_checks":
        return "required_checks"
    if phase in {"merged", "completed"} and event.get("pr_number") is not None:
        return "integration"
    if event.get("required_checks_result") is not None:
        return "required_checks"
    if kind == "run_publication" or phase in {
        "publishing",
        "publication_pending",
        "ready_for_approval",
    }:
        return "publication"
    return kind

def scope_change_details(event: dict[str, Any]) -> list[str]:
    if event.get("kind") != "unsupported_scope_change":
        return []
    summary = event.get("graph_change_summary")
    if not isinstance(summary, dict):
        return []
    details = []
    headline = summary.get("summary")
    if isinstance(headline, str):
        details.append(headline)
    for key, values in scope_ticket_changes(event).items():
        label = {"added_tickets": "新增 Ticket", "removed_tickets": "移除 Ticket"}[key]
        details.append(f"{label} {values}")
    return details

def scope_ticket_changes(event: dict[str, Any]) -> dict[str, list[Any]]:
    """Separate generated Ticket changes from the untouched external summary."""
    summary = event.get("graph_change_summary")
    if event.get("kind") != "unsupported_scope_change" or not isinstance(summary, dict):
        return {}
    return {
        key: values for key in ("added_tickets", "removed_tickets")
        if isinstance((values := summary.get(key)), list) and values
    }


def subject_label(state: dict[str, Any], subject: str) -> str:
    return delivery_object_label(state, subject)

def utc_timestamp(value: str) -> str | None:
    try:
        return _parse_timestamp(value).astimezone(UTC).isoformat()
    except ValueError:
        return None
