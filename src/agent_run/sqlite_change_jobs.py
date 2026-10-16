"""Read and commit bounded Change Jobs and identity-only selections."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_query_rows import bounded_query
from agent_run.sqlite_change_job_queries import (
    FIELDS as FIELDS, _ARCHIVES, _HISTORIES, _OWNERS, _load_owner as _load_owner,
    job_identity as job_identity,
    read_previous_review as read_previous_review,
    selected_ticket_number as selected_ticket_number,
    load_jobs as load_jobs,
    projection_archives as projection_archives,
    projection_repair_budget_scopes as projection_repair_budget_scopes,
    read_job_archives as read_job_archives,
    read_related_attempts as read_related_attempts,
    read_owner_history as read_owner_history,
)
from agent_run.sqlite_change_job_archives import read_metadata, save_metadata
from agent_run.sqlite_change_job_codec import (
    load_core,
    load_record,
    material,
    presence,
    present,
    put_material,
    save_core,
    save_record,
)
from agent_run.sqlite_change_job_schema import (
    ATTEMPT_FIELDS,
    CYCLE_DERIVED,
    GATE_FIELDS,
    HISTORY_FIELDS,
    JOB_FIELDS,
    RECORDS,
    ROOT_FIELDS,
    STRING_FIELDS,
    THREAD_FIELDS,
)
from agent_run.sqlite_change_job_schema import INDEXES as INDEXES, TABLES as TABLES
from agent_run.sqlite_change_job_gates import gate_projection, load_gate as _load_gate, save_gate as _save_gate
from agent_run.sqlite_encoding import copy_json_tree, encode_json
from agent_run.sqlite_mutations import _same
from agent_run.work_projection import JobProjection, JobHistoryAppend, RepairBudgetProjection

_MISSING = object()


def _save_history(c: sqlite3.Connection, run_id: str, job_id: str, field: str, value: Any, *, owner_id: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"Change Job {field} must be an array")
    delta = isinstance(value, JobHistoryAppend)
    base_count = value.base_count if isinstance(value, JobHistoryAppend) else 0
    if isinstance(value, JobHistoryAppend):
        prior_size, count = read_metadata(c, run_id, job_id, field)
        total = prior_size - 2 - max(count - 1, 0)
        if base_count > count:
            raise ValueError("Job history append has an invalid base count")
        if base_count == count and value.base_bytes != prior_size:
            raise ValueError("Job history size changed before its append")
    else:
        c.execute(
            "DELETE FROM work_job_archives WHERE run_id=? AND job_id=? AND history_kind=?", (run_id, job_id, field)
        )
        total = count = 0
    for index, item in enumerate(value):
        position = base_count + index
        if delta and position < count:
            row = bounded_query(c, "SELECT evidence_id,thread_id FROM work_job_archives WHERE run_id=? AND job_id=? AND history_kind=? AND position=?",
                (run_id, job_id, field, position),
            ).fetchone()
            previous = (
                row[1]
                if row is not None and row[0] is None
                else material(c, run_id, owner_id, field, row[0]) if row is not None else None
            )
            if not _same(previous, item):
                raise ValueError("Cannot overwrite unread Job history")
            continue
        if position != count:
            raise ValueError("Job history append is no longer contiguous")
        if field in THREAD_FIELDS:
            if not isinstance(item, str):
                raise ValueError("Thread history must contain strings")
            evidence_id, thread_id = None, item
        else:
            evidence_id, thread_id = put_material(c, run_id, owner_id, field, item), None
        size = len(encode_json(item).encode())
        total += size
        c.execute(
            "INSERT INTO work_job_archives VALUES (?,?,?,?,?,?,?)",
            (run_id, job_id, field, position, evidence_id, thread_id, size),
        )
        count += 1
    save_metadata(c, run_id, job_id, field, total + 2 + max(count - 1, 0), count)


def _save_completed_repairs(c: sqlite3.Connection, run_id: str, job_id: str, records: Any) -> None:
    if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
        raise ValueError("Completed Repairs must contain completion records")
    container = "completed_repair_jobs:" + job_id
    c.execute("DELETE FROM work_job_slots WHERE run_id=? AND container=?", (run_id, container))
    total = 0
    for position, item in enumerate(records):
        encoded = encode_json(item).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        total += len(encoded)
        identity = job_id + ":completed-repair:" + digest
        origin = bounded_query(c, "SELECT job_id FROM work_jobs WHERE run_id=? AND owner_kind='run_repair' AND value_repair_branch=? AND value_candidate_sha=?",
            (run_id, item.get("repair_branch"), item.get("candidate_sha")),
        ).fetchone()
        if not bounded_query(c, "SELECT 1 FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, identity)).fetchone():
            _save_owner(
                c,
                run_id,
                identity,
                "completed_repair",
                origin[0] if origin else job_id,
                item,
                evidence_owner_id=origin[0] if origin else identity,
                budget_owner_id=job_id,
            )
        _slot(c, run_id, container, str(position), position, identity)
    save_metadata(c, run_id, job_id, "completed_repair_jobs", total + 2 + max(len(records) - 1, 0), len(records))


def _save_owner(
    c: sqlite3.Connection,
    run_id: str,
    job_id: str,
    kind: str,
    owner_key: str,
    job: Mapping[str, Any],
    baseline: Mapping[str, Any] | None = None,
    *,
    evidence_owner_id: str | None = None,
    budget_owner_id: str | None = None,
) -> dict[str, Any]:
    from agent_run.sqlite_work_execution import save_attempts

    if set(job) - JOB_FIELDS:
        raise ValueError(f"Unsupported {kind} Job fields: {sorted(set(job) - JOB_FIELDS)}")
    existing = bounded_query(c, "SELECT presence,stage,evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id)
    ).fetchone()
    # A replacement generation has its own row. Values equal to its former
    # generation still need columns, child records and archive summaries here.
    old = (baseline or {}) if existing is not None else {}
    if isinstance(job, JobProjection):
        unread = job.archives.keys() - job.loaded_histories - job.keys()
        additionally_read = unread & old.keys()
        if additionally_read:
            if (
                not isinstance(old, JobProjection)
                or old.job_id != job_id
                or job.job_id != job_id
                or any(field not in old.loaded_histories or old.archives.get(field) != job.archives[field]
                       for field in additionally_read)
            ):
                raise ValueError("Cannot remove unread Job history using a complete baseline")
            # An explicit read-only consumer can materialize this exact
            # baseline after the proposed view was
            # copied. Its additional read does not request an archive removal.
            old = {key: value for key, value in old.items() if key not in additionally_read}
    owner_id = evidence_owner_id or (existing[2] if existing else job_id)
    if existing is None:
        c.execute(
            "INSERT INTO work_jobs (run_id,job_id,owner_kind,owner_key,revision,presence,evidence_owner_id,budget_owner_id) VALUES (?,?,?,?,0,?,?,?)",
            (run_id, job_id, kind, owner_key, presence((), JOB_FIELDS), owner_id, budget_owner_id or job_id),
        )
    stored_keys = present(existing[0], JOB_FIELDS) if existing else set()
    for field in (job.keys() & _HISTORIES) - old.keys():
        if field in stored_keys:
            if isinstance(job[field], JobHistoryAppend):
                continue
            if isinstance(job, JobProjection) and field in job.loaded_histories:
                old = dict(old)
                old[field] = read_owner_history(c, run_id, job_id, field)
                continue
            raise ValueError(f"cannot replace unloaded Job history: {field}")
    if isinstance(job, JobProjection) and job.publication_update is not None:
        from agent_run.sqlite_work_execution_attempts import save_publication_retry

        before_attempt, updated_attempt = job.publication_update
        if "semantic_attempt_history" not in job.loaded_histories and not _same(before_attempt, updated_attempt):
            history = job.get("semantic_attempt_history")
            committed = True
            if isinstance(history, JobHistoryAppend):
                ordinal = next((index for index, attempt in enumerate(history)
                                if isinstance(attempt, dict)
                                and attempt.get("attempt_id") == updated_attempt.get("attempt_id")), None)
                if ordinal is not None:
                    position = bounded_query(c,
                        "SELECT position FROM work_job_attempt_history WHERE run_id=? AND job_id=? AND attempt_id=?",
                        (run_id, job_id, updated_attempt.get("attempt_id")),
                    ).fetchone()
                    if position is None:
                        # A newly closed suffix has no archived version yet.
                        # Its initial retry facts join the ordinary append.
                        committed = False
                    elif position[0] != history.base_count + ordinal:
                        raise ValueError("Completed Publication retry has a different history position")
            if committed:
                save_publication_retry(c, run_id, job_id, before_attempt, updated_attempt)
    # The retry patch must precede any current-version advance: a retained
    # known suffix then observes the same exact version at its old position.
    save_attempts(c, run_id, job_id, dict(job), dict(old))
    if isinstance(job, JobProjection) and job.publication_update is not None:
        _, updated_attempt = job.publication_update
        job.publication_update = (copy_json_tree(updated_attempt), updated_attempt)
    changed = {key for key in job.keys() | old.keys() if not _same(job.get(key, _MISSING), old.get(key, _MISSING))}
    current, meta = load_core(c, run_id, job_id, fields=changed)
    _load_gate(c, run_id, job_id, current, meta["stage"])
    if existing is not None and baseline is not None:
        if (
            "phase" in changed
            and "phase" in old
            and not _same(current.get("phase"), old["phase"])
            and not _same(current.get("phase"), job.get("phase"))
        ):
            raise ValueError(f"Change Job {job_id} changed concurrently: phase")
        for key in changed - GATE_FIELDS - ATTEMPT_FIELDS:
            if key not in old or key not in current:
                continue
            if not _same(current[key], old[key]) and not _same(current[key], job.get(key, _MISSING)):
                raise ValueError(f"Change Job {job_id} changed concurrently: {key}")
    # Historical fields absent from both sparse views stay untouched.
    removed = old.keys() - job.keys()
    keys = (meta["keys"] - removed) | job.keys()
    for key in changed:
        if key in job:
            current[key] = job[key]
        else:
            current.pop(key, None)
    # Presence includes child records without embedding their mutable values.
    for key in keys - current.keys():
        current[key] = job.get(key)
    repair_id = meta["repair_job_id"]
    if "repair_job" in changed:
        repair = job.get("repair_job")
        repair_id = None
        if isinstance(repair, Mapping):
            repair_id = job_identity("run_repair", repair) if kind == "run_acceptance" else job_id + ":repair"
            old_repair = old.get("repair_job")
            saved_repair = _save_owner(
                c,
                run_id,
                repair_id,
                "run_repair" if kind == "run_acceptance" else "budget_window",
                owner_key,
                repair,
                old_repair if isinstance(old_repair, Mapping) else None,
                evidence_owner_id=owner_id if kind != "run_acceptance" else None,
                budget_owner_id=job_id,
            )
            if isinstance(job, dict):
                job["repair_job"] = saved_repair
    for field in RECORDS.keys() & changed:
        value = job.get(field)
        previous_value = old.get(field)
        if field == "repair_cycle" and isinstance(value, Mapping):
            value = {key: item for key, item in value.items() if key not in CYCLE_DERIVED}
            if isinstance(previous_value, Mapping):
                previous_value = {key: item for key, item in previous_value.items() if key not in CYCLE_DERIVED}
        if existing is not None and field in old:
            stored = load_record(c, run_id, job_id, field, owner_id=owner_id)
            if not _same(stored, previous_value) and not _same(stored, value):
                raise ValueError(f"Change Job {job_id} changed concurrently: {field}")
        save_record(c, run_id, job_id, field, value, owner_id=owner_id)
    for field in (HISTORY_FIELDS | THREAD_FIELDS) & changed:
        if field in job:
            if field == "completed_repair_jobs":
                _save_completed_repairs(c, run_id, job_id, job[field])
            else:
                _save_history(c, run_id, job_id, field, job[field], owner_id=owner_id)
    for field in STRING_FIELDS & changed:
        c.execute(
            "DELETE FROM work_job_record_strings WHERE run_id=? AND job_id=? AND record_kind='job' AND list_name=?",
            (run_id, job_id, field),
        )
        value = job.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"Change Job {field} must contain strings")
        c.executemany(
            "INSERT INTO work_job_record_strings VALUES (?,?,?,?,?,?)",
            ((run_id, job_id, "job", field, position, item) for position, item in enumerate(value)),
        )
    if "review_budget_history" in changed and "review_budget_history" in job:
        snapshots = job["review_budget_history"]
        if not isinstance(snapshots, list):
            raise ValueError("review_budget_history must be an array")
        prior_size, prior_count = read_metadata(
            c, run_id, job_id, "review_budget_history", required="review_budget_history" in stored_keys
        )
        total = prior_size - 2 - max(prior_count - 1, 0)
        if not isinstance(snapshots, JobHistoryAppend) and len(snapshots) < prior_count:
            raise ValueError("Change Job budget history cannot be removed")
        for ordinal, snapshot in enumerate(snapshots, 1):
            position = snapshots.base_count + ordinal if isinstance(snapshots, JobHistoryAppend) else ordinal
            identity = f"{job_id}:budget-window:{position}"
            if position <= prior_count:
                previous = old.get("review_budget_history")
                if (
                    isinstance(previous, list)
                    and position <= len(previous)
                    and _same(snapshot, previous[position - 1])
                ):
                    continue
                if not _same(snapshot, _load_owner(c, run_id, identity)):
                    raise ValueError("Historical budget evidence cannot be changed")
                continue
            _save_owner(c, run_id, identity, "budget_window", owner_key, snapshot, evidence_owner_id=owner_id)
            size = len(encode_json(snapshot).encode())
            total += size
            c.execute("INSERT INTO work_job_windows VALUES (?,?,?,?,?)", (run_id, job_id, position, identity, size))
        count = snapshots.base_count + len(snapshots) if isinstance(snapshots, JobHistoryAppend) else len(snapshots)
        save_metadata(c, run_id, job_id, "review_budget_history", total + 2 + max(count - 1, 0), count)
    if "repair_integration_budgets" in changed:
        from agent_run.sqlite_repair_budgets import save_budgets

        save_budgets(c, run_id, job_id, job, baseline or {}, owner_id=owner_id)
    cycle = job.get("repair_cycle")
    cycle_evidence = meta["cycle_evidence_id"]
    if isinstance(cycle, Mapping) and repair_id is None:
        cycle_evidence = put_material(
            c,
            run_id,
            owner_id,
            "repair_cycle.completed",
            {key: value for key, value in cycle.items() if key in CYCLE_DERIVED},
        )
    repair = job.get("repair_job")
    if (
        repair_id is not None
        and kind == "run_acceptance"
        and isinstance(cycle, Mapping)
        and isinstance(repair, Mapping)
        and cycle.get("generation") == repair.get("repair_generation")
    ):
        current.pop("code_modification_attempts", None)
        current.pop("modification_attempts", None)
    # While a Repair is current its counters are derived. Once it retires,
    # those same values become the completed cycle's fixed facts; their
    # absence from the SQL core still requires writing them even when the
    # caller's derived baseline had the same visible values.
    changed.update(current.keys() - meta["keys"])
    stage = _save_gate(c, run_id, job_id, job, old, meta["stage"])
    save_core(
        c,
        run_id,
        job_id,
        current,
        owner_id=owner_id,
        stage=stage,
        budget_owner_id=budget_owner_id or meta["budget_owner_id"] or job_id,
        repair_job_id=repair_id,
        cycle_evidence_id=cycle_evidence,
        changed=changed,
    )
    revision = bounded_query(c, "SELECT revision FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()[0]
    from agent_run.sqlite_repair_budgets import narrow_saved_scope

    narrow_saved_scope(job, run_id, job_id)
    if not isinstance(job, JobProjection):
        from agent_run.sqlite_work_execution import attempt_history_metadata

        archives = {
            field: (
                attempt_history_metadata(c, run_id, job_id)
                if field == "semantic_attempt_history"
                else read_metadata(c, run_id, job_id, field)
            )
            for field in (keys & _HISTORIES) - job.keys()
        }
        if not archives:
            # A complete caller still owns this exact mutable object. Only
            # unread archives need a JobProjection and its query metadata.
            return job if isinstance(job, dict) else dict(job)
        job = JobProjection(job, job_id=job_id, revision=revision, archives=archives)
    job.revision = revision
    job.loaded_histories.update(
        field for field in job.keys() & _HISTORIES if not isinstance(job[field], JobHistoryAppend)
    )
    return job


def _slot(c: sqlite3.Connection, run_id: str, field: str, key: str, position: int, identity: str) -> None:
    c.execute(
        "INSERT INTO work_job_slots VALUES (?,?,?,?,?) ON CONFLICT(run_id,container,slot) DO UPDATE SET position=excluded.position,job_id=excluded.job_id",
        (run_id, field, key, position, identity),
    )


def append_archive(c: sqlite3.Connection, run_id: str, field: str, items: list[Any]) -> None:
    if field not in _ARCHIVES:
        raise ValueError("Unsupported Change Job archive")
    position = bounded_query(c, "SELECT COALESCE(MAX(position)+1,0) FROM work_job_slots WHERE run_id=? AND container=?", (run_id, field)
    ).fetchone()[0]
    for offset, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError("Archived Job must be an object")
        budgets = item.get("repair_integration_budgets")
        if isinstance(budgets, RepairBudgetProjection):
            from agent_run.sqlite_repair_budgets import load_budgets

            # Retirement explicitly consumes this owner's history. Preserve
            # all generations in the new archive, including pending updates.
            scope = budgets.scope
            if scope.run_id != run_id:
                raise ValueError("Archived Repair budgets belong to another owner")
            complete = load_budgets(c, run_id, scope.job_id)
            complete.update(item.get("repair_integration_budgets", {}))
            item = item.copy() if isinstance(item, dict) else dict(item)
            item["repair_integration_budgets"] = complete
        slot = position + offset
        subject = str(item.get("work_subject", field))
        generation = (
            item.get("generation")
            or item.get("ticket_branch_generation")
            or item.get("parent_generation")
            or item.get("repair_generation")
            or 1
        )
        identity = f"archive:{field}:{slot}:{subject}:generation:{generation}"
        origin_kind = str(
            item.get("owner_kind")
            or (
                "ticket"
                if subject.startswith("ticket:")
                else (
                    "parent"
                    if subject.startswith("parent-only:")
                    else "run_repair" if subject.startswith("run-repair:") else ""
                )
            )
        )
        origin: str | None = None
        if origin_kind in {"ticket", "parent", "run_repair", "run_acceptance", "run_publication"}:
            origin_data = dict(item)
            field_name = {
                "ticket": "ticket_branch_generation",
                "parent": "parent_generation",
                "run_repair": "repair_generation",
            }.get(origin_kind)
            if field_name is not None:
                origin_data[field_name] = generation
            number = int(subject.split(":", 1)[1]) if origin_kind == "ticket" else None
            candidate = job_identity(origin_kind, origin_data, ticket_number=number)
            if bounded_query(c, "SELECT 1 FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, candidate)).fetchone():
                origin = candidate
        _save_owner(c, run_id, identity, _ARCHIVES[field], str(slot), item, evidence_owner_id=origin)
        _slot(c, run_id, field, str(slot), slot, identity)
    row = bounded_query(c, "SELECT presence,active_job_id FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
    keys = present(row[0], ROOT_FIELDS) if row else set()
    c.execute(
        "INSERT INTO work_job_layout VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET presence=excluded.presence",
        (run_id, presence(keys | {field}, ROOT_FIELDS), row[1] if row else None),
    )


def save_jobs(
    c: sqlite3.Connection,
    run_id: str,
    state: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    fields: set[str] | None = None,
    keyed_changes: Mapping[str, Any] | None = None,
) -> None:
    selected = FIELDS if fields is None else FIELDS & fields
    row = bounded_query(c, "SELECT presence,active_job_id FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
    keys, active_id = (present(row[0], ROOT_FIELDS), row[1]) if row else (set(), None)
    for field in ("ticket_jobs", "parent_job", "run_acceptance", "run_publication", *_ARCHIVES):
        if field not in selected:
            continue
        if field not in state and field not in baseline:
            continue
        if field in state:
            keys.add(field)
        else:
            keys.discard(field)
        if field == "active_ticket_job":
            continue
        incoming, previous = state.get(field), baseline.get(field)
        if field in _ARCHIVES:
            if _same(incoming, previous):
                continue
            values = incoming if isinstance(incoming, list) else [incoming] if isinstance(incoming, Mapping) else []
            if isinstance(previous, list) and _same(values[: len(previous)], previous):
                append_archive(c, run_id, field, values[len(previous) :])
            else:
                c.execute("DELETE FROM work_job_slots WHERE run_id=? AND container=?", (run_id, field))
                append_archive(c, run_id, field, values)
            continue
        if field == "ticket_jobs":
            jobs, old_jobs = incoming or {}, previous or {}
            changed = (
                set(keyed_changes[field])
                if keyed_changes is not None and field in keyed_changes
                else set(jobs) | set(old_jobs)
            )
            for key in changed:
                job, old = jobs.get(key), old_jobs.get(key)
                if _same(job, old):
                    continue
                if isinstance(job, Mapping):
                    identity = job_identity("ticket", job, ticket_number=int(key))
                    saved = _save_owner(
                        c, run_id, identity, "ticket", str(key), job, old if isinstance(old, Mapping) else None
                    )
                    if isinstance(jobs, dict):
                        jobs[key] = saved
                    _slot(c, run_id, field, str(key), int(key), identity)
                else:
                    c.execute(
                        "DELETE FROM work_job_slots WHERE run_id=? AND container=? AND slot=?",
                        (run_id, field, str(key)),
                    )
            continue
        if isinstance(incoming, Mapping):
            kind = _OWNERS[field]
            identity = job_identity(kind, incoming)
            saved = _save_owner(
                c, run_id, identity, kind, field, incoming, previous if isinstance(previous, Mapping) else None
            )
            if isinstance(state, dict):
                state[field] = saved
            _slot(c, run_id, field, "current", 0, identity)
        else:
            c.execute("DELETE FROM work_job_slots WHERE run_id=? AND container=?", (run_id, field))
    if "active_ticket_job" in selected and ("active_ticket_job" in state or "active_ticket_job" in baseline):
        if "active_ticket_job" in state:
            keys.add("active_ticket_job")
        else:
            keys.discard("active_ticket_job")
        active = state.get("active_ticket_job")
        active_id = None
        if isinstance(active, Mapping):
            number = active.get("ticket_number")
            kind = "ticket" if type(number) is int else "selected_ticket"
            active_id = job_identity(kind, active)
            jobs = state.get("ticket_jobs")
            stored = jobs.get(str(number)) if isinstance(jobs, Mapping) else None
            old_active = baseline.get("active_ticket_job")
            if stored is None or not _same(active, old_active) and not _same(active, stored):
                saved = _save_owner(
                    c,
                    run_id,
                    active_id,
                    kind,
                    str(number) if number is not None else "active_ticket_job",
                    active,
                    old_active if isinstance(old_active, Mapping) else None,
                )
                if isinstance(state, dict):
                    state["active_ticket_job"] = saved
            elif isinstance(stored, JobProjection) and isinstance(state, dict):
                state["active_ticket_job"] = stored
    c.execute(
        "INSERT INTO work_job_layout VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET presence=excluded.presence,active_job_id=excluded.active_job_id",
        (run_id, presence(keys, ROOT_FIELDS), active_id),
    )


def project_current_gates(
    c: sqlite3.Connection,
    run_id: str,
    state: dict[str, Any],
    *,
    fields: set[str],
    keyed_changes: Mapping[str, set[str]] | None = None,
) -> tuple[set[int], set[int]]:
    """Publish only committed gates for the owners touched by this save."""
    owners: list[tuple[str, dict[str, Any]]] = []
    if "ticket_jobs" in fields:
        tickets = state.get("ticket_jobs", {})
        for key in (keyed_changes or {}).get("ticket_jobs", tickets.keys()):
            job = tickets.get(key)
            if isinstance(job, dict):
                owners.append((job_identity("ticket", job, ticket_number=int(key)), job))
    for field, kind in _OWNERS.items():
        job = state.get(field)
        if field not in fields or not isinstance(job, dict):
            continue
        identity = job_identity(kind, job)
        owners.append((identity, job))
        repair = job.get("repair_job") if field == "run_acceptance" else None
        if isinstance(repair, dict):
            linked = bounded_query(c, "SELECT repair_job_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, identity)
            ).fetchone()
            if linked is not None and linked[0] is not None:
                owners.append((linked[0], repair))
    active = state.get("active_ticket_job")
    if isinstance(active, dict):
        selected = bounded_query(c, "SELECT active_job_id FROM work_job_layout WHERE run_id=?", (run_id,)).fetchone()
        if (
            selected is not None
            and selected[0] is not None
            and ("active_ticket_job" in fields or any(identity == selected[0] for identity, _ in owners))
        ):
            owners.append((selected[0], active))
    changed: set[int] = set()
    revealed: set[int] = set()
    projections: dict[str, dict[str, Any]] = {}
    for identity, job in owners:
        if identity not in projections:
            projections[identity] = gate_projection(c, run_id, identity)
        current = projections[identity]
        if _same({key: job[key] for key in GATE_FIELDS if key in job}, current):
            continue
        if any(not _same(job.get(key), current.get(key)) for key in ("phase", "blocked_reason")):
            revealed.add(id(job))
        for key in GATE_FIELDS:
            job.pop(key, None)
        job.update(current)
        changed.add(id(job))
    return changed, revealed
