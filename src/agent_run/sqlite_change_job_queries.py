"""Exact current Job queries and explicit historical scope readers."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from typing import Any

from agent_run.sqlite_change_job_archives import read_metadata, require_metadata
from agent_run.sqlite_change_job_codec import history_rows, load_core, load_record, material, present
from agent_run.sqlite_change_job_gates import load_gate as _load_gate
from agent_run.sqlite_change_job_schema import HISTORY_FIELDS, JOB_FIELDS, RECORDS, ROOT_FIELDS, STRING_FIELDS, THREAD_FIELDS
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_query_rows import bounded_query
from agent_run.work_projection import JobHistoryAppend, JobProjection, RepairBudgetProjection, RepairBudgetScope

FIELDS = frozenset(ROOT_FIELDS)
_ARCHIVES = {
    "retired_job_generations": "retired_job_generation",
    "retired_semantic_attempt_owners": "retired_attempt_owner",
    "retired_parent_job": "retired_parent",
    "retired_run_acceptance": "retired_acceptance",
    "retired_run_publication": "retired_publication",
}
_OWNERS = {"parent_job": "parent", "run_acceptance": "run_acceptance", "run_publication": "run_publication"}
_HISTORIES = HISTORY_FIELDS | THREAD_FIELDS | {"review_budget_history", "semantic_attempt_history"}


def job_identity(owner_kind: str, subject: Mapping[str, Any], *, ticket_number: int | None = None) -> str:
    """Use replacement generations for change work and fixed Run containers."""
    if owner_kind == "ticket":
        number = ticket_number if ticket_number is not None else subject.get("ticket_number")
        if type(number) is not int:
            raise ValueError("Ticket Job identity requires its Ticket number")
        if "selection_reason" in subject and not subject.keys() - {
            "ticket_number",
            "selection_reason",
            "source_revision_changed",
        }:
            return f"ticket:{number}:selection"
        return f"ticket:{number}:generation:{subject.get('ticket_branch_generation', 1)}"
    prefix, field = {
        "parent": ("parent-only", "parent_generation"),
        "run_repair": ("run-repair", "repair_generation"),
        "selected_ticket": ("active-ticket", "ticket_branch_generation"),
    }.get(owner_kind, (owner_kind.replace("_", "-"), ""))
    generation = subject.get(field, 1) if field else 1
    if type(generation) is not int or generation < 1:
        raise ValueError("Change Job generation must be positive")
    return f"{prefix}:generation:{generation}"


def _load_history(c: sqlite3.Connection, run_id: str, job_id: str, field: str, *, owner_id: str) -> list[Any]:
    result, total = [], 0
    for evidence_id, thread, size in history_rows(c, run_id, job_id, field, owner_id=owner_id):
        result.append(thread if evidence_id is None else material(c, run_id, owner_id, field, evidence_id))
        total += size
    require_metadata(c, run_id, job_id, field, total, len(result))
    return result


def _completed_repairs(c: sqlite3.Connection, run_id: str, job_id: str) -> list[Any]:
    return [
        _load_owner(c, run_id, row[0])
        for row in bounded_query(c,
            "SELECT job_id FROM work_job_slots WHERE run_id=? AND container=? ORDER BY position",
            (run_id, "completed_repair_jobs:" + job_id),
        )
    ]


def _review_windows(c: sqlite3.Connection, run_id: str, job_id: str) -> list[Any]:
    snapshots, total = [], 0
    for position, (window, identity, size) in enumerate(bounded_query(c,
        "SELECT window,snapshot_job_id,logical_bytes FROM work_job_windows "
        "WHERE run_id=? AND job_id=? ORDER BY window", (run_id, job_id),
    ), 1):
        if window != position or type(size) is not int or size < 0:
            raise ValueError("Invalid Change Job budget history size or window")
        snapshots.append(_load_owner(c, run_id, identity))
        total += size
    require_metadata(c, run_id, job_id, "review_budget_history", total, len(snapshots))
    return snapshots


def _load_owner(
    c: sqlite3.Connection,
    run_id: str,
    job_id: str,
    *,
    include_history: bool = True,
    omitted_history: set[str] | None = None,
    repair_omitted_history: set[str] | None = None,
    repair_budget_scope: RepairBudgetScope | None = None,
    current_budgets_only: bool = False,
) -> dict[str, Any]:
    from agent_run.sqlite_work_execution import load_attempts
    from agent_run.sqlite_work_execution_attempts import (
        load_expected_publication_attempt, publication_attempt_expectation,
    )

    job, meta = load_core(c, run_id, job_id)
    keys, owner = meta["keys"], meta["owner_id"]
    for field in RECORDS.keys() & keys:
        job[field] = load_record(c, run_id, job_id, field, owner_id=owner)
    omitted = omitted_history or set()
    for field in ((HISTORY_FIELDS | THREAD_FIELDS) & keys) - omitted:
        if include_history:
            if field == "completed_repair_jobs":
                job[field] = _completed_repairs(c, run_id, job_id)
            else:
                job[field] = _load_history(c, run_id, job_id, field, owner_id=owner)
    for field in STRING_FIELDS & keys:
        job[field] = [
            row[0]
            for row in bounded_query(c, "SELECT text_value FROM work_job_record_strings WHERE run_id=? AND job_id=? AND record_kind='job' AND list_name=? ORDER BY position",
                (run_id, job_id, field),
            )
        ]
    job.update(
        (field, value)
        for field, value in load_attempts(
            c, run_id, job_id, include_history=include_history and "semantic_attempt_history" not in omitted
        ).items()
        if field in keys
    )
    if include_history and "review_budget_history" in keys and "review_budget_history" not in omitted:
        job["review_budget_history"] = _review_windows(c, run_id, job_id)
    if "repair_job" in keys:
        repair_id = meta["repair_job_id"]
        job["repair_job"] = (
            _load_owner(c, run_id, repair_id, include_history=include_history, omitted_history=repair_omitted_history)
            if repair_id is not None
            else None
        )
    if "repair_integration_budgets" in keys:
        from agent_run.sqlite_repair_budgets import current_generations, load_budgets

        if repair_budget_scope is not None:
            if repair_budget_scope.run_id != run_id or repair_budget_scope.job_id != job_id:
                raise ValueError("Repair budget scope belongs to another owner")
        elif not include_history or current_budgets_only:
            repair_budget_scope = RepairBudgetScope(run_id, job_id, current_generations(job))
        job["repair_integration_budgets"] = load_budgets(
            c, run_id, job_id, generations=repair_budget_scope.generations if repair_budget_scope else None,
        )
        if repair_budget_scope is not None:
            job["repair_integration_budgets"] = RepairBudgetProjection(
                job["repair_integration_budgets"], scope=repair_budget_scope,
            )
    cycle = job.get("repair_cycle")
    if isinstance(cycle, dict):
        repair = job.get("repair_job")
        if isinstance(repair, dict) and cycle.get("generation") == repair.get("repair_generation"):
            cycle.update(
                code_modification_attempts=int(repair.get("modification_attempts", 0)),
                validation_attempts=int(repair.get("validation_attempts", 0)),
            )
            for source, target in (
                ("development_thread_id", "development_thread_id"),
                ("repair_checkout", "worktree"),
            ):
                if isinstance(repair.get(source), str):
                    cycle[target] = repair[source]
            job["code_modification_attempts"] = job["modification_attempts"] = int(
                repair.get("modification_attempts", 0)
            )
        elif meta["cycle_evidence_id"] is not None:
            cycle.update(material(c, run_id, owner, "repair_cycle.completed", meta["cycle_evidence_id"]))
    _load_gate(c, run_id, job_id, job, meta["stage"])
    ordinal = job.get("publication_attempts")
    pending = job.get("pending_semantic_attempt")
    expected = None
    if (type(ordinal) is int and ordinal > 0
            and not (isinstance(pending, dict) and pending.get("role") == "publication")):
        expected = publication_attempt_expectation(c, run_id, job_id, ordinal)
        retry = job.get("publication_operation_retry")
        if retry is not None:
            related: dict[str, Any] | None
            if expected is not None:
                related = load_expected_publication_attempt(c, run_id, job_id, expected)
            else:
                related = None
                for attempt in reversed(job.get("semantic_attempt_history", [])):
                    if (isinstance(attempt, dict) and attempt.get("role") == "publication"
                            and attempt.get("status") == "completed" and attempt.get("ordinal") == ordinal):
                        related = attempt
                        break
            if related is None:
                raise ValueError("Publication Operation Retry has no completed Attempt owner")
            if related.get("publication_operation_retry") != retry:
                raise ValueError("Stored Publication Operation Retry conflicts with its completed Attempt")
    missing = (keys & _HISTORIES) - job.keys()
    if missing or expected is not None:
        from agent_run.sqlite_work_execution import attempt_history_metadata

        archives = {
            field: (
                attempt_history_metadata(c, run_id, job_id)
                if field == "semantic_attempt_history"
                else read_metadata(c, run_id, job_id, field)
            )
            for field in missing
        }
        job = JobProjection(job, job_id=job_id, revision=meta["revision"], archives=archives)
        job.loaded_histories.update(job.keys() & _HISTORIES)
        job.expected_publication_attempt = expected
    return job


def read_previous_review(c: sqlite3.Connection, run_id: str, job_id: str) -> dict[str, Any] | None:
    from agent_run.sqlite_change_job_budget import latest_review_artifact

    row = bounded_query(c, "SELECT w.snapshot_job_id,j.evidence_owner_id FROM work_job_windows AS w "
        "JOIN work_jobs AS j ON j.run_id=w.run_id AND j.job_id=w.snapshot_job_id "
        "WHERE w.run_id=? AND w.job_id=? AND EXISTS ("
        "SELECT 1 FROM work_job_review_artifacts AS a WHERE a.run_id=w.run_id "
        "AND a.job_id=w.snapshot_job_id AND a.budget_kind='review_budget') "
        "ORDER BY w.window DESC LIMIT 1",
        (run_id, job_id),
    ).fetchone()
    return latest_review_artifact(c, run_id, row[0], owner_id=row[1]) if row is not None else None


def selected_ticket_number(c: sqlite3.Connection, run_id: str) -> int | None:
    """Read selection identity without materializing the selected Job."""
    row = bounded_query(c, "SELECT l.active_job_id,j.owner_kind,j.owner_key,j.value_ticket_number FROM work_job_layout l "
        "LEFT JOIN work_jobs j ON j.run_id=l.run_id AND j.job_id=l.active_job_id WHERE l.run_id=?",
        (run_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    if row[1] == "selected_ticket":
        return None
    if row[1] != "ticket":
        raise ValueError("Invalid selected Change Job identity")
    if type(row[3]) is int:
        return row[3]
    try:
        number = int(row[2])
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid selected Ticket owner") from error
    if str(number) != row[2]:
        raise ValueError("Invalid selected Ticket owner")
    return number


def load_jobs(
    c: sqlite3.Connection,
    run_id: str,
    *,
    ticket_numbers: set[int] | None = None,
    include_history: bool = True,
    fields: set[str] | None = None,
    archive_scope: Mapping[str, tuple[int, int]] | None = None,
    current_only: bool = False,
    filter_tickets: bool = False,
    repair_budget_scopes: Mapping[str, RepairBudgetScope] | None = None,
) -> dict[str, Any]:
    if fields is not None and not fields & FIELDS:
        return {}
    row = bounded_query(c, "SELECT presence,active_job_id FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return {}
    selected = present(row[0], ROOT_FIELDS) & (FIELDS if fields is None else fields)
    result: dict[str, Any] = {}
    loaded: dict[str, dict[str, Any]] = {}
    for field in selected - {"active_ticket_job"}:
        if field in _ARCHIVES and (not include_history or current_only):
            continue
        slots = bounded_query(c, "SELECT slot,job_id FROM work_job_slots WHERE run_id=? AND container=? ORDER BY position", (run_id, field)
        )
        values: Any = (
            {} if field == "ticket_jobs" else [] if field in _ARCHIVES and field != "retired_parent_job" else None
        )
        for key, identity in slots:
            if (
                field == "ticket_jobs"
                and ticket_numbers is not None
                and (include_history or filter_tickets)
                and int(key) not in ticket_numbers
            ):
                continue
            history = not current_only and (
                include_history or field != "ticket_jobs" or ticket_numbers is None or int(key) in ticket_numbers
            )
            path = (field, key) if field == "ticket_jobs" else (field,)
            omitted = {name for name in _HISTORIES if encode_json((*path, name)) in (archive_scope or {})}
            repair_omitted = {
                name for name in _HISTORIES if encode_json((*path, "repair_job", name)) in (archive_scope or {})
            }
            value = _load_owner(
                c, run_id, identity, include_history=history, omitted_history=omitted,
                repair_omitted_history=repair_omitted,
                repair_budget_scope=(repair_budget_scopes or {}).get(identity),
                current_budgets_only=not include_history or current_only,
            )
            loaded[identity] = value
            if isinstance(values, dict):
                values[key] = value
            elif isinstance(values, list):
                values.append(value)
            else:
                values = value
        result[field] = values
    if "active_ticket_job" in selected:
        identity = row[1]
        omitted = {name for name in _HISTORIES if encode_json(("active_ticket_job", name)) in (archive_scope or {})}
        result["active_ticket_job"] = (
            (
                loaded.get(identity)
                or _load_owner(
                    c, run_id, identity, include_history=include_history and not current_only,
                    omitted_history=omitted, repair_budget_scope=(repair_budget_scopes or {}).get(identity),
                )
            )
            if identity is not None
            else None
        )
    return result


def _projection_owners(state: Mapping[str, Any]) -> Iterator[tuple[tuple[str, ...], Mapping[str, Any], str]]:
    selected = state.get("active_ticket_job")
    if isinstance(selected, Mapping):
        yield ("active_ticket_job",), selected, job_identity(
            "ticket", selected, ticket_number=selected.get("ticket_number")
        )
    for number, job in state.get("ticket_jobs", {}).items():
        if isinstance(job, Mapping):
            yield ("ticket_jobs", number), job, job_identity("ticket", job, ticket_number=int(number))
    for field, kind in _OWNERS.items():
        job = state.get(field)
        if not isinstance(job, Mapping):
            continue
        yield (field,), job, job_identity(kind, job)
        repair = job.get("repair_job")
        if isinstance(repair, Mapping):
            yield (field, "repair_job"), repair, job_identity("run_repair", repair)


def projection_archives(
    state: Mapping[str, Any], fallback: Mapping[str, tuple[int, int]] | None = None
) -> dict[str, tuple[int, int]]:
    result = {}
    for path, job, _ in _projection_owners(state):
        if isinstance(job, JobProjection):
            for field, metadata in job.archives.items():
                value = job.get(field)
                if field in job.loaded_histories:
                    continue
                if isinstance(value, JobHistoryAppend):
                    metadata = value.base_bytes, value.base_count
                result[encode_json((*path, field))] = metadata
        else:
            prefix = encode_json(path)[:-1] + ","
            result.update((key, value) for key, value in (fallback or {}).items() if key.startswith(prefix))
    return result


def projection_repair_budget_scopes(state: Mapping[str, Any]) -> dict[str, RepairBudgetScope]:
    return {
        identity: budgets.scope
        for _, job, identity in _projection_owners(state)
        if isinstance(budgets := job.get("repair_integration_budgets"), RepairBudgetProjection)
    }


def read_job_archives(c: sqlite3.Connection, run_id: str, state: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    return projection_archives(state)


def read_related_attempts(
    c: sqlite3.Connection, run_id: str, state: Mapping[str, Any], archives: Mapping[str, tuple[int, int]]
) -> dict[str, Any]:
    from agent_run.sqlite_work_execution import load_completed_publication_attempt

    result = {}
    for path, job, identity in _projection_owners(state):
        if (
            job.get("publication_operation_retry") is None
            or encode_json((*path, "semantic_attempt_history")) not in archives
        ):
            continue
        pending = job.get("pending_semantic_attempt")
        if isinstance(pending, Mapping) and pending.get("role") == "publication":
            continue
        ordinal = job.get("publication_attempts")
        if type(ordinal) is not int:
            continue
        attempt = load_completed_publication_attempt(c, run_id, identity, ordinal)
        if attempt is not None:
            result[encode_json(path)] = attempt
    return result


def read_owner_history(c: sqlite3.Connection, run_id: str, job_id: str, field: str) -> list[Any]:
    if field not in _HISTORIES:
        raise ValueError("Unsupported Job history")
    row = bounded_query(c, "SELECT presence,evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?",
                        (run_id, job_id)).fetchone()
    if row is None:
        raise ValueError("Missing exact historical Job")
    if field not in present(row[0], JOB_FIELDS):
        return []
    if field == "review_budget_history":
        return _review_windows(c, run_id, job_id)
    if field == "completed_repair_jobs":
        return _completed_repairs(c, run_id, job_id)
    if field == "semantic_attempt_history":
        from agent_run.sqlite_work_execution import load_attempts

        value = load_attempts(c, run_id, job_id, include_history=True).get(field, [])
        if not isinstance(value, list):
            raise ValueError("Job history must be an array")
        return value
    if field in (HISTORY_FIELDS | THREAD_FIELDS) - {"completed_repair_jobs"}:
        return _load_history(c, run_id, job_id, field, owner_id=row[1])
    raise ValueError("Unsupported Job history")


def read_latest_thread(c: sqlite3.Connection, run_id: str, job_id: str, field: str) -> str | None:
    """Read the exact tail of one owned Thread relation without its prefix."""
    if field not in THREAD_FIELDS:
        raise ValueError("Unsupported exact Thread history")
    _, count = read_metadata(c, run_id, job_id, field)
    row = bounded_query(c,
        "SELECT position,thread_id,evidence_id,logical_bytes FROM work_job_archives "
        "WHERE run_id=? AND job_id=? AND history_kind=? ORDER BY position DESC LIMIT 1",
        (run_id, job_id, field),
    ).fetchone()
    if count == 0 and row is None:
        return None
    if (row is None or type(row[0]) is not int or row[0] != count - 1
            or type(row[1]) is not str or row[2] is not None
            or type(row[3]) is not int or row[3] != len(encode_json(row[1]).encode("utf-8"))):
        raise ValueError("Invalid exact Thread history tail")
    return row[1]
