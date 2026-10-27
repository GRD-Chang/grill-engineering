"""Current Task reuse and fail-closed admission without an archived producer."""

from dataclasses import replace
import errno
import json
import os
from pathlib import Path

import pytest

import agent_run.adapters.systemd as host_adapter
from agent_run.adapters.workspace import ManagedWorkspace, read_git_identity
import agent_run.cli.lifecycle as lifecycle
from agent_run.cli.main import main
from agent_run.config.creation import freeze_creation
from agent_run.config.defaults import UserDefaultsStore
from agent_run.contracts.task import TaskKey
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.storage.preflight import UnsupportedState
from support.repository_fixture import read_binding
from agent_run.storage.sqlite import Transaction
from support.systemd_manager import controlled_manager


ORIGINAL_ACTION = 'original-start'


@pytest.fixture
def occupied_task(git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = ManagedWorkspace.for_repository('example/project')
    root = workspace.ensure_path(str(git_repo), identity=read_git_identity(git_repo))
    base = Commit(read_git(root, 'HEAD'), read_git(root, 'HEAD^{tree}'))
    delivery = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'unobserved', base,
                        'refs/heads/task', 'refs/heads/main')
    defaults = UserDefaultsStore()
    defaults.configure(language='en')
    spec = freeze_creation(RunSpec('original', workspace.repository, 1, 'parent', 'graph',
        (delivery,), repository_root=str(root)), defaults)
    requirements = Requirements(ParentIssue(1, 'Parent', 'Current requirement'), ())
    kernel = Kernel.create_task(workspace.state_root / 'kernel.sqlite3', spec,
                                requirements=requirements)
    manager, fixture, response, calls = controlled_manager(tmp_path)
    request = kernel.admit_host(ORIGINAL_ACTION, kind='run', command=fixture.command,
        environment_carrier=tmp_path / 'original-carrier', preflight=manager.preflight)
    assert kernel.dispatch_host(request)
    # A dispatched grant with no observable unit is not proof that Host exited.
    raw = json.loads(response.read_text())
    raw['fields'].update(Id=request.unit_name, Description=request.description, LoadState='not-found')
    response.write_text(json.dumps(raw))
    observation = manager.observe(request)
    assert observation.disposition == 'unknown'
    assert kernel.observe_host(request, observation) == 'unknown'
    monkeypatch.setattr(lifecycle, '_active_runner', lambda: True)
    monkeypatch.setattr(host_adapter, 'SystemdHost', lambda **_kwargs: manager)
    return kernel, workspace, spec, requirements, manager, calls, request


def read_git(root: Path, revision: str) -> str:
    from agent_run.adapters.git_objects import LocalGit
    return LocalGit(root).run('rev-parse', revision).stdout.strip()


def admission_snapshot(kernel, workspace):
    task = TaskKey(workspace.repository_root, workspace.repository, 1)
    with kernel.storage.transaction(write=False) as tx:
        actions = [tuple(row) for row in tx.rows('SELECT id,kind,status FROM actions ORDER BY id')]
        hosts = [tuple(row) for row in tx.rows(
            'SELECT action_id,owner,status,usage_ready,usage_released FROM hosts ORDER BY action_id')]
    return (read_binding(task, kernel.storage.path), tuple(sorted(workspace.state_root.rglob('kernel.sqlite3'))),
            kernel.host_request(ORIGINAL_ACTION), actions, hosts)


