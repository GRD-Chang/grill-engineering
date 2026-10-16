"""Explicit Attempt versions, Invocation facts and identity references."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

ATTEMPT_IDENTITY = {
    "work_subject": str, "generation": int, "role": str, "ordinal": int,
    "budget_window": int, "currentness_boundary_fingerprint": str,
}
ATTEMPT_SCALARS = {"status": str, "outcome": str, "budget_consumed": bool}
ATTEMPT_EVIDENCE = (
    "budget_snapshot", "review_context", "history_facts", "review_identity",
    "development_summary", "publication", "publication_operation_retry",
    "publication_commit_observation", "returned_result",
)
ATTEMPT_FIELDS = ("attempt_id", *ATTEMPT_IDENTITY, *ATTEMPT_SCALARS, *ATTEMPT_EVIDENCE)
INVOCATION_SCALARS = {
    "generation": int, "role": str, "phase": str, "mode": str,
    "input_fingerprint": str, "status": str, "requested_thread_id": str,
    "reported_thread_id": str, "binding_id": str, "binding_role": str,
    "profile_role": str, "invocation_role": str, "model": str,
    "reasoning_effort": str, "profile_revision": int, "attempt_count": int,
    "ended_at": str, "predecessor_invocation_started_at": str, "error": str,
    "return_code": int, "signal": int, "resume_id": str, "resume_sequence": int,
    "output_attempt": int, "validation_error": str, "ordinary_recovery_used": bool,
    "capacity_recovery_count": int, "deadline_seconds": float, "deadline_at": str,
    "execution_interrupted": bool, "recovery_waiting": bool, "last_failure_at": str,
    "last_recovery_started_at": str, "recovery_kind": str, "machine_error": str,
    "execution_not_started": bool, "interruption_observed_at": str,
}
INVOCATION_EVIDENCE = ("currentness_boundary", "thread_execution_binding")
DETAIL_FIELDS = ("execution_segments", "recovery_wait_intervals")
TIMING_FIELDS = {
    "execution_seconds": int, "recorded_execution_seconds": int,
    "execution_timing_complete": bool,
}
SUMMARY_FIELDS = {"count": int, "sha256": str}
INVOCATION_FIELDS = (
    "work_subject", "started_at", *INVOCATION_SCALARS, *INVOCATION_EVIDENCE,
    "semantic_attempt", *DETAIL_FIELDS, "execution_timing_summary",
    "execution_segments_summary", "recovery_wait_intervals_summary",
)


def presence(record: Mapping[str, Any], fields: tuple[str, ...]) -> bytes:
    unknown = record.keys() - set(fields)
    if unknown:
        raise ValueError(f"Unknown execution record fields: {sorted(unknown)}")
    bits = sum(1 << index for index, key in enumerate(fields) if key in record)
    return bits.to_bytes((len(fields) + 7) // 8, "little")


def present_fields(encoded: object, fields: tuple[str, ...]) -> set[str]:
    if not isinstance(encoded, bytes) or len(encoded) != (len(fields) + 7) // 8:
        raise ValueError("Invalid execution record presence")
    bits = int.from_bytes(encoded, "little")
    if bits >> len(fields):
        raise ValueError("Invalid execution record presence")
    return {key for index, key in enumerate(fields) if bits & (1 << index)}


def scalar_values(record: Mapping[str, Any], fields: Mapping[str, type]) -> tuple[Any, ...]:
    values = []
    for key, expected in fields.items():
        value = record.get(key)
        if value is not None and not (
            type(value) is expected or expected is float and type(value) is int
        ):
            raise ValueError(f"Invalid execution record {key}")
        values.append(int(bool(value)) if expected is bool and value is not None else value)
    return tuple(values)


def restore_scalars(
    result: dict[str, Any], fields: Mapping[str, type], values: tuple[Any, ...],
    included: set[str],
) -> None:
    for (key, expected), value in zip(fields.items(), values, strict=True):
        if key not in included:
            if value is not None:
                raise ValueError("Unexpected execution record column")
            continue
        if expected is bool and value is not None:
            if type(value) is not int or value not in (0, 1):
                raise ValueError(f"Invalid execution record {key}")
            value = bool(value)
        result[key] = value
    scalar_values(result, {key: expected for key, expected in fields.items() if key in result})


def _columns(fields: Mapping[str, type], *, prefix: str = "") -> str:
    types = {str: "TEXT", int: "INTEGER", bool: "INTEGER", float: "REAL"}
    return ", ".join(f"{prefix}{key} {types[expected]}" for key, expected in fields.items())


def _evidence_fk(column: str) -> str:
    return (f"FOREIGN KEY(run_id,{column}) REFERENCES work_evidence(run_id,evidence_id) "
            "DEFERRABLE INITIALLY DEFERRED")


_ATTEMPT_FK = "FOREIGN KEY(run_id,attempt_id) REFERENCES work_attempts(run_id,attempt_id) ON DELETE CASCADE"
_VERSION_FK = (
    "FOREIGN KEY(run_id,attempt_id,version_id) REFERENCES "
    "work_attempt_versions(run_id,attempt_id,version_id) ON DELETE CASCADE"
)
_JOB_FK = "FOREIGN KEY(run_id,job_id) REFERENCES work_jobs(run_id,job_id) ON DELETE CASCADE"
_INVOCATION_KEY = "run_id TEXT NOT NULL, work_subject TEXT NOT NULL, started_at TEXT NOT NULL"
_INVOCATION_FK = (
    "FOREIGN KEY(run_id,work_subject,started_at) REFERENCES "
    "work_invocations(run_id,work_subject,started_at) ON DELETE CASCADE"
)
TABLES = {
    "work_attempts": (
        "CREATE TABLE work_attempts (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, job_id TEXT, "
        + _columns(ATTEMPT_IDENTITY) + ", PRIMARY KEY(run_id,attempt_id), " + _JOB_FK + ", "
        "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)"
    ),
    "work_attempt_versions": (
        "CREATE TABLE work_attempt_versions (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, "
        "version_id TEXT NOT NULL, presence BLOB NOT NULL, logical_bytes INTEGER NOT NULL, "
        + _columns(ATTEMPT_SCALARS) + ", "
        + ", ".join(f"{key}_evidence_id TEXT" for key in ATTEMPT_EVIDENCE)
        + ", PRIMARY KEY(run_id,attempt_id,version_id), " + _ATTEMPT_FK + ", "
        + ", ".join(_evidence_fk(f"{key}_evidence_id") for key in ATTEMPT_EVIDENCE) + ")"
    ),
    "work_attempt_current": (
        "CREATE TABLE work_attempt_current (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, "
        "version_id TEXT NOT NULL, PRIMARY KEY(run_id,attempt_id), " + _VERSION_FK + ")"
    ),
    "work_attempt_returns": (
        "CREATE TABLE work_attempt_returns (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, "
        "evidence_id TEXT NOT NULL, sequence INTEGER NOT NULL, PRIMARY KEY(run_id,attempt_id,evidence_id), "
        "UNIQUE(run_id,attempt_id,sequence), " + _ATTEMPT_FK + ", " + _evidence_fk("evidence_id") + ")"
    ),
    "work_attempt_result_members": (
        "CREATE TABLE work_attempt_result_members (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, "
        "evidence_id TEXT NOT NULL, result_field TEXT NOT NULL CHECK(result_field IN "
        "('development_summary','acceptance_artifact','publication')), member_name TEXT NOT NULL "
        "CHECK(member_name IN ('summary','artifact','publication_fields')), thread_id TEXT, "
        "review_identity_fingerprint TEXT, value_fingerprint TEXT NOT NULL, "
        "PRIMARY KEY(run_id,attempt_id,evidence_id,result_field,member_name), FOREIGN KEY(run_id,attempt_id,evidence_id) "
        "REFERENCES work_attempt_returns(run_id,attempt_id,evidence_id) ON DELETE CASCADE)"
    ),
    "work_job_attempt_roots": (
        "CREATE TABLE work_job_attempt_roots (run_id TEXT NOT NULL, job_id TEXT NOT NULL, "
        "pending_present INTEGER NOT NULL, history_present INTEGER NOT NULL, pending_attempt_id TEXT, "
        "history_count INTEGER NOT NULL CHECK(history_count >= 0), "
        "history_bytes INTEGER NOT NULL CHECK(history_bytes >= 2), "
        "PRIMARY KEY(run_id,job_id), " + _JOB_FK + ", "
        "FOREIGN KEY(run_id,pending_attempt_id) REFERENCES work_attempts(run_id,attempt_id))"
    ),
    "work_job_attempt_history": (
        "CREATE TABLE work_job_attempt_history (run_id TEXT NOT NULL, job_id TEXT NOT NULL, "
        "position INTEGER NOT NULL, attempt_id TEXT NOT NULL, version_id TEXT NOT NULL, "
        "PRIMARY KEY(run_id,job_id,position), UNIQUE(run_id,job_id,attempt_id), "
        + _JOB_FK + ", " + _VERSION_FK + ")"
    ),
    "work_invocations": (
        "CREATE TABLE work_invocations (" + _INVOCATION_KEY + ", presence BLOB NOT NULL, "
        + _columns(INVOCATION_SCALARS) + ", attempt_id TEXT, attempt_version_id TEXT, "
        + ", ".join(f"{key}_evidence_id TEXT" for key in INVOCATION_EVIDENCE)
        + ", raw_result_evidence_id TEXT, resume_pending INTEGER NOT NULL, details_retained INTEGER NOT NULL, execution_segments_null INTEGER NOT NULL, "
        "recovery_wait_intervals_null INTEGER NOT NULL, timing_presence BLOB, "
        + _columns(TIMING_FIELDS, prefix="timing_") + ", segments_summary_presence BLOB, "
        + _columns(SUMMARY_FIELDS, prefix="segments_summary_") + ", waits_summary_presence BLOB, "
        + _columns(SUMMARY_FIELDS, prefix="waits_summary_")
        + ", PRIMARY KEY(run_id,work_subject,started_at), FOREIGN KEY(run_id) REFERENCES runs(run_id) "
        "ON DELETE CASCADE, FOREIGN KEY(run_id,attempt_id,attempt_version_id) REFERENCES "
        "work_attempt_versions(run_id,attempt_id,version_id), "
        + ", ".join(_evidence_fk(column) for column in (
            *(f"{key}_evidence_id" for key in INVOCATION_EVIDENCE), "raw_result_evidence_id",
        )) + ")"
    ),
    "work_invocation_roots": (
        "CREATE TABLE work_invocation_roots (run_id TEXT PRIMARY KEY, active_present INTEGER NOT NULL, "
        "tickets_present INTEGER NOT NULL, history_present INTEGER NOT NULL, "
        "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)"
    ),
    "work_invocation_current": (
        "CREATE TABLE work_invocation_current (" + _INVOCATION_KEY + ", reference_kind TEXT NOT NULL "
        "CHECK(reference_kind IN ('active','ticket')), reference_key TEXT NOT NULL, "
        "PRIMARY KEY(run_id,reference_kind,reference_key), "
        + _INVOCATION_FK + ")"
    ),
    "work_invocation_history": (
        "CREATE TABLE work_invocation_history (" + _INVOCATION_KEY + ", position INTEGER NOT NULL, "
        "PRIMARY KEY(run_id,position), UNIQUE(run_id,work_subject,started_at), " + _INVOCATION_FK + ")"
    ),
    "work_invocation_segments": (
        "CREATE TABLE work_invocation_segments (" + _INVOCATION_KEY + ", position INTEGER NOT NULL, "
        "presence BLOB NOT NULL, started TEXT, ended TEXT, PRIMARY KEY(run_id,work_subject,started_at,position), "
        + _INVOCATION_FK + ")"
    ),
    "work_invocation_waits": (
        "CREATE TABLE work_invocation_waits (" + _INVOCATION_KEY + ", position INTEGER NOT NULL, "
        "presence BLOB NOT NULL, kind TEXT, started TEXT, ended TEXT, "
        "PRIMARY KEY(run_id,work_subject,started_at,position), " + _INVOCATION_FK + ")"
    ),
}
INDEXES = {
    "work_attempt_owner_role": "CREATE INDEX work_attempt_owner_role ON work_attempts(run_id,job_id,role,generation,ordinal)",
    "work_attempt_result_content": "CREATE INDEX work_attempt_result_content ON work_attempt_result_members(run_id,result_field,value_fingerprint,attempt_id)",
    "work_invocation_attempt": "CREATE INDEX work_invocation_attempt ON work_invocations(run_id,attempt_id,started_at)",
    "work_invocation_resume": "CREATE INDEX work_invocation_resume ON work_invocations(run_id,resume_id)",
    "work_invocation_history_subject": "CREATE INDEX work_invocation_history_subject ON work_invocation_history(run_id,work_subject,position)",
    "work_invocation_detail_retained": "CREATE INDEX work_invocation_detail_retained ON work_invocations(run_id,work_subject,started_at) WHERE details_retained=1",
    "work_job_attempt_pending": "CREATE INDEX work_job_attempt_pending ON work_job_attempt_roots(run_id,pending_attempt_id) WHERE pending_attempt_id IS NOT NULL",
}


def execute_values(connection: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> None:
    """Update one explicitly shaped domain record; callers define every column."""
    columns = ",".join(values)
    parameters = ",".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table}({columns}) VALUES({parameters})", tuple(values.values()))
