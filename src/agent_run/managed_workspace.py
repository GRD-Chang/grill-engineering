"""Independent, repository-scoped clones owned exclusively by the Runner."""

from __future__ import annotations

import fcntl
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from agent_run.git import GitRepository
from agent_run.git_errors import GitError
from agent_run.git_output import git_environment, run_git
from agent_run.paths import app_data_root
from agent_run.process_cleanup import terminate_process_group

_MARKER = ".agent-run-workspace.json"
_CLONE_TIMEOUT_SECONDS = 15 * 60


class ManagedWorkspaceError(GitError):
    """The Runner cannot safely open or create its independent clone."""


def normalize_repository(repository: str) -> str:
    parts = repository.split("/")
    if (
        len(parts) != 2
        or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", parts[0])
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[1])
        or parts[1] in {".", ".."}
    ):
        raise ManagedWorkspaceError("仓库必须使用 owner/name 格式")
    return repository.lower()


@dataclass(frozen=True)
class ManagedWorkspace:
    repository: str
    root: Path

    @classmethod
    def for_repository(cls, repository: str) -> ManagedWorkspace:
        canonical = normalize_repository(repository)
        return cls(canonical, app_data_root() / "repositories" / canonical)

    @property
    def repository_root(self) -> Path:
        return self.root / "repository"

    @property
    def state_root(self) -> Path:
        return self.root / "state"

    def open(self) -> GitRepository:
        """Validate an existing workspace without creating or updating anything."""
        validate_data_root()
        document = self._owned_document()
        git_directory = self.repository_root / ".git"
        roots = _command(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
            self.repository_root,
        ).splitlines()
        origin = _command(
            ["git", "remote", "get-url", "origin"], self.repository_root
        ).strip()
        if (
            roots != [str(git_directory), str(self.repository_root)]
            or origin != document["remote_url"]
        ):
            raise ManagedWorkspaceError("Runner 仓库的 Git 数据或远端被替换")
        return GitRepository(self.repository_root)

    def _owned_document(self) -> dict[str, str]:
        _check_owned_path(self.root)
        _check_owned_path(self.repository_root / ".git" / "objects")
        _check_owned_path(self.state_root)
        marker = self.root / _MARKER
        try:
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 16384:
                raise ManagedWorkspaceError("已有目录不属于 Runner 工作区")
            document = json.loads(marker.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or set(document) != {"repository", "remote_url"}
                or document["repository"] != self.repository
                or not isinstance(document["remote_url"], str)
            ):
                raise ManagedWorkspaceError("Runner 工作区标记与仓库身份不一致")
            git_directory = self.repository_root / ".git"
            if not git_directory.is_dir() or not self.state_root.is_dir():
                raise ManagedWorkspaceError("Runner 工作区不完整")
            if (git_directory / "objects" / "info" / "alternates").exists():
                raise ManagedWorkspaceError("Runner 仓库不能共享外部 Git objects")
            return {"repository": self.repository, "remote_url": document["remote_url"]}
        except (OSError, ValueError) as error:
            raise ManagedWorkspaceError("无法读取 Runner 工作区") from error

    def ensure(
        self, remote_url: str | None = None, *, identity: dict[str, str] | None = None,
    ) -> GitRepository:
        """Create once; never reset, fetch or modify an existing checkout."""
        _check_owned_path(self.root)
        if self.root.exists():
            return self.open()
        validate_data_root()
        _make_owned_directory(self.root.parent)
        lock = app_data_root() / "repositories" / ".locks" / (self.repository + ".lock")
        _make_owned_directory(lock.parent)
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ManagedWorkspaceError("正在创建该仓库的 Runner 工作区，请稍后重试") from error
            _check_owned_path(self.root)
            if self.root.exists():
                return self.open()
            staging = Path(tempfile.mkdtemp(prefix=f".{self.root.name}-", dir=self.root.parent))
            try:
                checkout = staging / "repository"
                command = (
                    ["git", "clone", "--no-local", "--quiet", "--", remote_url, str(checkout)]
                    if remote_url is not None else
                    ["gh", "repo", "clone", self.repository, str(checkout), "--", "--no-local", "--quiet"]
                )
                _command(command, staging, timeout=_CLONE_TIMEOUT_SECONDS)
                for key, value in (identity or {}).items():
                    if key not in {"user.name", "user.email"}:
                        raise ManagedWorkspaceError("不支持复制此 Git 配置")
                    _command(["git", "config", "--local", key, value], checkout)
                origin = _command(["git", "remote", "get-url", "origin"], checkout).strip()
                (staging / "state").mkdir(mode=0o700)
                (staging / _MARKER).write_text(
                    json.dumps({"repository": self.repository, "remote_url": origin}) + "\n",
                    encoding="utf-8",
                )
                _check_owned_path(self.root)
                if self.root.exists():
                    raise ManagedWorkspaceError("Runner 工作区目标已被占用")
                staging.rename(self.root)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return self.open()


