"""Assemble work dictionaries from their single relational authorities.

Run identity, scheduling, fixed inputs and global conditions join the same
relations as Jobs, Attempts and Invocations. Views
join the same SQLite read snapshot, and writes join the existing authorized
transaction; this module grants no execution or settlement permission.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_run import sqlite_change_jobs as jobs
from agent_run import sqlite_work_execution as execution
from agent_run import sqlite_work_evidence as evidence
from agent_run import sqlite_run_records as roots
from agent_run.sqlite_change_job_queries import _ARCHIVES, _HISTORIES, _projection_owners
from agent_run.sqlite_encoding import object_path
from agent_run.sqlite_query_rows import bounded_query
from agent_run.work_projection import JobHistoryAppend, JobProjection, RepairBudgetProjection


INVOCATION_FIELDS = frozenset(
    {
        "active_agent_invocation",
        "ticket_agent_invocations",
        "agent_invocation_history",
    }
)
FIELDS = frozenset(jobs.FIELDS) | INVOCATION_FIELDS | roots.FIELDS
CURRENT_FIELDS = FIELDS - frozenset(_ARCHIVES)
KEYED_FIELDS = frozenset(
    {"ticket_jobs", "ticket_agent_invocations", "retired_ticket_generations", "pending_ticket_retirements"}
)
TABLES = {**evidence.TABLES, **jobs.TABLES, **execution.TABLES, **roots.TABLES}
INDEXES = {**evidence.INDEXES, **jobs.INDEXES, **execution.INDEXES, **roots.INDEXES}


@dataclass
class WorkBaseline:
    run_id: str
    baseline: dict[str, Any]
    total_changes: int
    # Only object reads declare coverage. Other work/Timeline baselines keep
    # their existing purpose and cannot stand in for an object save query.
    fields: frozenset[str] = frozenset()
    partial_fields: frozenset[str] = frozenset()
    connection: sqlite3.Connection | None = None


def read_baseline_for_save(
    connection: sqlite3.Connection,
    run_id: str,
    state: Mapping[str, Any],
    fields: set[str],
    keyed_changes: Mapping[str, set[str]] | None,
    archive_scope: Mapping[str, tuple[int, int]] | None,
    view: WorkBaseline | None,
) -> dict[str, Any] | None:
    """Reuse facts only when this write snapshot covers the original query."""
    needed = fields | (set(INVOCATION_FIELDS) if fields & INVOCATION_FIELDS else set())
    if (not isinstance(view, WorkBaseline) or view.connection is not connection or view.run_id != run_id
            or view.total_changes != connection.total_changes or not needed <= view.fields):
        return None
    numbers = {
        number for field in ("ticket_jobs", "ticket_agent_invocations")
        for number in (keyed_changes or {}).get(field, set())
    }
    result = {field: view.baseline[field] for field in needed if field in view.baseline}
    for field in needed & KEYED_FIELDS:
        selected = (
            numbers or None if field in {"ticket_jobs", "ticket_agent_invocations"}
            else (keyed_changes or {}).get(field)
        )
        values = result.get(field, {})
        if not isinstance(values, Mapping):
            return None
        if field in view.partial_fields and (selected is None or not selected <= values.keys()):
            return None
        if selected is not None and field in result:
            result[field] = {key: value for key, value in values.items() if key in selected}
    if fields & INVOCATION_FIELDS:
        if not _resume_ids(state) <= _resume_ids(view.baseline):
            return None
        from agent_run.sqlite_work_execution_invocations import invocation_identity

        current = [result.get("active_agent_invocation"), *result.get("ticket_agent_invocations", {}).values()]
        identities = {invocation_identity(item) for item in current if isinstance(item, Mapping)}
        if "agent_invocation_history" in result:
            resumes = _resume_ids(state)
            result["agent_invocation_history"] = [
                item for item in result["agent_invocation_history"]
                if invocation_identity(item) in identities or item.get("resume_id") in resumes
            ]
    if fields & jobs.FIELDS:
        if archive_scope is None:
            return None
        expected = jobs.projection_archives(result)
        actual = {}
        for key, value in archive_scope.items():
            path = object_path(key)
            if path[0] in fields and (path[0] != "ticket_jobs" or not numbers or path[1] in numbers):
                actual[key] = value
        if expected != actual:
            return None
        proposed = {field: state[field] for field in fields & jobs.FIELDS if field in state}
        if numbers and "ticket_jobs" in proposed:
            if not isinstance(proposed["ticket_jobs"], Mapping):
                return None
            proposed["ticket_jobs"] = {key: value for key, value in proposed["ticket_jobs"].items() if key in numbers}
        if jobs.projection_repair_budget_scopes(result) != jobs.projection_repair_budget_scopes(proposed):
            return None
        previous = {path: job for path, job, _ in _projection_owners(result)}
        for path, job, _ in _projection_owners(proposed):
            before = previous.get(path)
            if any(not isinstance(job[field], JobHistoryAppend)
                   for field in (_HISTORIES & job.keys()) - (before or {}).keys()):
                return None
            budgets = (before or {}).get("repair_integration_budgets")
            if (isinstance(budgets, RepairBudgetProjection)
                    and not job.get("repair_integration_budgets", {}).keys() <= budgets.keys()):
                return None
            if isinstance(job, JobProjection) and job.publication_update is not None:
                from agent_run.sqlite_mutations import _same

                if (not isinstance(before, JobProjection) or before.publication_update is None
                        or not _same(before.publication_update[0], job.publication_update[0])):
                    return None
    return result


def without_work(state: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key not in FIELDS}


def _base(base: dict[str, Any]) -> dict[str, Any]:
    if base.keys() & FIELDS:
        raise ValueError("Relational Run facts cannot have legacy object nodes")
    return dict(base)


def _resume_ids(state: Mapping[str, Any]) -> set[str]:
    return {
        item["resume_id"]
        for item in state.get("resume_audit", {}).get("history", [])
        if isinstance(item, dict) and isinstance(item.get("resume_id"), str)
    }


def _related_tickets(state: Mapping[str, Any], requested: set[int] | None) -> set[int]:
    numbers = set(requested or ())
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and type(active.get("ticket_number")) is int:
        numbers.add(active["ticket_number"])
    holder = state.get("ticket_integration_holder")
    if type(holder) is int:
        numbers.add(holder)
    for field in ("ticket_work_queue", "ticket_integration_queue"):
        numbers.update(
            item["ticket_number"]
            for item in state.get(field, [])
            if isinstance(item, dict) and type(item.get("ticket_number")) is int
        )
    summary = state.get("unsupported_scope_change", {})
    if isinstance(summary, dict):
        summary = summary.get("graph_change_summary", {})
        if isinstance(summary, dict):
            numbers.update(number for number in summary.get("reopened_completed_tickets", []) if type(number) is int)
    return numbers


def load_complete(
    connection: sqlite3.Connection,
    run_id: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    state = _base(base)
    state.update(roots.load(connection, run_id))
    state.update(jobs.load_jobs(connection, run_id, include_history=True))
    invocations, _ = execution.load_invocations(
        connection,
        run_id,
        current_only=False,
        history=True,
    )
    state.update(invocations)
    return state


def load_objects(
    connection: sqlite3.Connection,
    run_id: str,
    base: dict[str, Any],
    *,
    excluded_fields: set[str],
    related_tickets: bool,
    ticket_numbers: set[int] | None,
    scheduler_tickets: bool,
    max_bytes: int,
) -> tuple[dict[str, Any], dict[str, int]]:
    state = _base(base)
    fields = set(FIELDS) - excluded_fields
    maps = {"retired_ticket_generations", "pending_ticket_retirements"}
    scoped = related_tickets or ticket_numbers is not None or scheduler_tickets
    state.update(roots.load(connection, run_id, fields=(fields - maps if scoped else fields) & roots.FIELDS))
    if related_tickets or ticket_numbers is not None or scheduler_tickets:
        fields.update({"ticket_jobs", "ticket_agent_invocations"})
    selected_number = jobs.selected_ticket_number(connection, run_id) if "active_ticket_job" in fields else None
    selection = {"active_ticket_job": {"ticket_number": selected_number}} if selected_number is not None else {}
    numbers = (
        _related_tickets({**state, **selection}, ticket_numbers)
        if related_tickets or ticket_numbers is not None
        else None
    )
    if numbers is not None:
        for (key,) in bounded_query(connection, "SELECT r.reference_key FROM work_invocation_current r "
            "JOIN work_invocations v USING(run_id,work_subject,started_at) "
            "WHERE r.run_id=? AND r.reference_kind='ticket' AND v.status IN ('running','resuming')",
            (run_id,),
        ):
            numbers.add(int(key))
    selected = jobs.load_jobs(
        connection,
        run_id,
        fields=fields & jobs.FIELDS,
        ticket_numbers=set() if scheduler_tickets else numbers,
        include_history=False,
        current_only=True,
        filter_tickets=not scheduler_tickets,
    )
    state.update(selected)
    if scoped:
        state.update(
            roots.load(
                connection,
                run_id,
                fields=maps,
                keyed_changes={
                    field: {str(number) for number in numbers or ()}
                    for field in maps
                    if not scheduler_tickets or field != "retired_ticket_generations"
                },
            )
        )
    invocations, positions = execution.load_invocations(
        connection,
        run_id,
        ticket_numbers=None if scheduler_tickets else numbers,
        current_only=True,
        resume_ids=_resume_ids(state),
        history=True,
    )
    state.update((key, value) for key, value in invocations.items() if key in fields)
    return state, positions


def load_work(
    connection: sqlite3.Connection,
    run_id: str,
    base: dict[str, Any],
    ticket_number: int | None,
) -> tuple[dict[str, Any], dict[str, tuple[int, int]], dict[str, dict[str, Any]]]:
    state = _base(base)
    state.update(roots.load(connection, run_id))
    targets = {ticket_number} if ticket_number is not None else set()
    state.update(jobs.load_jobs(connection, run_id, ticket_numbers=targets, include_history=False, current_only=True))
    current, _ = execution.load_invocations(
        connection,
        run_id,
        current_only=True,
        resume_ids=_resume_ids(state),
        history=True,
        history_ticket_numbers=targets if ticket_number is not None else None,
    )
    state.update(current)
    archives = jobs.read_job_archives(connection, run_id, state)
    related = jobs.read_related_attempts(connection, run_id, state, archives)
    return state, archives, related


def load_fields(
    connection: sqlite3.Connection,
    run_id: str,
    base: dict[str, Any],
    fields: set[str],
    *,
    ticket_numbers: set[int] | None = None,
    root_keys: Mapping[str, set[str]] | None = None,
) -> dict[str, Any]:
    state = _base(base)
    state.update(roots.load(connection, run_id, fields=fields & roots.FIELDS, keyed_changes=root_keys))
    state.update(
        jobs.load_jobs(
            connection,
            run_id,
            fields=fields & jobs.FIELDS,
            ticket_numbers=ticket_numbers,
            include_history=False,
            current_only=True,
            filter_tickets=True,
        )
    )
    if fields & INVOCATION_FIELDS:
        current, _ = execution.load_invocations(
            connection,
            run_id,
            ticket_numbers=ticket_numbers,
            current_only="agent_invocation_history" not in fields,
            history="agent_invocation_history" in fields,
        )
        state.update((key, value) for key, value in current.items() if key in fields)
    return state


def save(
    connection: sqlite3.Connection,
    run_id: str,
    state: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    fields: set[str],
    keyed_changes: Mapping[str, set[str]] | None = None,
) -> None:
    # Inputs precede Job bindings; closeout/requeue references follow their
    # source Job changes in this same transaction. Remove a retired reference
    # first when the transaction explicitly completes that transition.
    deferred = fields & {"requeue_transition", "run_abandonment", "delivery_cleanup"}
    removed = {field for field in deferred if state.get(field) is None}
    roots.save(
        connection,
        run_id,
        state,
        baseline,
        fields=(fields & roots.FIELDS) - deferred | removed,
        keyed_changes=keyed_changes,
    )
    if fields & jobs.FIELDS:
        jobs.save_jobs(connection, run_id, state, baseline, fields=fields & jobs.FIELDS, keyed_changes=keyed_changes)
    if deferred - removed:
        roots.save(connection, run_id, state, baseline, fields=deferred - removed)
    if fields & INVOCATION_FIELDS:
        proposed = dict(state)
        for field in INVOCATION_FIELDS - fields:
            if field in baseline:
                proposed[field] = baseline[field]
            else:
                proposed.pop(field, None)
        execution.save_invocations(connection, run_id, proposed, baseline)
    elif fields & jobs.FIELDS:
        # A backend completes its Invocation before independently saving the
        # return. Bind that return in this transaction without requiring an
        # Invocation delta or its dictionary in this owned Job projection.
        execution.bind_returns(connection, run_id, state)
        # Consuming an Attempt can release its old Invocation details even
        # when this commit has no new Invocation facts of its own.
        from agent_run.sqlite_work_execution_retention import compact_details

        compact_details(connection, run_id)
    if bounded_query(connection, "SELECT 1 FROM run_requeue_transition WHERE run_id=?", (run_id,)).fetchone():
        roots.load_requeue(connection, run_id)


def project_saved_gates(
    connection: sqlite3.Connection,
    run_id: str,
    state: dict[str, Any],
    *,
    fields: set[str],
    keyed_changes: Mapping[str, set[str]] | None = None,
) -> tuple[bool, set[str]]:
    """Read committed owner gates, then mirror only the selected owner."""
    from agent_run.sqlite_change_job_gates import reconcile_selected_gate

    owners, revealed = jobs.project_current_gates(
        connection, run_id, state, fields=fields, keyed_changes=keyed_changes
    )
    return bool(owners), reconcile_selected_gate(state, revealed)


def baseline_for_save(
    connection: sqlite3.Connection,
    run_id: str,
    state: Mapping[str, Any],
    fields: set[str],
    keyed_changes: Mapping[str, set[str]] | None,
    archive_scope: Mapping[str, tuple[int, int]] | None = None,
) -> dict[str, Any]:
    numbers = {
        int(number)
        for field in ("ticket_jobs", "ticket_agent_invocations")
        for number in (keyed_changes or {}).get(field, set())
    }
    baseline = roots.load(connection, run_id, fields=fields & roots.FIELDS, keyed_changes=keyed_changes)
    baseline.update(
        jobs.load_jobs(
            connection,
            run_id,
            fields=fields & jobs.FIELDS,
            ticket_numbers=numbers if numbers else None,
            include_history=True,
            archive_scope=archive_scope,
            repair_budget_scopes=jobs.projection_repair_budget_scopes(state),
        )
    )
    if archive_scope is None:
        # A direct owned patch carries no proof that omitted history was read.
        # Only an explicit projection scope may describe a loaded deletion.
        from agent_run.sqlite_job_history import FIELDS as ARCHIVES

        def preserve_omitted(previous: Any, proposed: Any) -> None:
            if not isinstance(previous, dict) or not isinstance(proposed, Mapping):
                return
            for field in ARCHIVES - proposed.keys():
                previous.pop(field, None)
            preserve_omitted(previous.get("repair_job"), proposed.get("repair_job"))

        for field in fields & jobs.FIELDS:
            if field == "ticket_jobs":
                for number, proposed in state.get(field, {}).items():
                    preserve_omitted(baseline.get(field, {}).get(number), proposed)
            else:
                preserve_omitted(baseline.get(field), state.get(field))
    if fields & INVOCATION_FIELDS:
        # Only the caller's referenced Invocation identities participate in an
        # object commit. The writer checks every changed row against SQLite.
        invocations, _ = execution.load_invocations(
            connection,
            run_id,
            ticket_numbers=numbers if numbers else None,
            current_only=True,
            resume_ids=_resume_ids(state),
            history=True,
        )
        baseline.update(invocations)
    return baseline


def append_archive(connection: sqlite3.Connection, run_id: str, field: str, items: list[Any]) -> None:
    jobs.append_archive(connection, run_id, field, items)


def object_scope(
    connection: sqlite3.Connection,
    run_id: str,
    state: Mapping[str, Any],
) -> tuple[dict[str, tuple[int, int]], dict[str, dict[str, Any]]]:
    archives = jobs.read_job_archives(connection, run_id, state)
    return archives, jobs.read_related_attempts(connection, run_id, state, archives)
