"""Final-protocol entry assembly and explicit carriers, using only SQLite/argv.

No Host, Worker, child, notification thread or real remote is started here.
The source defaults to one repository protocol. Synthetic exit entry tests stop at the
opening seam; they are not a systemd/foreground execution or receipt proof.
"""

from support.repository_fixture import create_kernel
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sys
import sqlite3

import pytest

from agent_run.cli import host_exit, lifecycle
from agent_run.cli.main import _parser
from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, HostLaunch, ParentIssue, Requirements, RunSpec
from agent_run.notifications.envelope import event_envelope
from agent_run.notifications.event_projection import event_state, project_event
from agent_run.notifications.progress import _source, obsolete_progress
from agent_run.notifications.sender import NativeNotifications
from agent_run.queries.host_exit import host_exit_current
from agent_run.queries.status_comment import status_subjects
from agent_run.runtime import executor
from agent_run.storage import routing, tasks
from agent_run.storage.routing import RepositoryRoute, open_run
from agent_run.storage.tasks import run_is_current
from support.environment import durable_snapshot
from support.offline import fixture_inputs

SUBJECT = 'shared-subject'


def proposal(root, name, parent):
    base = Commit('a' * 40, 'b' * 40)
    delivery = Delivery(SUBJECT, DeliveryKind.PARENT_ONLY, parent, 'unobserved', base,
        'refs/heads/work-' + name, 'refs/heads/main')
    spec = RunSpec(name, 'example/project', parent, 'r', 'g', (delivery,), language='en',
        repository_root=str(root), inputs=fixture_inputs())
    return spec, Requirements(ParentIssue(parent, 'Parent ' + str(parent), 'Frozen ' + str(parent)), ())


