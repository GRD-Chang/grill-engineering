"""Only synthetic executable diagnostics; no Host, manager or namespace starts."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from support.systemd_manager import _diagnostic_tail, _executable, _managed_diagnostics
from support.repository_fixture import create_kernel
from support.offline import fixture_inputs
from support.sqlite_faults import logical_state
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec


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
        assert facts['outputs'] == [] and facts['gates'] == [] and facts['waits'] == []
        assert facts['worker_errors'] == 'FileNotFoundError'
        assert 'PRIVATE_FROZEN_REQUIREMENT' not in json.dumps(facts)
    unavailable = _managed_diagnostics(database, 'absent', roles)
    assert 'unavailable' in unavailable and 'run' not in unavailable
    assert (logical_state(database), database.read_bytes()) == before
    assert not Path(str(roles) + '.errors').exists()


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
