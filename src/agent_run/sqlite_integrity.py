"""Bounded integrity checks for derived locators, sizes and material ownership."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from functools import lru_cache
from itertools import islice
from typing import Any

from agent_run.sqlite_encoding import encode_json

_MODULUS = 1 << 256
_DIGEST = re.compile(r"[0-9a-f]{64}")
KEYED_FIELDS = frozenset({"ticket_jobs", "ticket_agent_invocations", "retired_ticket_generations", "pending_ticket_retirements"})


# Job archives are independent canonical objects. Their proof can change with
# the current owner without decoding unchanged archived review evidence.
SOURCE_DIGEST_FIELDS = frozenset({"ticket_jobs", "active_ticket_job"})


def source_object_digest(path: str, source: tuple[str, str]) -> int:
    return int(hashlib.sha256(encode_json((path, *source)).encode()).hexdigest(), 16)


def source_objects_digest(objects: Mapping[str, tuple[str, str]]) -> str:
    return f"{sum(source_object_digest(path, source) for path, source in objects.items()) % _MODULUS:064x}"


def history_item_digest(position: str | int, item: Any) -> int:
    return int(hashlib.sha256(encode_json((position, item)).encode()).hexdigest(), 16)


def field_content_digest(field: str, value: Any) -> str:
    if isinstance(value, list):
        return f"{sum(history_item_digest(position, item) for position, item in enumerate(value)) % _MODULUS:064x}"
    if field in KEYED_FIELDS:
        return f"{sum(history_item_digest(key, item) for key, item in value.items()) % _MODULUS:064x}"
    return hashlib.sha256(encode_json(value).encode()).hexdigest()


def _field_size_proof(path: str, size: int, digest: str, kind: str, encoded: str, child_count: int | None) -> str:
    return hashlib.sha256(encode_json((path, size, digest, kind, encoded, child_count)).encode()).hexdigest()


_cached_field_size_proof = lru_cache(maxsize=4096, typed=True)(_field_size_proof)


def field_size_proof(path: str, size: int, digest: str, source: tuple[str, str], child_count: int | None) -> str:
    # Cache only this pure hash of bounded immutable content, never a database
    # validation result. Every read still supplies its actual source and index.
    calculate = _cached_field_size_proof if len(path) + len(digest) + sum(map(len, source)) <= 512 else _field_size_proof
    return calculate(path, size, digest, *source, child_count)


def root_fields(connection: sqlite3.Connection, run_id: str) -> set[str]:
    row = connection.execute("SELECT root_keys_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("Missing SQLite Run root manifest")
    value = json.loads(row[0])
    if (not isinstance(value, list) or any(not isinstance(field, str) for field in value)
            or len(set(value)) != len(value)):
        raise ValueError("Invalid SQLite Run root manifest")
    return set(value)


def require_field_sizes(connection: sqlite3.Connection, run_id: str, *, max_bytes: int) -> int:
    """Check every small field proof against its actual source object header.

    The root manifest prevents a missing derived row from hiding a field.
    Large maps/history are not decoded here. Their content digests are created
    with the authoritative payload and changed with it in the same transaction.
    """
    fields = root_fields(connection, run_id)
    total = 2 if fields else 3
    paths = [encode_json((field,)) for field in fields]
    rows: dict[str, tuple[Any, ...]] = {}
    # Bind exact manifest paths; do not scan Invocation indexes or decode any
    # archived payload. Batches also support SQLite's lower variable limits.
    for offset in range(0, len(paths), 256):
        batch = paths[offset:offset + 256]
        rows.update((row[0], row[1:]) for row in connection.execute(
            "SELECT o.object_path,i.logical_bytes,i.content_digest,i.size_proof,o.kind,o.value_json,i.child_count "
            "FROM run_objects o JOIN run_object_index i USING(run_id,object_path) "
            f"WHERE o.run_id=? AND o.object_path IN ({','.join('?' for _ in batch)})", (run_id, *batch),
        ))
    for path in paths:
        row = rows.get(path)
        if row is None:
            raise ValueError("Missing SQLite field size proof")
        size, digest, proof, kind, encoded, child_count = row
        if (type(size) is not int or size <= 0 or not isinstance(digest, str)
                or _DIGEST.fullmatch(digest) is None
                or (child_count is not None and (type(child_count) is not int or child_count < 0))
                or kind == "list" and child_count != json.loads(encoded)
                or proof != field_size_proof(path, size, digest, (kind, encoded), child_count)):
            raise ValueError("Invalid SQLite field size proof")
        total += size
    if total > max_bytes:
        raise ValueError(f"Run state exceeds {max_bytes} byte persistence limit")
    return total


def require_material_counts(connection: sqlite3.Connection, run_id: str, identities: Iterable[str]) -> None:
    iterator = iter(identities)
    while batch := tuple(islice(iterator, 256)):
        # Keep each requested identity even when its Run reference is missing;
        # aggregating only existing references would hide dangling dependencies.
        requested = ",".join("(?)" for _ in batch)
        rows = connection.execute(
            f"WITH requested(material_id) AS (VALUES {requested}) "
            "SELECT r.material_id,r.object_refcount,(SELECT count(*) FROM run_object_materials o "
            "WHERE o.run_id=? AND o.material_id=q.material_id) FROM requested q "
            "LEFT JOIN run_material_refs r ON r.run_id=? AND r.material_id=q.material_id",
            (*batch, run_id, run_id),
        )
        for linked, stored, actual in rows:
            if (linked is None and actual != 0 or linked is not None
                    and (type(stored) is not int or stored <= 0 or stored != actual)):
                raise ValueError("Invalid SQLite material dependency count")


def sync_material_counts(connection: sqlite3.Connection, run_id: str, identities: Iterable[str] | None = None) -> None:
    if identities is None:
        # Complete creation/save already owns all authoritative payloads.
        connection.execute(
            "UPDATE run_material_refs SET object_refcount=(SELECT count(*) FROM run_object_materials o "
            "WHERE o.run_id=run_material_refs.run_id AND o.material_id=run_material_refs.material_id) WHERE run_id=? "
            "AND object_refcount IS NOT (SELECT count(*) FROM run_object_materials o "
            "WHERE o.run_id=run_material_refs.run_id AND o.material_id=run_material_refs.material_id)",
            (run_id,),
        )
    else:
        iterator = iter(identities)
        while batch := tuple(islice(iterator, 256)):
            connection.execute(
                "UPDATE run_material_refs SET object_refcount=(SELECT count(*) FROM run_object_materials o "
                "WHERE o.run_id=run_material_refs.run_id AND o.material_id=run_material_refs.material_id) "
                f"WHERE run_id=? AND material_id IN ({','.join('?' for _ in batch)})",
                (run_id, *batch),
            )


def source_material_dependencies(
    connection: sqlite3.Connection, run_id: str, objects: Mapping[str, tuple[str, str]],
) -> set[str]:
    """Collect only changed objects' real material references, not their index."""
    result: set[str] = set()
    def material(identity: str, *, snapshot: bool) -> None:
        if identity in result:
            return
        row = connection.execute(
            "SELECT m.value,m.byte_length FROM run_materials m JOIN run_material_refs r USING(material_id) "
            "WHERE r.run_id=? AND m.material_id=?", (run_id, identity),
        ).fetchone()
        if row is None:
            raise ValueError("Missing SQLite Run material")
        text, size = row
        encoded = text.encode()
        if len(encoded) != size or hashlib.sha256(encoded).hexdigest() != identity:
            raise ValueError("Invalid SQLite Run material identity")
        result.add(identity)
        if snapshot:
            visit(json.loads(text))
    def visit(value: Any) -> None:
        if not isinstance(value, list) or len(value) != 2:
            return
        kind, content = value
        if kind == "map":
            for child in content.values():
                visit(child)
        elif kind == "list":
            for child in content:
                visit(child)
        elif kind in {"snapshot", "material"}:
            material(content, snapshot=kind == "snapshot")
    for kind, encoded in objects.values():
        if kind in {"material", "snapshot"}:
            material(json.loads(encoded), snapshot=kind == "snapshot")
    return result


def require_material_payloads(connection: sqlite3.Connection, materials: Mapping[str, str]) -> None:
    """A reused content address must still contain the exact proposed bytes."""
    for identity, value in materials.items():
        row = connection.execute("SELECT value,byte_length FROM run_materials WHERE material_id=?", (identity,)).fetchone()
        if row is not None and row != (value, len(value.encode())):
            raise ValueError("Invalid SQLite Run material identity")
