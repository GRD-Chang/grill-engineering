"""Explicit offline cutover and bounded return of accepted v6 units.

The private-copy return is limited to its recorded local workspace unit.
Original-source cutover separately requires the original settled Task, preserves
its managed clone, retires its protocol, then publishes its native Task pointer.
Neither path claims proof that an arbitrary systemd Host has exited.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import selectors
import shutil
import sqlite3
import subprocess
import tempfile
from time import monotonic
from typing import Any, Callable, cast

from agent_run.task_identity import TaskKey
from agent_run.managed_workspace import ManagedWorkspace
from agent_run.runner_lease import default_runner_lock_path, runner_management_lease

from .cleanup_adapter import _hold_ref
from .effects import LocalGit
from .kernel import Kernel
from .migration import (MigrationRefused, SourceSnapshot, _offline_source, _source_blocks,
                        restore_v6, capture_v6, source_identity, validate_snapshot)
from .legacy_admission import legacy_task_lock, read_source_fence, source_fence
from .migration_active import (_Active, _read_active, _read_profile, _returned_external,
                               _returned_layout, _existing_returned_git)
from .migration_completed import (_Completed, convert_completed_ticket_v6, _existing_completed_git,
                                  _read_completed, _read_document)
from .migration_quiescence import read_v6_host_exit
from .migration_records import V6RecordReader
from .migration_conversion import _git
from .model import HostLaunch, wire_json
from .storage import Transaction
from .task_location import bind_task as bind_task, locked_task, read_task


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
    required = {"nonce", "old_db", "new_db", "run_id", "source_sha256", "task_fingerprint"}
    if (not isinstance(body, dict) or not required <= set(body)
            or set(body) - required - {"workspace"}
            or any(not isinstance(item, str) or not item for item in body.values())
            or _marker(body) != value):
        raise MigrationRefused("copied_v6_fence_changed")
    return cast(dict[str, str], body)


def _legacy_checkout_identity(common: Path) -> str | None:
    marker = common / "agent-run-checkout-id"
    try:
        if marker.is_symlink():
            raise MigrationRefused("workspace_return_clone_identity")
        if not marker.exists():
            return None
        if not marker.is_file() or marker.stat().st_nlink != 1:
            raise MigrationRefused("workspace_return_clone_identity")
        value = marker.read_bytes()
        if (len(value) != 33 or value[-1:] != b"\n"
                or any(char not in b"0123456789abcdef" for char in value[:-1])):
            raise MigrationRefused("workspace_return_clone_identity")
        return value[:-1].decode("ascii")
    except OSError as error:
        raise MigrationRefused("workspace_return_clone_identity") from error


def _workspace_state(tx: Transaction, subject: str) -> dict[str, Any]:
    run = tx.one("SELECT * FROM runs")
    job = tx.one("SELECT * FROM subjects WHERE id=?", (subject,))
    state = {key: job[key] for key in (
        "id", "generation", "version", "phase", "revision", "completion_revision",
        "source_ref", "target_ref", "base_sha", "base_tree", "source_sha", "workspace_sha",
        "checkout", "owned_root", "common_git_dir", "policy_id", "purpose", "repair_source",
        "resume_purpose", "validation_checkout", "validation_cleanup",
    )}
    state.update({key: run[key] for key in (
        "repository_root", "parent_revision", "graph_revision", "accepted_parent_revision",
        "observed_parent_revision", "observed_graph_revision", "source_sequence", "profile_revision",
    )})
    state["policy_sha256"] = hashlib.sha256(run["policy"]).hexdigest()
    state["default_ref"] = tx.one("SELECT target_ref FROM final_target")[0]
    state["legacy_checkout_identity"] = _legacy_checkout_identity(Path(state["common_git_dir"]))
    return state


def _workspace_git(state: dict[str, Any], *, created: bool) -> bool:
    """Check exact original resources; only the new source ref may differ."""
    root, checkout, owned, common = (Path(state[key]) for key in (
        "repository_root", "checkout", "owned_root", "common_git_dir"))
    if (any(not path.is_absolute() or path.resolve() != path for path in (root, checkout, owned, common))
            or not checkout.is_relative_to(owned) or checkout == owned or common != root / ".git"
            or _git(root, "rev-parse", "--show-toplevel") != str(root)
            or _git(checkout, "rev-parse", "--show-toplevel") != str(checkout)
            or _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir") != str(common)
            or _git(checkout, "rev-parse", "HEAD") != state["base_sha"]
            or _git(checkout, "status", "--porcelain")
            or _legacy_checkout_identity(common) != state["legacy_checkout_identity"]
            or _git(root, "rev-parse", state["target_ref"]) != state["base_sha"]
            or _git(root, "rev-parse", state["default_ref"]) != state["base_sha"]):
        raise MigrationRefused("workspace_return_resource_changed")
    for ref in {state["source_ref"], state["target_ref"], state["default_ref"]}:
        if LocalGit(root).run("symbolic-ref", "-q", ref, check=False).returncode != 1:
            raise MigrationRefused("workspace_return_ref_changed")
    blocks = _git(root, "worktree", "list", "--porcelain").split("\n\n")
    own = [block.splitlines() for block in blocks if block.splitlines()[0] == f"worktree {checkout}"]
    if (len(own) != 1 or set(own[0]) != {f"worktree {checkout}", f"HEAD {state['base_sha']}", "detached"}
            or any(f"branch {state['source_ref']}" in block.splitlines() for block in blocks)):
        raise MigrationRefused("workspace_return_resource_changed")
    ref = _git(root, "for-each-ref", "--format=%(refname) %(objectname)", state["source_ref"])
    if ref and (not created or ref != f"{state['source_ref']} {state['base_sha']}"):
        raise MigrationRefused("workspace_return_ref_changed")
    return bool(ref)


def _delete_workspace_ref(state: dict[str, Any]) -> None:
    """Lock the exact refs, prove their direct kind, then commit only deletion."""
    root, source, base = Path(state["repository_root"]), state["source_ref"], state["base_sha"]
    present = _workspace_git(state, created=True)
    command = "start\0"
    for ref in sorted({state["target_ref"], state["default_ref"]}):
        command += f"verify {ref}\0{base}\0"
    command += f"delete {source}\0{base}\0" if present else f"verify {source}\0{'0' * len(base)}\0"
    process = subprocess.Popen(["git", "update-ref", "--no-deref", "--stdin", "-z"], cwd=root,
                               env=LocalGit(root).env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write((command + "prepare\0").encode())
        process.stdin.flush()
        deadline, output = monotonic() + 5.0, bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while b"prepare: ok\n" not in output:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise MigrationRefused("workspace_return_ref_lock")
                block = os.read(process.stdout.fileno(), 4096)
                if not block or len(output) + len(block) > 65536:
                    raise MigrationRefused("workspace_return_ref_lock")
                output.extend(block)
        # no-deref prevents touching another branch; the locked kind check
        # also preserves a foreign symref itself, including a same-OID race.
        if _workspace_git(state, created=True) != present:
            raise MigrationRefused("workspace_return_ref_changed")
        process.stdin.write(b"commit\0")
        process.stdin.flush()
        process.stdin.close()  # End the command stream after committing.
        if process.wait(timeout=5) != 0:
            raise MigrationRefused("workspace_return_ref_commit")
    except (OSError, subprocess.SubprocessError) as error:
        raise MigrationRefused("workspace_return_ref_transaction") from error
    finally:
        try:
            process.stdin.close()  # Uncommitted prepared transactions abort.
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def _workspace_inverse(tx: Transaction, receipt: dict[str, str]) -> dict[str, Any]:
    try:
        original = json.loads(receipt["workspace"])
    except (KeyError, ValueError, UnicodeError) as error:
        raise MigrationRefused("workspace_return_origin_unproved") from error
    if (not isinstance(original, dict) or not isinstance(original.get("id"), str)
            or not isinstance(original.get("legacy_checkout_identity"), str)):
        raise MigrationRefused("workspace_return_origin_unproved")
    state = _workspace_state(tx, original["id"])
    before = dict(state, version=state["version"] - 1, phase="workspace")
    effects = tx.all("SELECT * FROM effects")
    if (before != original or state["phase"] != "development" or len(effects) != 1
            or any(tx.one(f"SELECT count(*) FROM {table}")[0] for table in (
                "attempts", "completions", "checks", "grants", "final_barriers"))
            or tx.rows("SELECT subject FROM budgets WHERE developments_used!=0 OR reviews_used!=0")
            or tx.rows("SELECT id FROM actions WHERE kind NOT IN ('run','stop') OR subject IS NOT NULL")):
        raise MigrationRefused("workspace_return_progress_unsupported")
    effect = effects[0]
    if (effect["subject"] != state["id"] or effect["operation"] != "workspace"
            or effect["status"] != "confirmed" or effect["attempts"] != 1 or not effect["receipt_id"]
            or any(effect[key] != state[key] for key in (
                "generation", "revision", "parent_revision", "graph_revision", "source_ref", "target_ref",
                "base_sha", "base_tree"))
            or effect["before_sha"] != state["base_sha"] or effect["after_sha"] is not None
            or effect["candidate_sha"] is not None or effect["pr_number"] is not None):
        raise MigrationRefused("workspace_return_effect_unsupported")
    intent = json.loads(tx.get(effect["payload_id"], state["id"]))
    result = json.loads(tx.get(effect["receipt_id"], state["id"]))
    if (any(intent.get(key) != state[key] for key in ("checkout", "owned_root", "common_git_dir"))
            or intent.get("create_ref") is not True or intent.get("intent_token") != effect["id"]
            or result.get("disposition") != "target"
            or result.get("commit") != {"sha": state["base_sha"], "tree": state["base_tree"]}):
        raise MigrationRefused("workspace_return_effect_unsupported")
    return state


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
            original_identity = json.loads(receipt["workspace"])["legacy_checkout_identity"]
            if (original_identity is not None and db.execute(
                    "SELECT value_checkout_identity FROM run_root").fetchall() != [(original_identity,)]):
                raise MigrationRefused("workspace_return_clone_identity")
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
    with native.storage.transaction(write=False) as tx:
        workspace = _workspace_state(tx, subject)
    _workspace_git(workspace, created=False)
    receipt["workspace"] = wire_json(workspace).decode()
    if len(_marker(receipt)) > 8192:
        raise MigrationRefused("copied_v6_fence_size")
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
    return _restore_copied_v6(snapshot, old_copy, native, nonce=nonce, workspace=False)


def _restore_copied_v6(
    snapshot: SourceSnapshot, old_copy: Path, native: Kernel, *, nonce: str, workspace: bool,
) -> Path:
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
                            "invocations", "invocation_outputs", "leases", "resource_cleanup"))
                        or tx.rows("SELECT owner FROM hosts WHERE status!='exited' OR usage_released=0")
                        or tx.rows("SELECT owner FROM retired_executors WHERE status!='exited'")):
                    raise MigrationRefused("native_effect_or_host_unsettled")
                inverse = _workspace_inverse(tx, receipt) if workspace else None
                if not workspace and tx.one("SELECT count(*) FROM effects")[0]:
                    raise MigrationRefused("native_effect_or_host_unsettled")
                history = tx.rows("SELECT subject,material_id FROM history WHERE kind='v6_private_cutover' AND identity=?",
                                  (nonce,))
                if len(history) != 1 or json.loads(tx.get(history[0]["material_id"], history[0]["subject"])) != receipt:
                    raise MigrationRefused("copied_v6_fence_identity")
                binding = read_task(locator)
                if (binding != (receipt["run_id"], new)
                        and not (workspace and binding == (receipt["run_id"], old))):
                    raise MigrationRefused("native_task_location_changed")
                with _offline_source(old) as descriptor:
                    if _protocol_from_locked_copy(descriptor, old.parent) != _marker(receipt):
                        raise MigrationRefused("copied_v6_fence_changed")
                    if os.fstat(descriptor).st_nlink != 1:
                        raise MigrationRefused("copied_v6_changed")
                    if inverse is not None:
                        # The origin proves absence, and the sole confirmed
                        # native operation created exactly this local ref.
                        # CAS never deletes an advanced or borrowed branch.
                        _delete_workspace_ref(inverse)
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


def restore_copied_v6_after_workspace(
    snapshot: SourceSnapshot, old_copy: Path, native: Kernel, *, nonce: str,
) -> Path:
    """Undo only one witnessed local ref creation after exact native quiescence.

    Original refs and the pre-existing clean detached checkout must still
    match the cutover witness. Any Agent, other effect or unknown result keeps
    the native authority and retired v6 copy intact for explicit rollforward.
    The native ledger is retained; this does not invert arbitrary new work.
    """
    return _restore_copied_v6(snapshot, old_copy, native, nonce=nonce, workspace=True)


def _original_protocol(source: Path) -> str:
    source_identity(source)  # Before another open can release a caller's inode locks.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
        rows = db.execute('SELECT key,value FROM schema_meta').fetchmany(2)
    if len(rows) != 1 or rows[0][0] != 'protocol' or not isinstance(rows[0][1], str):
        raise MigrationRefused('source_protocol')
    return str(rows[0][1])


def _read_original_facts(path: Path, profile: dict[str, Any], profile_sha: str,
                         source_sha: str, readproof: dict[str, Any] | None) -> _Completed | _Active:
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        active = db.execute('SELECT status FROM runs').fetchone()[0] == 'active'
    if active:
        facts = _read_active(path, profile, profile_sha, source_sha, original=True)
        if readproof is not None:
            _returned_external(facts, readproof, source_sha)
        return facts
    return _read_completed(path, profile, profile_sha, source_sha, readproof)


def _original_refs(facts: _Completed) -> dict[str, str]:
    return {'refs/heads/' + facts.run_branch: facts.integrated_sha,
            'refs/heads/' + facts.base_branch: facts.base_sha,
            'refs/heads/' + facts.ticket_branch: facts.publication_sha if facts.pending_resources else '0' * len(facts.base_sha)}


def _original_prepared(native: Kernel, receipt: dict[str, str], task: TaskKey,
                       facts: _Completed, readproof: dict[str, Any] | None = None) -> None:
    """Require this unpublished, unexecuted candidate and all three source proofs."""
    with native.storage.transaction(write=False) as tx:
        run = tx.one('SELECT id,repository,parent_issue,repository_root,executor,stopped FROM runs')
        if (run['id'] != receipt['run_id'] or run['executor'] is not None or run['stopped']
                or run['repository_root'] != str(task.workspace)
                or run['repository'] != task.repository or run['parent_issue'] != task.parent_number
                or native.storage.path.resolve() != Path(receipt['target_db'])
                or any(tx.one(f'SELECT count(*) FROM {name}')[0] for name in (
                    'actions', 'hosts', 'invocations', 'leases', 'retired_executors', 'worker_activity'))):
            raise MigrationRefused('native_original_cutover_advanced')
        origin = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_original_cutover' AND identity=?",
                        (receipt['nonce'],))
        imported = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_completed_import'")
        body = json.loads(tx.get(imported['material_id'], imported['subject']))
        if (tx.get(origin['material_id'], origin['subject']) != wire_json(receipt)
                or body['source_sha256'] != receipt['source_sha256']
                or body.get('resource_mode') != 'existing-verified'):
            raise MigrationRefused('native_original_cutover_provenance')
        ticket = body['old_job']['value_ticket_number']
        expected = sorted((f"v6-import:{receipt['run_id']}:{ticket}:{operation}", operation, 'confirmed')
                          for operation in ('completion_comment', 'close'))
        if [tuple(row) for row in tx.rows('SELECT id,operation,status FROM effects ORDER BY id')] != expected:
            raise MigrationRefused('native_original_cutover_advanced')
        subjects = tx.rows('SELECT id,kind,issue,generation,phase,current_attempt FROM subjects')
        completions = tx.rows('SELECT subject,generation,source_sha,merge_sha,tree,pr_number FROM completions')
        if (len(subjects) != 1 or tuple(subjects[0]) != (
                imported['subject'], 'ticket', ticket, 1, 'complete', None)
                or [tuple(row) for row in completions] != [(
                    imported['subject'], 1, facts.publication_sha, facts.integrated_sha,
                    facts.integrated_tree, facts.pr_number)]
                or any(tx.one(f'SELECT count(*) FROM {name}')[0] for name in (
                    'final_barriers', 'retired_generations'))):
            raise MigrationRefused('native_original_cutover_advanced')
        checkout = Path(receipt['source_db']).parent / 'worktrees' / receipt['run_id'] / f'ticket-{ticket}'
        cleanup = [tuple(row) for row in tx.rows('SELECT subject,generation,source_ref,source_sha,checkout,workspace_sha,owned_root,common_git_dir,stage,state,effect_id,outcome FROM resource_cleanup')]
        expected_cleanup = [(imported['subject'], 1, 'refs/heads/' + facts.ticket_branch, facts.publication_sha,
                             str(checkout), facts.publication_sha, str(checkout.parent),
                             str(task.workspace / '.git'), 0, 'pending', None, None)] if facts.pending_resources else []
        if cleanup != expected_cleanup:
            raise MigrationRefused('native_original_cutover_advanced')
        ownership = tx.one("SELECT h.subject,h.identity,h.material_id,m.purpose FROM history h "
                           "JOIN material m ON m.id=h.material_id WHERE h.kind='v6_local_ref_ownership' AND h.identity=?",
                           ('refs/heads/' + facts.run_branch,))
        if (ownership['subject'] != imported['subject']
                or ownership['purpose'] != 'v6_local_ref_ownership'
                or ownership['identity'] != 'refs/heads/' + facts.run_branch
                or json.loads(tx.get(ownership['material_id'], ownership['subject'])) != {
                    'origin_nonce': receipt['nonce'], 'source_ref': ownership['identity'],
                    'expected_oid': facts.integrated_sha,
                    'source_record': {'table': 'run_root', 'key': [receipt['run_id']]},
                }):
            raise MigrationRefused('native_original_cutover_provenance')
        local = tx.rows("SELECT h.subject,h.kind,h.identity,h.material_id,m.purpose FROM history h JOIN material m ON m.id=h.material_id "
                        "WHERE h.kind IN ('v6_local_ref_ownership','v6_local_worktree_ownership') AND h.identity!=?",
                        (ownership['identity'],))
        expected_local = {('v6_local_ref_ownership', 'refs/heads/' + facts.ticket_branch),
                          ('v6_local_worktree_ownership', str(checkout))} if facts.pending_resources else set()
        if len(local) != len(expected_local) or {(row['kind'], row['identity']) for row in local} != expected_local:
            raise MigrationRefused('native_original_cutover_provenance')
        for fact in local:
            field = 'source_ref' if fact['kind'] == 'v6_local_ref_ownership' else 'checkout'
            resource = 'refs/heads/' + facts.ticket_branch if field == 'source_ref' else str(checkout)
            if (fact['subject'] != imported['subject'] or fact['identity'] != resource
                    or fact['purpose'] != fact['kind']
                    or json.loads(tx.get(fact['material_id'], fact['subject'])) != {
                        'origin_nonce': receipt['nonce'], field: resource, 'expected_oid': facts.publication_sha,
                        'source_record': {'table': 'work_jobs', 'key': [facts.run_id, facts.old_job['job_id']]}}):
                raise MigrationRefused('native_original_cutover_provenance')
    reader = V6RecordReader(native)
    if (reader.source['sha256'] != receipt['source_sha256']
            or reader.source['run_id'] != receipt['run_id']
            or reader.row('run_root', [receipt['run_id']])['value_checkout_identity'] != receipt['source_uuid']):
        raise MigrationRefused('native_original_cutover_provenance')


def _same_original_rows(db: sqlite3.Connection, sealed: Path, protocol: str) -> None:
    """After a crash, protocol is the only accepted logical source change."""
    with closing(sqlite3.connect(sealed.as_uri() + '?mode=ro&immutable=1', uri=True)) as peer:
        definitions = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        if [tuple(row) for row in db.execute(definitions)] != peer.execute(definitions).fetchall():
            raise MigrationRefused('source_fenced_data_changed')
        for (table,) in peer.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            quote = '"' + table.replace('"', '""') + '"'
            primary = [row[1] for row in sorted(peer.execute(f'PRAGMA table_info({quote})'), key=lambda row: row[5]) if row[5]]
            order = ','.join('"' + name.replace('"', '""') + '"' for name in primary)
            sql = f'SELECT * FROM {quote}' + (f' ORDER BY {order}' if order else '')
            expected, actual = peer.execute(sql), db.execute(sql)
            while True:
                before, after = expected.fetchmany(128), actual.fetchmany(128)
                if table == 'schema_meta':
                    before = [(key, protocol if key == 'protocol' else value) for key, value in before]
                if before != [tuple(row) for row in after]:
                    raise MigrationRefused('source_fenced_data_changed')
                if not before:
                    break


def _retire_original(source: Path, sealed: Path, receipt: dict[str, str], *, retired: bool,
                     release: bool = False) -> None:
    """Only protocol changes, under the source inode lock and one SQLite transaction."""
    protocol = source_fence(receipt)
    with _offline_source(source) as descriptor:
        # Keep this descriptor through the SQLite transaction. Never open and
        # close another same-inode hash/bridge connection inside this boundary.
        if not retired and not release:
            digest = hashlib.sha256()
            for block in _source_blocks(descriptor):
                digest.update(block)
            if digest.hexdigest() != receipt['source_sha256']:
                raise MigrationRefused('source_changed')
        with closing(sqlite3.connect(source.as_uri() + '?mode=rw', uri=True,
                                     isolation_level=None, timeout=0)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            try:
                db.execute('BEGIN EXCLUSIVE')
                current = db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()
                if current is None or current[0] != (protocol if retired else _OLD_PROTOCOL):
                    raise MigrationRefused('source_original_fence_changed')
                read_v6_host_exit(db, receipt['run_id'], required=True)
                _same_original_rows(db, sealed, protocol if retired else _OLD_PROTOCOL)
                target = _OLD_PROTOCOL if release else protocol
                if current[0] != target:
                    changed = db.execute("UPDATE schema_meta SET value=? WHERE key='protocol' AND value=?",
                                         (target, current[0]))
                    if changed.rowcount != 1:
                        raise MigrationRefused('source_original_fence_changed')
                db.commit()
            except BaseException:
                db.rollback()
                raise
    directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _original_header(snapshot: SourceSnapshot, task: TaskKey) -> tuple[str, bool]:
    validate_snapshot(snapshot)
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        rows = db.execute('SELECT run_id,repository,parent_number,status FROM runs').fetchmany(2)
    if len(rows) != 1 or rows[0][1:3] != (task.repository, task.parent_number):
        raise MigrationRefused('source_original_identity')
    return rows[0][0], rows[0][3] == 'active'


def migrate_completed_original_v6(
    snapshot: SourceSnapshot, *, repository: str, parent_number: int, nonce: str,
    profile_snapshot: Path, github_snapshot: Path | None = None,
    observation_snapshot: Path | None = None, original_report: Path | None = None,
    external_snapshot: Path | None = None,
) -> Kernel:
    """Transfer one authenticated original Task unit to its managed clone's native DB.

    All source evidence remains preserved. A crash after source retirement is
    recovered only with the original receipt/nonce and still-unexecuted target;
    this never restores an old snapshot over new native work.
    """
    managed = ManagedWorkspace.for_repository(repository)
    source = managed.state_root / 'state.sqlite3'
    task = TaskKey(managed.repository_root, managed.repository, parent_number)
    if snapshot.source != source or snapshot.path.is_symlink() or snapshot.path.samefile(source):
        raise MigrationRefused('source_original_identity')
    run_id, returned = _original_header(snapshot, task)
    if returned:
        if external_snapshot is None:
            raise MigrationRefused('source_external_readback_required')
        from .migration_active import read_original_returned_import
        from .migration_run_cutover import _execute_original_import
        canonical = read_original_returned_import(snapshot,
            profile_snapshot=profile_snapshot,
            external_snapshot=external_snapshot, checkout_archive=external_snapshot.parent / 'checkout.tar')
        return cast(Kernel, _execute_original_import(canonical, nonce=nonce,
            evidence=external_snapshot.parent, inverse=False))
    with runner_management_lease(default_runner_lock_path()), legacy_task_lock(task, managed.state_root), locked_task(task) as location:
        managed.open_path()
        sentinel = managed.state_root / 'task-control' / (task.fingerprint + '.json')
        if sentinel.exists() or sentinel.is_symlink():
            raise MigrationRefused('source_legacy_control_json')
        with tempfile.TemporaryDirectory(dir=managed.state_root, prefix='.original-cutover-') as directory:
            checked = capture_v6(snapshot.path, Path(directory) / 'sealed.sqlite3')
            if ((checked.sha256, checked.schema_sha256, checked.table_rows)
                    != (snapshot.sha256, snapshot.schema_sha256, snapshot.table_rows)):
                raise MigrationRefused('snapshot_changed')
            accepted = SourceSnapshot(source, checked.path, checked.sha256, checked.source_protocol,
                                      checked.schema_sha256, checked.table_rows)
            with closing(sqlite3.connect(checked.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                if (db.execute('SELECT count(*) FROM runs').fetchone()[0] != 1
                        or db.execute('SELECT count(*) FROM control_tasks').fetchone()[0] != 1):
                    raise MigrationRefused('source_task_scope')
                run_id, uuid = db.execute('SELECT run_id,value_checkout_identity FROM run_root').fetchone()
            profile, profile_sha = _read_profile(profile_snapshot)
            readproof = None if external_snapshot is None else _read_document(external_snapshot)[0]
            facts = _read_completed(checked.path, profile, profile_sha, snapshot.sha256, readproof)
            if (facts.run_id != run_id or facts.repository != task.repository
                    or facts.parent_number != task.parent_number):
                raise MigrationRefused('source_original_identity')
            destination = managed.state_root / run_id / 'kernel.sqlite3'
            final = managed.root / 'worktrees' / run_id / 'run-publication'
            receipt = read_source_fence(source_fence({
                'nonce': nonce, 'source_db': str(source), 'source_sha256': snapshot.sha256,
                'run_id': run_id, 'task_fingerprint': task.fingerprint,
                'source_uuid': uuid, 'target_db': str(destination)}))
            protocol = _original_protocol(source)
            retired = protocol != _OLD_PROTOCOL
            if retired and read_source_fence(protocol) != receipt:
                raise MigrationRefused('source_original_fence_changed')
            prior = read_task(location)
            if prior is not None and (not retired or prior != (run_id, destination)):
                raise MigrationRefused('native_task_already_bound')
            if destination.exists():
                if destination.is_symlink():
                    raise MigrationRefused('native_original_cutover_provenance')
                native = Kernel(destination)
            else:
                if retired:
                    raise MigrationRefused('native_original_cutover_missing')
                destination.parent.mkdir(mode=0o700, exist_ok=True)
                native = convert_completed_ticket_v6(
                    accepted, destination, git_store=managed.repository_root / '.git',
                    repository_root=managed.repository_root, final_checkout=final,
                    profile_snapshot=profile_snapshot, github_snapshot=github_snapshot,
                    observation_snapshot=observation_snapshot, original_report=original_report,
                    existing_verified=True, source_origin=receipt, external_snapshot=external_snapshot)
            _original_prepared(native, receipt, task, facts, readproof)
            reader = LocalGit(managed.repository_root)
            with ExitStack() as refs:
                for ref, sha in sorted(_original_refs(facts).items()):
                    refs.enter_context(_hold_ref(reader, managed.repository_root, ref, sha))
                _existing_completed_git(accepted, checked.path, facts, destination,
                                        managed.repository_root, final, managed.repository_root / '.git')
                _retire_original(source, checked.path, receipt, retired=retired)
                if read_task(location) != prior:
                    raise MigrationRefused('native_task_binding_changed')
                bind_task(location, run_id, destination)
    return Kernel(destination)


def _withdraw_original_pointer(location: Path, run_id: str, destination: Path) -> None:
    current = read_task(location)
    if current is not None:
        if current != (run_id, destination):
            raise MigrationRefused('native_task_binding_changed')
        location.unlink()
        descriptor = os.open(location.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def rollback_original_v6(snapshot: SourceSnapshot, *, repository: str,
                         parent_number: int, nonce: str,
                         external_snapshot: Path | None = None) -> dict[str, str]:
    """Return exactly an untouched original transfer; retain every native byte.

    No new native execution or external write is inverted. The original rows
    are compared with the preserved source, then only protocol is restored.
    Missing/advanced/foreign evidence refuses with both databases retained.
    """
    managed = ManagedWorkspace.for_repository(repository)
    source = managed.state_root / 'state.sqlite3'
    task = TaskKey(managed.repository_root, managed.repository, parent_number)
    if snapshot.source != source or snapshot.path.is_symlink() or snapshot.path.samefile(source):
        raise MigrationRefused('source_original_identity')
    run_id, returned = _original_header(snapshot, task)
    if returned:
        if external_snapshot is None:
            raise MigrationRefused('source_external_readback_required')
        from .migration_active import read_original_returned_import
        from .migration_run_cutover import _execute_original_import
        canonical = read_original_returned_import(snapshot,
            profile_snapshot=managed.state_root / 'profiles' / (run_id + '.json'),
            external_snapshot=external_snapshot, checkout_archive=external_snapshot.parent / 'checkout.tar')
        return cast(dict[str, str], _execute_original_import(canonical, nonce=nonce,
            evidence=external_snapshot.parent, inverse=True))
    with runner_management_lease(default_runner_lock_path()), legacy_task_lock(task, managed.state_root), locked_task(task) as location:
        managed.open_path()
        sentinel = managed.state_root / 'task-control' / (task.fingerprint + '.json')
        if sentinel.exists() or sentinel.is_symlink():
            raise MigrationRefused('source_legacy_control_json')
        with tempfile.TemporaryDirectory(dir=managed.state_root, prefix='.original-rollback-') as directory:
            checked = capture_v6(snapshot.path, Path(directory) / 'sealed.sqlite3')
            if ((checked.sha256, checked.schema_sha256, checked.table_rows)
                    != (snapshot.sha256, snapshot.schema_sha256, snapshot.table_rows)):
                raise MigrationRefused('snapshot_changed')
            with closing(sqlite3.connect(checked.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                run_id, uuid = db.execute('SELECT run_id,value_checkout_identity FROM run_root').fetchone()
            profile, profile_sha = _read_profile(managed.state_root / 'profiles' / (run_id + '.json'))
            readproof = None if external_snapshot is None else _read_document(external_snapshot)[0]
            facts = _read_completed(checked.path, profile, profile_sha, snapshot.sha256, readproof)
            if facts.repository != task.repository or facts.parent_number != task.parent_number:
                raise MigrationRefused('source_original_identity')
            destination = managed.state_root / run_id / 'kernel.sqlite3'
            if destination.is_symlink() or not destination.is_file():
                raise MigrationRefused('native_original_cutover_missing')
            native = Kernel(destination)
            receipt = read_source_fence(source_fence({
                'nonce': nonce, 'source_db': str(source), 'source_sha256': snapshot.sha256,
                'run_id': run_id, 'task_fingerprint': task.fingerprint,
                'source_uuid': uuid, 'target_db': str(destination)}))
            _original_prepared(native, receipt, task, facts, readproof)
            with native.storage.transaction(write=False) as tx:
                imported = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_completed_import'")
                body = json.loads(tx.get(imported['material_id'], imported['subject']))
                if body['profile_sha256'] != profile_sha:
                    raise MigrationRefused('source_profile_changed')
                started = tx.rows("SELECT subject,material_id FROM history WHERE kind='v6_original_rollback_started'")
                if started and (len(started) != 1 or started[0]['subject'] != imported['subject']
                                or tx.get(started[0]['material_id'], started[0]['subject']) != wire_json(receipt)):
                    raise MigrationRefused('native_original_cutover_provenance')
            prior = read_task(location)
            protocol = _original_protocol(source)
            if (protocol != source_fence(receipt) and not (started and protocol == _OLD_PROTOCOL)
                    or prior not in ((run_id, destination), None)
                    or prior is None and not started):
                raise MigrationRefused('source_original_fence_changed')
            reader = LocalGit(managed.repository_root)
            with ExitStack() as refs:
                for ref, sha in sorted(_original_refs(facts).items()):
                    refs.enter_context(_hold_ref(reader, managed.repository_root, ref, sha))
                accepted = SourceSnapshot(source, checked.path, checked.sha256, checked.source_protocol,
                                          checked.schema_sha256, checked.table_rows)
                _existing_completed_git(accepted, checked.path, facts, destination,
                                        managed.repository_root,
                                        managed.root / 'worktrees' / run_id / 'run-publication',
                                        managed.repository_root / '.git')
                # Prove source quiescence/rows before withdrawing its successor.
                # The same locks remain held through both durable boundaries.
                _retire_original(source, checked.path, receipt, retired=protocol != _OLD_PROTOCOL,
                                 release=protocol == _OLD_PROTOCOL)
                if not started:
                    with native.storage.transaction() as tx:
                        material = tx.put(imported['subject'], 'v6_original_rollback', wire_json(receipt))
                        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) '
                                   'VALUES (?,?,?,?,1,?)', (imported['subject'], 'v6_original_rollback_started',
                                                           nonce, material, native.clock()))
                _withdraw_original_pointer(location, run_id, destination)
                _retire_original(source, checked.path, receipt, retired=protocol != _OLD_PROTOCOL,
                                 release=True)
                with native.storage.transaction() as tx:
                    completed = tx.rows("SELECT material_id FROM history WHERE kind='v6_original_rollback_complete'")
                    if not completed:
                        material = tx.put(imported['subject'], 'v6_original_rollback', wire_json(receipt))
                        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) '
                                   'VALUES (?,?,?,?,1,?)', (imported['subject'], 'v6_original_rollback_complete',
                                                           nonce, material, native.clock()))
    return {'status': 'rolled_back', 'nonce': nonce, 'source_database': str(source),
            'retained_native_database': str(destination)}


if __name__ == '__main__':
    from .migration_cli import main

    raise SystemExit(main())
