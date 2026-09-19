from __future__ import annotations

import fcntl
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_run.managed_workspace import (
    ManagedWorkspace,
    ManagedWorkspaceError,
    read_git_identity,
    workspace_state_root,
)
from agent_run.paths import app_data_root


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_workspace_clones_committed_private_files_and_reuses_its_own_git(
    git_repo: Path,
) -> None:
    (git_repo / "research").mkdir()
    (git_repo / "research" / "private.md").write_text("private committed context\n")
    (git_repo / "AGENTS.md").write_text("private instructions\n")
    git(git_repo, "add", "research", "AGENTS.md")
    git(git_repo, "commit", "-m", "private context")
    (git_repo / "README.md").write_text("uncommitted local edit\n")
    (git_repo / "local-only.md").write_text("not pushed\n")
    before = {
        str(path.relative_to(git_repo)): path.read_bytes()
        for path in git_repo.rglob("*") if path.is_file()
    }

    workspace = ManagedWorkspace.for_repository("Owner/Project")
    managed = workspace.ensure(remote_url=str(git_repo))

    assert ManagedWorkspace.for_repository("owner/project") == workspace
    assert managed.root == workspace.repository_root
    assert workspace_state_root(managed.root) == workspace.state_root
    assert workspace.state_root.parent == managed.root.parent
    assert (managed.root / "research" / "private.md").read_text() == "private committed context\n"
    assert (managed.root / "AGENTS.md").read_text() == "private instructions\n"
    assert (managed.root / "README.md").read_text() == "# fixture\n"
    assert not (managed.root / "local-only.md").exists()
    assert (managed.root / ".git").is_dir()
    assert not (managed.root / ".git" / "objects" / "info" / "alternates").exists()
    (managed.root / "README.md").write_text("managed unfinished work\n")
    assert workspace.ensure().root == managed.root
    assert (managed.root / "README.md").read_text() == "managed unfinished work\n"
    assert before == {
        str(path.relative_to(git_repo)): path.read_bytes()
        for path in git_repo.rglob("*") if path.is_file()
    }
    source_inodes = {path.stat().st_ino for path in (git_repo / ".git").rglob("*") if path.is_file()}
    assert not source_inodes.intersection(
        path.stat().st_ino for path in (managed.root / ".git").rglob("*") if path.is_file()
    )


def test_missing_workspace_lookup_is_read_only(git_repo: Path) -> None:
    workspace = ManagedWorkspace.for_repository("owner/project")
    with pytest.raises(ManagedWorkspaceError):
        workspace.open()
    with pytest.raises(ManagedWorkspaceError):
        workspace_state_root(git_repo)
    assert not workspace.root.exists()
    assert not (git_repo / ".agent-run").exists()


def test_failed_clone_removes_only_its_partial_checkout(tmp_path: Path) -> None:
    workspace = ManagedWorkspace.for_repository("owner/project")
    with pytest.raises(ManagedWorkspaceError, match="远端仓库不存在"):
        workspace.ensure(remote_url=str(tmp_path / "missing-remote"))
    assert not workspace.root.exists()
    assert not list(workspace.root.parent.iterdir())


def test_workspace_refuses_existing_unowned_directory(tmp_path: Path) -> None:
    workspace = ManagedWorkspace.for_repository("owner/project")
    workspace.root.mkdir(parents=True)
    sentinel = workspace.root / "user-file"
    sentinel.write_text("keep")
    with pytest.raises(ManagedWorkspaceError, match="不属于 Runner"):
        workspace.ensure(remote_url=str(tmp_path / "not-used"))
    assert sentinel.read_text() == "keep"


def test_workspace_refuses_symlink_destination(tmp_path: Path) -> None:
    workspace = ManagedWorkspace.for_repository("owner/project")
    workspace.root.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace.root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManagedWorkspaceError, match="不是独立目录"):
        workspace.ensure()
    assert not list(outside.iterdir())


def test_workspace_initialization_is_exclusive(tmp_path: Path) -> None:
    workspace = ManagedWorkspace.for_repository("owner/project")
    workspace.root.parent.mkdir(parents=True)
    lock_path = app_data_root() / "repositories" / ".locks" / "owner" / "project.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ManagedWorkspaceError, match="正在创建"):
            workspace.ensure(remote_url=str(tmp_path / "not-used"))
    assert not workspace.root.exists()


@pytest.mark.parametrize("relative", ["data", ".git/runner-data"])
def test_workspace_rejects_data_root_inside_a_user_repository(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, relative: str,
) -> None:
    data_home = git_repo / relative
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    workspace = ManagedWorkspace.for_repository("owner/project")
    with pytest.raises(ManagedWorkspaceError, match="必须位于用户仓库之外"):
        workspace.ensure(remote_url=str(git_repo))
    assert not data_home.exists()


