"""Live transport/auth contracts independent of retired runtime fixture imports.

Function bodies and local helpers are retained from the fixed 259acd4 baseline.
"""
from __future__ import annotations

from agent_run.contracts.write_authority import WriteAuthority

import base64
from collections.abc import Iterator
from contextlib import contextmanager
import os
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.request

import pytest

from support.github_checks import repository_response

from agent_run.adapters.github import GhGitHubReader
from agent_run.adapters.auth.app import GitHubCredentialError, _CancellableHTTPSHandler, _SigningProcessRegistry, _create_app_jwt, mint_installation_credential
from agent_run.config.github_app import (
    GitHubAppProfile, GitHubAppProfileStore, GitHubAuthProfileError,
    _is_inside_git_repository,
)
from agent_run.adapters.github_commands import MAX_COMMAND_OUTPUT_BYTES, run_read_command


def test_github_reader_retries_transient_timeout(
    git_repo: Path, monkeypatch
) -> None:
    attempts = 0

    def fake_run(arguments, **_kwargs):
        nonlocal attempts
        if "repo" in arguments:
            attempts += 1
            if attempts < 3:
                return subprocess.CompletedProcess(
                    arguments, 1, "", "dial tcp: i/o timeout"
                )
        return _repository_response(arguments)

    monkeypatch.setattr('agent_run.adapters.github_commands._run_bounded_command', fake_run)
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _seconds: None)

    reader = GhGitHubReader("example/project")
    assert reader.repository().name_with_owner == "example/project"
    assert attempts == 3


def test_github_command_capture_has_a_direct_byte_bound() -> None:
    result = run_read_command(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.stdout.write('x' * ({MAX_COMMAND_OUTPUT_BYTES} + 4096) + 'out-tail'); "
                f"sys.stderr.write('y' * ({MAX_COMMAND_OUTPUT_BYTES} + 4096) + 'err-tail')"
            ),
        ]
    )

    assert result.returncode == 0
    assert result.stdout == "x" * (MAX_COMMAND_OUTPUT_BYTES - 8) + "out-tail"
    assert result.stderr == "y" * (MAX_COMMAND_OUTPUT_BYTES - 8) + "err-tail"


@pytest.mark.parametrize("stderr", ["permission denied", "unexpected response"])
def test_github_read_retries_do_not_classify_stderr(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    attempts = 0

    def fake_run(arguments, **_kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(arguments, 1, "", stderr)

    monkeypatch.setattr('agent_run.adapters.github_commands._run_bounded_command', fake_run)
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _seconds: None)

    result = run_read_command(["gh", "api", "repos/example/project"])

    assert result.stderr == stderr
    assert attempts == 3


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:example/project.git", "example/project"),
        ("https://github.com/example/project.git", "example/project"),
        ("https://gitlab.com/example/project.git", None),
    ],
)
def test_github_reader_derives_repository_hint_from_origin(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: str,
    expected: str | None,
) -> None:
    monkeypatch.setattr(
        'agent_run.adapters.github.subprocess.run',
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git"], 0, f"{remote}\n", ""
        ),
    )

    assert GhGitHubReader(working_directory=git_repo).repository_hint() == expected


def test_remote_merge_readback_returns_integrated_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from agent_run.adapters.git_objects import LocalGit
    from support.remote_effects import _fixture, _publish_pr

    port, git, push, _state, calls = _fixture(tmp_path, monkeypatch)
    source_ref = "refs/heads/agent-run-repair/run-1"
    target_ref = "refs/heads/agent-run/run-1"
    git.run("push", "origin", push.base.sha + ":" + source_ref, push.base.sha + ":" + target_ref)
    push = replace(push, source_ref=source_ref, target_ref=target_ref)
    _original, pr = _publish_pr(port, push)
    parents = [push.base.sha, push.after_sha]
    merge = replace(push, effect_id="merge-readback", operation="merge", pr_number=pr.number,
                    payload={"method": "merge", "parents": parents, "commit_message": "Reviewed merge"})
    assert port.execute(merge, WriteAuthority(lambda: lambda: None)).disposition == "target"
    remote = LocalGit(tmp_path / "origin.git")
    integrated = remote.commit(target_ref)
    before = len(calls.read_text().splitlines())

    readback = port.reconcile(merge)
    live = readback.pr

    assert readback.disposition == "target" and readback.commit.sha == integrated.sha
    assert live.number == pr.number and live.state == "MERGED"
    assert live.source_ref.removeprefix("refs/heads/") == "agent-run-repair/run-1"
    assert live.head_repository == live.repository == "example/project"
    assert live.head.tree == push.candidate.tree
    assert live.merged.tree == integrated.tree
    assert readback.raw["parents"] == parents
    assert remote.run("rev-list", "--parents", "-n", "1", integrated.sha).stdout.split()[1:] == parents
    assert live.base.sha == parents[0]
    observed = [json.loads(line) for line in calls.read_text().splitlines()][before:]
    assert any(command[:2] == ["api", f"repos/example/project/git/commits/{integrated.sha}"] for command in observed)
    assert not any("--method" in command or "--input" in command for command in observed)


