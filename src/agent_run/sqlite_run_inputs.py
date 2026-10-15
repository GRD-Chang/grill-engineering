"""Immutable Parent requirements and graph structures with exact references."""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_change_job_codec import presence, present
from agent_run.sqlite_work_evidence import _json, get_evidence, put_evidence

_PARENT = frozenset({"number", "title", "body", "revision"})


def identity(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def owner(run_id: str) -> str:
    return "run:" + run_id


def input_version_for_revision(c: sqlite3.Connection, run_id: str, kind: str, revision: str) -> str | None:
    """Resolve a fixed version, never substitute a later current observation."""
    tables = {"parent": "run_parent_versions", "graph": "run_graph_versions", "ticket_graph": "run_graph_versions"}
    if kind not in tables:
        raise ValueError("Unknown Run input kind")
    rows = c.execute(f"SELECT version_id FROM {tables[kind]} WHERE run_id=? AND revision=? LIMIT 2",
                     (run_id, revision)).fetchall()
    if len(rows) > 1:
        raise ValueError("Ambiguous immutable Run input revision")
    return rows[0][0] if rows else None


def save_parent(c: sqlite3.Connection, run_id: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.keys() - _PARENT:
        raise ValueError("Invalid Run Parent input")
    number = value.get("number")
    if number is not None and (type(number) is not int or number < 1):
        raise ValueError("Invalid Run Parent number")
    current_number = c.execute("SELECT parent_number FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    if current_number is not None and number is not None and number != current_number:
        raise ValueError("Cannot change the Run Parent identity")
    if number is not None and current_number is None:
        c.execute("UPDATE runs SET parent_number=? WHERE run_id=?", (number, run_id))
    revision = value.get("revision")
    if revision is not None and not isinstance(revision, str):
        raise ValueError("Invalid Run Parent revision")
    refs = []
    for key in ("title", "body"):
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise ValueError("Invalid Run Parent requirement text")
        refs.append(put_evidence(c, run_id, owner(run_id), "parent." + key, item) if item is not None else None)
    mask = presence(value, _PARENT)
    version = identity((mask.hex(), revision, *refs))
    if revision is not None:
        previous = input_version_for_revision(c, run_id, "parent", revision)
        if previous is not None and previous != version:
            raise ValueError("One Parent revision identifies different immutable requirements")
    expected = (mask, revision, *refs)
    row = c.execute("SELECT presence,revision,title_id,body_id FROM run_parent_versions WHERE run_id=? AND version_id=?",
                    (run_id, version)).fetchone()
    if row is not None and row != expected:
        raise ValueError("Immutable Parent input was changed")
    if row is None:
        c.execute("INSERT INTO run_parent_versions VALUES (?,?,?,?,?,?)", (run_id, version, *expected))
    return version


def load_parent(c: sqlite3.Connection, run_id: str, version: str) -> dict[str, Any]:
    row = c.execute("SELECT presence,revision,title_id,body_id FROM run_parent_versions WHERE run_id=? AND version_id=?",
                    (run_id, version)).fetchone()
    if row is None or version != identity((row[0].hex(), *row[1:])):
        raise ValueError("Missing or invalid immutable Parent input")
    keys = present(row[0], _PARENT)
    result: dict[str, Any] = {}
    if "number" in keys:
        result["number"] = c.execute("SELECT parent_number FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    if "revision" in keys:
        result["revision"] = row[1]
    for key, evidence_id in zip(("title", "body"), row[2:], strict=True):
        if key in keys:
            result[key] = (get_evidence(c, run_id, evidence_id, owner_id=owner(run_id), kind="parent." + key)
                           if evidence_id is not None else None)
    return result