def test_workspace_accepts_a_symlink_to_the_data_disk(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    disk = tmp_path / "data-disk"
    disk.mkdir()
    alias = tmp_path / "data-alias"
    alias.symlink_to(disk, target_is_directory=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(alias))
    workspace = ManagedWorkspace.for_repository("owner/project")

    managed = workspace.ensure(remote_url=str(git_repo))

    assert managed.root == disk / "agent-run/repositories/owner/project/repository"
    assert workspace.open().root == managed.root
    assert workspace.ensure().root == managed.root


@pytest.mark.parametrize("relative", ["data", ".git/runner-data"])
def test_existing_workspace_cannot_be_relocated_inside_a_user_repository(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, relative: str,
) -> None:
    workspace = ManagedWorkspace.for_repository("owner/project")
    workspace.ensure(remote_url=str(git_repo))
    data_home = git_repo / relative
    data_home.mkdir(parents=True)
    shutil.move(str(app_data_root()), str(data_home / "agent-run"))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    moved = ManagedWorkspace.for_repository("owner/project")
    before = {
        path.relative_to(git_repo): path.read_bytes()
        for path in git_repo.rglob("*") if path.is_file()
    }

    for action in (moved.open, moved.ensure):
        with pytest.raises(ManagedWorkspaceError, match="必须位于用户仓库之外"):
            action()

    assert before == {
        path.relative_to(git_repo): path.read_bytes()
        for path in git_repo.rglob("*") if path.is_file()
    }


def test_clone_timeout_removes_partial_workspace_and_releases_initialization_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import sys
    import agent_run.managed_workspace as module

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    fake_gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(module, "_CLONE_TIMEOUT_SECONDS", 0.05)
    workspace = ManagedWorkspace.for_repository("owner/project")
    with pytest.raises(ManagedWorkspaceError, match="超时") as caught:
        workspace.ensure()
    from agent_run.messages import error_detail

    assert error_detail(caught.value, "en") == (
        "The Runner independent repository operation timed out; rerun the command"
    )
    assert not workspace.root.exists()
    assert not list(workspace.root.parent.iterdir())
    with pytest.raises(ManagedWorkspaceError, match="远端仓库不存在"):
        workspace.ensure(remote_url=str(tmp_path / "missing"))


@pytest.mark.parametrize("repository", ["../project", "owner/..", "owner/", "/repo", "owner/repo/more"])
def test_workspace_rejects_invalid_repository_names(repository: str) -> None:
    with pytest.raises(ManagedWorkspaceError, match="owner/name"):
        ManagedWorkspace.for_repository(repository)


def test_workspace_accepts_github_dot_repository() -> None:
    assert ManagedWorkspace.for_repository("Owner/.github").repository == "owner/.github"


def test_workspace_copies_only_local_commit_identity_and_can_commit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_home = git_repo.parent / "identity-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_home / ".config"))
    for variable in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(variable, raising=False)
    for key in ("user.name", "user.email"):
        global_value = subprocess.run(
            ["git", "config", "--global", "--get", key], cwd=git_repo, capture_output=True,
        )
        assert global_value.returncode == 1
    git(git_repo, "config", "--local", "user.name", "Local Maintainer")
    git(git_repo, "config", "--local", "user.email", "local@example.invalid")
    hooks = git_repo / ".git" / "private-hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 97\n")
    hook.chmod(0o700)
    git(git_repo, "config", "--local", "core.hooksPath", str(hooks))
    git(git_repo, "config", "--local", "credential.helper", "!false")
    git(git_repo, "config", "--local", "agent-run.private-note", "local-only")
    before = {
        path.relative_to(git_repo): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in git_repo.rglob("*") if path.is_file()
    }

    identity = read_git_identity(git_repo)
    assert identity == {"user.name": "Local Maintainer", "user.email": "local@example.invalid"}
    workspace = ManagedWorkspace.for_repository("owner/project")
    managed = workspace.ensure(remote_url=str(git_repo), identity=identity)
    for key in ("core.hooksPath", "credential.helper", "agent-run.private-note"):
        local_value = subprocess.run(
            ["git", "config", "--local", "--get", key], cwd=managed.root, capture_output=True,
        )
        assert local_value.returncode == 1
    assert not (managed.root / ".git" / "private-hooks").exists()
    assert not (managed.root / ".git" / "hooks" / "pre-commit").exists()
    (managed.root / "candidate.txt").write_text("independent committed change\n")
    git(managed.root, "add", "candidate.txt")
    git(managed.root, "commit", "-m", "candidate using local identity")
    assert git(managed.root, "log", "-1", "--format=%an <%ae>") == "Local Maintainer <local@example.invalid>"
    workspace.ensure(identity={"user.name": "Different Maintainer"})
    assert git(managed.root, "config", "--local", "user.name") == "Local Maintainer"
    assert before == {
        path.relative_to(git_repo): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in git_repo.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("diagnostic,zh,en", [
    ("unknown failure", "请检查远端访问权限及 git/gh 认证", "Check remote access permissions and git/gh authentication"),
    ("repository not found", "仓库不存在或当前身份无权访问", "The repository does not exist or the current identity cannot access it"),
    ("does not exist", "远端仓库不存在", "The remote repository does not exist"),
    ("permission denied", "远端拒绝访问，请检查认证", "The remote denied access; check authentication"),
    ("authentication failed", "远端认证失败", "Remote authentication failed"),
    ("could not resolve host", "无法解析远端主机", "Cannot resolve the remote host"),
    ("failed to connect", "无法连接远端主机", "Cannot connect to the remote host"),
    ("no space left", "数据目录磁盘空间不足", "The data directory has insufficient disk space"),
])
def test_clone_failure_localizes_classified_reason_and_preserves_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diagnostic: str, zh: str, en: str,
) -> None:
    import os
    import sys
    from agent_run.messages import error_detail

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stderr.write({diagnostic!r})\nsys.exit(7)\n",
    )
    fake_gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    workspace = ManagedWorkspace.for_repository("owner/project")
    with pytest.raises(ManagedWorkspaceError) as caught:
        workspace.ensure()
    audit = f"Runner 独立仓库操作失败（退出码 7）：{zh}"
    assert str(caught.value) == audit
    assert error_detail(caught.value, "zh") == audit
    assert error_detail(caught.value, "en") == (
        f"Runner independent repository operation failed (exit code 7): {en}"
    )
    assert not workspace.root.exists()
    assert not list(workspace.root.parent.iterdir())
