"""Immutable expected metadata; actual indexes are read in every SQL snapshot."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_run import sqlite_job_history


@dataclass(frozen=True)
class IndexFacts:
    run_id: str
    size: tuple[int, int]
    history: tuple[tuple[Any, ...], ...]
    references: frozenset[tuple[str, str]]
    job_sizes: Mapping[str, tuple[int, int]]
    attempts: tuple[tuple[Any, ...], ...]


def check_facts(connection: sqlite3.Connection, run_id: str, expected: IndexFacts, *, max_bytes: int) -> None:
    """Keep SQLite values uncoerced so BLOBs cannot imitate expected text."""
    if expected.run_id != run_id:
        raise ValueError("SQLite Run index facts belong to another Run")
    if connection.execute("SELECT logical_bytes,canonical_count FROM run_sizes WHERE run_id=?", (run_id,)).fetchone() != expected.size:
        raise ValueError("Invalid SQLite Run size metadata")
    actual_history = tuple(connection.execute(
        "SELECT run_id,position,ticket_number,work_subject,started_at,resume_id,logical_bytes,detailed "
        "FROM run_history WHERE run_id=? ORDER BY position", (run_id,),
    ))
    actual_references = frozenset(connection.execute(
        "SELECT object_path,material_id FROM run_object_material_refs WHERE run_id=?", (run_id,),
    ))
    actual_attempts = tuple(connection.execute(
        "SELECT object_path,parent_path,position,role,status,ordinal "
        "FROM run_job_attempts WHERE run_id=? ORDER BY object_path", (run_id,),
    ))
    if actual_history != expected.history:
        raise ValueError("Invalid SQLite Run history index")
    if actual_references != expected.references:
        raise ValueError("Invalid SQLite Run object material references")
    if sqlite_job_history.read_sizes(connection, run_id, max_bytes) != expected.job_sizes:
        raise ValueError("Invalid SQLite Run Job history size")
    if actual_attempts != expected.attempts:
        raise ValueError("Invalid SQLite Run Job Attempt index")
