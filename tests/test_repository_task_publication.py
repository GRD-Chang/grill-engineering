"""Canonical Task/current publication: real SQLite and synthetic Host records.

No Host, Worker, child, systemd, external service, or physical exit is run.
Terminal qualification fixtures exercise the existing predicate; they are not
proof of a real delivered/merged PR. This file is supplied UNRUN by the author.
"""
from agent_run.runtime.task import create_task, reconcile_task, selected_kernel
from contextlib import contextmanager, closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from agent_run.runtime.run_selection import repository_candidates, select_repository
from agent_run.core import kernel as core
from agent_run.contracts.task import TaskKey
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, Execution, HostObservation, ParentIssue, Requirements, RunSpec
from agent_run.queries.read_facts import QueryError
from agent_run.storage import routing, sqlite as storage_module, tasks
from agent_run.storage.routing import RepositoryRoute
from agent_run.storage.preflight import UnsupportedState
from agent_run.storage.sqlite import Storage
from support.offline import fixture_inputs

SUBJECT = 'shared-subject'


def proposal(route, name, parent=1, *, language='en'):
    base = Commit('a' * 40, 'b' * 40)
    delivery = Delivery(SUBJECT, DeliveryKind.PARENT_ONLY, parent, 'unobserved', base,
        'refs/heads/work-' + name, 'refs/heads/main')
    spec = RunSpec(name, route.repository, parent, 'r', 'g', (delivery,), language=language,
        repository_root=str(route.repository_root), inputs=fixture_inputs())
    return spec, Requirements(ParentIssue(parent, 'Parent ' + str(parent), 'Frozen ' + str(parent)), ())


def create(route, name, parent=1, *, language='en'):
    spec, source = proposal(route, name, parent, language=language)
    return create_task(route, TaskKey(route.repository_root, route.repository, parent), spec, source)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    route = RepositoryRoute(tmp_path / 'state' / 'kernel.sqlite3', tmp_path / 'repository', 'example/project')
    route.initialize()
    assert create(route, 'A', 1).created and create(route, 'B', 2).created
    def no_locator(*args, **kwargs):
        raise AssertionError('Canonical repository operation touched legacy publication/admission')
    for module, names in ((core, ('read_task', 'bind_task', 'locked_admission')), (tasks, ('read_task', 'bind_task'))):
        for name in names:
            assert not hasattr(module, name)
    return route


def dump(route):
    with closing(sqlite3.connect(route.database)) as db:
        return tuple(db.iterdump())


def parsed(*, command='resume', run_id=None, parent=None, repo='example/project', state_dir=None, data_root=None):
    return SimpleNamespace(command=command, run_id=run_id, parent=parent, repo=repo,
        state_dir=state_dir, data_root=data_root)


def admit(kernel, directory, action='same-host'):
    # This is a durable Core request, not a physical launch or platform proof.
    return kernel.admit_host(action, kind='run', command=('/unexecuted/python',),
        environment_carrier=directory / 'environment.json', preflight=lambda: None)


