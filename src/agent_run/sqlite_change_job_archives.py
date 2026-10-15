"""Exact archive summaries used without reading unrelated historical entries."""
from __future__ import annotations

import hashlib
import sqlite3

from agent_run.sqlite_encoding import encode_json


TABLES = {
    "work_job_archive_roots": "CREATE TABLE work_job_archive_roots (run_id TEXT NOT NULL, job_id TEXT NOT NULL, "
        "history_kind TEXT NOT NULL, item_count INTEGER NOT NULL, logical_bytes INTEGER NOT NULL, size_proof TEXT NOT NULL, "
        "PRIMARY KEY(run_id, job_id, history_kind), "
        "FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
}


def _proof(run_id: str, job_id: str, field: str, size: int, count: int) -> str:
    return hashlib.sha256(encode_json((run_id, job_id, field, size, count)).encode()).hexdigest()


def save_metadata(c: sqlite3.Connection, run_id: str, job_id: str, field: str, size: int, count: int) -> None:
    c.execute("INSERT INTO work_job_archive_roots VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,job_id,history_kind) "
              "DO UPDATE SET item_count=excluded.item_count,logical_bytes=excluded.logical_bytes,size_proof=excluded.size_proof",
              (run_id, job_id, field, count, size, _proof(run_id, job_id, field, size, count)))


def read_metadata(c: sqlite3.Connection, run_id: str, job_id: str, field: str, *, required: bool = True) -> tuple[int, int]:
    row = c.execute("SELECT logical_bytes,item_count,size_proof FROM work_job_archive_roots "
                    "WHERE run_id=? AND job_id=? AND history_kind=?", (run_id, job_id, field)).fetchone()
    if row is None:
        if required:
            raise ValueError("Missing Change Job history size summary")
        return 2, 0
    size, count, proof = row
    if (type(size) is not int or type(count) is not int or count < 0 or size < 2 + max(count - 1, 0)
            or count == 0 and size != 2 or proof != _proof(run_id, job_id, field, size, count)):
        raise ValueError("Invalid Change Job history size summary")
    return size, count


def require_metadata(c: sqlite3.Connection, run_id: str, job_id: str, field: str, total: int, count: int) -> None:
    if read_metadata(c, run_id, job_id, field) != (total + 2 + max(count - 1, 0), count):
        raise ValueError("Invalid Change Job history size summary")
