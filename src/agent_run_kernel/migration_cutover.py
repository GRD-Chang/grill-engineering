"""A fenced cutover of one private, copied, idle v6 Run.

The v6 copy has no Executor, Worker, Attempt or external write.  Changing its
protocol blocks unchanged v6 entry points; it does not retire the original Run
or prove a real systemd Host has exited.  A caller supplies the native Host
preflight and must settle that Host before requesting the no-effect return.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
from typing import Callable, cast

from agent_run.task_identity import TaskKey

from .kernel import Kernel
from .migration import (MigrationRefused, SourceSnapshot, _offline_source, _source_blocks,
                        restore_v6)
from .model import HostLaunch, wire_json
from .task_location import bind_task, locked_task, read_task


_OLD_PROTOCOL = "sqlite-work-records-v6"
_RETIRED_PREFIX = "retired-to-native-copy/1:"


def _marker(receipt: dict[str, str]) -> str:
    return _RETIRED_PREFIX + base64.urlsafe_b64encode(wire_json(receipt)).decode("ascii")


def _read_marker(value: str) -> dict[str, str]:
    if not value.startswith(_RETIRED_PREFIX) or len(value) > 8192:
        raise MigrationRefused("copied_v6_fence_changed")
    try:
        body = json.loads(base64.urlsafe_b64decode(value[len(_RETIRED_PREFIX):]))
    except (binascii.Error, ValueError, UnicodeError) as error:
        raise MigrationRefused("copied_v6_fence_changed") from error
    if (not isinstance(body, dict) or set(body) != {
            "nonce", "old_db", "new_db", "run_id", "source_sha256", "task_fingerprint"}
            or any(not isinstance(item, str) or not item for item in body.values())
            or _marker(body) != value):
        raise MigrationRefused("copied_v6_fence_changed")
    return cast(dict[str, str], body)


def _protocol_from_locked_copy(descriptor: int, parent: Path) -> str:
    """Inspect a different inode while the marked v6 main-file lock is held."""
    with tempfile.TemporaryDirectory(dir=parent, prefix=".v6-fence-read-") as directory:
        copied = Path(directory) / "state.sqlite3"
        with copied.open("xb") as target:
            for block in _source_blocks(descriptor):
                target.write(block)
        try:
            with closing(sqlite3.connect(copied.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                row = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()
        except sqlite3.Error as error:
            raise MigrationRefused("copied_v6_fence_changed") from error
        return "" if row is None else str(row[0])


def _private_paths(snapshot: SourceSnapshot, old_copy: Path, native: Kernel,
                   private_root: Path) -> tuple[Path, Path, Path]:
    root, old, new = private_root.resolve(), old_copy.absolute(), native.storage.path.absolute()
    if (old.is_symlink() or old.exists() or old.parent.exists() or old.parent.parent != root
            or new.is_symlink() or not new.is_file() or not new.resolve().is_relative_to(root)
            or snapshot.path.resolve().is_relative_to(root)
            or snapshot.source.resolve().is_relative_to(root)
            or not root.is_dir()):
        raise MigrationRefused("private_copy_required")
    return root, old, new


def _native_idle(native: Kernel, source_sha256: str) -> tuple[str, TaskKey, str]:
    with native.storage.transaction(write=False) as tx:
        run = tx.one("SELECT * FROM runs")
        subjects = tx.all("SELECT id,phase FROM subjects WHERE kind='ticket'")
        if (len(subjects) != 1 or subjects[0]["phase"] != "workspace"
                or run["stopped"] or run["executor"] is not None
                or run["repository_root"] is None
                or any(tx.one(f"SELECT count(*) FROM {name}")[0] for name in (
                    "effects", "invocations", "hosts", "leases", "actions"))
                or tx.rows("SELECT identity FROM history WHERE kind='v6_private_cutover'")):
            raise MigrationRefused("native_cutover_not_idle")
        imported = tx.rows("SELECT body FROM material WHERE subject=? AND purpose='v6_import_identity'",
                           (subjects[0]["id"],))
        if len(imported) != 1 or json.loads(imported[0]["body"]).get("source_sha256") != source_sha256:
            raise MigrationRefused("native_source_copy_mismatch")
        task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
        return str(run["id"]), task, str(subjects[0]["id"])


def _retire_old_copy(old: Path, receipt: dict[str, str], native: Kernel) -> None:
    with closing(sqlite3.connect(old.resolve().as_uri() + "?mode=rw", uri=True,
                                 isolation_level=None, timeout=5.0)) as db:
        db.execute("PRAGMA synchronous=FULL")
        if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
            raise MigrationRefused("copied_v6_journal")
        try:
            db.execute("BEGIN IMMEDIATE")
            if dict(db.execute("SELECT key,value FROM schema_meta")) != {"protocol": _OLD_PROTOCOL}:
                raise MigrationRefused("copied_v6_protocol")
            old_run = db.execute("SELECT run_id,repository,parent_number,status FROM runs").fetchall()
            with native.storage.transaction(write=False) as tx:
                new_run = tx.one("SELECT id,repository,parent_issue FROM runs")
                if (old_run != [(new_run["id"], new_run["repository"],
                                new_run["parent_issue"], "active")]
                        or old_run[0][0] != receipt["run_id"]):
                    raise MigrationRefused("copied_v6_identity")
            if (any(db.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in (
                    "control_tasks", "control_actions", "control_executors", "control_workers",
                    "work_attempts", "work_invocations"))
                    or db.execute("SELECT count(*) FROM work_jobs").fetchone()[0] != 1):
                raise MigrationRefused("old_process_or_work_unproved")
            db.execute("UPDATE schema_meta SET value=? WHERE key='protocol'", (_marker(receipt),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    if any(Path(str(old) + suffix).exists() and Path(str(old) + suffix).stat().st_size
           for suffix in ("-wal", "-journal")):
        raise MigrationRefused("copied_v6_sidecar")
    descriptor = os.open(old.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cutover_idle_copied_v6(
    snapshot: SourceSnapshot, old_copy: Path, native: Kernel, *, private_root: Path,
    action_id: str, command: tuple[str, ...], environment_carrier: Path,
    preflight: Callable[[], None],
) -> tuple[str, HostLaunch]:
    """Retire one owned v6 copy, then bind the already converted native Host.

    Any failure after the old protocol commit leaves the copy retired.  It is
    never silently restored because a new Host allocation may have succeeded.
    """
    _, old, new = _private_paths(snapshot, old_copy, native, private_root)
    run_id, task, subject = _native_idle(native, snapshot.sha256)
    if not task.workspace.is_relative_to(private_root.resolve()):
        raise MigrationRefused("private_native_task_required")
    receipt = {"nonce": secrets.token_hex(16), "old_db": str(old), "new_db": str(new),
               "run_id": run_id, "source_sha256": snapshot.sha256,
               "task_fingerprint": task.fingerprint}
    old.parent.mkdir(mode=0o700)
    retired = False
    try:
        copied = restore_v6(snapshot, old)
        if copied.sha256 != snapshot.sha256:
            raise MigrationRefused("copied_v6_changed")
        with native.storage.transaction() as tx:
            material = tx.put(subject, "v6_private_cutover", wire_json(receipt))
            tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) "
                       "VALUES (?,?,?,?,1,?)", (subject, "v6_private_cutover",
                                               receipt["nonce"], material, native.clock()))
        retired = True
        _retire_old_copy(old, receipt, native)
        launch = native.admit_host(action_id, kind="run", command=command,
                                   environment_carrier=environment_carrier,
                                   preflight=preflight)
        return receipt["nonce"], launch
    except BaseException:
        if not retired and old.parent.exists():
            shutil.rmtree(old.parent)
        raise


def restore_idle_copied_v6_without_new_effects(
    snapshot: SourceSnapshot, old_copy: Path, native: Kernel, *, nonce: str,
) -> Path:
    """Return a stopped, effect-free private copy to its exact v6 bytes.

    The native Task locator is first pointed at the incompatible v6 database,
    so native re-entry fails closed.  The original v6 source is never changed.
    """
    old, new = old_copy.absolute(), native.storage.path.absolute()
    if old.is_symlink() or not old.is_file() or not nonce:
        raise MigrationRefused("copied_v6_fence_missing")
    with _offline_source(old) as descriptor:
        receipt = _read_marker(_protocol_from_locked_copy(descriptor, old.parent))
    if (receipt["nonce"] != nonce or receipt["old_db"] != str(old)
            or receipt["new_db"] != str(new) or receipt["source_sha256"] != snapshot.sha256):
        raise MigrationRefused("copied_v6_fence_identity")
    with native.storage.transaction(write=False) as tx:
        run = tx.one("SELECT * FROM runs")
        task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
    # Own a fresh staging directory. A deterministic same-nonce pathname can
    # belong to a concurrent attempt; a failed no-clobber restore must never
    # remove its file in our cleanup.
    staging = Path(tempfile.mkdtemp(prefix=".restored-v6-", dir=old.parent))
    restored = staging / "state.sqlite3"
    try:
        if restore_v6(snapshot, restored).sha256 != snapshot.sha256:
            raise MigrationRefused("restored_v6_changed")
        with locked_task(task) as locator:
            # Match Kernel.admit_host's Task -> Storage lock order. Keep the
            # writer lock until the old copy has been replaced so a concurrent
            # native write cannot invalidate the no-effect return proof.
            with native.storage.transaction() as tx:
                run = tx.one("SELECT * FROM runs")
                if (run["id"] != receipt["run_id"] or TaskKey(
                        Path(run["repository_root"]), run["repository"], run["parent_issue"]) != task
                        or task.fingerprint != receipt["task_fingerprint"]
                        or not run["stopped"] or run["executor"] is not None
                        or any(tx.one(f"SELECT count(*) FROM {name}")[0] for name in (
                            "effects", "invocations", "invocation_outputs", "leases", "resource_cleanup"))
                        or tx.rows("SELECT owner FROM hosts WHERE status!='exited' OR usage_released=0")
                        or tx.rows("SELECT owner FROM retired_executors WHERE status!='exited'")):
                    raise MigrationRefused("native_effect_or_host_unsettled")
                history = tx.rows("SELECT subject,material_id FROM history WHERE kind='v6_private_cutover' AND identity=?",
                                  (nonce,))
                if len(history) != 1 or json.loads(tx.get(history[0]["material_id"], history[0]["subject"])) != receipt:
                    raise MigrationRefused("copied_v6_fence_identity")
                if read_task(locator) != (receipt["run_id"], new):
                    raise MigrationRefused("native_task_location_changed")
                with _offline_source(old) as descriptor:
                    if _protocol_from_locked_copy(descriptor, old.parent) != _marker(receipt):
                        raise MigrationRefused("copied_v6_fence_changed")
                    if os.fstat(descriptor).st_nlink != 1:
                        raise MigrationRefused("copied_v6_changed")
                    # The v6 DB is incompatible with native admission. A
                    # durable locator tombstone precedes restoring its bytes.
                    bind_task(locator, receipt["run_id"], old)
                    os.replace(restored, old)
                    parent_fd = os.open(old.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
    finally:
        shutil.rmtree(staging)
    return old
