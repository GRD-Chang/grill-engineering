"""Minimal relational keys for the inactive schema, never a Core/Host story.

The profile bytes are intentionally opaque to these SQL-constraint tests. Do
not use this producer as an authorization, Invocation or lifecycle fixture.
"""
from hashlib import sha256
from pathlib import Path
import sqlite3

from agent_run.storage.repository_schema import REPOSITORY_RUN_SCHEMA


def connection(path: Path | str = ":memory:") -> sqlite3.Connection:
    database = sqlite3.connect(path, isolation_level=None)
    try:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        database.executescript(REPOSITORY_RUN_SCHEMA)
        return database
    except BaseException:
        database.close()
        raise


def insert(database: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    names = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    database.execute(f"INSERT INTO {table}({names}) VALUES ({placeholders})", tuple(values.values()))


def seed(database: sqlite3.Connection, run: str, *, subject: str = "parent:1") -> None:
    insert(database, "runs", {"id": run, "repository": "example/project", "parent_issue": 1,
        "parent_revision": "r", "graph_revision": "g", "accepted_parent_revision": "r", "concurrency": 1,
        "policy": b"{}", "language": "en", "repository_root": "/private/owned", "created_at": 100., "profile_revision": 1})
    add_subject(database, run, subject)
    body = b"original profile"
    insert(database, "material", {"run_id": run, "id": "profile", "subject": subject, "purpose": "profile",
        "digest": sha256(body).hexdigest(), "size": len(body), "body": body})
    insert(database, "profile_revisions", {"run_id": run, "id": 1, "subject": subject,
        "material_id": "profile", "created_at": 100.})


def add_subject(database: sqlite3.Connection, run: str, subject: str) -> None:
    insert(database, "subjects", {"run_id": run, "id": subject, "kind": "parent_only", "issue": 1,
        "revision": "r", "phase": "development", "purpose": "requirements", "source_ref": "refs/heads/work",
        "target_ref": "refs/heads/main", "base_sha": "a" * 40, "base_tree": "b" * 40, "source_sha": "a" * 40,
        "completion_revision": "no-tickets"})
