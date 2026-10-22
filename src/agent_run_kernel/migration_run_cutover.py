"""One fenced whole-Run cutover and bounded inverse of its entire resource set."""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Mapping

from agent_run.managed_workspace import ManagedWorkspace
from agent_run.runner_lease import default_runner_lock_path, runner_management_lease
from agent_run.task_identity import TaskKey

from .cleanup_adapter import _hold_ref
from .effects import LocalGit
from .kernel import Kernel
from .legacy_admission import legacy_task_lock, read_source_fence, source_fence
from .migration import MigrationRefused, SourceSnapshot, capture_v6
from .migration_completed import _read_document
from .migration_conversion import _git
from .migration_cutover import _OLD_PROTOCOL, _original_protocol, _retire_original, _withdraw_original_pointer
from .migration_layout import detach_layout, restore_layout, verify_layout
from .migration_run import OriginalRunFacts, read_original_run
from .migration_run_external import verify_run_external
from .migration_run_install import build_original_run_candidate, run_head_evidence
from .migration_run_proof import ResourceProof
from .migration_run_resources import authenticate_run_resources, run_resource_proofs
from .model import wire_json
from .task_location import bind_task, locked_task, read_task
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
                _retire_original(snapshot.source, checked.path, origin, retired=retired, release=protocol == _OLD_PROTOCOL if inverse else False)
                if inverse and not started:
                    with native.storage.transaction() as tx:
                        material = tx.put(anchor, 'v6_original_rollback', wire_json(origin))
                        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                            (anchor, 'v6_original_rollback_started', nonce, material, native.clock()))
                for subject in sorted(current.subjects, key=lambda value: value.subject):
                    for resource in subject.resources:
                        if resource.disposition == 'existing':
                            layout = imported_resource_layout(resource, origin)
                            (restore_layout if inverse else detach_layout)(layout)
                authenticate_imported_resources(current, origin, evidence, detached=not inverse)
                if inverse:
                    _withdraw_original_pointer(location, spec.run_id, destination)
                    _retire_original(snapshot.source, checked.path, origin, retired=retired, release=True)
                    with native.storage.transaction() as tx:
                        if not tx.rows("SELECT 1 FROM history WHERE kind='v6_original_rollback_complete'"):
                            material = tx.put(anchor, 'v6_original_rollback', wire_json(origin))
                            tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                                (anchor, 'v6_original_rollback_complete', nonce, material, native.clock()))
                else:
                    if read_task(location) != prior:
                        raise MigrationRefused('native_task_binding_changed')
                    bind_task(location, spec.run_id, destination)
    if inverse:
        return {'status': 'rolled_back', 'nonce': nonce, 'source_database': str(snapshot.source),
            'retained_native_database': str(destination), 'next_action': 'original_continuation'}
    return Kernel(destination)


def prepared_original_run(native: Kernel, facts: OriginalRunFacts, origin: Mapping[str, str],
        proof: Mapping[str, Any], proof_sha: str, resources: Mapping[str, tuple[ResourceProof, ...]], run_head: str) -> None:
    """Check the one source-derived canonical installation, allowing only named audit changes."""
    from .migration_run_projection import verify_prepared_installation
    from .model import KernelError
    if native.storage.path != Path(origin['target_db']):
        raise MigrationRefused('native_original_cutover_advanced')
    try:
        verify_prepared_installation(native, facts, origin, proof, proof_sha, resources, run_head)
    except (KernelError, KeyError, TypeError, ValueError, sqlite3.Error) as error:
        raise MigrationRefused('native_original_cutover_advanced') from error


def _origin(snapshot: SourceSnapshot, facts: OriginalRunFacts, managed: ManagedWorkspace, nonce: str) -> dict[str, str]:
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        uuid = db.execute('SELECT value_checkout_identity FROM run_root WHERE run_id=?', (facts.header.run_id,)).fetchone()[0]
    return read_source_fence(source_fence({'nonce': nonce, 'source_db': str(snapshot.source),
        'source_sha256': snapshot.sha256, 'run_id': facts.header.run_id,
        'task_fingerprint': facts.header.task.fingerprint, 'source_uuid': uuid,
        'target_db': str(managed.state_root / facts.header.run_id / 'kernel.sqlite3')}))


def _refs(facts: OriginalRunFacts, resources: Mapping[str, tuple[ResourceProof, ...]], run_head: str) -> dict[str, str]:
    refs = {'refs/heads/' + facts.header.run_branch: run_head,
            'refs/heads/' + facts.header.default_branch: facts.header.default_sha}
    for group in resources.values():
        for resource in group:
            layout = resource.layout
            if layout['source_ref'] in refs:
                raise MigrationRefused('source_resource_union')
            refs[layout['source_ref']] = layout['expected_oid']
    return refs


