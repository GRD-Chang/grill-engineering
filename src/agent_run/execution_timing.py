"""Reliable execution aggregates from semantic history, excluding waiting gaps."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def invocation_started(invocation: dict[str, Any]) -> bool:
    """A reported Thread proves execution; preparing an Invocation does not."""
    thread_id = invocation.get("reported_thread_id")
    return (
        isinstance(thread_id, str) and bool(thread_id.strip())
        and parse_optional_timestamp(invocation.get("started_at")) is not None
    )


def invocation_links_complete(
    state: dict[str, Any], records: list[dict[str, Any]],
) -> bool:
    """Require all known same-Attempt predecessor and Resume endpoints."""
    starts = {
        record.get("attempt_id"): {invocation.get("started_at")
                                   for invocation in record.get("invocations", [])}
        for record in records
    }
    for record in records:
        identity = record.get("attempt_id")
        if not isinstance(identity, str) or not identity:
            return False
        for invocation in record.get("invocations", []):
            predecessor = invocation.get("predecessor_invocation_started_at")
            if predecessor is not None and predecessor not in starts[identity]:
                return False
    active = [state.get("active_agent_invocation")]
    tickets = state.get("ticket_agent_invocations")
    if isinstance(tickets, dict):
        active.extend(tickets.values())
    for invocation in active:
        if not isinstance(invocation, dict):
            continue
        attempt = invocation.get("semantic_attempt")
        identity = attempt.get("attempt_id") if isinstance(attempt, dict) else None
        if identity in starts and invocation.get("started_at") not in starts[identity]:
            return False
    for resume in (state.get("resume_audit") or {}).get("history", []):
        identity = resume.get("semantic_attempt_id")
        if identity not in starts:
            continue
        for endpoint in ("source_invocation_started_at", "successor_invocation_started_at"):
            if resume.get(endpoint) is not None and resume[endpoint] not in starts[identity]:
                return False
    return True


def aggregate_invocation_timing(
    invocations: list[dict[str, Any]], *, complete: bool = True,
) -> dict[str, Any]:
    """Sum proven segments while keeping missing evidence explicit."""
    timings = [_invocation_timing(invocation) for invocation in invocations]
    known = [timing["recorded_execution_seconds"] for timing in timings
             if timing["recorded_execution_seconds"] is not None]
    complete = bool(invocations) and complete and all(timing["execution_timing_complete"] for timing in timings)
    recorded = sum(known) if known else None
    return {
        "execution_seconds": recorded if complete else None,
        "execution_timing_complete": complete,
        "recorded_execution_seconds": recorded,
    }


def record_execution_timing(
    state: dict[str, Any], record: dict[str, Any],
) -> dict[str, Any]:
    """Project closed evidence and optional live time without filling gaps."""
    invocations = record.get("invocations") or []
    linked = invocation_links_complete(state, [record])
    facts = aggregate_invocation_timing(invocations, complete=linked)
    # A trusted live last segment remains useful in status/history. It never
    # proves a completed total, nor repairs any earlier missing segment.
    if (
        not facts["execution_timing_complete"] and invocations and linked
        and record.get("activity") in {"running", "capacity_wait", "recovery_wait"}
    ):
        previous = [invocation_execution_seconds(invocation) for invocation in invocations[:-1]]
        live = invocation_execution_seconds(invocations[-1], allow_open=True)
        if all(value is not None for value in previous) and live is not None:
            facts["execution_seconds"] = sum(value for value in previous if value is not None) + live
    return facts


def execution_totals(
    state: dict[str, Any], records: list[dict[str, Any]], at: object,
    *, work_subject: str | None = None,
) -> dict[str, Any]:
    end, start = _time(at), _time(state.get("created_at"))
    facts: dict[str, Any] = {"execution_timing_complete": False}
    if end and start and end >= start:
        facts["elapsed_seconds"] = int((end - start).total_seconds())
    rounds = [record for record in records if not record.get("event_record")]
    raw = state.get("agent_invocation_history") or []
    if work_subject is not None:
        rounds = [record for record in rounds if record.get("work_subject") == work_subject]
        raw = [invocation for invocation in raw
               if (invocation.get("work_subject") or (invocation.get("semantic_attempt") or {}).get("work_subject")) == work_subject]
    known = [invocation for record in rounds for invocation in record.get("invocations", [])]
    missing = [invocation for invocation in raw if invocation not in known]
    # Unassociated work prevents a complete total, but its closed segment is
    # still valid partial evidence. Parallel subjects are summed independently.
    included = [
        invocation for invocation in [*known, *missing]
        if end is None or (ended := _time(invocation.get("ended_at"))) is None or ended <= end
    ]
    recorded = aggregate_invocation_timing(included)["recorded_execution_seconds"]
    if recorded is not None:
        facts["recorded_execution_seconds"] = recorded
    complete = bool(rounds) and not missing and all(
        record_execution_timing(state, record)["execution_timing_complete"]
        for record in rounds
    )
    if complete and end and any(
        (ended := _time(invocation.get("ended_at"))) is not None and ended > end
        for invocation in known
    ):
        complete = False
    # Lifecycle snapshots may be bounded independently of durable Attempt and
    # Invocation facts; timeline truncation alone does not lose execution time.
    facts["execution_timing_complete"] = complete
    if complete:
        facts["total_seconds"] = recorded
    return facts


def invocation_execution_seconds(
    invocation: dict[str, Any], *, allow_open: bool = False
) -> int | None:
    """Return execution time only from observed Worker boundaries."""
    seconds = _invocation_timing(invocation, allow_open=allow_open)["execution_seconds"]
    return seconds if isinstance(seconds, int) else None


def _invocation_timing(invocation: dict[str, Any], *, allow_open: bool = False) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "execution_seconds": None, "execution_timing_complete": False,
        "recorded_execution_seconds": None,
    }
    if not invocation_started(invocation):
        if invocation.get("execution_not_started") is True and invocation.get("status") == "failed":
            return {"execution_seconds": 0, "execution_timing_complete": True,
                    "recorded_execution_seconds": 0}
        return facts
    segments = invocation.get("execution_segments")
    summary = invocation.get("execution_timing_summary")
    if segments == [] and invocation.get("status") in {"completed", "failed"} and isinstance(summary, dict):
        if (set(summary) == set(facts)
                and type(summary.get("execution_timing_complete")) is bool
                and all(value is None or type(value) is int and value >= 0
                        for value in (summary.get("execution_seconds"), summary.get("recorded_execution_seconds")))
                and (summary["execution_timing_complete"] is True
                     and summary["execution_seconds"] == summary["recorded_execution_seconds"]
                     or summary["execution_timing_complete"] is False and summary["execution_seconds"] is None)):
            return dict(summary)
        return facts
    if not isinstance(segments, list) or not segments:
        # Invocation timestamps include Controller checks and setup. Legacy
        # records cannot prove Worker duration, even when both timestamps exist.
        return facts
    closed_seconds = live_seconds = 0.0
    known = False
    complete = live_complete = True
    previous_end = parse_optional_timestamp(invocation.get("started_at"))
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            complete = live_complete = False
            continue
        started = parse_optional_timestamp(segment.get("started_at"))
        ended = parse_optional_timestamp(segment.get("ended_at"))
        if started is None or (previous_end is not None and started < previous_end):
            complete = live_complete = False
            continue
        if ended is None:
            complete = False
            if (segment.get("ended_at") is None and allow_open and index == len(segments) - 1
                    and invocation.get("status") == "running"):
                current = datetime.now(UTC)
                if current >= started:
                    live_seconds += (current - started).total_seconds()
                    continue
            live_complete = False
            continue
        if ended < started:
            complete = live_complete = False
            continue
        closed_seconds += (ended - started).total_seconds()
        previous_end = ended
        known = True
    facts["execution_timing_complete"] = complete
    if known:
        facts["recorded_execution_seconds"] = int(closed_seconds)
    if complete or (allow_open and live_complete):
        facts["execution_seconds"] = int(closed_seconds + live_seconds)
    return facts


def invocation_activity(
    invocation: dict[str, Any] | None, audit: dict[str, Any]
) -> str:
    """Project execution evidence separately from persisted Invocation status."""
    if not invocation or invocation.get("status") not in {"running", "resuming"}:
        return "not_running"
    control = audit.get("executor_control")
    activity = control.get("activity") if isinstance(control, dict) else "unknown"
    if activity == "not_running":
        return "interrupted"
    if activity != "running":
        return "unknown"
    if invocation.get("recovery_waiting") is True:
        return (
            "capacity_wait"
            if invocation.get("recovery_kind") == "capacity"
            else "recovery_wait"
        )
    return "running"



def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed



def parse_optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_timestamp(value).astimezone(UTC)
    except ValueError:
        return None




def run_elapsed_seconds(
    state: dict[str, Any], *, events: list[dict[str, Any]] | None = None
) -> int | None:
    started = parse_optional_timestamp(state.get("created_at"))
    if started is None:
        return None
    if state.get("status") not in {"completed", "abandoned"}:
        return max(0, int((datetime.now(UTC) - started).total_seconds()))
    terminal_events = events if events is not None else [
        event for key in ("timeline", "timeline_continuation")
        for event in state.get(key, []) if isinstance(event, dict)
    ]
    candidates = [
        timestamp for event in terminal_events
        if event.get("status") == state.get("status")
        and (timestamp := parse_optional_timestamp(event.get("at"))) is not None
    ]
    if not candidates:
        return None
    end = max(candidates)
    return int((end - started).total_seconds()) if end >= started else None
