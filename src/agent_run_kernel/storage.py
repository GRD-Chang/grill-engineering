"""SQLite durability, protocol refusal, bounded reads and immutable material.

Business choices live in Kernel. This module neither reconstitutes a mutable
Run nor offers a method per state field. A transaction presents SQL facts and
rolls back as a whole, including when a nested failure was caught by a caller.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence

from .model import KernelError

PROTOCOL = "independent-delivery-kernel-v3-exploration"
MATERIAL_LIMIT = 4 * 1024 * 1024
CURRENT_LIMIT = 256 * 1024
DATABASE_LIMIT = 256 * 1024 * 1024
PHYSICAL_LIMIT = 1024 * 1024 * 1024
QUERY_LIMIT = 32 * 1024 * 1024

SCHEMA = """
CREATE TABLE meta (protocol TEXT NOT NULL);
CREATE TABLE runs (
 id TEXT PRIMARY KEY, repository TEXT NOT NULL, parent_issue INTEGER NOT NULL,
 parent_revision TEXT NOT NULL, graph_revision TEXT NOT NULL, accepted_parent_revision TEXT NOT NULL,
 concurrency INTEGER NOT NULL CHECK(concurrency>0), policy BLOB NOT NULL,
 executor TEXT, epoch INTEGER NOT NULL DEFAULT 0, stopped INTEGER NOT NULL DEFAULT 0,
 final_action TEXT, integration_holder TEXT, next_ready INTEGER NOT NULL DEFAULT 0, blocked TEXT,
 source_sequence INTEGER, observed_parent_revision TEXT, observed_graph_revision TEXT
);
CREATE TABLE subjects (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
 issue INTEGER NOT NULL, revision TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
 version INTEGER NOT NULL DEFAULT 1, phase TEXT NOT NULL, purpose TEXT NOT NULL,
 source_ref TEXT NOT NULL, target_ref TEXT NOT NULL,
 base_sha TEXT NOT NULL, base_tree TEXT NOT NULL, source_sha TEXT NOT NULL,
 candidate_sha TEXT, candidate_tree TEXT, integrated_sha TEXT, integrated_tree TEXT,
 expected_tree TEXT, integration_target TEXT, completion_revision TEXT NOT NULL,
 current_attempt TEXT, review_attempt TEXT, publication_attempt TEXT,
 publication_sha TEXT, publication_tree TEXT, pr_number INTEGER,
 checks_id TEXT, grant_id TEXT, merge_sha TEXT, gate_mode TEXT,
 development_thread TEXT, ci_failure TEXT, last_failure TEXT
 ,checkout TEXT NOT NULL DEFAULT '', workspace_sha TEXT, resume_purpose TEXT, repair_source TEXT,
 validation_checkout TEXT, validation_cleanup TEXT, owned_root TEXT NOT NULL DEFAULT '', common_git_dir TEXT NOT NULL DEFAULT '',
 refresh_revision TEXT
);
CREATE TABLE dependencies (
 subject TEXT NOT NULL REFERENCES subjects(id), predecessor TEXT NOT NULL REFERENCES subjects(id),
 PRIMARY KEY(subject,predecessor)
);
CREATE TABLE requirement_versions (
 issue INTEGER NOT NULL, revision TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(issue,revision)
);
CREATE TABLE issue_facts (
 issue INTEGER PRIMARY KEY, revision TEXT NOT NULL, state TEXT NOT NULL, labels BLOB NOT NULL,
 position INTEGER NOT NULL
);
CREATE TABLE dependency_facts (
 issue INTEGER NOT NULL, blocker INTEGER NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(issue,blocker)
);
CREATE TABLE source_observations (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, parent_revision TEXT NOT NULL,
 graph_revision TEXT NOT NULL, body BLOB NOT NULL
);
CREATE TABLE final_target (
 id TEXT PRIMARY KEY, source_ref TEXT NOT NULL, target_ref TEXT NOT NULL, checkout TEXT NOT NULL,
 owned_root TEXT NOT NULL, common_git_dir TEXT NOT NULL
);
CREATE TABLE completions (
 subject TEXT NOT NULL REFERENCES subjects(id), generation INTEGER NOT NULL,
 revision TEXT NOT NULL, parent_revision TEXT NOT NULL, graph_revision TEXT NOT NULL,
 source_sha TEXT NOT NULL, merge_sha TEXT NOT NULL, tree TEXT NOT NULL,
 pr_number INTEGER NOT NULL, gate_mode TEXT NOT NULL, review_attempt TEXT, development_attempt TEXT,
 comment_effect TEXT NOT NULL REFERENCES effects(id), close_effect TEXT NOT NULL REFERENCES effects(id),
 PRIMARY KEY(subject,generation)
);
CREATE TABLE final_barriers (
 subject TEXT NOT NULL REFERENCES subjects(id), generation INTEGER NOT NULL,
 completion_revision TEXT NOT NULL, run_sha TEXT NOT NULL, default_sha TEXT NOT NULL,
 evidence TEXT NOT NULL REFERENCES material(id), PRIMARY KEY(subject,generation)
);
CREATE TABLE retired_generations (
 subject TEXT NOT NULL REFERENCES subjects(id), generation INTEGER NOT NULL, revision TEXT NOT NULL,
 source_ref TEXT NOT NULL, checkout TEXT NOT NULL, source_sha TEXT NOT NULL,
 candidate_sha TEXT, publication_sha TEXT, pr_number INTEGER, reason TEXT NOT NULL,
 validation_checkout TEXT, validation_cleanup TEXT,
 PRIMARY KEY(subject,generation)
);
CREATE TABLE resource_cleanup (
 subject TEXT NOT NULL REFERENCES subjects(id), generation INTEGER NOT NULL,
 source_ref TEXT NOT NULL, source_sha TEXT NOT NULL, checkout TEXT NOT NULL, workspace_sha TEXT NOT NULL,
 owned_root TEXT NOT NULL, common_git_dir TEXT NOT NULL, stage INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'pending', effect_id TEXT REFERENCES effects(id), outcome TEXT,
 PRIMARY KEY(subject,generation)
);
CREATE TABLE issue_links (
 subject TEXT PRIMARY KEY REFERENCES subjects(id), generation INTEGER NOT NULL,
 source_ref TEXT NOT NULL, source_sha TEXT NOT NULL, issue INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', effect_id TEXT NOT NULL REFERENCES effects(id)
);
CREATE TABLE budgets (
 subject TEXT NOT NULL REFERENCES subjects(id), purpose TEXT NOT NULL,
 window INTEGER NOT NULL, developments_used INTEGER NOT NULL DEFAULT 0,
 reviews_used INTEGER NOT NULL DEFAULT 0, development_limit INTEGER NOT NULL,
 review_limit INTEGER, current INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(subject,purpose,window)
);
CREATE UNIQUE INDEX current_budget ON budgets(subject,purpose) WHERE current=1;
CREATE TABLE material (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), purpose TEXT NOT NULL,
 digest TEXT NOT NULL, size INTEGER NOT NULL, body BLOB NOT NULL,
 UNIQUE(subject,purpose,digest)
);
CREATE TABLE threads (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), role TEXT NOT NULL,
 model TEXT NOT NULL, effort TEXT NOT NULL, backend_id TEXT UNIQUE
);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), generation INTEGER NOT NULL,
 ordinal INTEGER NOT NULL, role TEXT NOT NULL, purpose TEXT NOT NULL, window INTEGER NOT NULL,
 parent_revision TEXT NOT NULL, graph_revision TEXT NOT NULL, revision TEXT NOT NULL,
 base_sha TEXT NOT NULL, candidate_sha TEXT, candidate_tree TEXT, expected_tree TEXT,
 completion_revision TEXT NOT NULL, context_id TEXT NOT NULL REFERENCES material(id),
 current_invocation TEXT, result_id TEXT REFERENCES material(id), status TEXT NOT NULL,
 accounted INTEGER NOT NULL DEFAULT 0, outcome TEXT, new_thread INTEGER NOT NULL DEFAULT 0, UNIQUE(subject,ordinal)
);
CREATE TABLE invocations (
 id TEXT PRIMARY KEY, attempt TEXT NOT NULL REFERENCES attempts(id), thread TEXT NOT NULL REFERENCES threads(id),
 context_id TEXT NOT NULL REFERENCES material(id),
 executor TEXT NOT NULL, epoch INTEGER NOT NULL, lease TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL, raw_id TEXT REFERENCES material(id), output_repairs INTEGER NOT NULL DEFAULT 0,
 dispatch_state TEXT NOT NULL DEFAULT 'allocated', process_identity TEXT REFERENCES material(id),
 exit_code INTEGER, start_evidence TEXT REFERENCES material(id)
);
CREATE TABLE work_queue (
 subject TEXT PRIMARY KEY REFERENCES subjects(id), ready INTEGER NOT NULL UNIQUE
);
CREATE TABLE integration_queue (
 subject TEXT PRIMARY KEY REFERENCES subjects(id), ready INTEGER NOT NULL UNIQUE
);
CREATE TABLE leases (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL UNIQUE REFERENCES subjects(id),
 invocation TEXT NOT NULL UNIQUE REFERENCES invocations(id), executor TEXT NOT NULL, epoch INTEGER NOT NULL
);
CREATE TABLE retired_executors (
 run_id TEXT NOT NULL REFERENCES runs(id), owner TEXT NOT NULL, epoch INTEGER NOT NULL,
 status TEXT NOT NULL, PRIMARY KEY(run_id,owner,epoch)
);
CREATE TABLE waits (
 subject TEXT NOT NULL REFERENCES subjects(id), kind TEXT NOT NULL, deadline REAL NOT NULL,
 PRIMARY KEY(subject,kind)
);
CREATE TABLE gates (
 subject TEXT NOT NULL REFERENCES subjects(id), code TEXT NOT NULL,
 purpose TEXT, evidence TEXT REFERENCES material(id), PRIMARY KEY(subject,code)
);
CREATE TABLE effects (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), operation TEXT NOT NULL,
 status TEXT NOT NULL, executor TEXT NOT NULL, epoch INTEGER NOT NULL,
 parent_revision TEXT NOT NULL, graph_revision TEXT NOT NULL, revision TEXT NOT NULL,
 generation INTEGER NOT NULL, source_ref TEXT NOT NULL, target_ref TEXT NOT NULL,
 before_sha TEXT NOT NULL, after_sha TEXT, candidate_sha TEXT, candidate_tree TEXT,
 base_sha TEXT NOT NULL, base_tree TEXT NOT NULL, pr_number INTEGER, actor TEXT NOT NULL,
 payload_id TEXT NOT NULL REFERENCES material(id), receipt_id TEXT REFERENCES material(id),
 attempts INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX pending_effect ON effects(subject) WHERE status IN ('prepared','sent','observed') AND operation NOT IN ('cleanup_worktree','cleanup_local_ref','cleanup_remote_ref','issue_link');
