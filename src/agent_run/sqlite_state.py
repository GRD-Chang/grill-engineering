"""Single-authority SQLite object storage for new Delivery Runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import weakref
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, cast

from agent_run.state_errors import IncompatibleRunStateError
from agent_run.sqlite_encoding import copy_json_tree, copy_projection_fields, encode_json, object_path, scalar_value, snapshot_fields
from agent_run.sqlite_mutations import create_run_objects, save_work
from agent_run import sqlite_timeline
from agent_run.sqlite_work_objects import check_indexes, load_work, index_run, _object_rows, _decode
from agent_run.sqlite_work_indexes import IndexFacts
from agent_run.sqlite_work_fields import field_node_counts
from agent_run.sqlite_rows import ReadConnection, RowSource, read_document, read_run_rows, read_schema
from agent_run.sqlite_job_history import FIELDS as JOB_HISTORIES, CURRENT_WORK_PREDICATE


_PROTOCOL = "sqlite-run-objects-v2"
_MATERIAL_THRESHOLD = 1024
_SNAPSHOT_FIELDS = {"prompt_resources", "review_context", "parent", "ticket", "issue_snapshot"}
_OBJECT_COLLECTIONS = {"ticket_jobs", "ticket_agent_invocations", "agent_invocation_history",
                       "timeline", "timeline_continuation"}
_CURRENT_OBJECTS = {"active_ticket_job", "active_agent_invocation"}
_READ_VIEW_BYTES = 512 * 1024
_VALID_SCHEMAS: set[tuple[tuple[str, str, str, str | None], ...]] = set()
_SCHEMA_CACHE_LOCK = threading.Lock()
_COLUMNS = {
    "schema_meta": ("key", "value"),
    "runs": ("run_id", "repository", "parent_number", "status", "phase", "created_at", "updated_at", "root_keys_json"),
    "run_objects": ("run_id", "object_path", "kind", "value_json"),
    "run_materials": ("material_id", "value", "byte_length"),
    "run_material_refs": ("run_id", "material_id", "object_refcount"),
    "run_object_index": ("run_id", "object_path", "invocation_id", "resume_id", "logical_bytes", "content_digest", "size_proof", "child_count", "attempt_id"),
    "run_object_materials": ("run_id", "object_path", "material_id"),
    "run_sizes": ("run_id", "logical_bytes", "canonical_count"),
    "run_history": ("run_id", "position", "ticket_number", "work_subject", "started_at", "resume_id", "logical_bytes", "detailed"),
    "run_object_material_refs": ("run_id", "object_path", "material_id"),
    "run_job_history": ("run_id", "object_path", "logical_bytes", "item_count"),
    "run_job_attempts": ("run_id", "object_path", "parent_path", "position", "role", "status", "ordinal"),
}
_KEYS = {
    "schema_meta": ("key",),
    "runs": ("run_id",),
    "run_objects": ("run_id", "object_path"),
    "run_materials": ("material_id",),
    "run_material_refs": ("run_id", "material_id"),
    "run_object_index": ("run_id", "object_path"),
    "run_object_materials": ("run_id", "object_path", "material_id"),
    "run_sizes": ("run_id",),
    "run_history": ("run_id", "position"),
    "run_object_material_refs": ("run_id", "object_path", "material_id"),
    "run_job_history": ("run_id", "object_path"),
    "run_job_attempts": ("run_id", "object_path"),
}
_FOREIGN_KEYS = {
    "run_objects": {("runs", "run_id", "run_id", "CASCADE")},
    "run_object_index": {("runs", "run_id", "run_id", "CASCADE")},
    "run_object_materials": {("runs", "run_id", "run_id", "CASCADE")},
    "run_material_refs": {
        ("runs", "run_id", "run_id", "CASCADE"),
        ("run_materials", "material_id", "material_id", "NO ACTION"),
    },
    "run_sizes": {("runs", "run_id", "run_id", "CASCADE")},
    "run_history": {("runs", "run_id", "run_id", "CASCADE")},
    "run_object_material_refs": {
        ("run_objects", "run_id", "run_id", "CASCADE"),
        ("run_objects", "object_path", "object_path", "CASCADE"),
        ("run_materials", "material_id", "material_id", "NO ACTION"),
    },
    "run_job_history": {
        ("run_objects", "run_id", "run_id", "CASCADE"),
        ("run_objects", "object_path", "object_path", "CASCADE"),
    },
    "run_job_attempts": {
        ("run_objects", "run_id", "run_id", "CASCADE"),
        ("run_objects", "object_path", "object_path", "CASCADE"),
    },
}
_SCHEMA = (
    "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, repository TEXT, "
    "parent_number INTEGER, status TEXT, phase TEXT, created_at TEXT, updated_at TEXT, root_keys_json TEXT NOT NULL)",
    "CREATE TABLE run_objects (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
    "kind TEXT NOT NULL CHECK (kind IN ('map', 'list', 'scalar', 'material', 'snapshot')), "
    "value_json TEXT NOT NULL, PRIMARY KEY (run_id, object_path), "
    "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "CREATE TABLE run_materials (material_id TEXT PRIMARY KEY, value TEXT NOT NULL, "
    "byte_length INTEGER NOT NULL)",
    "CREATE TABLE run_material_refs (run_id TEXT NOT NULL, material_id TEXT NOT NULL, object_refcount INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY (run_id, material_id), "
    "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE, "
    "FOREIGN KEY (material_id) REFERENCES run_materials(material_id))",
    "CREATE TABLE run_object_index (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
    "invocation_id TEXT, resume_id TEXT, logical_bytes INTEGER NOT NULL, content_digest TEXT, size_proof TEXT, child_count INTEGER, attempt_id TEXT, "
    "PRIMARY KEY (run_id, object_path), FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "CREATE TABLE run_object_materials (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
    "material_id TEXT NOT NULL, PRIMARY KEY (run_id, object_path, material_id), "
    "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "CREATE TABLE run_sizes (run_id TEXT PRIMARY KEY, logical_bytes INTEGER NOT NULL, canonical_count INTEGER NOT NULL, "
    "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "CREATE TABLE run_history (run_id TEXT NOT NULL, position INTEGER NOT NULL, "
    "ticket_number INTEGER, work_subject TEXT, started_at TEXT, resume_id TEXT, "
    "logical_bytes INTEGER NOT NULL, detailed INTEGER NOT NULL, PRIMARY KEY (run_id, position), "
    "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "CREATE TABLE run_object_material_refs (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
    "material_id TEXT NOT NULL, PRIMARY KEY (run_id, object_path, material_id), "
    "FOREIGN KEY (run_id, object_path) REFERENCES run_objects(run_id, object_path) ON DELETE CASCADE, "
    "FOREIGN KEY (material_id) REFERENCES run_materials(material_id))",
    "CREATE TABLE run_job_history (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
    "logical_bytes INTEGER NOT NULL, item_count INTEGER NOT NULL, PRIMARY KEY (run_id, object_path), "
    "FOREIGN KEY (run_id, object_path) REFERENCES run_objects(run_id, object_path) ON DELETE CASCADE)",
    "CREATE TABLE run_job_attempts (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
    "parent_path TEXT NOT NULL, position INTEGER NOT NULL, role TEXT, status TEXT, ordinal INTEGER, "
    "PRIMARY KEY (run_id, object_path), FOREIGN KEY (run_id, object_path) "
    "REFERENCES run_objects(run_id, object_path) ON DELETE CASCADE)",
    "CREATE INDEX current_field_sizes ON run_object_index(run_id, logical_bytes) WHERE logical_bytes > 0",
    "CREATE INDEX run_invocation_identity ON run_object_index(run_id, invocation_id)",
    "CREATE INDEX run_attempt_identity ON run_object_index(run_id, attempt_id)",
    "CREATE INDEX run_resume_identity ON run_object_index(run_id, resume_id)",
    "CREATE INDEX object_material_identity ON run_object_materials(run_id, material_id)",
    "CREATE INDEX runs_parent ON runs(repository COLLATE NOCASE, parent_number, created_at)",
    "CREATE INDEX history_ticket ON run_history(run_id, ticket_number, position)",
    "CREATE INDEX history_resume ON run_history(run_id, resume_id, position)",
    "CREATE INDEX history_identity ON run_history(run_id, work_subject, started_at, position)",
    "CREATE INDEX history_detail ON run_history(run_id, detailed, position)",
    "CREATE INDEX object_material ON run_object_material_refs(material_id, run_id)",
    "CREATE INDEX job_attempt_role ON run_job_attempts(run_id, parent_path, role, status, ordinal, position)",
    "CREATE INDEX current_work_objects ON run_objects(run_id, object_path) WHERE " + CURRENT_WORK_PREDICATE,
    "CREATE INDEX material_run_refs ON run_material_refs(material_id, run_id)",
)
_CANONICAL_TABLES = dict(zip(_COLUMNS, _SCHEMA[:len(_COLUMNS)], strict=True))
_CANONICAL_INDEXES = {statement.split()[2]: statement for statement in _SCHEMA[len(_COLUMNS):]}
_WORK_SCHEMA_PARAMETERS = {
    "schema_definitions": encode_json(sorted(
        [("table", name, ddl) for name, ddl in _CANONICAL_TABLES.items()]
        + [("index", name, ddl) for name, ddl in _CANONICAL_INDEXES.items()],
    )),
    "schema_protocol": _PROTOCOL,
}


_json = encode_json


@dataclass(frozen=True)
class _RunView:
    run_id: str
    state: dict[str, Any]
    objects: dict[str, tuple[str, str]]
    materials: dict[str, str]
    logical_bytes: int
    fields: dict[str, str] | None
    snapshots: dict[str, str]
    snapshot_refs: dict[str, frozenset[str]]
    index_facts: IndexFacts | None = None
    physical_bytes: int | None = None

    @cached_property
    def field_node_counts(self) -> dict[str, int]:
        return field_node_counts(self.objects)

    @cached_property
    def timeline_sources(self) -> Mapping[str, sqlite_timeline.ScopeSource]:
        return sqlite_timeline.scopes_from_view(self)


@dataclass(frozen=True)
class _ReadView:
    view: _RunView
    source: RowSource


@dataclass
class _WorkConnections:
    identity: tuple[int, int] | None = None
    reader: ReadConnection | None = None
    writer: sqlite3.Connection | None = None

    def close(self) -> None:
        reader, writer = self.reader, self.writer
        self.reader = self.writer = None
        self.identity = None
        try:
            if reader is not None:
                reader.close()
        finally:
            if writer is not None:
                writer.close()


class SQLiteStateRepository:
    """Persist keyed current objects without a second JSON authority.

    Mapping keys and list positions form stable object paths. Only changed
    nodes are written; a shifted bounded history list may change multiple rows.
    ``atomic`` lets a caller read the latest Run and patch objects in one real
    transaction. Connections belong to one boundary or a scoped Ticket delivery;
    they are never shared across threads or left in a transaction between calls.
    WAL preserves committed facts after process exit without a reader repairing
    the business database. Read-only SQLite may update the temporary ``-shm``
    coordination index; it never checkpoints the database or writes Run facts.
    """

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        self.root = root
        self.path = root / "state.sqlite3"
        self.max_bytes = max_bytes
        self._local = threading.local()
        self._read_view: _ReadView | None = None
        self._read_view_lock = threading.Lock()
        self._aggregate_rows = True

    @contextmanager
    def work_session(self) -> Iterator[None]:
        """Reuse only prepared statements during this thread's Ticket work."""
        if getattr(self._local, "work_connections", None) is not None:
            yield
            return
        connections = _WorkConnections()
        self._local.work_connections = connections
        try:
            yield
        finally:
            self._local.work_connections = None
            connections.close()

    def _work_connection(self, *, readonly: bool) -> sqlite3.Connection | None:
        connections = cast(_WorkConnections, self._local.work_connections)
        try:
            stat = self.path.stat()
            identity: tuple[int, int] | None = stat.st_dev, stat.st_ino
        except FileNotFoundError:
            identity = None
        if identity != connections.identity:
            connections.close()
        if readonly:
            if identity is None:
                return None
            if connections.reader is None:
                connections.reader = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro", uri=True,
                    timeout=5.0, isolation_level=None, factory=ReadConnection,
                )
                connections.reader.execute("PRAGMA query_only = ON")
            connection: sqlite3.Connection = connections.reader
        else:
            if connections.writer is None:
                connections.writer = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            connection = connections.writer
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            connections.close()
            raise
        opened_identity = stat.st_dev, stat.st_ino
        if identity is not None and identity != opened_identity:
            connections.close()
            raise OSError("SQLite Run database changed while opening a work connection")
        connections.identity = opened_identity
        return connection

    @staticmethod
    def _check_schema(connection: sqlite3.Connection, *, allow_empty: bool = False) -> bool:
        definitions, metadata = read_schema(connection)
        tables = {
            name for kind, name, _, _ in definitions
            if kind == "table" and not name.startswith("sqlite_")
        }
        if any(kind in {"trigger", "view"} for kind, _, _, _ in definitions):
            raise IncompatibleRunStateError("Incompatible SQLite Run state schema")
        if not tables and allow_empty:
            return False
        if tables != set(_COLUMNS):
            raise IncompatibleRunStateError("Incompatible SQLite Run state schema")
        if metadata is None:
            metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
        if metadata != {"protocol": _PROTOCOL}:
            raise IncompatibleRunStateError("Incompatible SQLite Run state protocol")
        indexes = {name: sql for kind, name, _, sql in definitions if kind == "index"}
        if any(indexes.get(name) != sql for name, sql in _CANONICAL_INDEXES.items()):
            raise IncompatibleRunStateError("Incompatible SQLite Run state indexes")
        if isinstance(connection, ReadConnection) and connection.schema_facts is not None:
            # read_document proved these exact table definitions against the
            # creator's DDL in the same SQL snapshot as all returned Run facts.
            return True
        # PRAGMA column/key facts derive from these exact definitions. Reuse
        # their validation, while still reading all current DDL and protocol
        # facts in this connection's snapshot on every call.
        with _SCHEMA_CACHE_LOCK:
            if definitions in _VALID_SCHEMAS:
                return True
        for name, expected in _COLUMNS.items():
            info = list(connection.execute(f"PRAGMA table_info({name})"))
            keys = tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])
            foreign_keys = {
                (row[2], row[3], row[4], row[6])
                for row in connection.execute(f"PRAGMA foreign_key_list({name})")
            }
            if (tuple(row[1] for row in info) != expected or keys != _KEYS[name]
                    or foreign_keys != _FOREIGN_KEYS.get(name, set())):
                raise IncompatibleRunStateError("Incompatible SQLite Run state schema")
            for row in info:
                expected_type = "INTEGER" if row[1] in {"parent_number", "byte_length", "logical_bytes", "object_refcount", "child_count", "position", "ticket_number", "detailed", "canonical_count", "item_count", "ordinal"} else "TEXT"
                nullable = ((name == "runs" and row[1] != "root_keys_json") or row[1] == "key"
                            or name in {"run_materials", "run_sizes"} and row[1] in {"material_id", "run_id"}
                            or name == "run_object_index" and row[1] in {"invocation_id", "resume_id", "content_digest", "size_proof", "child_count", "attempt_id"}
                            or name == "run_history" and row[1] in {"ticket_number", "work_subject", "started_at", "resume_id"}
                            or name == "run_job_attempts" and row[1] in {"role", "status", "ordinal"})
                if row[2] != expected_type or bool(row[3]) == nullable:
                    raise IncompatibleRunStateError("Incompatible SQLite Run state schema")
        if sum(len(text or "") for _, _, _, text in definitions) <= 16 * 1024:
            with _SCHEMA_CACHE_LOCK:
                if len(_VALID_SCHEMAS) >= 16:
                    _VALID_SCHEMAS.clear()
                _VALID_SCHEMAS.add(definitions)
        return True

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Commit all nested repository calls together, or roll them back.

        Reentry joins the current transaction; it is not an independent commit
        or savepoint. A caught inner failure marks the whole transaction failed.
        """
        active = getattr(self._local, "connection", None)
        if active is not None:
            try:
                yield
            except BaseException:
                self._local.failed = True
                raise
            return
        self.root.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        session = getattr(self._local, "work_connections", None)
        try:
            connection = (self._work_connection(readonly=False) if session is not None
                          else sqlite3.connect(self.path, timeout=5.0, isolation_level=None))
            assert connection is not None
            initialized = self._check_schema(connection, allow_empty=True)
            # Reapplying this flag expires all prepared statements, even when
            # unchanged. A scoped writer still proves its actual flag at every
            # boundary and restores it before BEGIN when necessary.
            if session is None or connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                connection.execute("PRAGMA foreign_keys = ON")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise OSError("SQLite Run state requires WAL journal support")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            if not initialized:
                # A concurrent creator may have initialized the database while
                # this connection waited for the write transaction.
                if not self._check_schema(connection, allow_empty=True):
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_meta(key, value) VALUES ('protocol', ?)", (_PROTOCOL,)
                    )
            self._local.connection = connection
            self._local.failed = False
            self._local.view = None
            self._local.work_view = None
            self._local.work_source = None
            self._local.work_archives = {}
            self._local.work_attempts = {}
            self._local.work_timelines = {}
            try:
                yield
                if self._local.failed:
                    raise RuntimeError("SQLite Run transaction contains a failed nested operation")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        except sqlite3.Error as error:
            if session is not None:
                session.close()
            raise OSError(f"Cannot persist SQLite Run state: {error}") from error
        finally:
            self._local.connection = None
            self._local.failed = False
            self._local.view = None
            self._local.work_view = None
            self._local.work_source = None
            self._local.work_archives = {}
            self._local.work_attempts = {}
            self._local.work_timelines = {}
            if connection is not None and session is None:
                # SQLite handles checkpoints and WAL/SHM cleanup on writer
                # close when other connections allow it. Readers never perform
                # this maintenance or manually unlink coordination files.
                connection.close()

    @property
    def in_transaction(self) -> bool:
        return getattr(self._local, "connection", None) is not None

    @contextmanager
    def use_reader(self, connection: ReadConnection | None) -> Iterator[None]:
        previous = getattr(self._local, "reader", None)
        self._local.reader = connection
        try:
            yield
        finally:
            self._local.reader = previous

    @contextmanager
    def _reading(self, run_id: str | None = None, *, check_schema: bool = True) -> Iterator[sqlite3.Connection | None]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        session = getattr(self._local, "work_connections", None)
        if not self.path.is_file():
            if session is not None:
                session.close()
            yield None
            return
        borrowed = cast(ReadConnection | None, getattr(self._local, "reader", None))
        connection: sqlite3.Connection | None = borrowed
        try:
            if borrowed is None:
                connection = (self._work_connection(readonly=True) if session is not None else sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro", uri=True,
                    timeout=5.0, isolation_level=None, factory=ReadConnection,
                ))
                if connection is None:
                    yield None
                    return
                if session is None:
                    connection.execute("PRAGMA query_only = ON")
            if isinstance(connection, ReadConnection):
                connection.schema_facts = None
                connection.run_rows = None
            assert connection is not None
            # The object aggregation and sparse metadata validation must share
            # one snapshot even when the former fits in a single statement.
            connection.execute("BEGIN")
            if run_id is not None and self._aggregate_rows:
                read_document(cast(ReadConnection, connection), run_id, max_bytes=self.max_bytes,
                              aggregate_bytes=_READ_VIEW_BYTES, tables=_CANONICAL_TABLES)
            # Work reads prove the current canonical DDL and protocol in their
            # bounded query; every other reader validates before querying.
            if not check_schema or self._check_schema(connection, allow_empty=True):
                yield connection
            else:
                # A failed first business transaction may leave an empty
                # database file. It contains no Run and needs no read repair.
                yield None
        except sqlite3.Error as error:
            raise ValueError(f"Cannot read SQLite Run state: {error}") from error
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    if isinstance(connection, ReadConnection):
                        connection.schema_facts = None
                        connection.run_rows = None
                    if borrowed is None and session is None:
                        connection.close()

    def _check_size(
        self, state: Mapping[str, Any], *, fields: dict[str, str] | None = None,
        encoded_fields: Mapping[str, str] | None = None,
        unloaded_fields: Mapping[str, str] | None = None,
    ) -> int:
        # Encode one top-level field at a time with the C JSON encoder. Key
        # ordering does not change the byte count. This avoids both Python's
        # per-token iterencode hot path and an additional whole-Run JSON string.
        size = 3  # Braces and the former JSON authority's trailing newline.
        if unloaded_fields and state.keys() & unloaded_fields.keys():
            raise ValueError("Unloaded SQLite fields were supplied in the projection")
        items: Iterable[tuple[str, Any]]
        if unloaded_fields:
            items = [*state.items(), *((key, None) for key in unloaded_fields)]
        else:
            items = state.items()
        for position, (key, value) in enumerate(items):
            size += bool(position) + len(_json(key).encode("utf-8")) + 1
            encoded = unloaded_fields.get(key) if unloaded_fields is not None else None
            if encoded is None:
                encoded = encoded_fields.get(key) if encoded_fields is not None else None
            if encoded is None:
                encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                           if isinstance(value, dict | list | tuple) else _json(value))
            size += len(encoded.encode("utf-8"))
            if fields is not None:
                if size > _READ_VIEW_BYTES:
                    fields.clear()
                elif isinstance(value, dict | list | tuple):
                    fields[key] = encoded
            if size > self.max_bytes:
                raise ValueError(f"Run state exceeds {self.max_bytes} byte persistence limit")
        if size > self.max_bytes:
            raise ValueError(f"Run state exceeds {self.max_bytes} byte persistence limit")
        return size

    def read_token(self) -> object | None:
        reference = getattr(self._local, "last_read_view", None)
        return reference() if reference is not None else None

    def _copy_view(self, view: _RunView) -> dict[str, Any]:
        if view.fields is not None:
            self._local.last_read_view = weakref.ref(view)
            return copy_projection_fields(view.state, view.fields)
        return cast(dict[str, Any], copy_json_tree(view.state))

    def load(self, run_id: str) -> dict[str, Any] | None:
        self._local.last_read_view = None
        view = cast(_RunView | None, getattr(self._local, "view", None))
        if view is not None and view.run_id == run_id:
            # Only the current write transaction can reuse its validated view.
            # Callers own mutable projections, never the transaction baseline.
            return self._copy_view(view)
        with self._reading(run_id) as connection:
            if connection is None:
                return None
            source, material_bytes, object_bytes, self._aggregate_rows = read_run_rows(
                connection, run_id, max_bytes=self.max_bytes,
                aggregate_bytes=_READ_VIEW_BYTES, aggregate=self._aggregate_rows,
            )
            if source is None:
                return None
            with self._read_view_lock:
                cached = self._read_view
            if (cached is not None and cached.view.run_id == run_id
                    and cached.source == source):
                # Exact current SQL contents, not timestamps or a process-local
                # revision, determine whether reconstruction can be reused.
                if cached.view.logical_bytes > self.max_bytes:
                    raise ValueError(f"Run state exceeds {self.max_bytes} byte persistence limit")
                if getattr(self._local, "connection", None) is not None:
                    self._local.view = cached.view
                assert cached.view.fields is not None
                state = self._copy_view(cached.view)
                check_indexes(self, connection, run_id, state, cached.view.logical_bytes,
                              cached.view.objects, cached.view.materials, snapshot_refs=cached.view.snapshot_refs,
                              expected=cached.view.index_facts)
                return state
            materials: dict[str, str] = {}
            for identity, material_value, byte_length in source.material_rows():
                if not isinstance(material_value, str) or type(byte_length) is not int:
                    raise ValueError("Invalid SQLite Run material identity")
                material_encoded = material_value.encode("utf-8")
                if len(material_encoded) != byte_length or hashlib.sha256(material_encoded).hexdigest() != identity:
                    raise ValueError("Invalid SQLite Run material identity")
                materials[identity] = material_value
            objects = {path: (kind, encoded) for path, kind, encoded in source.object_rows()}
            prior = cached.view if cached is not None and cached.view.run_id == run_id else None
            unchanged = set(prior.fields) if prior is not None and prior.fields is not None else set()
            if unchanged and prior is not None:
                for raw_path in objects.keys() | prior.objects.keys():
                    if objects.get(raw_path) != prior.objects.get(raw_path):
                        parsed = object_path(raw_path)
                        if parsed and isinstance(parsed[0], str):
                            unchanged.discard(parsed[0])
                        else:
                            unchanged.clear()
                            break
                # Source nodes alone do not prove a snapshot's material closure.
                # The current query has verified every material's actual SHA and
                # length; all dependencies must still be present before reuse.
                for raw_path, (kind, _) in objects.items():
                    parsed = object_path(raw_path)
                    if kind == "snapshot" and parsed and parsed[0] in unchanged:
                        refs = prior.snapshot_refs.get(raw_path)
                        if isinstance(parsed[0], str) and (refs is None or not refs <= materials.keys()):
                            unchanged.discard(parsed[0])
            nodes: dict[tuple[str | int, ...], Any] = {}
            snapshots: dict[str, str] = {}
            snapshot_refs: dict[str, frozenset[str]] = {}
            snapshot_bytes = 0
            list_lengths: dict[tuple[str | int, ...], int] = {}
            if prior is not None and prior.fields is not None:
                for field_key in unchanged:
                    nodes[(field_key,)] = json.loads(prior.fields[field_key])
                for raw_path, plain in prior.snapshots.items():
                    parsed = object_path(raw_path)
                    if parsed and parsed[0] in unchanged:
                        snapshots[raw_path] = plain
                        snapshot_refs[raw_path] = prior.snapshot_refs[raw_path]
                        snapshot_bytes += len(plain.encode("utf-8"))
            for encoded_path, (kind, encoded) in objects.items():
                path = object_path(encoded_path)
                if path and path[0] in unchanged:
                    continue
                value = scalar_value(encoded)
                if kind == "map" and value is None:
                    value = {}
                elif kind == "list" and type(value) is int and 0 <= value <= self.max_bytes:
                    # Do not allocate from a possibly damaged declared length.
                    # First prove that exactly these children exist on disk.
                    list_lengths[path] = value
                    value = []
                elif kind == "material" and isinstance(value, str) and value in materials:
                    value = materials[value]
                elif kind == "snapshot" and isinstance(value, str) and value in materials:
                    prior = cached.view if cached is not None and cached.view.run_id == run_id else None
                    references = prior.snapshot_refs.get(encoded_path) if prior is not None else None
                    if (prior is not None and prior.objects.get(encoded_path) == (kind, encoded)
                            and references is not None and references <= materials.keys()):
                        # Every current material has already passed its SHA and
                        # byte-length check. The unchanged content-addressed
                        # object and complete dependencies prove this encoding.
                        plain = prior.snapshots[encoded_path]
                        value = json.loads(plain)
                    else:
                        dependencies = {value}
                        value = self._restore_snapshot(json.loads(materials[value]), materials, dependencies)
                        plain = _json(value)
                        references = frozenset(dependencies)
                    snapshot_bytes += len(plain.encode("utf-8"))
                    if snapshot_bytes <= _READ_VIEW_BYTES:
                        snapshots[encoded_path] = plain
                        snapshot_refs[encoded_path] = references
                elif kind != "scalar" or isinstance(value, dict | list):
                    raise ValueError("Invalid SQLite Run object value")
                if path in nodes:
                    raise ValueError("Duplicate SQLite Run object path")
                nodes[path] = value
            child_counts = dict.fromkeys(list_lengths, 0)
            for path in nodes:
                parent_path = path[:-1]
                if path and parent_path in list_lengths:
                    index = path[-1]
                    if type(index) is not int or not 0 <= index < list_lengths[parent_path]:
                        raise ValueError("Invalid SQLite Run list item")
                    child_counts[parent_path] += 1
            for path, length in list_lengths.items():
                if child_counts[path] != length:
                    raise ValueError("Missing SQLite Run list item")
                nodes[path] = [None] * length
            root = nodes.get(())
            if not isinstance(root, dict):
                raise ValueError("Missing SQLite Run object root")
            for path in sorted(nodes, key=len):
                if not path:
                    continue
                parent = nodes.get(path[:-1])
                key = path[-1]
                if isinstance(parent, dict) and isinstance(key, str):
                    parent[key] = nodes[path]
                elif isinstance(parent, list) and type(key) is int and 0 <= key < len(parent):
                    parent[key] = nodes[path]
                else:
                    raise ValueError("Invalid SQLite Run object parent")
            fields: dict[str, str] = {}
            # A snapshot with separately stored descendants can be overridden
            # during assembly. Only complete, unmodified subtree encodings may
            # stand in for the assembled value in size/copy calculations.
            overridden = {path[:length] for raw_path in objects for path in (object_path(raw_path),)
                          for length in range(1, len(path))}
            encodings = {object_path(path): plain for path, plain in snapshots.items()
                         if object_path(path) not in overridden}
            proven_fields = snapshot_fields(root, encodings)
            if prior is not None and prior.fields is not None:
                proven_fields.update((key, prior.fields[key]) for key in unchanged)
            logical_bytes = self._check_size(root, fields=fields, encoded_fields=proven_fields)
            index_facts = check_indexes(self, connection, run_id, root, logical_bytes, objects, materials,
                                        snapshot_refs=snapshot_refs, snapshot_encodings=encodings)
            reusable = (logical_bytes <= _READ_VIEW_BYTES
                        and material_bytes + object_bytes <= _READ_VIEW_BYTES)
            active = getattr(self._local, "connection", None) is not None
            # Small views already own immutable container encodings. Retain
            # only scalar values/placeholders, rather than copying and holding
            # a second decoded tree that every read immediately copies again.
            baseline = ({key: None if key in fields else value for key, value in root.items()}
                        if reusable else copy_json_tree(root)) if reusable or active else {}
            view = (_RunView(run_id, baseline,
                             objects, materials, logical_bytes, fields if reusable else None,
                             snapshots, snapshot_refs, index_facts if reusable else None,
                             material_bytes + object_bytes)
                    if reusable or active else None)
            with self._read_view_lock:
                self._read_view = (_ReadView(view, source)
                                   if reusable and view is not None else None)
            if active:
                self._local.view = view
            if view is not None and view.fields is not None:
                self._local.last_read_view = weakref.ref(view)
            return root

    def load_work(self, run_id: str, ticket_number: int | None, *, thin_timeline: bool = False) -> dict[str, Any] | None:
        """Read work facts and indexed related history in one SQL snapshot."""
        return load_work(self, run_id, ticket_number, thin_timeline=thin_timeline)

    def work_baseline(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """Borrow the proved transaction baseline for internal read-only use.

        The caller mutates its separate loaded projection. A matching baseline
        is already isolated from it; a custom or changed view keeps the copy
        path. Neither result grants authority outside the current transaction.
        """
        from agent_run.sqlite_work_objects import WorkView
        from agent_run.sqlite_mutations import _same

        view = getattr(self._local, "work_view", None)
        connection = getattr(self._local, "connection", None)
        if (connection is not None and isinstance(view, WorkView) and view.run_id == run_id
                and view.total_changes == connection.total_changes and _same(view.baseline, state)):
            return view.baseline
        return cast(dict[str, Any], copy_json_tree(state))

    def current_work_baseline(self, run_id: str) -> dict[str, Any]:
        """Borrow only the isolated source of this unchanged write snapshot."""
        from agent_run.sqlite_work_objects import WorkView

        view = getattr(self._local, "work_view", None)
        connection = getattr(self._local, "connection", None)
        if (connection is None or not isinstance(view, WorkView) or view.run_id != run_id
                or view.total_changes != connection.total_changes):
            raise ValueError("Work baseline requires a current transaction read")
        return view.baseline

    def has_ticket_supervision_work(self, run_id: str) -> bool:
        """Skip only a proved empty normal supervision observation."""
        with self._reading(check_schema=False) as connection:
            if connection is None:
                return False
            if self.in_transaction:
                self._check_schema(connection)
            if self._aggregate_rows:
                from agent_run.sqlite_supervision_read import read_supervision_work

                observed = read_supervision_work(connection, run_id, max_bytes=self.max_bytes,
                                                 schema_parameters=_WORK_SCHEMA_PARAMETERS)
                if observed is not None:
                    return observed
            if not self.in_transaction and not self._check_schema(connection, allow_empty=True):
                return False
            size = connection.execute(
                "SELECT s.logical_bytes FROM runs r LEFT JOIN run_sizes s "
                "ON s.run_id=r.run_id WHERE r.run_id=?", (run_id,),
            ).fetchone()
            if size is None:
                return False
            if type(size[0]) is not int or not 3 <= size[0] <= self.max_bytes:
                raise ValueError("Invalid SQLite Run size metadata")
            status_path = encode_json(("status",))
            holder_path = encode_json(("ticket_integration_holder",))
            queue_path = encode_json(("ticket_integration_queue",))
            rows = _object_rows(self, connection, run_id, ["[]", status_path, holder_path, queue_path])
            by_path = {path: (kind, value) for path, kind, value in rows}
            if by_path.get("[]") != ("map", "null"):
                raise ValueError("Invalid SQLite Run object root")
            for path, kind in ((holder_path, "scalar"), (queue_path, "list")):
                if path not in by_path or by_path[path][0] != kind:
                    raise ValueError("Invalid SQLite Ticket supervision object shape")
            status_entry = by_path.get(status_path)
            if status_entry is None or status_entry[0] not in {"scalar", "material"}:
                raise ValueError("Invalid SQLite Ticket supervision status shape")
            status = scalar_value(status_entry[1])
            holder = scalar_value(by_path[holder_path][1])
            count = scalar_value(by_path[queue_path][1])
            if (not isinstance(status, str)
                    or holder is not None and (type(holder) is not int or holder < 1)
                    or type(count) is not int or not 0 <= count <= self.max_bytes):
                raise ValueError("Invalid SQLite Ticket supervision status or queue")
            leaf_paths = [status_path, holder_path] + ([queue_path] if count == 0 else [])
            # Separate range arms keep each lookup on (run_id, object_path).
            # A combined OR otherwise makes SQLite scan every path in the Run.
            ranges = " UNION ALL ".join(
                "SELECT 1 FROM run_objects WHERE run_id=? AND object_path>=? AND object_path<?"
                for _ in leaf_paths
            )
            bounds = tuple(bound for path in leaf_paths for bound in (run_id, path[:-1] + ",", path[:-1] + "-"))
            if connection.execute(
                "SELECT 1 FROM (" + ranges + ") LIMIT 1", bounds,
            ).fetchone() is not None:
                raise ValueError("Invalid SQLite Ticket supervision object children")
            # All other strings, including control gates or future statuses,
            # retain the original complete work contract and owner selection.
            return (status_entry[0] == "material" or status not in {"active", "ticket_delivery_pending"}
                    or holder is not None or count != 0)

    def ticket_pause_requested(self, run_id: str, ticket_number: int) -> bool:
        """Observe one Stop intent without loading execution or history bodies."""
        if type(ticket_number) is not int or ticket_number < 1:
            raise ValueError("Ticket number must be positive")
        with self._reading(check_schema=False) as connection:
            if connection is None:
                raise ValueError("Delivery Run disappeared at Ticket pause boundary")
            # Writers retain their fresh structural validation. Ordinary
            # readers first prove current canonical DDL in the scalar query.
            if self.in_transaction:
                self._check_schema(connection)
            if self._aggregate_rows:
                from agent_run.sqlite_ticket_pause_read import read_ticket_pause

                observed = read_ticket_pause(connection, run_id, ticket_number, max_bytes=self.max_bytes,
                                             schema_parameters=_WORK_SCHEMA_PARAMETERS)
                if observed is not None:
                    return observed
            if not self.in_transaction and not self._check_schema(connection, allow_empty=True):
                raise ValueError("Delivery Run disappeared at Ticket pause boundary")
            size = connection.execute(
                "SELECT s.logical_bytes FROM runs r LEFT JOIN run_sizes s "
                "ON s.run_id=r.run_id WHERE r.run_id=?", (run_id,),
            ).fetchone()
            if size is None:
                raise ValueError("Delivery Run disappeared at Ticket pause boundary")
            if type(size[0]) is not int or not 3 <= size[0] <= self.max_bytes:
                raise ValueError("Invalid SQLite Run size metadata")
            root = encode_json(("ticket_pause_requests",))
            target = encode_json(("ticket_pause_requests", str(ticket_number)))
            rows = _object_rows(self, connection, run_id, ["[]", root, target])
            by_path = {path: (kind, value) for path, kind, value in rows}
            if any(by_path.get(path) != ("map", "null") for path in ("[]", root)):
                raise ValueError("Invalid SQLite Ticket pause request map")
            entry = by_path.get(target)
            if entry is not None and entry[0] not in {"scalar", "material"}:
                raise ValueError("Invalid SQLite Ticket pause request value")
            if connection.execute(
                "SELECT 1 FROM run_objects WHERE run_id=? "
                "AND object_path>=? AND object_path<? LIMIT 1",
                (run_id, target[:-1] + ",", target[:-1] + "-"),
            ).fetchone() is not None:
                raise ValueError("Invalid SQLite Ticket pause request children")
            state, _ = _decode(self, connection, run_id, rows)
            requests = state["ticket_pause_requests"]
            if entry is not None and not isinstance(requests[str(ticket_number)], str):
                raise ValueError("Invalid SQLite Ticket pause request timestamp")
            return entry is not None

    def work_archives(self, state: Mapping[str, Any] | None = None) -> dict[str, tuple[int, int]]:
        """Metadata proved by the current thread's latest consistent work read."""
        archives = dict(getattr(self._local, "work_archives", {}))
        view = getattr(self._local, "work_view", None)
        if state is not None and view is not None:
            before, after = view.baseline.get("active_ticket_job"), state.get("active_ticket_job")
            if isinstance(before, dict) and (not isinstance(after, dict) or before.get("ticket_number") != after.get("ticket_number")):
                archives = {path: size for path, size in archives.items()
                            if object_path(path)[:1] != ("active_ticket_job",)}
        return archives

    def work_attempts(self, state: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """Return only exact related records proved by this work-read boundary."""
        archives = self.work_archives(state)
        return {path: copy_json_tree(attempt) for path, attempt in getattr(self._local, "work_attempts", {}).items()
                if encode_json((*object_path(path), "semantic_attempt_history")) in archives}

    def work_timelines(self) -> dict[str, sqlite_timeline.TimelineScope]:
        """Describe absent Timeline fields from this read, without save authority."""
        return dict(getattr(self._local, "work_timelines", {}))

    def save_work(self, run_id: str, state: Mapping[str, Any], baseline: Mapping[str, Any],
                  *, timeline_updates: Mapping[str, dict[str, Any]] | None = None) -> None:
        """Patch a work projection without replacing unloaded history."""
        with self.atomic():
            self._local.work_source = None
            save_work(self, run_id, state, baseline, timeline_updates=timeline_updates)

    def _objects(
        self, state: Mapping[str, Any],
    ) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        objects: dict[str, tuple[str, str]] = {}
        materials: dict[str, str] = {}

        def material(value: str) -> str:
            identity = hashlib.sha256(value.encode("utf-8")).hexdigest()
            materials.setdefault(identity, value)
            return identity

        def snapshot(value: Any) -> str:
            def encode(item: Any, key: str = "", parent_key: str = "") -> tuple[Any, bool]:
                if isinstance(item, Mapping):
                    if key in _SNAPSHOT_FIELDS or parent_key == "tickets":
                        return ["snapshot", snapshot(item)], True
                    children = {name: encode(child, name, key) for name, child in item.items()}
                    referenced = any(child[1] for child in children.values())
                    # An ordinary JSON map is already a literal in the snapshot
                    # protocol. Keep unreferenced subtrees in that form so the
                    # C decoder reconstructs them without Python tag traversal.
                    if not referenced:
                        return item, False
                    return ["map", {name: child[0] for name, child in children.items()}], True
                if isinstance(item, list | tuple):
                    children_list = [encode(child) for child in item]
                    return ["list", [child[0] for child in children_list]], any(
                        child[1] for child in children_list
                    )
                if isinstance(item, str) and len(item.encode("utf-8")) >= _MATERIAL_THRESHOLD:
                    return ["material", material(item)], True
                return item, False

            return material(_json(encode(value)[0]))

        def add(path: tuple[str | int, ...], value: Any) -> None:
            location = _json(path)
            fixed_snapshot = bool(path) and (
                path[-1] in _SNAPSHOT_FIELDS or len(path) > 1 and path[-2] == "tickets"
            )
            keyed_object = (
                len(path) == 1 and path[0] in _CURRENT_OBJECTS
                or len(path) == 2 and path[0] in _OBJECT_COLLECTIONS
            )
            job_object = (path == ("active_ticket_job",)
                          or len(path) == 2 and path[0] == "ticket_jobs")
            if isinstance(value, Mapping) and job_object and JOB_HISTORIES.intersection(value):
                # The current Job owns its scheduling facts; an archive remains
                # independently addressable without expanding sibling evidence.
                add(path, {key: item for key, item in value.items() if key not in JOB_HISTORIES})
                for key in JOB_HISTORIES.intersection(value):
                    if key == "semantic_attempt_history" and isinstance(value[key], list):
                        objects[_json((*path, key))] = ("list", str(len(value[key])))
                        for position, attempt in enumerate(value[key]):
                            objects[_json((*path, key, position))] = ("snapshot", _json(snapshot(attempt)))
                    else:
                        objects[_json((*path, key))] = ("snapshot", _json(snapshot(value[key])))
            elif isinstance(value, Mapping) and (fixed_snapshot or keyed_object):
                objects[location] = ("snapshot", _json(snapshot(value)))
            elif isinstance(value, Mapping):
                objects[location] = ("map", "null")
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("SQLite Run mapping keys must be strings")
                    add(path + (key,), item)
            elif isinstance(value, list | tuple):
                objects[location] = ("list", str(len(value)))
                for position, item in enumerate(value):
                    add(path + (position,), item)
            elif isinstance(value, str) and len(value.encode("utf-8")) >= _MATERIAL_THRESHOLD:
                identity = material(value)
                objects[location] = ("material", _json(identity))
            else:
                objects[location] = ("scalar", _json(value))

        add((), state)
        return objects, materials

    @classmethod
    def _restore_snapshot(
        cls, value: Any, materials: Mapping[str, str], references: set[str] | None = None,
    ) -> Any:
        if not isinstance(value, list):
            return value
        if len(value) != 2:
            raise ValueError("Invalid SQLite Run snapshot")
        kind, content = value
        if kind == "map" and isinstance(content, dict):
            return {key: cls._restore_snapshot(item, materials, references) for key, item in content.items()}
        if kind == "list" and isinstance(content, list):
            return [cls._restore_snapshot(item, materials, references) for item in content]
        if kind in {"material", "snapshot"} and isinstance(content, str) and content in materials:
            text = materials[content]
            if references is not None:
                references.add(content)
            return text if kind == "material" else cls._restore_snapshot(json.loads(text), materials, references)
        raise ValueError("Missing or invalid SQLite Run snapshot material")

    def save(self, run_id: str, state: Mapping[str, Any]) -> None:
        """Create one new Run; existing Runs require explicit object commits."""
        with self.atomic():
            connection = self._local.connection
            if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is not None:
                raise ValueError("SQLite Run already exists; use object commits to update it")
            self._check_size(state)
            objects, materials = self._objects(state)
            representation_bytes = sum(len(path.encode()) + len(value.encode())
                                       for path, (_, value) in objects.items())
            representation_bytes += sum(len(value.encode()) for value in materials.values())
            if representation_bytes > 8 * self.max_bytes:
                raise ValueError(f"Run state exceeds {self.max_bytes} byte persistence limit")
            parent = state.get("parent")
            parent_number = parent.get("number") if isinstance(parent, Mapping) else None
            if type(parent_number) is not int:
                parent_number = None
            metadata = tuple(
                value if isinstance(value, str) else None
                for value in (state.get("repository"), state.get("status"), state.get("phase"),
                              state.get("created_at"), state.get("updated_at"))
            )
            self._local.view = None
            self._local.work_source = None
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, metadata[0], parent_number, *metadata[1:], _json(sorted(state))),
            )
            from agent_run.sqlite_objects import index_objects
            index_objects(connection, run_id, state, objects, materials, {})
            self._aggregate_rows = create_run_objects(
                connection, run_id, objects=objects, materials=materials, native=self._aggregate_rows,
            )
            from agent_run.sqlite_integrity import sync_material_counts
            sync_material_counts(connection, run_id)
            index_run(self, connection, run_id, state, objects, materials)
            self._local.work_view = None

    def load_objects(
        self, run_id: str, *, excluded_fields: set[str] | None = None,
        related_tickets: bool = False, ticket_numbers: set[int] | None = None, scheduler_tickets: bool = False,
    ) -> tuple[dict[str, Any], dict[str, int]] | None:
        """Read current facts and only history linked to current identities."""
        from agent_run.sqlite_objects import load_projection
        with self._reading() as connection:
            return None if connection is None else load_projection(connection, run_id, excluded_fields=excluded_fields, max_bytes=self.max_bytes,
                                                                       related_tickets=related_tickets, ticket_numbers=ticket_numbers,
                                                                       scheduler_tickets=scheduler_tickets)

    def load_fields(self, run_id: str, fields: set[str]) -> dict[str, Any] | None:
        from agent_run.sqlite_objects import load_fields
        with self._reading() as connection:
            return None if connection is None else load_fields(connection, run_id, fields, max_bytes=self.max_bytes)

    def save_objects(
        self, run_id: str, state: Mapping[str, Any], *, fields: set[str],
        history_positions: Mapping[str, int] | None = None,
        keyed_changes: Mapping[str, set[str]] | None = None,
    ) -> None:
        from agent_run.sqlite_objects import save_projection
        with self.atomic():
            save_projection(self, self._local.connection, run_id, state, fields,
                            history_positions or {}, keyed_changes=keyed_changes)
            self._local.view = None

    def load_paths(self, run_id: str, paths: set[tuple[str | int, ...]]) -> dict[str, Any] | None:
        from agent_run.sqlite_collections import load_paths
        from agent_run.sqlite_integrity import require_field_sizes
        with self._reading() as connection:
            if connection is None or connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                return None
            require_field_sizes(connection, run_id, max_bytes=self.max_bytes)
            return load_paths(connection, run_id, paths)

    def append_array(self, run_id: str, field: str, items: list[Any]) -> None:
        from agent_run.sqlite_objects import save_projection
        with self.atomic():
            if field == "agent_invocation_history":
                save_projection(self, self._local.connection, run_id, {field: items}, {field}, {})
            else:
                save_projection(self, self._local.connection, run_id, {}, {field}, {}, append_changes={field: items})
            self._local.view = None

    def has(self, run_id: str) -> bool:
        with self._reading() as connection:
            return connection is not None and connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone() is not None

    def list_ids(self, *, limit: int | None = None) -> list[str]:
        if limit is not None and limit < 0:
            raise ValueError("Run ID listing limit must not be negative")
        with self._reading() as connection:
            if connection is None:
                return []
            query = "SELECT run_id FROM runs ORDER BY run_id"
            params: tuple[int, ...] = ()
            if limit is not None:
                query += " LIMIT ?"
                params = (limit + 1,)
            ids = [row[0] for row in connection.execute(query, params)]
            if limit is not None and len(ids) > limit:
                raise ValueError("Run ID listing exceeds the requested limit")
            return ids

    def find_ids(self, repository: str, parent_number: int, *, unfinished_only: bool) -> list[str]:
        with self._reading() as connection:
            if connection is None:
                return []
            query = "SELECT run_id FROM runs WHERE repository = ? COLLATE NOCASE AND parent_number = ?"
            if unfinished_only:
                query += " AND (status IS NULL OR status NOT IN ('completed', 'abandoned'))"
            query += " ORDER BY created_at DESC, run_id"
            return [row[0] for row in connection.execute(query, (repository, parent_number))]


__all__ = ["SQLiteStateRepository"]