def test_github_reader_retries_transient_http_status(
    git_repo: Path, monkeypatch
) -> None:
    attempts = 0

    def fake_run(arguments, **_kwargs):
        nonlocal attempts
        if "repo" in arguments:
            attempts += 1
            if attempts < 3:
                return subprocess.CompletedProcess(
                    arguments, 1, "", "HTTP 503: upstream unavailable"
                )
        return _repository_response(arguments)

    monkeypatch.setattr('agent_run.adapters.github_commands._run_bounded_command', fake_run)
    monkeypatch.setattr('agent_run.adapters.github_commands.time.sleep', lambda _seconds: None)

    reader = GhGitHubReader("example/project")
    assert reader.repository().name_with_owner == "example/project"
    assert attempts == 3


def _repository_response(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return repository_response(arguments[1:], repository="example/project", default_branch="main",
                               refs={"refs/heads/main": "a" * 40})


@contextmanager
def _synthetic_app_profile() -> Iterator[GitHubAppProfile]:
    """Use the configured test temp root without falling back past a failed guard."""
    prerequisite = "Test environment prerequisite: TMPDIR must be an existing, writable directory outside Git"
    try:
        configured_root = os.environ.get("TMPDIR")
        root = Path(configured_root if configured_root is not None else tempfile.gettempdir())
        if not root.is_absolute() or not root.is_dir():
            raise RuntimeError(prerequisite)
        root = root.resolve(strict=True)
        # The original guard checks this candidate's parent and all ancestors.
        if _is_inside_git_repository(root / "private.pem"):
            raise RuntimeError(prerequisite)
        temporary = tempfile.TemporaryDirectory(prefix="agent-run-http-status-", dir=root)
    except OSError as error:
        raise RuntimeError(prerequisite) from error
    with temporary as directory:
        private_key = Path(directory) / "private.pem"
        try:
            # Validate the real 0600 file before writing synthetic key bytes.
            private_key.touch(mode=0o600)
            private_key.chmod(0o600)
            profile = GitHubAppProfileStore._validate_values("123", "456", str(private_key))
            private_key.write_text("private-key", encoding="utf-8")
        except (OSError, GitHubAuthProfileError) as error:
            raise RuntimeError(prerequisite) from error
        yield profile


def test_github_app_token_mint_extracts_only_the_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('agent_run.adapters.auth.app._create_app_jwt', lambda *_: "jwt")
    response_reached = False

    def reject(*_args: object, **_kwargs: object) -> object:
        nonlocal response_reached
        response_reached = True
        raise urllib.error.HTTPError(
            "https://api.github.com/private-response",
            429,
            "rate limited",
            {},
            None,
        )

    monkeypatch.setattr('agent_run.adapters.auth.app.urllib.request.urlopen', reject)
    with _synthetic_app_profile() as profile:
        directory = profile.private_key_path.parent
        with pytest.raises(GitHubCredentialError) as raised:
            mint_installation_credential(profile)

    assert response_reached
    assert raised.value.http_status == 429
    assert "private-response" not in str(raised.value)
    assert not Path(directory).exists()


@pytest.mark.parametrize("invalid_root", ["missing", "file", "git"])
def test_http_status_fixture_rejects_unsafe_temp_root_before_key_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_root: str,
) -> None:
    root = tmp_path / "key-root"
    if invalid_root == "file":
        root.write_text("not a directory", encoding="utf-8")
    elif invalid_root == "git":
        root.mkdir()
        (root / ".git").mkdir()
    monkeypatch.setenv("TMPDIR", str(root))
    before = set(root.iterdir()) if root.is_dir() else None

    def unexpected_allocation(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unsafe test temp root must be rejected before allocation")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", unexpected_allocation)
    with pytest.raises(RuntimeError, match="Test environment prerequisite: TMPDIR"):
        with _synthetic_app_profile():
            pytest.fail("unsafe test temp root must not yield a profile")

    assert (set(root.iterdir()) if root.is_dir() else None) == before
    assert root.exists() == (invalid_root != "missing")


