"""Normalize and validate related work before a sparse SQLite commit."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from agent_run.sqlite_encoding import copy_json_tree
from agent_run import sqlite_timeline
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state import _append_timeline_event, _sanitize_durable_errors
from agent_run.state_contract import require_current_work_state
from agent_run.ticket_state import capture_ticket_wait


def save_work(
    repository: SQLiteStateRepository, run_id: str, state: dict[str, Any],
    baseline: dict[str, Any], ticket_number: int | None,
) -> dict[str, Any]:
    """Preserve untouched objects, including history absent from this view."""
    invocation = state.get("active_agent_invocation")
    job = state.get("active_ticket_job")
    if (isinstance(job, dict) and isinstance(invocation, dict)
            and invocation.get("work_subject") == f"ticket:{job['ticket_number']}"):
        state["ticket_agent_invocations"][str(job["ticket_number"])] = deepcopy(invocation)
    capture_ticket_wait(state)
    from agent_run.ticket_work import reconcile

    reconcile(state)
    durable = dict(state)
    for key, value in state.items():
        if value == baseline.get(key) and key in baseline:
            continue
        if key in {"ticket_jobs", "ticket_agent_invocations"}:
            previous = baseline.get(key, {})
            durable[key] = {
                identity: (_sanitize_durable_errors({"value": item})["value"]
                           if item != previous.get(identity) else item)
                for identity, item in value.items()
            }
        else:
            durable[key] = _sanitize_durable_errors({key: value})[key]
    # Timeline uses the actual committing Ticket, irrespective of selection.
    # Appending/trimming changes the list, never an existing event. Detach the
    # list without copying every old event before adding the new marker.
    for key in ("timeline", "timeline_continuation"):
        if isinstance(durable.get(key), list):
            durable[key] = durable[key].copy()
    before, after = baseline, durable
    if ticket_number is not None:
        before = _ticket_view(baseline, ticket_number)
        after = _ticket_view(durable, ticket_number)
    timelines = repository.work_timelines()
    timeline_updates = (sqlite_timeline.append_events(after, before, timelines)
                        if timelines else {})
    if not timelines:
        _append_timeline_event(after, before)
    for key in ("timeline", "timeline_continuation", "timeline_at_capacity"):
        if key in after:
            durable[key] = after[key]
            state[key] = copy_json_tree(after[key])
    require_current_work_state(
        durable, repository.work_archives(durable), repository.work_attempts(durable), timelines,
    )
    repository.save_work(run_id, durable, baseline, timeline_updates=timeline_updates)
    return durable


def _ticket_view(state: dict[str, Any], number: int) -> dict[str, Any]:
    view = dict(state)
    job = state["ticket_jobs"].get(str(number))
    if not isinstance(job, dict):
        return view
    view["active_ticket_job"] = job
    view["active_agent_invocation"] = state["ticket_agent_invocations"].get(str(number))
    wait = job.get("external_wait")
    if isinstance(wait, dict):
        view.update(wait)
    elif job.get("phase") == "blocked":
        view["status"] = (
            "ready_for_human" if job.get("blocked_reason") in {
                "agent_requires_human", "reviewer_requires_human",
            } else "blocked"
        )
    elif not isinstance(state.get("active_ticket_job"), dict) or state["active_ticket_job"].get("ticket_number") != number:
        view["status"] = "active"
    return view
