"""Immutable expected metadata; actual indexes are read in every SQL snapshot."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass



@dataclass(frozen=True)
class IndexFacts:
    run_id: str
    size: tuple[int, int]
    references: frozenset[tuple[str, str]]


def check_facts(connection: sqlite3.Connection, run_id: str, expected: IndexFacts, *, max_bytes: int) -> None:
    """Keep SQLite values uncoerced so BLOBs cannot imitate expected text."""
    if expected.run_id != run_id:
        raise ValueError("SQLite Run index facts belong to another Run")
    if connection.execute("SELECT logical_bytes,canonical_count FROM run_sizes WHERE run_id=?", (run_id,)).fetchone() != expected.size:
        raise ValueError("Invalid SQLite Run size metadata")
    actual_references = frozenset(connection.execute(
        "SELECT object_path,material_id FROM run_object_material_refs WHERE run_id=?", (run_id,),
    ))
    if actual_references != expected.references:
        raise ValueError("Invalid SQLite Run object material references")
