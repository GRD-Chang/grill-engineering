"""Single-authority SQLite object storage for new Delivery Runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agent_run.state_errors import IncompatibleRunStateError
from agent_run.sqlite_encoding import copy_json_tree, copy_projection_fields, encode_json, object_path, scalar_value, snapshot_fields
from agent_run.sqlite_mutations import patch_run
from agent_run.sqlite_rows import ReadConnection, RowSource, read_document, read_run_rows, read_schema


_PROTOCOL = "sqlite-run-objects-v1"
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
    "runs": ("run_id", "repository", "parent_number", "status", "phase", "created_at", "updated_at"),
    "run_objects": ("run_id", "object_path", "kind", "value_json"),
    "run_materials": ("material_id", "value", "byte_length"),
    "run_material_refs": ("run_id", "material_id"),
}
_KEYS = {
    "schema_meta": ("key",),
    "runs": ("run_id",),
    "run_objects": ("run_id", "object_path"),
    "run_materials": ("material_id",),
    "run_material_refs": ("run_id", "material_id"),
}
_FOREIGN_KEYS = {
    "run_objects": {("runs", "run_id", "run_id", "CASCADE")},
    "run_material_refs": {
        ("runs", "run_id", "run_id", "CASCADE"),
        ("run_materials", "material_id", "material_id", "NO ACTION"),
    },
}
_SCHEMA = (
    "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, repository TEXT, "
    "parent_number INTEGER, status TEXT, phase TEXT, created_at TEXT, updated_at TEXT)",
    "CREATE TABLE run_objects (run_id TEXT NOT NULL, object_path TEXT NOT NULL, "
    "kind TEXT NOT NULL CHECK (kind IN ('map', 'list', 'scalar', 'material', 'snapshot')), "
    "value_json TEXT NOT NULL, PRIMARY KEY (run_id, object_path), "
    "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "CREATE TABLE run_materials (material_id TEXT PRIMARY KEY, value TEXT NOT NULL, "
    "byte_length INTEGER NOT NULL)",
    "CREATE TABLE run_material_refs (run_id TEXT NOT NULL, material_id TEXT NOT NULL, "
    "PRIMARY KEY (run_id, material_id), "
    "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE, "
    "FOREIGN KEY (material_id) REFERENCES run_materials(material_id))",
    "CREATE INDEX runs_parent ON runs(repository COLLATE NOCASE, parent_number, created_at)",
)
_CANONICAL_TABLES = dict(zip(_COLUMNS, _SCHEMA[:len(_COLUMNS)], strict=True))


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


@dataclass(frozen=True)
class _ReadView:
    view: _RunView
    source: RowSource


class SQLiteStateRepository:
    """Persist keyed current objects without a second JSON authority.

    Mapping keys and list positions form stable object paths. Only changed
    nodes are written; a shifted bounded history list may change multiple rows.
    ``atomic`` lets a caller read the latest Run and patch objects in one real
    transaction. Connections are short lived and never shared across threads.
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
                expected_type = "INTEGER" if row[1] in {"parent_number", "byte_length"} else "TEXT"
                nullable = name == "runs" or row[1] == "key" or (name == "run_materials" and row[1] == "material_id")
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
        try:
            connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
            initialized = self._check_schema(connection, allow_empty=True)
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
            try:
                yield
                if self._local.failed:
                    raise RuntimeError("SQLite Run transaction contains a failed nested operation")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        except sqlite3.Error as error:
            raise OSError(f"Cannot persist SQLite Run state: {error}") from error
        finally:
            self._local.connection = None
            self._local.failed = False
            self._local.view = None
            if connection is not None:
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
    def _reading(self, run_id: str | None = None) -> Iterator[sqlite3.Connection | None]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        if not self.path.is_file():
            yield None
            return
        borrowed = cast(ReadConnection | None, getattr(self._local, "reader", None))
        connection: sqlite3.Connection | None = borrowed
        try:
            if borrowed is None:
                connection = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro", uri=True,
                    timeout=5.0, isolation_level=None, factory=ReadConnection,
                )
                connection.execute("PRAGMA query_only = ON")
            else:
                borrowed.schema_facts = None
                borrowed.run_rows = None
            assert connection is not None
            captured = (run_id is not None and self._aggregate_rows and read_document(
                cast(ReadConnection, connection), run_id, max_bytes=self.max_bytes,
                aggregate_bytes=_READ_VIEW_BYTES, tables=_CANONICAL_TABLES,
            ))
            if not captured:
                connection.execute("BEGIN")
            if self._check_schema(connection, allow_empty=True):
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
                    if borrowed is None:
                        connection.close()

    def _check_size(
        self, state: Mapping[str, Any], *, fields: dict[str, str] | None = None,
        encoded_fields: Mapping[str, str] | None = None,
    ) -> int:
        # Encode one top-level field at a time with the C JSON encoder. Key
        # ordering does not change the byte count. This avoids both Python's
        # per-token iterencode hot path and an additional whole-Run JSON string.
        size = 3  # Braces and the former JSON authority's trailing newline.
        for position, (key, value) in enumerate(state.items()):
            size += bool(position) + len(_json(key).encode("utf-8")) + 1
            encoded = encoded_fields.get(key) if encoded_fields is not None else None
            if encoded is None:
                encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
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
                return self._copy_view(cached.view)
            materials: dict[str, str] = {}
            for identity, material_value, byte_length in source.material_rows():
                material_encoded = material_value.encode("utf-8")
                if len(material_encoded) != byte_length or hashlib.sha256(material_encoded).hexdigest() != identity:
                    raise ValueError("Invalid SQLite Run material identity")
                materials[identity] = material_value
            nodes: dict[tuple[str | int, ...], Any] = {}
            objects: dict[str, tuple[str, str]] = {}
            snapshots: dict[str, str] = {}
            snapshot_refs: dict[str, frozenset[str]] = {}
            snapshot_bytes = 0
            list_lengths: dict[tuple[str | int, ...], int] = {}
            for encoded_path, kind, encoded in source.object_rows():
                objects[encoded_path] = (kind, encoded)
                path = object_path(encoded_path)
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
            overridden = {path[:length] for path in nodes for length in range(1, len(path))}
            encodings = {object_path(path): plain for path, plain in snapshots.items()
                         if object_path(path) not in overridden}
            logical_bytes = self._check_size(root, fields=fields, encoded_fields=snapshot_fields(root, encodings))
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
                             snapshots, snapshot_refs)
                    if reusable or active else None)
            with self._read_view_lock:
                self._read_view = (_ReadView(view, source)
                                   if reusable and view is not None else None)
            if active:
                self._local.view = view
            if view is not None and view.fields is not None:
                self._local.last_read_view = weakref.ref(view)
            return root

    def _objects(
        self, state: Mapping[str, Any], *, previous: _RunView | None = None,
    ) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        objects: dict[str, tuple[str, str]] = {}
        materials: dict[str, str] = {}

        def material(value: str) -> str:
            identity = hashlib.sha256(value.encode("utf-8")).hexdigest()
            materials.setdefault(identity, value)
            return identity

        def reuse_material(identity: str) -> None:
            assert previous is not None
            if identity in materials:
                return
            text = previous.materials[identity]
            materials[identity] = text
            # Literal map subtrees contain no references. Tagged branches
            # contain the exact transitive dependencies of this object.
            if text.startswith("["):
                def references(value: Any) -> None:
                    if not isinstance(value, list):
                        return
                    kind, content = value
                    if kind == "map":
                        for item in content.values():
                            references(item)
                    elif kind == "list":
                        for item in content:
                            references(item)
                    elif kind == "snapshot":
                        reuse_material(content)
                    elif kind == "material":
                        materials[content] = previous.materials[content]
                references(json.loads(text))

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
            if isinstance(value, Mapping) and (fixed_snapshot or keyed_object):
                old = previous.objects.get(location) if previous is not None else None
                if previous is not None and old is not None and old[0] == "snapshot":
                    # Python equality conflates bool/int/float values. Reuse
                    # requires the exact JSON contents, including their types.
                    prior_json = previous.snapshots.get(location)
                    if prior_json is None:
                        prior: Any = previous.state
                        for position, part in enumerate(path):
                            if position == 0 and previous.fields is not None and isinstance(part, str) and part in previous.fields:
                                prior = json.loads(previous.fields[part])
                            else:
                                prior = prior[part]
                        prior_json = _json(prior)
                    if prior_json == _json(value):
                        objects[location] = old
                        references = previous.snapshot_refs.get(location)
                        if references is None:
                            reuse_material(json.loads(old[1]))
                        else:
                            materials.update((identity, previous.materials[identity]) for identity in references)
                        return
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
        self._check_size(state)
        previous = cast(_RunView | None, getattr(self._local, "view", None))
        if previous is not None and previous.run_id != run_id:
            previous = None
        objects, materials = self._objects(state, previous=previous)
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
        with self.atomic():
            # A following read must see this write, including normalization to
            # JSON types. A failed write also cannot leave a reusable old view.
            self._local.view = None
            connection = self._local.connection
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET repository=excluded.repository, "
                "parent_number=excluded.parent_number, status=excluded.status, phase=excluded.phase, "
                "created_at=excluded.created_at, updated_at=excluded.updated_at "
                "WHERE (repository, parent_number, status, phase, created_at, updated_at) "
                "IS NOT (excluded.repository, excluded.parent_number, excluded.status, "
                "excluded.phase, excluded.created_at, excluded.updated_at)",
                (run_id, metadata[0], parent_number, *metadata[1:]),
            )
            prior_objects = previous.objects if previous is not None else {
                path: (kind, value) for path, kind, value in connection.execute(
                    "SELECT object_path, kind, value_json FROM run_objects WHERE run_id = ?", (run_id,)
                )
            }
            self._aggregate_rows = patch_run(
                connection, run_id, objects=objects, previous=prior_objects, materials=materials,
                known_materials=previous.materials if previous is not None else {}, native=self._aggregate_rows,
            )

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