def test_app_jwt_is_rs256_and_cleans_temporary_key(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    private_key = tmp_path / "private.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        capture_output=True,
    )
    before = set(Path(tempfile.gettempdir()).glob("agent-run-app-key-*"))
    monkeypatch.setattr('agent_run.adapters.auth.app.time.time', lambda: 1_800_000_000)

    token = _create_app_jwt("123", private_key.read_text(encoding="utf-8"))

    header_part, payload_part, signature_part = token.split(".")
    header = json.loads(_decode_base64url(header_part))
    payload = json.loads(_decode_base64url(payload_part))
    signature = tmp_path / "signature.bin"
    signature.write_bytes(_decode_base64url(signature_part))
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public_key),
            "-signature",
            str(signature),
        ],
        input=f"{header_part}.{payload_part}".encode(),
        capture_output=True,
        check=False,
    )

    assert verified.returncode == 0
    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload == {
        "exp": 1_800_000_540,
        "iat": 1_799_999_940,
        "iss": "123",
    }
    assert set(Path(tempfile.gettempdir()).glob("agent-run-app-key-*")) == before


def test_app_jwt_signing_failure_does_not_expose_openssl_stderr(
    monkeypatch: Any,
) -> None:
    def failed_signing(*arguments: Any, **options: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            list(arguments[0]),
            1,
            b"",
            b"Provider routines::bad decrypt private-key-material",
        )

    monkeypatch.setattr('agent_run.adapters.auth.app.subprocess.run', failed_signing)

    with pytest.raises(GitHubCredentialError, match="could not sign GitHub App JWT") as error:
        _create_app_jwt("123", "private-key")

    assert "private-key-material" not in str(error.value)


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_signing_registry_reclaims_resources_registered_after_cancel() -> None:
    class Transport:
        shutdown_called = False
        close_called = False

        def shutdown(self, _how: int) -> None:
            self.shutdown_called = True

        def close(self) -> None:
            self.close_called = True

    class Response:
        def __init__(self, transport: Transport) -> None:
            self.fp = type(
                "FileObject",
                (),
                {"raw": type("RawSocket", (), {"_sock": transport})()},
            )()
            self.closed = False

        def close(self) -> None:
            assert self.fp.raw._sock.shutdown_called
            self.closed = True

    registry = _SigningProcessRegistry()
    registry.cancel()
    transport = Transport()
    response = Response(transport)

    registry.register_response(response)

    assert transport.shutdown_called
    assert transport.close_called
    assert response.closed


def test_signing_registry_cancels_before_https_connection_can_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    connection_seen = threading.Event()
    stop_accept = threading.Event()

    def accept_connection() -> None:
        listener.settimeout(0.05)
        while not stop_accept.is_set():
            try:
                accepted, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            connection_seen.set()
            accepted.close()
            return

    accept_thread = threading.Thread(target=accept_connection)
    accept_thread.start()
    registry = _SigningProcessRegistry()
    handler = _CancellableHTTPSHandler(registry)
    connection_ready = threading.Event()
    release_connection = threading.Event()
    original_open_connection = handler._open_connection

    def paused_open_connection(host: str, **options: Any) -> Any:
        connection = original_open_connection(host, **options)
        connection_ready.set()
        assert release_connection.wait(timeout=2)
        return connection

    monkeypatch.setattr(handler, "_open_connection", paused_open_connection)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), handler)
    request = urllib.request.Request(
        f"https://127.0.0.1:{listener.getsockname()[1]}/token",
        data=b"{}",
        method="POST",
    )
    errors: list[BaseException] = []

    def open_request() -> None:
        try:
            opener.open(request, timeout=15)
        except BaseException as error:
            errors.append(error)

    request_thread = threading.Thread(target=open_request)
    request_thread.start()
    try:
        assert connection_ready.wait(timeout=2)
        cancel_started = time.monotonic()
        registry.cancel()
        release_connection.set()
        request_thread.join(timeout=2)
        assert time.monotonic() - cancel_started < 3
        assert not request_thread.is_alive()
        assert errors
        assert not connection_seen.is_set()
    finally:
        release_connection.set()
        registry.cancel()
        request_thread.join(timeout=2)
        stop_accept.set()
        listener.close()
        accept_thread.join(timeout=2)

    assert not accept_thread.is_alive()
