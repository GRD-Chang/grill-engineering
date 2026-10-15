"""Immutable Ticket completion sets shared by Run review and publication bindings."""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_work_evidence import get_evidence, put_evidence


MAX_COMPLETION_BYTES = 4 * 1024 * 1024
_FIELDS = ("ticket_number", "integrated_sha", "effective_revision", "reviewed_base_sha",
           "reviewed_candidate_tree", "ci_repair_publication_receipt")
_COLUMNS = ",".join(_FIELDS[:-1])
TABLES = {
    "work_completion_sets": "CREATE TABLE work_completion_sets (run_id TEXT NOT NULL, set_id TEXT NOT NULL, "
    "item_count INTEGER NOT NULL, logical_bytes INTEGER NOT NULL, PRIMARY KEY(run_id,set_id), "
    "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "work_ticket_completions": "CREATE TABLE work_ticket_completions (run_id TEXT NOT NULL, set_id TEXT NOT NULL, "
    "position INTEGER NOT NULL, presence INTEGER NOT NULL, ticket_number INTEGER, integrated_sha TEXT, effective_revision TEXT, "
    "reviewed_base_sha TEXT, reviewed_candidate_tree TEXT, receipt_evidence_id TEXT, PRIMARY KEY(run_id,set_id,position), "
    "FOREIGN KEY(run_id,set_id) REFERENCES work_completion_sets(run_id,set_id) ON DELETE CASCADE, "
    "FOREIGN KEY(run_id,receipt_evidence_id) REFERENCES work_evidence(run_id,evidence_id))",
    "work_completion_refs": "CREATE TABLE work_completion_refs (run_id TEXT NOT NULL, owner_id TEXT NOT NULL, "
    "purpose TEXT NOT NULL, set_id TEXT NOT NULL, PRIMARY KEY(run_id,owner_id,purpose,set_id), "
    "FOREIGN KEY(run_id,set_id) REFERENCES work_completion_sets(run_id,set_id) ON DELETE CASCADE)",
}


def _validate(item: Any) -> None:
    if not isinstance(item, Mapping) or item.keys() - set(_FIELDS):
        raise ValueError("Invalid Ticket completion fields")
    for key, value in item.items():
        expected = int if key == "ticket_number" else dict if key == _FIELDS[-1] else str
        if value is not None and type(value) is not expected:
            raise ValueError(f"Invalid Ticket completion {key}")


def put_completions(
    c: sqlite3.Connection, run_id: str, owner_id: str, purpose: str, records: Any,
) -> str:
    if not isinstance(records, list):
        raise ValueError("Ticket completion records must be an array")
    for item in records:
        _validate(item)
    raw = encode_json(records).encode("utf-8")
    if len(raw) > MAX_COMPLETION_BYTES:
        raise ValueError("Ticket completion set exceeds byte limit")
    identity = hashlib.sha256(raw).hexdigest()
    existing = c.execute("SELECT 1 FROM work_completion_sets WHERE run_id=? AND set_id=?", (run_id, identity)).fetchone()
    if existing is None:
        c.execute("INSERT INTO work_completion_sets VALUES (?,?,?,?)", (run_id, identity, len(records), len(raw)))
        for position, item in enumerate(records):
            receipt = item.get(_FIELDS[-1])
            evidence = (put_evidence(c, run_id, "completion:" + identity, f"receipt:{position}", receipt)
                        if receipt is not None else None)
            mask = sum(1 << index for index, key in enumerate(_FIELDS) if key in item)
            c.execute("INSERT INTO work_ticket_completions VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (run_id, identity, position, mask, *(item.get(key) for key in _FIELDS[:-1]), evidence))
    elif _load(c, run_id, identity) != records:
        raise ValueError("Immutable Ticket completion set was changed")
    c.execute("INSERT OR IGNORE INTO work_completion_refs VALUES (?,?,?,?)", (run_id, owner_id, purpose, identity))
    return identity


def get_completions(
    c: sqlite3.Connection, run_id: str, owner_id: str, purpose: str, identity: str,
) -> list[dict[str, Any]]:
    if c.execute("SELECT 1 FROM work_completion_refs WHERE run_id=? AND owner_id=? AND purpose=? AND set_id=?",
                 (run_id, owner_id, purpose, identity)).fetchone() is None:
        raise ValueError("Missing owned Ticket completion reference")
    return _load(c, run_id, identity)


def _load(c: sqlite3.Connection, run_id: str, identity: str) -> list[dict[str, Any]]:
    header = c.execute("SELECT item_count,logical_bytes FROM work_completion_sets WHERE run_id=? AND set_id=?", (run_id, identity)).fetchone()
    if (header is None or any(type(value) is not int for value in header)
            or not 0 <= header[0] <= MAX_COMPLETION_BYTES // 2 or not 2 <= header[1] <= MAX_COMPLETION_BYTES):
        raise ValueError("Invalid Ticket completion set size")
    lengths = "+".join(f"COALESCE(LENGTH(CAST(t.{key} AS BLOB)),0)" for key in _FIELDS[:-1])
    count, actual = c.execute(
        f"SELECT COUNT(*),COALESCE(SUM({lengths}+COALESCE(e.value_bytes,0)),0) "
        "FROM work_ticket_completions t LEFT JOIN work_evidence e ON e.run_id=t.run_id AND e.evidence_id=t.receipt_evidence_id "
        "WHERE t.run_id=? AND t.set_id=?", (run_id, identity),
    ).fetchone()
    if count != header[0] or actual > MAX_COMPLETION_BYTES:
        raise ValueError("Invalid Ticket completion set size")
    result = []
    for ordinal, row in enumerate(c.execute(
        f"SELECT position,presence,{_COLUMNS},receipt_evidence_id FROM work_ticket_completions "
        "WHERE run_id=? AND set_id=? ORDER BY position", (run_id, identity),
    )):
        position, mask, *values = row
        if position != ordinal or type(mask) is not int or not 0 <= mask < 1 << len(_FIELDS):
            raise ValueError("Invalid Ticket completion position or presence")
        item = {key: value for index, (key, value) in enumerate(zip(_FIELDS, values, strict=True)) if mask & (1 << index)}
        if any(value is not None and not mask & (1 << index) for index, value in enumerate(values)):
            raise ValueError("Unexpected Ticket completion column")
        if values[-1] is not None:
            item[_FIELDS[-1]] = get_evidence(c, run_id, values[-1], owner_id="completion:" + identity, kind=f"receipt:{position}")
        _validate(item)
        result.append(item)
    raw = encode_json(result).encode("utf-8")
    if len(raw) != header[1] or hashlib.sha256(raw).hexdigest() != identity:
        raise ValueError("Immutable Ticket completion set was changed")
    return result
