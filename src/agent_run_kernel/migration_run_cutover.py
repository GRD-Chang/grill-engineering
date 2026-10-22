"""One fenced whole-Run cutover and bounded inverse of its entire resource set."""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import cast

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.runner_lease import default_runner_lock_path, runner_management_lease
from agent_run.task_identity import TaskKey

from .cleanup_adapter import _hold_ref
from .effects import LocalGit
from .kernel import Kernel
from .legacy_admission import legacy_task_lock, read_source_fence, source_fence
from .migration import MigrationRefused, SourceSnapshot, capture_v6
from .migration_cutover import _OLD_PROTOCOL, _original_protocol
from .migration_run import OriginalRunFacts, read_original_run_import
from .model import wire_json
from .task_location import locked_task, read_task
from .migration_facts import ImportedRunFacts
from .migration_run_install import install_imported_facts
from .migration_run_projection import verify_imported_installation
from .migration_run_resources import authenticate_imported_resources
from .migration_run_proof import imported_resource_layout


def _import_origin(facts: ImportedRunFacts, managed: ManagedWorkspace, nonce: str) -> dict[str, str]:
    if facts.source.task is None or facts.source.checkout_uuid is None:
        raise MigrationRefused('source_original_identity')
    return read_source_fence(source_fence({'nonce': nonce, 'source_db': str(facts.source.snapshot.source),
        'source_sha256': facts.source.snapshot.sha256, 'run_id': facts.spec.run_id,
        'task_fingerprint': facts.source.task.fingerprint, 'source_uuid': facts.source.checkout_uuid,
        'target_db': str(managed.state_root / facts.spec.run_id / 'kernel.sqlite3')}))


def _import_refs(facts: ImportedRunFacts) -> dict[str, str]:
    final = next(delivery for delivery in facts.spec.deliveries if delivery.kind.value == 'final_run')
    run = facts.run_resource
    if run.expected_oid is None:
        raise MigrationRefused('source_run_head_unproven')
    refs = {run.source_ref: run.expected_oid, final.target_ref: final.base.sha}
    for subject in facts.subjects:
        for resource in subject.resources:
            if resource.source_ref in refs or resource.disposition == 'new-unused':
                raise MigrationRefused('source_resource_union')
            refs[resource.source_ref] = resource.expected_oid if resource.expected_oid is not None else '0' * len(run.expected_oid)
    return refs


