"""One immutable identity and explicit state versions for each Semantic Attempt."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_query_rows import bounded_query, current_collection_size
from agent_run.semantic_attempt import require_semantic_attempt_record
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_mutations import _same
from agent_run.work_projection import JobHistoryAppend
from agent_run.sqlite_work_evidence import (
    evidence_size,
    get_evidence,
    put_evidence,
    put_evidence_member,
)
from agent_run.sqlite_work_execution_schema import (
    ATTEMPT_EVIDENCE,
    ATTEMPT_FIELDS,
    ATTEMPT_IDENTITY,
    ATTEMPT_SCALARS,
    execute_values,
    presence,
    present_fields,
    restore_scalars,
    scalar_values,
)

_VERSION_COLUMNS = (
    "presence",
    "logical_bytes",
    *ATTEMPT_SCALARS,
    *(f"{field}_evidence_id" for field in ATTEMPT_EVIDENCE),
)
_ARCHIVE_KINDS = {
    "retired_job_generation",
    "retired_attempt_owner",
    "retired_parent",
    "retired_acceptance",
    "retired_publication",
    "budget_window",
}


def _digest(value: Any) -> str:
    return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()


def _version_row(c: sqlite3.Connection, run_id: str, attempt_id: str, version_id: str) -> dict[str, Any]:
    row = bounded_query(c, "SELECT " + ",".join(_VERSION_COLUMNS) + " FROM work_attempt_versions "
        "WHERE run_id=? AND attempt_id=? AND version_id=?",
        (run_id, attempt_id, version_id),
    ).fetchone()
    if row is None:
        raise ValueError("Missing Semantic Attempt version")
    result = dict(zip(_VERSION_COLUMNS, row, strict=True))
    present_fields(result["presence"], ATTEMPT_FIELDS)
    if type(result["logical_bytes"]) is not int or result["logical_bytes"] < 2:
        raise ValueError("Invalid Semantic Attempt size")
    if _version_id(attempt_id, result) != version_id:
        raise ValueError("Immutable Semantic Attempt version was changed")
    return result


def _version_id(attempt_id: str, row: Mapping[str, Any]) -> str:
    return _digest(
        (
            attempt_id,
            row["presence"].hex(),
            tuple(row[field] for field in ATTEMPT_SCALARS),
            tuple(row[f"{field}_evidence_id"] for field in ATTEMPT_EVIDENCE),
        )
    )


def _identity(
    c: sqlite3.Connection,
    run_id: str,
    record: Mapping[str, Any],
    job_id: str | None,
) -> None:
    attempt_id = record["attempt_id"]
    values = scalar_values(record, ATTEMPT_IDENTITY)
    row = bounded_query(c, "SELECT job_id," + ",".join(ATTEMPT_IDENTITY) + " FROM work_attempts " "WHERE run_id=? AND attempt_id=?",
        (run_id, attempt_id),
    ).fetchone()
    if row is None:
        execute_values(
            c,
            "work_attempts",
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "job_id": job_id,
                **dict(zip(ATTEMPT_IDENTITY, values, strict=True)),
            },
        )
        return
    if row[1:] != values:
        raise ValueError("Semantic Attempt identity changed")
    if job_id is None or row[0] == job_id:
        return
    if row[0] is None:
        c.execute("UPDATE work_attempts SET job_id=? WHERE run_id=? AND attempt_id=?", (job_id, run_id, attempt_id))
        return
    owner = bounded_query(c, "SELECT owner_kind FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
    if owner is None or owner[0] not in _ARCHIVE_KINDS:
        raise ValueError("Semantic Attempt belongs to another Job")


def _selected_member(original: Any, member: str) -> Any:
    if not isinstance(original, dict):
        raise ValueError("Invalid Semantic Attempt raw result")
    if member == "publication_fields":
        artifact = original.get("artifact")
        fields = ("commit_message", "pr_title", "pr_body_markdown")
        if not isinstance(artifact, dict) or any(key not in artifact for key in fields):
            raise ValueError("Missing Publication result fields")
        return {key: artifact[key] for key in fields}
    if member not in original:
        raise ValueError("Missing Semantic Attempt raw result member")
    return original[member]


def _remember_return(
    c: sqlite3.Connection,
    run_id: str,
    attempt_id: str,
    evidence_id: str,
    value: Any,
    record: Mapping[str, Any],
) -> None:
    existing = bounded_query(c, "SELECT sequence FROM work_attempt_returns WHERE run_id=? AND attempt_id=? AND evidence_id=?",
        (run_id, attempt_id, evidence_id),
    ).fetchone()
    if existing is not None:
        return
    sequence = bounded_query(c, "SELECT COALESCE(MAX(sequence),0)+1 FROM work_attempt_returns WHERE run_id=? AND attempt_id=?",
        (run_id, attempt_id),
    ).fetchone()[0]
    c.execute("INSERT INTO work_attempt_returns VALUES (?,?,?,?)", (run_id, attempt_id, evidence_id, sequence))
    if not isinstance(value, dict):
        return
    result_member = {
        "development": ("development_summary", "summary"),
        "review": ("acceptance_artifact", "artifact"),
        "publication": ("publication", "artifact"),
        "publication_wire": ("publication", "artifact"),
    }.get(str(value.get("kind")))
    if result_member is not None:
        field, member = result_member
        thread_id = value.get("thread_id")
        if thread_id is None and isinstance(value.get("artifact"), dict):
            thread_id = value["artifact"].get("_thread_id")
        context = record.get("review_context")
        review_identity = None
        if field == "acceptance_artifact" and isinstance(context, dict):
            facts = context.get("facts")
            if isinstance(facts, dict) and "current_review_identity" in facts:
                from agent_run.review_context import review_record_identity

                review_identity = _digest(review_record_identity(context))
        members = [member]
        artifact = value.get("artifact")
        if (
            field == "publication"
            and isinstance(artifact, dict)
            and all(key in artifact for key in ("commit_message", "pr_title", "pr_body_markdown"))
        ):
            members.append("publication_fields")
        for selector in members:
            c.execute(
                "INSERT INTO work_attempt_result_members VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    attempt_id,
                    evidence_id,
                    field,
                    selector,
                    thread_id,
                    review_identity,
                    _digest(_selected_member(value, selector)),
                ),
            )


def _member_reference(
    c: sqlite3.Connection,
    run_id: str,
    attempt_id: str,
    field: str,
    value: Any,
) -> tuple[str, str] | None:
    row = bounded_query(c, "SELECT m.evidence_id,m.member_name FROM work_attempt_result_members m "
        "JOIN work_attempt_returns r USING(run_id,attempt_id,evidence_id) "
        "WHERE m.run_id=? AND m.attempt_id=? AND m.result_field=? AND m.value_fingerprint=? "
        "ORDER BY r.sequence DESC,CASE m.member_name WHEN 'artifact' THEN 0 ELSE 1 END LIMIT 1",
        (run_id, attempt_id, field, _digest(value)),
    ).fetchone()
    if row is not None:
        original = get_evidence(c, run_id, row[0], owner_id="attempt:" + attempt_id, kind="returned_result")
        if not _same(_selected_member(original, row[1]), value):
            raise ValueError("Semantic Attempt result member changed")
        return str(row[0]), str(row[1])
    return None


def _record_size(c: sqlite3.Connection, run_id: str, record: Mapping[str, Any], refs: Mapping[str, Any]) -> int:
    size = 2 + max(len(record) - 1, 0)
    for key, value in record.items():
        reference = refs.get(key)
        value_size = (
            evidence_size(c, run_id, reference) if reference is not None else len(encode_json(value).encode("utf-8"))
        )
        size += len(encode_json(key).encode("utf-8")) + 1 + value_size
    return size


def save_version(
    c: sqlite3.Connection,
    run_id: str,
    record: Mapping[str, Any],
    *,
    job_id: str | None = None,
    before: Mapping[str, Any] | None = None,
    previous_version: str | None = None,
) -> str:
    included = presence(record, ATTEMPT_FIELDS)
    require_semantic_attempt_record(dict(record), expected_status=str(record.get("status")))
    if record.get("status") not in {"pending", "completed"}:
        raise ValueError("Invalid Semantic Attempt status")
    _identity(c, run_id, record, job_id)
    attempt_id = str(record["attempt_id"])
    owner = "attempt:" + attempt_id
    old = _version_row(c, run_id, attempt_id, previous_version) if previous_version is not None else {}
    refs: dict[str, Any] = {}
    # Persist the raw response first, so consumed fields can reference it.
    for field in ("returned_result", *(field for field in ATTEMPT_EVIDENCE if field != "returned_result")):
        value = record.get(field)
        if field not in record or value is None:
            refs[field] = None
        elif before is not None and field in before and _same(before[field], value) and old:
            refs[field] = old[f"{field}_evidence_id"]
        else:
            source = (
                _member_reference(c, run_id, attempt_id, field, value)
                if field in {"development_summary", "publication"}
                else None
            )
            refs[field] = (
                put_evidence_member(c, run_id, owner, field, source[0], (source[1],))
                if source is not None
                else put_evidence(c, run_id, owner, field, value)
            )
        if field == "returned_result" and refs[field] is not None:
            _remember_return(c, run_id, attempt_id, refs[field], value, record)
    row = {
        "presence": included,
        "logical_bytes": _record_size(c, run_id, record, refs),
        **dict(zip(ATTEMPT_SCALARS, scalar_values(record, ATTEMPT_SCALARS), strict=True)),
        **{f"{field}_evidence_id": refs[field] for field in ATTEMPT_EVIDENCE},
    }
    version_id = _version_id(attempt_id, row)
    existing = bounded_query(c, "SELECT " + ",".join(_VERSION_COLUMNS) + " FROM work_attempt_versions "
        "WHERE run_id=? AND attempt_id=? AND version_id=?",
        (run_id, attempt_id, version_id),
    ).fetchone()
    if existing is not None and existing != tuple(row[column] for column in _VERSION_COLUMNS):
        raise ValueError("Immutable Semantic Attempt version was changed")
    if existing is None:
        execute_values(
            c,
            "work_attempt_versions",
            {
                "run_id": run_id,
                "attempt_id": attempt_id,
                "version_id": version_id,
                **row,
            },
        )
    current_collection_size(c, "SELECT " + ",".join(_VERSION_COLUMNS) + " FROM work_attempt_versions "
                            "WHERE run_id=? AND attempt_id=? AND version_id=?",
                            (run_id, attempt_id, version_id), _VERSION_COLUMNS)
    return version_id


def load_version(c: sqlite3.Connection, run_id: str, attempt_id: str, version_id: str) -> dict[str, Any]:
    row = _version_row(c, run_id, attempt_id, version_id)
    identity = bounded_query(c, "SELECT " + ",".join(ATTEMPT_IDENTITY) + " FROM work_attempts WHERE run_id=? AND attempt_id=?",
        (run_id, attempt_id),
    ).fetchone()
    if identity is None:
        raise ValueError("Missing Semantic Attempt identity")
    included = present_fields(row["presence"], ATTEMPT_FIELDS)
    record: dict[str, Any] = {"attempt_id": attempt_id}
    restore_scalars(record, ATTEMPT_IDENTITY, identity, included)
    restore_scalars(record, ATTEMPT_SCALARS, tuple(row[key] for key in ATTEMPT_SCALARS), included)
    for field in ATTEMPT_EVIDENCE:
        reference = row[f"{field}_evidence_id"]
        if field in included:
            record[field] = (
                None
                if reference is None
                else get_evidence(
                    c,
                    run_id,
                    reference,
                    owner_id="attempt:" + attempt_id,
                    kind=field,
                )
            )
        elif reference is not None:
            raise ValueError("Unexpected Semantic Attempt evidence")
    if (
        _record_size(c, run_id, record, {field: row[f"{field}_evidence_id"] for field in ATTEMPT_EVIDENCE})
        != row["logical_bytes"]
    ):
        raise ValueError("Invalid Semantic Attempt size")
    require_semantic_attempt_record(record, expected_status=str(record.get("status")))
    return record


def _records(subject: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    records: dict[str, Mapping[str, Any]] = {}
    history = subject.get("semantic_attempt_history", [])
    if not isinstance(history, list):
        raise ValueError("semantic_attempt_history must be an array")
    order = []
    for entry in history:
        if not isinstance(entry, dict) or not isinstance(entry.get("attempt_id"), str):
            raise ValueError("Invalid Semantic Attempt history")
        identity = entry["attempt_id"]
        if identity in records:
            raise ValueError("Duplicate Semantic Attempt identity")
        records[identity] = entry
        order.append(identity)
    pending = subject.get("pending_semantic_attempt")
    if pending is not None:
        if not isinstance(pending, dict) or not isinstance(pending.get("attempt_id"), str):
            raise ValueError("Invalid pending Semantic Attempt")
        if pending["attempt_id"] in records:
            raise ValueError("Duplicate Semantic Attempt identity")
        records[pending["attempt_id"]] = pending
    return records, order


def _roots(c: sqlite3.Connection, run_id: str, job_id: str) -> tuple[Any, ...] | None:
    row = bounded_query(c, "SELECT pending_present,history_present,pending_attempt_id,history_count,history_bytes "
        "FROM work_job_attempt_roots WHERE run_id=? AND job_id=?",
        (run_id, job_id),
    ).fetchone()
    if (
        row is None
        and bounded_query(c, "SELECT 1 FROM work_job_attempt_history WHERE run_id=? AND job_id=? LIMIT 1", (run_id, job_id)
        ).fetchone()
        is not None
    ):
        raise ValueError("Missing Semantic Attempt history summary")
    if row is not None:
        pending, history, _, count, size = row
        if (
            type(pending) is not int
            or pending not in (0, 1)
            or type(history) is not int
            or history not in (0, 1)
            or type(count) is not int
            or count < 0
            or type(size) is not int
            or size < 2 + count * 2 + max(count - 1, 0)
            or count == 0
            and size != 2
            or not history
            and count != 0
        ):
            raise ValueError("Invalid Semantic Attempt history summary")
    return None if row is None else tuple(row)


def _history_size(c: sqlite3.Connection, run_id: str, attempt_id: str, version_id: str) -> int:
    row = bounded_query(c, "SELECT logical_bytes FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?",
        (run_id, attempt_id, version_id),
    ).fetchone()
    if row is None or type(row[0]) is not int or row[0] < 2:
        raise ValueError("Missing or invalid Semantic Attempt history version")
    return int(row[0])


def save_attempts(
    c: sqlite3.Connection,
    run_id: str,
    job_id: str,
    subject: Mapping[str, Any],
    baseline_subject: Mapping[str, Any],
) -> None:
    records, order = _records(subject)
    before, old_order = _records(baseline_subject)
    versions: dict[str, str] = {}
    for attempt_id, record in records.items():
        current = bounded_query(c, "SELECT version_id FROM work_attempt_current WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)
        ).fetchone()
        previous_version = str(current[0]) if current is not None else None
        previous = before.get(attempt_id)
        if previous is not None and previous_version is not None:
            if _same(record, previous):
                versions[attempt_id] = previous_version
                continue
            if not _same(load_version(c, run_id, attempt_id, previous_version), previous):
                raise ValueError("Semantic Attempt changed before save")
            if (
                "returned_result" in record
                and "returned_result" in previous
                and not _same(record["returned_result"], previous["returned_result"])
            ):
                raise ValueError("Semantic Attempt already has a different saved result")
        version = save_version(c, run_id, record, job_id=job_id, before=previous, previous_version=previous_version)
        owner = bounded_query(c, "SELECT job_id FROM work_attempts WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)
        ).fetchone()[0]
        if owner == job_id:
            c.execute(
                "INSERT INTO work_attempt_current VALUES (?,?,?) ON CONFLICT(run_id,attempt_id) "
                "DO UPDATE SET version_id=excluded.version_id",
                (run_id, attempt_id, version),
            )
        # A retired owner can close its last pending Attempt while it is
        # removed. Its archive references that exact immutable completion
        # version; the original owner's current pointer and Invocation
        # startup versions remain untouched. _identity validates the alias.
        versions[attempt_id] = version
    delta = subject.get("semantic_attempt_history")
    root = _roots(c, run_id, job_id) or (0, 0, None, 0, 2)
    pending_present, history_present, pending_id, history_count, history_bytes = root
    history_sum = history_bytes - 2 - max(history_count - 1, 0)
    if "pending_semantic_attempt" in subject or "pending_semantic_attempt" in baseline_subject:
        pending_present = int("pending_semantic_attempt" in subject)
        pending = subject.get("pending_semantic_attempt")
        pending_id = pending["attempt_id"] if isinstance(pending, dict) else None
    if "semantic_attempt_history" in subject or "semantic_attempt_history" in baseline_subject:
        history_present = int("semantic_attempt_history" in subject)
        for removed in set() if isinstance(delta, JobHistoryAppend) else set(old_order) - set(order):
            old = bounded_query(c, "SELECT version_id FROM work_job_attempt_history WHERE run_id=? AND job_id=? AND attempt_id=?",
                (run_id, job_id, removed),
            ).fetchone()
            if old is not None:
                history_sum -= _history_size(c, run_id, removed, old[0])
                history_count -= 1
            c.execute(
                "DELETE FROM work_job_attempt_history WHERE run_id=? AND job_id=? AND attempt_id=?",
                (run_id, job_id, removed),
            )
        # Bounded history positions belong to this owner, rather than the Run selection.
        for ordinal, attempt_id in enumerate(order):
            position = delta.base_count + ordinal if isinstance(delta, JobHistoryAppend) else ordinal
            if isinstance(delta, JobHistoryAppend):
                existing = bounded_query(c, "SELECT position,version_id FROM work_job_attempt_history WHERE run_id=? AND job_id=? AND attempt_id=?",
                    (run_id, job_id, attempt_id),
                ).fetchone()
                if existing is not None:
                    if existing != (position, versions[attempt_id]):
                        raise ValueError("Cannot overwrite an unread Semantic Attempt")
                    continue
                if position != history_count:
                    raise ValueError("Semantic Attempt append no longer matches its history count")
            if (
                not isinstance(delta, JobHistoryAppend)
                and position < len(old_order)
                and old_order[position] == attempt_id
                and _same(records[attempt_id], before.get(attempt_id))
            ):
                continue
            displaced = bounded_query(c, "SELECT attempt_id,version_id FROM work_job_attempt_history WHERE run_id=? AND job_id=? "
                "AND (position=? OR attempt_id=?)",
                (run_id, job_id, position, attempt_id),
            ).fetchall()
            for old_id, old_version in displaced:
                history_sum -= _history_size(c, run_id, old_id, old_version)
                history_count -= 1
            c.execute(
                "DELETE FROM work_job_attempt_history WHERE run_id=? AND job_id=? AND (position=? OR attempt_id=?)",
                (run_id, job_id, position, attempt_id),
            )
            c.execute(
                "INSERT INTO work_job_attempt_history VALUES (?,?,?,?,?)",
                (run_id, job_id, position, attempt_id, versions[attempt_id]),
            )
            history_sum += _history_size(c, run_id, attempt_id, versions[attempt_id])
            history_count += 1
    history_bytes = history_sum + 2 + max(history_count - 1, 0)
    if not history_present and (history_count != 0 or history_bytes != 2):
        raise ValueError("Cannot hide retained Semantic Attempt history")
    updated = (pending_present, history_present, pending_id, history_count, history_bytes)
    if updated != root or any(field in subject for field in ("pending_semantic_attempt", "semantic_attempt_history")):
        c.execute(
            "INSERT INTO work_job_attempt_roots VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id,job_id) DO UPDATE SET "
            "pending_present=excluded.pending_present,history_present=excluded.history_present,pending_attempt_id=excluded.pending_attempt_id,"
            "history_count=excluded.history_count,history_bytes=excluded.history_bytes",
            (run_id, job_id, *updated),
        )


def load_attempts(c: sqlite3.Connection, run_id: str, job_id: str, include_history: bool = True) -> dict[str, Any]:
    row = _roots(c, run_id, job_id)
    if row is None:
        return {}
    pending_present, history_present, pending_id, count, size = row
    result: dict[str, Any] = {}
    if pending_present:
        current = (
            bounded_query(c, "SELECT version_id FROM work_attempt_current WHERE run_id=? AND attempt_id=?", (run_id, pending_id)
            ).fetchone()
            if pending_id is not None
            else None
        )
        if pending_id is not None and current is None:
            raise ValueError("Missing pending Semantic Attempt version")
        result["pending_semantic_attempt"] = (
            None if current is None else load_version(c, run_id, pending_id, current[0])
        )
    if include_history:
        rows = bounded_query(c, "SELECT h.position,h.attempt_id,h.version_id,v.logical_bytes FROM work_job_attempt_history h "
            "LEFT JOIN work_attempt_versions v USING(run_id,attempt_id,version_id) "
            "WHERE h.run_id=? AND h.job_id=? ORDER BY h.position",
            (run_id, job_id),
        )
        history, total, observed = [], 0, 0
        for position, identity, version, logical_bytes in rows:
            if (
                type(position) is not int
                or position != observed
                or type(logical_bytes) is not int
                or logical_bytes < 2
            ):
                raise ValueError("Invalid Semantic Attempt size, history positions or count")
            total += logical_bytes
            observed += 1
            if history_present:
                history.append(load_version(c, run_id, identity, version))
        if observed != count or total + 2 + max(count - 1, 0) != size:
            raise ValueError("Invalid Semantic Attempt size, history positions or count")
        if history_present:
            result["semantic_attempt_history"] = history
    return result


def attempt_history_metadata(c: sqlite3.Connection, run_id: str, job_id: str) -> tuple[int, int]:
    root = _roots(c, run_id, job_id)
    return (2, 0) if root is None else (int(root[4]), int(root[3]))


def save_publication_retry(
    c: sqlite3.Connection, run_id: str, job_id: str, before: dict[str, Any], attempt: dict[str, Any]
) -> None:
    """Patch one precisely read completed Attempt without materializing its archive."""
    if _same(before, attempt):
        return
    identity = before.get("attempt_id")
    if (
        not isinstance(identity, str)
        or before.get("role") != "publication"
        or before.get("status") != "completed"
        or {key: value for key, value in before.items() if key != "publication_operation_retry"}
        != {key: value for key, value in attempt.items() if key != "publication_operation_retry"}
    ):
        raise ValueError("Only the exact completed Publication retry may be updated")
    row = bounded_query(c, "SELECT position,version_id FROM work_job_attempt_history WHERE run_id=? AND job_id=? AND attempt_id=?",
        (run_id, job_id, identity),
    ).fetchone()
    root = _roots(c, run_id, job_id)
    if row is None or root is None or not 0 <= row[0] < root[3] or load_version(c, run_id, identity, row[1]) != before:
        raise ValueError("Completed Publication Attempt changed before retry update")
    current = bounded_query(c, "SELECT version_id FROM work_attempt_current WHERE run_id=? AND attempt_id=?", (run_id, identity)
    ).fetchone()
    if current != (row[1],):
        raise ValueError("Completed Publication current version changed before retry update")
    old_size = _history_size(c, run_id, identity, row[1])
    version = save_version(c, run_id, attempt, job_id=job_id, before=before, previous_version=row[1])
    new_size = _history_size(c, run_id, identity, version)
    c.execute(
        "UPDATE work_job_attempt_history SET version_id=? WHERE run_id=? AND job_id=? AND attempt_id=?",
        (version, run_id, job_id, identity),
    )
    c.execute(
        "UPDATE work_attempt_current SET version_id=? WHERE run_id=? AND attempt_id=?", (version, run_id, identity)
    )
    c.execute(
        "UPDATE work_job_attempt_roots SET history_bytes=? WHERE run_id=? AND job_id=?",
        (root[4] + new_size - old_size, run_id, job_id),
    )


def load_completed_publication_attempt(
    c: sqlite3.Connection, run_id: str, job_id: str, ordinal: int
) -> dict[str, Any] | None:
    root = _roots(c, run_id, job_id)
    row = bounded_query(c, "SELECT h.attempt_id,h.version_id,h.position FROM work_attempts a INDEXED BY work_attempt_owner_role "
        "CROSS JOIN work_job_attempt_history h CROSS JOIN work_attempt_versions v "
        "WHERE a.run_id=? AND a.job_id=? AND a.role='publication' AND a.ordinal=? "
        "AND h.run_id=a.run_id AND h.job_id=? AND h.attempt_id=a.attempt_id "
        "AND v.run_id=h.run_id AND v.attempt_id=h.attempt_id AND v.version_id=h.version_id "
        "AND v.status='completed' ORDER BY h.position DESC LIMIT 1",
        (run_id, job_id, ordinal, job_id),
    ).fetchone()
    if row is not None and (root is None or type(row[2]) is not int or not 0 <= row[2] < root[3]):
        raise ValueError("Invalid related Semantic Attempt history position")
    return None if row is None else load_version(c, run_id, row[0], row[1])


def find_result_reference(
    c: sqlite3.Connection,
    run_id: str,
    job_id: str,
    field: str,
    value: Any,
) -> tuple[str, str, str, tuple[str, ...]] | None:
    thread_id, review_identity = None, None
    if field == "review_artifact" and isinstance(value, Mapping) and "artifact" in value:
        thread_id = value.get("reviewer_thread_id")
        if value.get("review_identity") is not None:
            review_identity = _digest(value["review_identity"])
        value = value["artifact"]
    field = {
        "review_artifact": "acceptance_artifact",
        "pending_review_result": "acceptance_artifact",
        "pending_publication_result": "publication",
    }.get(field, field)
    if field not in {"development_summary", "acceptance_artifact", "publication"}:
        return None
    row = bounded_query(c, "SELECT m.evidence_id,m.attempt_id,m.member_name FROM work_attempts a "
        "JOIN work_attempt_result_members m USING(run_id,attempt_id) "
        "JOIN work_attempt_returns r USING(run_id,attempt_id,evidence_id) "
        "WHERE a.run_id=? AND (a.job_id=? OR EXISTS (SELECT 1 FROM work_job_attempt_history h "
        "WHERE h.run_id=a.run_id AND h.job_id=? AND h.attempt_id=a.attempt_id) OR EXISTS "
        "(SELECT 1 FROM work_jobs j WHERE j.run_id=a.run_id AND j.job_id=a.job_id AND j.budget_owner_id=?)) "
        "AND m.result_field=? AND m.value_fingerprint=? "
        "AND (? IS NULL OR m.thread_id=?) AND (? IS NULL OR m.review_identity_fingerprint=?) "
        "ORDER BY a.generation DESC,a.budget_window DESC,a.ordinal DESC,r.sequence DESC,"
        "CASE m.member_name WHEN 'artifact' THEN 0 ELSE 1 END LIMIT 1",
        (
            run_id,
            job_id,
            job_id,
            job_id,
            field,
            _digest(value),
            thread_id,
            thread_id,
            review_identity,
            review_identity,
        ),
    ).fetchone()
    if row is None:
        return None
    owner = "attempt:" + row[1]
    original = get_evidence(c, run_id, row[0], owner_id=owner, kind="returned_result")
    if not _same(_selected_member(original, row[2]), value):
        raise ValueError("Semantic Attempt result member changed")
    return str(row[0]), owner, "returned_result", (str(row[2]),)