@pytest.fixture
def repository(tmp_path):
    route = RepositoryRoute(tmp_path / 'state' / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    route.initialize()
    for name, parent in (('A', 1), ('B', 2)):
        spec, source = proposal(route.repository_root, name, parent)
        assert route.create(TaskKey(route.repository_root, route.repository, parent), spec, source).created
    return route


def test_existing_handle_is_carried_unchanged_and_never_reopened_as_an_unscoped_root(repository, tmp_path):
    a = repository.at_run('A')
    assert open_run(repository.database, run_id='A', storage=a) is a
    assert open_run(repository.database, storage=a) is a
    for database, run in ((repository.database, 'B'), (tmp_path / 'other.sqlite3', 'A')):
        with pytest.raises(KernelError, match='run_scope_changed'):
            open_run(database, run_id=run, storage=a)
    # Final public opening requires the same exact Run, never an implicit root.
    opened = Kernel(repository.database, run_id='A')
    assert opened.storage.run_id == a.run_id == 'A'
    with pytest.raises(TypeError):
        Kernel(repository.database)


@pytest.mark.parametrize('has_executor', [False, True], ids=['idle', 'executor'])
def test_current_public_stop_keeps_durable_fence_and_exact_selected_run_without_host(tmp_path, monkeypatch, has_executor):
    from agent_run.adapters import workspace as workspace_module
    from agent_run.cli import selection
    from agent_run.runtime import package_lease
    from agent_run.notifications import sender
    database = tmp_path / 'legacy' / 'kernel.sqlite3'
    root = tmp_path / 'repository'
    spec, source = proposal(root, 'legacy-A', 99)
    with tasks.task_lock(TaskKey(root, spec.repository, spec.parent_issue)):
        original = create_kernel(database, spec, requirements=source)
    # The synthetic Executor claim uses the unchanged native save-clock guard;
    # it creates no Host or process. The idle branch needs no ordered write.
    execution = original.claim_executor('original-executor') if has_executor else None
    selected, notices = [], []
    @contextmanager
    def lease(path):
        selected.append(path)
        yield None  # Only the public selection/control seam is under test.
    monkeypatch.setattr(package_lease, 'runner_usage_lease', lease)
    monkeypatch.setattr(package_lease, 'default_runner_lock_path', lambda env: tmp_path / 'runner.lock')
    monkeypatch.setattr(workspace_module, 'workspace_for_root', lambda path:
        SimpleNamespace(repository_root=root, repository='example/project', state_root=database.parent))
    from agent_run.adapters.workspace import ManagedWorkspace
    canonical = SimpleNamespace(repository_root=root, repository='example/project', state_root=database.parent)
    monkeypatch.setattr(ManagedWorkspace, 'for_repository', staticmethod(lambda repository: canonical))
    monkeypatch.setattr(selection, 'managed_repository_routes', lambda request: (RepositoryRoute(database, root, spec.repository),))
    monkeypatch.setattr(sender, 'notify_committed', lambda path, **kw: notices.append((path, kw['storage'].run_id)))
    parsed = _parser('en').parse_args(['stop', spec.run_id, '--repo', spec.repository, '--json'])
    state, history = original.status(SUBJECT), original.history(SUBJECT)
    before = durable_snapshot(tmp_path)
    result = lifecycle.execute(parsed, language='en')
    assert result['run_id'] == spec.run_id and result['action'] == 'stop'
    assert selected == [tmp_path / 'runner.lock']
    if has_executor:
        assert result['result'] == 'accepted' and result['status'] == 'action_applied'
        assert result['action_id'] is not None and result['host'] == 'unknown'
        assert original.status(SUBJECT)['stopped'] is True
        assert notices == [(database, spec.run_id)]
        assert durable_snapshot(tmp_path) != before
    else:
        assert result['result'] == 'no_op' and result['status'] == 'not_running'
        assert result['action_id'] is None and notices == []
        assert original.status(SUBJECT) == state and original.history(SUBJECT) == history
        assert state['stopped'] is False and state['phase'] != 'complete'
        assert durable_snapshot(tmp_path) == before
    with original.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM hosts')[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert run_is_current(tx)
        if has_executor:
            assert execution is not None
            assert tx.one('SELECT status FROM actions WHERE run_id=? AND id=?',
                          (spec.run_id, result['action_id']))[0] == 'applied'
            assert tx.one('SELECT status FROM retired_executors WHERE run_id=? AND owner=? AND epoch=?',
                          (execution.run_id, execution.owner, execution.epoch))[0] == 'pending'
        else:
            assert tx.one('SELECT count(*) FROM actions')[0] == 0
            assert tx.one('SELECT count(*) FROM retired_executors')[0] == 0


def test_current_producer_nonempty_notification_page_retains_the_original_history_layout(tmp_path):
    spec, source = proposal(tmp_path / 'repository', 'current-page', 99)
    kernel = create_kernel(tmp_path / 'current' / 'kernel.sqlite3', spec, requirements=source)
    # Explicit subject cancellation still records its Action on an idle Run.
    assert kernel.stop_task('page-stop', subject=SUBJECT) == 'applied'
    observer = NativeNotifications.__new__(NativeNotifications)
    observer.database, observer.storage = kernel.storage.path, kernel.storage
    with kernel.storage.transaction(write=False) as tx:
        columns = tx.execute('PRAGMA table_info(history)').fetchall()
        assert 'run_id' in [column['name'] for column in columns]
        assert tx.one('SELECT count(*) FROM history')[0] > 0
        assert tx.one("SELECT count(*) FROM history WHERE run_id=? AND kind='control_applied' AND identity=?",
                      (spec.run_id, 'page-stop'))[0] == 1
        observer.run_id, observer.binding, observer.config, observer.state = observer._run(tx)
        highwater = tx.one('SELECT max(position) FROM history')[0]
    events, more, cursor = observer._page(0)
    assert events == [] and not more and cursor == highwater


@pytest.mark.parametrize('operation', ['run', 'resume', 'approve', 'stop', 'revise', 'requeue', 'abandon'])
def test_internal_public_route_stays_closed_before_lease_io_or_control(repository, monkeypatch, operation):
    assert not hasattr(routing, 'REPOSITORY_PUBLIC_READY')
    from agent_run.runtime import package_lease
    def forbidden(*args, **kwargs):
        raise KernelError('fixture_lease_boundary', 'Canonical route reached its required usage lease')
    monkeypatch.setattr(package_lease, 'runner_usage_lease', forbidden)
    arguments = [operation, '1' if operation == 'run' else 'A', '--repo', 'example/project']
    if operation == 'revise':
        arguments += ['--message', 'Accurate retained request']
    parsed = _parser('en').parse_args(arguments)
    with pytest.raises(KernelError, match='fixture_lease_boundary'):
        lifecycle.execute(parsed, language='en', repository_route=repository)
    assert repository.task(TaskKey(repository.repository_root, repository.repository, 1)).current_run == 'A'


def test_direct_creation_assembly_is_also_closed_before_preparation(repository, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Foreign repository route reached creation preparation')
    monkeypatch.setattr(lifecycle, 'prepare_creation', forbidden)
    parsed = _parser('en').parse_args(['run', '1', '--repo', 'example/project'])
    with pytest.raises(KernelError, match='managed_state_identity'):
        lifecycle._run(parsed, manager=None, environment={}, runner_lock=Path('/not-opened'),
            repository_route=repository)


def test_canonical_workspace_route_has_one_database_and_does_not_create_it(tmp_path):
    from agent_run.adapters.workspace import ManagedWorkspace
    workspace = ManagedWorkspace('example/project', tmp_path / 'managed')
    route = RepositoryRoute.for_workspace(workspace)
    assert route.database == workspace.state_root / 'kernel.sqlite3'
    assert route.repository_root == workspace.repository_root and route.repository == workspace.repository
    assert not route.database.exists()


def test_task_observation_uses_canonical_sql_and_original_replaceability_without_json(repository, monkeypatch):
    def forbidden(*args):
        raise AssertionError('Repository mode consulted a JSON Task publication')
    assert not hasattr(tasks, 'read_task')
    task = TaskKey(repository.repository_root, repository.repository, 1)
    current = repository.open_task(task)
    assert current is not None and current.storage.run_id == 'A'
    with current.storage.transaction(write=False) as tx:
        assert run_is_current(tx)
    with current.storage.transaction() as tx:
        tx.execute("UPDATE subjects SET phase='complete' WHERE run_id='A'")
    assert repository.open_task(task) is None
    spec, source = proposal(repository.repository_root, 'successor', 1)
    assert repository.create(task, spec, source).created
    with current.storage.transaction(write=False) as tx:
        assert not run_is_current(tx)
    assert repository.open_task(task).storage.run_id == 'successor'
    assert status_subjects(repository.database, storage=repository.at_run('B')) == []


@pytest.mark.parametrize('run_id', ['A', '-a`b`c % quoted value'])
def test_host_argv_seals_explicit_selector_as_one_item_and_keeps_original_default(tmp_path, run_id):
    database, lock = tmp_path / 'kernel.sqlite3', tmp_path / 'runner.lock'
    old = (sys.executable, '-m', 'agent_run.runtime.executor', '--database', str(database),
        '--action', 'action', '--runner-lock', str(lock))
    with pytest.raises(TypeError):
        executor.command(database, 'action', lock, {})  # No implicit Run selector.
    selected = executor.command(database, 'action', lock, {}, run_id=run_id)
    assert selected == (*old, '--run-id=' + run_id)
    assert executor.command(database, 'action', lock, {'LD_LIBRARY_PATH': '/controlled/library'}, run_id=run_id) == (
        '/usr/bin/env', 'LD_LIBRARY_PATH=/controlled/library', *selected)


@pytest.mark.parametrize('entry', ['executor', 'exit-hook'])
@pytest.mark.parametrize('run_id', [None, '-a`b`c % quoted value'])
def test_entry_parsers_propagate_exact_run_before_any_managed_execution(tmp_path, monkeypatch, entry, run_id):
    module = executor if entry == 'executor' else host_exit
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path / 'unread-credential'))
    monkeypatch.setenv('NOTIFY_SOCKET', 'unopened-socket')
    monkeypatch.setenv('SERVICE_RESULT', 'exit-code')
    monkeypatch.setenv('EXIT_CODE', 'exited')
    monkeypatch.setenv('EXIT_STATUS', '23')
    captured = []
    def opening(database, **kwargs):
        captured.append((database, kwargs))
        raise KernelError('controlled_opening_boundary')
    monkeypatch.setattr(module, 'Kernel', opening)
    arguments = ['--database', str(tmp_path / 'state.sqlite3'), '--action', 'action',
        '--runner-lock', str(tmp_path / 'runner.lock')]
    if run_id is not None:
        arguments.append('--run-id=' + run_id)
    if run_id is None:
        with pytest.raises(SystemExit) as refused:
            module.main(arguments)
        assert refused.value.code == 2 and captured == []
    else:
        assert module.main(arguments) == 1
        assert captured == [(tmp_path / 'state.sqlite3', {'run_id': run_id})]


