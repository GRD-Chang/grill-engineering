"""A real original-source fence, rather than retirement of another copy."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import traceback

import pytest

from agent_run.runner_lease import RunnerLeaseBusy
from agent_run.task_identity import TaskKey
from agent_run_kernel import migration_cutover
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.kernel import Kernel
from agent_run_kernel.local_effects import LocalEffects
from agent_run_kernel.legacy_admission import read_source_fence
from agent_run_kernel.migration import MigrationRefused, capture_v6
from agent_run_kernel.offline import OfflinePort
from agent_run_kernel.task_location import read_task, task_path

from test_kernel_migration_completed_history import _continue_final
from test_kernel_migration_original_source import _git_bytes, _managed_source


class _ManagedSourcePort(OfflinePort):
    local_root: Path

    @property
    def local(self) -> LocalEffects:
        # Match the production Executor's exact managed resource root while
        # retaining the independent fake remote database under self.root.
        return LocalEffects(self.git, owned_root=self.local_root)


_OLD_WRITES = r'''
import json,sys
from pathlib import Path
from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.task_control import TaskControlStore
from agent_run.task_identity import TaskKey
request=json.load(sys.stdin)
root,state,github=map(Path,(request['root'],request['state'],request['github']))
task=TaskKey(root,'example/project',1)
for operation in (lambda:TaskControlStore(state).claim_action(task,kind='run',payload={'parent':1}),
                  lambda:Controller(FixtureGitHubReader(github),GitRepository(root),StateStore(state)).start(1)):
    try:
        operation()
    except IncompatibleRunStateError:
        pass
    else:
        raise AssertionError('unchanged02 accepted its original retired authority')
print('both-original-write-entries-refused')
'''


def test_original_completed_cutover_fences_old_writers_and_continues_same_managed_task(
    tmp_path: Path, git_repo: Path,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    task = TaskKey(managed.repository_root, 'example/project', 1)
    with closing(sqlite3.connect(snapshot.path)) as source:
        run_id, uuid = source.execute('SELECT run_id,value_checkout_identity FROM run_root').fetchone()
        assert source.execute('SELECT count(*) FROM runs').fetchone()[0] == 1
        assert source.execute('SELECT count(*) FROM control_tasks').fetchone()[0] == 1
        assert source.execute("SELECT count(*) FROM control_executors WHERE status='exited' AND exited_at IS NOT NULL AND pid>0 AND process_start_token IS NOT NULL").fetchone()[0] == 1
    assert read_task(task_path(task)) is None
    before = _git_bytes(managed.repository_root)
    nonce = '6b' * 16
    migrate = getattr(migration_cutover, 'migrate_completed_original_v6')
    kernel = migrate(snapshot, repository='example/project', parent_number=1, nonce=nonce,
                     profile_snapshot=paths['profile'], github_snapshot=paths['github'],
                     observation_snapshot=paths['observation'], original_report=paths['report'])
    target = managed.state_root / run_id / 'kernel.sqlite3'
    assert kernel.storage.path == target
    assert read_task(task_path(task)) == (run_id, target)
    assert _git_bytes(managed.repository_root) == before
    assert hashlib.sha256(snapshot.path.read_bytes()).hexdigest() == snapshot.sha256
    with closing(sqlite3.connect(paths['database'])) as source:
        protocol = source.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
    receipt = read_source_fence(protocol)
    assert receipt == {'nonce': nonce, 'source_db': str(paths['database']),
                       'source_sha256': snapshot.sha256, 'run_id': run_id,
                       'task_fingerprint': task.fingerprint, 'source_uuid': uuid,
                       'target_db': str(target)}
    with kernel.storage.transaction(write=False) as tx:
        proof = tx.one("SELECT subject,identity,material_id FROM history WHERE kind='v6_original_cutover'")
        assert proof['identity'] == nonce
        assert json.loads(tx.get(proof['material_id'], proof['subject'])) == receipt
        assert tx.one('SELECT executor FROM runs')[0] is None
        for table in ('actions', 'hosts', 'invocations', 'leases'):
            assert tx.one(f'SELECT count(*) FROM {table}')[0] == 0
    frozen = tmp_path / 'unchanged-02'
    old_environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.pathsep.join(
        str(frozen / name) for name in ('project/src', 'project/tests'))}
    rejected = subprocess.run([sys.executable, '-c', _OLD_WRITES], input=json.dumps({
        'root': str(managed.repository_root), 'state': str(managed.state_root),
        'github': str(paths['github'])}), env=old_environment, text=True,
        capture_output=True, timeout=15)
    assert rejected.returncode == 0, rejected.stdout + rejected.stderr
    assert rejected.stdout.strip() == 'both-original-write-entries-refused'
    assert _git_bytes(managed.repository_root) == before
    admitted = Kernel.open_task(task)  # The actual Core gate must accept the exact transfer.
    assert admitted is not None and admitted.storage.path == target
    port = _ManagedSourcePort.create(tmp_path / 'same-workspace-remote')
    port.local_root = managed.root / 'worktrees' / run_id
    port.git = LocalGit(managed.repository_root)
    _continue_final(admitted, port, paths)
    assert admitted.source_status()['completions'][0]['merge_sha'] == json.loads(
        paths['report'].read_text())['integrated_sha']


def test_original_cutover_refuses_another_real_runner_usage_without_stopping_it(
    tmp_path: Path, git_repo: Path,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    task = TaskKey(managed.repository_root, 'example/project', 1)
    before = _git_bytes(managed.repository_root)
    migrate = getattr(migration_cutover, 'migrate_completed_original_v6')
    command = '''
from agent_run.runner_lease import runner_usage_lease, default_runner_lock_path
import sys
with runner_usage_lease(default_runner_lock_path()):
    print("other-runner-live", flush=True)
    assert sys.stdin.read(1) == "x"
'''
    child = subprocess.Popen([sys.executable, '-c', command], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(5), 'usage child did not acquire its actual shared lease'
        assert child.stdout.readline().strip() == 'other-runner-live'
        with pytest.raises(RunnerLeaseBusy):
            migrate(snapshot, repository='example/project', parent_number=1, nonce='6c' * 16,
                    profile_snapshot=paths['profile'], github_snapshot=paths['github'],
                    observation_snapshot=paths['observation'], original_report=paths['report'])
        assert child.poll() is None  # Migration cannot stop another Task to acquire the lease.
        assert read_task(task_path(task)) is None
        assert paths['database'].read_bytes() == snapshot.path.read_bytes()
        assert _git_bytes(managed.repository_root) == before
    finally:
        try:
            child.stdin.write('x')
            child.stdin.close()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            error = child.stderr.read()
            child.stderr.close()
    assert child.returncode == 0, error


@pytest.mark.parametrize('published', [False, True], ids=['source-fenced', 'pointer-published'])
def test_original_cutover_recovers_only_exact_nonce_after_real_process_exit(
    tmp_path: Path, git_repo: Path, published: bool,
) -> None:
    managed, paths = _managed_source(tmp_path, git_repo)
    snapshot = capture_v6(paths['database'], tmp_path / 'sealed.sqlite3')
    task = TaskKey(managed.repository_root, 'example/project', 1)
    with closing(sqlite3.connect(snapshot.path)) as source:
        run_id = source.execute('SELECT run_id FROM runs').fetchone()[0]
    before_git = _git_bytes(managed.repository_root)
    nonce = '7c' * 16
    arguments = dict(repository='example/project', parent_number=1, nonce=nonce,
                     profile_snapshot=paths['profile'], github_snapshot=paths['github'],
                     observation_snapshot=paths['observation'], original_report=paths['report'])
    pid = os.fork()
    if pid == 0:
        original_bind = migration_cutover.bind_task

        def exit_at_publication(location: Path, selected_run: str, destination: Path) -> None:
            if published:
                original_bind(location, selected_run, destination)
            os._exit(72 if published else 71)

        migration_cutover.bind_task = exit_at_publication
        try:
            migration_cutover.migrate_completed_original_v6(snapshot, **arguments)
        except BaseException:
            (tmp_path / 'cutover-child-error.txt').write_text(traceback.format_exc())
            os._exit(79)
        os._exit(78)
    ready, descriptor = False, None
    try:
        descriptor = os.pidfd_open(pid)
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            ready = bool(selector.select(15))
        assert ready, 'cutover child did not exit at the selected publication boundary'
        waited, status = os.waitpid(pid, 0)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not ready:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    error = tmp_path / 'cutover-child-error.txt'
    assert waited == pid and os.WIFEXITED(status), status
    assert os.WEXITSTATUS(status) == (72 if published else 71), error.read_text() if error.exists() else status
    target = managed.state_root / run_id / 'kernel.sqlite3'
    assert target.is_file()
    with closing(sqlite3.connect(paths['database'])) as source:
        receipt = read_source_fence(source.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0])
    assert receipt['nonce'] == nonce and receipt['target_db'] == str(target)
    assert read_task(task_path(task)) == ((run_id, target) if published else None)
    assert _git_bytes(managed.repository_root) == before_git
    source_before, target_before = paths['database'].read_bytes(), target.read_bytes()
    with pytest.raises(MigrationRefused, match='source_original_fence_changed'):
        migration_cutover.migrate_completed_original_v6(snapshot, **dict(arguments, nonce='7d' * 16))
    assert paths['database'].read_bytes() == source_before
    assert target.read_bytes() == target_before
    assert read_task(task_path(task)) == ((run_id, target) if published else None)
    recovered = migration_cutover.migrate_completed_original_v6(snapshot, **arguments)
    assert recovered.storage.path == target
    assert read_task(task_path(task)) == (run_id, target)
    assert paths['database'].read_bytes() == source_before
    assert target.read_bytes() == target_before
    assert hashlib.sha256(snapshot.path.read_bytes()).hexdigest() == snapshot.sha256
    assert _git_bytes(managed.repository_root) == before_git
