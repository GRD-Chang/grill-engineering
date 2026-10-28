"""Only synthetic executable diagnostics; no Host, manager or namespace starts."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from support.systemd_manager import _diagnostic_tail, _executable, _managed_diagnostics, _managed_failure_note, MANAGED_NOTE_BYTES
from support.systemd_manager import SidecarStatTrace
from agent_run.core.kernel import Kernel
from support.repository_fixture import create_kernel
from support.offline import fixture_inputs
from support.sqlite_faults import logical_state
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec


def test_sidecar_trace_returns_exact_original_stat_once_and_bounds_only_exact_database(tmp_path):
    database = tmp_path / 'kernel.sqlite3'
    sidecar = Path(str(database) + '-wal')
    sidecar.write_bytes(b'Original sidecar bytes')
    metadata = sidecar.stat(follow_symlinks=False)
    trace, calls = SidecarStatTrace(database), []
    def original(path, *args, **kwargs):
        calls.append((path, args, kwargs))
        return metadata
    wrapped = trace.wrap(original)
    other = tmp_path / 'sibling.sqlite3-wal'
    assert wrapped(other, follow_symlinks=False) is metadata
    for _ in range(40):
        assert wrapped(sidecar, follow_symlinks=False) is metadata
    failure = ValueError('Original failure')
    trace.note(failure)
    facts = json.loads(failure.__notes__[0])
    assert len(calls) == 41 and all(call[1:] == ((), {'follow_symlinks': False}) for call in calls)
    assert facts['total_calls'] == 40 and facts['retained_calls'] == 16
    assert len(facts['sidecar_stat_trace']) == 16 and len(failure.__notes__[0].encode()) <= 8192
    for row in facts['sidecar_stat_trace']:
        assert row['suffix'] == '-wal' and row['phase'] == 'completion-poll'
        assert row['thread'] > 0 and row['monotonic_ns'] > 0 and row['follow_symlinks'] is False
        assert {key: row[key] for key in ('dev', 'ino', 'mode', 'nlink', 'size')} == dict(
            dev=metadata.st_dev, ino=metadata.st_ino, mode=metadata.st_mode,
            nlink=metadata.st_nlink, size=metadata.st_size)
    assert str(database) not in failure.__notes__[0]


@pytest.mark.parametrize('failure_type,number', [(FileNotFoundError, 2),
    (PermissionError, 13), (KeyboardInterrupt, None)])
@pytest.mark.parametrize('selected', [False, True])
def test_sidecar_trace_reraises_exact_exception_without_retry_or_private_detail(tmp_path, failure_type, number, selected):
    failure = failure_type('Private detail') if number is None else failure_type(number, 'Private detail')
    database = tmp_path / 'kernel.sqlite3'
    trace, calls = SidecarStatTrace(database), []
    path = Path(str(database) + '-shm') if selected else tmp_path / 'other-shm'
    def original(*args, **kwargs):
        calls.append((args, kwargs))
        raise failure
    with pytest.raises(type(failure)) as caught:
        trace.wrap(original)(path, follow_symlinks=False)
    assert caught.value is failure and len(calls) == 1
    trace.note(failure)
    facts = json.loads(failure.__notes__[0])
    assert facts['total_calls'] == int(selected)
    if selected:
        assert facts['sidecar_stat_trace'][0]['errno'] == getattr(failure, 'errno', None)
        assert facts['sidecar_stat_trace'][0]['outcome'] == 'raise'
    assert 'Private detail' not in failure.__notes__[0]


def test_sidecar_recording_failure_cannot_change_stat_return_or_error(tmp_path, monkeypatch):
    database = tmp_path / 'kernel.sqlite3'
    sidecar = Path(str(database) + '-wal')
    sidecar.touch()
    metadata, trace = sidecar.stat(), SidecarStatTrace(database)
    def unavailable(_row):
        raise RuntimeError('Evidence unavailable')
    monkeypatch.setattr(trace, '_record', unavailable)
    assert trace.wrap(lambda *_a, **_kw: metadata)(sidecar) is metadata
    failure = OSError(5, 'Original stat failure')
    def original(*_args, **_kwargs):
        raise failure
    with pytest.raises(OSError) as caught:
        trace.wrap(original)(sidecar)
    assert caught.value is failure


@pytest.mark.parametrize('suffix', ['-wal', '-shm'])
@pytest.mark.parametrize('outcome', ['existing', 'replacement', 'unreadable'])
def test_sidecar_trace_preserves_preflight_zero_link_rejection(tmp_path, monkeypatch, suffix, outcome):
    from types import SimpleNamespace
    import stat
    from agent_run.storage import preflight
    from agent_run.storage.sqlite import Storage
    storage = Storage.create(tmp_path / 'kernel.sqlite3', namespace=('example/project', str(tmp_path.resolve())))
    selected = Path(str(storage.path) + suffix)
    original, calls, prior_wal = Path.stat, [], []
    def observed(path, *args, **kwargs):
        if path != selected:
            metadata = original(path, *args, **kwargs)
            if path == Path(str(storage.path) + '-wal'):
                prior_wal.append(metadata)
            return metadata
        calls.append(path)
        assert len(calls) <= 2 and kwargs == {'follow_symlinks': False}
        if len(calls) == 2 and outcome == 'unreadable':
            raise PermissionError(13, 'Controlled refusal')
        return SimpleNamespace(st_dev=1, st_ino=3 if len(calls) == 2 and outcome == 'replacement' else 2,
            st_mode=stat.S_IFREG | 0o600, st_nlink=0 if len(calls) == 1 else 1, st_size=32768)
    monkeypatch.setattr(Path, 'stat', observed)
    trace = SidecarStatTrace(storage.path)
    with pytest.raises(preflight.UnsupportedState) as caught:
        with trace.capture(monkeypatch, finish=lambda: None):
            with preflight.read_state(storage.path):
                pytest.fail('Trace allowed unsafe sidecar adoption')
    assert calls == [selected, selected]
    facts = json.loads(caught.value.__notes__[-1])
    rows = facts['sidecar_stat_trace']
    assert facts['total_calls'] == 2 + int(suffix == '-shm')
    if suffix == '-shm':
        assert len(prior_wal) == 1 and rows[0]['suffix'] == '-wal' and rows[0]['outcome'] == 'return'
        assert {key: rows[0][key] for key in ('dev', 'ino', 'mode', 'nlink', 'size')} == dict(
            dev=prior_wal[0].st_dev, ino=prior_wal[0].st_ino, mode=prior_wal[0].st_mode,
            nlink=prior_wal[0].st_nlink, size=prior_wal[0].st_size)
    selected_rows = [row for row in rows if row['suffix'] == suffix]
    assert len(selected_rows) == 2 and selected_rows[0]['nlink'] == 0
    second = selected_rows[1]
    assert second['outcome'] == ('raise' if outcome == 'unreadable' else 'return')
    if outcome != 'unreadable':
        assert second['nlink'] == 1 and second['ino'] == (3 if outcome == 'replacement' else 2)


@pytest.mark.parametrize('body_fails', [False, True])
@pytest.mark.parametrize('timing_fails', [False, True])
def test_sidecar_capture_keeps_primary_failure_runs_timing_and_restores_stat(tmp_path, monkeypatch,
                                                                          body_fails, timing_fails):
    database = tmp_path / 'kernel.sqlite3'
    sidecar = Path(str(database) + '-wal')
    sidecar.touch()
    original_stat, trace, finished = Path.stat, SidecarStatTrace(database), []
    body_error, timing_error = ValueError('Original failure'), OSError(5, 'Timing failure')
    def finish():
        finished.append(True)
        sidecar.stat(follow_symlinks=False)
        if timing_fails:
            raise timing_error
    failure = None
    try:
        with trace.capture(monkeypatch, finish=finish):
            sidecar.stat(follow_symlinks=False)
            if body_fails:
                raise body_error
    except BaseException as error:
        failure = error
    assert Path.stat is original_stat and finished == [True]
    assert failure is (body_error if body_fails else timing_error if timing_fails else None)
    if failure is not None:
        facts = json.loads(failure.__notes__[0])
        assert [row['phase'] for row in facts['sidecar_stat_trace']] == (
            ['completion-poll'] if body_fails else ['completion-poll', 'timing-finally'])
        assert facts['total_calls'] == (1 if body_fails else 2)
        if body_fails and timing_fails:
            assert failure.__notes__[1] == 'Managed timing evidence unavailable: OSError'


def test_timing_cleanup_cannot_overwrite_the_failing_sidecar_ring(tmp_path, monkeypatch):
    database = tmp_path / 'kernel.sqlite3'
    sidecar = Path(str(database) + '-wal')
    sidecar.touch()
    trace, failure = SidecarStatTrace(database), ValueError('Original failure')
    def finish():
        for _ in range(40):
            sidecar.stat(follow_symlinks=False)
    with pytest.raises(ValueError) as caught:
        with trace.capture(monkeypatch, finish=finish):
            sidecar.stat(follow_symlinks=False)
            raise failure
    assert caught.value is failure
    facts = json.loads(failure.__notes__[0])
    assert facts['total_calls'] == facts['retained_calls'] == 1
    assert facts['sidecar_stat_trace'][0]['phase'] == 'completion-poll'


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'socket', 'symlink'])
def test_diagnostic_read_rejects_nonregular_open_without_blocking(tmp_path, kind):
    path = tmp_path / 'trace.errors'
    target = tmp_path / 'private-target'
    with ExitStack() as owned:
        if kind == 'fifo':
            os.mkfifo(path)
        elif kind == 'directory':
            path.mkdir()
        elif kind == 'socket':
            channel = owned.enter_context(socket.socket(socket.AF_UNIX))
            channel.bind(str(path))
        else:
            target.write_bytes(b'Original target bytes')
            path.symlink_to(target)
        before = path.lstat()
        program = 'from pathlib import Path;import sys;from support.systemd_manager import _diagnostic_tail;print(_diagnostic_tail(Path(sys.argv[1])))'
        result = subprocess.run([sys.executable, '-c', program, str(path)], timeout=2,
            env={'PATH': os.defpath, 'PYTHONPATH': str(Path(__file__).parent)}, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ('NonRegularFile' if kind in {'fifo', 'directory'} else 'OSError')
        assert path.lstat().st_ino == before.st_ino
        if kind == 'symlink':
            assert target.read_bytes() == b'Original target bytes'


@pytest.mark.parametrize('kind', ['fifo', 'fifo-reader', 'directory', 'socket', 'symlink', 'close-fault'])
def test_diagnostic_write_rejects_nonregular_fd_and_preserves_original_exit(tmp_path, kind):
    executable, trace = tmp_path / 'codex', tmp_path / 'roles.jsonl'
    errors = Path(str(trace) + '.errors')
    target = tmp_path / 'private-target'
    with ExitStack() as owned:
        if kind in {'fifo', 'fifo-reader', 'close-fault'}:
            os.mkfifo(errors)
            if kind != 'fifo':
                reader = os.open(errors, os.O_RDONLY | os.O_NONBLOCK)
                owned.callback(os.close, reader)
        elif kind == 'directory':
            errors.mkdir()
        elif kind == 'socket':
            channel = owned.enter_context(socket.socket(socket.AF_UNIX))
            channel.bind(str(errors))
        else:
            target.write_bytes(b'Original target bytes')
            errors.symlink_to(target)
        before = errors.lstat()
        program = '''import os
original_close = os.close
def failed_close(descriptor):
    original_close(descriptor)
    raise OSError('Synthetic diagnostic close failure')
'''
        if kind == 'close-fault':
            program += 'os.close = failed_close\n'
        program += "raise ValueError('Original synthetic failure')\n"
        _executable(executable, program)
        result = subprocess.run([str(executable)], timeout=2, capture_output=True, text=True,
            env={'PATH': os.defpath, 'ENTRY_ROLE_TRACE': str(trace)})
        assert result.returncode == 1 and 'ValueError: Original synthetic failure' in result.stderr
        assert 'Error in sys.excepthook' not in result.stderr
        assert 'Synthetic diagnostic close failure' not in result.stderr
        assert errors.lstat().st_ino == before.st_ino
        if kind in {'fifo-reader', 'close-fault'}:
            assert os.read(reader, 1) == b''
        if kind == 'symlink':
            assert target.read_bytes() == b'Original target bytes'


def test_diagnostic_read_closes_rejected_fd_without_replacing_failure(tmp_path):
    path = tmp_path / 'trace.errors'
    os.mkfifo(path)
    program = '''import json,os,sys
from pathlib import Path
from support.systemd_manager import _diagnostic_tail
original = os.close
closed = []
def failed_close(descriptor):
    original(descriptor)
    closed.append(descriptor)
    raise OSError('Synthetic diagnostic close failure')
os.close = failed_close
try:
    result = _diagnostic_tail(Path(sys.argv[1]))
finally:
    os.close = original
assert result == 'NonRegularFile' and len(closed) == 1
try:
    os.fstat(closed[0])
except OSError:
    print(json.dumps({'result': result, 'descriptor_closed': True}))
else:
    raise AssertionError('Rejected diagnostic FD was retained')
'''
    result = subprocess.run([sys.executable, '-c', program, str(path)], timeout=2,
        env={'PATH': os.defpath, 'PYTHONPATH': str(Path(__file__).parent)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'result': 'NonRegularFile', 'descriptor_closed': True}


def test_managed_failure_snapshot_is_readonly_exact_run_and_handles_unavailable_state(tmp_path):
    database = tmp_path / 'state' / 'kernel.sqlite3'
    for number, name in enumerate(('one', 'two'), 1):
        delivery = Delivery('shared-parent', DeliveryKind.PARENT_ONLY, number, 'original',
            Commit('a' * 40, 'b' * 40), 'refs/heads/' + name, 'refs/heads/main')
        create_kernel(database, RunSpec(name, 'example/project', number, 'r', 'g', (delivery,),
            language='en', repository_root=str(tmp_path / 'repository'), inputs=fixture_inputs()),
            requirements=Requirements(ParentIssue(number, 'Parent', 'PRIVATE_FROZEN_REQUIREMENT'), ()))
    before = logical_state(database), database.read_bytes()
    roles = tmp_path / 'roles.jsonl'
    for name in ('one', 'two'):
        facts = _managed_diagnostics(database, name, roles)
        assert 'unavailable' not in facts and facts['run']['id'] == name
        assert len(facts['subjects']) == 1 and facts['subjects'][0]['id'] == 'shared-parent'
        assert facts['run']['executor'] is None and facts['run']['final_action'] is None
        assert facts['actions'] == []
        assert facts['outputs'] == [] and facts['gates'] == [] and facts['waits'] == []
        assert all(facts[key] == [] for key in ('attempts', 'leases', 'work_queue', 'integration_queue'))
        assert facts['worker_errors'] == 'FileNotFoundError'
        assert 'PRIVATE_FROZEN_REQUIREMENT' not in json.dumps(facts)
    unavailable = _managed_diagnostics(database, 'absent', roles)
    assert 'unavailable' in unavailable and 'run' not in unavailable
    assert (logical_state(database), database.read_bytes()) == before
    assert not Path(str(roles) + '.errors').exists()

    # Separate normal-control stage: keep the original empty-gate assertions
    # above, then prove new Action diagnostics cannot mix same-subject Runs.
    for name in ('one', 'two'):
        selected = Kernel(database, run_id=name)
        assert selected.control('stop', name + '-action', subject='shared-parent') == 'applied'
    after_control = logical_state(database), database.read_bytes()
    for name in ('one', 'two'):
        facts = _managed_diagnostics(database, name, roles)
        assert facts['actions'] == [{'id': name + '-action', 'kind': 'stop',
            'subject': 'shared-parent', 'status': 'applied'}]
        assert facts['gates'] == [{'subject': 'shared-parent', 'code': 'operator_stopped'}]
    assert (logical_state(database), database.read_bytes()) == after_control


def test_managed_failure_note_is_structured_bounded_and_keeps_the_original_error(tmp_path, monkeypatch):
    from support import systemd_manager
    facts = {'attempts': [{'id': str(i), 'status': 'returned', 'result_id': 'r' * 1000} for i in range(32)],
        'leases': [], 'work_queue': [{'subject': str(i), 'ready': i} for i in range(32)]}
    monkeypatch.setattr(systemd_manager, '_managed_diagnostics', lambda *_args: facts)
    original = ValueError('Original owned failure')
    with pytest.raises(ValueError) as observed:
        try:
            raise original
        except ValueError as error:
            _managed_failure_note(error, stage='original-stage', database=tmp_path / 'unused', run_id='A', roles=tmp_path / 'unused-roles')
            raise
    assert observed.value is original
    note, = original.__notes__
    assert len(note.encode('utf-8')) <= MANAGED_NOTE_BYTES
    value = json.loads(note)
    assert value['stage'] == 'original-stage' and value['run_id'] == 'A'
    assert value['omitted_rows']['attempts'] > 0 and value['facts']['attempts'][0]['status'] == 'returned'
    assert value['facts']['work_queue'][0] == {'subject': '0', 'ready': 0}
    assert not (tmp_path / 'unused').exists()


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_failed_managed_diagnostic_read_cannot_replace_the_original_error(tmp_path, monkeypatch, failure):
    from support import systemd_manager
    def failed(*_args):
        raise failure('Untrusted private diagnostic detail')
    monkeypatch.setattr(systemd_manager, '_managed_diagnostics', failed)
    original = AssertionError('Original predicate failed')
    _managed_failure_note(original, stage='owned-stage', database=tmp_path / 'unused', run_id='A', roles=tmp_path / 'unused-roles')
    assert original.__notes__ == ['Managed snapshot unavailable: ' + failure.__name__]
    assert not (tmp_path / 'unused').exists()


@pytest.mark.parametrize('outcome', ['success', 'failure', 'oversize', 'unavailable'])
def test_synthetic_worker_trace_preserves_exit_and_bounds_diagnostics(tmp_path, outcome):
    executable = tmp_path / 'codex'
    trace = tmp_path / 'roles.jsonl'
    sentinel = 'SYNTHETIC_PRIVATE_ENVIRONMENT_VALUE'
    environment = {'PATH': os.defpath, 'PRIVATE_DIAGNOSTIC_SENTINEL': sentinel}
    if outcome != 'unavailable':
        environment['ENTRY_ROLE_TRACE'] = str(trace)
    program = ("print('Original successful stdout')\n" if outcome == 'success' else
               "raise ValueError('Synthetic Worker failure.'" + (" + 'x' * 20000" if outcome == 'oversize' else '') + ")\n")
    _executable(executable, program)
    result = subprocess.run([str(executable), sentinel], env=environment,
                            capture_output=True, text=True, timeout=5)
    errors = Path(str(trace) + '.errors')
    assert result.returncode == (0 if outcome == 'success' else 1)
    if outcome in {'success', 'unavailable'}:
        assert not errors.exists()
        assert _diagnostic_tail(errors) == 'FileNotFoundError'
        assert result.stdout == ('Original successful stdout\n' if outcome == 'success' else '')
    else:
        record, = [json.loads(line) for line in errors.read_text().splitlines()]
        assert set(record) == {'error_type', 'traceback'} and record['error_type'] == 'ValueError'
        assert 'Synthetic Worker failure.' in record['traceback']
        assert str(executable) in record['traceback']
        assert len(record['traceback'].encode()) <= 8192
        assert ('fixture traceback truncated' in record['traceback']) == (outcome == 'oversize')
        assert len(_diagnostic_tail(errors).encode()) <= 8192
        assert sentinel not in errors.read_text()
    assert sentinel not in result.stdout + result.stderr
    if outcome != 'success':
        assert 'ValueError: Synthetic Worker failure.' in result.stderr


def test_managed_gh_retains_canonical_exact_head_base_filter_without_starting_host(tmp_path, git_repo, monkeypatch):
    from support.systemd_manager import _control_environment, _git
    directory = tmp_path / 'managed-fixture'
    directory.mkdir()
    environment, state = _control_environment(git_repo, directory, monkeypatch, codex='raise SystemExit(99)')
    document = json.loads(state.read_text())
    actual_main = _git(git_repo, 'rev-parse', 'HEAD')
    document['prs'] = [
        {'number': number, 'merged': False, 'head': {'ref': head, 'sha': 'a' * 40},
         'base': {'ref': base, 'sha': actual_main if base == 'main' else 'b' * 40}}
        for number, head, base in ((17, 'ticket-2', 'run'), (18, 'ticket-3', 'run'), (19, 'ticket-2', 'main'))]
    state.write_text(json.dumps(document))
    before = state.read_bytes()
    gh = directory / 'control-bin' / 'gh'
    for query, expected in (('head=owner:ticket-2&base=run', [17]),
                             ('head=owner:ticket-3&base=run', [18]),
                             ('head=owner:ticket-2&base=main', [19]), ('base=run', [17, 18])):
        result = subprocess.run([str(gh), 'api', 'repos/example/project/pulls?' + query, '--paginate', '--slurp'],
            cwd=git_repo, env=environment, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        page, = json.loads(result.stdout)
        assert [pull['number'] for pull in page] == expected
    assert state.read_bytes() == before
    assert not (directory / 'roles.jsonl').exists()
