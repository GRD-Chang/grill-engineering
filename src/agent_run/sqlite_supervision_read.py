"""Fresh bounded scalar facts for the empty Ticket supervision precheck."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_encoding import encode_json, scalar_value


_MATERIALIZED = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""

_SUPERVISION = f"""
WITH nodes AS {_MATERIALIZED}(
 SELECT object_path,kind,
 LENGTH(CAST(object_path AS BLOB))+LENGTH(CAST(value_json AS BLOB)) AS byte_length,
 CASE WHEN LENGTH(CAST(value_json AS BLOB))<=:aggregate_bytes THEN value_json END AS value_json
 FROM run_objects WHERE run_id=:run_id AND object_path IN ('[]',:status,:holder,:queue)
), sizes AS (
 SELECT COALESCE(SUM(byte_length),0) AS physical FROM nodes
)
SELECT
(SELECT json_group_array(json_array(type,name,sql)) FROM (
 SELECT type,name,sql FROM sqlite_master
 WHERE type NOT IN ('table','index') OR name NOT GLOB 'sqlite_*' ORDER BY type,name))=:schema_definitions
AND (SELECT COUNT(*) FROM schema_meta)=1
AND (SELECT value FROM schema_meta WHERE key='protocol')=:schema_protocol,
EXISTS(SELECT 1 FROM runs WHERE run_id=:run_id),
(SELECT logical_bytes FROM run_sizes WHERE run_id=:run_id),physical,
(SELECT kind FROM nodes WHERE object_path='[]'),
CASE WHEN physical<=:aggregate_bytes THEN (SELECT value_json FROM nodes WHERE object_path='[]') END,
(SELECT kind FROM nodes WHERE object_path=:status),
CASE WHEN physical<=:aggregate_bytes THEN (SELECT value_json FROM nodes WHERE object_path=:status) END,
(SELECT kind FROM nodes WHERE object_path=:holder),
CASE WHEN physical<=:aggregate_bytes THEN (SELECT value_json FROM nodes WHERE object_path=:holder) END,
(SELECT kind FROM nodes WHERE object_path=:queue),
CASE WHEN physical<=:aggregate_bytes THEN (SELECT value_json FROM nodes WHERE object_path=:queue) END,
EXISTS(SELECT 1 FROM run_objects WHERE run_id=:run_id AND object_path>=:status_low AND object_path<:status_high),
EXISTS(SELECT 1 FROM run_objects WHERE run_id=:run_id AND object_path>=:holder_low AND object_path<:holder_high),
EXISTS(SELECT 1 FROM run_objects WHERE run_id=:run_id AND object_path>=:queue_low AND object_path<:queue_high)
FROM sizes
"""


def read_supervision_work(
    connection: sqlite3.Connection, run_id: str, *, max_bytes: int,
    schema_parameters: Mapping[str, Any],
) -> bool | None:
    """None retains the original structural reader for unsupported sources."""
    parameters: dict[str, Any] = {**schema_parameters, "run_id": run_id,
                                  "aggregate_bytes": min(512 * 1024, 8 * max_bytes)}
    for name, field in (("status", "status"), ("holder", "ticket_integration_holder"),
                        ("queue", "ticket_integration_queue")):
        path = encode_json((field,))
        parameters.update({name: path, name + "_low": path[:-1] + ",", name + "_high": path[:-1] + "-"})
    try:
        row = connection.execute(_SUPERVISION, parameters).fetchone()
    except sqlite3.OperationalError as error:
        if any(reason in str(error) for reason in ("no such function", "no such table", "no such column")):
            return None
        raise
    assert row is not None
    (proved, found, size, physical, root_kind, root_value, status_kind, status_value,
     holder_kind, holder_value, queue_kind, queue_value, status_children, holder_children, queue_children) = row
    if not proved:
        return None
    if not found:
        return False
    if type(size) is not int or not 3 <= size <= max_bytes:
        raise ValueError("Invalid SQLite Run size metadata")
    if physical > 8 * max_bytes:
        raise ValueError(f"Run state exceeds {max_bytes} byte persistence limit")
    if physical > parameters["aggregate_bytes"]:
        return None
    if (root_kind, root_value) != ("map", "null"):
        raise ValueError("Invalid SQLite Run object root")
    if holder_kind != "scalar" or queue_kind != "list":
        raise ValueError("Invalid SQLite Ticket supervision object shape")
    if status_kind not in {"scalar", "material"}:
        raise ValueError("Invalid SQLite Ticket supervision status shape")
    if not all(isinstance(value, str) for value in (status_value, holder_value, queue_value)):
        return None
    try:
        status, holder, count = (scalar_value(value) for value in (status_value, holder_value, queue_value))
    except ValueError:
        return None
    if (not isinstance(status, str) or holder is not None and (type(holder) is not int or holder < 1)
            or type(count) is not int or not 0 <= count <= max_bytes):
        raise ValueError("Invalid SQLite Ticket supervision status or queue")
    if status_children or holder_children or count == 0 and queue_children:
        raise ValueError("Invalid SQLite Ticket supervision object children")
    return status_kind == "material" or status not in {"active", "ticket_delivery_pending"} or holder is not None or count != 0