CREATE TABLE checks (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), pr_number INTEGER NOT NULL,
 head_sha TEXT NOT NULL, result TEXT NOT NULL, material_id TEXT NOT NULL REFERENCES material(id)
);
CREATE TABLE grants (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), action TEXT NOT NULL,
 pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL, base_sha TEXT NOT NULL,
 review_attempt TEXT NOT NULL, checks_id TEXT NOT NULL, revision TEXT NOT NULL,
 parent_revision TEXT NOT NULL, graph_revision TEXT NOT NULL, completion_revision TEXT NOT NULL
);
CREATE TABLE actions (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
 subject TEXT REFERENCES subjects(id), payload_id TEXT REFERENCES material(id), status TEXT NOT NULL,
 receipt_id TEXT REFERENCES material(id)
);
CREATE TABLE history (
 position INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL REFERENCES subjects(id),
 kind TEXT NOT NULL, identity TEXT NOT NULL, material_id TEXT REFERENCES material(id)
);
"""


class Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.bytes_read = 0
        self.rows_read = 0
        self.failed = False

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        try:
            return self.connection.execute(sql, parameters)
        except BaseException:
            self.failed = True
            raise

    def rows(self, sql: str, parameters: Sequence[Any] = (), *, limit: int = 128) -> list[sqlite3.Row]:
        if not 1 <= limit <= 128:
            raise KernelError("query_limit")
        description = self.execute(f"SELECT * FROM ({sql}) LIMIT 0", parameters).description
        if description is None:
            raise KernelError("query_shape")
        columns = ['"' + field[0].replace('"', '""') + '"' for field in description]
        sizes = ",".join(f"CASE WHEN typeof({column}) IN ('text','blob') THEN length(CAST({column} AS BLOB)) ELSE 16 END" for column in columns)
        # SQLite measures bytes before any selected body crosses into Python.
        headers = self.execute(f"SELECT {sizes} FROM ({sql}) LIMIT ?", (*parameters, limit + 1)).fetchall()
        if len(headers) > limit:
            raise KernelError("query_limit", "request a bounded history page")
        byte_count = sum(sum(value or 0 for value in header) for header in headers)
        if self.rows_read + len(headers) > 65536 or self.bytes_read + byte_count > QUERY_LIMIT:
            raise KernelError("query_limit")
        rows = self.execute(f"SELECT * FROM ({sql}) LIMIT ?", (*parameters, limit)).fetchall()
        self.rows_read += len(rows)
        self.bytes_read += byte_count
        return rows

    def one(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row:
        rows = self.rows(sql, parameters, limit=1)
        if len(rows) != 1:
            raise KernelError("missing_or_ambiguous_fact")
        return rows[0]

    def put(self, subject: str, purpose: str, body: bytes) -> str:
        if len(body) > MATERIAL_LIMIT:
            raise KernelError("material_limit")
        digest = hashlib.sha256(body).hexdigest()
        identity = hashlib.sha256(f"{subject}\0{purpose}\0{digest}".encode()).hexdigest()
        self.execute("INSERT OR IGNORE INTO material VALUES (?,?,?,?,?,?)",
                     (identity, subject, purpose, digest, len(body), body))
        # An existing digest never licenses trusting damaged or foreign bytes.
        if self.get(identity, subject) != body:
            raise KernelError("material_corrupt")
        return identity

    def get(self, identity: str, subject: str) -> bytes:
        header = self.one("SELECT subject,digest,size,typeof(body),length(body) FROM material WHERE id=?", (identity,))
        if (header[0] != subject or header[3] != "blob" or header[2] != header[4]
                or not 0 <= header[4] <= MATERIAL_LIMIT):
            raise KernelError("material_corrupt", identity)
        if self.bytes_read + header[4] > QUERY_LIMIT:
            raise KernelError("query_limit")
        body = self.one("SELECT body FROM material WHERE id=?", (identity,))[0]
        if hashlib.sha256(body).hexdigest() != header[1]:
            raise KernelError("material_corrupt", identity)
        return bytes(body)


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.is_file():
            raise KernelError("missing_database", str(path))
        with self.transaction(write=False):
            pass

    @classmethod
    def create(cls, path: Path) -> Storage:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation refuses to adopt or upgrade any existing database.
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            connection.execute("INSERT INTO meta VALUES (?)", (PROTOCOL,))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return cls(path)

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[Transaction]:
        uri = self.path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
        connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        if not write:
            connection.execute("PRAGMA query_only=ON")
        transaction = Transaction(connection)
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                protocol = connection.execute("SELECT protocol FROM meta").fetchall()
            except sqlite3.Error as error:
                raise KernelError("incompatible_database", str(self.path)) from error
            if len(protocol) != 1 or protocol[0][0] != PROTOCOL:
                raise KernelError("incompatible_database", str(self.path))
            self._capacity(connection)
            yield transaction
            if transaction.failed:
                raise KernelError("transaction_aborted", "a caught SQL failure poisoned this transaction")
            self._capacity(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _capacity(self, connection: sqlite3.Connection) -> None:
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        physical = sum(path.stat().st_size for path in (self.path, Path(str(self.path) + "-wal"),
                                                      Path(str(self.path) + "-shm")) if path.exists())
        if page_count * page_size > DATABASE_LIMIT or physical > PHYSICAL_LIMIT:
            raise KernelError("storage_limit")
