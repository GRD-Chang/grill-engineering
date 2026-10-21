"""Offline v6 capture, without importing its runtime or changing its authority.

Capture and exact-byte restore are physical copies, not new-to-old conversion.
The narrow selected-Ticket converter lives separately. A v6 snapshot cannot
serve as rollback authority after the new runtime executes; no inverse facts
or external-effect reconciliation are implemented here. The Linux SQLite lock
bytes exclude other SQLite processes; open handles in this process are refused
as POSIX record locks would otherwise be shared.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Iterator


SOURCE_PROTOCOL = "sqlite-work-records-v6"
_DATABASE_LIMIT = 256 * 1024 * 1024
_SQLITE_PENDING_BYTE = 0x40000000
_SQLITE_LOCK_BYTES = 512
SOURCE_TABLES = frozenset({
    "schema_meta", "runs", "work_jobs", "work_attempts", "work_attempt_versions",
    "work_invocations", "work_evidence", "work_evidence_materials",
})


class MigrationRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceSnapshot:
    source: Path
    path: Path
    sha256: str
    source_protocol: str
    schema_sha256: str
    table_rows: tuple[tuple[str, int], ...]


def source_identity(path: Path) -> os.stat_result:
    """Check the inode before opening it; this does not prove another Host's exit.

    Closing any new descriptor for an already held source could release this
    process's POSIX capture lock. Both capture and admission must refuse first.
    """
    if path.is_symlink():
        raise MigrationRefused("source_symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _DATABASE_LIMIT:
        raise MigrationRefused("source_size_or_kind")
    if info.st_nlink != 1:
        # Another name can have its own nonempty WAL. The main inode lock does
        # not establish that this pathname is the only journal authority.
        raise MigrationRefused("source_alias")
    # Refuse BEFORE opening another source descriptor: even closing a refused
    # descriptor could cancel an existing SQLite connection's POSIX locks.
    for entry in Path("/proc/self/fd").iterdir():
        try:
            opened = entry.stat()
        except FileNotFoundError:
            continue
        if (opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino):
            raise MigrationRefused("source_not_offline")
    return info


@contextmanager
def _offline_source(path: Path) -> Iterator[int]:
    info = source_identity(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise MigrationRefused("source_replaced")
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB,
                        _SQLITE_LOCK_BYTES, _SQLITE_PENDING_BYTE)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise MigrationRefused("source_not_offline") from error
            raise
        # Under the exclusive SQLite lock, an empty WAL/journal has no frame or
        # recovery header. Read-only v6 queries can leave an empty WAL and SHM;
        # neither is modified. Any nonempty sidecar still requires recovery and
        # is refused, not checkpointed or treated as missing.
        for suffix in ("-wal", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_symlink() or sidecar.exists() and sidecar.stat().st_size != 0:
                raise MigrationRefused("source_sidecar")
        current = path.stat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise MigrationRefused("source_replaced")
        yield descriptor
    finally:
        os.close(descriptor)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _source_blocks(descriptor: int) -> Iterator[bytes]:
    offset = 0
    while block := os.pread(descriptor, 1024 * 1024, offset):
        offset += len(block)
        if offset > _DATABASE_LIMIT:
            raise MigrationRefused("source_size_or_kind")
        yield block


def _inspect_copy(path: Path) -> tuple[str, tuple[tuple[str, int], ...]]:
    """Check the copied container; this does not certify all v6 domain facts."""
    if not 0 < path.stat().st_size <= _DATABASE_LIMIT:
        raise MigrationRefused("snapshot_size")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
            raise MigrationRefused("snapshot_corrupt")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MigrationRefused("snapshot_foreign_key")
        count, size = connection.execute(
            "SELECT count(*),coalesce(sum(length(CAST(type AS BLOB))+length(CAST(name AS BLOB))"
            "+coalesce(length(CAST(sql AS BLOB)),0)),0) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchone()
        if count > 512 or size > 2 * 1024 * 1024:
            raise MigrationRefused("snapshot_schema_size")
        schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchmany(513)
        if len(schema) > 512 or any(kind not in {"table", "index"} for kind, _, _ in schema):
            raise MigrationRefused("snapshot_schema")
        tables = [name for kind, name, _ in schema if kind == "table"]
        if not SOURCE_TABLES <= set(tables):
            raise MigrationRefused("snapshot_schema")
        metadata_count, metadata_size = connection.execute(
            "SELECT count(*),coalesce(sum(length(CAST(key AS BLOB))+length(CAST(value AS BLOB))),0) FROM schema_meta"
        ).fetchone()
        if metadata_count != 1 or metadata_size > 128:
            raise MigrationRefused("source_protocol")
        protocol = connection.execute("SELECT key,value FROM schema_meta").fetchmany(2)
        if protocol != [("protocol", SOURCE_PROTOCOL)]:
            raise MigrationRefused("source_protocol")
        encoded = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > 2 * 1024 * 1024:
            raise MigrationRefused("snapshot_schema_size")
        counts = tuple((name, connection.execute(
            'SELECT count(*) FROM "' + name.replace('"', '""') + '"'
        ).fetchone()[0]) for name in sorted(tables))
        return hashlib.sha256(encoded).hexdigest(), counts
    except sqlite3.Error as error:
        raise MigrationRefused("snapshot_corrupt") from error
    finally:
        connection.close()


def capture_v6(source: Path, destination: Path) -> SourceSnapshot:
    """Publish an exact, verified offline copy; never overwrite either file.

    The parent directory must already exist. Source write access is required
    solely for a Linux write lock; no SQLite connection opens the source and
    no source byte is written. Run in a dedicated offline process: no other
    thread may open the source. Capture does not grant new-runtime authority.
    """
    source, destination = source.absolute(), destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise MigrationRefused("destination_exists")
    with _offline_source(source) as descriptor:
        temporary: Path | None = None
        published = False
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".v6-capture-", delete=False) as stream:
                temporary = Path(stream.name)
                digest = hashlib.sha256()
                for block in _source_blocks(descriptor):
                    stream.write(block)
                    digest.update(block)
                stream.flush()
                os.fsync(stream.fileno())
            schema_digest, counts = _inspect_copy(temporary)
            # Opening and closing another descriptor for this inode would
            # release this process's POSIX locks. Reuse the locked descriptor.
            verified = hashlib.sha256()
            for block in _source_blocks(descriptor):
                verified.update(block)
            current, opened = source.stat(), os.fstat(descriptor)
            if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or verified.digest() != digest.digest()):
                raise MigrationRefused("source_changed")
            # A hard link publishes a completed file atomically and refuses an
            # intervening destination, unlike rename/replace over another file.
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise MigrationRefused("destination_exists") from error
            published = True
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return SourceSnapshot(source, destination, digest.hexdigest(), SOURCE_PROTOCOL, schema_digest, counts)
        except BaseException:
            if published and temporary is not None:
                try:
                    if destination.samefile(temporary):
                        destination.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def validate_snapshot(snapshot: SourceSnapshot) -> SourceSnapshot:
    """Read back the exact captured bytes, schema and relation cardinalities."""
    if snapshot.path.is_symlink() or _file_digest(snapshot.path) != snapshot.sha256:
        raise MigrationRefused("snapshot_changed")
    schema_digest, counts = _inspect_copy(snapshot.path)
    if (snapshot.source_protocol != SOURCE_PROTOCOL or schema_digest != snapshot.schema_sha256
            or counts != snapshot.table_rows or _file_digest(snapshot.path) != snapshot.sha256):
        raise MigrationRefused("snapshot_changed")
    return snapshot


def _require_no_v6_notification_journal(snapshot: SourceSnapshot, run_id: str) -> None:
    """A copied database alone cannot preserve the old sender's delivery budget."""
    try:
        if not stat.S_ISREG(snapshot.source.lstat().st_mode):
            raise MigrationRefused("source_notification_journal_unproved")
        root = snapshot.source.resolve(strict=True).parent
        directory = root / "notifications"
        try:
            directory_mode = directory.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(directory_mode):
            raise MigrationRefused("source_notification_journal_unproved")
        name = hashlib.sha256(run_id.encode()).hexdigest()[:32] + ".json"
        journal = directory / name
        try:
            journal.lstat()
        except FileNotFoundError:
            return
        raise MigrationRefused("unsupported_source_notification_journal")
    except OSError as error:
        raise MigrationRefused("source_notification_journal_unproved") from error


