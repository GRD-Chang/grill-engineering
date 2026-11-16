"""Structured read success requires original complete bytes; writes retain tails."""
import json
import sys

import pytest

from support.github_retries import set_read_retry_sleep

from agent_run.adapters import github_commands as commands
from agent_run.errors import GitHubReadError
from agent_run.adapters.github import GhGitHubReader


def _structured_read(monkeypatch, code, attempts=None):
    bounded = commands._run_bounded_command

    def invoke(arguments, **options):
        if attempts is not None:
            attempts.append("read")
        return bounded([sys.executable, "-I", "-c", code], **options)

    monkeypatch.setattr(commands, "_run_bounded_command", invoke)
    return GhGitHubReader("example/project")._gh_json("api", "graphql")


@pytest.mark.parametrize('output', ['oversized-valid-tail', 'invalid-utf8'])
def test_structured_read_never_accepts_a_valid_json_tail_or_replacement_character(monkeypatch, output):
    monkeypatch.setattr(commands, 'MAX_COMMAND_OUTPUT_BYTES', 128)
    code = ("import sys;sys.stdout.buffer.write(b' '*128+b'{\"ok\":true}')" if output == 'oversized-valid-tail'
            else "import sys;sys.stdout.buffer.write(b'{\"text\":\"\\xff\"}')")
    attempts = []
    with pytest.raises(GitHubReadError) as caught:
        _structured_read(monkeypatch, code, attempts)
    assert caught.value.code == 'github_invalid_response'
    assert attempts == ['read']


@pytest.mark.parametrize('error', [OSError('unreadable stdout'), TimeoutError('watchdog'), InterruptedError('interrupted')])
def test_read_error_is_not_eof_and_keeps_original_exception_after_child_cleanup(monkeypatch, error):
    from agent_run.runtime import process_io
    real_popen, read = commands.subprocess.Popen, process_io.os.read
    children = []

    def process(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def unavailable(fd, count):
        if children and not children[-1].stdout.closed and fd == children[-1].stdout.fileno():
            raise error
        return read(fd, count)

    monkeypatch.setattr(commands.subprocess, 'Popen', process)
    monkeypatch.setattr(process_io.os, 'read', unavailable)
    with pytest.raises(type(error)) as caught:
        _structured_read(monkeypatch, "print('{}',flush=True)")
    assert caught.value is error
    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdout.closed and children[0].stderr.closed


@pytest.mark.parametrize('control', [None, TimeoutError('stderr watchdog'), InterruptedError('stderr interrupted')])
def test_stderr_stays_diagnostic_without_swallowing_read_control(monkeypatch, control):
    from agent_run.runtime import process_io
    popen, read = commands.subprocess.Popen, process_io.os.read
    children = []
    failure = control or OSError('unreadable diagnostic pipe')

    def process(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def diagnostic(fd, count):
        if children and not children[-1].stderr.closed and fd == children[-1].stderr.fileno():
            raise failure
        return read(fd, count)

    monkeypatch.setattr(commands.subprocess, 'Popen', process)
    monkeypatch.setattr(process_io.os, 'read', diagnostic)
    call = [sys.executable, '-I', '-c', "import sys;print('{}',flush=True);sys.stderr.write('diagnostic')"]
    if control is None:
        assert commands.run_read_command(call, complete_stdout=True).stdout == '{}\n'
    else:
        with pytest.raises(type(control)) as caught:
            commands.run_read_command(call, complete_stdout=True)
        assert caught.value is control
    assert len(children) == 1 and children[0].returncode is not None
    assert children[0].stdout.closed and children[0].stderr.closed


def test_read_byte_budget_is_exact_and_stderr_keeps_only_its_tail(monkeypatch):
    monkeypatch.setattr(commands, 'MAX_COMMAND_OUTPUT_BYTES', 128)
    body = json.dumps({'data': 'x' * 116})
    assert len(body.encode()) == 128
    code = f'import sys;sys.stdout.write({body!r});sys.stderr.write("y"*200+"tail")'
    result = commands.run_read_command([sys.executable, '-I', '-c', code], complete_stdout=True)
    assert result.stdout == body and result.stderr == 'y' * 124 + 'tail'


def test_nonzero_read_and_write_ack_keep_original_tail_and_replacement_contract(monkeypatch):
    monkeypatch.setattr(commands, 'MAX_COMMAND_OUTPUT_BYTES', 128)
    set_read_retry_sleep(monkeypatch, lambda _: None)
    body = "import sys;sys.stdout.buffer.write(b'x'*200+b'\\xff');raise SystemExit(1)"
    read = commands.run_read_command([sys.executable, '-I', '-c', body], complete_stdout=True)
    writes = []
    write = commands.run_write_command([sys.executable, '-I', '-c', body], authorize=lambda: writes.append('write'))
    assert read.returncode == write.returncode == 1
    assert read.stdout == write.stdout == 'x' * 127 + '\ufffd'
    assert writes == ['write']


def test_successful_write_ack_still_has_tail_and_replacement_decoding(monkeypatch):
    monkeypatch.setattr(commands, 'MAX_COMMAND_OUTPUT_BYTES', 128)
    result = commands.run_write_command([sys.executable, '-I', '-c',
        "import sys;sys.stdout.buffer.write(b'x'*200+b'\\xff')"], authorize=lambda: None)
    assert result.returncode == 0 and result.stdout == 'x' * 127 + '\ufffd'


def test_command_timeout_keeps_its_retry_budget_and_priority_over_malformed_output(monkeypatch):
    from dataclasses import replace
    capture = commands.capture_command
    calls = []

    def expired(*args, **kwargs):
        result = capture(*args, **kwargs)
        calls.append(result.returncode)
        return replace(result, timed_out=True)  # Controlled command-deadline result.

    monkeypatch.setattr(commands, 'capture_command', expired)
    set_read_retry_sleep(monkeypatch, lambda _: None)
    result = commands.run_read_command([sys.executable, '-I', '-c',
        "import sys;sys.stdout.buffer.write(b'{\"text\":\"\\xff\"}')"], complete_stdout=True)
    assert calls == [0, 0, 0] and result.returncode == 1
    assert commands.read_response_error(result).code == 'github_read_failed'
    assert 'category=timeout' in str(commands.read_response_error(result))


def test_cancellation_keeps_priority_over_incomplete_output():
    with pytest.raises(InterruptedError, match='cancelled'):
        commands.run_read_command([sys.executable, '-I', '-c', "print('{}')"], cancelled=lambda: True, complete_stdout=True)
