"""Reject existing v6 Task authority at native admission, without running v6.

The original Task lock precedes the native Task lock and both outlive the
native transaction. This read barrier does not make an unchanged old CLI obey
native ownership: actual migration must durably retire the original database.
Runner installation separately excludes switching a live installed runtime.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from agent_run.managed_workspace import ManagedWorkspace, ManagedWorkspaceError, normalize_repository
from agent_run.paths import app_data_root
from agent_run.task_identity import TaskKey

from .migration import SOURCE_PROTOCOL, SOURCE_TABLES, source_identity
from .migration_quiescence import read_v6_host_exit
from .model import KernelError, wire_json
from .storage import Storage, Transaction
from .task_location import locked_task, read_task, task_path


_SOURCE_FENCE_PREFIX = "original-source-to-native/1:"
_SOURCE_FENCE_KEYS = frozenset({"nonce", "source_db", "source_sha256", "run_id", "task_fingerprint",
                                "source_uuid", "target_db"})
_SOURCE_FENCE_LIMIT = 16 * 1024


@dataclass(frozen=True, order=True)
class _Source:
    state: Path
    run_id: str = ""
    routed: bool = False


def _refuse() -> KernelError:
    return KernelError("legacy_requires_offline_migration",
                       "The original Task authority must be retired by an explicit offline migration")


def _fence_material(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _SOURCE_FENCE_KEYS:
        raise _refuse()
    receipt: dict[str, str] = {}
    for key in sorted(_SOURCE_FENCE_KEYS):
        item = value[key]
        if not isinstance(item, str) or not item or any(ord(character) < 32 for character in item):
            raise _refuse()
        receipt[key] = item
    for key, length in (("nonce", 32), ("source_uuid", 32), ("source_sha256", 64), ("task_fingerprint", 64)):
        if len(receipt[key]) != length or any(character not in "0123456789abcdef" for character in receipt[key]):
            raise _refuse()
    for key in ("source_db", "target_db"):
        path = Path(receipt[key])
        if (not path.is_absolute() or str(path) != receipt[key] or ".." in path.parts
                or receipt[key].startswith("//") or len(receipt[key]) > 4096):
            raise _refuse()
    if receipt["source_db"] == receipt["target_db"] or len(receipt["run_id"]) > 256:
        raise _refuse()
    return receipt


def source_fence(receipt: Mapping[str, str]) -> str:
    """Encode the shared original-source receipt; encoding grants no admission."""
    protocol = _SOURCE_FENCE_PREFIX + base64.urlsafe_b64encode(wire_json(_fence_material(dict(receipt)))).decode("ascii")
    if len(protocol) > _SOURCE_FENCE_LIMIT:
        raise _refuse()
    return protocol


def read_source_fence(protocol: str) -> dict[str, str]:
    """Read only this exact canonical protocol, never a private-copy marker."""
    if not protocol.startswith(_SOURCE_FENCE_PREFIX) or len(protocol) > _SOURCE_FENCE_LIMIT:
        raise _refuse()
    try:
        encoded = protocol[len(_SOURCE_FENCE_PREFIX):].encode("ascii")
        receipt = _fence_material(json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True)))
    except (ValueError, UnicodeError) as error:
        raise _refuse() from error
    if source_fence(receipt) != protocol:
        raise _refuse()
    return receipt


def origin_reference(database: Path, task: TaskKey, run_id: str) -> dict[str, str] | None:
    """Read this native Run's immutable origin reference, without deciding replacement."""
    if database.is_symlink() or database.resolve() != database:
        raise _refuse()
    storage = Storage(database)
    with storage.transaction(write=False) as tx:
        rows = tx.rows("SELECT subject,kind,identity,material_id FROM history WHERE kind IN ('v6_original_cutover','v6_source_origin')", limit=2)
        if not rows:
            return None
        if len(rows) != 1:
            raise _refuse()
        run = tx.one("SELECT id,repository,parent_issue,repository_root FROM runs")
        if (run["id"] != run_id or run["repository_root"] != str(task.workspace)
                or run["repository"].lower() != task.repository or run["parent_issue"] != task.parent_number):
            raise _refuse()
        row = rows[0]
        material = tx.one("SELECT purpose FROM material WHERE id=?", (row["material_id"],))
        body = tx.get(row["material_id"], row["subject"])
        value = json.loads(body)
        if material[0] != row["kind"] or wire_json(value) != body:
            raise _refuse()
        if row["kind"] == "v6_original_cutover":
            receipt = _fence_material(value)
            if (receipt["run_id"] != run_id or receipt["target_db"] != str(database)
                    or receipt["task_fingerprint"] != task.fingerprint or row["identity"] != receipt["nonce"]):
                raise _refuse()
            return {"origin_db": str(database), "nonce": receipt["nonce"]}
        if (not isinstance(value, dict) or set(value) != {"origin_db", "nonce"}
                or not all(isinstance(item, str) for item in value.values())
                or row["identity"] != value["nonce"]
                or len(value["nonce"]) != 32 or any(character not in "0123456789abcdef" for character in value["nonce"])
                or not Path(value["origin_db"]).is_absolute() or Path(value["origin_db"]).resolve() != Path(value["origin_db"])
                or database != Path(value["origin_db"]).parent.parent / run_id / "kernel.sqlite3"):
            raise _refuse()
        return {"origin_db": value["origin_db"], "nonce": value["nonce"]}


