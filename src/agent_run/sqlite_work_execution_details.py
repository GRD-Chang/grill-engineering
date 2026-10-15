"""Typed execution segments, recovery waits and retained timing summaries."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_work_execution_schema import (
    DETAIL_FIELDS, SUMMARY_FIELDS, TIMING_FIELDS, presence, present_fields,
    restore_scalars, scalar_values,
)

SUMMARY_SPECS = (
    ("execution_timing_summary", "timing_", TIMING_FIELDS),
    ("execution_segments_summary", "segments_summary_", SUMMARY_FIELDS),
    ("recovery_wait_intervals_summary", "waits_summary_", SUMMARY_FIELDS),
)
DETAIL_COLUMNS = (
    *(f"{field}_null" for field in DETAIL_FIELDS),
    *(column for _, prefix, fields in SUMMARY_SPECS
      for column in (prefix + "presence", *(prefix + key for key in fields))),
)


def summary_values(record: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in DETAIL_FIELDS:
        value = record.get(field)
        if value is not None and not isinstance(value, list):
            raise ValueError(f"Invalid Invocation {field}")
        result[field + "_null"] = int(field in record and value is None)
    for field, prefix, fields in SUMMARY_SPECS:
        value = record.get(field)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"Invalid Invocation {field}")
        result[prefix + "presence"] = None if value is None else presence(value, tuple(fields))
        result.update(zip((prefix + key for key in fields), scalar_values(value or {}, fields), strict=True))
    return result


def save_arrays(
    c: sqlite3.Connection, identity: tuple[str, str, str], record: Mapping[str, Any],
    before: Mapping[str, Any] | None,
) -> None:
    for field, table, fields in (
        ("execution_segments", "work_invocation_segments", {"started_at": str, "ended_at": str}),
        ("recovery_wait_intervals", "work_invocation_waits", {"kind": str, "started_at": str, "ended_at": str}),
    ):
        values = record.get(field)
        if before is not None and (field in before) == (field in record) and values == before.get(field):
            continue
        c.execute(f"DELETE FROM {table} WHERE run_id=? AND work_subject=? AND started_at=?", identity)
        if values is None:
            continue
        if not isinstance(values, list):
            raise ValueError(f"Invalid Invocation {field}")
        for position, entry in enumerate(values):
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid Invocation {field} entry")
            included = presence(entry, tuple(fields))
            row = (*identity, position, included, *scalar_values(entry, fields))
            c.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})", row)


def restore_details(
    c: sqlite3.Connection, identity: tuple[str, str, str], row: Mapping[str, Any],
    record: dict[str, Any], included: set[str],
) -> None:
    for field, table, fields, columns in (
        ("execution_segments", "work_invocation_segments", {"started_at": str, "ended_at": str}, "started,ended"),
        ("recovery_wait_intervals", "work_invocation_waits", {"kind": str, "started_at": str, "ended_at": str}, "kind,started,ended"),
    ):
        if field not in included:
            continue
        if row[field + "_null"]:
            record[field] = None
            continue
        values: list[dict[str, Any]] = []
        for position, mask, *scalars in c.execute(
            f"SELECT position,presence,{columns} FROM {table} WHERE run_id=? AND work_subject=? AND started_at=? ORDER BY position",
            identity,
        ):
            if position != len(values):
                raise ValueError("Invalid Invocation detail position")
            entry: dict[str, Any] = {}
            restore_scalars(entry, fields, tuple(scalars), present_fields(mask, tuple(fields)))
            values.append(entry)
        record[field] = values
    for summary_field, prefix, summary_fields in SUMMARY_SPECS:
        if summary_field not in included:
            continue
        mask = row[prefix + "presence"]
        if mask is None:
            record[summary_field] = None
        else:
            value: dict[str, Any] = {}
            restore_scalars(value, summary_fields, tuple(row[prefix + key] for key in summary_fields), present_fields(mask, tuple(summary_fields)))
            record[summary_field] = value
