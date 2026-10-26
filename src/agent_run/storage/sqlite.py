"""SQLite durability, protocol refusal, bounded reads and immutable material.

Business choices live in Kernel. This module neither reconstitutes a mutable
Run nor offers a method per state field. A transaction presents SQL facts and
rolls back as a whole, including when a nested failure was caught by a caller.
"""
from __future__ import annotations


from contextlib import ExitStack, contextmanager
import hashlib
import math
import mmap
from pathlib import Path
import sqlite3
import sys
from threading import Event
from time import monotonic, sleep
from typing import Any, Iterator, Sequence

from agent_run.storage.sqlite_runtime import require_wal_runtime

from agent_run.errors import KernelError
from agent_run.storage.preflight import PROTOCOL, UnsupportedState, read_state, require_supported_connection, require_supported_state
from agent_run.storage.save_order import (WRITE_CONTENTION_SECONDS, MAX_PENDING_WRITES, RepositorySave, SaveClock, SaveCommit,
    SaveNotHead, check_cancelled, database_identity, remaining, save_turn)

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
 resume_action TEXT REFERENCES actions(id),
 thread_id TEXT REFERENCES threads(id), opening_id TEXT REFERENCES material(id), UNIQUE(subject,ordinal)
);
CREATE TABLE invocations (
 id TEXT PRIMARY KEY, attempt TEXT NOT NULL REFERENCES attempts(id), thread TEXT NOT NULL REFERENCES threads(id),
 executor TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL, allocated_at REAL NOT NULL,
 deadline_seconds REAL NOT NULL, deadline_at REAL NOT NULL,
 predecessor TEXT REFERENCES invocations(id), recovery_kind TEXT CHECK(recovery_kind IN ('ordinary','capacity','explicit','semantic')),
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
 unit_name TEXT UNIQUE, backend TEXT NOT NULL CHECK(backend IN ('systemd','foreground')),
 domain_id TEXT REFERENCES material(id), allocated_at REAL NOT NULL, dispatched_at REAL,
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


class _SaveAdmissionBusy(RuntimeError):
    """BEGIN contention with confirmed rollback, before any callback."""


def _save_warning() -> None:
    try:
        print('Storage save cleanup unavailable; preserve original state', file=sys.stderr)
    except (OSError, ValueError):
        pass  # Closed diagnostic channels cannot undo a confirmed COMMIT.


@contextmanager
def _owned_connection(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Close this handle without replacing an exception from its own scope."""
    original: BaseException | None = None
    try:
        yield connection
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            connection.close()
        except (OSError, sqlite3.Error) as cleanup:
            if original is None or isinstance(original, (_SaveAdmissionBusy, SaveNotHead)):
                # Failed handle cleanup must not disappear into an internal wait/retry.
                raise
            original.add_note('SQLite connection cleanup unavailable: ' + type(cleanup).__name__)


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
    def __init__(self, path: Path, *, run_id: str | None = None, _repository: bool = False,
                 _namespace: tuple[str, str] | None = None) -> None:
        require_wal_runtime()
        if run_id is not None and (type(run_id) is not str or not run_id):
            raise KernelError("run_scope_invalid")
        self.path = path
        self._run_id = run_id
        self._repository_mode = _repository
        if _namespace is not None and not _repository:
            raise KernelError("repository_namespace_required")
        self._namespace = _namespace
        if not path.is_file():
            raise KernelError("missing_database", str(path))
        with self.transaction(write=False):
            pass

    @classmethod
    def create(cls, path: Path, *, _repository: bool = False,
               _namespace: tuple[str, str] | None = None) -> Storage:
        require_wal_runtime()
        if (_repository and _namespace is None) or (_namespace is not None and not _repository):
            raise KernelError("repository_namespace_required")
        if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
               for suffix in ("-wal", "-shm", "-journal")):
            raise UnsupportedState(path, "uncertain_state")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation refuses to adopt or upgrade any existing database.
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            if _repository:
                from agent_run.storage.repository_schema import (
                    REPOSITORY_RUN_SCHEMA, REPOSITORY_TASK_SCHEMA, REPOSITORY_ROUTING_SCHEMA, STAGED_REPOSITORY_PROTOCOL,
                )
                schema = REPOSITORY_RUN_SCHEMA + REPOSITORY_TASK_SCHEMA + REPOSITORY_ROUTING_SCHEMA
                protocol = STAGED_REPOSITORY_PROTOCOL
            else:
                schema, protocol = SCHEMA, PROTOCOL
            connection.executescript("BEGIN IMMEDIATE;\n" + schema)
            connection.execute("INSERT INTO meta VALUES (?)", (protocol,))
            if _repository:
                assert _namespace is not None
                connection.execute("INSERT INTO repository_binding VALUES (1,?,?)", _namespace)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return cls(path, _repository=True, _namespace=_namespace) if _repository else cls(path)

    @classmethod
    def _repository(cls, path: Path, *, run_id: str | None = None,
                    namespace: tuple[str, str] | None = None) -> Storage:
        """Internal staged opening only; public admission still rejects its marker."""
        return cls(path, run_id=run_id, _repository=True, _namespace=namespace)

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def at_run(self, run_id: str) -> Storage:
        """Select explicitly without mutating this handle or guessing current work."""
        return type(self)(self.path, run_id=run_id, _repository=self._repository_mode, _namespace=self._namespace)

    def _require_namespace(self, owner: Transaction) -> None:
        if not self._repository_mode:
            return
        # One immutable file namespace, not work state, a locator or a scan of
        # every historical Run. Canonical SQL constraints enforce row ownership.
        row = owner.one("SELECT singleton,repository,repository_root FROM repository_binding")
        if row[0] != 1 or self._namespace is not None and (row[1], row[2]) != self._namespace:
            raise KernelError("repository_namespace_changed")

    @contextmanager
    def transaction(self, *, write: bool = True, ordered: bool = True,
                    cancelled: Event | None = None, deadline: float | None = None) -> Iterator[Transaction]:
        require_wal_runtime()
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ValueError("A finite absolute transaction deadline is required")
        deadline = min(monotonic() + WRITE_CONTENTION_SECONDS, deadline) if deadline is not None else monotonic() + WRITE_CONTENTION_SECONDS
        with ExitStack() as scopes:
            identity = None
            if write:
                # Staged repository writes use only the durable queue below.
                # The old deque remains solely for the still-active v21 layout;
                # ordered=False retains its shared nested-owner/afterfork guard.
                identity = scopes.enter_context(save_turn(self.path, ordered=ordered and not self._repository_mode,
                    deadline=deadline, cancelled=cancelled))
                require_supported_state(self.path, timeout=remaining(deadline), **({'_repository': True} if self._repository_mode else {}))
                if database_identity(self.path) != identity:
                    raise UnsupportedState(self.path, "uncertain_state")
            if write and ordered and self._repository_mode:
                assert identity is not None
                with self._repository_save(identity, deadline, cancelled) as tx:
                    yield tx
            else:
                with self._transaction(write=write, identity=identity, deadline=deadline, cancelled=cancelled) as tx:
                    yield tx

    def _verify_save_database(self, identity: tuple[int, int], deadline: float) -> None:
        if database_identity(self.path) != identity:
            raise UnsupportedState(self.path, "uncertain_state")
        with read_state(self.path, timeout=remaining(deadline)) as connection:
            connection.row_factory = sqlite3.Row
            require_supported_connection(connection, self.path, _repository=True)
            self._require_namespace(Transaction(connection))
        if database_identity(self.path) != identity:
            raise UnsupportedState(self.path, "uncertain_state")

    @staticmethod
    def _save_pause(deadline: float, cancelled: Event | None) -> None:
        check_cancelled(cancelled)
        delay = min(remaining(deadline), 0.05)
        if cancelled is None:
            sleep(delay)
        else:
            cancelled.wait(delay)
        check_cancelled(cancelled)
        remaining(deadline)

    def _save_metadata(self, request: RepositorySave, identity: tuple[int, int],
                       cancelled: Event | None, operation: str, commit: SaveCommit | None = None) -> None:
        # Never call public transaction recursively or expose this metadata
        # owner to business code. Every retry has ended BEFORE any callback.
        while True:
            request.require()
            try:
                with self._transaction(write=True, identity=identity, deadline=request.deadline,
                        cancelled=cancelled, select_run=False, commit=commit, retry_admission=True) as tx:
                    if operation == 'register':
                        request.register(tx)
                    elif operation == 'reconcile':
                        request.reconcile(tx)
                    elif operation == 'discard':
                        request.discard(tx)
                    else:
                        raise ValueError('Unknown save metadata operation')
                return
            except _SaveAdmissionBusy:
                self._save_pause(request.deadline, cancelled)

    @contextmanager
    def _repository_save(self, identity: tuple[int, int], deadline: float,
                         cancelled: Event | None) -> Iterator[Transaction]:
        from agent_run.adapters.foreground import DomainUnknown
        try:
            clock = SaveClock()
        except (OSError, ValueError, DomainUnknown) as error:
            raise KernelError('storage_clock_domain', 'Comparable save clock is unavailable') from error
        request: RepositorySave | None = None
        original: BaseException | None = None
        may_be_registered = False
        try:
            request = RepositorySave(clock, self._run_id, deadline)
            registration = SaveCommit()
            try:
                self._save_metadata(request, identity, cancelled, 'register', registration)
                may_be_registered = True
            except (OSError, sqlite3.Error, KernelError) as error:
                may_be_registered = registration.attempted
                code = getattr(error, 'sqlite_errorcode', None)
                uncertain_io = (isinstance(error, OSError) and not isinstance(error, InterruptedError)
                    or isinstance(error, sqlite3.OperationalError) and type(code) is int
                    and (code & 255) == sqlite3.SQLITE_IOERR)
                if not registration.attempted or registration.confirmed or not uncertain_io:
                    raise  # FULL/INTERRUPT/cancel/protocol/capacity retain their original category.
                # COMMIT began: absence is never rollback. Reconcile only this
                # original UUID/position/binding; never INSERT it again.
                try:
                    self._save_metadata(request, identity, cancelled, 'reconcile')
                except (OSError, sqlite3.Error, KernelError) as unavailable:
                    failure = KernelError('storage_registration_unknown', 'Original save registration is unproven')
                    failure.add_note('Registration readback unavailable: ' + type(unavailable).__name__)
                    raise failure from error
            while True:
                try:
                    with self._transaction(write=True, identity=identity, deadline=deadline,
                            cancelled=cancelled, request=request, commit=request.commit, retry_admission=True) as tx:
                        request.body_entered = True
                        yield tx
                    return
                except (_SaveAdmissionBusy, SaveNotHead):
                    if request.body_entered:
                        raise  # An application callback is never replayed.
                    self._save_pause(deadline, cancelled)
                except (OSError, sqlite3.Error):
                    if not request.commit.confirmed:
                        raise
                    _save_warning()  # Only connection-close I/O remains after acknowledged COMMIT.
                    return
        except BaseException as error:
            original = error
            raise
        finally:
            try:
                if request is not None and may_be_registered and not request.commit.confirmed:
                    try:
                        # A set cancellation flag must not forbid exact cleanup;
                        # the original absolute budget still bounds acquisition.
                        self._save_metadata(request, identity, None, 'discard')
                    except (OSError, sqlite3.Error, KernelError) as cleanup:
                        if original is None:
                            raise
                        original.add_note('Save registration cleanup unavailable: ' + type(cleanup).__name__)
            finally:
                try:
                    clock.close()
                except OSError as cleanup:
                    if original is not None:
                        original.add_note('Save clock cleanup unavailable: ' + type(cleanup).__name__)
                    elif request is not None and request.commit.confirmed:
                        _save_warning()
                    else:
                        raise

    @contextmanager
    def _transaction(self, *, write: bool, identity: tuple[int, int] | None, deadline: float,
                     cancelled: Event | None, select_run: bool = True, request: RepositorySave | None = None,
                     commit: SaveCommit | None = None, retry_admission: bool = False) -> Iterator[Transaction]:
        if write and self._repository_mode:
            assert identity is not None
            self._verify_save_database(identity, deadline)
        with ExitStack() as scopes:
            scope = (_owned_connection(sqlite3.connect(self.path.absolute().as_uri() + "?mode=rw", uri=True,
                                            isolation_level=None, timeout=remaining(deadline))) if write else read_state(self.path))
            connection = scopes.enter_context(scope)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
            transaction = Transaction(connection)
            beginning = False
            try:
                if write:
                    check_cancelled(cancelled)
                    allowance = min(remaining(deadline), 0.05) if retry_admission else remaining(deadline)
                    connection.execute(f"PRAGMA busy_timeout={int(allowance * 1000)}")
                    beginning = True
                    connection.execute("BEGIN IMMEDIATE")
                    beginning = False
                    if database_identity(self.path) != identity:
                        raise UnsupportedState(self.path, "uncertain_state")
                if self._repository_mode:
                    require_supported_connection(connection, self.path, _repository=True)
                else:
                    require_supported_connection(connection, self.path)
                self._capacity(connection)
                self._require_namespace(transaction)
                if request is not None:
                    request.admit(transaction)
                exposed = transaction
                if self._repository_mode and self._run_id is not None and select_run:
                    from agent_run.storage.run_scope import RunTransaction
                    exposed = RunTransaction(transaction, self._run_id)
                elif not self._repository_mode and self._run_id is not None:
                    if transaction.one("SELECT id FROM runs")[0] != self._run_id:
                        raise KernelError("run_scope_changed")
                if write:
                    check_cancelled(cancelled)
                    remaining(deadline)
                yield exposed
                if transaction.failed:
                    raise KernelError("transaction_aborted", "a caught SQL failure poisoned this transaction")
                if write:
                    if database_identity(self.path) != identity:
                        raise UnsupportedState(self.path, "uncertain_state")
                    if self._repository_mode:
                        require_supported_connection(connection, self.path, _repository=True)
                    else:
                        require_supported_connection(connection, self.path)
                    self._require_namespace(transaction)
                    if request is not None:
                        request.finish(transaction)
                self._capacity(connection, committing=write)
                if commit is not None:
                    commit.attempted = True
                connection.commit()
                if commit is not None:
                    commit.confirmed = True
            except BaseException as error:
                rollback_complete = False
                try:
                    connection.rollback()
                    rollback_complete = not connection.in_transaction
                except (OSError, sqlite3.Error) as cleanup:
                    error.add_note('SQLite rollback unavailable: ' + type(cleanup).__name__)
                if isinstance(error, SaveNotHead) and not rollback_complete:
                    raise KernelError('transaction_aborted', 'Save admission rollback is unproven') from error
                busy = isinstance(error, sqlite3.OperationalError) and getattr(error, 'sqlite_errorcode', None) in (
                    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_RECOVERY,
                    sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_BUSY_TIMEOUT)
                if beginning and busy and retry_admission and rollback_complete:
                    raise _SaveAdmissionBusy('Save BEGIN contention ended before callback') from error
                if busy:
                    raise KernelError("storage_busy", "SQLite transaction is locked; retry the command") from error
                if isinstance(error, sqlite3.OperationalError):
                    code = getattr(error, "sqlite_errorcode", None)
                    if code in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_INTERRUPT):
                        diagnostic = "storage_limit" if code == sqlite3.SQLITE_FULL else "transaction_aborted"
                        raise KernelError(diagnostic, f"{error.sqlite_errorname}: {error}") from error
                raise

    def _capacity(self, connection: sqlite3.Connection, *, committing: bool = False) -> None:
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        database_size = page_count * page_size
        sizes = [path.stat().st_size if path.exists() else 0 for path in
                 (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"))]
        physical = sum(sizes)
        if committing:
            # Under BEGIN IMMEDIATE no other writer can append. Commit writes
            # at most one remaining frame per database page, plus a WAL header
            # and FULL-sync sector padding (SQLite clamps sectors to 64 KiB).
            # Already-spilled frames are included in the measured WAL size.
            frame_size = page_size + 24
            wal_size = sizes[1] + 32 + page_count * frame_size + 65536 + frame_size
            # WAL-index regions are 32 KiB: 4062 frames in the first, then 4096.
            frames = (wal_size - 32 + frame_size - 1) // frame_size
            shm_size = (1 + (frames + 33) // 4096) * 32768
            shm_unit = max(32768, mmap.PAGESIZE)
            shm_size = ((shm_size + shm_unit - 1) // shm_unit) * shm_unit
            # The writer lock does not exclude an older checkpoint. Reserve
            # its possible database growth within the existing database cap.
            physical += (wal_size - sizes[1] + max(0, shm_size - sizes[2])
                         + max(0, DATABASE_LIMIT - sizes[0]))
        if self._repository_mode:
            tickets = connection.execute("SELECT count(*) FROM save_admissions").fetchone()[0]
            if tickets > MAX_PENDING_WRITES:
                raise KernelError("storage_limit")
        if database_size > DATABASE_LIMIT or physical > PHYSICAL_LIMIT:
            raise KernelError("storage_limit")
