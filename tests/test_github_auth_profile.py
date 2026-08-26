from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
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
    assert not list(profile_path.parent.glob(".github-app.*"))


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


@pytest.mark.parametrize("repository_kind", ["worktree", "linked", "bare"])
def test_public_auth_rejects_private_key_inside_any_git_repository_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    repository_kind: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    if repository_kind == "bare":
        subprocess.run(["git", "init", "--bare", str(repository)], check=True)
        key_parent = repository
    else:
        subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
        key_parent = repository
        if repository_kind == "linked":
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Test"],
                check=True,
            )
            (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-m", "initial"],
                check=True,
                capture_output=True,
            )
            linked = tmp_path / "linked"
            subprocess.run(
                ["git", "-C", str(repository), "worktree", "add", "--detach", str(linked)],
                check=True,
                capture_output=True,
            )
            key_parent = linked

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    outside_key = _private_key(tmp_path / "outside.pem")
    inside_key = _private_key(key_parent / "inside.pem")
    store = GitHubAppProfileStore()
    store.configure(app_id="123", installation_id="456", private_key_path=str(outside_key))
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


def test_auth_profile_symlink_is_rejected_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    target = tmp_path / "victim.json"
    target_bytes = b'{"keep":"exactly"}\n'
    target.write_bytes(target_bytes)
    target.chmod(0o600)
    profile_path = tmp_path / "config" / "agent-run" / "github-app.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.symlink_to(target)
    key = _private_key(tmp_path / "outside.pem")

    assert main(["auth", "status"]) == 2
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
    ) == 2
    assert main(["auth", "app", "remove"]) == 2

    assert profile_path.is_symlink()
    assert profile_path.readlink() == target
    assert target.read_bytes() == target_bytes
    assert (target.stat().st_mode & 0o777) == 0o600
    assert capsys.readouterr().out.count('"status": "invalid_auth_profile"') == 3


def test_auth_profile_directory_symlink_is_rejected_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    target_directory = tmp_path / "external-config"
    target_directory.mkdir()
    target = target_directory / "github-app.json"
    target_bytes = b'{"keep":"exactly"}\n'
    target.write_bytes(target_bytes)
    target.chmod(0o600)
    (config_home / "agent-run").symlink_to(target_directory, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    key = _private_key(tmp_path / "outside.pem")

    assert main(["auth", "status"]) == 2
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
    ) == 2
    assert main(["auth", "app", "remove"]) == 2

    assert (config_home / "agent-run").is_symlink()
    assert target.read_bytes() == target_bytes
    assert (target.stat().st_mode & 0o777) == 0o600
    assert capsys.readouterr().out.count('"status": "invalid_auth_profile"') == 3


def test_auth_rejects_relative_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")

    assert main(["auth", "status"]) == 2
    assert "XDG_CONFIG_HOME 必须是绝对路径" in capsys.readouterr().out
    assert not (tmp_path / "relative-config").exists()


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


def test_public_remove_directory_fsync_failure_preserves_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore()
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    before = store.path.read_bytes()
    before_mode = store.path.stat().st_mode & 0o777
    failed = False
    original_fsync = GitHubAppProfileStore._fsync_directory

    def fail_once(instance: GitHubAppProfileStore) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected remove directory fsync failure")
        original_fsync(instance)

    monkeypatch.setattr(GitHubAppProfileStore, "_fsync_directory", fail_once)

    assert main(["auth", "app", "remove"]) == 2
    assert "无法删除 GitHub App profile" in capsys.readouterr().out
    assert store.path.read_bytes() == before
    assert store.path.stat().st_mode & 0o777 == before_mode
    assert store.load() is not None
    assert key.exists()
    assert not list(store.path.parent.glob(".github-app.remove.*"))


def test_remove_rename_failure_after_swap_preserves_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    before = store.path.read_bytes()
    original_replace = os.replace

    def replace_then_fail(source: object, destination: object, *args: object) -> None:
        original_replace(source, destination, *args)
        if Path(source) == store.path:
            raise OSError("injected remove rename failure")

    monkeypatch.setattr(os, "replace", replace_then_fail)

    with pytest.raises(GitHubAuthProfileError, match="无法删除 GitHub App profile"):
        store.remove()

    assert store.path.read_bytes() == before
    assert store.load() is not None
    assert not list(store.path.parent.glob(".github-app.remove.*"))


def test_remove_delete_failure_after_swap_preserves_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    before = store.path.read_bytes()
    original_unlink = os.unlink
    failed = False

    def unlink_then_fail(path: object, *args: object, **kwargs: object) -> None:
        nonlocal failed
        original_unlink(path, *args, **kwargs)
        if not failed and Path(path).name.startswith(".github-app.remove."):
            failed = True
            raise OSError("injected remove delete failure")

    monkeypatch.setattr(os, "unlink", unlink_then_fail)

    with pytest.raises(GitHubAuthProfileError, match="无法删除 GitHub App profile"):
        store.remove()

    assert store.path.read_bytes() == before
    assert store.load() is not None
    assert not list(store.path.parent.glob(".github-app.remove.*"))


