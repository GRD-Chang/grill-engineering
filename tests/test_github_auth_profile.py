from __future__ import annotations

import json
import os
import socket
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

from agent_run.cli import main
from agent_run.github_auth_profile import (
    GitHubAppProfileStore,
    GitHubAuthProfileError,
)
import agent_run.worker_credentials as worker_credentials
from agent_run.worker_credentials import (
    HostGitHubReadChannel,
    ReadCredential,
    WorkerCredentialChannel,
    WorkerCredentialError,
)
from agent_run.worker_sandbox import WorkerSandboxError, worker_environment


def _private_key(path: Path, *, mode: int = 0o600) -> Path:
    path.write_bytes(_valid_private_key())
    path.chmod(mode)
    return path


@lru_cache(maxsize=1)
def _valid_private_key() -> bytes:
    return subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:1024",
        ],
        check=True,
        capture_output=True,
    ).stdout


def test_public_auth_configure_status_and_remove_work_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    key = _private_key(tmp_path / "app.pem")

    assert main(
        [
            "auth",
            "app",
            "configure",
            "--app-id",
            "123",
            "--installation-id",
            "456",
            "--private-key",
            str(key),
        ]
    ) == 0
    configured = json.loads(capsys.readouterr().out)
    assert configured == {"provider": "app", "result": "configured"}

    profile_path = tmp_path / "config" / "agent-run" / "github-app.json"
    assert json.loads(profile_path.read_text(encoding="utf-8")) == {
        "app_id": "123",
        "installation_id": "456",
        "private_key_path": str(key.resolve()),
    }
    assert (profile_path.parent.stat().st_mode & 0o777) == 0o700
    assert (profile_path.stat().st_mode & 0o777) == 0o600
    assert key.read_bytes() == _valid_private_key()

    assert main(["auth", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "app_profile": "configured",
        "provider": "app",
    }

    assert main(["auth", "app", "remove"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "app_profile": "not_configured",
        "provider": "host",
        "result": "removed",
    }
    assert not profile_path.exists()
    assert key.exists()


def test_invalid_reconfiguration_preserves_existing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    valid_key = _private_key(tmp_path / "valid.pem")
    invalid_key = _private_key(tmp_path / "invalid.pem", mode=0o644)
    store = GitHubAppProfileStore()
    store.configure(app_id="123", installation_id="456", private_key_path=str(valid_key))
    before = store.path.read_bytes()

    assert main(
        [
            "auth",
            "app",
            "configure",
            "--app-id",
            "789",
            "--installation-id",
            "987",
            "--private-key",
            str(invalid_key),
        ]
    ) == 2
    assert "私钥文件必须只允许当前用户访问" in capsys.readouterr().out
    assert store.path.read_bytes() == before


def test_public_auth_rejects_private_key_inside_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    outside_key = _private_key(tmp_path / "outside.pem")
    inside_key = _private_key(repository / "inside.pem")
    store = GitHubAppProfileStore()
    store.configure(
        app_id="123", installation_id="456", private_key_path=str(outside_key)
    )
    before = store.path.read_bytes()

    assert main(
        [
            "auth",
            "app",
            "configure",
            "--app-id",
            "789",
            "--installation-id",
            "987",
            "--private-key",
            str(inside_key),
        ]
    ) == 2
    assert "私钥文件必须位于 Git 仓库外" in capsys.readouterr().out
    assert store.path.read_bytes() == before


def test_invalid_signature_reconfiguration_preserves_existing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    valid_key = _private_key(tmp_path / "valid.pem")
    invalid_key = tmp_path / "invalid.pem"
    invalid_key.write_text("not a private key\n", encoding="utf-8")
    invalid_key.chmod(0o600)
    store = GitHubAppProfileStore()
    store.configure(app_id="123", installation_id="456", private_key_path=str(valid_key))
    before = store.path.read_bytes()

    assert main(
        [
            "auth",
            "app",
            "configure",
            "--app-id",
            "789",
            "--installation-id",
            "987",
            "--private-key",
            str(invalid_key),
        ]
    ) == 2
    assert "私钥无法完成本地签名校验" in capsys.readouterr().out
    assert store.path.read_bytes() == before


def test_directory_fsync_failure_restores_old_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_key = _private_key(tmp_path / "first.pem")
    second_key = _private_key(tmp_path / "second.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(first_key))
    before = store.path.read_bytes()
    failed = False

    def fail_once() -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise GitHubAuthProfileError("injected directory fsync failure")

    monkeypatch.setattr(store, "_fsync_directory", fail_once)
    with pytest.raises(GitHubAuthProfileError, match="injected directory fsync failure"):
        store.configure(
            app_id="789", installation_id="987", private_key_path=str(second_key)
        )

    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob(".github-app.*.tmp"))
    assert not list(store.path.parent.glob(".github-app.restore.*.tmp"))


def test_invalid_existing_profile_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "github-app.json"
    path.write_text(
        json.dumps(
            {
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": str(tmp_path / "missing.pem"),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(GitHubAuthProfileError, match="私钥文件不存在"):
        GitHubAppProfileStore(path).load()


def test_insecure_profile_directory_fails_closed(tmp_path: Path) -> None:
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    store.path.parent.chmod(0o755)

    with pytest.raises(GitHubAuthProfileError, match="目录权限必须为 700"):
        store.load()


def test_worker_environment_rejects_external_host_and_hides_authorized_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", "outside.example")
    with pytest.raises(WorkerSandboxError, match="host is not authorized"):
        worker_environment(tmp_path / "outside-gh", "reader")

    monkeypatch.setenv("GH_HOST", "github.com")
    environment = worker_environment(tmp_path / "github-gh", "reader")
    assert "GH_HOST" not in environment


def test_host_gh_channel_runs_one_allowlisted_read_without_token_retry(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls"
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(calls)!r}).write_text(' '.join(sys.argv[1:]))\n"
        "print('read-result')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    channel = HostGitHubReadChannel(gh_executable=str(executable))
    try:
        result = channel._request(  # noqa: SLF001 - broker request seam
            {"kind": "run", "arguments": ["issue", "view", "3"]}
        )
    finally:
        channel.close()

    assert result == {"returncode": 0, "stdout": "read-result\n", "stderr": ""}
    assert calls.read_text(encoding="utf-8") == "issue view 3"


def test_host_gh_channel_bounds_and_redacts_command_failures(tmp_path: Path) -> None:
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/bin/sh\n"
        "echo 'not logged in token=ghp_1234567890abcdef' >&2\n"
        "exit 4\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    channel = HostGitHubReadChannel(gh_executable=str(executable))
    try:
        result = channel._request(  # noqa: SLF001 - broker request seam
            {"kind": "run", "arguments": ["repo", "view"]}
        )
    finally:
        channel.close()

    assert result["returncode"] == 4
    assert "not logged in" in result["stderr"]
    assert "ghp_1234567890abcdef" not in result["stderr"]


def test_host_gh_channel_rejects_mutation_before_starting_gh(tmp_path: Path) -> None:
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\ntouch invoked\n", encoding="utf-8")
    executable.chmod(0o700)
    channel = HostGitHubReadChannel(gh_executable=str(executable))
    try:
        with pytest.raises(WorkerCredentialError, match="only permits read commands"):
            channel._request(  # noqa: SLF001 - broker request seam
                {"kind": "run", "arguments": ["issue", "edit", "3"]}
            )
    finally:
        channel.close()
    assert not (tmp_path / "invoked").exists()


def test_host_gh_channel_rejects_other_repository_before_starting_gh(
    tmp_path: Path,
) -> None:
    invoked = tmp_path / "invoked"
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/bin/sh\n"
        f"touch {invoked}\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    channel = HostGitHubReadChannel(
        gh_executable=str(executable), repository="owner/repository"
    )
    try:
        with pytest.raises(WorkerCredentialError, match="only permits read commands"):
            channel._request(  # noqa: SLF001 - broker request seam
                {
                    "kind": "run",
                    "arguments": [
                        "issue",
                        "list",
                        "--repo",
                        "other-owner/other-repository",
                    ],
                }
            )
    finally:
        channel.close()
    assert not invoked.exists()


def test_host_gh_channel_rejects_external_host_before_starting_gh(
    tmp_path: Path,
) -> None:
    invoked = tmp_path / "invoked"
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/bin/sh\n"
        f"touch {invoked}\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    channel = HostGitHubReadChannel(
        gh_executable=str(executable),
        gh_environment={"GH_HOST": "outside.example"},
        repository="owner/repository",
    )
    try:
        with pytest.raises(WorkerCredentialError, match="host is not authorized"):
            channel._request(  # noqa: SLF001 - broker request seam
                {"kind": "run", "arguments": ["issue", "view", "3"]}
            )
    finally:
        channel.close()
    assert not invoked.exists()


def test_app_gh_channel_rejects_external_host_before_minting_or_starting_gh(
    tmp_path: Path,
) -> None:
    invoked = tmp_path / "invoked"
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/bin/sh\n"
        f"touch {invoked}\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    provider_calls = 0

    def provider() -> ReadCredential:
        nonlocal provider_calls
        provider_calls += 1
        return ReadCredential("reader", 9999999999)

    channel = WorkerCredentialChannel(
        provider,
        gh_executable=str(executable),
        gh_environment={"GH_HOST": "outside.example"},
        repository="owner/repository",
    )
    try:
        with pytest.raises(WorkerCredentialError, match="host is not authorized"):
            channel._request(  # noqa: SLF001 - broker request seam
                {"kind": "run", "arguments": ["issue", "view", "3"]}
            )
    finally:
        channel.close()
    assert provider_calls == 0
    assert not invoked.exists()


def test_host_gh_channel_keeps_serving_after_oversize_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_credentials, "MAX_CHANNEL_MESSAGE_BYTES", 1024)
    marker = tmp_path / "first-call"
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if not marker.exists():\n"
        "    marker.touch()\n"
        "    sys.stdout.write('x' * 2048)\n"
        "    sys.stderr.write('y' * 2048)\n"
        "else:\n"
        "    print('normal-read')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    socket_path = tmp_path / "channel.sock"
    channel = HostGitHubReadChannel(gh_executable=str(executable))
    channel.start(socket_path)

    def request() -> dict[str, object]:
        payload = json.dumps(
            {"kind": "run", "arguments": ["issue", "view", "3"]}
        ).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(os.fspath(socket_path))
            connection.sendall(len(payload).to_bytes(4, "big") + payload)
            header = _read_socket_bytes(connection, 4)
            size = int.from_bytes(header, "big")
            return json.loads(_read_socket_bytes(connection, size))

    try:
        first = request()
        second = request()
        server_thread = channel._server_thread  # noqa: SLF001 - lifecycle seam
        assert first["error"] == (
            "Worker GitHub read response exceeded the size limit"
        )
        assert second == {
            "returncode": 0,
            "stdout": "normal-read\n",
            "stderr": "",
        }
        assert server_thread is not None and server_thread.is_alive()
    finally:
        channel.close()

    assert not socket_path.exists()
    assert server_thread is not None and not server_thread.is_alive()


def _read_socket_bytes(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    while size:
        chunk = connection.recv(size)
        if not chunk:
            raise AssertionError("socket closed before response completed")
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)
