"""One native Parent integration story plus cheap current-work resume rules.

The native story requires the unchanged Linux save clock and Worker facilities.
Its manager/GitHub/model inputs are controlled; Git, CLI, Host and Workers run.
"""
from contextlib import ExitStack, closing, contextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agent_run.core.attempt_continuation import load_continuation
from agent_run.core.kernel import Kernel
from agent_run.model import Commit, Delivery, DeliveryKind, RunSpec, wire_json
from agent_run.runtime.process_identity import _process_binding_status
from support.core_operations import CoreOperations, Ports, delivery_run, native_agent_fixture, scripted_return, source_operations
from agent_run.runtime.package_lease import default_runner_lock_path, runner_management_lease
from support.fixture_text import replace_once
from support.managed_abandon_transport import install_currentness
from support.managed_abandon_recovery import abandon_at_approval, final_approval_hold, report_abandon_failure
from support.offline import OfflinePort, review_result
from support.pending_control import committed_control, pending_public_control
from support.repository_fixture import create_kernel
from support.run_scoped_kernel import ScopedKernel
from support.systemd_manager import _CODEX, _approval_read_barrier, _control_environment, _git, _local_manager


MESSAGE = '保留原集成成果。\r\nResolve shared.txt using both recorded inputs.'
QUESTION = 'Choose the integration resolution.'


