"""Real bounded SQLite faults, not fabricated database/business records."""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from agent_run.core.kernel import Kernel
from agent_run.cli.main import main
from agent_run.adapters.git_objects import LocalGit
from agent_run.errors import KernelError
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.runtime.execution import execute
from support.core_operations import CoreOperations, native_agent_fixture
from support.offline import fixture_inputs
from support.workspace import prepare_workspace
from support.sqlite_faults import sqlite_fault




def logical_state(database: Path):
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        return tuple(connection.iterdump())


def allocated_work(tmp_path):
    base = Commit('base', 'tree')
    job = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'original', base,
                   'refs/heads/source', 'refs/heads/main', checkout=str(tmp_path))
    kernel, ports = native_agent_fixture(tmp_path / 'kernel.sqlite3',
        RunSpec('sqlite-fault-run', 'example/project', 1, 'original', 'graph', (job,)))
    execution = kernel.claim_executor('sqlite-fault-owner')
    original = CoreOperations(kernel, execution, lambda _: ports).once().call
    assert original is not None
    return kernel, ports, execution, original


@pytest.mark.parametrize('fault,sqlite_code,diagnostic', [
    ('full', sqlite3.SQLITE_FULL, 'storage_limit'),
    ('commit-interrupt', sqlite3.SQLITE_INTERRUPT, 'transaction_aborted'),
])
def test_sqlite_fault_rolls_back_the_whole_resume_and_first_reopen_preserves_work(
    tmp_path, monkeypatch, fault, sqlite_code, diagnostic,
):
    kernel, ports, execution, original = allocated_work(tmp_path)
    # This driver never calls execute/spawn; it owns and settles that exact
    # allocated call before asking Stop to revoke further work.
    kernel.agent_unavailable(original, 'preparation_failed', 'No process was created', before_dispatch=True)
    kernel.control('stop', 'original-stop', subject=original.subject)
    before = logical_state(kernel.storage.path)
    budget = kernel.status(original.subject)['budgets']
    arguments = dict(subject=original.subject, message='Continue the original work. ' + 'x' * 8000)
    with sqlite_fault(monkeypatch, kernel.storage.path, fault) as failures:
        with pytest.raises(KernelError) as refused:
            kernel.control('resume', 'faulted-resume', **arguments)
        assert refused.value.code == diagnostic
        assert refused.value.__cause__ in failures
        assert refused.value.__cause__.sqlite_errorcode == sqlite_code
    assert logical_state(kernel.storage.path) == before
    reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    assert reopened.status(original.subject)['budgets'] == budget
    assert reopened.control('resume', 'faulted-resume', **arguments) == 'applied'
    assert reopened.control('resume', 'faulted-resume', **arguments) == 'applied'
    next_call = CoreOperations(reopened, execution, lambda _: ports).once().call
    assert next_call is not None and next_call.attempt_id == original.attempt_id
    assert next_call.thread_id == original.thread_id
    assert reopened.status(original.subject)['budgets'] == budget
    assert sum(event['kind'] == 'control_applied' and event['identity'] == 'faulted-resume'
               for event in reopened.history(original.subject)) == 1
    assert sum(item.stat().st_size for item in tmp_path.iterdir() if item.is_file()) < 2 * 1024 * 1024