def workspace_state_root(repository_root: Path) -> Path:
    return workspace_for_root(repository_root).state_root


def read_git_identity(directory: Path) -> dict[str, str]:
    """Read only author identity; never copy hooks, credentials or Git paths."""
    identity: dict[str, str] = {}
    for key in ("user.name", "user.email"):
        result = run_git(["git", "config", "--get", key], cwd=directory)
        if result.returncode == 0 and result.stdout.strip():
            identity[key] = result.stdout.strip()
    return identity


def workspace_for_root(repository_root: Path) -> ManagedWorkspace:
    """Accept only a canonical, owned clone; never fall back to a user checkout."""
    base = app_data_root() / "repositories"
    try:
        parts = repository_root.relative_to(base).parts
    except ValueError as error:
        raise ManagedWorkspaceError("该仓库不是 Runner 受管仓库") from error
    if len(parts) != 3 or parts[-1] != "repository":
        raise ManagedWorkspaceError("该仓库不是 Runner 受管仓库")
    workspace = ManagedWorkspace.for_repository("/".join(parts[:2]))
    if workspace.repository_root != repository_root:
        raise ManagedWorkspaceError("Runner 仓库路径不规范")
    workspace._owned_document()
    return workspace


def _check_owned_path(path: Path) -> None:
    base = app_data_root()
    try:
        relative = path.relative_to(base)
    except ValueError as error:
        raise ManagedWorkspaceError("Runner 工作区超出数据目录") from error
    current = base
    for component in ("", *relative.parts):
        current = current / component
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ManagedWorkspaceError(f"Runner 工作区路径不是独立目录：{current}")


def _make_owned_directory(path: Path) -> None:
    _check_owned_path(path)
    base = app_data_root()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = base
    for component in path.relative_to(base).parts:
        current = current / component
        current.mkdir(mode=0o700, exist_ok=True)


def validate_data_root() -> None:
    """Reject application data inside a user checkout, without creating paths."""
    ancestor = app_data_root()
    while not ancestor.exists():
        ancestor = ancestor.parent
    result = run_git(["git", "rev-parse", "--absolute-git-dir"], cwd=ancestor)
    if result.returncode != 0:
        return
    raise ManagedWorkspaceError("Runner 数据目录必须位于用户仓库之外，请调整 XDG_DATA_HOME")


def _command(arguments: list[str], cwd: Path, *, timeout: float = 30) -> str:
    environment = git_environment()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        process = subprocess.Popen(
            arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as error:
        raise ManagedWorkspaceError("无法启动 Runner 仓库操作，请检查 git/gh 安装") from error
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    assert process.stdout is not None and process.stderr is not None
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ManagedWorkspaceError("Runner 独立仓库操作超时；可重新运行")
                for key, _ in selector.select(timeout=min(remaining, 0.1)):
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(chunk)
                    del output[key.data][:-16384]
                if selector.get_map() and process.poll() is not None:
                    terminate_process_group(process)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        raise ManagedWorkspaceError("Runner 独立仓库操作超时；可重新运行") from error
    finally:
        terminate_process_group(process)
        process.stdout.close()
        process.stderr.close()
    if process.returncode != 0:
        diagnostic = output["stderr"].decode("utf-8", errors="replace").lower()
        reason = "请检查远端访问权限及 git/gh 认证"
        for needle, message in (
            ("repository not found", "仓库不存在或当前身份无权访问"),
            ("does not exist", "远端仓库不存在"),
            ("permission denied", "远端拒绝访问，请检查认证"),
            ("authentication failed", "远端认证失败"),
            ("could not resolve host", "无法解析远端主机"),
            ("failed to connect", "无法连接远端主机"),
            ("no space left", "数据目录磁盘空间不足"),
        ):
            if needle in diagnostic:
                reason = message
                break
        raise ManagedWorkspaceError(f"Runner 独立仓库操作失败（退出码 {process.returncode}）：{reason}")
    return output["stdout"].decode("utf-8")
