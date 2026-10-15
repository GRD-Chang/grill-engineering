from __future__ import annotations

import http.client
import http.server
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from agent_run.credential_lifecycle import RenewableCredential, ReadCredential
from agent_run.github_auth import _GitHubAppCredentialProvider, mint_installation_credential, GitHubCredentialError
from agent_run.github_auth_profile import GitHubAppProfile
from agent_run.worker_credentials import NativeCredentialDelivery

def test_app_credential_lifecycle_close_reclaims_a_blocked_signer_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation_path = tmp_path / "openssl-invocations"
    parent_pid_path = tmp_path / "signer.pid"
    child_pid_path = tmp_path / "signer-child.pid"
    signer = tmp_path / "openssl"
    signer.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        f"invocation_path = pathlib.Path({str(invocation_path)!r})\n"
        "count = int(invocation_path.read_text()) + 1 if invocation_path.exists() else 1\n"
        "invocation_path.write_text(str(count))\n"
        "if count <= 2:\n"
        "    sys.stdout.buffer.write(b'signature')\n"
        "    sys.stdout.flush()\n"
        "    raise SystemExit(0)\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os.setsid()\n"
        f"    pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    raise SystemExit(0)\n"
        f"pathlib.Path({str(parent_pid_path)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    signer.chmod(0o700)
    monkeypatch.setenv(
        "PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", "")
    )

    permissions = {
        "actions": "read",
        "checks": "read",
        "contents": "read",
        "issues": "read",
        "metadata": "read",
        "pull_requests": "read",
        "statuses": "read",
    }

    def fake_urlopen(_request: Any, timeout: float) -> io.BytesIO:
        assert timeout == 15.0
        return io.BytesIO(
            json.dumps(
                {
                    "token": "reader",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "permissions": permissions,
                }
            ).encode()
        )

    monkeypatch.setattr("agent_run.github_auth.urllib.request.urlopen", fake_urlopen)
    profile = GitHubAppProfile("123", "456", tmp_path / "private.pem")
    profile.private_key_path.write_text("private-key", encoding="utf-8")
    profile.private_key_path.chmod(0o600)
    credentials = RenewableCredential(
        _GitHubAppCredentialProvider(profile),
        renewal_margin=10**12,
        on_update=lambda credential: None,
    )
    unrelated: subprocess.Popen[str] | None = None
    renewal_thread: threading.Thread | None = None

    try:
        credentials.start()
        renewal_thread = credentials._renewal_thread  # noqa: SLF001 - lifecycle seam
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not (
                parent_pid_path.exists() and child_pid_path.exists()
            ):
                time.sleep(0.02)
            assert parent_pid_path.exists()
            assert child_pid_path.exists()
            assert int(invocation_path.read_text(encoding="utf-8")) >= 3
            assert renewal_thread is not None

            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            credentials.close()
            assert unrelated.poll() is None
        finally:
            if renewal_thread is not None and renewal_thread.is_alive():
                credentials.close()
    finally:
        if unrelated is not None and unrelated.poll() is None:
            unrelated.terminate()
        if unrelated is not None:
            unrelated.wait(timeout=5)

    assert renewal_thread is not None
    assert not renewal_thread.is_alive()
    assert credentials._credential is None  # noqa: SLF001 - lifecycle seam
    for pid_path in (parent_pid_path, child_pid_path):
        pid = int(pid_path.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_app_credential_lifecycle_close_after_network_timeout_joins_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_key = tmp_path / "private.pem"
    private_key.write_text("private-key", encoding="utf-8")
    private_key.chmod(0o600)
    profile = GitHubAppProfile("123", "456", private_key)
    timeout_seen = threading.Event()
    calls = 0

    def fake_jwt(*_arguments: Any, **_options: Any) -> str:
        return "jwt"

    def fake_urlopen(_request: Any, *, timeout: float) -> io.BytesIO:
        nonlocal calls
        assert timeout == 15.0
        calls += 1
        if calls > 1:
            timeout_seen.set()
            time.sleep(1.5)
            raise socket.timeout("issuer timeout")
        return io.BytesIO(
            json.dumps(
                {
                    "token": "reader",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "permissions": {
                        "actions": "read",
                        "checks": "read",
                        "contents": "read",
                        "issues": "read",
                        "metadata": "read",
                        "pull_requests": "read",
                        "statuses": "read",
                    },
                }
            ).encode()
        )

    monkeypatch.setattr("agent_run.github_auth._create_app_jwt", fake_jwt)
    monkeypatch.setattr("agent_run.github_auth.urllib.request.urlopen", fake_urlopen)
    credentials = RenewableCredential(
        _GitHubAppCredentialProvider(profile),
        renewal_margin=10**12,
        on_update=lambda credential: None,
    )
    renewal_thread: threading.Thread | None = None
    try:
        credentials.start()
        assert timeout_seen.wait(timeout=2)
        renewal_thread = credentials._renewal_thread  # noqa: SLF001 - lifecycle seam
    finally:
        credentials.close()

    assert calls >= 2
    assert renewal_thread is not None
    assert not renewal_thread.is_alive()
    assert credentials._credential is None  # noqa: SLF001 - lifecycle seam


@pytest.mark.parametrize("client_reads_first", [False, True])
def test_app_credential_lifecycle_close_interrupts_network_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client_reads_first: bool
) -> None:
    certificate = tmp_path / "localhost.crt"
    private_key = tmp_path / "localhost.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    response_started = threading.Event()
    client_read_started = threading.Event()
    release_response = threading.Event()
    calls = 0
    body = json.dumps(
        {
            "token": "reader",
            "expires_at": "2030-01-01T00:00:00Z",
            "permissions": {
                "actions": "read",
                "checks": "read",
                "contents": "read",
                "issues": "read",
                "metadata": "read",
                "pull_requests": "read",
                "statuses": "read",
            },
        }
    ).encode()

    class BlockingTokenHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal calls
            calls += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Test-Token-Request", str(calls))
            if calls > 1 and not client_reads_first:
                response_started.set()
            self.end_headers()
            if calls == 1:
                self.wfile.write(body)
                return
            if client_reads_first:
                if not client_read_started.wait(timeout=2):
                    return
                response_started.set()
            release_response.wait(timeout=30)
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BlockingTokenHandler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    original_request = urllib.request.Request

    def local_request(url: str, *arguments: Any, **options: Any) -> Any:
        return original_request(
            url.replace(
                "https://api.github.com",
                f"https://localhost:{server.server_port}",
                1,
            ),
            *arguments,
            **options,
        )

    monkeypatch.setattr("agent_run.github_auth.urllib.request.Request", local_request)
    monkeypatch.setattr(
        "agent_run.github_auth._create_app_jwt",
        lambda *_arguments, **_options: "jwt",
    )
    original_read = http.client.HTTPResponse.read

    def synchronized_read(
        response: http.client.HTTPResponse, *arguments: Any, **options: Any
    ) -> bytes:
        # Identify the renewal response itself: the server-side marker may be
        # published either before or after this client reaches body reading.
        if response.getheader("X-Test-Token-Request") == "2":
            client_read_started.set()
        return original_read(response, *arguments, **options)

    monkeypatch.setattr(http.client.HTTPResponse, "read", synchronized_read)
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    profile = GitHubAppProfile("123", "456", tmp_path / "private.pem")
    profile.private_key_path.write_text("private-key", encoding="utf-8")
    profile.private_key_path.chmod(0o600)
    credentials = RenewableCredential(
        _GitHubAppCredentialProvider(profile),
        renewal_margin=10**12,
        on_update=lambda credential: None,
    )
    renewal_thread: threading.Thread | None = None
    try:
        credentials.start()
        assert response_started.wait(timeout=2)
        assert client_read_started.wait(timeout=2)
        renewal_thread = credentials._renewal_thread  # noqa: SLF001 - lifecycle seam
        close_started = time.monotonic()
        credentials.close()
        assert time.monotonic() - close_started < 3
        # Prove cancellation joined the renewal while the response is still
        # withheld; teardown releasing the body must not satisfy this assertion.
        assert renewal_thread is not None
        assert not renewal_thread.is_alive()
    finally:
        release_response.set()
        credentials.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert calls >= 2
    assert renewal_thread is not None
    assert not renewal_thread.is_alive()
    assert credentials._credential is None  # noqa: SLF001 - lifecycle seam
    assert not server_thread.is_alive()


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("repository", [None, "acme/selected"])
def test_installation_scopes_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only: bool, repository: str | None,
) -> None:
    profile = GitHubAppProfile("123", "456", tmp_path / "key")
    profile.private_key_path.write_text("test-key")
    profile.private_key_path.chmod(0o600)
    monkeypatch.setattr("agent_run.github_auth._create_app_jwt", lambda *a, **kw: "jwt")
    requests: list[urllib.request.Request] = []
    granted = {"contents": "write", "metadata": "read", "workflows": "write",
               "profile": "write", "organization_projects": "admin"}
    expected = {"contents": "read", "metadata": "read", "organization_projects": "read"}
    issued: dict[str, dict[str, str]] = {}

    def respond(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        requests.append(request)
        if request.get_method() == "GET":
            body: dict[str, Any] = {"permissions": granted, "account": {"login": "acme"}}
        else:
            payload = json.loads(request.data)
            permissions = payload.get("permissions", granted)
            # Model the API's write-only fields; an invented read level is invalid.
            if any(key in permissions and permissions[key] != "write"
                   for key in ("workflows", "profile")):
                raise urllib.error.HTTPError(request.full_url, 422, "invalid permission", {}, None)
            issued["token-A"] = permissions
            body = {"token": "token-A", "expires_at": "2030-01-01T00:00:00Z",
                    "permissions": permissions}
            if repository:
                body["repositories"] = [{"full_name": repository}]
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr("agent_run.github_auth.urllib.request.urlopen", respond)
    credential = mint_installation_credential(profile, read_only=read_only, repository=repository)
    payload = json.loads(requests[-1].data)
    assert payload.get("permissions") == (expected if read_only else None)
    assert payload.get("repositories") == (["selected"] if repository else None)
    with NativeCredentialDelivery(lambda: credential, tmp_path / "gh") as delivery:
        delivery.start()
        token = json.loads((delivery.directory / "hosts.yml").read_text())["github.com"]["oauth_token"]
        assert issued[token] == (expected if read_only else granted)
    assert not (tmp_path / "gh" / "hosts.yml").exists()


@pytest.mark.parametrize("permissions", [
    {"contents": "write"},
    {"contents": "read", "workflows": "write"},
    {"contents": "read", "workflows": "read"},
    {"contents": "read", "profile": "write"},
    {"contents": "read", "profile": "read"},
])
def test_installation_response_cannot_broaden_selected_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, permissions: dict[str, str],
) -> None:
    profile = GitHubAppProfile("123", "456", tmp_path / "key")
    profile.private_key_path.write_text("test-key")
    profile.private_key_path.chmod(0o600)
    monkeypatch.setattr("agent_run.github_auth._create_app_jwt", lambda *a, **kw: "jwt")
    def respond(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        granted = {"contents": "write", "workflows": "write", "profile": "write"}
        body = {"permissions": granted if request.get_method() == "GET" else permissions,
                "account": {"login": "acme"}, "token": "secret",
                "expires_at": "2030-01-01T00:00:00Z"}
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr("agent_run.github_auth.urllib.request.urlopen", respond)
    with pytest.raises(GitHubCredentialError, match="read-only"):
        mint_installation_credential(profile, read_only=True)
    with pytest.raises(GitHubCredentialError, match="does not belong"):
        mint_installation_credential(profile, repository="other/repo")


def test_renewal_recovers_and_exhaustion_is_sanitized():
    now = [100.0]
    delivered = []
    updated = threading.Event()
    failure = threading.Event()
    errors = []
    calls = 0
    def provider():
        nonlocal calls
        calls += 1
        if calls in (2, 4, 5):
            raise RuntimeError("secret-sensitive-token")
        return ReadCredential("A" if calls == 1 else "B", now[0] + 20)
    def deliver(value):
        delivered.append(value.token)
        updated.set()
    lifecycle = RenewableCredential(provider, on_update=deliver,
        on_exhausted=lambda error: (errors.append(error), failure.set()),
        clock=lambda: now[0], renewal_margin=5, renewal_window=2)
    try:
        lifecycle.start()
        updated.clear()
        now[0] = 115
        lifecycle.wake()
        deadline = time.monotonic() + 2
        while calls < 2 and time.monotonic() < deadline:
            time.sleep(.001)
        assert calls == 2
        assert delivered == ["A"]
        now[0] = 116
        lifecycle.wake()
        assert updated.wait(2)
        assert delivered == ["A", "B"]
        now[0] = 131
        lifecycle.wake()
        deadline = time.monotonic() + 2
        while calls < 4 and time.monotonic() < deadline:
            time.sleep(.001)
        assert calls == 4
        now[0] = 137
        lifecycle.wake()
        assert failure.wait(2)
        assert "resume" in errors[0]
        assert "secret" not in errors[0]
    finally:
        lifecycle.close()
    assert not lifecycle._renewal_thread.is_alive()


def test_expired_old_token_stops_before_retry_window_ends():
    now = [10.0]
    called = threading.Event()
    exhausted = threading.Event()
    count = 0
    def provider():
        nonlocal count
        count += 1
        if count == 1:
            return ReadCredential("A", 12)
        called.set()
        raise OSError("private error")
    lifecycle = RenewableCredential(provider, on_update=lambda value: None,
        on_exhausted=lambda error: exhausted.set(), clock=lambda: now[0],
        renewal_margin=1, renewal_window=600)
    try:
        lifecycle.start()
        now[0] = 11
        lifecycle.wake()
        assert called.wait(2)
        now[0] = 12
        lifecycle.wake()
        assert exhausted.wait(2)
    finally:
        lifecycle.close()


def test_scope_instances_do_not_share_delivered_tokens():
    updates = [[], []]
    lifecycles = [RenewableCredential(lambda i=i: ReadCredential(str(i), 9999999999),
        on_update=lambda value, i=i: updates[i].append(value.token)) for i in range(2)]
    try:
        for lifecycle in lifecycles:
            lifecycle.start()
        assert updates == [["0"], ["1"]]
    finally:
        for lifecycle in lifecycles:
            lifecycle.close()


@pytest.mark.parametrize("provider, category", [
    (lambda: "", "invalid credential"),
    (lambda: (_ for _ in ()).throw(OSError("secret credential")), "provider unavailable"),
])
def test_initial_credential_failure_has_safe_actionable_category(provider, category):
    from agent_run.credential_lifecycle import WorkerCredentialError
    lifecycle = RenewableCredential(provider, on_update=lambda value: None)
    with pytest.raises(WorkerCredentialError, match=category) as caught:
        lifecycle.start()
    assert "secret" not in str(caught.value)
    assert lifecycle._renewal_thread is None


@pytest.mark.parametrize("kind", ["fifo", "public", "oversized"])
def test_private_key_input_is_bounded_and_private(tmp_path, kind):
    key = tmp_path / "key"
    if kind == "fifo":
        os.mkfifo(key, 0o600)
    else:
        key.write_bytes(b"x" * (65537 if kind == "oversized" else 10))
        key.chmod(0o644 if kind == "public" else 0o600)
    with pytest.raises(GitHubCredentialError, match="private key"):
        mint_installation_credential(GitHubAppProfile("123", "456", key))