@pytest.mark.parametrize('fault,sqlite_code,diagnostic', [
    ('full', sqlite3.SQLITE_FULL, 'storage_limit'),
    ('commit-interrupt', sqlite3.SQLITE_INTERRUPT, 'transaction_aborted'),
])
def test_real_worker_raw_save_fault_reaps_the_process_and_reopen_resumes_original_attempt(
    tmp_path, monkeypatch, fault, sqlite_code, diagnostic,
):
    kernel, ports, execution, original = allocated_work(tmp_path)
    starts = tmp_path / 'worker-starts'
    raw = json.dumps({'result_kind': 'development', 'summary': 'Delivered. ' + 'x' * 32768,
                      'human_blockers': None}).encode()
    arguments = [sys.executable, '-I', '-c',
        f"from pathlib import Path;import os,sys;Path({str(starts)!r}).open('a').write(str(os.getpid())+'\\n');sys.stdout.buffer.write({raw!r})"]
    receive = kernel.receive
    observed = []

    def save(call, body):
        with sqlite_fault(monkeypatch, kernel.storage.path, fault) as failures:
            try:
                return receive(call, body)
            finally:
                observed.extend(failures)

    # This is the local process adapter's supported seam, not a replacement
    # systemd/PID namespace Host or a claim of real Codex/GitHub E2E.
    unrelated = subprocess.Popen([sys.executable, '-I', '-c', 'import sys;sys.stdin.read(1)'], stdin=subprocess.PIPE)
    try:
        monkeypatch.setattr(kernel, 'receive', save)
        with pytest.raises(KernelError) as failed:
            execute(kernel, original, arguments=arguments, cwd=tmp_path, prompt='', environment={'PATH': os.defpath}, timeout=5)
        assert failed.value.code == diagnostic
        assert failed.value.__cause__ in observed and failed.value.__cause__.sqlite_errorcode == sqlite_code
        pid, = map(int, starts.read_text().splitlines())
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        assert unrelated.poll() is None
        reopened = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
        status = reopened.status(original.subject)
        assert status['pending_attempt'] == original.attempt_id and not status['pending_return']
        assert status['phase'] == 'development' and status['gates'] == ['execution_failed']
        assert status['budgets']['requirements']['developments_used'] == 1
        assert reopened.worker_settled(original) and reopened.operation_state().active == ()
        with reopened.storage.transaction(write=False) as tx:
            assert tx.one('SELECT raw_id FROM invocation_outputs WHERE invocation=?', (original.invocation_id,))[0] is None
        assert reopened.control('resume', 'first-reopen-resume', subject=original.subject) == 'applied'
        continued = CoreOperations(reopened, execution, lambda _: ports).once().call
        assert continued is not None and continued.attempt_id == original.attempt_id and continued.thread_id == original.thread_id
        assert continued.invocation_id != original.invocation_id
        result = execute(reopened, continued, arguments=arguments, cwd=tmp_path, prompt='', environment={'PATH': os.defpath}, timeout=5)
        assert result is not None and result.raw == raw and result.returncode == 0
        assert reopened.status(original.subject)['pending_return']
        assert reopened.status(original.subject)['budgets']['requirements']['developments_used'] == 1
        assert len(starts.read_text().splitlines()) == 2 and unrelated.poll() is None
    finally:
        try:
            unrelated.communicate(input=b'1', timeout=5)
        finally:
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=3)
            if unrelated.stdin is not None:
                unrelated.stdin.close()
        assert unrelated.returncode == 0


def test_unclassified_sqlite_errors_and_interruptions_keep_their_original_identity(tmp_path):
    kernel, _, _, original = allocated_work(tmp_path)
    for error in (sqlite3.OperationalError('unclassified failure'), KeyboardInterrupt(), SystemExit(7)):
        before = logical_state(kernel.storage.path)
        with closing(sqlite3.connect(kernel.storage.path)) as connection:
            sequence = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='save_admissions'").fetchone()
            original_sequence = sequence[0] if sequence is not None else 0
        with pytest.raises(type(error)) as caught:
            with kernel.storage.transaction():
                raise error
        assert caught.value is error
        after = logical_state(kernel.storage.path)
        technical = 'INSERT INTO "sqlite_sequence" VALUES(\'save_admissions\','
        assert tuple(row for row in after if not row.startswith(technical)) == tuple(
            row for row in before if not row.startswith(technical))
        with closing(sqlite3.connect(kernel.storage.path)) as connection:
            assert connection.execute("SELECT seq FROM sqlite_sequence WHERE name='save_admissions'").fetchone()[0] == original_sequence + 1
            assert connection.execute('SELECT count(*) FROM save_admissions').fetchone()[0] == 0


def test_public_stop_reports_real_commit_interruption_without_partial_action(
    git_repo, tmp_path, monkeypatch, capsys,
):
    workspace = prepare_workspace(git_repo)
    base = LocalGit(workspace.repository_root).commit('HEAD')
    source = Requirements(ParentIssue(1, 'Parent', 'Preserve this original requirement', ()), ())
    delivery = Delivery('parent:1', DeliveryKind.PARENT_ONLY, 1, 'unobserved', base,
        'refs/heads/fault-delivery', 'refs/heads/main', checkout=str(workspace.repository_root))
    kernel = Kernel.create_task(workspace.state_root / 'kernel.sqlite3',
        RunSpec('sqlite-fault-run', 'example/project', 1, 'unobserved', 'graph', (delivery,),
            inputs=fixture_inputs(), language='en', repository_root=str(workspace.repository_root)), requirements=source)
    before = logical_state(kernel.storage.path)
    arguments = ['stop', 'sqlite-fault-run', '--repo', 'example/project', '--json']
    with sqlite_fault(monkeypatch, kernel.storage.path, 'commit-interrupt') as failures:
        assert main(arguments) == 2
        response = json.loads(capsys.readouterr().out)
        assert response['diagnostics'][0]['code'] == 'transaction_aborted'
        assert 'SQLITE_INTERRUPT' in response['diagnostics'][0]['message']
        assert any(error.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT for error in failures)
    assert logical_state(kernel.storage.path) == before
    assert main(arguments) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['run_id'] == 'sqlite-fault-run' and receipt['action'] == 'stop'
    assert kernel.source_status()['stopped']
    assert sum(event['kind'] == 'control_applied' and event['identity'] == receipt['action_id']
               for event in kernel.history(delivery.key)) == 1