def migrate_original_run_v6(snapshot: SourceSnapshot, *, repository: str, parent_number: int,
        nonce: str, profile_snapshot: Path, external_snapshot: Path) -> Kernel:
    managed = ManagedWorkspace.for_repository(repository)
    task = TaskKey(managed.repository_root, repository, parent_number)
    if snapshot.source != managed.state_root / 'state.sqlite3' or snapshot.path.is_symlink() or snapshot.path.samefile(snapshot.source):
        raise MigrationRefused('source_original_identity')
    with runner_management_lease(default_runner_lock_path()), legacy_task_lock(task, managed.state_root), locked_task(task) as location:
        managed.open_path()
        sentinel = managed.state_root / 'task-control' / (task.fingerprint + '.json')
        if sentinel.exists() or sentinel.is_symlink():
            raise MigrationRefused('source_legacy_control_json')
        with tempfile.TemporaryDirectory(dir=managed.state_root, prefix='.whole-cutover-') as directory:
            checked = capture_v6(snapshot.path, Path(directory) / 'sealed.sqlite3')
            if (checked.sha256, checked.schema_sha256, checked.table_rows) != (snapshot.sha256, snapshot.schema_sha256, snapshot.table_rows):
                raise MigrationRefused('snapshot_changed')
            accepted = SourceSnapshot(snapshot.source, checked.path, checked.sha256, checked.source_protocol, checked.schema_sha256, checked.table_rows)
            facts = read_original_run(accepted, profile_snapshot)
            if facts.header.task != task:
                raise MigrationRefused('source_original_identity')
            origin = _origin(accepted, facts, managed, nonce)
            proof, proof_sha = _read_document(external_snapshot)
            sealed = _read_document(external_snapshot.parent / 'checkouts.json')[0]
            run_head = proof.get('remote_heads', {}).get('refs/heads/' + facts.header.run_branch)
            if not isinstance(run_head, str):
                raise MigrationRefused('source_external_refs')
            destination = Path(origin['target_db'])
            protocol = _original_protocol(snapshot.source)
            retired = protocol != _OLD_PROTOCOL
            if retired and read_source_fence(protocol) != origin:
                raise MigrationRefused('source_original_fence_changed')
            prior = read_task(location)
            if prior is not None and (not retired or prior != (facts.header.run_id, destination)):
                raise MigrationRefused('native_task_already_bound')
            resources = run_resource_proofs(facts, origin, sealed)
            if destination.exists():
                if destination.is_symlink():
                    raise MigrationRefused('native_original_cutover_provenance')
                native = Kernel(destination)
                prepared_original_run(native, facts, origin, proof, proof_sha, resources, run_head)
            else:
                if retired:
                    raise MigrationRefused('native_original_cutover_missing')
                destination.parent.mkdir(mode=0o700, exist_ok=True)
                run_head, closes = verify_run_external(facts, proof, managed.repository_root)
                resources = authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head)
                with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.whole-prepared-') as private:
                    candidate = Path(private) / 'kernel.sqlite3'
                    build_original_run_candidate(accepted, facts, candidate, repository_root=managed.repository_root,
                        state_root=managed.state_root, origin=origin, resources=resources, run_head=run_head,
                        head_evidence=run_head_evidence(facts, run_head), external_readproof=proof,
                        external_readproof_sha256=proof_sha, close_readproofs=closes, profile_document=profile_snapshot.read_bytes())
                    descriptor = os.open(candidate, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    os.link(candidate, destination)
                    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                native = Kernel(destination)
            prepared_original_run(native, facts, origin, proof, proof_sha, resources, run_head)
            verify_run_external(facts, proof, managed.repository_root)
            with native.storage.transaction(write=False) as tx:
                if tx.rows("SELECT 1 FROM history WHERE kind='v6_original_rollback_started'"):
                    raise MigrationRefused('native_original_rollback_started')
            with ExitStack() as refs:
                for ref, sha in sorted(_refs(facts, resources, run_head).items()):
                    refs.enter_context(_hold_ref(LocalGit(managed.repository_root), managed.repository_root, ref, sha))
                authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head, detached=retired)
                _retire_original(snapshot.source, checked.path, origin, retired=retired)
                for subject in sorted(resources):
                    for resource in resources[subject]:
                        detach_layout(resource.layout)
                authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head, detached=True)
                for group in resources.values():
                    for resource in group:
                        verify_layout(resource.layout, state='detached')
                if read_task(location) != prior:
                    raise MigrationRefused('native_task_binding_changed')
                bind_task(location, facts.header.run_id, destination)
    return Kernel(destination)