@pytest.mark.parametrize('entry', ['executor', 'exit-hook'])
def test_staged_marker_is_rejected_by_real_entry_opening_before_a_host_or_credential_read(repository, monkeypatch, entry, tmp_path):
    module = executor if entry == 'executor' else host_exit
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', '/unread-credential')
    monkeypatch.setenv('NOTIFY_SOCKET', 'unopened-socket')
    monkeypatch.setenv('SERVICE_RESULT', 'exit-code')
    monkeypatch.setenv('EXIT_CODE', 'exited')
    monkeypatch.setenv('EXIT_STATUS', '23')
    import shutil
    retired = tmp_path / 'retired.sqlite3'
    shutil.copyfile(repository.database, retired)
    with closing(sqlite3.connect(retired)) as connection:
        connection.execute('UPDATE meta SET protocol=?', ('independent-delivery-repository-unactivated',))
    original_bytes = retired.read_bytes()
    assert module.main(['--database', str(retired), '--action', 'no-host',
        '--runner-lock', '/unopened-runner.lock', '--run-id=A']) == 1
    assert retired.read_bytes() == original_bytes
    with repository.at_run('A').transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM hosts WHERE run_id=?', ('A',))[0] == 0


def test_exit_hook_builder_carries_exact_selection_without_claiming_a_real_host(repository, monkeypatch, tmp_path):
    selected = repository.at_run('A')
    request = HostLaunch('A', 1, 'owner', 'nonce', 'unit.service', ('unexecuted',), tmp_path / 'environment.json')
    # Only command assembly: no synthetic observation is installed as Core exit proof.
    def kernel(storage):
        assert storage is selected
        return SimpleNamespace(storage=storage, host_request=lambda action: request)
    monkeypatch.setattr(host_exit.Kernel, '_from_storage', staticmethod(kernel))
    properties = host_exit.exit_properties(repository.database, 'action', request, tmp_path / 'runner.lock', {}, storage=selected)
    assert '"--run-id=A"' in properties[0]
    assert properties[1] == 'TimeoutStopSec=5s'
    assert selected.run_id == 'A'
    foreign = replace(request, run_id='B')
    with pytest.raises(ValueError, match='binding changed'):
        host_exit.exit_properties(repository.database, 'action', foreign, tmp_path / 'runner.lock', {}, storage=selected)


