"""The single offline tool's input parsing and original-source observation.

No delivery decisions or writable Run representation live here. Inspection
reuses the converter's source/resource validation and does not transfer Task
authority, observe GitHub, create native work, or claim that cutover completed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Sequence

from agent_run.git_errors import GitError
from agent_run.github import GhGitHubReader, GitHubReadError
from agent_run.managed_workspace import ManagedWorkspace, normalize_repository
from agent_run.runner_lease import RunnerLeaseError, default_runner_lock_path, runner_management_lease
from agent_run.task_identity import TaskKey

from .legacy_admission import legacy_task_lock
from .migration import MigrationRefused, SOURCE_PROTOCOL, SourceSnapshot, capture_v6
from .migration_active import _read_profile
from .migration_completed import _completed_evidence, _read_document, read_original_completed_inspection
from .migration_readback import read_issue_close_facts
from .source_reading import read_pull_request_facts, read_remote_heads, read_requirements
from .model import KernelError, wire_json
from .task_location import locked_task, read_task


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError('Expected a positive Parent Issue number') from error
    if number < 1:
        raise argparse.ArgumentTypeError('Expected a positive Parent Issue number')
    return number


def _repository(value: str) -> str:
    try:
        return normalize_repository(value)
    except GitError as error:
        raise argparse.ArgumentTypeError('Expected an exact owner/repository') from error


def _write(path: Path, body: bytes) -> None:
    with path.open('xb') as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _collect_original_run_locked(managed: ManagedWorkspace, task: TaskKey, destination: Path,
                                 snapshot: SourceSnapshot, *, external: bool) -> tuple[dict[str, Any], SourceSnapshot]:
    """Collect one whole Run while the caller holds management and both Task locks."""
    from .migration_conversion import _git
    from .migration_run import read_original_run, WorkedTicket
    from .migration_run_cutover import _origin
    from .migration_run_external import collect_run_external, prove_run_head, verify_run_external
    from .migration_run_resources import authenticate_run_resources, seal_run_checkouts
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        run_id = db.execute('SELECT run_id FROM runs').fetchone()[0]
    profile_path = managed.state_root / 'profiles' / (run_id + '.json')
    facts = read_original_run(snapshot, profile_path)
    header = facts.header
    if header.task != task:
        raise MigrationRefused('source_original_identity')
    target = managed.state_root / run_id / 'kernel.sqlite3'
    if target.exists() or target.is_symlink():
        raise MigrationRefused('native_original_candidate_exists')
    proof = collect_run_external(facts, managed.repository_root) if external else None
    if proof is not None:
        run_head, _ = verify_run_external(facts, proof, managed.repository_root)
    else:
        run_head = _git(managed.repository_root, 'rev-parse', '--verify', 'refs/heads/' + header.run_branch)
        prove_run_head(facts, managed.repository_root, run_head)
    sealed = seal_run_checkouts(facts, destination)
    origin = _origin(snapshot, facts, managed, '0' * 32)
    authenticate_run_resources(snapshot, facts, origin, destination, sealed, run_head)
    _write(destination / 'checkouts.json', wire_json(sealed))
    if proof is not None:
        _write(destination / 'external-readproof.json', wire_json(proof))
    profile_body = profile_path.read_bytes()
    if hashlib.sha256(profile_body).hexdigest() != facts.profile_sha256:
        raise MigrationRefused('source_profile_changed')
    _write(destination / 'profile.json', profile_body)
    result: dict[str, Any] = {
        'status': 'inspection_only', 'external_status': 'verified' if external else 'not_read',
        'source': {'path': str(snapshot.source), 'run_id': run_id, 'sha256': snapshot.sha256,
                   'task_fingerprint': task.fingerprint},
        'profile': {'source_path': str(profile_path), 'sha256': facts.profile_sha256},
        'git': {'repository_root': str(managed.repository_root), 'clone_uuid': origin['source_uuid'],
                'common_git_dir': str(managed.repository_root / '.git'), 'run_ref': 'refs/heads/' + header.run_branch,
                'run_head': run_head, 'default_ref': 'refs/heads/' + header.default_branch, 'default_head': header.default_sha},
        'supported_source': {'kind': 'original_run', 'generation': 1, 'review_window': 1,
            'physical_scope': 'reaped-local', 'subjects': [
                {'subject': ticket.owner.subject, 'kind': ticket.kind if isinstance(ticket, WorkedTicket) else 'unallocated'}
                for ticket in facts.tickets]}, 'evidence_dir': str(destination)}
    _write(destination / 'inspection.json', wire_json(result))
    _sync_directory(destination)
    return result, snapshot


def _collect_original_v6(repository: str, parent_number: int, evidence_dir: Path, *,
                         external: bool = False) -> tuple[dict[str, Any], SourceSnapshot]:
    """Collect one supported original unit without changing its authority."""
    from .migration_active import read_original_returned_inspection, _verify_returned_git
    from .migration_checkout import seal_checkout
    managed = ManagedWorkspace.for_repository(repository)
    task = TaskKey(managed.repository_root, managed.repository, parent_number)
    destination = evidence_dir.absolute()
    if (destination.parent.resolve() != destination.parent
            or destination.is_relative_to(managed.root)
            or destination.exists() or destination.is_symlink()):
        raise MigrationRefused('evidence_directory_required')
    with runner_management_lease(default_runner_lock_path()), legacy_task_lock(task, managed.state_root), locked_task(task) as location:
        managed.open_path()
        if read_task(location) is not None:
            raise MigrationRefused('native_task_already_bound')
        sentinel = managed.state_root / 'task-control' / (task.fingerprint + '.json')
        if sentinel.exists() or sentinel.is_symlink():
            raise MigrationRefused('source_legacy_control_json')
        destination.mkdir(mode=0o700)
        snapshot = capture_v6(managed.state_root / 'state.sqlite3', destination / 'source.sqlite3')
        with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.row_factory = sqlite3.Row
            runs = db.execute('SELECT run_id,repository,parent_number,status FROM runs').fetchmany(2)
            if (len(runs) != 1 or tuple(runs[0])[1:3] != (task.repository, task.parent_number)
                    or not isinstance(runs[0][0], str)
                    or re.fullmatch(r'run-[1-9][0-9]*-[0-9a-f]{16}', runs[0][0]) is None):
                raise MigrationRefused('source_original_identity')
            root_id = db.execute('SELECT value_graph_snapshot_id FROM run_root WHERE run_id=?', (runs[0][0],)).fetchone()
            if root_id is not None and db.execute('SELECT count(*) FROM run_graph_scope_order WHERE run_id=? AND snapshot_id=?',
                                                  (runs[0][0], root_id[0])).fetchone()[0] > 1:
                return _collect_original_run_locked(managed, task, destination, snapshot, external=external)
            run_id = runs[0][0]
            root = db.execute('SELECT * FROM run_root WHERE run_id=?', (run_id,)).fetchone()
            job = db.execute('SELECT j.* FROM work_jobs j JOIN work_job_layout l ON l.run_id=j.run_id AND l.active_job_id=j.job_id WHERE j.run_id=?', (run_id,)).fetchone()
            base = db.execute('SELECT value_branch FROM run_condition_base WHERE run_id=?', (run_id,)).fetchone()
            if root is None or job is None or base is None:
                raise MigrationRefused('source_original_identity')
            returned = runs[0]['status'] == 'active'
            ticket, pr_number = job['value_ticket_number'], job['value_pr_number']
            if type(ticket) is not int or ticket < 1 or (not returned and (type(pr_number) is not int or pr_number < 1)):
                raise MigrationRefused('source_original_identity')
            runner = _completed_evidence(db, run_id, root['value_runner_github_identity'],
                                         owner='run:' + run_id, kind='runner_github_identity')
            run_ref, ticket_ref = 'refs/heads/' + root['value_run_branch'], 'refs/heads/' + job['value_ticket_branch']
            base_ref = 'refs/heads/' + base['value_branch']
            clone_uuid = root['value_checkout_identity']
        profile_path = managed.state_root / 'profiles' / (run_id + '.json')
        profile, profile_sha = _read_profile(profile_path)
        readproof: dict[str, Any] | None = None
        if external:
            reader = GhGitHubReader(repository, working_directory=managed.repository_root, runner_identity=runner)
            requirements = read_requirements(reader, parent_number)
            before = None if returned else read_pull_request_facts(reader, pr_number)
            close = None if returned else read_issue_close_facts(reader, ticket)
            run_head, base_head = read_remote_heads(managed.repository_root, run_ref, base_ref, runner_identity=runner)
            ticket_head, repeated_base = read_remote_heads(managed.repository_root, ticket_ref, base_ref, runner_identity=runner)
            if base_head != repeated_base:
                raise MigrationRefused('source_external_refs')
            after = None if returned else read_pull_request_facts(reader, pr_number)
            readproof = {'source_sha256': snapshot.sha256, 'runner_identity': runner,
                         'requirements': {'parent': asdict(requirements.parent), 'issues': [
                             {**asdict(issue), 'labels': sorted(issue.labels)} for issue in requirements.issues]},
                         'remote_heads': {run_ref: run_head, ticket_ref: ticket_head, base_ref: base_head}}
            if returned:
                readproof['source_kind'] = 'returned_role'
            else:
                readproof.update(close=close, pr_before=before, pr_after=after)
            readproof = json.loads(wire_json(readproof))
        target = managed.state_root / run_id / 'kernel.sqlite3'
        if returned:
            imported = read_original_returned_inspection(snapshot, profile_snapshot=profile_path, readproof=readproof)
            selected = imported.subjects[0]
            resource, candidate, historical_base = selected.resources[0], selected.candidate, selected.historical_base
            if resource.checkout is None or historical_base is None:
                raise MigrationRefused('source_existing_resources')
            if target.exists() or target.is_symlink():
                raise MigrationRefused('native_original_candidate_exists')
            archive_sha, index_sha = seal_checkout(resource.checkout, managed.repository_root, destination / 'checkout.tar')
            _verify_returned_git(snapshot, target, destination / 'checkout.tar', archive_sha, index_sha,
                repository=imported.spec.repository, run_id=imported.spec.run_id, ticket=selected.issue,
                source_ref=resource.source_ref, candidate_sha=None if candidate is None else candidate.sha,
                candidate_tree=None if candidate is None else candidate.tree, base_sha=historical_base.sha,
                run_branch=imported.run_resource.source_ref.removeprefix('refs/heads/'),
                base_branch=imported.spec.deliveries[-1].target_ref.removeprefix('refs/heads/'))
            if readproof is not None:
                readproof.update(checkout_archive_sha256=archive_sha, checkout_index_sha256=index_sha)
            run_oid = base_sha = historical_base.sha
            budget = next(item for item in selected.budgets if item.current)
            pending = next(call for call in selected.attempts if call.pending)
            supported = {'ticket': selected.issue, 'generation': selected.generation, 'review_window': budget.window,
                'physical_scope': 'reaped-local', 'kind': 'returned_role', 'role': pending.role.value}
        else:
            imported = read_original_completed_inspection(snapshot, profile_snapshot=profile_path, readproof=readproof)
            completed = imported.subjects[0]
            if completed.completion is None or completed.historical_base is None:
                raise MigrationRefused('source_completion')
            run_oid, base_sha = completed.completion.integrated.sha, completed.historical_base.sha
            budget = next(item for item in completed.budgets if item.current)
            supported = {'ticket': completed.issue, 'generation': completed.generation, 'review_window': budget.window,
                'physical_scope': 'reaped-local', 'developments_used': budget.developments_used,
                'reviews_used': budget.reviews_used}
        if readproof is not None:
            _write(destination / 'external-readproof.json', wire_json(readproof))
        profile_body = profile_path.read_bytes()
        if hashlib.sha256(profile_body).hexdigest() != profile_sha:
            raise MigrationRefused('source_profile_changed')
        _write(destination / 'profile.json', profile_body)
        result: dict[str, Any] = {
            'status': 'inspection_only', 'external_status': 'verified' if external else 'not_read',
            'source': {'path': str(snapshot.source), 'run_id': run_id,
                       'sha256': snapshot.sha256, 'task_fingerprint': task.fingerprint},
            'profile': {'source_path': str(profile_path), 'sha256': profile_sha},
            'git': {'repository_root': str(managed.repository_root), 'clone_uuid': clone_uuid,
                    'common_git_dir': str(managed.repository_root / '.git'),
                    'run_ref': run_ref, 'run_head': run_oid,
                    'default_ref': base_ref, 'default_head': base_sha},
            'supported_source': supported,
            'evidence_dir': str(destination),
        }
        _write(destination / 'inspection.json', wire_json(result))
        _sync_directory(destination)
        return result, snapshot


def inspect_original_v6(repository: str, parent_number: int, evidence_dir: Path) -> dict[str, Any]:
    return _collect_original_v6(repository, parent_number, evidence_dir)[0]


def _load_operation(repository: str, parent_number: int, evidence_dir: Path) -> tuple[dict[str, Any], SourceSnapshot, str]:
    """Read only the tool's original intent; it grants no admission by itself."""
    managed = ManagedWorkspace.for_repository(repository)
    directory = evidence_dir.absolute()
    if (directory.resolve() != directory or not directory.is_dir()
            or directory.is_relative_to(managed.root)):
        raise MigrationRefused('source_operation_required')
    try:
        operation = _read_document(directory / 'operation.json', limit=64 * 1024)[0]
        result = _read_document(directory / 'inspection.json')[0]
    except (MigrationRefused, OSError) as error:
        raise MigrationRefused('source_operation_required') from error
    fields = {'nonce', 'repository', 'parent_number', 'source', 'sha256', 'protocol', 'schema_sha256', 'table_rows'}
    source = managed.state_root / 'state.sqlite3'
    if (set(operation) != fields or operation['repository'] != repository
            or type(operation['parent_number']) is not int or operation['parent_number'] != parent_number
            or operation['source'] != str(source) or operation['protocol'] != SOURCE_PROTOCOL
            or not isinstance(operation['nonce'], str) or re.fullmatch(r'[0-9a-f]{32}', operation['nonce']) is None
            or any(not isinstance(operation[key], str) or re.fullmatch(r'[0-9a-f]{64}', operation[key]) is None
                   for key in ('sha256', 'schema_sha256'))
            or not isinstance(operation['table_rows'], dict)
            or any(not isinstance(name, str) or re.fullmatch(r'[a-z][a-z_0-9]*', name) is None
                   or type(count) is not int or count < 0 for name, count in operation['table_rows'].items())):
        raise MigrationRefused('source_operation_identity')
    original = result.get('source')
    task = TaskKey(managed.repository_root, repository, parent_number)
    if (not isinstance(original, dict) or original.get('path') != str(source)
            or original.get('sha256') != operation['sha256']
            or original.get('task_fingerprint') != task.fingerprint
            or not isinstance(original.get('run_id'), str)
            or re.fullmatch(r'run-[1-9][0-9]*-[0-9a-f]{16}', original['run_id']) is None):
        raise MigrationRefused('source_operation_identity')
    snapshot = SourceSnapshot(source, directory / 'source.sqlite3', operation['sha256'], operation['protocol'],
                              operation['schema_sha256'], tuple(sorted(operation['table_rows'].items())))
    return result, snapshot, operation['nonce']


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Inspect or explicitly migrate an offline v6 Task')
    parser.add_argument('--repo', required=True, type=_repository)
    parser.add_argument('--parent', required=True, type=_positive)
    parser.add_argument('--evidence-dir', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--inspect', action='store_true', help='Collect original local evidence without retiring source authority')
    mode.add_argument('--resume', action='store_true', help='Resume the original operation and nonce from its evidence directory')
    mode.add_argument('--rollback', action='store_true', help='Return the original authority only before native execution advances')
    options = parser.parse_args(argv)
    recovery_paths: dict[str, str] | None = None
    try:
        if options.inspect:
            result = inspect_original_v6(options.repo, options.parent, options.evidence_dir)
        else:
            from .migration_cutover import migrate_completed_original_v6, rollback_original_v6
            if options.resume or options.rollback:
                result, snapshot, nonce = _load_operation(options.repo, options.parent, options.evidence_dir)
            else:
                result, snapshot = _collect_original_v6(options.repo, options.parent, options.evidence_dir, external=True)
                nonce = secrets.token_hex(16)
                operation = {'nonce': nonce, 'repository': options.repo, 'parent_number': options.parent,
                             'source': str(snapshot.source), 'sha256': snapshot.sha256,
                             'protocol': snapshot.source_protocol, 'schema_sha256': snapshot.schema_sha256,
                             'table_rows': dict(snapshot.table_rows)}
                _write(options.evidence_dir / 'operation.json', wire_json(operation))
                _sync_directory(options.evidence_dir)
            managed = ManagedWorkspace.for_repository(options.repo)
            recovery_paths = {'source_database': str(snapshot.source), 'retained_native_database': str(
                managed.state_root / result['source']['run_id'] / 'kernel.sqlite3')}
            if options.rollback:
                if result.get('supported_source', {}).get('kind') == 'original_run':
                    from .migration_run_cutover import rollback_original_run_v6
                    result = rollback_original_run_v6(snapshot, repository=options.repo, parent_number=options.parent,
                        nonce=nonce, external_snapshot=options.evidence_dir / 'external-readproof.json')
                else:
                    result = rollback_original_v6(snapshot, repository=options.repo, parent_number=options.parent,
                                               nonce=nonce, external_snapshot=options.evidence_dir / 'external-readproof.json')
            else:
                if result.get('supported_source', {}).get('kind') == 'original_run':
                    from .migration_run_cutover import migrate_original_run_v6
                    kernel = migrate_original_run_v6(snapshot, repository=options.repo, parent_number=options.parent, nonce=nonce,
                        profile_snapshot=options.evidence_dir / 'profile.json', external_snapshot=options.evidence_dir / 'external-readproof.json')
                else:
                    kernel = migrate_completed_original_v6(
                        snapshot, repository=options.repo, parent_number=options.parent, nonce=nonce,
                        profile_snapshot=options.evidence_dir / 'profile.json',
                        external_snapshot=options.evidence_dir / 'external-readproof.json')
                result.update(status='migrated', nonce=nonce,
                              target={'database': str(kernel.storage.path), 'run_id': result['source']['run_id']})
    except (MigrationRefused, KernelError, GitError, GitHubReadError, RunnerLeaseError, OSError, sqlite3.Error) as error:
        failure: dict[str, Any] = {'status': 'refused', 'code': getattr(error, 'code', type(error).__name__),
                                   'message': str(error), 'evidence_dir': str(options.evidence_dir.absolute())}
        if failure['code'] == 'native_original_cutover_advanced' and recovery_paths is not None:
            failure.update(recovery_paths, next_action='rollforward')
        print(json.dumps(failure))
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0
