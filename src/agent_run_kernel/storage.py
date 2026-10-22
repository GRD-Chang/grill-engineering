"""SQLite durability, protocol refusal, bounded reads and immutable material.

Business choices live in Kernel. This module neither reconstitutes a mutable
Run nor offers a method per state field. A transaction presents SQL facts and
rolls back as a whole, including when a nested failure was caught by a caller.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence

from agent_run.sqlite_runtime import require_wal_runtime

from .model import KernelError

PREVIOUS_PROTOCOL = "independent-delivery-kernel-v17-exploration"
PROTOCOL = "independent-delivery-kernel-v18-exploration"
CONTINUATION_UPGRADE_SQL = (
    "ALTER TABLE attempts ADD COLUMN thread_id TEXT REFERENCES threads(id)",
    "ALTER TABLE attempts ADD COLUMN opening_id TEXT REFERENCES material(id)",
)
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
 source_sequence INTEGER, observed_parent_revision TEXT, observed_graph_revision TEXT,
 language TEXT CHECK(language IN ('zh','en')), repository_root TEXT, created_at REAL NOT NULL,
 profile_revision INTEGER NOT NULL DEFAULT 1 REFERENCES profile_revisions(id) DEFERRABLE INITIALLY DEFERRED
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
 refresh_revision TEXT, policy_id TEXT REFERENCES material(id)
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
 validation_checkout TEXT, validation_cleanup TEXT, workspace_sha TEXT NOT NULL, owned_root TEXT NOT NULL, common_git_dir TEXT NOT NULL,
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
 review_limit INTEGER, review_required INTEGER NOT NULL, current INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(subject,purpose,window)
);
CREATE UNIQUE INDEX current_budget ON budgets(subject,purpose) WHERE current=1;
CREATE TABLE resync_allowances (
 subject TEXT NOT NULL REFERENCES subjects(id), generation INTEGER NOT NULL,
 used INTEGER NOT NULL DEFAULT 0, allowance INTEGER NOT NULL CHECK(allowance>=0),
 current INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(subject,generation)
);
CREATE UNIQUE INDEX current_resync ON resync_allowances(subject) WHERE current=1;
CREATE TABLE integration_snapshots (
 subject TEXT NOT NULL REFERENCES subjects(id), window INTEGER NOT NULL,
 allowance_generation INTEGER NOT NULL, source_ref TEXT NOT NULL, target_ref TEXT NOT NULL,
 source_sha TEXT NOT NULL, target_sha TEXT NOT NULL, open INTEGER NOT NULL,
 PRIMARY KEY(subject,window), FOREIGN KEY(subject,allowance_generation) REFERENCES resync_allowances(subject,generation)
);
CREATE TABLE material (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), purpose TEXT NOT NULL,
 digest TEXT NOT NULL, size INTEGER NOT NULL, body BLOB NOT NULL,
 UNIQUE(subject,purpose,digest)
);
CREATE TABLE threads (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), role TEXT NOT NULL,
 profile_revision INTEGER NOT NULL REFERENCES profile_revisions(id), backend_id TEXT UNIQUE
);
CREATE TABLE profile_revisions (
 id INTEGER PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id),
 material_id TEXT NOT NULL REFERENCES material(id), created_at REAL NOT NULL
);
CREATE TABLE run_inputs (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), subject TEXT NOT NULL REFERENCES subjects(id),
 material_id TEXT NOT NULL REFERENCES material(id)
);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), generation INTEGER NOT NULL,
 ordinal INTEGER NOT NULL, role TEXT NOT NULL, purpose TEXT NOT NULL, window INTEGER NOT NULL,
 parent_revision TEXT NOT NULL, graph_revision TEXT NOT NULL, revision TEXT NOT NULL,
 base_sha TEXT NOT NULL, candidate_sha TEXT, candidate_tree TEXT, expected_tree TEXT,
 completion_revision TEXT NOT NULL, context_id TEXT NOT NULL REFERENCES material(id),
 current_invocation TEXT, result_id TEXT REFERENCES material(id), status TEXT NOT NULL,
 accounted INTEGER NOT NULL DEFAULT 0, outcome TEXT, new_thread INTEGER NOT NULL DEFAULT 0,
 resume_action TEXT REFERENCES actions(id), UNIQUE(subject,ordinal)
);
CREATE TABLE invocations (
 id TEXT PRIMARY KEY, attempt TEXT NOT NULL REFERENCES attempts(id), thread TEXT NOT NULL REFERENCES threads(id),
 executor TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL, allocated_at REAL NOT NULL,
 deadline_seconds REAL NOT NULL, deadline_at REAL NOT NULL,
 predecessor TEXT REFERENCES invocations(id), recovery_kind TEXT CHECK(recovery_kind IN ('ordinary','capacity','explicit')),
 action_id TEXT REFERENCES actions(id)
);
CREATE TABLE invocation_outputs (
 invocation TEXT NOT NULL REFERENCES invocations(id), ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 2),
 context_id TEXT NOT NULL REFERENCES material(id), lease TEXT NOT NULL UNIQUE,
 raw_id TEXT REFERENCES material(id),
 dispatch_state TEXT NOT NULL DEFAULT 'allocated', process_identity TEXT REFERENCES material(id),
 exit_code INTEGER, start_evidence TEXT REFERENCES material(id), allocated_at REAL NOT NULL,
 dispatched_at REAL, started_at REAL, returned_at REAL, exited_at REAL,
 mode TEXT NOT NULL, requested_thread TEXT,
 failure_kind TEXT CHECK(failure_kind IN ('ordinary','capacity','fatal')), recovery_ready_at REAL,
 PRIMARY KEY(invocation,ordinal)
);
CREATE TABLE worker_activity (
 invocation TEXT NOT NULL, ordinal INTEGER NOT NULL, identity TEXT NOT NULL,
 active INTEGER NOT NULL, observed_at REAL NOT NULL, evidence TEXT NOT NULL REFERENCES material(id),
 PRIMARY KEY(invocation,ordinal,identity), FOREIGN KEY(invocation,ordinal) REFERENCES invocation_outputs(invocation,ordinal)
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
CREATE TABLE hosts (
 run_id TEXT NOT NULL REFERENCES runs(id), epoch INTEGER NOT NULL, owner TEXT NOT NULL,
 nonce TEXT NOT NULL UNIQUE, action_id TEXT NOT NULL UNIQUE REFERENCES actions(id),
 subject TEXT NOT NULL REFERENCES subjects(id), request_id TEXT NOT NULL REFERENCES material(id),
 unit_name TEXT NOT NULL UNIQUE, allocated_at REAL NOT NULL, dispatched_at REAL,
 status TEXT NOT NULL CHECK(status IN ('allocated','dispatched','running','unknown','exited')),
 invocation_id TEXT, cgroup TEXT, boot_id TEXT, main_pid INTEGER, start_token TEXT,
 exit_status INTEGER, ended_at REAL, observation_id TEXT REFERENCES material(id),
 usage_ready INTEGER NOT NULL DEFAULT 0, usage_released INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(run_id,epoch), UNIQUE(run_id,owner)
);
CREATE TABLE waits (
 subject TEXT REFERENCES subjects(id), kind TEXT NOT NULL,
 scope_id TEXT NOT NULL REFERENCES material(id),
 started_at REAL NOT NULL, deadline REAL NOT NULL,
 retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at REAL,
 evidence TEXT REFERENCES material(id),
 PRIMARY KEY(scope_id,kind)
);
CREATE TABLE gates (
 subject TEXT NOT NULL REFERENCES subjects(id), code TEXT NOT NULL,
 purpose TEXT, evidence TEXT REFERENCES material(id), PRIMARY KEY(subject,code)
);
CREATE TABLE effects (
 id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES subjects(id), operation TEXT NOT NULL,
 status TEXT NOT NULL, executor TEXT, epoch INTEGER,
 parent_revision TEXT NOT NULL, graph_revision TEXT NOT NULL, revision TEXT NOT NULL,
 generation INTEGER NOT NULL, source_ref TEXT NOT NULL, target_ref TEXT NOT NULL,
 before_sha TEXT NOT NULL, after_sha TEXT, candidate_sha TEXT, candidate_tree TEXT,
 base_sha TEXT NOT NULL, base_tree TEXT NOT NULL, pr_number INTEGER, actor TEXT CHECK(actor IS NULL OR length(trim(actor))>0),
 payload_id TEXT NOT NULL REFERENCES material(id), receipt_id TEXT REFERENCES material(id),
 attempts INTEGER NOT NULL DEFAULT 0, retry_baseline INTEGER NOT NULL DEFAULT 0, control_action TEXT REFERENCES actions(id),
 CHECK((executor IS NOT NULL AND epoch IS NOT NULL) OR (status='confirmed' AND executor IS NULL AND epoch IS NULL)),
 CHECK(actor IS NOT NULL OR status='confirmed' OR operation NOT IN ('push','run_branch','pr_write','merge','completion_comment','close','issue_link','cleanup_remote_ref','retire_pr','reopen_ticket'))
);
CREATE INDEX control_effects ON effects(control_action);
CREATE TABLE effect_failures (
 effect TEXT NOT NULL REFERENCES effects(id), dispatch_serial INTEGER NOT NULL,
 evidence TEXT NOT NULL REFERENCES material(id), observed_at REAL NOT NULL,
 PRIMARY KEY(effect,dispatch_serial)
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
 kind TEXT NOT NULL, identity TEXT NOT NULL, material_id TEXT REFERENCES material(id),
 generation INTEGER NOT NULL, created_at REAL NOT NULL, phase TEXT, purpose TEXT, status TEXT
);
CREATE INDEX history_action_facts ON history(kind,identity);
"""
PREVIOUS_SCHEMA = SCHEMA
SCHEMA += '\n' + ';\n'.join(CONTINUATION_UPGRADE_SQL) + ';\n'


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

    def all(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Read a collection in physical pages within this transaction's budget."""
        result: list[sqlite3.Row] = []
        while True:
            page = self.rows(f"SELECT * FROM ({sql}) LIMIT 128 OFFSET ?", (*parameters, len(result)))
            result.extend(page)
            if len(page) < 128:
                return result

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
        require_wal_runtime()
        self.path = path
        if not path.is_file():
            raise KernelError("missing_database", str(path))
        with self.transaction(write=False):
            pass

    @classmethod
    def upgrade(cls, path: Path) -> None:
        """Upgrade only canonical v17 native storage, atomically and explicitly.

        Read-only consumers never invoke this. ALTER's official schema text is
        intentionally not compared with freshly CREATEd v18 text on reopen.
        Offline prepared classification owns its strict schema variants.
        """
        storage = cls(path)
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=1.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
            protocol = connection.execute('SELECT protocol FROM meta').fetchall()
            if len(protocol) != 1:
                raise KernelError('incompatible_database', str(path))
            if protocol[0][0] == PROTOCOL:
                connection.rollback()
                return
            if protocol[0][0] != PREVIOUS_PROTOCOL:
                raise KernelError('incompatible_database', str(path))
            with closing(sqlite3.connect(':memory:')) as expected:
                expected.executescript(PREVIOUS_SCHEMA)
                query = "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                if [tuple(row) for row in connection.execute(query)] != expected.execute(query).fetchall():
                    raise KernelError('continuation_upgrade_schema')
            storage._capacity(connection)
            if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise KernelError('continuation_upgrade_relations')
            for sql in CONTINUATION_UPGRADE_SQL:
                connection.execute(sql)
            from .attempt_continuation import backfill_native_continuation

            cursor = connection.execute('SELECT * FROM attempts ORDER BY subject,ordinal')
            while rows := cursor.fetchmany(128):
                for attempt in rows:
                    # One bounded row/material closure at a time; the whole
                    # database is capacity-bounded and still commits atomically.
                    tx = Transaction(connection)
                    backfill_native_continuation(tx, attempt)
                    if tx.failed:
                        raise KernelError('transaction_aborted')
            storage._capacity(connection)
            connection.execute('UPDATE meta SET protocol=? WHERE protocol=?', (PROTOCOL, PREVIOUS_PROTOCOL))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @classmethod
    def create(cls, path: Path) -> Storage:
        require_wal_runtime()
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
        require_wal_runtime()
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
            supported = {PROTOCOL} if write else {PROTOCOL, PREVIOUS_PROTOCOL}
            if len(protocol) != 1 or protocol[0][0] not in supported:
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