def test_notification_pages_and_progress_rechecks_keep_same_id_siblings_outside_the_selected_snapshot(repository, monkeypatch):
    a, b = repository.kernel('A'), repository.kernel('B')
    a.control('stop', 'same-action', subject=SUBJECT)
    b.control('stop', 'same-action', subject=SUBJECT)
    with a.storage.transaction(write=False) as tx:
        state_a = event_state(tx)
        assert state_a['run_id'] == 'A' and state_a['parent']['title'] == 'Parent 1'
        rows_a = tx.all('SELECT * FROM history WHERE run_id=? ORDER BY position', ('A',))
    with b.storage.transaction(write=False) as tx:
        state_b = event_state(tx)
        assert state_b['run_id'] == 'B' and state_b['parent']['title'] == 'Parent 2'
        foreign = dict(tx.one("SELECT * FROM history WHERE run_id=? AND kind='control_applied'", ('B',)))
    with a.storage.transaction(write=False) as tx:
        with pytest.raises(KernelError):
            project_event(tx, foreign, state_a, 'concise')
        with pytest.raises(KernelError, match='run_scope_changed'):
            project_event(tx, dict(rows_a[-1]), state_b, 'concise')
        foreign_event = {'source_position': foreign['position'], 'source_material_id': foreign['material_id'],
            'work_subject': foreign['subject'], 'generation': foreign['generation']}
        assert _source(tx, foreign_event) is None
        start_a = event_envelope(state_a, 'run_start', 'A', '')
        start_b = event_envelope(state_b, 'run_start', 'B', '')
        assert obsolete_progress(tx, [start_a, start_b], 'concise', current_boundary=True) == {start_a['id']}
    # Inspect sender page through the existing owner seam without starting its thread.
    observer = NativeNotifications.__new__(NativeNotifications)
    observer.database, observer.storage = repository.database, a.storage
    with a.storage.transaction(write=False) as tx:
        observer.run_id, observer.binding, observer.config, observer.state = observer._run(tx)
    from agent_run.notifications import sender
    projected_rows = []
    def project(tx, row, state, mode):
        assert row['run_id'] == tx.run_id == state['run_id'] == 'A'
        projected_rows.append(dict(row))
        return project_event(tx, row, state, mode)
    monkeypatch.setattr(sender, 'project_event', project)
    events, more, cursor = observer._page(0)
    assert events == [] and not more and cursor == rows_a[-1]['position']
    assert projected_rows and all(row['run_id'] == 'A' for row in projected_rows)
    from support.status_comments import core_status_comments
    context = core_status_comments(a)
    binding_runs = []
    transaction = a.storage.transaction
    @contextmanager
    def selected_transaction(**kwargs):
        assert kwargs['write'] is False
        with transaction(**kwargs) as tx:
            binding_runs.append(tx.run_id)
            yield tx
    with monkeypatch.context() as patch:
        patch.setattr(a.storage, 'transaction', selected_transaction)
        binding = context._read_binding()
    assert binding_runs == ['A']
    assert binding.repository_root == repository.repository_root
    assert binding.repository == repository.repository
    assert binding.runner_identity == a.operation_binding().inputs.runner_identity
    assert a.operation_binding().parent_number == 1
    assert b.operation_binding().parent_number == 2
    assert status_subjects(repository.database, storage=a.storage) == []
    assert host_exit_current(repository.database, {'run_id': 'B'}, storage=a.storage) is False