def restore_v6(snapshot: SourceSnapshot, destination: Path) -> SourceSnapshot:
    """Restore exact v6 bytes to a new path as an unchanged-old physical copy.

    This does not reverse new state or authorize old-runtime resumption after
    new execution; such resumption needs a separately proven effect boundary.
    """
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise MigrationRefused("destination_exists")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".v6-restore-") as directory:
        restored = capture_v6(snapshot.path, Path(directory) / "state.sqlite3")
        if ((restored.sha256, restored.source_protocol, restored.schema_sha256, restored.table_rows)
                != (snapshot.sha256, snapshot.source_protocol, snapshot.schema_sha256, snapshot.table_rows)):
            raise MigrationRefused("snapshot_changed")
        try:
            try:
                os.link(restored.path, destination)
            except FileExistsError as error:
                raise MigrationRefused("destination_exists") from error
            parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except BaseException:
            try:
                if destination.samefile(restored.path):
                    destination.unlink()
            except FileNotFoundError:
                pass
            raise
    return SourceSnapshot(snapshot.path, destination, snapshot.sha256, snapshot.source_protocol,
                          snapshot.schema_sha256, snapshot.table_rows)


def _evidence_value(connection: sqlite3.Connection, run_id: str, evidence_id: str,
                    kind: str) -> object:
    """Resolve one v6 fact through its digest-checked material, not a Run blob."""
    row = connection.execute(
        "SELECT owner_id,kind,value_digest,value_bytes,material_id,source_evidence_id "
        "FROM work_evidence WHERE run_id=? AND evidence_id=?", (run_id, evidence_id)
    ).fetchone()
    if (row is None or row[0] != f"run:{run_id}" or row[1] != kind
            or not isinstance(row[3], int) or not 0 <= row[3] <= 4 * 1024 * 1024
            or row[4] != row[2] or row[5] is not None):
        raise MigrationRefused("source_evidence")
    material = connection.execute(
        "SELECT body,byte_length FROM work_evidence_materials WHERE material_id=?", (row[4],)
    ).fetchone()
    if (material is None or not isinstance(material[0], str)
            or not isinstance(material[1], int) or material[1] != row[3]):
        raise MigrationRefused("source_evidence")
    body = material[0].encode("utf-8")
    if (len(body) != row[3] or len(body) != material[1]
            or hashlib.sha256(body).hexdigest() != row[2]):
        raise MigrationRefused("source_evidence")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, ValueError) as error:
        raise MigrationRefused("source_evidence") from error


def _created_at_epoch(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise MigrationRefused("source_created_at") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MigrationRefused("source_created_at")
    epoch = parsed.timestamp()
    if not math.isfinite(epoch):
        raise MigrationRefused("source_created_at")
    return epoch
