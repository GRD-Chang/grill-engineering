"""Single-authority SQLite object storage for new Delivery Runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, cast

from agent_run.state_errors import IncompatibleRunStateError
from agent_run.sqlite_encoding import copy_json_tree, copy_projection_fields, encode_json, object_path, scalar_value, snapshot_fields
from agent_run.sqlite_mutations import create_run_objects
from agent_run import sqlite_timeline
from agent_run.sqlite_work_objects import check_indexes, index_run
from agent_run.sqlite_work_indexes import IndexFacts
from agent_run.sqlite_work_fields import field_node_counts
from agent_run.sqlite_rows import ReadConnection, RowSource, read_document, read_run_rows, read_schema
from agent_run import sqlite_control
from agent_run.sqlite_scope import context_for
from agent_run.sqlite_runtime import enable_wal, require_wal_runtime
from agent_run import sqlite_work_records as work_records


_PROTOCOL = "sqlite-work-records-v5"
_MATERIAL_THRESHOLD = 1024
_SNAPSHOT_FIELDS = {"prompt_resources", "review_context", "parent", "ticket", "issue_snapshot"}
_OBJECT_COLLECTIONS = {"timeline", "timeline_continuation"}
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
    "run_object_material_refs": ("run_id", "object_path", "material_id"),
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
    "run_object_material_refs": ("run_id", "object_path", "material_id"),
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
    "run_object_material_refs": {
        ("run_objects", "run_id", "run_id", "CASCADE"),
        ("run_objects", "object_path", "object_path", "CASCADE"),
        ("run_materials", "material_id", "material_id", "NO ACTION"),
    },
}
_SCHEMA: tuple[str, ...] = (
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
    "CREATE TABLE run_object_material_refs (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
    "material_id TEXT NOT NULL, PRIMARY KEY (run_id, object_path, material_id), "
    "FOREIGN KEY (run_id, object_path) REFERENCES run_objects(run_id, object_path) ON DELETE CASCADE, "
    "FOREIGN KEY (material_id) REFERENCES run_materials(material_id))",
    "CREATE INDEX current_field_sizes ON run_object_index(run_id, logical_bytes) WHERE logical_bytes > 0",
    "CREATE INDEX object_material_identity ON run_object_materials(run_id, material_id)",
    "CREATE INDEX runs_parent ON runs(repository COLLATE NOCASE, parent_number, created_at)",
    "CREATE INDEX object_material ON run_object_material_refs(material_id, run_id)",
    "CREATE INDEX material_run_refs ON run_material_refs(material_id, run_id)",
)
_BUSINESS_TABLES = dict(zip(_COLUMNS, _SCHEMA[:len(_COLUMNS)], strict=True))
_BUSINESS_INDEXES = {statement.split()[2]: statement for statement in _SCHEMA[len(_COLUMNS):]}
_COLUMNS.update(sqlite_control.COLUMNS)
_KEYS.update(sqlite_control.KEYS)
_FOREIGN_KEYS.update(sqlite_control.FOREIGN_KEYS)
_CANONICAL_TABLES = {**_BUSINESS_TABLES, **sqlite_control.TABLES, **work_records.TABLES}
_CANONICAL_INDEXES = {**_BUSINESS_INDEXES, **sqlite_control.INDEXES, **work_records.INDEXES}
_SCHEMA = (*_CANONICAL_TABLES.values(), *_CANONICAL_INDEXES.values())



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
        self._local = context_for(root)
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
        try:
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
        except BaseException:
            connections.close()
            raise

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
        if tables != set(_CANONICAL_TABLES):
            raise IncompatibleRunStateError("Incompatible SQLite Run state schema")
        if metadata is None:
            metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
        if metadata != {"protocol": _PROTOCOL}:
            raise IncompatibleRunStateError("Incompatible SQLite Run state protocol")
        indexes = {name: sql for kind, name, _, sql in definitions if kind == "index"}
        if any(indexes.get(name) != sql for name, sql in _CANONICAL_INDEXES.items()):
            raise IncompatibleRunStateError("Incompatible SQLite Run state indexes")
        actual_tables = {name: sql for kind, name, _, sql in definitions if kind == "table"}
        if any(actual_tables.get(name) != ddl for name, ddl in work_records.TABLES.items()):
            raise IncompatibleRunStateError("Incompatible SQLite work records schema")
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
            if name in sqlite_control.COLUMNS:
                actual = next((ddl for kind, table, _, ddl in definitions
                               if kind == "table" and table == name), None)
                if actual != _CANONICAL_TABLES[name]:
                    raise IncompatibleRunStateError("Incompatible SQLite Task Control schema")
                continue
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
                            )
                if row[2] != expected_type or bool(row[3]) == nullable:
                    raise IncompatibleRunStateError("Incompatible SQLite Run state schema")
        if sum(len(text or "") for _, _, _, text in definitions) <= 128 * 1024:
            with _SCHEMA_CACHE_LOCK:
                if len(_VALID_SCHEMAS) >= 16:
                    _VALID_SCHEMAS.clear()
                _VALID_SCHEMAS.add(definitions)
        return True

    @contextmanager
    def atomic(self, *, timeout: float = 5.0) -> Iterator[None]:
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
        require_wal_runtime()
        self.root.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        session = getattr(self._local, "work_connections", None)
        try:
            connection = (self._work_connection(readonly=False) if session is not None
                          else sqlite3.connect(self.path, timeout=timeout, isolation_level=None))
            assert connection is not None
            connection.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
            initialized = self._check_schema(connection, allow_empty=True)
            # Reapplying this flag expires all prepared statements, even when
            # unchanged. A scoped writer still proves its actual flag at every
            # boundary and restores it before BEGIN when necessary.
            if session is None or connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                connection.execute("PRAGMA foreign_keys = ON")
            enable_wal(connection, timeout=timeout)
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            if initialized:
                self._check_schema(connection)
            else:
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
            self._local.rollback_callbacks = []
            self._local.view = None
            self._local.work_view = None
            self._local.work_archives = {}
            self._local.work_attempts = {}
            self._local.work_timelines = {}
            self._local.object_archives = {}
            self._local.object_attempts = {}
            try:
                yield
                if self._local.failed:
                    raise RuntimeError("SQLite Run transaction contains a failed nested operation")
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                finally:
                    for invalidate in self._local.rollback_callbacks:
                        invalidate()
                raise
        except sqlite3.Error as error:
            if session is not None:
                session.close()
            raise OSError(f"Cannot persist SQLite Run state: {error}") from error
        except BaseException:
            if session is not None:
                session.close()
            raise
        finally:
            self._local.connection = None
            self._local.failed = False
            self._local.rollback_callbacks = []
            self._local.view = None
            self._local.work_view = None
            self._local.work_archives = {}
            self._local.work_attempts = {}
            self._local.work_timelines = {}
            self._local.object_archives = {}
            self._local.object_attempts = {}
            if connection is not None and session is None:
                # SQLite handles checkpoints and WAL/SHM cleanup on writer
                # close when other connections allow it. Readers never perform
                # this maintenance or manually unlink coordination files.
                connection.close()

    @property
    def in_transaction(self) -> bool:
        return getattr(self._local, "connection", None) is not None

    def _on_rollback(self, invalidate: Callable[[], None]) -> None:
        """Invalidate provisional in-memory baselines if the shared commit fails."""
        if self.in_transaction:
            self._local.rollback_callbacks.append(invalidate)

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
        require_wal_runtime()
        borrowed = cast(ReadConnection | None, getattr(self._local, "reader", None))
        connection: sqlite3.Connection | None = borrowed
        failed = False
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
            failed = True
            raise ValueError(f"Cannot read SQLite Run state: {error}") from error
        except BaseException:
            failed = True
            raise
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
                    elif borrowed is None and session is not None and failed:
                        session.close()

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

    def _copy_view(self, view: _RunView) -> dict[str, Any]:
        if view.fields is not None:
            return copy_projection_fields(view.state, view.fields)
        return cast(dict[str, Any], copy_json_tree(view.state))

    def load(self, run_id: str) -> dict[str, Any] | None:
        view = cast(_RunView | None, getattr(self._local, "view", None))
        if view is not None and view.run_id == run_id:
            # Only the current write transaction can reuse its validated view.
            # Callers own mutable projections, never the transaction baseline.
            return work_records.load_complete(self._local.connection, run_id, self._copy_view(view))
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
                return work_records.load_complete(connection, run_id, state)
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
            return work_records.load_complete(connection, run_id, root)

    def load_work(self, run_id: str, ticket_number: int | None, *, thin_timeline: bool = False) -> dict[str, Any] | None:
        """Read work facts and indexed related history in one SQL snapshot."""
        from agent_run.sqlite_work_objects import load_base_work

        with self._reading() as connection:
            if connection is None:
                return None
            if self.in_transaction:
                self._check_schema(connection)
            loaded = load_base_work(self, connection, run_id, thin_timeline=thin_timeline)
            if loaded is None:
                return None
            state, archives, related = work_records.load_work(connection, run_id, loaded[0], ticket_number)
            self._local.work_archives = archives
            self._local.work_attempts = related
            self._local.work_timelines = loaded[1]
            self._local.work_projection_bytes = loaded[2]
            self._local.work_view = work_records.WorkBaseline(run_id, copy_json_tree(state), connection.total_changes)
            return state

    def work_baseline(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """Borrow the proved transaction baseline for internal read-only use.

        The caller mutates its separate loaded projection. A matching baseline
        is already isolated from it; a custom or changed view keeps the copy
        path. Neither result grants authority outside the current transaction.
        """
        from agent_run.sqlite_mutations import _same

        view = getattr(self._local, "work_view", None)
        connection = getattr(self._local, "connection", None)
        if (connection is not None and isinstance(view, work_records.WorkBaseline) and view.run_id == run_id
                and view.total_changes == connection.total_changes and _same(view.baseline, state)):
            return view.baseline
        return cast(dict[str, Any], copy_json_tree(state))

    def current_work_baseline(self, run_id: str) -> dict[str, Any]:
        """Borrow only the isolated source of this unchanged write snapshot."""
        view = getattr(self._local, "work_view", None)
        connection = getattr(self._local, "connection", None)
        if (connection is None or not isinstance(view, work_records.WorkBaseline) or view.run_id != run_id
                or view.total_changes != connection.total_changes):
            raise ValueError("Work baseline requires a current transaction read")
        return view.baseline

    def _require_run_query(self, connection: sqlite3.Connection, run_id: str) -> bool:
        row = connection.execute(
            "SELECT s.logical_bytes FROM runs r LEFT JOIN run_sizes s ON s.run_id=r.run_id WHERE r.run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            return False
        if type(row[0]) is not int or not 3 <= row[0] <= self.max_bytes:
            raise ValueError("Invalid SQLite Run size metadata")
        from agent_run.sqlite_objects import _require_root
        _require_root(connection, run_id)
        return True

    def has_ticket_supervision_work(self, run_id: str) -> bool:
        """Read the same authoritative queue and status without history bodies."""
        from agent_run.sqlite_run_records import has_supervision_work

        with self._reading() as connection:
            if connection is not None and self.in_transaction:
                self._check_schema(connection)
            if connection is None or not self._require_run_query(connection, run_id):
                return False
            return has_supervision_work(connection, run_id)

    def ticket_pause_requested(self, run_id: str, ticket_number: int) -> bool:
        """Observe one exact Ticket Stop in the current read-only snapshot."""
        from agent_run.sqlite_run_records import pause_requested

        if type(ticket_number) is not int or ticket_number < 1:
            raise ValueError("Ticket number must be positive")
        with self._reading() as connection:
            if connection is not None and self.in_transaction:
                self._check_schema(connection)
            if connection is None or not self._require_run_query(connection, run_id):
                raise ValueError("Delivery Run disappeared at Ticket pause boundary")
            return pause_requested(connection, run_id, ticket_number)

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
                  *, timeline_updates: Mapping[str, dict[str, Any]] | None = None) -> bool:
        """Patch a work projection without replacing unloaded history."""
        with self.atomic():
            from agent_run.sqlite_mutations import _same
            from agent_run.sqlite_objects import save_projection

            view = getattr(self._local, "work_view", None)
            connection = self._local.connection
            if (not isinstance(view, work_records.WorkBaseline) or view.run_id != run_id
                    or view.total_changes != connection.total_changes or not _same(view.baseline, baseline)):
                raise ValueError("SQLite work patch requires its current transaction baseline")
            scopes = self.work_timelines()
            updates = timeline_updates or {}
            if updates.keys() - scopes.keys() or any(key in state or key in baseline for key in scopes):
                raise ValueError("SQLite work patch cannot replace unloaded Timeline fields")
            fields = {key for key in state.keys() | baseline.keys()
                      if key not in state or key not in baseline or not _same(state[key], baseline[key])}
            owned = fields & work_records.FIELDS
            remaining = fields - work_records.FIELDS
            run_state = work_records.without_work(state)
            append = {}
            for key in remaining & sqlite_timeline.FIELDS:
                old_events, new_events = baseline.get(key), state.get(key)
                if (isinstance(old_events, list) and isinstance(new_events, list)
                        and len(new_events) >= len(old_events) and new_events[:len(old_events)] == old_events):
                    append[key] = new_events[len(old_events):]
            for key, event in updates.items():
                if not isinstance(event, dict):
                    raise ValueError("Timeline event must be an object")
                scope = scopes[key]
                if scope.count >= sqlite_timeline.limit(key):
                    if key != "timeline_continuation":
                        raise ValueError("Timeline reached its event limit")
                    # Continuation is capped at 256 entries. Only this rare
                    # rotation replaces its bounded field; ordinary appends
                    # encode the new event and retain prior material rows.
                    run_state[key] = [*json.loads(scope.encoded)[1:], event]
                else:
                    append[key] = [event]
                remaining.add(key)
            if remaining:
                save_projection(self, connection, run_id, run_state, remaining, {}, append_changes=append)
            work_records.save(self._local.connection, run_id, state, baseline, fields=owned)
            gates_changed = False
            if isinstance(state, dict):
                gates_changed, mirrors = self.project_saved_job_gates(run_id, state, fields=owned)
                if gates_changed and "ticket_work_queue" in state:
                    from agent_run.ticket_work import reconcile

                    before_queue = state["ticket_work_queue"]
                    reconcile(state)
                    if not _same(before_queue, state["ticket_work_queue"]):
                        mirrors.add("ticket_work_queue")
                if mirrors:
                    plain = mirrors - work_records.FIELDS
                    if plain:
                        save_projection(self, connection, run_id, work_records.without_work(state), plain, {})
                    owned_mirrors = mirrors & work_records.FIELDS
                    if owned_mirrors:
                        current = work_records.baseline_for_save(connection, run_id, state, owned_mirrors, None)
                        work_records.save(connection, run_id, state, current, fields=owned_mirrors)
            self._local.view = None
            self._local.work_view = None
            if state.get("action_application_receipt") != baseline.get("action_application_receipt"):
                self._observe_application(self._local.connection, run_id, state)
            return gates_changed

    def _objects(
        self, state: Mapping[str, Any],
    ) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        if state.keys() & work_records.FIELDS:
            raise ValueError("Work records must use their relational storage boundary")
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
            keyed_object = len(path) == 2 and path[0] in _OBJECT_COLLECTIONS
            if isinstance(value, Mapping) and (fixed_snapshot or keyed_object):
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
            unowned = work_records.without_work(state)
            self._check_size(unowned)
            objects, materials = self._objects(unowned)
            representation_bytes = sum(len(path.encode()) + len(value.encode())
                                       for path, (_, value) in objects.items())
            representation_bytes += sum(len(value.encode()) for value in materials.values())
            if representation_bytes > 8 * self.max_bytes:
                raise ValueError(f"Run state exceeds {self.max_bytes} byte persistence limit")
            self._local.view = None
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, None, None, None, None, None, None, _json(sorted(unowned))),
            )
            from agent_run.sqlite_objects import index_objects
            index_objects(connection, run_id, unowned, objects, materials, {})
            self._aggregate_rows = create_run_objects(
                connection, run_id, objects=objects, materials=materials, native=self._aggregate_rows,
            )
            from agent_run.sqlite_integrity import sync_material_counts
            sync_material_counts(connection, run_id)
            index_run(self, connection, run_id, unowned, objects, materials)
            work_records.save(connection, run_id, state, {}, fields=set(state) & work_records.FIELDS)
            self._local.work_view = None
            if "action_application_receipt" in state:
                self._observe_application(connection, run_id, state)

    def _observe_application(
        self, connection: sqlite3.Connection, run_id: str, state: Mapping[str, Any],
    ) -> None:
        receipt = state.get("action_application_receipt")
        if not isinstance(receipt, Mapping) or receipt.get("run_id") != run_id:
            raise ValueError("Cannot remove or replace the Run application receipt identity")
        sqlite_control.observe_application(connection, receipt, str(self.root.resolve()))

    def load_objects(
        self, run_id: str, *, excluded_fields: set[str] | None = None,
        related_tickets: bool = False, ticket_numbers: set[int] | None = None, scheduler_tickets: bool = False,
    ) -> tuple[dict[str, Any], dict[str, int]] | None:
        """Read current facts and only history linked to current identities."""
        from agent_run.sqlite_objects import load_projection
        with self._reading() as connection:
            if connection is None:
                return None
            loaded = load_projection(connection, run_id, excluded_fields=set(excluded_fields or ()) | work_records.FIELDS,
                                     max_bytes=self.max_bytes, related_tickets=related_tickets,
                                     ticket_numbers=ticket_numbers, scheduler_tickets=scheduler_tickets)
            if loaded is None:
                return None
            result = work_records.load_objects(
                connection, run_id, loaded[0], excluded_fields=set(excluded_fields or ()),
                related_tickets=related_tickets, ticket_numbers=ticket_numbers, scheduler_tickets=scheduler_tickets,
                max_bytes=self.max_bytes,
            )
            self._local.object_archives, self._local.object_attempts = work_records.object_scope(connection, run_id, result[0])
            return result

    def load_fields(self, run_id: str, fields: set[str]) -> dict[str, Any] | None:
        from agent_run.sqlite_objects import load_fields
        with self._reading() as connection:
            if connection is None:
                return None
            base = load_fields(connection, run_id, fields - work_records.FIELDS, max_bytes=self.max_bytes)
            return None if base is None else work_records.load_fields(connection, run_id, base, fields)

    def save_objects(
        self, run_id: str, state: Mapping[str, Any], *, fields: set[str],
        history_positions: Mapping[str, int] | None = None,
        keyed_changes: Mapping[str, set[str]] | None = None,
        archive_scope: Mapping[str, tuple[int, int]] | None = None,
    ) -> None:
        from agent_run.sqlite_objects import save_projection
        with self.atomic():
            connection = self._local.connection
            remaining = fields - work_records.FIELDS
            if remaining:
                save_projection(self, connection, run_id, work_records.without_work(state), remaining,
                                history_positions or {}, keyed_changes={key: value for key, value in (keyed_changes or {}).items()
                                                                       if key not in work_records.FIELDS})
            owned = fields & work_records.FIELDS
            if owned:
                baseline = work_records.baseline_for_save(connection, run_id, state, owned, keyed_changes, archive_scope)
                work_records.save(connection, run_id, state, baseline, fields=owned, keyed_changes=keyed_changes)
            self._local.view = None
            self._local.work_view = None
            if "action_application_receipt" in fields:
                self._observe_application(self._local.connection, run_id, state)

    def project_saved_job_gates(
        self, run_id: str, state: dict[str, Any], *, fields: set[str],
        keyed_changes: Mapping[str, set[str]] | None = None,
    ) -> tuple[bool, set[str]]:
        """Refresh only gates in the caller's existing save transaction."""
        from agent_run.sqlite_change_jobs import FIELDS as JOB_FIELDS
        if not fields & JOB_FIELDS:
            return False, set()
        connection = getattr(self._local, "connection", None)
        if connection is None or not connection.in_transaction:
            raise RuntimeError("Job gate projection requires the save transaction")
        return work_records.project_saved_gates(
            connection, run_id, state, fields=fields, keyed_changes=keyed_changes,
        )

    def load_paths(self, run_id: str, paths: set[tuple[str | int, ...]]) -> dict[str, Any] | None:
        from agent_run.sqlite_collections import load_paths
        from agent_run.sqlite_integrity import require_field_sizes
        if any(not path or any(not isinstance(part, str) for part in path) for path in paths):
            raise ValueError("Collection path projection requires mapping ancestors")
        with self._reading() as connection:
            if connection is None or connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                return None
            require_field_sizes(connection, run_id, max_bytes=self.max_bytes)
            plain_paths = {path for path in paths if path[0] not in work_records.FIELDS}
            base = load_paths(connection, run_id, plain_paths)
            owned = {path for path in paths if path[0] in work_records.FIELDS}
            if not owned:
                return base
            numbers = {int(path[1]) for path in owned if len(path) > 1
                       and path[0] in {"ticket_jobs", "ticket_agent_invocations"}}
            root_maps = {"retired_ticket_generations", "pending_ticket_retirements", "ticket_pause_requests"}
            root_maps -= {str(path[0]) for path in owned if len(path) == 1}
            root_keys = {field: {str(path[1]) for path in owned if len(path) > 1 and path[0] == field}
                         for field in root_maps if any(len(path) > 1 and path[0] == field for path in owned)}
            work = work_records.load_fields(connection, run_id, {}, {str(path[0]) for path in owned},
                                            ticket_numbers=numbers if numbers else None, root_keys=root_keys)
            for path in owned:
                source: Any = work
                target: Any = base
                for part in path[:-1]:
                    if not isinstance(source, dict) or part not in source:
                        break
                    source = source[part]
                    target = target.setdefault(part, {})
                else:
                    if isinstance(source, dict) and path[-1] in source:
                        target[path[-1]] = source[path[-1]]
            return base

    def append_array(self, run_id: str, field: str, items: list[Any]) -> None:
        from agent_run.sqlite_objects import save_projection
        with self.atomic():
            if field in work_records.FIELDS:
                if field == "agent_invocation_history":
                    work_records.save(self._local.connection, run_id, {field: items}, {}, fields={field})
                else:
                    work_records.append_archive(self._local.connection, run_id, field, items)
            else:
                save_projection(self, self._local.connection, run_id, {}, {field}, {}, append_changes={field: items})
            self._local.view = None
            self._local.work_view = None

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
