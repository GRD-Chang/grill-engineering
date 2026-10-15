"""Single authoritative Run root; all operations join the caller transaction."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_change_job_codec import _typed, presence, present
from agent_run.sqlite_mutations import _same
from agent_run import sqlite_run_conditions as conditions
from agent_run import sqlite_run_closeout as closeout
from agent_run import sqlite_run_scheduling as scheduling
from agent_run.sqlite_run_graph import load_graph, save_graph
from agent_run.sqlite_run_inputs import identity, load_parent, owner, save_parent
from agent_run.sqlite_run_schema import (
    BOOLEANS, CORE, FIELDS, INDEXES, INTEGERS, MATERIALS, METADATA, RECORDS, SCHEDULING,
    TABLES as ROOT_TABLES, TEXT, column,
)
from agent_run.sqlite_work_evidence import get_evidence, put_evidence

MAX_CURRENT_BYTES = 256 * 1024
_COLUMNS = sorted(CORE)
TABLES = {**ROOT_TABLES, **closeout.TABLES}
INDEXES = {**INDEXES, **closeout.INDEXES}


def _root(c: sqlite3.Connection, run_id: str) -> tuple[set[str], set[str], dict[str, Any]]:
    # Prove the actual stored bytes before transferring any potentially
    # damaged text/BLOB into Python. Metadata is the same current record.
    byte_columns = ["r." + column(key) for key in _COLUMNS] + ["i." + key for key in sorted(METADATA)]
    byte_columns += ["r.presence", "r.nulls"]
    size = c.execute("SELECT " + "+".join("COALESCE(LENGTH(CAST(" + name + " AS BLOB)),0)" for name in byte_columns)
                     + " FROM run_root r JOIN runs i USING(run_id) WHERE r.run_id=?", (run_id,)).fetchone()
    if size is None:
        raise ValueError("Missing relational Run root")
    if type(size[0]) is not int or size[0] + 16 * len(byte_columns) > MAX_CURRENT_BYTES:
        raise ValueError("Run current record exceeds byte limit")
    row = c.execute("SELECT presence,nulls," + ",".join(column(key) for key in _COLUMNS)
                    + " FROM run_root WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("Missing relational Run root")
    keys, nulls = present(row[0], FIELDS), present(row[1], FIELDS)
    if nulls - keys:
        raise ValueError("Invalid relational Run null fields")
    return keys, nulls, dict(zip(_COLUMNS, row[2:], strict=True))


def _requeue(c: sqlite3.Connection, run_id: str, value: Any) -> None:
    from agent_run.sqlite_change_jobs import _load_owner, job_identity
    from agent_run.requeue import _retired_record

    c.execute("DELETE FROM run_requeue_transition WHERE run_id=?", (run_id,))
    if value is None:
        return
    if not isinstance(value, Mapping) or value.keys() - {"retired", "base_sha", "close_nonce"}:
        raise ValueError("Invalid Run requeue transition")
    retired = value.get("retired")
    if not isinstance(retired, Mapping) or not isinstance(retired.get("work_subject"), str):
        raise ValueError("Run requeue requires its exact source Job")
    subject, generation = retired["work_subject"], retired.get("generation")
    if subject.startswith("ticket:"):
        kind, field, number = "ticket", "ticket_branch_generation", int(subject.split(":", 1)[1])
    elif subject.startswith("parent-only:"):
        kind, field, number = "parent", "parent_generation", None
    else:
        kind, field, number = "run_repair", "repair_generation", None
    job_id = job_identity(kind, {field: generation}, ticket_number=number)
    row = c.execute("SELECT 1 FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    if row is None or not _same(_retired_record(subject, _load_owner(c, run_id, job_id)), retired):
        raise ValueError("Run requeue source no longer matches its exact Job")
    base, nonce = value.get("base_sha"), value.get("close_nonce")
    _typed(base, "TEXT", "requeue.base_sha")
    _typed(nonce, "TEXT", "requeue.close_nonce")
    header = (presence(value, {"retired", "base_sha", "close_nonce"}), base, nonce, job_id, identity(retired))
    if sum(len(item.encode()) if isinstance(item, str) else len(item) if isinstance(item, bytes) else 0
           for item in header) + 16 * len(header) > MAX_CURRENT_BYTES:
        raise ValueError("Run requeue current record exceeds byte limit")
    c.execute("INSERT INTO run_requeue_transition VALUES (?,?,?,?,?,?)",
              (run_id, *header))


def load_requeue(c: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    from agent_run.sqlite_change_jobs import _load_owner
    from agent_run.requeue import _retired_record

    columns = ("presence", "base_sha", "close_nonce", "source_job_id", "source_record_id")
    lengths = "+".join(f"COALESCE(LENGTH(CAST({name} AS BLOB)),0)+16" for name in columns)
    size = c.execute(f"SELECT {lengths} FROM run_requeue_transition WHERE run_id=?", (run_id,)).fetchone()
    if size is not None and (type(size[0]) is not int or size[0] > MAX_CURRENT_BYTES):
        raise ValueError("Run requeue current record exceeds byte limit")
    row = c.execute("SELECT presence,base_sha,close_nonce,source_job_id,source_record_id FROM run_requeue_transition WHERE run_id=?",
                    (run_id,)).fetchone()
    if row is None:
        raise ValueError("Missing Run requeue transition")
    keys = present(row[0], {"retired", "base_sha", "close_nonce"})
    source = _load_owner(c, run_id, row[3])
    if row[3].startswith("ticket:"):
        subject = ":".join(row[3].split(":")[:2])
    else:
        subject = ("parent-only:" if row[3].startswith("parent-only:") else "run-repair:") + run_id
    retired = _retired_record(subject, source)
    # A save or a refresh can change the Job row revision without changing
    # the exact retirement facts. Bind those facts, not that incidental counter.
    if identity(retired) != row[4]:
        raise ValueError("Run requeue source Job version changed")
    result = {"retired": retired} if "retired" in keys else {}
    for key, value in zip(("base_sha", "close_nonce"), row[1:3], strict=True):
        if key in keys:
            result[key] = _typed(value, "TEXT", "requeue." + key)
    return result


def save(c: sqlite3.Connection, run_id: str, state: Mapping[str, Any], baseline: Mapping[str, Any], *,
         fields: set[str], keyed_changes: Mapping[str, set[str]] | None = None) -> None:
    c.execute("INSERT OR IGNORE INTO run_root(run_id,presence,nulls) VALUES (?,?,?)",
              (run_id, presence((), FIELDS), presence((), FIELDS)))
    fields = {field for field in fields & FIELDS
              if (field in state) != (field in baseline)
              or field in state and not _same(state[field], baseline[field])}
    if not fields:
        return
    keys, nulls, _ = _root(c, run_id)
    for field in fields & FIELDS:
        if field not in state and field not in baseline:
            continue
        if field in state and field in baseline and _same(state[field], baseline[field]):
            continue
        value = state.get(field)
        if field in state:
            keys.add(field)
            (nulls.add if value is None else nulls.discard)(field)
        else:
            keys.discard(field)
            nulls.discard(field)
        target, encoded = field, value
        if field == "run_id":
            if field in state and value != run_id:
                raise ValueError("Run root identity does not match its owner")
            continue
        if field in METADATA:
            _typed(value, "TEXT", field)
            c.execute(f"UPDATE runs SET {field}=? WHERE run_id=?", (value, run_id))
            continue
        if field == "parent":
            target, encoded = "parent_version_id", save_parent(c, run_id, value)
        elif field == "ticket_graph":
            target, encoded = "graph_snapshot_id", save_graph(c, run_id, value)
        elif field in MATERIALS:
            encoded = put_evidence(c, run_id, owner(run_id), field, value) if value is not None else None
        elif field in TEXT | INTEGERS | BOOLEANS:
            encoded = _typed(value, "BOOLEAN" if field in BOOLEANS else "INTEGER" if field in INTEGERS else "TEXT", field)
        elif field in SCHEDULING:
            scheduling.save(c, run_id, field, value, keys=(keyed_changes or {}).get(field) if field in state else None)
            continue
        elif field in RECORDS:
            conditions.save_record(c, run_id, field, value)
            continue
        elif field in closeout.FIELDS:
            closeout.save(c, run_id, field, value)
            continue
        elif field == "diagnostics":
            conditions.save_diagnostics(c, run_id, "diagnostics", value)
            continue
        elif field == "requeue_transition":
            _requeue(c, run_id, value)
            continue
        c.execute(f"UPDATE run_root SET {column(target)}=? WHERE run_id=?", (encoded, run_id))
    c.execute("UPDATE run_root SET presence=?,nulls=? WHERE run_id=?", (presence(keys, FIELDS), presence(nulls, FIELDS), run_id))
    _root(c, run_id)


def load(c: sqlite3.Connection, run_id: str, *, fields: set[str] | None = None,
         keyed_changes: Mapping[str, set[str]] | None = None) -> dict[str, Any]:
    keys, nulls, values = _root(c, run_id)
    selected = keys & (FIELDS if fields is None else fields)
    result: dict[str, Any] = {}
    metadata = sorted(selected & METADATA)
    if metadata:
        row = c.execute("SELECT " + ",".join(metadata) + " FROM runs WHERE run_id=?", (run_id,)).fetchone()
        for key, value in zip(metadata, row, strict=True):
            _typed(value, "TEXT", key)
            result[key] = value
    for field in selected - METADATA:
        if field in nulls:
            result[field] = None
            continue
        if field == "run_id":
            value = run_id
        elif field == "parent":
            value = load_parent(c, run_id, values["parent_version_id"])
        elif field == "ticket_graph":
            value = load_graph(c, run_id, values["graph_snapshot_id"])
        elif field in MATERIALS:
            value = get_evidence(c, run_id, values[field], owner_id=owner(run_id), kind=field)
        elif field in TEXT | INTEGERS:
            value = values[field]
            _typed(value, "INTEGER" if field in INTEGERS else "TEXT", field)
        elif field in BOOLEANS:
            value = values[field]
            if type(value) is not int or value not in {0, 1}:
                raise ValueError("Invalid Run root boolean")
            value = bool(value)
        elif field in SCHEDULING:
            value = scheduling.load(c, run_id, field, keys=(keyed_changes or {}).get(field))
        elif field in RECORDS:
            value = conditions.load_record(c, run_id, field)
        elif field in closeout.FIELDS:
            value = closeout.load(c, run_id, field)
        elif field == "diagnostics":
            value = conditions.load_diagnostics(c, run_id, "diagnostics")
        else:
            value = load_requeue(c, run_id)
        result[field] = value
    return result


def has_supervision_work(c: sqlite3.Connection, run_id: str) -> bool:
    keys, nulls, values = _root(c, run_id)
    required = {"status", "ticket_integration_holder", "ticket_integration_queue"}
    if required - keys or (required - {"ticket_integration_holder"}) & nulls:
        raise ValueError("Missing Run Ticket supervision facts")
    status = c.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    holder = values["ticket_integration_holder"]
    if not isinstance(status, str) or holder is not None and (type(holder) is not int or holder < 1):
        raise ValueError("Invalid Run Ticket supervision status or holder")
    count = c.execute("SELECT COUNT(*) FROM run_integration_queue WHERE run_id=?", (run_id,)).fetchone()[0]
    if count > scheduling.MAX_SCHEDULING_ITEMS:
        raise ValueError("Run integration queue exceeds item limit")
    return status not in {"active", "ticket_delivery_pending"} or holder is not None or count != 0


def pause_requested(c: sqlite3.Connection, run_id: str, number: int) -> bool:
    keys, nulls, _ = _root(c, run_id)
    if "ticket_pause_requests" not in keys or "ticket_pause_requests" in nulls:
        raise ValueError("Missing Run Ticket pause request map")
    return bool(scheduling.load(c, run_id, "ticket_pause_requests", keys={str(number)}))


__all__ = ["FIELDS", "TABLES", "INDEXES", "load", "save"]
