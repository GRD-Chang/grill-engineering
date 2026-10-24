"""Long native probe responses retain only bounded, structured evidence."""
from pathlib import Path
import sys
import pytest

from agent_run.adapters.auth.identity import validate_worker_auth
from agent_run.errors import GitHubIdentityError, GitHubReadError


@pytest.mark.parametrize(('status', 'hint', 'padding', 'category', 'authorization_failed'), [
    (401, '', 0, 'unauthorized', True),
    (401, '', 5000, 'unauthorized', True),
    (403, 'X-RateLimit-Remaining: 0\r\n', 5000, 'rate_limited', False),
    (403, 'Retry-After: 30\r\n', 5000, 'rate_limited', False),
    (403, '', 5000, 'forbidden', True),
    (503, '', 5000, 'http_error', False),
])
def test_long_auth_headers_preserve_status_and_safe_hints(tmp_path, monkeypatch, status, hint, padding, category, authorization_failed):
    executable = tmp_path / 'gh'
    headers = f'HTTP/2.0 {status} Synthetic\r\n{hint}X-Long: ' + 'x' * padding + '\r\n\r\n'
    executable.write_text(f'#!{sys.executable}\nimport sys\nsys.stdout.write({headers!r})\nsys.stderr.write("token=synthetic-private-value")\nraise SystemExit(1)\n')
    executable.chmod(0o700)
    expected = GitHubIdentityError if authorization_failed else GitHubReadError
    with pytest.raises(expected) as caught:
        validate_worker_auth({'source': 'host'}, {'PATH': str(tmp_path), 'HOME': str(tmp_path)})
    cause = caught.value.__cause__
    assert cause.category == category and cause.http_status == status
    assert cause.authorization_failed is authorization_failed
    assert cause.returncode == 1
    assert 'synthetic-private-value' not in str(caught.value) + str(cause)
    assert len(str(cause).encode()) < 512


def test_body_cannot_supply_status_or_rate_headers(tmp_path, monkeypatch):
    executable = tmp_path / 'gh'
    output = 'HTTP/2.0 403 Forbidden\r\nX-Long: ' + 'x' * 5000 + '\r\n\r\nHTTP/2.0 401 Unauthorized\r\nRetry-After: 30\r\n'
    executable.write_text(f'#!{sys.executable}\nimport sys\nsys.stdout.write({output!r})\nraise SystemExit(1)\n')
    executable.chmod(0o700)
    with pytest.raises(GitHubIdentityError) as caught:
        validate_worker_auth({'source': 'host'}, {'PATH': str(tmp_path)})
    assert caught.value.__cause__.http_status == 403
    assert caught.value.__cause__.category == 'forbidden'


def test_header_collector_handles_split_chunks_without_retaining_values():
    from dataclasses import asdict
    from agent_run.adapters.auth.native_probe import AuthProbeCapture, _HeaderEvidence
    parser = _HeaderEvidence()
    stream = (b'HTTP/2.0 403 Forbidden\r\nx-long: ' + b'x' * 5000
              + b'\r\nx-ratelimit-remaining: 0\r\nAuthorization: synthetic-private-value\r\n\r\n'
              + b'HTTP/2.0 401 Spoof\r\n')
    for byte in stream:
        parser.feed(bytes([byte]))
    assert parser.http_status == 403 and parser.rate_limited
    assert parser.data == b'' and len(parser._prefix) <= 32
    assert asdict(AuthProbeCapture(1, False, parser.http_status, parser.rate_limited, False)) == {
        'returncode': 1, 'timed_out': False, 'http_status': 403,
        'rate_limited': True, 'network_failure': False,
    }


def test_early_network_marker_survives_long_stderr(tmp_path, monkeypatch):
    executable = tmp_path / 'gh'
    executable.write_text(f'#!{sys.executable}\nimport sys\nsys.stderr.write("dial tcp: network unavailable\\n" + "x" * 5000)\nraise SystemExit(1)\n')
    executable.chmod(0o700)
    with pytest.raises(GitHubReadError) as caught:
        validate_worker_auth({'source': 'host'}, {'PATH': str(tmp_path)})
    assert caught.value.__cause__.category == 'network'
    assert caught.value.__cause__.http_status is None


def test_non_header_prefix_cannot_later_supply_http_status(tmp_path, monkeypatch):
    executable = tmp_path / 'gh'
    output = 'not a header\r\n\r\nHTTP/2.0 401 Unauthorized\r\n'
    executable.write_text(f'#!{sys.executable}\nimport sys\nsys.stdout.write({output!r})\nraise SystemExit(1)\n')
    executable.chmod(0o700)
    with pytest.raises(GitHubReadError) as caught:
        validate_worker_auth({'source': 'host'}, {'PATH': str(tmp_path)})
    assert caught.value.__cause__.category == 'command_failed'
    assert caught.value.__cause__.http_status is None