def rollback_original_run_v6(snapshot: SourceSnapshot, *, repository: str, parent_number: int,
        nonce: str, external_snapshot: Path) -> dict[str, str]:
    managed = ManagedWorkspace.for_repository(repository)
    task = TaskKey(managed.repository_root, repository, parent_number)
    with runner_management_lease(default_runner_lock_path()), legacy_task_lock(task, managed.state_root), locked_task(task) as location:
        managed.open_path()
        with tempfile.TemporaryDirectory(dir=managed.state_root, prefix='.whole-inverse-') as directory:
            checked = capture_v6(snapshot.path, Path(directory) / 'sealed.sqlite3')
            if (checked.sha256, checked.schema_sha256, checked.table_rows) != (snapshot.sha256, snapshot.schema_sha256, snapshot.table_rows):
                raise MigrationRefused('snapshot_changed')
            accepted = SourceSnapshot(snapshot.source, checked.path, checked.sha256, checked.source_protocol, checked.schema_sha256, checked.table_rows)
            facts = read_original_run(accepted, external_snapshot.parent / 'profile.json')
            if facts.header.task != task or snapshot.source != managed.state_root / 'state.sqlite3':
                raise MigrationRefused('source_original_identity')
            origin = _origin(accepted, facts, managed, nonce)
            destination = Path(origin['target_db'])
            if destination.is_symlink() or not destination.is_file():
                raise MigrationRefused('native_original_cutover_missing')
            proof, proof_sha = _read_document(external_snapshot)
            sealed = _read_document(external_snapshot.parent / 'checkouts.json')[0]
            run_head = proof.get('remote_heads', {}).get('refs/heads/' + facts.header.run_branch)
            if not isinstance(run_head, str):
                raise MigrationRefused('source_external_refs')
            resources = run_resource_proofs(facts, origin, sealed)
            native = Kernel(destination)
            prepared_original_run(native, facts, origin, proof, proof_sha, resources, run_head)
            verify_run_external(facts, proof, managed.repository_root)
            authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head, detached=True)
            with native.storage.transaction(write=False) as tx:
                anchor = tx.one("SELECT subject FROM history WHERE kind='v6_run_import'")[0]
                started = tx.rows("SELECT subject,identity,material_id FROM history WHERE kind='v6_original_rollback_started'")
                if started and (len(started) != 1 or started[0]['identity'] != nonce
                        or tx.get(started[0]['material_id'], started[0]['subject']) != wire_json(origin)):
                    raise MigrationRefused('native_original_cutover_provenance')
            protocol = _original_protocol(snapshot.source)
            prior = read_task(location)
            if (protocol != source_fence(origin) and not (started and protocol == _OLD_PROTOCOL)
                    or prior not in ((facts.header.run_id, destination), None) or prior is None and not started):
                raise MigrationRefused('source_original_fence_changed')
            with ExitStack() as refs:
                for ref, sha in sorted(_refs(facts, resources, run_head).items()):
                    refs.enter_context(_hold_ref(LocalGit(managed.repository_root), managed.repository_root, ref, sha))
                authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head, detached=True)
                _retire_original(snapshot.source, checked.path, origin, retired=protocol != _OLD_PROTOCOL, release=protocol == _OLD_PROTOCOL)
                if not started:
                    with native.storage.transaction() as tx:
                        material = tx.put(anchor, 'v6_original_rollback', wire_json(origin))
                        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                            (anchor, 'v6_original_rollback_started', nonce, material, native.clock()))
                for subject in sorted(resources):
                    for resource in resources[subject]:
                        restore_layout(resource.layout)
                authenticate_run_resources(accepted, facts, origin, external_snapshot.parent, sealed, run_head)
                _withdraw_original_pointer(location, facts.header.run_id, destination)
                _retire_original(snapshot.source, checked.path, origin, retired=protocol != _OLD_PROTOCOL, release=True)
                with native.storage.transaction() as tx:
                    if not tx.rows("SELECT 1 FROM history WHERE kind='v6_original_rollback_complete'"):
                        material = tx.put(anchor, 'v6_original_rollback', wire_json(origin))
                        tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) VALUES (?,?,?,?,1,?)',
                            (anchor, 'v6_original_rollback_complete', nonce, material, native.clock()))
    return {'status': 'rolled_back', 'nonce': nonce, 'source_database': str(snapshot.source),
            'retained_native_database': str(destination), 'next_action': 'original_continuation'}
