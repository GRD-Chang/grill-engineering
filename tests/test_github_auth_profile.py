from __future__ import annotations

import json
import os
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

from agent_run.github_auth_profile import (
    GitHubAppProfileStore,
    GitHubAuthProfileError,
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