def _returned_transfer_identity(tx: Transaction, imported: sqlite3.Row, body: dict[str, Any],
                                receipt: dict[str, str], managed: ManagedWorkspace,
                                db: sqlite3.Connection) -> None:
    """Authenticate immutable conversion facts, without reauthorizing a live checkout."""
    run_id = receipt["run_id"]
    jobs = db.execute("SELECT j.job_id,j.value_ticket_number,j.value_ticket_branch,j.value_candidate_sha,j.value_base_sha "
                      "FROM work_jobs j JOIN work_job_layout l ON l.run_id=j.run_id AND l.active_job_id=j.job_id "
                      "WHERE j.run_id=?", (run_id,)).fetchall()
    roots = db.execute("SELECT r.value_run_branch,b.value_sha FROM run_root r "
                       "JOIN run_condition_base b ON b.run_id=r.run_id WHERE r.run_id=?", (run_id,)).fetchall()
    if len(jobs) != 1 or len(roots) != 1:
        raise _refuse()
    factory = db.row_factory
    try:
        db.row_factory = sqlite3.Row
        read_v6_host_exit(db, run_id, required=True)
    finally:
        db.row_factory = factory
    job, root = jobs[0], roots[0]
    ticket, source_ref = job[1], "refs/heads/" + job[2]
    if (type(ticket) is not int or ticket < 1 or job[0] != f"ticket:{ticket}:generation:1"
            or imported["subject"] != f"ticket:{ticket}" or imported["generation"] != 1
            or imported["identity"] != receipt["source_sha256"] or imported["purpose"] != "v6_returned_import"
            or body.get("source_record") != {"table": "work_jobs", "key": [run_id, job[0]]}):
        raise _refuse()
    for key in ("profile_sha256", "external_readproof_sha256", "checkout_index_sha256"):
        value = body.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise _refuse()
    layout = body.get("original_layout")
    if not isinstance(layout, dict):
        raise _refuse()
    archive = layout.get("checkout_archive_sha256")
    if not isinstance(archive, str) or len(archive) != 64 or any(c not in "0123456789abcdef" for c in archive):
        raise _refuse()
    checkout = managed.state_root / "worktrees" / run_id / f"ticket-{ticket}"
    expected = {"version": 1, "nonce": receipt["nonce"], "source_sha256": receipt["source_sha256"],
                "source_uuid": receipt["source_uuid"], "repository_root": str(managed.repository_root),
                "checkout": str(checkout), "owned_root": str(checkout.parent),
                "common_git_dir": str(managed.repository_root / ".git"), "source_ref": source_ref,
                "expected_oid": job[3] or job[4], "before_head": "ref: " + source_ref + "\n",
                "checkout_archive_sha256": archive}
    saved_layout = tx.one("SELECT h.subject,h.identity,h.material_id,m.purpose FROM history h "
                          "JOIN material m ON m.id=h.material_id WHERE h.kind='v6_original_layout'")
    observed = tx.one("SELECT h.subject,h.identity,h.material_id,m.purpose FROM history h "
                      "JOIN material m ON m.id=h.material_id WHERE h.kind='v6_external_readproof'")
    proof_bytes = tx.get(observed["material_id"], observed["subject"])
    proof = json.loads(proof_bytes)
    if (layout != expected or body.get("run_ref") != "refs/heads/" + root[0]
            or body.get("run_ref_ownership") != {"source_ref": "refs/heads/" + root[0], "expected_oid": root[1],
                                                 "source_record": {"table": "run_root", "key": [run_id]}}
            or saved_layout["subject"] != imported["subject"] or saved_layout["identity"] != receipt["nonce"]
            or saved_layout["purpose"] != "v6_original_layout"
            or tx.get(saved_layout["material_id"], saved_layout["subject"]) != wire_json(expected)
            or observed["subject"] != imported["subject"] or observed["purpose"] != "v6_external_readproof"
            or observed["identity"] != body["external_readproof_sha256"]
            or hashlib.sha256(proof_bytes).hexdigest() != observed["identity"]
            or proof.get("source_kind") != "returned_role" or proof.get("source_sha256") != receipt["source_sha256"]
            or proof.get("checkout_archive_sha256") != archive
            or proof.get("checkout_index_sha256") != body["checkout_index_sha256"]):
        raise _refuse()


