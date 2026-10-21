"""The single offline tool's input parsing and original-source observation.

No delivery decisions or writable Run representation live here. Inspection
reuses the converter's source/resource validation and does not transfer Task
authority, observe GitHub, create native work, or claim that cutover completed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Sequence

from agent_run.git_errors import GitError
from agent_run.managed_workspace import ManagedWorkspace, normalize_repository
from agent_run.runner_lease import RunnerLeaseError, default_runner_lock_path, runner_management_lease
from agent_run.task_identity import TaskKey

from .legacy_admission import legacy_task_lock
from .migration import MigrationRefused, capture_v6
from .migration_active import _read_profile
from .migration_completed import _existing_completed_git, _read_completed
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


def inspect_original_v6(repository: str, parent_number: int, evidence_dir: Path) -> dict[str, Any]:
    """Collect one settled original completion without changing its authority."""
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
            runs = db.execute('SELECT run_id,repository,parent_number FROM runs').fetchmany(2)
        if (len(runs) != 1 or runs[0][1:] != (task.repository, task.parent_number)
                or not isinstance(runs[0][0], str)
                or re.fullmatch(r'run-[1-9][0-9]*-[0-9a-f]{16}', runs[0][0]) is None):
            raise MigrationRefused('source_original_identity')
        run_id = runs[0][0]
        profile_path = managed.state_root / 'profiles' / (run_id + '.json')
        profile, profile_sha = _read_profile(profile_path)
        facts = _read_completed(snapshot.path, profile, profile_sha, snapshot.sha256)
        target = managed.state_root / run_id / 'kernel.sqlite3'
        if target.exists() or target.is_symlink():
            raise MigrationRefused('native_original_candidate_exists')
        final = managed.root / 'worktrees' / run_id / 'run-publication'
        _existing_completed_git(snapshot, snapshot.path, facts, target,
                                managed.repository_root, final, managed.repository_root / '.git')
        profile_body = profile_path.read_bytes()
        if hashlib.sha256(profile_body).hexdigest() != profile_sha:
            raise MigrationRefused('source_profile_changed')
        _write(destination / 'profile.json', profile_body)
        with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            clone_uuid = db.execute('SELECT value_checkout_identity FROM run_root WHERE run_id=?',
                                    (run_id,)).fetchone()[0]
        result = {
            'status': 'inspection_only', 'external_status': 'not_read',
            'source': {'path': str(snapshot.source), 'run_id': run_id,
                       'sha256': snapshot.sha256, 'task_fingerprint': task.fingerprint},
            'profile': {'source_path': str(profile_path), 'sha256': profile_sha},
            'git': {'repository_root': str(managed.repository_root), 'clone_uuid': clone_uuid,
                    'common_git_dir': str(managed.repository_root / '.git'),
                    'run_ref': 'refs/heads/' + facts.run_branch, 'run_head': facts.integrated_sha,
                    'default_ref': 'refs/heads/' + facts.base_branch, 'default_head': facts.base_sha},
            'supported_source': {'ticket': facts.ticket, 'generation': 1, 'review_window': 1,
                                 'physical_scope': 'reaped-local',
                                 'developments_used': facts.developments_used,
                                 'reviews_used': facts.reviews_used},
            'evidence_dir': str(destination),
        }
        _write(destination / 'inspection.json', wire_json(result))
        descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Inspect or explicitly migrate an offline v6 Task')
    parser.add_argument('--repo', required=True, type=_repository)
    parser.add_argument('--parent', required=True, type=_positive)
    parser.add_argument('--evidence-dir', required=True, type=Path)
    parser.add_argument('--inspect', action='store_true', help='Collect original local evidence without retiring source authority')
    options = parser.parse_args(argv)
    try:
        if not options.inspect:
            raise MigrationRefused('source_external_readback_required')
        result = inspect_original_v6(options.repo, options.parent, options.evidence_dir)
    except (MigrationRefused, KernelError, GitError, RunnerLeaseError, OSError, sqlite3.Error) as error:
        print(json.dumps({'status': 'refused', 'code': getattr(error, 'code', type(error).__name__),
                          'message': str(error), 'evidence_dir': str(options.evidence_dir.absolute())}))
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0
