"""Original physical-host raw returns pass one explicit migration authority seam."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from agent_run_kernel import Kernel
from agent_run_kernel.effects import LocalGit
from agent_run_kernel.offline import Scenario
from agent_run_kernel.model import RunSpec, Policy, CreationInputs
from agent_run_kernel.migration import capture_v6
from agent_run_kernel.migration_records import V6RecordReader
from support.v6_returned_source import returned_managed_source, continue_original_returned
from support.returned_remote import ReturnedRemote
from support.migration_operations import configure_source, admit_final
from test_kernel_migration_completed_history import _assert_original_records
from test_kernel_migration_original_source import _git_bytes


@pytest.mark.parametrize('role', ['development', 'review', 'publication'])
def test_original_returned_role_transfers_exact_pending_work_without_replay(
    tmp_path: Path, git_repo: Path, role: str, monkeypatch: pytest.MonkeyPatch, *, rollback_first: bool = False, foreign_ref: bool = False, inverse_damage: str | None = None, review_findings: bool = False,
) -> None:
    old_git = tmp_path / 'oracle-repository'
    shutil.copytree(git_repo, old_git, symlinks=True)
    with monkeypatch.context() as old_environment:
        old_environment.setenv('XDG_DATA_HOME', str(tmp_path / 'oracle-data'))
        old_managed, old_paths = returned_managed_source(tmp_path / 'original-oracle', old_git, role, review_findings=review_findings)
        original_outcome = continue_original_returned(old_managed, old_paths, role)
    managed, paths = returned_managed_source(tmp_path, git_repo, role, review_findings=review_findings)
    report = json.loads(paths['report'].read_text())
    run_id = report['run_id']
    with closing(sqlite3.connect(paths['database'])) as db:
        db.row_factory = sqlite3.Row
        host = db.execute('SELECT * FROM control_executors').fetchone()
        assert host['status'] == 'exited' and host['exited_at'] and host['pid'] > 0
        assert not Path('/proc/' + str(host['pid'])).exists()
        current = db.execute('SELECT * FROM work_job_attempt_roots').fetchone()
        old_development = db.execute("SELECT attempt_id FROM work_attempts WHERE role='development'").fetchone()[0]
        invocation = db.execute('SELECT * FROM work_invocations WHERE attempt_id=?',
                                (current['pending_attempt_id'],)).fetchone()
        assert invocation['status'] == 'completed' and invocation['ended_at']
        assert invocation['raw_result_evidence_id'] and invocation['reported_thread_id']
        assert invocation['attempt_version_id'] != db.execute(
            'SELECT version_id FROM work_attempt_current WHERE attempt_id=?',
            (current['pending_attempt_id'],)).fetchone()[0]
        budget = db.execute('SELECT * FROM work_job_review_budget').fetchone()
        assert (budget['value_development_attempts'], budget['value_reviewer_invocations']) == (1, int(role == 'publication'))
    original, git_before = paths['database'].read_bytes(), _git_bytes(managed.repository_root)
    checkout = managed.state_root / 'worktrees' / run_id / 'ticket-2'
    def git(*args: str) -> str:
        return subprocess.run(['git', '-C', str(checkout), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    expected_oid = git('rev-parse', 'HEAD')
    assert git('symbolic-ref', 'HEAD') == 'refs/heads/' + report['ticket_branch']
    head_path = Path(git('rev-parse', '--path-format=absolute', '--git-path', 'HEAD'))
    log_path = Path(git('rev-parse', '--path-format=absolute', '--git-path', 'logs/HEAD'))
    evidence = tmp_path / 'returned-evidence'
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
                   'PATH': str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'],
                   'V6_CLOSE_SERVICE': str(tmp_path / 'close-service.json'),
                   'V6_CLOSE_CALLS': str(tmp_path / 'close-service-calls.jsonl'),
                   'V6_CLOSE_REAL_GIT': shutil.which('git')}
    observed = subprocess.run([sys.executable, '-m', 'agent_run_kernel.migration_cutover',
        '--repo', 'example/project', '--parent', '1', '--evidence-dir', str(evidence)],
        env=environment, text=True, capture_output=True, timeout=20)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    result = json.loads(observed.stdout)
    assert result['status'] == 'migrated'
    kernel = Kernel(Path(result['target']['database']))
    assert kernel.status('ticket:2')['pending_attempt'] == current['pending_attempt_id']
    assert kernel.status('ticket:2')['phase'] == {'development': 'development', 'review': 'review', 'publication': 'publish'}[role]
    with kernel.storage.transaction(write=False) as tx:
        assert not tx.rows('SELECT id FROM invocations')
        assert not tx.rows('SELECT id FROM effects')
        assert tuple(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject='ticket:2' AND purpose='requirements'")) == (1, int(role == 'publication'))
    assert (evidence / 'source.sqlite3').read_bytes() == original
    after_git = _git_bytes(managed.repository_root)
    changed = {name for name in git_before.keys() | after_git.keys() if git_before.get(name) != after_git.get(name)}
    permitted = {str(path.relative_to(managed.repository_root)) for path in (head_path, log_path)}
    assert str(head_path.relative_to(managed.repository_root)) in changed <= permitted
    assert git('rev-parse', 'HEAD') == expected_oid
    assert git('rev-parse', 'refs/heads/' + report['ticket_branch']) == expected_oid
    assert subprocess.run(['git', '-C', str(checkout), 'symbolic-ref', '-q', 'HEAD'], capture_output=True).returncode == 1
    _assert_original_records(capture_v6(evidence / 'source.sqlite3', tmp_path / 'record-copy.sqlite3'), V6RecordReader(kernel))
    if rollback_first:
        recovery = [sys.executable, '-m', 'agent_run_kernel.migration_cutover', '--repo', 'example/project',
                    '--parent', '1', '--evidence-dir', str(evidence)]
        before = paths['database'].read_bytes(), kernel.storage.path.read_bytes(), _git_bytes(managed.repository_root)
        resumed = subprocess.run(recovery + ['--resume'], env=environment, text=True, capture_output=True, timeout=20)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert json.loads(resumed.stdout)['nonce'] == result['nonce']
        assert (paths['database'].read_bytes(), kernel.storage.path.read_bytes(), _git_bytes(managed.repository_root)) == before
        if inverse_damage is not None:
            if inverse_damage == 'index':
                index = Path(git('rev-parse', '--path-format=absolute', '--git-path', 'index'))
                index.write_bytes(index.read_bytes() + b'foreign staged index bytes')
            elif inverse_damage == 'dirty-content':
                dirty = checkout / 'history-feature.txt'
                assert dirty.is_file()
                dirty.write_bytes(dirty.read_bytes() + b'foreign uncommitted work')
            else:
                assert inverse_damage == 'foreign-same-oid-head'
                git('update-ref', 'refs/heads/foreign-layout', expected_oid)
                git('symbolic-ref', 'HEAD', 'refs/heads/foreign-layout')
            retained = paths['database'].read_bytes(), kernel.storage.path.read_bytes(), _git_bytes(managed.repository_root)
            refused = subprocess.run(recovery + ['--rollback'], env=environment, text=True, capture_output=True, timeout=20)
            assert refused.returncode == 1, refused.stdout + refused.stderr
            assert json.loads(refused.stdout)['status'] == 'refused'
            assert (paths['database'].read_bytes(), kernel.storage.path.read_bytes(), _git_bytes(managed.repository_root)) == retained
            assert (checkout / 'history-feature.txt').read_bytes().startswith(b'implemented round 1')
            _assert_original_records(capture_v6(evidence / 'source.sqlite3', tmp_path / 'refused-record-copy.sqlite3'), V6RecordReader(kernel))
            return
        restored = subprocess.run(recovery + ['--rollback'], env=environment, text=True, capture_output=True, timeout=20)
        assert restored.returncode == 0, restored.stdout + restored.stderr
        assert json.loads(restored.stdout)['status'] == 'rolled_back' and kernel.storage.path.is_file()
        assert git('symbolic-ref', 'HEAD') == 'refs/heads/' + report['ticket_branch']
        assert git('rev-parse', 'HEAD') == expected_oid
        for name, digest in git_before.items():
            if name != str(log_path.relative_to(managed.repository_root)):
                assert _git_bytes(managed.repository_root)[name] == digest, name
        with closing(sqlite3.connect(paths['database'])) as restored_db:
            assert restored_db.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0] == 'sqlite-work-records-v6'
        restored_outcome = continue_original_returned(managed, paths, role)
        assert restored_outcome == original_outcome
        return
    port = ReturnedRemote.create(tmp_path / 'native-remote')
    port.git = LocalGit(managed.repository_root)
    port.local_root = checkout.parent
    external = json.loads(paths['github'].read_text())
    with closing(sqlite3.connect(port.database)) as remote:
        remote.execute('DELETE FROM refs')
        heads = {**external['delivery']['published_branches'], external['default_branch']: external['default_head_sha']}
        remote.executemany('INSERT INTO refs VALUES (?,?)', [('refs/heads/' + ref, oid) for ref, oid in heads.items()])
        remote.commit()
    class PendingScenario(Scenario):
        def agent(self, call):
            assert review_findings or call.role.value != 'development', 'original Development raw must not be replayed'
            assert review_findings or role == 'development' or call.role.value != 'review', 'original Reviewer raw must not be replayed'
            assert role != 'publication' or call.role.value != 'publication', 'original Publication raw must not be replayed'
            if review_findings and call.role.value == 'development':
                assert call.attempt_id not in {old_development, current['pending_attempt_id']}
                assert call.context['semantic_continuation'] is False
                with kernel.storage.transaction(write=False) as tx:
                    assert tuple(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject='ticket:2' AND purpose='requirements'")) == (2, 1)
            if call.role.value == 'publication' or review_findings and call.role.value == 'development':
                assert call.reported_thread == original_outcome['development_thread']
                assert call.mode == 'resume'
            return super().agent(call)
    with kernel.storage.transaction(write=False) as tx:
        frozen = tx.one('SELECT subject,material_id FROM run_inputs')
        inputs = json.loads(tx.get(frozen['material_id'], frozen['subject']))
        profile = tx.one('SELECT subject,material_id FROM profile_revisions WHERE id=1')
        profiles = json.loads(tx.get(profile['material_id'], profile['subject']))
    scenario = PendingScenario.__new__(PendingScenario)
    scenario.script, scenario.kernel, scenario.port = None, kernel, port
    scenario.delivery = kernel.delivery('ticket:2')
    scenario.execution = kernel.claim_executor('original-returned-native-executor')
    status = kernel.status('ticket:2')
    scenario.spec = RunSpec(run_id, 'example/project', 1, status['parent_revision'], status['graph_revision'],
        (scenario.delivery,), Policy(), inputs=CreationInputs(profiles, inputs['prompt_resources'],
        inputs['worker_identity'], inputs['runner_identity'], inputs['notifications'], inputs['provenance']))
    scenario.initial = port.git.commit('refs/heads/main')
    scenario.reviews, scenario.checks = ['pass'], ['none']
    scenario.review_count = scenario.development_count = scenario.publication_count = 0
    scenario.heads, scenario.role_calls, scenario.checkpoints, scenario.reviewed_heads = [], [], [], []
    configure_source(scenario, external)
    scenario.edits = [{'history-feature.txt': 'implemented round 2\n'}] if review_findings else None
    cleanup_effects = []
    original_execute = port.execute
    foreign_oid = None
    def execute(effect, authorize=None):
        nonlocal foreign_oid
        if effect.operation == 'cleanup_local_ref':
            cleanup_effects.append(effect)
            assert effect.before_sha == effect.payload['expected_head'] == expected_oid
            assert effect.source_ref == 'refs/heads/' + report['ticket_branch']
            if foreign_ref:
                prior = port.git.commit(expected_oid)
                foreign_oid = port.git.make_commit(prior.tree, [prior.sha], 'foreign private fixture commit\n').sha
                port.git.run('update-ref', effect.source_ref, foreign_oid, expected_oid)
        return original_execute(effect, authorize)
    port.execute = execute
    boundary = scenario.run()
    # Business completion survives a refused cleanup of a foreign ref. The
    # runner reports the remaining cleanup separately instead of claiming it
    # succeeded; the original ownership assertions below remain unchanged.
    assert kernel.status('ticket:2')['phase'] == 'complete'
    assert boundary.kind == ('paused' if foreign_ref else 'complete'), boundary
    assert len(cleanup_effects) == 1
    if foreign_ref:
        assert foreign_oid is not None
        assert port.git.commit('refs/heads/' + report['ticket_branch']).sha == foreign_oid
        cleanup = kernel.status('ticket:2')['resource_cleanup']
        assert cleanup['stage'] == 1 and cleanup['state'] == 'pending' and cleanup['outcome'] == 'third'
        assert 'refs/heads/' + report['ticket_branch'] in port.remote_refs()
        assert not any(write['operation'] == 'cleanup_remote_ref' for write in port.writes())
        _assert_original_records(capture_v6(evidence / 'source.sqlite3', tmp_path / 'foreign-record-copy.sqlite3'), V6RecordReader(kernel))
        return
    port.execute = original_execute
    assert [call['role'] for call in scenario.role_calls] == original_outcome['calls']
    assert kernel.status('ticket:2')['resource_cleanup']['state'] == 'complete'
    assert not checkout.exists()
    with kernel.storage.transaction(write=False) as tx:
        assert list(tx.one("SELECT developments_used,reviews_used FROM budgets WHERE subject='ticket:2' AND purpose='requirements'")) == original_outcome['budget']
        completed = tx.one("SELECT tree FROM completions WHERE subject='ticket:2'")
        assert completed['tree'] == original_outcome['integrated_tree']
        assert tx.one("SELECT candidate_tree FROM subjects WHERE id='ticket:2'")[0] == original_outcome['candidate_tree']
    assert port.remote_refs().get('refs/heads/' + report['ticket_branch']) is None
    assert all(write['operation'] != 'workspace' for write in port.writes())
    run_ref = 'refs/heads/' + report['run_branch']
    run_head, default = port.remote_commit(run_ref), port.remote_commit('refs/heads/main')
    assert run_head is not None and default is not None
    original_run_oid = port.git.commit(run_ref).sha
    assert original_run_oid == default.sha and run_head.sha != original_run_oid
    final = admit_final(scenario)
    port.local_root = managed.root / 'worktrees' / run_id
    final_scenario = Scenario.__new__(Scenario)
    final_scenario.script, final_scenario.kernel, final_scenario.port = None, kernel, port
    final_scenario.execution, final_scenario.delivery = scenario.execution, kernel.delivery(final)
    final_scenario.spec = replace(scenario.spec, deliveries=(final_scenario.delivery,))
    final_scenario.initial = default
    final_scenario.reviews, final_scenario.checks = ['pass'], ['none']
    final_scenario.review_count = final_scenario.development_count = final_scenario.publication_count = 0
    final_scenario.heads, final_scenario.role_calls, final_scenario.checkpoints, final_scenario.reviewed_heads = [], [], [], []
    configure_source(final_scenario, external)
    final_scenario.edits = None
    final_ref_cleanup = []
    def execute_final(effect, authorize=None):
        if effect.operation == 'cleanup_local_ref':
            final_ref_cleanup.append(effect)
            assert effect.source_ref == run_ref
            assert effect.before_sha == effect.payload['expected_head'] == original_run_oid
            assert port.remote_refs()[run_ref] == run_head.sha
        return original_execute(effect, authorize)
    port.execute = execute_final
    assert final_scenario.run(approve=True).kind == 'complete'
    assert [call['role'] for call in final_scenario.role_calls] == ['review', 'publication']
    assert kernel.status(final)['resource_cleanup']['state'] == 'complete'
    assert len(final_ref_cleanup) == 1 and run_ref not in port.remote_refs()
    assert port.git.run('for-each-ref', '--format=%(refname)', run_ref).stdout == ''
    assert not Path(final_scenario.delivery.checkout).exists()
    _assert_original_records(capture_v6(evidence / 'source.sqlite3', tmp_path / 'final-record-copy.sqlite3'), V6RecordReader(kernel))


def test_original_development_raw_returns_exact_unadvanced_layout_and_old_continuation(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_original_returned_role_transfers_exact_pending_work_without_replay(
        tmp_path, git_repo, 'development', monkeypatch, rollback_first=True)


def test_original_returned_cleanup_refuses_foreign_original_ref(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_original_returned_role_transfers_exact_pending_work_without_replay(
        tmp_path, git_repo, 'development', monkeypatch, foreign_ref=True)


@pytest.mark.parametrize('role', ['review', 'publication'])
def test_original_returned_raw_restores_old_role_budget_and_thread_on_inverse(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    test_original_returned_role_transfers_exact_pending_work_without_replay(
        tmp_path, git_repo, role, monkeypatch, rollback_first=True)


@pytest.mark.parametrize('boundary', ['fenced', 'detached', 'published', 'restored', 'withdrawn', 'released'])
def test_original_returned_layout_crash_reentry_keeps_one_authority(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    # Each point is an actual child exit after a different durable boundary.
    # The source itself is always produced by the unchanged physical runtime.
    import selectors
    import signal
    import traceback
    from agent_run.task_identity import TaskKey
    from agent_run_kernel import migration_cli, migration_cutover, migration_layout
    from agent_run_kernel.task_location import read_task, task_path

    managed, paths = returned_managed_source(tmp_path, git_repo, 'development')
    report = json.loads(paths['report'].read_text())
    checkout = managed.state_root / 'worktrees' / report['run_id'] / 'ticket-2'
    git = LocalGit(managed.repository_root)
    before_git = _git_bytes(managed.repository_root)
    log = Path(git.run('rev-parse', '--path-format=absolute', '--git-path', 'logs/HEAD', cwd=checkout).stdout.strip())
    evidence = tmp_path / 'crash-evidence'
    arguments = ['--repo', 'example/project', '--parent', '1', '--evidence-dir', str(evidence)]
    real_git = shutil.which('git')
    assert real_git is not None
    monkeypatch.setenv('PATH', str(tmp_path / 'close-service-bin') + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('V6_CLOSE_SERVICE', str(tmp_path / 'close-service.json'))
    monkeypatch.setenv('V6_CLOSE_CALLS', str(tmp_path / 'close-service-calls.jsonl'))
    monkeypatch.setenv('V6_CLOSE_REAL_GIT', real_git)
    inverse = boundary in {'restored', 'withdrawn', 'released'}
    if inverse:
        assert migration_cli.main(arguments) == 0
    pid = os.fork()
    if pid == 0:
        def exit_after(function, *, release_only=False):
            def wrapped(*args, **kwargs):
                result = function(*args, **kwargs)
                if not release_only or kwargs.get('release'):
                    os._exit(81)
                return result
            return wrapped
        if boundary in {'fenced', 'released'}:
            migration_cutover._retire_original = exit_after(
                migration_cutover._retire_original, release_only=boundary == 'released')
        elif boundary == 'detached':
            migration_layout.detach_layout = exit_after(migration_layout.detach_layout)
        elif boundary == 'published':
            migration_cutover.bind_task = exit_after(migration_cutover.bind_task)
        elif boundary == 'restored':
            migration_layout.restore_layout = exit_after(migration_layout.restore_layout)
        else:
            migration_cutover._withdraw_original_pointer = exit_after(migration_cutover._withdraw_original_pointer)
        try:
            migration_cli.main(arguments + (['--rollback'] if inverse else []))
        except BaseException:
            (tmp_path / 'crash-error.txt').write_text(traceback.format_exc())
        os._exit(89)
    descriptor, ready = os.pidfd_open(pid), False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            ready = bool(selector.select(20))
        assert ready, 'child did not exit at the selected migration boundary'
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(descriptor)
        if not ready:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    error = tmp_path / 'crash-error.txt'
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 81, error.read_text() if error.exists() else status
    task = TaskKey(managed.repository_root, 'example/project', 1)
    target = managed.state_root / report['run_id'] / 'kernel.sqlite3'
    kernel = Kernel(target)
    pointer = read_task(task_path(task))
    assert pointer == ((report['run_id'], target) if boundary in {'published', 'restored'} else None)
    with closing(sqlite3.connect(paths['database'])) as source:
        protocol = source.execute("SELECT value FROM schema_meta WHERE key='protocol'").fetchone()[0]
    assert (protocol == 'sqlite-work-records-v6') == (boundary == 'released')
    if inverse:
        from agent_run_kernel.model import KernelError
        retained = paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)
        with pytest.raises(KernelError, match='legacy_requires_offline_migration'):
            kernel.claim_executor('cannot-race-an-incomplete-original-inverse')
        assert (paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root)) == retained
    attached = git.run('symbolic-ref', '-q', 'HEAD', cwd=checkout, check=False)
    assert (attached.returncode == 0) == (boundary in {'fenced', 'restored', 'withdrawn', 'released'})
    if attached.returncode == 0:
        assert attached.stdout.strip() == 'refs/heads/' + report['ticket_branch']
    _assert_original_records(capture_v6(evidence / 'source.sqlite3', tmp_path / 'crash-record-copy.sqlite3'), V6RecordReader(kernel))
    # Wrong operation identity cannot change either database, layout or pointer.
    operation = evidence / 'operation.json'
    operation_bytes = operation.read_bytes()
    wrong = json.loads(operation_bytes)
    wrong['nonce'] = 'f' * 32
    operation.write_text(json.dumps(wrong))
    before = paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root), pointer
    assert migration_cli.main(arguments + (['--rollback'] if inverse else ['--resume'])) == 1
    assert (paths['database'].read_bytes(), target.read_bytes(), _git_bytes(managed.repository_root), read_task(task_path(task))) == before
    operation.write_bytes(operation_bytes)
    if not inverse:
        assert migration_cli.main(arguments + ['--resume']) == 0
        assert read_task(task_path(task)) == (report['run_id'], target)
        assert git.run('symbolic-ref', '-q', 'HEAD', cwd=checkout, check=False).returncode == 1
    assert migration_cli.main(arguments + ['--rollback']) == 0
    assert read_task(task_path(task)) is None
    assert git.run('symbolic-ref', 'HEAD', cwd=checkout).stdout.strip() == 'refs/heads/' + report['ticket_branch']
    final_git = _git_bytes(managed.repository_root)
    for name, digest in before_git.items():
        if name != str(log.relative_to(managed.repository_root)):
            assert final_git[name] == digest, name
    source_copy = capture_v6(evidence / 'source.sqlite3', tmp_path / 'restored-record-copy.sqlite3')
    _assert_original_records(source_copy, V6RecordReader(kernel))
    assert continue_original_returned(managed, paths, 'development')['calls'] == ['review', 'publication']


@pytest.mark.parametrize('damage', ['index', 'dirty-content', 'foreign-same-oid-head'])
def test_original_returned_inverse_keeps_drifted_layout_and_both_databases(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    test_original_returned_role_transfers_exact_pending_work_without_replay(
        tmp_path, git_repo, 'development', monkeypatch, rollback_first=True, inverse_damage=damage)


def test_original_returned_review_findings_consumes_once_then_allocates_new_development(
    tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_original_returned_role_transfers_exact_pending_work_without_replay(
        tmp_path, git_repo, 'review', monkeypatch, review_findings=True)
