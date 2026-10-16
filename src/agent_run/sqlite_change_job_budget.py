"""Relational budget events and exact Reviewer-result references."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_query_rows import bounded_query, integer_aggregate
from agent_run.sqlite_change_job_codec import material, presence, present, put_material

_EVENT_FIELDS = (
    "kind",
    "reason",
    "window",
    "generation",
    "used",
    "grant",
    "developments_used",
    "failure_head",
    "source_sha",
    "target_sha",
    "renewed",
)
_EVENT_COLUMNS = (
    "kind",
    "reason",
    "window",
    "generation",
    "used",
    "grant_amount",
    "developments_used",
    "failure_head",
    "source_sha",
    "target_sha",
    "renewed",
)
_ARTIFACT_SCALARS = ("reviewer_thread_id", "candidate_sha", "reviewed_base_sha", "expected_merge_tree")
_ARTIFACT_FIELDS = (*_ARTIFACT_SCALARS, "review_identity", "artifact", "material")


def record_list_bytes(c: sqlite3.Connection, run_id: str, job_id: str, budget: str) -> int:
    """Measure fixed events and material references belonging to one Budget."""
    total = 0
    for table, columns in (
        ("work_job_budget_events", ("list_name", "position", "presence", *_EVENT_COLUMNS)),
        ("work_job_review_artifacts", ("position", "presence", *_ARTIFACT_SCALARS,
                                       "review_identity_id", "artifact_id", "material_id")),
    ):
        lengths = "+".join(f"COALESCE(LENGTH(CAST({key} AS BLOB)),0)+16" for key in columns)
        total += integer_aggregate(c, f"SELECT COALESCE(SUM({lengths}),0) FROM {table} "
                               "WHERE run_id=? AND job_id=? AND budget_kind=?",
                                  (run_id, job_id, budget))[0]
    return total


def save_list(
    c: sqlite3.Connection, run_id: str, job_id: str, budget: str, field: str, value: Any, *, kind: str, owner_id: str
) -> None:
    if kind == "EVENTS":
        _save_events(c, run_id, job_id, budget, field, value)
    else:
        _save_artifacts(c, run_id, job_id, budget, value, owner_id=owner_id)


def load_list(
    c: sqlite3.Connection, run_id: str, job_id: str, budget: str, field: str, *, kind: str, owner_id: str
) -> list[Any]:
    if kind == "EVENTS":
        return _load_events(c, run_id, job_id, budget, field)
    return _load_artifacts(c, run_id, job_id, budget, owner_id=owner_id)


def _save_events(c: sqlite3.Connection, run_id: str, job_id: str, budget: str, field: str, value: Any) -> None:
    c.execute(
        "DELETE FROM work_job_budget_events WHERE run_id=? AND job_id=? AND budget_kind=? AND list_name=?",
        (run_id, job_id, budget, field),
    )
    if value is None:
        return
    if not isinstance(value, list):
        raise ValueError(f"{budget}.{field} must be an array")
    for position, event in enumerate(value):
        if not isinstance(event, Mapping) or set(event) - set(_EVENT_FIELDS):
            raise ValueError(f"Unsupported {budget}.{field} event")
        values = [event.get(key) for key in _EVENT_FIELDS]
        if values[-1] is not None:
            values[-1] = int(values[-1])
        c.execute(
            "INSERT INTO work_job_budget_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, job_id, budget, field, position, presence(event, _EVENT_FIELDS), *values),
        )


def _load_events(c: sqlite3.Connection, run_id: str, job_id: str, budget: str, field: str) -> list[Any]:
    values = []
    for row in bounded_query(c, f"SELECT presence,{','.join(_EVENT_COLUMNS)} FROM work_job_budget_events WHERE run_id=? AND job_id=? AND budget_kind=? AND list_name=? ORDER BY position",
        (run_id, job_id, budget, field),
    ):
        keys = present(row[0], _EVENT_FIELDS)
        event = {key: value for key, value in zip(_EVENT_FIELDS, row[1:], strict=True) if key in keys}
        if "renewed" in event and event["renewed"] is not None:
            event["renewed"] = bool(event["renewed"])
        values.append(event)
    return values


def _save_artifacts(
    c: sqlite3.Connection, run_id: str, job_id: str, budget: str, value: Any, *, owner_id: str
) -> None:
    c.execute(
        "DELETE FROM work_job_review_artifacts WHERE run_id=? AND job_id=? AND budget_kind=?", (run_id, job_id, budget)
    )
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Reviewer materials must contain objects")
    for position, item in enumerate(value):
        keys = set(item) & set(_ARTIFACT_FIELDS)
        remainder = {key: part for key, part in item.items() if key not in _ARTIFACT_FIELDS}
        if remainder and "artifact" in item:
            raise ValueError(f"Unsupported Reviewer identity fields: {sorted(remainder)}")
        if remainder:
            keys.add("material")
        refs = []
        for key in ("review_identity", "artifact", "material"):
            part = remainder if key == "material" else item.get(key)
            # Role results use the same kind as the Job's view, so an exact
            # historical return remains the sole owner of its report body.
            name = "acceptance_artifact" if key == "artifact" else f"{budget}.review_artifacts.{key}"
            refs.append(
                put_material(c, run_id, owner_id, name, part, review_record=item if key == "artifact" else None)
                if part is not None and (key != "material" or remainder)
                else None
            )
        c.execute(
            "INSERT INTO work_job_review_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                job_id,
                budget,
                position,
                presence(keys, _ARTIFACT_FIELDS),
                *(item.get(key) for key in _ARTIFACT_SCALARS),
                *refs,
            ),
        )


def latest_review_artifact(c: sqlite3.Connection, run_id: str, job_id: str, *, owner_id: str) -> dict[str, Any] | None:
    """Read one exact result from the latest qualifying historical window."""
    values = _load_artifacts(c, run_id, job_id, "review_budget", owner_id=owner_id, latest_only=True)
    latest = values[0] if values else None
    return latest if isinstance(latest, dict) else None


def _load_artifacts(
    c: sqlite3.Connection, run_id: str, job_id: str, budget: str, *, owner_id: str, latest_only: bool = False
) -> list[Any]:
    values = []
    order = "ORDER BY position DESC LIMIT 1" if latest_only else "ORDER BY position"
    rows = bounded_query(c, "SELECT presence,reviewer_thread_id,candidate_sha,reviewed_base_sha,expected_merge_tree,review_identity_id,artifact_id,material_id FROM work_job_review_artifacts WHERE run_id=? AND job_id=? AND budget_kind=? "
        + order,
        (run_id, job_id, budget),
    )
    for row in rows:
        keys = present(row[0], _ARTIFACT_FIELDS)
        item = {key: value for key, value in zip(_ARTIFACT_SCALARS, row[1:5], strict=True) if key in keys}
        for key, reference in zip(_ARTIFACT_FIELDS[4:], row[5:], strict=True):
            if key not in keys:
                continue
            name = "acceptance_artifact" if key == "artifact" else f"{budget}.review_artifacts.{key}"
            part = material(c, run_id, owner_id, name, reference) if reference is not None else None
            if key == "material":
                item.update(part or {})
            else:
                item[key] = part
        values.append(item)
    return values
