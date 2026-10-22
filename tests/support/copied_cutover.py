"""Private copied-v6 cutover stories; never an installed migration interface.

These helpers preserve the real Host, marker, ref and old-02 return checks for
isolated copied-source tests. Original Task transfer stays in the product.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import selectors
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from time import monotonic
from typing import Any, Callable, Iterator, cast

from agent_run.task_identity import TaskKey
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.migration import (
    MigrationRefused, SourceSnapshot, SOURCE_PROTOCOL as _OLD_PROTOCOL,
    _offline_source, _source_blocks, restore_v6, validate_snapshot,
)
from agent_run_kernel.migration_conversion import _git
from agent_run_kernel.model import HostLaunch, Profile, wire_json
from agent_run_kernel.storage import Transaction
from agent_run_kernel.task_location import bind_task as bind_task, locked_task, read_task


_RETIRED_PREFIX = "retired-to-native-copy/1:"
_COPY_IDENTITY_KEYS = frozenset({'legacy_checkout_identity', 'copy_checkout_identity',
    'marker_device', 'marker_inode', 'copy_proof_sha256'})


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
    return state


def _copied_workspace_origin(snapshot: SourceSnapshot, native: Kernel, tx: Transaction,
        subject: str, state: dict[str, Any]) -> dict[str, Any]:
    """Derive the copy commitment again from the sealed decoder, not native claims."""
    from agent_run_kernel.migration_conversion import read_initial_import
    from agent_run_kernel.migration_run_proof import copied_resource_proof
    from agent_run_kernel.migration_run_projection import verify_imported_installation
    profiles = native._profile_document(tx, tx.one('SELECT profile_revision FROM runs')[0])['profiles']
    selected = {role: Profile(profiles[role]['model'], profiles[role]['reasoning_effort'])
                for role in ('development', 'review', 'publication')}
    facts = read_initial_import(snapshot, repository_root=Path(state['repository_root']),
        source_ref=state['source_ref'], checkout=Path(state['checkout']),
        final_checkout=Path(tx.one('SELECT checkout FROM final_target')[0]),
        development_profile=selected['development'], review_profile=selected['review'],
        publication_profile=selected['publication'])
    verify_imported_installation(native, facts)
    # Profiles are explicit frozen import inputs; they do not enter this UUID.
    proof = copied_resource_proof(facts)
    if (proof is None or proof['format'] != 'copied-idle-resources/1'
            or facts.spec.run_id != tx.one('SELECT id FROM runs')[0]
            or len(facts.subjects) != 1 or facts.subjects[0].subject != subject
            or facts.source.checkout_uuid is None or facts.source.checkout_uuid == proof['copy_uuid']):
        raise MigrationRefused('workspace_return_origin_unproved')
    physical = (Path(state['common_git_dir']) / 'agent-run-checkout-id').lstat()
    result = dict(state, legacy_checkout_identity=facts.source.checkout_uuid,
        copy_checkout_identity=proof['copy_uuid'], marker_device=physical.st_dev, marker_inode=physical.st_ino,
        copy_proof_sha256=hashlib.sha256(wire_json(proof)).hexdigest())
    _verify_copied_origin(snapshot, tx, result)
    with _copy_marker(result, source_allowed=False):
        pass
    return result


def _verify_copied_origin(snapshot: SourceSnapshot, tx: Transaction, state: dict[str, Any]) -> None:
    validate_snapshot(snapshot)
    if (not _COPY_IDENTITY_KEYS <= state.keys()
            or any(not isinstance(state[k], str) or len(state[k]) != 32
                   or any(c not in '0123456789abcdef' for c in state[k])
                   for k in ('legacy_checkout_identity', 'copy_checkout_identity'))
            or state['legacy_checkout_identity'] == state['copy_checkout_identity']
            or any(type(state[k]) is not int for k in ('marker_device', 'marker_inode'))):
        raise MigrationRefused('workspace_return_origin_unproved')
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        roots = db.execute('SELECT run_id,value_checkout_identity FROM run_root').fetchall()
    run_id = tx.one('SELECT id FROM runs')[0]
    rows = tx.rows("SELECT id,subject FROM material WHERE purpose='v6_import_identity'", limit=2)
    if len(rows) != 1 or roots != [(run_id, state['legacy_checkout_identity'])]:
        raise MigrationRefused('workspace_return_origin_unproved')
    imported = json.loads(tx.get(rows[0]['id'], rows[0]['subject']))
    proof = imported.get('local_resources')
    if (imported.get('source_sha256') != snapshot.sha256 or imported.get('source_mode') != 'copied'
            or not isinstance(proof, dict) or proof.get('source_sha256') != snapshot.sha256
            or proof.get('run_id') != run_id or proof.get('copy_uuid') != state['copy_checkout_identity']
            or hashlib.sha256(wire_json(proof)).hexdigest() != state['copy_proof_sha256']):
        raise MigrationRefused('workspace_return_origin_unproved')


def _check_copy_marker(descriptor: int, state: dict[str, Any], *, source_allowed: bool) -> bytes:
    marker = Path(state['common_git_dir']) / 'agent-run-checkout-id'
    opened, current = os.fstat(descriptor), marker.lstat()
    identity = state['marker_device'], state['marker_inode']
    if (any(not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 33
            or (info.st_dev, info.st_ino) != identity for info in (opened, current))):
        raise MigrationRefused('workspace_return_clone_identity')
    value = os.pread(descriptor, 34, 0)
    allowed = {(state['copy_checkout_identity'] + '\n').encode('ascii')}
    if source_allowed:
        allowed.add((state['legacy_checkout_identity'] + '\n').encode('ascii'))
    if value not in allowed:
        raise MigrationRefused('workspace_return_clone_identity')
    return value


@contextmanager
def _copy_marker(state: dict[str, Any], *, source_allowed: bool) -> Iterator[int]:
    try:
        descriptor = os.open(Path(state['common_git_dir']) / 'agent-run-checkout-id',
            os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            _check_copy_marker(descriptor, state, source_allowed=source_allowed)
            yield descriptor
        finally:
            os.close(descriptor)
    except OSError as error:
        raise MigrationRefused('workspace_return_clone_identity') from error


def _handoff_copy_marker(descriptor: int, state: dict[str, Any]) -> None:
    """Fixed-size write to the authenticated object, never replace a pathname."""
    _check_copy_marker(descriptor, state, source_allowed=True)
    target = (state['legacy_checkout_identity'] + '\n').encode('ascii')
    if os.pwrite(descriptor, target, 0) != len(target):
        raise MigrationRefused('workspace_return_clone_identity')
    os.fsync(descriptor)
    if _check_copy_marker(descriptor, state, source_allowed=True) != target:
        raise MigrationRefused('workspace_return_clone_identity')


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
            or _legacy_checkout_identity(common) != state.get('_physical_checkout_identity', state['copy_checkout_identity'])
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
    if (before != {k: v for k, v in original.items() if k not in _COPY_IDENTITY_KEYS}
            or state["phase"] != "development" or len(effects) != 1
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
    return {**state, **{k: original[k] for k in _COPY_IDENTITY_KEYS}}


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
        workspace = _copied_workspace_origin(snapshot, native, tx, subject, _workspace_state(tx, subject))
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
                history = tx.rows("SELECT subject,material_id FROM history WHERE kind='v6_private_cutover' AND identity=?",
                                  (nonce,))
                if len(history) != 1 or json.loads(tx.get(history[0]["material_id"], history[0]["subject"])) != receipt:
                    raise MigrationRefused("copied_v6_fence_identity")
                binding = read_task(locator)
                if (binding != (receipt["run_id"], new)
                        and binding != (receipt["run_id"], old)):
                    raise MigrationRefused("native_task_location_changed")
                original = json.loads(receipt['workspace'])
                if not isinstance(original, dict) or not isinstance(original.get('id'), str):
                    raise MigrationRefused('workspace_return_origin_unproved')
                _verify_copied_origin(snapshot, tx, original)
                inverse = _workspace_inverse(tx, receipt) if workspace else None
                if not workspace and (tx.one("SELECT count(*) FROM effects")[0]
                        or _workspace_state(tx, original['id']) != {
                            k: v for k, v in original.items() if k not in _COPY_IDENTITY_KEYS}
                        or any(tx.one(f'SELECT count(*) FROM {name}')[0] for name in (
                            'attempts', 'completions', 'checks', 'grants', 'final_barriers'))
                        or tx.rows("SELECT subject FROM budgets WHERE developments_used!=0 OR reviews_used!=0")
                        or tx.rows("SELECT id FROM actions WHERE kind NOT IN ('run','stop') OR subject IS NOT NULL")):
                    raise MigrationRefused('native_effect_or_host_unsettled')
                with _offline_source(old) as descriptor:
                    if _protocol_from_locked_copy(descriptor, old.parent) != _marker(receipt):
                        raise MigrationRefused("copied_v6_fence_changed")
                    if os.fstat(descriptor).st_nlink != 1:
                        raise MigrationRefused("copied_v6_changed")
                    with _copy_marker(original, source_allowed=binding == (receipt['run_id'], old)) as marker_fd:
                        current = os.pread(marker_fd, 34, 0).decode('ascii').rstrip('\n')
                        physical = dict(inverse or original, _physical_checkout_identity=current)
                        if inverse is not None:
                            # The sole confirmed native operation created this
                            # ref; its exact CAS remains idempotent after exit.
                            _delete_workspace_ref(physical)
                        else:
                            _workspace_git(physical, created=False)
                        # A durable locator tombstone precedes handing back
                        # the same physical marker and publishing old bytes.
                        bind_task(locator, receipt["run_id"], old)
                        _handoff_copy_marker(marker_fd, original)
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

