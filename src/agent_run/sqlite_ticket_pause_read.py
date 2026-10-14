"""One fresh, bounded SQL snapshot for an ordinary scalar Ticket Stop."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_encoding import encode_json


_MATERIALIZED = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""

_PAUSE = f"""
WITH nodes AS {_MATERIALIZED}(
 -- A planner may materialize this shared relation. Keep oversized source
 -- encodings out of it before either the aggregate or Python receives them.
 SELECT object_path,kind,
 LENGTH(CAST(object_path AS BLOB))+LENGTH(CAST(value_json AS BLOB)) AS byte_length,
 CASE WHEN LENGTH(CAST(value_json AS BLOB))<=:aggregate_bytes THEN value_json END AS value_json
 FROM run_objects
 WHERE run_id=:run_id AND object_path IN ('[]',:map_path,:target_path)
), sizes AS (
 SELECT COALESCE(SUM(byte_length),0) AS physical
 FROM nodes
)
SELECT
(SELECT json_group_array(json_array(type,name,sql)) FROM (
 SELECT type,name,sql FROM sqlite_master
 WHERE type NOT IN ('table','index') OR name NOT GLOB 'sqlite_*' ORDER BY type,name))=:schema_definitions
AND (SELECT COUNT(*) FROM schema_meta)=1
AND (SELECT value FROM schema_meta WHERE key='protocol')=:schema_protocol,
EXISTS(SELECT 1 FROM runs WHERE run_id=:run_id),
(SELECT logical_bytes FROM run_sizes WHERE run_id=:run_id), physical,
(SELECT kind FROM nodes WHERE object_path='[]'),
CASE WHEN physical<=:aggregate_bytes THEN (SELECT value_json FROM nodes WHERE object_path='[]') END,
(SELECT kind FROM nodes WHERE object_path=:map_path),
CASE WHEN physical<=:aggregate_bytes THEN (SELECT value_json FROM nodes WHERE object_path=:map_path) END,
EXISTS(SELECT 1 FROM nodes WHERE object_path=:target_path),
(SELECT kind FROM nodes WHERE object_path=:target_path),
CASE WHEN physical<=:aggregate_bytes THEN (SELECT value_json FROM nodes WHERE object_path=:target_path) END,
EXISTS(SELECT 1 FROM run_objects WHERE run_id=:run_id AND object_path>=:target_low AND object_path<:target_high)
FROM sizes
"""


def read_ticket_pause(
    connection: sqlite3.Connection, run_id: str, ticket_number: int, *, max_bytes: int,
    schema_parameters: Mapping[str, Any],
) -> bool | None:
    """Return None when the original structural/material reader is required."""
    target = encode_json(("ticket_pause_requests", str(ticket_number)))
    aggregate_bytes = min(512 * 1024, 8 * max_bytes)
    try:
        row = connection.execute(_PAUSE, {
            **schema_parameters, "run_id": run_id, "map_path": encode_json(("ticket_pause_requests",)),
            "target_path": target, "target_low": target[:-1] + ",", "target_high": target[:-1] + "-",
            "aggregate_bytes": aggregate_bytes,
        }).fetchone()
    except sqlite3.OperationalError as error:
        if any(reason in str(error) for reason in ("no such function", "no such table", "no such column")):
            return None
        raise
    assert row is not None
    proved, found, size, physical, root_kind, root_value, map_kind, map_value, present, kind, value, children = row
    if not proved:
        return None
    if not found:
        raise ValueError("Delivery Run disappeared at Ticket pause boundary")
    if type(size) is not int or not 3 <= size <= max_bytes:
        raise ValueError("Invalid SQLite Run size metadata")
    if physical > 8 * max_bytes:
        raise ValueError(f"Run state exceeds {max_bytes} byte persistence limit")
    if physical > aggregate_bytes:
        return None
    if (root_kind, root_value) != ("map", "null") or (map_kind, map_value) != ("map", "null"):
        raise ValueError("Invalid SQLite Ticket pause request map")
    if present and kind not in {"scalar", "material"}:
        raise ValueError("Invalid SQLite Ticket pause request value")
    if children:
        raise ValueError("Invalid SQLite Ticket pause request children")
    if not present:
        return False
    if kind == "material" or not isinstance(value, str):
        return None
    try:
        timestamp = json.loads(value)
    except ValueError:
        return None
    if isinstance(timestamp, dict | list):
        return None
    if not isinstance(timestamp, str):
        raise ValueError("Invalid SQLite Ticket pause request timestamp")
    return True
