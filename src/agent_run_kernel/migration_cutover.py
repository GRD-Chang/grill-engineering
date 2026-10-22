"""Explicit original-source cutover and bounded return of accepted v6 Tasks.

Original-source cutover requires the original settled Task, preserves
its managed clone, retires its protocol, then publishes its native Task pointer.
It does not claim proof that an arbitrary systemd Host has exited.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, cast

from agent_run.task_identity import TaskKey
from agent_run.managed_workspace import ManagedWorkspace
from agent_run.runner_lease import default_runner_lock_path, runner_management_lease

from .cleanup_adapter import _hold_ref
from .effects import LocalGit
from .kernel import Kernel
from .migration import (MigrationRefused, SourceSnapshot, _offline_source, _source_blocks,
                        capture_v6, source_identity, validate_snapshot)
from .legacy_admission import legacy_task_lock, read_source_fence, source_fence
from .migration_active import _read_profile
from .migration_completed import (_Completed, convert_completed_ticket_v6, _existing_completed_git,
                                  _read_completed, _read_document)
from .migration_quiescence import read_v6_host_exit
from .migration_records import V6RecordReader
from .model import wire_json
from .task_location import bind_task as bind_task, locked_task, read_task


_OLD_PROTOCOL = "sqlite-work-records-v6"


def _original_protocol(source: Path) -> str:
    source_identity(source)  # Before another open can release a caller's inode locks.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=0)) as db:
        rows = db.execute('SELECT key,value FROM schema_meta').fetchmany(2)
    if len(rows) != 1 or rows[0][0] != 'protocol' or not isinstance(rows[0][1], str):
        raise MigrationRefused('source_protocol')
    return str(rows[0][1])


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
    if returned or external_snapshot is not None:
        if external_snapshot is None:
            raise MigrationRefused('source_external_readback_required')
        from .migration_active import read_original_returned_import
        from .migration_completed import read_original_completed_import
        from .migration_run_cutover import _execute_original_import
        canonical = (read_original_returned_import(snapshot, profile_snapshot=profile_snapshot,
            external_snapshot=external_snapshot, checkout_archive=external_snapshot.parent / 'checkout.tar') if returned else
            read_original_completed_import(snapshot, profile_snapshot=profile_snapshot, external_snapshot=external_snapshot))
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
    if returned or external_snapshot is not None:
        if external_snapshot is None:
            raise MigrationRefused('source_external_readback_required')
        from .migration_active import read_original_returned_import
        from .migration_completed import read_original_completed_import
        from .migration_run_cutover import _execute_original_import
        profile_snapshot = managed.state_root / 'profiles' / (run_id + '.json')
        canonical = (read_original_returned_import(snapshot, profile_snapshot=profile_snapshot,
            external_snapshot=external_snapshot, checkout_archive=external_snapshot.parent / 'checkout.tar') if returned else
            read_original_completed_import(snapshot, profile_snapshot=profile_snapshot, external_snapshot=external_snapshot))
        result = cast(dict[str, str], _execute_original_import(canonical, nonce=nonce,
            evidence=external_snapshot.parent, inverse=True))
        result.pop('next_action', None)  # Preserve the existing Single CLI result.
        return result
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
