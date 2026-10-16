"""Codecs for fixed Job schemas, preserving absent, null and empty values."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from typing import Any

from agent_run.sqlite_query_rows import bounded_query, integer_aggregate
from agent_run.sqlite_change_job_schema import (
    BOOLEAN_FIELDS, EVIDENCE_FIELDS, INPUT_BOUND_RECORDS, INTEGER_FIELDS, JOB_FIELDS, RECORDS, TEXT_FIELDS, THREAD_FIELDS,
    column, record_table,
)
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_mutations import _same
from agent_run.state_limits import MAX_CURRENT_BYTES

_CORE = sorted(TEXT_FIELDS | INTEGER_FIELDS | BOOLEAN_FIELDS | EVIDENCE_FIELDS |
               {"had_candidate", "had_acceptance", "had_publication"})


def presence(keys: Any, fields: Any) -> bytes:
    names = tuple(sorted(fields))
    selected = set(keys)
    if selected - set(names):
        raise ValueError(f"Unsupported Change Job fields: {sorted(selected - set(names))}")
    value = sum(1 << index for index, name in enumerate(names) if name in selected)
    return value.to_bytes((len(names) + 7) // 8, "little")


def present(mask: Any, fields: Any) -> set[str]:
    names = tuple(sorted(fields))
    if not isinstance(mask, bytes) or len(mask) != (len(names) + 7) // 8:
        raise ValueError("Invalid Change Job presence mask")
    bits = int.from_bytes(mask, "little")
    if bits >> len(names):
        raise ValueError("Invalid Change Job presence bits")
    return {name for index, name in enumerate(names) if bits & (1 << index)}


def put_material(c: sqlite3.Connection, run_id: str, owner_id: str, field: str, value: Any, *,
                 review_record: Mapping[str, Any] | None = None) -> str:
    if field == "ticket_completion_records":
        from agent_run.sqlite_ticket_completions import put_completions
        return put_completions(c, run_id, f"job:{owner_id}", field, value)
    from agent_run.sqlite_work_evidence import get_evidence, put_evidence, put_evidence_member, put_job_material
    from agent_run.sqlite_work_execution import find_result_reference
    query = "review_artifact" if review_record is not None else {
        "artifact": "publication", "repair_input_artifact": "acceptance_artifact",
        "unresolved_acceptance_artifact": "acceptance_artifact",
    }.get(field, field)
    if query != field:
        source = find_result_reference(c, run_id, owner_id, query, review_record if review_record is not None else value)
        if source is None and field in {"repair_input_artifact", "unresolved_acceptance_artifact"}:
            parent = bounded_query(c, "SELECT budget_owner_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, owner_id)).fetchone()
            if parent is not None and parent[0] != owner_id:
                source = find_result_reference(c, run_id, parent[0], query, review_record if review_record is not None else value)
        if source is not None:
            evidence_id, original_owner, original_kind, member = source
            get_evidence(c, run_id, evidence_id, owner_id=original_owner, kind=original_kind)
            return put_evidence_member(c, run_id, f"job:{owner_id}", field, evidence_id, member)
        if review_record is not None:
            return put_evidence(c, run_id, f"job:{owner_id}", field, value)
    return put_job_material(c, run_id, owner_id, field, value)


def material(c: sqlite3.Connection, run_id: str, owner_id: str, field: str, identity: str) -> Any:
    if field == "ticket_completion_records":
        from agent_run.sqlite_ticket_completions import get_completions
        return get_completions(c, run_id, f"job:{owner_id}", field, identity)
    from agent_run.sqlite_work_evidence import get_evidence
    return get_evidence(c, run_id, identity, owner_id=f"job:{owner_id}", kind=field)


def history_rows(c: sqlite3.Connection, run_id: str, job_id: str, field: str, *,
                 owner_id: str) -> Iterator[tuple[str | None, str | None, int]]:
    """Validate archive lengths from owned headers without loading report bodies."""
    from agent_run.sqlite_work_evidence import MAX_EVIDENCE_BYTES
    rows = bounded_query(c, "SELECT a.position,a.evidence_id,a.thread_id,a.logical_bytes,e.owner_id,e.kind,e.value_bytes "
        "FROM work_job_archives a LEFT JOIN work_evidence e USING(run_id,evidence_id) "
        "WHERE a.run_id=? AND a.job_id=? AND a.history_kind=? ORDER BY a.position", (run_id, job_id, field))
    for ordinal, row in enumerate(rows):
        position, evidence_id, thread_id, size, owner, kind, actual_size = row
        if type(position) is not int or position != ordinal:
            raise ValueError("Invalid Change Job history position")
        if field in THREAD_FIELDS:
            if evidence_id is not None or type(thread_id) is not str:
                raise ValueError("Invalid Change Job Thread history identity")
            actual_size = len(encode_json(thread_id).encode("utf-8"))
        elif (type(evidence_id) is not str or thread_id is not None or owner != f"job:{owner_id}" or kind != field
              or type(actual_size) is not int or not 0 <= actual_size <= MAX_EVIDENCE_BYTES):
            raise ValueError("Invalid Change Job history evidence")
        if type(size) is not int or size != actual_size:
            raise ValueError("Invalid Change Job history size")
        yield evidence_id, thread_id, size


def _typed(value: Any, kind: str, field: str) -> Any:
    if value is None:
        return None
    accepted = {"TEXT": (str,), "INTEGER": (int,), "BOOLEAN": (bool,), "REAL": (int, float), "IDENTITY": (str, int)}
    if type(value) not in accepted[kind]:
        raise ValueError(f"Change Job {field} must be {kind.lower()} or null")
    return int(value) if kind == "BOOLEAN" else value


def check_core_size(c: sqlite3.Connection, run_id: str, job_id: str) -> None:
    """Admit the complete current Job, including its explicit string lists.

    Length aggregation stays inside SQLite; only the selected fields are
    subsequently transferred and charged to the caller's shared query budget.
    """
    columns = [column(key) for key in _CORE] + [
        "presence", "stage", "evidence_owner_id", "budget_owner_id", "repair_job_id", "cycle_evidence_id", "revision",
    ]
    lengths = "+".join(f"COALESCE(LENGTH(CAST(j.{name} AS BLOB)),0)+16" for name in columns)
    row = integer_aggregate(c,
        f"SELECT {lengths}+COALESCE((SELECT SUM(LENGTH(CAST(s.text_value AS BLOB))+"
        "LENGTH(CAST(s.list_name AS BLOB))+48) FROM work_job_record_strings s "
        "WHERE s.run_id=j.run_id AND s.job_id=j.job_id AND s.record_kind='job'),0) "
        "FROM work_jobs j WHERE j.run_id=? AND j.job_id=?", (run_id, job_id),
    )
    if row is not None and (type(row[0]) is not int or row[0] > MAX_CURRENT_BYTES):
        raise ValueError(f"Change Job current record exceeds {MAX_CURRENT_BYTES} bytes")


def save_core(c: sqlite3.Connection, run_id: str, job_id: str, job: Mapping[str, Any], *,
              owner_id: str, stage: Any, budget_owner_id: str | None, repair_job_id: str | None,
              cycle_evidence_id: str | None = None, changed: set[str] | None = None) -> None:
    selected = [key for key in _CORE if changed is None or key in changed]
    values = []
    for key in selected:
        value = job.get(key)
        if key in EVIDENCE_FIELDS:
            review = None
            if key in {"acceptance_artifact", "repair_input_artifact", "unresolved_acceptance_artifact"}:
                for record_name in ("acceptance_record", "previous_parent_acceptance_record"):
                    record = job.get(record_name)
                    if isinstance(record, Mapping) and _same(record.get("artifact"), value) and isinstance(record.get("reviewer_thread_id"), str):
                        review = {"artifact": value, "reviewer_thread_id": record["reviewer_thread_id"]}
                        break
            value = put_material(c, run_id, owner_id, key, value, review_record=review) if value is not None else None
        else:
            kind = "BOOLEAN" if key in BOOLEAN_FIELDS or key.startswith("had_") else "INTEGER" if key in INTEGER_FIELDS else "TEXT"
            value = _typed(value, kind, key)
        values.append(value)
    assignments = "".join(f"{column(key)}=?," for key in selected)
    c.execute(f"UPDATE work_jobs SET {assignments}stage=?,presence=?,budget_owner_id=?,repair_job_id=?,cycle_evidence_id=?,revision=revision+1 "
              "WHERE run_id=? AND job_id=?", (*values, stage, presence(job, JOB_FIELDS), budget_owner_id, repair_job_id,
                                             cycle_evidence_id, run_id, job_id))
    check_core_size(c, run_id, job_id)


def load_core(c: sqlite3.Connection, run_id: str, job_id: str, *, fields: set[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    check_core_size(c, run_id, job_id)
    selected = [key for key in _CORE if fields is None or key in fields]
    columns = "".join("," + column(key) for key in selected)
    row = bounded_query(c, f"SELECT presence,stage,evidence_owner_id,budget_owner_id,repair_job_id,cycle_evidence_id,revision{columns} "
                    "FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    if row is None:
        raise ValueError("Missing Change Job identity")
    keys = present(row[0], JOB_FIELDS)
    metadata = dict(zip(("presence", "stage", "owner_id", "budget_owner_id", "repair_job_id", "cycle_evidence_id", "revision"), row[:7], strict=True))
    metadata["keys"] = keys
    job: dict[str, Any] = {}
    for key, value in zip(selected, row[7:], strict=True):
        if key not in keys:
            continue
        if key in EVIDENCE_FIELDS and value is not None:
            value = material(c, run_id, metadata["owner_id"], key, value)
        elif key in BOOLEAN_FIELDS or key.startswith("had_"):
            if value is not None and value not in {0, 1}:
                raise ValueError("Invalid Change Job boolean")
            value = bool(value) if value is not None else None
        job[key] = value
    if "phase" in keys:
        job["phase"] = metadata["stage"]
    return job, metadata


def _shape(schema: Mapping[str, str]) -> set[str]:
    return {part for path in schema for part in (path, *[path.rsplit(".", index)[0] for index in range(1, path.count(".") + 1)])}


def _flatten(value: Mapping[str, Any], schema: Mapping[str, str], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    allowed = _shape(schema)
    for key, item in value.items():
        path = prefix + key
        if path not in allowed:
            raise ValueError(f"Unsupported Change Job record field: {path}")
        result[path] = item
        if path not in schema:
            if item is not None:
                if not isinstance(item, Mapping):
                    raise ValueError(f"Change Job {path} must be an object or null")
                result.update(_flatten(item, schema, path + "."))
    return result


def _assign(value: dict[str, Any], path: str, item: Any) -> None:
    keys = path.split(".")
    target = value
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = item


def _input_versions(c: sqlite3.Connection, run_id: str, field: str, record: Mapping[str, Any]) -> dict[str, str | None]:
    if field not in INPUT_BOUND_RECORDS:
        return {}
    # Direct Job-layer tests and small storage consumers can omit Run inputs.
    # A real Run supplies them before saving its review/publication relations.
    has_inputs = bounded_query(c, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_root'").fetchone()
    boundary = record.get("acceptance_boundary", {}) if field == "merge_intent" else record
    result: dict[str, str | None] = {}
    for kind, key in (("parent", "parent_revision"), ("graph", "ticket_graph_revision")):
        revision = boundary.get(key) if isinstance(boundary, Mapping) else None
        identity = None
        if has_inputs and isinstance(revision, str):
            from agent_run.sqlite_run_inputs import input_version_for_revision
            identity = input_version_for_revision(c, run_id, kind, revision)
            table = "run_parent_versions" if kind == "parent" else "run_graph_versions"
            if identity is None and bounded_query(c, f"SELECT 1 FROM {table} WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
                raise ValueError(f"Missing immutable {kind} input version")
        result[f"{kind}_version_id"] = identity
    return result


def _check_record_size(c: sqlite3.Connection, run_id: str, job_id: str, field: str, columns: list[str]) -> bool:
    from agent_run.sqlite_change_job_budget import record_list_bytes

    lengths = "+".join(f"COALESCE(LENGTH(CAST(r.{name} AS BLOB)),0)+16" for name in ["presence", "nulls", *columns])
    size = integer_aggregate(c, f"SELECT {lengths}+COALESCE((SELECT SUM(LENGTH(CAST(s.text_value AS BLOB))+16) "
        "FROM work_job_record_strings s WHERE s.run_id=r.run_id AND s.job_id=r.job_id AND s.record_kind=?),0) "
        f"FROM {record_table(field)} r WHERE r.run_id=? AND r.job_id=?", (field, run_id, job_id),
    )
    if size is None:
        return False
    children = record_list_bytes(c, run_id, job_id, field) if any(
        kind in {"EVENTS", "ARTIFACTS"} for kind in RECORDS[field].values()
    ) else 0
    if type(size[0]) is not int or size[0] + children > MAX_CURRENT_BYTES:
        raise ValueError("Change Job record exceeds byte limit")
    return True


def save_record(c: sqlite3.Connection, run_id: str, job_id: str, field: str, value: Any, *, owner_id: str) -> None:
    schema = RECORDS[field]
    table = record_table(field)
    c.execute(f"DELETE FROM {table} WHERE run_id=? AND job_id=?", (run_id, job_id))
    c.execute("DELETE FROM work_job_record_strings WHERE run_id=? AND job_id=? AND record_kind=?", (run_id, job_id, field))
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"Change Job {field} must be an object or null")
    flat = _flatten(value, schema)
    columns, encoded = [], []
    string_bytes = 0
    for path, kind in schema.items():
        item = flat.get(path)
        if kind in {"ARTIFACTS", "EVENTS"}:
            from agent_run.sqlite_change_job_budget import save_list
            save_list(c, run_id, job_id, field, path, item, kind=kind, owner_id=owner_id)
            continue
        if kind == "STRINGS":
            if item is not None:
                if not isinstance(item, list) or any(type(text) is not str for text in item):
                    raise ValueError(f"Change Job {field}.{path} must contain strings")
                string_bytes += sum(len(text.encode("utf-8")) + 16 for text in item)
                if string_bytes > MAX_CURRENT_BYTES:
                    raise ValueError("Change Job record exceeds byte limit")
                c.executemany("INSERT INTO work_job_record_strings VALUES (?,?,?,?,?,?)",
                              ((run_id, job_id, field, path, position, text) for position, text in enumerate(item)))
            continue
        if kind == "COMPLETIONS":
            from agent_run.sqlite_ticket_completions import put_completions
            item = put_completions(c, run_id, f"job:{owner_id}", f"{field}.{path}", item) if item is not None else None
        elif kind == "EVIDENCE":
            review = value if path == "artifact" and "reviewer_thread_id" in value else None
            item = put_material(c, run_id, owner_id, f"{field}.{path}", item, review_record=review) if item is not None else None
        else:
            item = _typed(item, kind, f"{field}.{path}")
        columns.append(column(path))
        encoded.append(item)
    versions = _input_versions(c, run_id, field, value)
    columns.extend(versions)
    encoded.extend(versions.values())
    if string_bytes + sum(len(item.encode()) if isinstance(item, str) else 16 for item in encoded) > MAX_CURRENT_BYTES:
        raise ValueError(f"Change Job {field} record exceeds {MAX_CURRENT_BYTES} bytes")
    c.execute(f"INSERT INTO {table} (run_id,job_id,presence,nulls,{','.join(columns)}) VALUES ({','.join('?' for _ in range(len(columns) + 4))})",
              (run_id, job_id, presence(flat, _shape(schema)), presence((key for key, item in flat.items() if item is None), _shape(schema)), *encoded))
    _check_record_size(c, run_id, job_id, field, columns)


def load_record(c: sqlite3.Connection, run_id: str, job_id: str, field: str, *, owner_id: str) -> Any:
    schema = RECORDS[field]
    columns = [column(path) for path, kind in schema.items() if kind not in {"STRINGS", "ARTIFACTS", "EVENTS"}]
    inputs = ",parent_version_id,graph_version_id" if field in INPUT_BOUND_RECORDS else ""
    if not _check_record_size(c, run_id, job_id, field, columns + (["parent_version_id", "graph_version_id"] if inputs else [])):
        return None
    row = bounded_query(c, f"SELECT presence,nulls,{','.join(columns)}{inputs} FROM {record_table(field)} WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    if row is None:
        return None
    keys = present(row[0], _shape(schema))
    nulls = present(row[1], _shape(schema))
    if not nulls <= keys:
        raise ValueError("Invalid Change Job record null fields")
    scalars = iter(row[2:])
    values: dict[str, Any] = {}
    for path in sorted(keys - schema.keys(), key=lambda key: key.count(".")):
        _assign(values, path, None if path in nulls else {})
    for path, kind in schema.items():
        item = next(scalars) if kind not in {"STRINGS", "ARTIFACTS", "EVENTS"} else None
        if path not in keys:
            continue
        if path in nulls:
            _assign(values, path, None)
            continue
        if kind == "STRINGS":
            item = []
            for position, text in bounded_query(c, "SELECT position,text_value FROM work_job_record_strings WHERE run_id=? AND job_id=? AND record_kind=? AND list_name=? ORDER BY position", (run_id, job_id, field, path)):
                if type(position) is not int or position != len(item) or not isinstance(text, str):
                    raise ValueError("Invalid Change Job record strings")
                item.append(text)
        elif kind in {"ARTIFACTS", "EVENTS"}:
            from agent_run.sqlite_change_job_budget import load_list
            item = load_list(c, run_id, job_id, field, path, kind=kind, owner_id=owner_id)
        elif kind == "COMPLETIONS" and item is not None:
            from agent_run.sqlite_ticket_completions import get_completions
            item = get_completions(c, run_id, f"job:{owner_id}", f"{field}.{path}", item)
        elif kind == "EVIDENCE" and item is not None:
            item = material(c, run_id, owner_id, f"{field}.{path}", item)
        elif kind == "BOOLEAN":
            if item is not None and (type(item) is not int or item not in (0, 1)):
                raise ValueError("Invalid Change Job record boolean")
            item = bool(item) if item is not None else None
        elif kind not in {"EVIDENCE", "COMPLETIONS", "STRINGS", "ARTIFACTS", "EVENTS"}:
            item = _typed(item, kind, f"{field}.{path}")
        _assign(values, path, item)
    if inputs and tuple(_input_versions(c, run_id, field, values).values()) != row[-2:]:
        raise ValueError("Run evidence does not match its immutable input version")
    return values