def test_remove_recovery_rename_failure_uses_exact_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    before = store.path.read_bytes()
    original_replace = os.replace
    calls = 0

    def fail_initial_and_recovery(
        source: object, destination: object, *args: object
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            original_replace(source, destination, *args)
            raise OSError("injected initial remove rename failure")
        if calls == 2:
            raise OSError("injected recovery rename failure")
        original_replace(source, destination, *args)

    monkeypatch.setattr(os, "replace", fail_initial_and_recovery)

    with pytest.raises(GitHubAuthProfileError, match="无法删除 GitHub App profile"):
        store.remove()

    assert calls == 2
    assert store.path.read_bytes() == before
    assert store.load() is not None
    assert not list(store.path.parent.glob(".github-app.remove.*"))


def test_remove_retries_when_snapshot_restore_link_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    before = store.path.read_bytes()
    original_replace = os.replace
    original_rename = os.rename
    original_link = os.link
    replace_calls = 0
    link_failed = False

    def fail_initial_and_first_recovery(
        source: object, destination: object, *args: object
    ) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            original_replace(source, destination, *args)
            raise OSError("injected initial remove rename failure")
        if replace_calls == 2:
            raise OSError("injected recovery rename failure")
        original_replace(source, destination, *args)

    def fail_link_once(source: object, destination: object, *args: object) -> None:
        nonlocal link_failed
        if not link_failed and Path(destination) == store.path:
            link_failed = True
            raise OSError("injected recovery link failure")
        original_link(source, destination, *args)

    def fail_recovery_rename(source: object, destination: object, *args: object) -> None:
        if Path(destination) == store.path:
            raise OSError("injected recovery rename fallback failure")
        original_rename(source, destination, *args)

    monkeypatch.setattr(os, "replace", fail_initial_and_first_recovery)
    monkeypatch.setattr(os, "rename", fail_recovery_rename)
    monkeypatch.setattr(os, "link", fail_link_once)

    with pytest.raises(GitHubAuthProfileError, match="无法删除 GitHub App profile"):
        store.remove()

    assert replace_calls == 2
    assert link_failed
    assert store.path.read_bytes() == before
    assert store.load() is not None
    assert not list(store.path.parent.glob(".github-app.remove.*"))


def test_remove_recovers_without_tombstone_when_snapshot_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    before = store.path.read_bytes()
    original_fsync = GitHubAppProfileStore._fsync_directory
    original_mkstemp = tempfile.mkstemp
    fsync_calls = 0

    def fail_after_delete(instance: GitHubAppProfileStore) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected remove commit fsync failure")
        original_fsync(instance)

    def fail_restore_mkstemp(*args: object, **kwargs: object) -> object:
        if kwargs.get("prefix") == ".github-app.restore.":
            raise OSError("injected recovery temporary-file failure")
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(GitHubAppProfileStore, "_fsync_directory", fail_after_delete)
    monkeypatch.setattr(tempfile, "mkstemp", fail_restore_mkstemp)

    with pytest.raises(GitHubAuthProfileError, match="无法删除 GitHub App profile"):
        store.remove()

    assert fsync_calls == 3
    assert store.path.read_bytes() == before
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.load() is not None
    assert not list(store.path.parent.glob(".github-app.remove.*"))


def test_remove_recovers_when_recovery_syscalls_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    before = store.path.read_bytes()
    original_replace = os.replace
    original_open = os.open
    original_link = os.link
    original_rename = os.rename

    def fail_replace(source: object, destination: object, *args: object) -> None:
        if Path(source) == store.path:
            original_replace(source, destination, *args)
            raise OSError("injected initial remove rename failure")
        if Path(destination) == store.path:
            raise OSError("injected recovery replace failure")
        original_replace(source, destination, *args)

    def fail_rename(source: object, destination: object, *args: object) -> None:
        if Path(destination) == store.path:
            raise OSError("injected recovery rename failure")
        original_rename(source, destination, *args)

    def fail_link(source: object, destination: object, *args: object) -> None:
        if Path(destination) == store.path:
            raise OSError("injected recovery link failure")
        original_link(source, destination, *args)

    def fail_profile_open(path: object, *args: object, **kwargs: object) -> int:
        if Path(path) == store.path:
            raise OSError("injected recovery open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(os, "rename", fail_rename)
    monkeypatch.setattr(os, "link", fail_link)
    monkeypatch.setattr(os, "open", fail_profile_open)

    with pytest.raises(GitHubAuthProfileError, match="无法删除 GitHub App profile"):
        store.remove()

    assert store.path.read_bytes() == before
    assert store.load() is not None
    assert not list(store.path.parent.glob(".github-app.*"))


def test_remove_commit_fsync_failure_preserves_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _private_key(tmp_path / "app.pem")
    store = GitHubAppProfileStore(tmp_path / "config" / "github-app.json")
    store.configure(app_id="123", installation_id="456", private_key_path=str(key))
    before = store.path.read_bytes()
    original_fsync = GitHubAppProfileStore._fsync_directory
    fsync_calls = 0

    def fail_final_fsync(instance: GitHubAppProfileStore) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 3:
            raise OSError("injected remove commit fsync failure")
        original_fsync(instance)

    monkeypatch.setattr(GitHubAppProfileStore, "_fsync_directory", fail_final_fsync)

    with pytest.raises(GitHubAuthProfileError, match="无法删除 GitHub App profile"):
        store.remove()

    assert fsync_calls == 4
    assert store.path.read_bytes() == before
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.load() is not None
    assert not list(store.path.parent.glob(".github-app.*"))


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
