"""Owned, immutable evidence versions for work records.

Only original reports and fixed inputs use JSON. Mutable work identities,
progress, gates and counters live in their domain relations. A consumed field
can reference a member of an original Agent return without saving its body a
second time. All functions join the caller's existing SQLite transaction.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from agent_run.state_limits import consume_query_bytes, consume_query_rows
from agent_run.sqlite_query_rows import bounded_query, bounded_rows


MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
RETURN_MEMBERS = frozenset({"summary", "artifact", "human_blockers", "thread_id", "publication_fields"})
TABLES = {
    "work_evidence_materials": "CREATE TABLE work_evidence_materials ("
    "material_id TEXT NOT NULL PRIMARY KEY, body TEXT NOT NULL, byte_length INTEGER NOT NULL "
    "CHECK(byte_length >= 0))",
    "work_evidence": "CREATE TABLE work_evidence ("
    "run_id TEXT NOT NULL, evidence_id TEXT NOT NULL, owner_id TEXT NOT NULL, kind TEXT NOT NULL, "
    "version INTEGER NOT NULL CHECK(version > 0), value_digest TEXT NOT NULL, "
    "value_bytes INTEGER NOT NULL CHECK(value_bytes >= 0), material_id TEXT, "
    "source_evidence_id TEXT, source_member TEXT, PRIMARY KEY(run_id,evidence_id), "
    "UNIQUE(run_id,owner_id,kind,version), "
    "CHECK((material_id IS NOT NULL AND source_evidence_id IS NULL AND source_member IS NULL) "
    "OR (material_id IS NULL AND source_evidence_id IS NOT NULL AND source_member IS NOT NULL)), "
    "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE, "
    "FOREIGN KEY(material_id) REFERENCES work_evidence_materials(material_id), "
    "FOREIGN KEY(run_id,source_evidence_id) REFERENCES work_evidence(run_id,evidence_id))",
}
INDEXES = {
    "work_evidence_content": "CREATE INDEX work_evidence_content ON "
    "work_evidence(run_id,owner_id,kind,value_digest)",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _encoded(value: Any) -> tuple[str, str, int]:
    body = _json(value)
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_EVIDENCE_BYTES:
        raise ValueError(f"Work evidence exceeds {MAX_EVIDENCE_BYTES} byte limit")
    return body, hashlib.sha256(encoded).hexdigest(), len(encoded)


def _identity(owner: str, kind: str, digest: str, source: str | None, member: str | None) -> str:
    return hashlib.sha256(_json((owner, kind, digest, source, member)).encode("utf-8")).hexdigest()


def _insert(
    connection: sqlite3.Connection, run_id: str, owner: str, kind: str,
    digest: str, size: int, *, material: str | None = None,
    source: str | None = None, member: str | None = None,
) -> str:
    if not owner or not kind:
        raise ValueError("Work evidence requires an owner and purpose")
    evidence_id = _identity(owner, kind, digest, source, member)
    previous = bounded_query(connection,
        "SELECT evidence_id,value_digest,value_bytes,material_id,source_evidence_id,source_member "
        "FROM work_evidence WHERE run_id=? AND evidence_id=?", (run_id, evidence_id),
    ).fetchone()
    expected = (evidence_id, digest, size, material, source, member)
    if previous is not None:
        if previous != expected:
            raise ValueError("Immutable work evidence was changed")
        return evidence_id
    version = bounded_query(connection,
        "SELECT COALESCE(MAX(version),0)+1 FROM work_evidence WHERE run_id=? AND owner_id=? AND kind=?",
        (run_id, owner, kind),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO work_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, evidence_id, owner, kind, version, digest, size, material, source, member),
    )
    return evidence_id


def put_evidence(
    connection: sqlite3.Connection, run_id: str, owner_id: str, kind: str, payload: Any,
) -> str:
    body, digest, size = _encoded(payload)
    previous = bounded_query(connection,
        "SELECT typeof(body),byte_length,LENGTH(CAST(body AS BLOB)) "
        "FROM work_evidence_materials WHERE material_id=?", (digest,),
    ).fetchone()
    if previous is None:
        connection.execute("INSERT INTO work_evidence_materials VALUES (?,?,?)", (digest, body, size))
    else:
        if previous != ("text", size, size):
            raise ValueError("Immutable work evidence material was changed")
        consume_query_rows()
        consume_query_bytes(size)
        stored = connection.execute("SELECT body FROM work_evidence_materials WHERE material_id=?", (digest,)).fetchone()[0]
        if stored != body:
            raise ValueError("Immutable work evidence material was changed")
    return _insert(connection, run_id, owner_id, kind, digest, size, material=digest)


def evidence_size(connection: sqlite3.Connection, run_id: str, evidence_id: str) -> int:
    row = bounded_query(connection,
        "SELECT value_bytes FROM work_evidence WHERE run_id=? AND evidence_id=?", (run_id, evidence_id),
    ).fetchone()
    if row is None or type(row[0]) is not int or not 0 <= row[0] <= MAX_EVIDENCE_BYTES:
        raise ValueError("Missing or invalid work evidence size")
    return int(row[0])


def get_evidence(
    connection: sqlite3.Connection, run_id: str, evidence_id: str,
    *, owner_id: str | None = None, kind: str | None = None,
) -> Any:
    return _get(connection, run_id, evidence_id, owner_id=owner_id, kind=kind, visited=frozenset())


def _return_member(original: Any, member: str) -> Any:
    if not isinstance(original, dict):
        raise ValueError("Missing Agent return member")
    if member == "publication_fields":
        artifact = original.get("artifact")
        fields = ("commit_message", "pr_title", "pr_body_markdown")
        if not isinstance(artifact, dict) or any(key not in artifact for key in fields):
            raise ValueError("Missing original Publication narrative")
        return {key: artifact[key] for key in fields}
    if member not in original:
        raise ValueError("Missing Agent return member")
    return original[member]


def _get(
    connection: sqlite3.Connection, run_id: str, evidence_id: str, *,
    owner_id: str | None, kind: str | None, visited: frozenset[str],
) -> Any:
    if evidence_id in visited or len(visited) >= 8:
        raise ValueError("Invalid work evidence reference cycle")
    # Identity and actual material lengths belong to the same caller snapshot.
    # Read their narrow headers together; the body remains behind both the
    # ownership/length checks and the query-budget admission below.
    columns = ("owner_id", "kind", "version", "value_digest", "value_bytes", "material_id",
               "source_evidence_id", "source_member", "material_type", "material_bytes", "actual_bytes")
    row = next(bounded_rows(connection,
        "SELECT e.owner_id,e.kind,e.version,e.value_digest,e.value_bytes,e.material_id,"
        "e.source_evidence_id,e.source_member,typeof(m.body) AS material_type,"
        "m.byte_length AS material_bytes,LENGTH(CAST(m.body AS BLOB)) AS actual_bytes "
        "FROM work_evidence e LEFT JOIN work_evidence_materials m ON m.material_id=e.material_id "
        "WHERE e.run_id=? AND e.evidence_id=?", (run_id, evidence_id), columns,
    ), None)
    if row is None:
        raise ValueError("Missing owned work evidence")
    owner, purpose, version, digest, size, material, source, member = row[:8]
    if (not isinstance(owner, str) or not isinstance(purpose, str)
            or owner_id is not None and owner != owner_id or kind is not None and purpose != kind
            or type(version) is not int or version < 1
            or type(size) is not int or not 0 <= size <= MAX_EVIDENCE_BYTES
            or evidence_id != _identity(owner, purpose, digest, source, member)):
        raise ValueError("Invalid work evidence identity, owner or version")
    if source is not None:
        if material is not None or member not in RETURN_MEMBERS | {"response"}:
            raise ValueError("Invalid Agent return member reference")
        purpose = "human_response_history" if member == "response" else "returned_result"
        original = _get(connection, run_id, source, owner_id=None, kind=purpose, visited=visited | {evidence_id})
        value = _human_response(original) if member == "response" else _return_member(original, member)
        _, actual_digest, actual_size = _encoded(value)
        if (actual_digest, actual_size) != (digest, size):
            raise ValueError("Invalid Agent return member evidence")
        return value
    if material != digest or member is not None:
        raise ValueError("Invalid work evidence material reference")
    if row[8:] != ("text", size, size):
        raise ValueError("Missing or invalid work evidence byte length")
    # The join returns two stored headers in one SQL row. Admit the material
    # header and body in addition to the already metered owner header.
    consume_query_rows(2)
    consume_query_bytes(size)
    body = connection.execute("SELECT body FROM work_evidence_materials WHERE material_id=?", (material,)).fetchone()[0]
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != digest:
        raise ValueError("Invalid work evidence material digest")
    return json.loads(body)


def put_evidence_member(
    connection: sqlite3.Connection, run_id: str, owner_id: str, kind: str,
    source_evidence_id: str, source_member: tuple[str, ...],
) -> str:
    if len(source_member) != 1 or source_member[0] not in RETURN_MEMBERS:
        raise ValueError("Unsupported Agent return member reference")
    original = get_evidence(connection, run_id, source_evidence_id, kind="returned_result")
    member = source_member[0]
    _, digest, size = _encoded(_return_member(original, member))
    return _insert(connection, run_id, owner_id, kind, digest, size, source=source_evidence_id, member=member)


def put_job_material(
    connection: sqlite3.Connection, run_id: str, job_id: str, field: str, value: Any,
) -> str:
    from agent_run.sqlite_work_execution import find_result_reference

    if field == "current_human_response":
        _, digest, _ = _encoded(value)
        retained = bounded_query(connection,
            "SELECT evidence_id FROM work_evidence WHERE run_id=? AND owner_id=? AND kind=? "
            "AND value_digest=? AND source_member='response' ORDER BY version DESC LIMIT 1",
            (run_id, "job:" + job_id, field, digest),
        ).fetchone()
        if retained is not None:
            if get_evidence(connection, run_id, retained[0], owner_id="job:" + job_id, kind=field) != value:
                raise ValueError("Immutable human response was changed")
            return str(retained[0])
        response = bounded_query(connection,
            "SELECT a.evidence_id FROM work_job_archives a JOIN work_jobs j USING(run_id,job_id) "
            "WHERE a.run_id=? AND a.job_id=? AND a.history_kind='human_response_history' "
            "ORDER BY a.position DESC LIMIT 1", (run_id, job_id),
        ).fetchone()
        if response is not None:
            expected_owner = "job:" + job_id
            owner_row = bounded_query(connection,
                "SELECT evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id),
            ).fetchone()
            if owner_row is not None:
                expected_owner = "job:" + owner_row[0]
            original = get_evidence(connection, run_id, response[0], owner_id=expected_owner,
                                    kind="human_response_history")
            if _human_response(original) == value:
                return put_human_response_member(connection, run_id, "job:" + job_id,
                                                 response[0], source_owner_id=expected_owner,
                                                 kind=field)
    source = find_result_reference(connection, run_id, job_id, field, value)
    if source is not None:
        evidence_id, owner, kind, member = source
        get_evidence(connection, run_id, evidence_id, owner_id=owner, kind=kind)
        return put_evidence_member(connection, run_id, "job:" + job_id, field, evidence_id, member)
    return put_evidence(connection, run_id, "job:" + job_id, field, value)


def _human_response(original: Any) -> str:
    if (not isinstance(original, dict) or not isinstance(original.get("response"), str)
            or not original["response"].strip()):
        raise ValueError("Missing original human response")
    return str(original["response"])


def put_human_response_member(
    connection: sqlite3.Connection, run_id: str, owner_id: str, source_evidence_id: str,
    *, source_owner_id: str | None = None, kind: str = "human_response",
) -> str:
    """Reference one exact Job response; arbitrary evidence paths are forbidden."""
    owned = bounded_query(connection,
        "SELECT owner_id FROM work_evidence WHERE run_id=? AND evidence_id=?",
        (run_id, source_evidence_id),
    ).fetchone()
    if (owned is None or not isinstance(owned[0], str) or not owned[0].startswith("job:")
            or source_owner_id is not None and owned[0] != source_owner_id):
        raise ValueError("Human response belongs to another Job")
    original = get_evidence(connection, run_id, source_evidence_id, owner_id=owned[0],
                            kind="human_response_history")
    _, digest, size = _encoded(_human_response(original))
    return _insert(connection, run_id, owner_id, kind, digest, size,
                   source=source_evidence_id, member="response")