def _original_transfer(task: TaskKey, source: _Source, path: Path,
                       db: sqlite3.Connection, protocol: str) -> dict[str, str] | None:
    """Verify the retired source and its initial native proof; never interpret old work."""
    receipt = read_source_fence(protocol)
    old_tasks = db.execute("SELECT task_id,workspace,repository,parent_number,run_id FROM control_tasks LIMIT 2").fetchall()
    old_runs = db.execute("SELECT run_id,repository,parent_number FROM runs LIMIT 2").fetchall()
    if len(old_tasks) != 1 or len(old_runs) != 1:
        raise _refuse()
    old_task = old_tasks[0]
    transferred = TaskKey(Path(old_task[1]), old_task[2], old_task[3])
    managed = ManagedWorkspace.for_repository(transferred.repository)
    if (transferred.workspace != task.workspace or transferred.workspace != managed.repository_root.resolve()
            or source.state != managed.state_root or path.resolve() != path
            or receipt["source_db"] != str(path) or receipt["task_fingerprint"] != transferred.fingerprint
            or old_task[0] != transferred.fingerprint or old_task[4] != receipt["run_id"]
            or old_runs != [(receipt["run_id"], transferred.repository, transferred.parent_number)]
            or receipt["target_db"] != str(managed.state_root / receipt["run_id"] / "kernel.sqlite3")):
        raise _refuse()
    roots = db.execute("SELECT value_checkout_identity FROM run_root WHERE run_id=? LIMIT 2", (receipt["run_id"],)).fetchall()
    marker = transferred.workspace / ".git" / "agent-run-checkout-id"
    if (roots != [(receipt["source_uuid"],)] or marker.is_symlink() or not marker.is_file()
            or marker.stat().st_nlink != 1 or marker.stat().st_size != 33
            or marker.read_bytes() != receipt["source_uuid"].encode("ascii") + b"\n"):
        raise _refuse()
    origin = Path(receipt["target_db"])
    reference = {"origin_db": str(origin), "nonce": receipt["nonce"]}
    if origin_reference(origin, transferred, receipt["run_id"]) != reference:
        raise _refuse()
    with Storage(origin).transaction(write=False) as tx:
        proof = tx.one("SELECT subject,identity,material_id FROM history WHERE kind='v6_original_cutover'")
        imports = tx.rows("SELECT h.subject,h.kind,h.identity,h.material_id,h.generation,m.purpose FROM history h "
                          "JOIN material m ON m.id=h.material_id WHERE h.kind IN ('v6_completed_import','v6_returned_import')", limit=2)
        if len(imports) != 1 or tx.rows("SELECT identity FROM history WHERE kind='v6_original_rollback_started' LIMIT 1"):
            raise _refuse()
        imported = imports[0]
        records = tx.one("SELECT h.subject,h.identity,h.material_id,m.purpose FROM history h JOIN material m ON m.id=h.material_id WHERE h.kind='v6_record_import'")
        imported_body = json.loads(tx.get(imported["material_id"], imported["subject"]))
        record_body = json.loads(tx.get(records["material_id"], records["subject"]))
        if (tx.get(proof["material_id"], proof["subject"]) != wire_json(receipt)
                or imported_body.get("source_sha256") != receipt["source_sha256"]
                or imported_body.get("resource_mode") != "existing-verified"
                or records["identity"] != receipt["run_id"] or records["purpose"] != "v6_record_manifest"
                or record_body.get("source", {}).get("sha256") != receipt["source_sha256"]
                or record_body.get("source", {}).get("run_id") != receipt["run_id"]):
            raise _refuse()
        if imported["kind"] == "v6_completed_import":
            if (imported["identity"] != receipt["run_id"] or imported["purpose"] != "v6_completed_import_identity"
                    or imported_body.get("copied_repository_root") != str(transferred.workspace)):
                raise _refuse()
        else:
            if (proof["subject"] != imported["subject"] or records["subject"] != imported["subject"]
                    or imported_body.get("profile_sha256") != record_body.get("documents", {}).get("profile", {}).get("sha256")):
                raise _refuse()
            _returned_transfer_identity(tx, imported, imported_body, receipt, managed, db)
    if transferred != task:
        return None  # The sole retired old Task cannot authorize or block another Parent.
    current = read_task(task_path(task))
    if current is None or origin_reference(current[1], task, current[0]) != reference:
        raise _refuse()
    return reference


