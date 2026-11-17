"""两个现有入口共享网络线索，保留各自的分类和信息边界。"""
import subprocess
import sys

import pytest

from agent_run.adapters import github_commands as commands
from agent_run.adapters.auth import identity, native_probe
from agent_run.errors import GitHubIdentityError, GitHubReadError
from agent_run.runtime.command import CommandCapture


def _auth_failure(monkeypatch, chunks, *, headers=b'', timed_out=False):
    def capture(process, *, stdout, stderr, **options):
        stdout.feed(headers)
        for chunk in chunks:
            stderr.feed(chunk)
        # Probe collectors must not retain arbitrary response data.
        assert stdout.data == stderr.data == b''
        return CommandCapture(1, b'', b'', timed_out)

    monkeypatch.setattr(identity.subprocess, 'Popen', lambda *args, **kwargs: object())
    monkeypatch.setattr(native_probe, 'capture_command', capture)
    with pytest.raises((GitHubReadError, GitHubIdentityError)) as caught:
        identity.validate_worker_auth({'source': 'host'}, {'PATH': '/synthetic'})
    assert caught.value.__context__ is None
    cause = caught.value.__cause__
    assert cause.__context__ is None
    assert 'private-payload' not in str(caught.value) + str(cause)
    assert len(str(cause)) < 200
    return cause


@pytest.mark.parametrize(('marker', 'category'), [
    ('dial tcp', 'network'),
    ('no such host', 'network'),
    ('network is unreachable', 'network'),
    ('connection refused', 'network'),
    ('connection reset', 'network'),
    ('i/o timeout', 'network'),
    ('TLS handshake timeout', 'network'),
    ('permission denied', 'command_failed'),
    ('connection restored', 'command_failed'),
    ('', 'command_failed'),
])
def test_existing_network_markers_from_both_entries_at_every_split(monkeypatch, marker, category):
    for text in (marker, marker.upper()):
        response = subprocess.CompletedProcess(['gh'], 1, '', text + ' private-payload')
        error = commands.read_response_error(response)
        assert f'category={category}' in str(error)
        assert 'private-payload' not in str(error)
        raw = text.encode()
        for split in range(len(raw) + 1):
            failure = _auth_failure(monkeypatch, [raw[:split], raw[split:], b' private-payload'])
            assert failure.category == category, (text, split)
            assert failure.http_status is None


@pytest.mark.parametrize(('status', 'hint', 'timed_out', 'category'), [
    (None, b'', False, 'network'),
    (None, b'secondary rate limit', False, 'network'),
    (401, b'', False, 'unauthorized'),
    (403, b'', False, 'forbidden'),
    (403, b'X-RateLimit-Remaining: 0\r\n', False, 'rate_limited'),
    (403, b'Retry-After: 30\r\n', False, 'rate_limited'),
    (403, b'secondary rate limit', False, 'rate_limited'),
    (403, b'rate limit exceeded', False, 'rate_limited'),
    (429, b'', False, 'http_error'),
    (503, b'', False, 'http_error'),
    (403, b'Retry-After: 30\r\n', True, 'timeout'),
])
def test_network_hint_preserves_distinct_entry_priorities(monkeypatch, status, hint, timed_out, category):
    headers = f'HTTP/2.0 {status} Synthetic\r\n'.encode() + hint + b'\r\n' if status else b''
    diagnostic = b'connection reset private-payload\n' + hint
    failure = _auth_failure(monkeypatch, [diagnostic], headers=headers, timed_out=timed_out)
    assert failure.category == category
    assert failure.http_status == status
    assert failure.authorization_failed is (category in {'unauthorized', 'forbidden'})
    assert failure.retryable is (category not in {'unauthorized', 'forbidden'})

    stderr = diagnostic.decode() + (f' HTTP {status}' if status else '')
    response = subprocess.CompletedProcess(['gh'], 1, '', stderr)
    error = commands.read_response_error(response)
    assert 'category=network' in str(error)
    assert ('HTTP ' in str(error)) is (status is not None)
    assert 'private-payload' not in str(error)
    assert 'category=invalid_json' in str(commands.read_response_error(response, invalid_json=True))


def test_generic_network_diagnostic_uses_only_bounded_stderr_tail(monkeypatch):
    monkeypatch.setattr(commands, 'MAX_COMMAND_OUTPUT_BYTES', 128)
    response = subprocess.CompletedProcess(['gh'], 1, 'connection reset', 'dial tcp' + 'x' * 128)
    assert 'category=command_failed' in str(commands.read_response_error(response))


def test_network_command_capture_redacts_before_classifying(monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'synthetic-private-payload')
    monkeypatch.setattr(commands, 'MAX_COMMAND_OUTPUT_BYTES', 128)
    response = commands.run_read_command([sys.executable, '-I', '-c',
        "import sys;print('synthetic-private-payload');"
        "sys.stderr.write('x'*200+'synthetic-private-payload connection reset')"])
    assert response.stdout == '[REDACTED]\n'
    assert response.stderr.endswith('[REDACTED] connection reset')
    assert len(response.stderr.encode()) <= 128
    assert 'synthetic-private-payload' not in response.stdout + response.stderr
    assert 'category=network' in str(commands.read_response_error(response))
