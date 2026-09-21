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


def invocation_links_complete(
    state: dict[str, Any], records: list[dict[str, Any]],
) -> bool:
    """Reject totals when Resume proves a retained Attempt lost a segment."""
    starts = {
        record.get("attempt_id"): {invocation.get("started_at")
                                   for invocation in record.get("invocations", [])}
        for record in records
    }
    for resume in (state.get("resume_audit") or {}).get("history", []):
        identity = resume.get("semantic_attempt_id")
        if identity not in starts:
            continue
        for endpoint in ("source_invocation_started_at", "successor_invocation_started_at"):
            if resume.get(endpoint) is not None and resume[endpoint] not in starts[identity]:
                return False
    return True


def execution_totals(
    state: dict[str, Any], records: list[dict[str, Any]], at: object,
    *, work_subject: str | None = None,
) -> dict[str, Any]:
    end, start = _time(at), _time(state.get("created_at"))
    facts: dict[str, Any] = {}
    if end and start and end >= start:
        facts["elapsed_seconds"] = int((end - start).total_seconds())
    rounds = [record for record in records if not record.get("event_record")]
    raw = state.get("agent_invocation_history") or []
    if work_subject is not None:
        rounds = [record for record in rounds if record.get("work_subject") == work_subject]
        raw = [invocation for invocation in raw
               if (invocation.get("work_subject") or (invocation.get("semantic_attempt") or {}).get("work_subject")) == work_subject]
    known = [invocation for record in rounds for invocation in record.get("invocations", [])]
    # An invocation with no semantic association is still executed work. Its
    # omission from history projection makes an aggregate incomplete.
    if any(invocation not in known for invocation in raw):
        return facts
    if not invocation_links_complete(state, rounds):
        return facts
    complete = rounds and all(
        _time(record.get("ended_at")) is not None
        and type(record.get("execution_seconds")) is int
        for record in rounds
    )
    if not state.get("timeline_at_capacity") and complete:
        if end and any((ended := _time(record.get("ended_at"))) is not None and ended > end for record in rounds):
            return facts
        facts["total_seconds"] = sum(record["execution_seconds"] for record in rounds)
    return facts


def invocation_execution_seconds(
    invocation: dict[str, Any], *, allow_open: bool = False
) -> int | None:
    """Return trusted active execution time, excluding persisted recovery waits."""

    started = parse_optional_timestamp(invocation.get("started_at"))
    ended = parse_optional_timestamp(invocation.get("ended_at"))
    if started is None:
        return None
    if ended is None:
        if not allow_open:
            return None
        ended = datetime.now(UTC)
    if ended < started:
        return None

    waits = _recovery_wait_intervals(invocation, started=started, ended=ended)
    if waits is None:
        return None
    wait_seconds = 0.0
    previous_end = started
    for wait_start, wait_end in sorted(waits):
        if wait_start < started or wait_end > ended or wait_start < previous_end:
            return None
        wait_seconds += (wait_end - wait_start).total_seconds()
        previous_end = wait_end
    return max(0, int((ended - started).total_seconds() - wait_seconds))



def _recovery_wait_intervals(
    invocation: dict[str, Any], *, started: datetime, ended: datetime
) -> list[tuple[datetime, datetime]] | None:
    raw_intervals = invocation.get("recovery_wait_intervals")
    if isinstance(raw_intervals, list) and not raw_intervals:
        # An explicitly persisted empty list belongs to this Invocation. In
        # particular, a successor may retain the semantic Attempt's cumulative
        # recovery count while having no waits of its own.
        if not invocation.get("recovery_waiting") and not any(
            invocation.get(key)
            for key in ("last_failure_at", "last_recovery_started_at")
        ):
            return []
        # A legacy/current waiting record can have an empty list because its
        # interval was not persisted. Fall through to the conservative legacy
        # reconstruction below rather than claiming all elapsed time ran.
        raw_intervals = None
    if raw_intervals is None:
        # Older records only have enough evidence for one capacity wait. More
        # than one unversioned wait cannot be reconstructed safely.
        count = invocation.get("capacity_recovery_count")
        if count in (None, 0) and not any(
            invocation.get(key)
            for key in ("last_failure_at", "last_recovery_started_at")
        ):
            return []
        if count not in (None, 0, 1):
            return None
        raw_intervals = [
            {
                "started_at": invocation.get("last_failure_at"),
                "ended_at": invocation.get("last_recovery_started_at"),
            }
        ]
    if not isinstance(raw_intervals, list):
        return None

    intervals: list[tuple[datetime, datetime]] = []
    for raw in raw_intervals:
        if not isinstance(raw, dict):
            return None
        wait_start = parse_optional_timestamp(raw.get("started_at"))
        wait_end = parse_optional_timestamp(raw.get("ended_at"))
        if wait_start is None:
            return None
        if wait_end is None:
            if not invocation.get("recovery_waiting"):
                return None
            wait_end = ended
        if wait_end < wait_start:
            return None
        intervals.append((wait_start, wait_end))
    return intervals



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
