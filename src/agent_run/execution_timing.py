"""Reliable execution aggregates from semantic history, excluding waiting gaps."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


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

