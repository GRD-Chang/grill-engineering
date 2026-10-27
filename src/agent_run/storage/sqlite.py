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
    SaveNotHead, check_cancelled, database_identity, remaining, write_owner)

MATERIAL_LIMIT = 4 * 1024 * 1024
CURRENT_LIMIT = 256 * 1024
DATABASE_LIMIT = 256 * 1024 * 1024
PHYSICAL_LIMIT = 1024 * 1024 * 1024
QUERY_LIMIT = 32 * 1024 * 1024

# The schema has one definition; early experimental layouts are refused.
from agent_run.storage.repository_schema import REPOSITORY_RUN_SCHEMA, REPOSITORY_TASK_SCHEMA, REPOSITORY_ROUTING_SCHEMA
SCHEMA = REPOSITORY_RUN_SCHEMA + REPOSITORY_TASK_SCHEMA + REPOSITORY_ROUTING_SCHEMA


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
        raise KernelError("run_scope_required", "Material requires an explicit Run transaction")

    def get(self, identity: str, subject: str) -> bytes:
        raise KernelError("run_scope_required", "Material requires an explicit Run transaction")


class Storage:
    def __init__(self, path: Path, *, run_id: str | None = None, _metadata: bool = False,
                 _namespace: tuple[str, str] | None = None) -> None:
        require_wal_runtime()
        if _metadata and run_id is not None or not _metadata and (type(run_id) is not str or not run_id):
            raise KernelError("run_scope_required")
        self.path, self._run_id, self._namespace = path, run_id, _namespace
        if not path.is_file():
            raise KernelError("missing_database", str(path))
        with self.transaction(write=False):
            pass

    @classmethod
    def create(cls, path: Path, *, namespace: tuple[str, str]) -> Storage:
        """Exclusively initialize an explicitly vacant repository, never adopt state."""
        require_wal_runtime()
        if any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
               for suffix in ("-wal", "-shm", "-journal")):
            raise UnsupportedState(path, "uncertain_state")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            connection.execute("INSERT INTO meta VALUES (?)", (PROTOCOL,))
            connection.execute("INSERT INTO repository_binding VALUES (1,?,?)", namespace)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return cls.metadata(path, namespace=namespace)

    @classmethod
    def metadata(cls, path: Path, *, namespace: tuple[str, str] | None = None) -> Storage:
        """Explicit repository metadata owner; never an implicit Run opening."""
        return cls(path, _metadata=True, _namespace=namespace)

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def at_run(self, run_id: str) -> Storage:
        """Select explicitly without mutating this handle or guessing current work."""
        return type(self)(self.path, run_id=run_id, _namespace=self._namespace)

    def _require_namespace(self, owner: Transaction) -> None:
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
                # One durable queue; this local guard only rejects reentry and
                # clears inherited ownership after fork. Control bypass remains explicit.
                identity = scopes.enter_context(write_owner(self.path,
                    deadline=deadline, cancelled=cancelled))
                require_supported_state(self.path, timeout=remaining(deadline))
                if database_identity(self.path) != identity:
                    raise UnsupportedState(self.path, "uncertain_state")
            if write and ordered:
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
            require_supported_connection(connection, self.path)
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
                        cancelled=cancelled, select_run=False, commit=commit, retry_admission=True,
                        clock=request.clock) as tx:
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
                     commit: SaveCommit | None = None, retry_admission: bool = False,
                     clock: SaveClock | None = None) -> Iterator[Transaction]:
        if write:
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
                require_supported_connection(connection, self.path)
                self._capacity(connection)
                self._require_namespace(transaction)
                if ((request is not None or clock is not None)
                        and connection.execute('PRAGMA journal_mode').fetchone()[0] != 'wal'):
                    raise KernelError('storage_clock_domain', 'Native save recovery requires the local WAL deployment contract')
                if request is not None:
                    request.admit(transaction)
                exposed = transaction
                if self._run_id is not None and select_run:
                    from agent_run.storage.run_scope import RunTransaction
                    exposed = RunTransaction(transaction, self._run_id)
                if write:
                    if request is not None:
                        request.require()  # Final body admission uses the same current-task clock evidence.
                    check_cancelled(cancelled)
                    remaining(deadline)
                yield exposed
                if transaction.failed:
                    raise KernelError("transaction_aborted", "a caught SQL failure poisoned this transaction")
                if write:
                    if database_identity(self.path) != identity:
                        raise UnsupportedState(self.path, "uncertain_state")
                    require_supported_connection(connection, self.path)
                    self._require_namespace(transaction)
                    if request is not None:
                        request.finish(transaction)
                self._capacity(connection, committing=write)
                if request is not None:
                    request.require()  # Recheck source evidence before business COMMIT, not its deadline.
                elif clock is not None:
                    clock.require()
                    check_cancelled(cancelled)
                    remaining(deadline)  # Metadata never receives a fresh contention allowance.
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
        tickets = connection.execute("SELECT count(*) FROM save_admissions").fetchone()[0]
        if tickets > MAX_PENDING_WRITES:
            raise KernelError("storage_limit")
        if database_size > DATABASE_LIMIT or physical > PHYSICAL_LIMIT:
            raise KernelError("storage_limit")