def _execute_original_import(facts: ImportedRunFacts, *, nonce: str, evidence: Path,
        inverse: bool = False) -> Kernel | dict[str, str]:
    """One fixed original-Task transfer/inverse, internal to the sealed decoder route.

    Public entry must decode the sealed source and its documents; a caller's
    dataclass is not admission. This implementation still authenticates current
    source identity, all physical claims, the fence and complete prepared file.
    It cannot be used for a copied/private-Host transport or arbitrary steps.
    """
    from . import migration_cutover as original
    from . import migration_layout as layouts

    if facts.source.external_readproof is None:
        raise MigrationRefused('source_external_readback_required')
    snapshot, spec = facts.source.snapshot, facts.spec
    managed = ManagedWorkspace.for_repository(spec.repository)
    task = TaskKey(managed.repository_root, spec.repository, spec.parent_issue)
    if (facts.source.mode != 'original' or facts.source.task != task
            or snapshot.source != managed.state_root / 'state.sqlite3'
            or snapshot.path.is_symlink() or snapshot.path.samefile(snapshot.source)):
        raise MigrationRefused('source_original_identity')
    with runner_management_lease(default_runner_lock_path()), legacy_task_lock(task, managed.state_root), locked_task(task) as location:
        managed.open_path()
        sentinel = managed.state_root / 'task-control' / (task.fingerprint + '.json')
        if sentinel.exists() or sentinel.is_symlink():
            raise MigrationRefused('source_legacy_control_json')
        with tempfile.TemporaryDirectory(dir=managed.state_root, prefix='.canonical-cutover-') as directory:
            checked = capture_v6(snapshot.path, Path(directory) / 'sealed.sqlite3')
            if (checked.sha256, checked.source_protocol, checked.schema_sha256, checked.table_rows) != (
                    snapshot.sha256, snapshot.source_protocol, snapshot.schema_sha256, snapshot.table_rows):
                raise MigrationRefused('snapshot_changed')
            accepted = SourceSnapshot(snapshot.source, checked.path, checked.sha256, checked.source_protocol,
                checked.schema_sha256, checked.table_rows)
            current = replace(facts, source=replace(facts.source, snapshot=accepted))
            origin = _import_origin(current, managed, nonce)
            destination = Path(origin['target_db'])
            protocol = _original_protocol(snapshot.source)
            retired = protocol != _OLD_PROTOCOL
            if retired and read_source_fence(protocol) != origin:
                raise MigrationRefused('source_original_fence_changed')
            prior = read_task(location)
            if not inverse and prior is not None and (not retired or prior != (spec.run_id, destination)):
                raise MigrationRefused('native_task_already_bound')
            if destination.exists():
                if destination.is_symlink():
                    raise MigrationRefused('native_original_cutover_provenance')
                native = Kernel(destination)
            else:
                if retired or inverse:
                    raise MigrationRefused('native_original_cutover_missing')
                authenticate_imported_resources(current, origin, evidence)
                destination.parent.mkdir(mode=0o700, exist_ok=True)
                native = install_imported_facts(current, destination, origin=origin)
            verify_imported_installation(native, current, origin=origin)
            anchor = min(subject.subject for subject in facts.subjects)
            with native.storage.transaction(write=False) as tx:
                started = tx.rows("SELECT subject,identity,material_id FROM history WHERE kind='v6_original_rollback_started'")
                if started and (len(started) != 1 or started[0]['subject'] != anchor or started[0]['identity'] != nonce
                        or tx.get(started[0]['material_id'], anchor) != wire_json(origin)):
                    raise MigrationRefused('native_original_cutover_provenance')
            if not inverse and started:
                raise MigrationRefused('native_original_rollback_started')
            if inverse and (protocol != source_fence(origin) and not (started and protocol == _OLD_PROTOCOL)
                    or prior not in ((spec.run_id, destination), None) or prior is None and not started):
                raise MigrationRefused('source_original_fence_changed')
            with ExitStack() as refs:
                for ref, sha in sorted(_import_refs(current).items()):
                    refs.enter_context(_hold_ref(LocalGit(managed.repository_root), managed.repository_root, ref, sha))
                authenticate_imported_resources(current, origin, evidence, detached=retired or inverse)
                original._retire_original(snapshot.source, checked.path, origin, retired=retired, release=protocol == _OLD_PROTOCOL if inverse else False)
                if inverse and not started:
                    with native.storage.transaction() as tx:
                        material = tx.put(anchor, 'v6_original_rollback', wire_json(origin))
                        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                            (anchor, 'v6_original_rollback_started', nonce, material, native.clock()))
                for subject in sorted(current.subjects, key=lambda value: value.subject):
                    for resource in subject.resources:
                        if resource.disposition == 'existing' and not resource.cleanup_pending:
                            layout = imported_resource_layout(resource, origin)
                            (layouts.restore_layout if inverse else layouts.detach_layout)(layout)
                authenticate_imported_resources(current, origin, evidence, detached=not inverse)
                if inverse:
                    original._withdraw_original_pointer(location, spec.run_id, destination)
                    original._retire_original(snapshot.source, checked.path, origin, retired=retired, release=True)
                    with native.storage.transaction() as tx:
                        if not tx.rows("SELECT 1 FROM history WHERE kind='v6_original_rollback_complete'"):
                            material = tx.put(anchor, 'v6_original_rollback', wire_json(origin))
                            tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                                (anchor, 'v6_original_rollback_complete', nonce, material, native.clock()))
                else:
                    if read_task(location) != prior:
                        raise MigrationRefused('native_task_binding_changed')
                    original.bind_task(location, spec.run_id, destination)
    if inverse:
        return {'status': 'rolled_back', 'nonce': nonce, 'source_database': str(snapshot.source),
            'retained_native_database': str(destination), 'next_action': 'original_continuation'}
    return Kernel(destination)


def _origin(snapshot: SourceSnapshot, facts: OriginalRunFacts, managed: ManagedWorkspace, nonce: str) -> dict[str, str]:
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        uuid = db.execute('SELECT value_checkout_identity FROM run_root WHERE run_id=?', (facts.header.run_id,)).fetchone()[0]
    return read_source_fence(source_fence({'nonce': nonce, 'source_db': str(snapshot.source),
        'source_sha256': snapshot.sha256, 'run_id': facts.header.run_id,
        'task_fingerprint': facts.header.task.fingerprint, 'source_uuid': uuid,
        'target_db': str(managed.state_root / facts.header.run_id / 'kernel.sqlite3')}))


def migrate_original_run_v6(snapshot: SourceSnapshot, *, repository: str, parent_number: int,
        nonce: str, profile_snapshot: Path, external_snapshot: Path) -> Kernel:
    managed = ManagedWorkspace.for_repository(repository)
    task = TaskKey(managed.repository_root, repository, parent_number)
    facts = read_original_run_import(snapshot, profile_snapshot, external_snapshot=external_snapshot,
        checkouts_snapshot=external_snapshot.parent / 'checkouts.json')
    if facts.source.task != task:
        raise MigrationRefused('source_original_identity')
    return cast(Kernel, _execute_original_import(facts, nonce=nonce, evidence=external_snapshot.parent))


def rollback_original_run_v6(snapshot: SourceSnapshot, *, repository: str, parent_number: int,
        nonce: str, external_snapshot: Path) -> dict[str, str]:
    managed = ManagedWorkspace.for_repository(repository)
    task = TaskKey(managed.repository_root, repository, parent_number)
    facts = read_original_run_import(snapshot, external_snapshot.parent / 'profile.json',
        external_snapshot=external_snapshot, checkouts_snapshot=external_snapshot.parent / 'checkouts.json')
    if facts.source.task != task:
        raise MigrationRefused('source_original_identity')
    return cast(dict[str, str], _execute_original_import(facts, nonce=nonce,
        evidence=external_snapshot.parent, inverse=True))