def forbid_new_launches(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Occupied Task entered creation or new Host submission')
    monkeypatch.setattr(lifecycle, 'prepare_creation', forbidden)
    monkeypatch.setattr(lifecycle, 'submit', forbidden)


def assert_original_only(kernel, workspace, spec, calls, before):
    task = TaskKey(workspace.repository_root, workspace.repository, 1)
    assert read_binding(task, kernel.storage.path) == (spec.run_id, kernel.storage.path.resolve())
    assert admission_snapshot(kernel, workspace) == before
    assert before[1] == (kernel.storage.path,)
    assert len(before[3]) == len(before[4]) == 1
    assert kernel.active_host_action() == ORIGINAL_ACTION
    assert kernel.host_dispatched(ORIGINAL_ACTION)
    assert all(json.loads(line)[0] != 'systemd-run' for line in calls.read_text().splitlines())


def test_unknown_native_host_reuses_original_run_without_a_new_binding(occupied_task, tmp_path):
    kernel, workspace, spec, requirements, manager, calls, request = occupied_task
    before = admission_snapshot(kernel, workspace)
    task = TaskKey(workspace.repository_root, workspace.repository, 1)
    destination = workspace.state_root / 'replacement' / 'kernel.sqlite3'
    assert Kernel.open_task(task).storage.path == kernel.storage.path
    reused = Kernel.create_task(kernel.storage.path, replace(spec, run_id='replacement'), requirements=requirements)
    assert reused.storage.path == kernel.storage.path
    assert not destination.parent.exists()
    carrier = tmp_path / 'second-carrier'
    with pytest.raises(KernelError, match='executor_exit_unproven'):
        reused.admit_host('second-start', kind='run', command=request.command,
                         environment_carrier=carrier, preflight=manager.preflight)
    assert not carrier.exists()
    assert_original_only(kernel, workspace, spec, calls, before)


def test_public_run_only_attaches_the_original_unknown_host(occupied_task, monkeypatch, capsys):
    kernel, workspace, spec, _requirements, _manager, calls, request = occupied_task
    before = admission_snapshot(kernel, workspace)
    forbid_new_launches(monkeypatch)
    assert main(['run', '1', '--repo', workspace.repository, '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['result'] == 'attached' and result['host'] == 'unknown'
    assert result['run_id'] == spec.run_id and result['action_id'] == ORIGINAL_ACTION
    assert result['requested_action'] == 'run' and result['requested_action_applied'] is False
    assert_original_only(kernel, workspace, spec, calls, before)


def test_human_attach_receipt_does_not_claim_a_new_action_or_expose_private_identity(occupied_task, monkeypatch, capsys):
    kernel, workspace, spec, _requirements, _manager, calls, request = occupied_task
    before = admission_snapshot(kernel, workspace)
    UserDefaultsStore().configure(language='zh')  # The existing Run remains English.
    forbid_new_launches(monkeypatch)
    assert main(['run', '1', '--repo', workspace.repository]) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert f': {spec.run_id} ' not in output.out and ORIGINAL_ACTION not in output.out
    assert request.unit_name not in output.out and request.nonce not in output.out
    assert 'new action not applied' in output.out
    assert 'Host state unconfirmed' in output.out
    assert 'agent-run status --repo example/project --parent 1' in output.out
    assert_original_only(kernel, workspace, spec, calls, before)


@pytest.mark.parametrize('entry', ['open', 'create', 'admit', 'run'])
def test_unreadable_current_task_pointer_refuses_before_new_state(
    occupied_task, tmp_path, monkeypatch, capsys, entry,
):
    kernel, workspace, spec, requirements, manager, calls, request = occupied_task
    before = admission_snapshot(kernel, workspace)
    task = TaskKey(workspace.repository_root, workspace.repository, 1)
    destination = workspace.state_root / 'replacement' / 'kernel.sqlite3'
    carrier = tmp_path / 'new-carrier'
    before = admission_snapshot(kernel, workspace)
    reads = []
    original_rows = Transaction.rows
    def unavailable(owner, sql, parameters=(), *, limit=128):
        if sql == 'SELECT * FROM tasks WHERE id=?' and parameters == (task.fingerprint,):
            reads.append(task.fingerprint)
            raise OSError(errno.EIO, 'Controlled canonical Task read unavailable')
        return original_rows(owner, sql, parameters, limit=limit)
    forbid_new_launches(monkeypatch)
    # Fail only the real canonical Task reader, never fabricate or remove a binding.
    with monkeypatch.context() as outage:
        outage.setattr(Transaction, 'rows', unavailable)
        if entry == 'run':
            assert main(['run', '1', '--repo', workspace.repository, '--json']) == 2
            result = json.loads(capsys.readouterr().out)
            assert result['result'] == 'error'
            assert result['diagnostics'][0]['code'] == 'task_location_unavailable'
        else:
            with pytest.raises(KernelError) as refused:
                if entry == 'open':
                    Kernel.open_task(task)
                elif entry == 'create':
                    Kernel.create_task(kernel.storage.path, replace(spec, run_id='replacement'), requirements=requirements)
                else:
                    kernel.admit_host('second-start', kind='run', command=request.command,
                        environment_carrier=carrier, preflight=manager.preflight)
            assert refused.value.code == 'task_location_unavailable'
    assert reads
    assert not destination.parent.exists() and not carrier.exists()
    assert_original_only(kernel, workspace, spec, calls, before)
