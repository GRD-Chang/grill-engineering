"""Existing public Profile/Prompt commands use native facts and retained assemblers."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import agent_run_kernel
from agent_run import prompt_resources
from agent_run.managed_workspace import ManagedWorkspace
from agent_run.user_defaults import UserDefaultsStore
from agent_run_kernel import Commit, Delivery, DeliveryKind, Kernel, ParentIssue, Requirements, RunSpec, World
from agent_run_kernel.configuration import freeze_creation
from agent_run_kernel.query_cli import main
from agent_run_kernel.operation_runner import DeliveryFacts
from support.core_operations import CoreOperations, ReadOnlyPorts


def _published_profile_task(git_repo: Path) -> Kernel:
    workspace = ManagedWorkspace.for_repository('example/project')
    root = workspace.ensure_path(str(git_repo))
    defaults = UserDefaultsStore()
    defaults.configure(language='en', profile={'development_model': 'original-development'})
    base = Commit(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                  subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=root, text=True).strip())
    delivery = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'parent-v1', base,
                        'refs/heads/parent', 'refs/heads/main')
    spec = freeze_creation(RunSpec('profile-public', workspace.repository, 1, 'parent-v1', 'graph-v1',
        (delivery,), repository_root=str(workspace.repository_root)), defaults)
    return Kernel.create_task(workspace.state_root / spec.run_id / 'kernel.sqlite3', spec,
        requirements=Requirements(ParentIssue(1, 'Profile Parent', 'Frozen original requirement'), ()))


@pytest.mark.parametrize('command', ['configure', 'config', 'profile'])
def test_public_run_profile_revision_changes_only_future_threads(git_repo, command, monkeypatch, capsys):
    from agent_run_kernel import host_adapter, lifecycle_cli

    kernel = _published_profile_task(git_repo)
    execution = kernel.claim_executor('profile-test-owner')
    source = kernel.source_status()
    original_delivery = kernel.delivery('parent:1')
    ports = ReadOnlyPorts(Requirements(ParentIssue(1, 'Profile Parent', 'Frozen original requirement'), ()),
        original_delivery.source_ref, original_delivery.target_ref, DeliveryFacts(original_delivery.base, None))
    operations = CoreOperations(kernel, execution, lambda binding: ports)
    original = operations.once().call
    assert original is not None and original.profile_revision == 1
    assert kernel.dispatch_agent(original)
    kernel.bind_thread(original, 'original-backend')
    process = {'pid': 21, 'pgid': 21, 'start_token': 'profile-original',
               'host_pid': 10, 'host_start_token': 'profile-host'}
    kernel.agent_started(original, process)
    before = kernel.status('parent:1')
    defaults_before = UserDefaultsStore().path.read_bytes()
    with kernel.storage.transaction(write=False) as tx:
        thread = dict(tx.one('SELECT * FROM threads WHERE id=?', (original.thread_id,)))

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Profile configuration launched or controlled a Host')

    monkeypatch.setattr(host_adapter, 'SystemdHost', forbidden)
    monkeypatch.setattr(lifecycle_cli, 'submit', forbidden)
    monkeypatch.setattr(Kernel, 'admit_host', forbidden)
    monkeypatch.setattr(Kernel, 'control_host', forbidden)
    selector = '1' if command == 'configure' else 'profile-public'
    location = ['--state-dir', str(kernel.storage.path.parent.parent)] if command == 'profile' else []
    assert main([command, selector, '--repo', 'example/project', *location,
                 '--dev-model', 'future-development', '--dev-effort', 'high', '--json']) == 0
    configured = json.loads(capsys.readouterr().out)
    assert configured['result'] == 'configured' and configured['run_id'] == 'profile-public'
    assert configured['profile_revision'] == 2
    assert configured['profiles']['development']['model'] == 'future-development'
    assert configured['profiles']['development']['reasoning_effort'] == 'high'
    assert UserDefaultsStore().path.read_bytes() == defaults_before
    after = kernel.status('parent:1')
    assert after['phase'] == before['phase'] and after['budgets'] == before['budgets']
    with kernel.storage.transaction(write=False) as tx:
        assert dict(tx.one('SELECT * FROM threads WHERE id=?', (original.thread_id,))) == thread
        assert tx.one('SELECT count(*) FROM threads')[0] == 1
        assert tx.one('SELECT count(*) FROM hosts')[0] == 0
        assert tx.one("SELECT count(*) FROM actions WHERE kind='configure_profiles'")[0] == 1

    # Explicit synthetic process observations exercise the real recovery and
    # Thread allocation authority; no Worker subprocess is claimed here.
    kernel.agent_exited(original, process, 1)
    continuing = operations.once().call
    assert continuing is not None and continuing.thread_id == original.thread_id
    assert continuing.profile_revision == 1 and continuing.profile == original.profile
    assert kernel.dispatch_agent(continuing)
    successor_process = {**process, 'pid': 22, 'pgid': 22, 'start_token': 'profile-continuing'}
    kernel.agent_started(continuing, successor_process)
    kernel.agent_exited(continuing, successor_process, 1)
    assert operations.once().kind == 'paused'
    assert kernel.control('resume', 'replace-profile-thread', subject='parent:1', new_thread=True) == 'applied'
    changed = operations.once().call
    assert changed is not None and changed.thread_id != original.thread_id
    assert changed.profile_revision == 2 and changed.profile.model == 'future-development'
    assert changed.attempt_id == original.attempt_id


@pytest.mark.parametrize('case', ['empty', 'conflict', 'invalid-effort', 'unbound', 'wrong-repository', 'custom-state', 'zero-parent'])
def test_public_run_profile_rejects_invalid_changes_and_unpublished_snapshots_without_saving(
    git_repo, tmp_path, capsys, case,
):
    from tests.test_kernel_query_cli import _snapshot
    from agent_run.task_identity import TaskKey
    from agent_run_kernel.task_location import task_path
    import shutil

    kernel = _published_profile_task(git_repo)
    repository = 'wrong/project' if case == 'wrong-repository' else 'example/project'
    state = kernel.storage.path.parent.parent
    if case == 'unbound':
        workspace = ManagedWorkspace.for_repository('example/project')
        task_path(TaskKey(workspace.repository_root, 'example/project', 1)).unlink()
    elif case == 'custom-state':
        state = tmp_path / 'diagnostic-copy'
        state.mkdir()
        shutil.copy2(kernel.storage.path, state / 'kernel.sqlite3')
    options = {'empty': [], 'conflict': ['--publication-model', 'independent', '--publication-from-development'],
               'invalid-effort': ['--dev-effort', 'not-a-real-effort']}.get(case, ['--dev-model', 'must-not-apply'])
    before = _snapshot(tmp_path)
    selector = '0' if case == 'zero-parent' else 'profile-public'
    assert main(['configure', selector, '--repo', repository, '--state-dir', str(state),
                 *options, '--json']) == 2
    output = json.loads(capsys.readouterr().out)
    assert output['result'] == 'error'
    if case == 'empty':
        assert 'preset or an explicit role override' in output['diagnostics'][0]['message']
    assert _snapshot(tmp_path) == before
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT profile_revision FROM runs')[0] == 1
        assert tx.one('SELECT count(*) FROM profile_revisions')[0] == 1
        assert tx.one('SELECT count(*) FROM actions')[0] == 0
        assert tx.one('SELECT count(*) FROM hosts')[0] == 0


def _native(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(agent_run_kernel.__file__).resolve().parents[1])
    return subprocess.run([sys.executable, '-m', 'agent_run_kernel', *arguments], env=environment,
                          text=True, capture_output=True, timeout=15)


def test_public_native_prompts_preserve_personal_methods_and_preview_original_frozen_resources(tmp_path):
    from agent_run.paths import app_data_root

    UserDefaultsStore().configure(language='en')
    directory = prompt_resources.personal_method_directory('en')
    empty = _native('prompts', 'diff', '--json')
    assert empty.returncode == 0, empty.stdout + empty.stderr
    assert json.loads(empty.stdout)['result'] == 'prompts_diff'
    assert not directory.exists()
    initialized = _native('prompts', 'init', '--json')
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    assert set(json.loads(initialized.stdout)['created']) == {'development.md', 'repair.md', 'acceptance.md', 'publishing.md'}
    method = directory / 'development.md'
    method.write_text('Original personal development instruction.\n')
    (directory / 'acceptance.md').unlink()
    repeated = _native('prompts', 'init', '--json')
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(repeated.stdout)['created'] == ['acceptance.md']
    assert method.read_text() == 'Original personal development instruction.\n'
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    diff = _native('prompts', 'diff', '--json')
    assert diff.returncode == 0, diff.stdout + diff.stderr
    assert '+Original personal development instruction.' in diff.stdout
    request = {'language': 'en', 'task_issue_url': 'https://example.invalid/2',
               '_prompt_resources': prompt_resources.resolve_resources('en')}
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(request))
    frozen = request['_prompt_resources']['methods/development']
    method.write_text('New personal instruction must not replace the request snapshot.\n')
    preview = _native('prompts', 'preview', '--request', str(path), '--role', 'development', '--json')
    assert preview.returncode == 0, preview.stdout + preview.stderr
    value = json.loads(preview.stdout)
    assert frozen in value['prompt']
    assert 'New personal instruction must not replace' not in value['prompt']
    assert set(value['sources'].values()) == {'request_snapshot'}
    assert not app_data_root().exists()
    # diff made no changes; only this test's explicit edit changed development.
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == before[p.name]
               for p in directory.iterdir() if p.name != 'development.md')


def test_public_prompt_preview_errors_preserve_request_and_personal_files(tmp_path, capsys):
    from agent_run.paths import app_data_root

    UserDefaultsStore().configure(language='en')
    path = tmp_path / 'invalid-request.json'
    path.write_text(json.dumps({'language': 'en', '_prompt_resources': {}}))
    before = path.read_bytes()
    assert main(['prompts', 'preview', '--request', str(path), '--role', 'development', '--json']) == 2
    output = json.loads(capsys.readouterr().out)
    assert output['result'] == 'error' and 'snapshot' in output['diagnostics'][0]['message']
    assert path.read_bytes() == before
    assert not prompt_resources.personal_method_directory('en').exists()
    assert not app_data_root().exists()
    help_result = _native('prompts', 'preview', '--help')
    assert help_result.returncode == 0 and '--continuation' in help_result.stdout