def test_normal_task_controls_profiles_and_host_intent_use_only_canonical_current_rows(repository, tmp_path):
    a, b = selected_kernel(repository, 'A'), selected_kernel(repository, 'B')
    # Normal synthetic claims make both Stops actual fences. Their original
    # exit producer must settle each Run before Resume may admit another Host.
    executions = (a.claim_executor('same-executor'), b.claim_executor('same-executor'))
    assert a.stop_task('same-stop') == b.stop_task('same-stop') == 'applied'
    assert a.status(SUBJECT)['stopped'] and b.status(SUBJECT)['stopped']
    for kernel, execution in zip((a, b), executions):
        kernel.confirm_executor_exit(execution)
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one('SELECT executor FROM runs WHERE id=?', (execution.run_id,))[0] is None
            assert tx.one('SELECT status FROM retired_executors WHERE run_id=? AND owner=? AND epoch=?',
                          (execution.run_id, execution.owner, execution.epoch))[0] == 'exited'
    assert a.configure_task_profiles('same-config') == 2
    with b.storage.transaction(write=False) as tx:
        assert tx.one('SELECT profile_revision FROM runs WHERE id=?', ('B',))[0] == 1
    assert b.configure_task_profiles('same-config') == 2
    assert a.configure_task_profiles('same-config') == 2
    # Explicit internal Resume clears the ordinary fence; Host admission still
    # performs the canonical current-pointer check inside its original writer.
    assert a.control('resume', 'resume-A') == 'applied'
    request = admit(a, tmp_path)
    assert request.run_id == 'A' and a.host_request('same-host') == request
    before = dump(repository)
    assert admit(a, tmp_path) == request  # Same original intent, one durable Host.
    assert dump(repository) == before
    with a.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM hosts WHERE run_id=?', ('A',))[0] == 1
        assert tx.one('SELECT count(*) FROM hosts WHERE run_id=?', ('B',))[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    assert b.status(SUBJECT)['stopped']
    assert not tasks.task_path(TaskKey(repository.repository_root, repository.repository, 1)).exists()


def test_foreign_host_request_is_refused_before_task_lock_or_audit(repository, tmp_path, monkeypatch):
    a, b = selected_kernel(repository, 'A'), selected_kernel(repository, 'B')
    request = admit(a, tmp_path)
    before = dump(repository)
    def forbidden(*args, **kwargs):
        raise AssertionError('Foreign Host entered a Task mutex')
    monkeypatch.setattr(core, 'task_lock', forbidden)
    with pytest.raises(KernelError):
        b.control_host(request, 'stop', 'foreign-control')
    assert dump(repository) == before


def test_control_and_failed_executor_settle_only_the_original_run_with_synthetic_observation(repository, tmp_path):
    a = selected_kernel(repository, 'A')
    request = admit(a, tmp_path)
    assert a.dispatch_host(request)
    observation = HostObservation('running', request.unit_name, invocation_id='synthetic-invocation',
        cgroup='/synthetic/A', boot_id='synthetic-boot', main_pid=321, start_token='synthetic-token', usage_ready=True)
    assert a.observe_host(request, observation) == 'running'
    assert a.control_host(request, 'stop', 'host-stop', subject=SUBJECT) == 'applied'
    execution = Execution(request.run_id, request.owner, request.epoch)
    sibling = selected_kernel(repository, 'B').status(SUBJECT)
    with a.executor_scope(execution) as fail:
        fail(RuntimeError('Controlled non-physical failure'))
    with a.storage.transaction(write=False) as tx:
        assert tx.one('SELECT epoch FROM runs WHERE id=?', ('A',))[0] == request.epoch + 1
        assert tx.one('SELECT count(*) FROM retired_executors WHERE run_id=?', ('A',))[0] == 1
        assert tx.one('SELECT count(*) FROM retired_executors WHERE run_id=?', ('B',))[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    assert selected_kernel(repository, 'B').status(SUBJECT) == sibling


def qualify_terminal_fixture(route, run):
    # Only the no-Host/no-seat terminal branch of the admitted predicate. This
    # does not claim a physical execution, acceptance, merge or cleanup proof.
    with route.at_run(run).transaction(ordered=False) as tx:
        tx.execute('UPDATE subjects SET phase=? WHERE run_id=?', ('complete', run))


def test_terminal_stop_keeps_the_original_complete_predicate_without_new_action(repository):
    qualify_terminal_fixture(repository, 'A')
    before = dump(repository)
    assert selected_kernel(repository, 'A').stop_task('complete-noop') == 'complete'
    assert dump(repository) == before


def test_foreign_task_current_pointer_is_refused_by_the_same_database_constraint(repository):
    before = dump(repository)
    task = TaskKey(repository.repository_root, repository.repository, 1)
    with pytest.raises(sqlite3.IntegrityError):
        with repository.metadata().transaction(ordered=False) as owner:
            owner.execute('UPDATE tasks SET current_run=? WHERE id=?', ('B', task.fingerprint))
    assert dump(repository) == before


def test_unreadable_current_pointer_keeps_original_failed_executor_barrier_without_claiming_exit(repository, tmp_path, monkeypatch):
    a = selected_kernel(repository, 'A')
    request = admit(a, tmp_path)
    assert a.dispatch_host(request)
    observation = HostObservation('running', request.unit_name, invocation_id='synthetic-invocation',
        cgroup='/synthetic/A', boot_id='synthetic-boot', main_pid=321, start_token='synthetic-token', usage_ready=True)
    assert a.observe_host(request, observation) == 'running'
    before = dump(repository)
    retained = []
    @contextmanager
    def lock(task):
        # Observe transfer intent but keep this fixture's real lock bounded to
        # the test scope; never leak a process-lifetime FD into a pytest worker.
        with tasks.task_lock(task):
            yield lambda: retained.append(True)
    def unreadable(*args):
        raise OSError('Controlled current pointer unavailable')
    error = RuntimeError('Controlled executor failure')
    monkeypatch.setattr(core, 'task_lock', lock)
    with monkeypatch.context() as patch:
        patch.setattr(core, 'run_is_current', unreadable)
        with a.executor_scope(Execution(request.run_id, request.owner, request.epoch)) as fail:
            fail(error)
    assert retained == [True]
    assert len(error.__notes__) == 2
    assert dump(repository) == before


@pytest.mark.parametrize('operation', ['stop', 'abandon', 'configure', 'admit'])
def test_pointer_advancement_between_observation_and_writer_never_mutates_the_historical_run(repository, tmp_path, monkeypatch, operation):
    a = selected_kernel(repository, 'A')
    original = core.Kernel._task_admission
    after_winner = []
    @contextmanager
    def admission(task, **kwargs):
        with original(task, **kwargs) as location:
            qualify_terminal_fixture(repository, 'A')
            assert create(repository, 'successor', 1).created
            after_winner.append(dump(repository))
            yield location
    monkeypatch.setattr(core.Kernel, '_task_admission', staticmethod(admission))
    with pytest.raises(KernelError, match='task_location_identity'):
        if operation == 'stop':
            a.stop_task('stale-stop')
        elif operation == 'abandon':
            a.abandon_task('stale-abandon', resource_inspections=())
        elif operation == 'configure':
            a.configure_task_profiles('stale-config')
        else:
            admit(a, tmp_path, 'stale-admit')
    assert after_winner and dump(repository) == after_winner[0]
    assert repository.task(TaskKey(repository.repository_root, repository.repository, 1)).current_run == 'successor'


def test_profile_mutations_share_original_owner_and_poison_rollback_with_the_current_guard(repository, monkeypatch):
    a = selected_kernel(repository, 'A')
    original = core.Kernel._configure_profiles_in
    before = dump(repository)
    def caught_failure(kernel, tx, action, **kwargs):
        original(kernel, tx, action, **kwargs)
        try:
            tx.execute('INSERT INTO nonexistent_task_scope_table VALUES (1)')
        except sqlite3.OperationalError:
            pass
        assert tx.failed
        return 2
    monkeypatch.setattr(core.Kernel, '_configure_profiles_in', caught_failure)
    with pytest.raises(KernelError, match='transaction_aborted'):
        a.configure_task_profiles('rolled-back-config')
    assert dump(repository) == before


def test_first_resume_selector_finds_current_run_without_a_json_publication_or_execution(repository):
    request = parsed(parent=1)
    selected = select_repository(request, repository_candidates(request, (repository,)))
    assert len(selected) == 1 and selected[0].run_id == 'A' and selected[0].current
    assert selected[0].task == TaskKey(repository.repository_root, repository.repository, 1)
    assert selected_kernel(repository, selected[0].run_id).storage.path == repository.database
    # No CLI/Host is enabled or run by a successful metadata selection.
    assert not hasattr(routing, 'REPOSITORY_PUBLIC_READY')
    with repository.at_run('A').transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM hosts')[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0


def test_parent_history_exact_id_and_current_selection_do_not_alias_another_task(repository, monkeypatch):
    qualify_terminal_fixture(repository, 'A')
    assert create(repository, 'successor', 1).created
    with repository.at_run('B').transaction() as tx:
        tx.execute('UPDATE requirement_versions SET body=? WHERE run_id=?', ('B' * 100000, 'B'))
    monkeypatch.setattr(storage_module, 'QUERY_LIMIT', 8192)
    parent = parsed(parent=1)
    candidates = repository_candidates(parent, (repository,))
    assert sorted((i.run_id, i.current) for i in candidates) == [('A', False), ('successor', True)]
    assert select_repository(parent, candidates)[0].run_id == 'successor'
    history = parsed(command='history', run_id='A')
    assert select_repository(history, repository_candidates(history, (repository,)))[0].run_id == 'A'
    sibling = parsed(parent=2)
    assert select_repository(sibling, repository_candidates(sibling, (repository,)))[0].run_id == 'B'


@pytest.mark.parametrize('field,value', [('parent', True), ('parent', 0), ('run_id', ''), ('run_id', 1)])
def test_invalid_selector_never_selects_or_changes_task_binding(repository, field, value):
    request = parsed(**{field: value})
    before = dump(repository)
    for invoke in (lambda: repository_candidates(request, (repository,)), lambda: select_repository(request, ())):
        with pytest.raises(QueryError) as refused:
            invoke()
        assert refused.value.code == 'run_locator_invalid'
    assert dump(repository) == before


def test_ambiguous_same_id_across_repository_roots_is_refused_and_exact_repo_isolated(repository, tmp_path):
    foreign = RepositoryRoute(tmp_path / 'other' / 'kernel.sqlite3', tmp_path / 'other-repository', 'another/project')
    foreign.initialize()
    assert create(foreign, 'A', 1).created
    request = parsed(run_id='A', repo=None)
    candidates = repository_candidates(request, (repository, foreign))
    with pytest.raises(QueryError) as ambiguous:
        select_repository(request, candidates)
    assert ambiguous.value.code == 'run_locator_conflict'
    request.repo = 'EXAMPLE/PROJECT'
    selected = select_repository(request, repository_candidates(request, (repository, foreign)))
    assert len(selected) == 1 and selected[0].database == repository.database
    with pytest.raises(QueryError) as duplicate:
        repository_candidates(request, (repository, repository))
    assert duplicate.value.code == 'run_locator_conflict'


def test_unreadable_or_missing_metadata_does_not_mean_new_task_or_initialization(repository, tmp_path, monkeypatch):
    before = dump(repository)
    def unreadable(self):
        raise OSError('Controlled repository metadata read failure')
    with monkeypatch.context() as patch:
        patch.setattr(RepositoryRoute, 'metadata', unreadable)
        with pytest.raises(OSError):
            repository_candidates(parsed(parent=1), (repository,))
    missing = RepositoryRoute(tmp_path / 'missing' / 'kernel.sqlite3', repository.repository_root, repository.repository)
    with pytest.raises(UnsupportedState, match='unsupported_state') as failure:
        repository_candidates(parsed(parent=1), (missing,))
    assert failure.value.reason == 'unreadable_state' and failure.value.path == missing.database
    assert failure.value.recovery_hint
    assert not missing.database.exists() and not missing.database.parent.exists()
    assert dump(repository) == before


def test_committed_creation_reply_loss_is_discoverable_as_original_proposal_not_a_second_run(repository, monkeypatch):
    original = sqlite3.connect
    fired = []
    class ReplyLost(sqlite3.Connection):
        def commit(self):
            trigger = self.total_changes > 0 and not fired
            super().commit()
            if trigger:
                fired.append(True)
                raise OSError('Controlled lost reply after real COMMIT')
    def connect(*args, **kwargs):
        kwargs['factory'] = ReplyLost
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    assert create(repository, 'original-proposal', 3).run_id == 'original-proposal'
    reopened = RepositoryRoute(repository.database, repository.repository_root, repository.repository)
    request = parsed(parent=3)
    selected = select_repository(request, repository_candidates(request, (reopened,)))
    assert [item.run_id for item in selected] == ['original-proposal']
    assert reconcile_task(reopened, selected[0].task, 'original-proposal').run_id == 'original-proposal'
    assert create(reopened, 'loser', 3, language='zh').run_id == 'original-proposal'
    with reopened.at_run('original-proposal').transaction(write=False) as tx:
        assert tx.one('SELECT language FROM runs WHERE id=?', ('original-proposal',))[0] == 'en'
        assert tx.one('SELECT count(*) FROM runs WHERE parent_issue=?', (3,))[0] == 1
        assert tx.one('SELECT count(*) FROM runs WHERE id=?', ('loser',))[0] == 0
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
    assert fired == [True]