def _rows(kernel):
    with kernel.storage.transaction(write=False) as tx:
        run = kernel.storage.run_id
        tables = [row[0] for row in tx.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        result = {name: tuple(tuple(row) for row in tx.execute(f'SELECT * FROM {name} WHERE run_id=? ORDER BY rowid', (run,)))
                  for name in tables if 'run_id' in {row[1] for row in tx.execute(f'PRAGMA table_info({name})')}}
        result['runs'] = tuple(tx.one('SELECT * FROM runs WHERE id=?', (run,)))
        result['tasks'] = tuple(tx.one('SELECT * FROM tasks WHERE current_run=?', (run,)))
        return result


def _codex():
    classification = """schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
role = 'review' if 'checks' in schema['properties'] else 'publication' if 'commit_message' in schema['properties'] else 'development'
"""
    program = _CODEX[:_CODEX.index('if prompt.startswith')] + classification + _CODEX[_CODEX.index('thread = '):]
    program = replace_once(program, "'managed-entry-' + role)", "'managed-entry-' + role + '-' + os.urandom(16).hex())")
    control = """import socket
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
    channel.settimeout(35)
    channel.connect(chr(0) + os.environ['ENTRY_INTEGRATION_WORKER'][1:])
    channel.sendall(json.dumps({'role':role, 'thread':thread, 'checkout':str(Path.cwd()),
        'prompt':prompt, 'resumed':sys.argv[1:3] == ['exec', 'resume']}).encode())
    channel.shutdown(socket.SHUT_WR)
    assert channel.recv(1) == b'1'
"""
    program = replace_once(program, "content = 'Implementation", control + "\ncontent = 'Implementation")
    original = """if role == 'development':
    Path('managed-delivery.txt').write_text(content)
    result = {'result_kind': 'development', 'summary': 'Implemented the requested file.', 'human_blockers': None}
"""
    replacement = f"""if role == 'development':
    if Path.cwd().name == 'run-publication':
        assert subprocess.check_output(['git', 'ls-files', '-u'], text=True).strip()
        assert subprocess.check_output(['git', 'rev-parse', 'MERGE_HEAD'], text=True).strip()
        if {json.dumps(MESSAGE, ensure_ascii=False)!r} in prompt:
            assert Path('retained-evidence.txt').read_text() == 'Original integration investigation\\n'
            Path('shared.txt').write_text('Resolved ticket and external content\\n')
            result = {{'result_kind':'development', 'summary':'Resolved the original integration conflict.', 'human_blockers':None}}
        else:
            Path('retained-evidence.txt').write_text('Original integration investigation\\n')
            result = {{'result_kind':'human_blocker', 'summary':None, 'human_blockers':[{QUESTION!r}]}}
    else:
        Path('shared.txt').write_text('Ticket content\\n')
        Path('managed-delivery.txt').write_text(content)
        result = {{'result_kind':'development', 'summary':'Implemented the Ticket.', 'human_blockers':None}}
"""
    return replace_once(program, original, replacement)


def _hold_human_return(directory, monkeypatch):
    """Pause only after the real Core has committed the exact HumanBlocker."""
    directory.mkdir()
    hook = directory / 'sitecustomize.py'
    hook.write_text("""import os, sys
if sys.orig_argv[1:3] == ['-m', 'agent_run.runtime.executor']:
    import socket
    from agent_run.core.operation_runner import OperationRunner
    original = OperationRunner.run_once
    held = False
    def observed(self, *, agent_capacity=1):
        global held
        step = original(self, agent_capacity=agent_capacity)
        if not held and step.subject == 'final:1' and step.kind == 'paused' and step.reason == 'human_blocker':
            held = True
            address = os.environ['ENTRY_INTEGRATION_HUMAN']
        elif held and step.kind in {'progress', 'approval'} and step.call is None:
            address = os.environ['ENTRY_INTEGRATION_APPROVAL']
        else:
            return step
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(25)
            channel.connect(chr(0) + address[1:])
            channel.sendall(b'1'); channel.shutdown(socket.SHUT_WR)
            assert channel.recv(1) == b'1'
        return step
    OperationRunner.run_once = observed
""")
    with hook.open('a') as stream:
        stream.write(f"    sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
                     "    from pathlib import Path\n"
                     "    from support.host_close_trace import install\n"
                     f"    install(Path({str(directory / 'close-trace.jsonl')!r}))\n")
    monkeypatch.setenv('PYTHONPATH', str(directory) + os.pathsep + os.environ.get('PYTHONPATH', ''))


def test_public_resume_repairs_final_then_abandon_recovers_the_completed_ticket(git_repo, tmp_path, monkeypatch):
    (git_repo / 'shared.txt').write_text('Base content\n')
    _git(git_repo, 'add', 'shared.txt')
    _git(git_repo, 'commit', '-m', 'Integration conflict base')

    def blocked(kernel):
        with kernel.storage.transaction(write=False) as tx:
            return bool(tx.rows("SELECT subject FROM gates WHERE run_id=? AND subject='final:1' AND code='human_blocker'",
                (kernel.storage.run_id,))) and not tx.rows('SELECT id FROM leases WHERE run_id=?', (kernel.storage.run_id,))

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as workers, ExitStack() as barriers:
        workers.bind('')
        workers.listen(2)
        monkeypatch.setenv('ENTRY_INTEGRATION_WORKER', '@' + workers.getsockname()[1:].decode())
        _hold_human_return(tmp_path / 'host-return-hook', monkeypatch)
        environment, state = _control_environment(git_repo, tmp_path, monkeypatch, codex=_codex(), tickets=(2,))
        address, databases, reached, release = barriers.enter_context(_approval_read_barrier(boundary=blocked))
        install_currentness(tmp_path, state)
        approval_address, approval_runs, at_approval, release_approval = barriers.enter_context(_approval_read_barrier())
        environment['ENTRY_INTEGRATION_HUMAN'] = address
        environment['ENTRY_INTEGRATION_APPROVAL'] = approval_address
        with _local_manager(tmp_path / 'manager', environment, host_timeout=180) as (manager, serving, errors, children, trace, commands, output):
            deadline = time.monotonic() + 180
            environment['PATH'] = str(Path(manager.systemctl).parent) + os.pathsep + environment['PATH']

            def remaining():
                assert time.monotonic() < deadline, (errors, output)
                return deadline - time.monotonic()

            def public(*arguments):
                result = subprocess.run([sys.executable, '-m', 'agent_run', *arguments, '--repo', 'example/project', '--json'],
                    cwd=git_repo, env=environment, text=True, capture_output=True, timeout=min(20, remaining()))
                assert result.returncode == 0, (result.stdout, result.stderr)
                return json.loads(result.stdout)

            def next_worker():
                workers.settimeout(min(35, remaining()))
                channel, _ = workers.accept()
                channel.settimeout(min(20, remaining()))
                try:
                    data = bytearray()
                    while block := channel.recv(65536):
                        data.extend(block)
                        assert len(data) <= 1024 * 1024
                    return json.loads(data), channel
                except BaseException:
                    channel.close()
                    raise

            try:
                accepted = public('run', '1', '--no-notifications')
                assert accepted['result'] == 'accepted'
                command = commands[0]
                path = Path(command[command.index('--database') + 1])
                kernel = Kernel(path, run_id=accepted['run_id'])
                databases.append(kernel)
                approval_runs.append(kernel)
                original_host = kernel.host_request(accepted['action_id'])
                for role in ('development', 'review', 'publication'):
                    event, channel = next_worker()
                    with closing(channel):
                        assert event['role'] == role
                        if role == 'review':
                            assert Path(event['checkout']).name.startswith('review-')
                            assert (Path(event['checkout']) / 'shared.txt').read_text() == 'Ticket content\n'
                        else:
                            assert Path(event['checkout']).name == 'ticket-2'
                        if role == 'publication':
                            # External writer, confined to the test's Git repository.
                            (git_repo / 'shared.txt').write_text('External content\n')
                            _git(git_repo, 'add', 'shared.txt')
                            _git(git_repo, 'commit', '-m', 'Concurrent target change')
                            _git(git_repo, 'push', str(tmp_path / 'origin.git'), 'HEAD:refs/heads/main')
                            external_head = _git(git_repo, 'rev-parse', 'HEAD')
                        channel.sendall(b'1')
                first, channel = next_worker()
                with closing(channel):
                    assert first['role'] == 'development' and Path(first['checkout']).name == 'run-publication'
                    checkout = Path(first['checkout'])
                    assert _git(checkout, 'ls-files', '-u')
                    merge_head = _git(checkout, 'rev-parse', 'MERGE_HEAD')
                    channel.sendall(b'1')
                assert reached.wait(min(25, remaining())), (errors, output)
                assert serving.is_alive() and children[0].poll() is None
                reopened = Kernel(path, run_id=accepted['run_id'])
                assert reopened is not kernel and blocked(reopened)
                before = reopened.status('final:1')
                assert (before['phase'], before['purpose'], before['gates']) == ('development', 'integration', ['human_blocker'])
                budget = before['budgets']['integration']
                assert budget['developments_used'] == 1
                with reopened.storage.transaction(write=False) as tx:
                    attempt = dict(tx.one("SELECT * FROM attempts WHERE run_id=? AND id=(SELECT current_attempt FROM subjects WHERE run_id=? AND id='final:1')",
                        (accepted['run_id'], accepted['run_id'])))
                    prior = load_continuation(tx, attempt['id'])
                    assert prior.prior.dispatch_state == 'exited' and prior.prior.exit_code == 0
                    old_output = dict(tx.one('SELECT * FROM invocation_outputs WHERE run_id=? AND invocation=? AND ordinal=?',
                        (accepted['run_id'], prior.prior.invocation_id, prior.output_ordinal)))
                    assert json.loads(tx.get(prior.prior.raw_id, 'final:1'))['human_blockers'] == [QUESTION]
                    conflict = dict(tx.one("SELECT * FROM effects WHERE run_id=? AND subject='final:1' AND operation='integrate' AND receipt_id IS NOT NULL",
                        (accepted['run_id'],)))
                    receipt = tx.get(conflict['receipt_id'], 'final:1')
                    assert json.loads(receipt)['disposition'] == 'failure'
                    requirements = [tuple(row) for row in tx.all('SELECT * FROM requirement_versions WHERE run_id=?', (accepted['run_id'],))]
                binding = reopened.operation_binding()
                sibling_base = Commit(_git(git_repo, 'rev-parse', 'HEAD'), _git(git_repo, 'rev-parse', 'HEAD^{tree}'))
                sibling = create_kernel(path, RunSpec('sibling', binding.repository, 10001, 'unused', 'unused', (
                    Delivery('parent:10001', DeliveryKind.PARENT_ONLY, 10001, 'unused', sibling_base, 'refs/heads/sibling', 'refs/heads/main'),),
                    repository_root=str(binding.repository_root), language='en', inputs=binding.inputs))
                sibling.control('stop', 'preserve-sibling-gate', subject='parent:10001')
                sibling_rows = _rows(sibling)
                assert sibling.status('parent:10001')['gates']
                with pending_public_control('resume', accepted['run_id'], '--new-thread', '--message', MESSAGE,
                                            cwd=git_repo, environment=environment,
                                            timeout=min(20, remaining())) as (control, control_remaining):
                    action = committed_control(reopened, control, 'resume', timeout=control_remaining())
                    assert action['status'] == 'applied'
                    assert serving.is_alive() and children[0].poll() is None
                    assert reopened.host_request(accepted['action_id']) == original_host
                    assert reopened.status('final:1')['budgets'] == before['budgets']
                    assert _git(checkout, 'rev-parse', 'MERGE_HEAD') == merge_head
                    assert (checkout / 'retained-evidence.txt').read_text() == 'Original integration investigation\n'
                    assert _rows(sibling) == sibling_rows
                    release.set()
                    stdout, stderr = control.communicate(timeout=control_remaining())
                    assert control.returncode == 0, (stdout, stderr)
                    resumed = json.loads(stdout)
                    assert resumed['requested_action'] == 'resume'
                    assert resumed['result'] == 'accepted' and resumed['host'] == 'running'
                    assert resumed['action_id'] == action['id']
                    assert resumed['requested_action_applied'] and resumed['requested_action_succeeded']
                    assert resumed['request_outcome'] == 'resumed' and resumed['executor_started']
                continued, channel = next_worker()
                with closing(channel):
                    assert continued['role'] == 'development' and continued['checkout'] == first['checkout']
                    assert continued['thread'] != first['thread'] and not continued['resumed']
                    assert json.dumps(MESSAGE, ensure_ascii=False) in continued['prompt']
                    assert json.dumps(json.loads(receipt), ensure_ascii=False, indent=2, sort_keys=True) in continued['prompt']
                    with reopened.storage.transaction(write=False) as tx:
                        current = load_continuation(tx, attempt['id'])
                        assert current.thread_id != prior.thread_id
                        continued_thread = current.thread_id
                        invocation = tx.one('SELECT * FROM invocations WHERE run_id=? AND id=?',
                            (accepted['run_id'], current.prior.invocation_id))
                        assert invocation['id'] != prior.prior.invocation_id
                        assert (invocation['predecessor'], invocation['recovery_kind'], invocation['action_id']) == (
                            prior.prior.invocation_id, 'explicit', resumed['action_id'])
                        assert tx.one('SELECT count(*) FROM invocations WHERE run_id=? AND attempt=?',
                            (accepted['run_id'], attempt['id']))[0] == 2
                        retained = tx.one('SELECT opening_id,window,purpose,accounted FROM attempts WHERE run_id=? AND id=?',
                            (accepted['run_id'], attempt['id']))
                        assert tuple(retained) == tuple(attempt[key] for key in ('opening_id', 'window', 'purpose', 'accounted'))
                        assert current.context['human_responses'] == [MESSAGE]
                        assert current.context['integration_evidence'] == json.loads(receipt)
                        assert current.context['prior_human_blockers'] == [QUESTION]
                        job = tx.one("SELECT * FROM subjects WHERE run_id=? AND id='final:1'", (accepted['run_id'],))
                        assert (job['current_attempt'], job['generation'], job['purpose']) == (attempt['id'], before['generation'], 'integration')
                        event = tx.one("SELECT material_id FROM history WHERE run_id=? AND identity=? AND kind='human_response'",
                            (accepted['run_id'], resumed['action_id']))
                        assert tx.get(event[0], 'final:1') == MESSAGE.encode()
                    assert reopened.status('final:1')['budgets'] == before['budgets']
                    channel.sendall(b'1')
                review, channel = next_worker()
                with closing(channel):
                    assert review['role'] == 'review'
                    assert (Path(review['checkout']) / 'shared.txt').read_text() == 'Resolved ticket and external content\n'
                    assert not _git(Path(review['checkout']), 'ls-files', '-u')
                    with reopened.storage.transaction(write=False) as tx:
                        review_attempt = tx.one("SELECT context_id FROM attempts WHERE run_id=? AND subject='final:1' AND role='review' ORDER BY ordinal DESC LIMIT 1",
                            (accepted['run_id'],))
                        record = json.loads(tx.get(review_attempt[0], 'final:1'))['review_context']['facts']['integration_record']
                        assert (record['effect_id'], record['receipt_id'], record['development_attempt']) == (conflict['id'], conflict['receipt_id'], attempt['id'])
                        assert record['receipt'] == json.loads(receipt)
                    channel.sendall(b'1')
                publication, channel = next_worker()
                with closing(channel):
                    assert publication['role'] == 'publication'
                    channel.sendall(b'1')
                barriers.enter_context(final_approval_hold(at_approval, release_approval, serving, reopened,
                                                          tmp_path, children, deadline))
                assert serving.is_alive() and children[0].poll() is None
                final = reopened.status('final:1')
                assert final['phase'] == 'approval' and final['generation'] == before['generation']
                assert final['budgets']['integration']['window'] == budget['window']
                assert final['budgets']['integration']['developments_used'] == 1
                assert final['budgets']['integration'] == dict(budget, reviews_used=budget['reviews_used'] + 1)
                with reopened.storage.transaction(write=False) as tx:
                    assert not tx.rows('SELECT id FROM leases WHERE run_id=?', (accepted['run_id'],))
                    outputs = tx.all('SELECT dispatch_state,exit_code FROM invocation_outputs WHERE run_id=?', (accepted['run_id'],))
                    assert [tuple(row) for row in outputs] == [('exited', 0)] * 7
                    owned_processes = [json.loads(tx.get(row['process_identity'], row['subject'])) for row in tx.all(
                        'SELECT o.process_identity,a.subject FROM invocation_outputs o JOIN invocations i '
                        'ON i.run_id=o.run_id AND i.id=o.invocation JOIN attempts a ON a.run_id=i.run_id AND a.id=i.attempt '
                        'WHERE o.run_id=?', (accepted['run_id'],))]
                    assert tx.get(conflict['receipt_id'], 'final:1') == receipt
                    assert dict(tx.one('SELECT * FROM invocation_outputs WHERE run_id=? AND invocation=? AND ordinal=?',
                        (accepted['run_id'], prior.prior.invocation_id, prior.output_ordinal))) == old_output
                    for logical, backend in ((prior.thread_id, first['thread']), (continued_thread, continued['thread'])):
                        assert tx.one('SELECT backend_id FROM threads WHERE run_id=? AND id=?', (accepted['run_id'], logical))[0] == backend
                    assert [tuple(row) for row in tx.all('SELECT * FROM requirement_versions WHERE run_id=?', (accepted['run_id'],))] == requirements
                history = reopened.history('final:1')
                assert sum(event['kind'] == 'worker_started' for event in history) == 4
                assert sum(event['kind'] == 'worker_exited' for event in history) == 4
                assert _rows(sibling) == sibling_rows
                assert all(_process_binding_status(process['pid'], process['start_token']) == 'absent' for process in owned_processes)
                prs = json.loads(state.read_text())['prs']
                assert len(prs) == 3  # Ticket, checked Repair, and Final.
                repair_record, = final['repair_history']
                repair_pr, = [pr for pr in prs if pr['number'] == repair_record['pr_number']]
                final_pr, = [pr for pr in prs if pr['number'] == final['pr_number']]
                run_ref = reopened.delivery('final:1').source_ref
                assert repair_record['state'] == 'merged' and repair_pr['merged']
                assert repair_pr['state'] == 'closed'
                assert 'refs/heads/' + repair_pr['base']['ref'] == repair_record['target_ref'] == run_ref
                assert 'refs/heads/' + repair_pr['head']['ref'] == repair_record['source_ref']
                assert repair_pr['head']['sha'] == repair_record['published_sha'] == repair_record['integrated_sha']
                assert repair_pr['merge_commit_sha'] == repair_record['merge_sha'] == final_pr['head']['sha']
                assert _git(tmp_path / 'origin.git', 'show', '-s', '--format=%T', repair_pr['head']['sha']) == repair_record['integrated_tree']
                assert _git(tmp_path / 'origin.git', 'show', '-s', '--format=%T', repair_record['merge_sha']) == repair_record['expected_tree']
                assert _git(tmp_path / 'origin.git', 'rev-parse', 'refs/heads/main') == external_head
                with abandon_at_approval(public, reopened, accepted, state, external_head, deadline):
                    release_approval.set()
                    with report_abandon_failure('abandon-host-after-barrier-release', reopened, tmp_path, children):
                        serving.join(timeout=remaining())
                        assert not serving.is_alive() and not errors, (errors, output)
                        assert len(children) == len(commands) == 1 and children[0].returncode == 0
                        assert trace == ['fdstore', 'barrier', 'host_exited', 'store_closed']
                assert _rows(sibling) == sibling_rows
                with runner_management_lease(default_runner_lock_path(environment)):
                    pass
                assert _git(git_repo, 'status', '--porcelain=v1') == ''
            finally:
                release_approval.set()
                release.set()


@pytest.mark.parametrize('kind', [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_current_development_resume_rules_keep_work_and_budget(tmp_path, kind):
    """Core current-work rules only; the native test owns integration/CLI proof."""
    subject = 'ticket:2' if kind == DeliveryKind.TICKET else 'parent:1'
    spec = RunSpec('rules', 'example/repository', 1, 'unused', 'unused', (
        Delivery(subject, kind, 2 if kind == DeliveryKind.TICKET else 1, 'unused', Commit('base', 'tree'),
            'refs/heads/work', 'refs/heads/target', checkout=str(tmp_path)),))
    path = tmp_path / 'rules.sqlite3'
    _, ports = native_agent_fixture(path, spec)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        kernel = ScopedKernel(db, spec.run_id)
        kernel.storage.path, kernel.storage.run_id = path, spec.run_id
        execution = kernel.claim_executor('rules-owner')
        operations = CoreOperations(kernel, execution, lambda _: ports)
        first = operations.once().call
        assert first is not None and first.role == 'development'
        scripted_return(kernel, operations.runner, first, lambda _: wire_json({
            'result_kind':'human_blocker', 'summary':None, 'human_blockers':[QUESTION]}))
        assert operations.once().reason == 'human_blocker'
        budget = kernel.status(subject)['budgets']
        with kernel.storage.transaction(write=False) as tx:
            original = load_continuation(tx, first.attempt_id)
            opening = tx.one('SELECT opening_id FROM attempts WHERE run_id=? AND id=?', (spec.run_id, first.attempt_id))[0]
        kernel.control('resume', 'answer', subject=subject, message=MESSAGE, new_thread=True)
        applied = _rows(kernel)
        kernel.control('resume', 'answer', subject=subject, message=MESSAGE, new_thread=True)
        assert _rows(kernel) == applied
        second = operations.once().call
        assert second is not None and second.role == 'development'
        assert (second.subject, second.attempt_id, second.purpose) == (first.subject, first.attempt_id, first.purpose)
        assert second.thread_id != first.thread_id and second.mode == 'fresh' and second.reported_thread is None
        assert second.context['generation'] == first.context['generation']
        assert second.context['human_responses'] == [MESSAGE] and second.context['prior_human_blockers'] == [QUESTION]
        assert kernel.status(subject)['budgets'] == budget
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one('SELECT opening_id FROM attempts WHERE run_id=? AND id=?', (spec.run_id, first.attempt_id))[0] == opening
            assert tx.get(original.prior.raw_id, subject) == wire_json({'result_kind':'human_blocker', 'summary':None, 'human_blockers':[QUESTION]})
            event = tx.one("SELECT material_id FROM history WHERE run_id=? AND identity='answer' AND kind='human_response'", (spec.run_id,))
            assert tx.get(event[0], subject) == MESSAGE.encode()
        scripted_return(kernel, operations.runner, second, lambda _: wire_json({
            'result_kind':'development', 'summary':'Continued original work.', 'human_blockers':None}))
        assert kernel.status(subject)['budgets'] == budget
        assert budget['requirements']['developments_used'] == 1
        assert not db.execute('SELECT id FROM leases WHERE run_id=?', (spec.run_id,)).fetchall()


@contextmanager
def _opened_resume_core(path, run):
    """Existing transaction seam; no native clock or physical Host claim."""
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        kernel = ScopedKernel(db, run)
        kernel.storage.path, kernel.storage.run_id = path, run
        yield kernel


@pytest.mark.parametrize('kind', [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_git_integration_human_resume_preserves_attempt_receipt_and_budget(tmp_path, kind):
    physical = OfflinePort.create(tmp_path / 'integration')
    spec, delivery = physical.prepare(kind)
    path = tmp_path / 'integration.sqlite3'
    _, original = native_agent_fixture(path, spec)
    ports = Ports(SimpleNamespace(port=physical, spec=spec, requirements=original.requirements))
    subject = delivery.key
    with _opened_resume_core(path, spec.run_id) as kernel:
        execution = kernel.claim_executor('integration-owner')
        operations = CoreOperations(kernel, execution, lambda _: ports)
        first = operations.until(lambda step: step.call is not None).call
        assert first is not None and first.role == 'development'
        checkout = Path(first.context['checkout'])
        (checkout / 'shared.txt').write_text('Candidate content\n')
        scripted_return(kernel, operations.runner, first, lambda _: wire_json({
            'result_kind': 'development', 'summary': 'Original candidate', 'human_blockers': None}))
        review = operations.until(lambda step: step.call is not None).call
        assert review is not None and review.role == 'review'
        scripted_return(kernel, operations.runner, review, lambda _: wire_json(review_result('pass')))
        assert operations.once().kind == 'progress' and kernel.status(subject)['phase'] == 'integration'
        physical.external_append(delivery.target_ref, {'shared.txt': 'External content\n'}, 'conflict')
        repair = operations.until(lambda step: step.call is not None).call
        assert repair is not None and repair.role == 'development' and repair.purpose == 'integration'
        assert repair.context['repair_source'] == 'integration'
        evidence = repair.context['integration_evidence']
        assert evidence['disposition'] == 'failure' and evidence['raw']['merge_conflict'] is True
        assert _git(checkout, 'ls-files', '-u')
        merge_head = _git(checkout, 'rev-parse', 'MERGE_HEAD')
        assert merge_head == evidence['raw']['merge_head']
        (checkout / 'retained-evidence.txt').write_text('Original investigation\n')
        raw = wire_json({'result_kind': 'human_blocker', 'summary': None, 'human_blockers': [QUESTION]})
        scripted_return(kernel, operations.runner, repair, lambda _: raw)
        assert operations.once().reason == 'human_blocker'
        paused = kernel.status(subject)
        assert (paused['purpose'], paused['gates']) == ('integration', ['human_blocker'])
        assert paused['budgets']['integration']['developments_used'] == 1
        with kernel.storage.transaction(write=False) as tx:
            previous = load_continuation(tx, repair.attempt_id)
            attempt = dict(tx.one('SELECT * FROM attempts WHERE run_id=? AND id=?', (spec.run_id, repair.attempt_id)))
            failed = dict(tx.one("SELECT * FROM effects WHERE run_id=? AND subject=? AND operation='integrate' AND status='confirmed'",
                (spec.run_id, subject)))
            receipt = tx.get(failed['receipt_id'], subject)
            assert json.loads(receipt) == evidence

    with _opened_resume_core(path, spec.run_id) as kernel:
        assert kernel.status(subject) == paused
        kernel.control('resume', 'integration-answer', subject=subject, message=MESSAGE, new_thread=True)
        operations = CoreOperations(kernel, execution, lambda _: ports)
        continued = operations.once().call
        assert continued is not None and continued.role == 'development'
        assert (continued.attempt_id, continued.purpose, continued.context['generation']) == (
            repair.attempt_id, 'integration', repair.context['generation'])
        assert continued.thread_id != repair.thread_id and continued.mode == 'fresh'
        assert continued.context['repair_source'] == 'integration'
        assert continued.context['integration_evidence'] == evidence
        assert continued.context['human_responses'] == [MESSAGE]
        assert continued.context['prior_human_blockers'] == [QUESTION]
        assert kernel.status(subject)['budgets'] == paused['budgets']
        with kernel.storage.transaction(write=False) as tx:
            current = tx.one('SELECT opening_id,window,purpose,accounted FROM attempts WHERE run_id=? AND id=?',
                (spec.run_id, continued.attempt_id))
            assert tuple(current) == tuple(attempt[key] for key in ('opening_id', 'window', 'purpose', 'accounted'))
            assert tx.get(previous.prior.raw_id, subject) == raw
            assert tx.get(failed['receipt_id'], subject) == receipt
            response = tx.one("SELECT material_id FROM history WHERE run_id=? AND identity='integration-answer' AND kind='human_response'",
                (spec.run_id,))
            assert tx.get(response[0], subject) == MESSAGE.encode()
        assert _git(checkout, 'rev-parse', 'MERGE_HEAD') == merge_head
        assert (checkout / 'retained-evidence.txt').read_text() == 'Original investigation\n'
        (checkout / 'shared.txt').write_text('Resolved candidate and external content\n')
        scripted_return(kernel, operations.runner, continued, lambda _: wire_json({
            'result_kind': 'development', 'summary': 'Resolved original conflict', 'human_blockers': None}))
        operations.until(lambda _: kernel.status(subject)['phase'] == 'integration')
        assert kernel.status(subject)['purpose'] == 'integration'
        assert kernel.status(subject)['budgets'] == paused['budgets']
        assert not _git(checkout, 'ls-files', '-u')
        assert set(_git(checkout, 'show', '-s', '--format=%P', 'HEAD').split()) == {
            evidence['raw']['head'], merge_head}
        with kernel.storage.transaction(write=False) as tx:
            assert tx.get(failed['receipt_id'], subject) == receipt
            assert tx.one('SELECT count(*) FROM invocations WHERE run_id=? AND attempt=?',
                (spec.run_id, repair.attempt_id))[0] == 2
            assert not tx.rows('SELECT id FROM leases WHERE run_id=?', (spec.run_id,))


def test_targeted_ticket_resume_preserves_same_run_sibling_human_gate(tmp_path):
    path = tmp_path / 'siblings.sqlite3'
    created, source = delivery_run(path)
    run = created.storage.run_id
    with _opened_resume_core(path, run) as kernel:
        execution = kernel.claim_executor('sibling-owner')
        operations = source_operations(kernel, source, execution)
        selected, sibling = operations.once().call, operations.once().call
        assert selected is not None and sibling is not None
        assert (selected.subject, sibling.subject) == ('ticket:2', 'ticket:3')
        for call in (selected, sibling):
            scripted_return(kernel, operations.runner, call, lambda _: wire_json({
                'result_kind': 'human_blocker', 'summary': None, 'human_blockers': [QUESTION]}))
            assert operations.once(agent_capacity=0).reason == 'human_blocker'
        before = kernel.status(sibling.subject)
        assert before['gates'] == ['human_blocker']
        with kernel.storage.transaction(write=False) as tx:
            gate = tuple(tx.one('SELECT * FROM gates WHERE run_id=? AND subject=?', (run, sibling.subject)))
            original = load_continuation(tx, sibling.attempt_id)
        target = kernel.resume_subject(ticket=2)
        assert target == selected.subject
        kernel.control('resume', 'selected-answer', subject=target, message=MESSAGE, new_thread=True)
        resumed = operations.once().call
        assert resumed is not None and resumed.attempt_id == selected.attempt_id
        assert resumed.thread_id != selected.thread_id and resumed.context['human_responses'] == [MESSAGE]
        scripted_return(kernel, operations.runner, resumed, lambda _: wire_json({
            'result_kind': 'development', 'summary': 'Continued selected Ticket', 'human_blockers': None}))
        assert kernel.status(sibling.subject) == before
        with kernel.storage.transaction(write=False) as tx:
            assert tuple(tx.one('SELECT * FROM gates WHERE run_id=? AND subject=?', (run, sibling.subject))) == gate
            assert load_continuation(tx, sibling.attempt_id) == original
            assert not tx.rows("SELECT position FROM history WHERE run_id=? AND subject=? AND kind='human_response'",
                (run, sibling.subject))

    with _opened_resume_core(path, run) as kernel:
        assert kernel.status(sibling.subject) == before
