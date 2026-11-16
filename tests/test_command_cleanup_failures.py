"""Real owned children; secondary cleanup faults cannot erase capture failure."""
import subprocess
import sys

import pytest

from agent_run.adapters import github_commands
from agent_run.runtime import command, process_io
from agent_run.runtime.process_io import CompleteOutput


@pytest.mark.parametrize('mode,failure_kind', [
    ('structured', 'watchdog'), ('structured', 'interrupt'),
    ('combined', 'watchdog'), ('combined', 'interrupt'),
    ('tail', 'business'), ('write', 'business'),
])
@pytest.mark.parametrize('cleanup', ['wait', 'stdout', 'stderr', 'both-pipes'])
def test_capture_failure_survives_cleanup_faults_and_closes_both_pipes(monkeypatch, mode, failure_kind, cleanup):
    original = (TimeoutError('original read watchdog') if failure_kind == 'watchdog' else
                InterruptedError('original read interrupt') if failure_kind == 'interrupt' else
                RuntimeError('original capture failure'))
    _exercise(monkeypatch, mode, cleanup, original)


@pytest.mark.parametrize('mode', ['structured', 'combined', 'write'])
@pytest.mark.parametrize('cleanup', ['wait', 'stdout', 'stderr', 'both-pipes'])
def test_cleanup_failure_cannot_return_read_success_or_write_ack(monkeypatch, mode, cleanup):
    _exercise(monkeypatch, mode, cleanup, None)


@pytest.mark.parametrize('control', [TimeoutError('cleanup watchdog'), InterruptedError('cleanup interrupt')])
@pytest.mark.parametrize('mode', ['structured', 'combined'])
def test_cleanup_control_is_not_replaced_by_an_ordinary_cleanup_error(monkeypatch, control, mode):
    _exercise(monkeypatch, mode, 'wait-and-control', None, control=control)


@pytest.mark.parametrize('boundary', ['start', 'cancel_after_capture'])
def test_combined_eof_recovery_does_not_replace_start_or_cancellation_failure(monkeypatch, boundary):
    original = OSError('Cannot start child') if boundary == 'start' else InterruptedError('Read cancelled')
    popen, capture = subprocess.Popen, github_commands.capture_command
    children, starts = [], []
    captured = False

    def process(*args, **options):
        starts.append(1)
        if boundary == 'start':
            raise original
        child = popen(*args, **options)
        children.append(child)
        return child

    def completed(*args, **options):
        nonlocal captured
        result = capture(*args, **options)
        options['stdout'].eof = False
        captured = True
        return result

    def cancelled():
        if captured:
            raise original
        return False

    monkeypatch.setattr(github_commands.subprocess, 'Popen', process)
    monkeypatch.setattr(github_commands, 'capture_command', completed)
    with pytest.raises(type(original)) as caught:
        github_commands._run_bounded_command([sys.executable, '-I', '-c', "print('{}',flush=True)"],
            cwd=None, timeout=30, cancelled=cancelled, complete_stdout=True, complete_nonzero_stdout=True)
    assert caught.value is original and starts == [1]
    assert len(children) == (0 if boundary == 'start' else 1)
    assert all(child.returncode == 0 and child.stdout.closed and child.stderr.closed for child in children)


def _exercise(monkeypatch, mode, cleanup, original, *, control=None):
    popen = subprocess.Popen
    children, closed, grants = [], [], []
    secondary = OSError('ordinary secondary cleanup fault')

    def process(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        wait = child.wait
        fail_wait = cleanup in {'wait', 'wait-and-control'}

        def waited(*args, **kwargs):
            nonlocal fail_wait
            result = wait(*args, **kwargs)  # Reap the actual owned child first.
            if fail_wait:
                fail_wait = False
                raise secondary
            return result

        monkeypatch.setattr(child, 'wait', waited)
        for name in ('stdout', 'stderr'):
            stream = getattr(child, name)
            close = stream.close

            def finished(_name=name, _close=close):
                _close()  # Inject after actual close, never manufacture a proof.
                closed.append(_name)
                if cleanup == _name or cleanup == 'both-pipes':
                    raise secondary
                if cleanup == 'wait-and-control' and _name == 'stdout':
                    raise control

            monkeypatch.setattr(stream, 'close', finished)
        return child

    def completed(process, *, stdout, stderr, **kwargs):
        if original is not None:
            raise original
        # Controlled successful pump boundary, with actual child/output/EOF.
        popen.wait(process)
        stdout.feed(process.stdout.read())
        stderr.feed(process.stderr.read())
        if isinstance(stdout, CompleteOutput):
            stdout.eof = True

    if original is not None and mode in {'structured', 'combined'}:
        read = process_io.os.read
        def unreadable(fd, count):
            if children and fd == children[0].stdout.fileno():
                raise original
            return read(fd, count)
        monkeypatch.setattr(process_io.os, 'read', unreadable)
    else:
        monkeypatch.setattr(command, 'pump', completed)
    monkeypatch.setattr(github_commands.subprocess, 'Popen', process)
    arguments = [sys.executable, '-I', '-c', "print('{}',flush=True)"]
    expected = original if original is not None else control if control is not None else secondary
    try:
        with pytest.raises(type(expected)) as caught:
            if mode == 'write':
                github_commands.run_write_command(arguments, authorize=lambda: grants.append('authorized'))
            elif mode == 'combined':
                github_commands._run_bounded_command(arguments, cwd=None, timeout=30,
                    complete_stdout=True, complete_nonzero_stdout=True)
            else:
                github_commands.run_read_command(arguments, complete_stdout=mode == 'structured')
        assert caught.value is expected
        assert len(children) == 1 and children[0].returncode is not None
        assert closed == ['stdout', 'stderr']
        assert children[0].stdout.closed and children[0].stderr.closed
        assert grants == (['authorized'] if mode == 'write' else [])
    finally:
        for child in children:
            # Test rescue only; never contributes to the assertions above.
            if child.returncode is None:
                child.kill()
                popen.wait(child)
            for name in ('stdout', 'stderr'):
                stream = getattr(child, name)
                if not stream.closed:
                    type(stream).close(stream)