def _sources(task: TaskKey) -> tuple[_Source, ...]:
    sources: set[_Source] = set()
    try:
        managed = ManagedWorkspace.for_repository(task.repository)
    except ManagedWorkspaceError:
        pass  # Non-managed library Tasks can still have an exact legacy locator.
    else:
        if managed.repository_root.resolve() == task.workspace and managed.state_root.exists():
            sources.add(_Source(managed.state_root))
    locator = app_data_root() / "run-locator.json"
    if not locator.exists() and not locator.is_symlink():
        return tuple(sorted(sources))
    if locator.is_symlink() or locator.stat().st_size > 1024 * 1024:
        raise _refuse()
    descriptor = os.open(locator.parent / ".run-locator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        value = json.loads(locator.read_bytes())
    if not isinstance(value, dict) or set(value) != {"entries"} or not isinstance(value["entries"], list):
        raise _refuse()
    if len(value["entries"]) > 32:
        raise _refuse()
    for entry in value["entries"]:
        if not isinstance(entry, dict):
            raise _refuse()
        repository = entry.get("repository")
        parent = entry.get("parent_number")
        canonical_repository = None
        if isinstance(repository, str):
            try:
                canonical_repository = normalize_repository(repository)
            except ManagedWorkspaceError:
                pass
        routed = canonical_repository is not None and type(parent) is int and parent > 0
        if routed and (canonical_repository != task.repository or parent != task.parent_number):
            # Only a complete, valid route proves another Parent is unrelated.
            continue
        root_value = entry.get("repository_root")
        if not isinstance(root_value, str) or not Path(root_value).is_absolute():
            raise _refuse()
        if Path(root_value).resolve() != task.workspace:
            continue
        if not routed and ("repository" in entry or "parent_number" in entry):
            raise _refuse()
        state, run_id = entry.get("state_dir"), entry.get("run_id")
        if not isinstance(state, str) or not Path(state).is_absolute() or not isinstance(run_id, str) or not run_id:
            raise _refuse()
        sources.add(_Source(Path(state), run_id, routed))
    return tuple(sorted(sources))


@contextmanager
def legacy_task_lock(task: TaskKey, state_root: Path) -> Iterator[None]:
    """Use the unchanged v6 TaskControl lock; callers then take native Task/storage locks.

    This coordination lock proves no Host exit and changes no v6 database byte.
    The mapper must separately establish actual exit and a source SQLite fence.
    """
    if not state_root.is_dir() or state_root.resolve() != state_root:
        raise _refuse()
    directory = state_root / "task-control"
    if directory.is_symlink():
        raise _refuse()
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(directory / ("." + task.fingerprint + ".lock"),
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _check(task: TaskKey, source: _Source) -> dict[str, str] | None:
    sentinel = source.state / "task-control" / (task.fingerprint + ".json")
    if sentinel.exists() or sentinel.is_symlink():
        raise _refuse()
    path = source.state / "state.sqlite3"
    if path.is_symlink():
        raise _refuse()
    if not path.exists():
        if source.run_id:
            raise _refuse()
        return None
    before = source_identity(path)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0, isolation_level=None)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        # A retired *copy* is not authority to admit against the original.
        metadata = db.execute("SELECT key,value FROM schema_meta").fetchmany(2)
        if len(metadata) != 1 or metadata[0][0] != "protocol":
            raise _refuse()
        if metadata[0][1] != SOURCE_PROTOCOL:
            try:
                transfer = _original_transfer(task, source, path, db, metadata[0][1])
            except (KernelError, KeyError, TypeError, AttributeError) as error:
                raise _refuse() from error
            after = path.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise _refuse()
            return transfer
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchmany(513)}
        if not SOURCE_TABLES <= tables or "control_tasks" not in tables:
            raise _refuse()
        control = db.execute("SELECT task_id,workspace,repository,parent_number FROM control_tasks "
                             "WHERE task_id=? OR (workspace=? AND repository=? COLLATE NOCASE AND parent_number=?) LIMIT 1",
                             (task.fingerprint, str(task.workspace), task.repository, task.parent_number)).fetchone()
        if control is not None:
            # Any original record needs explicit migration. Do not reinterpret
            # old exited/failed/unknown/control-target facts as new authority.
            raise _refuse()
        run = db.execute("SELECT run_id FROM runs WHERE repository=? COLLATE NOCASE AND parent_number=? LIMIT 1",
                         (task.repository, task.parent_number)).fetchone()
        if run is not None or source.routed:
            raise _refuse()
        if source.run_id:
            original = db.execute("SELECT repository,parent_number FROM runs WHERE run_id=?", (source.run_id,)).fetchmany(2)
            if len(original) != 1:
                raise _refuse()
        after = path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise _refuse()
        return None
    finally:
        db.close()


@contextmanager
def locked_admission(task: TaskKey) -> Iterator[Path]:
    """Check existing legacy authority while holding both original admission locks."""
    with ExitStack() as held:
        try:
            sources = _sources(task)
            for state in sorted({source.state for source in sources}):
                held.enter_context(legacy_task_lock(task, state))
            location = held.enter_context(locked_task(task))
            if _sources(task) != sources:
                raise _refuse()
            transfers = [_check(task, source) for source in sources]
            current = read_task(location)
            if current is not None:
                origin = origin_reference(current[1], task, current[0])
                if origin is not None and origin not in transfers:
                    raise _refuse()
        except (OSError, ValueError, sqlite3.Error) as error:
            raise _refuse() from error
        yield location
