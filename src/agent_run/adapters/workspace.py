"""Independent, repository-scoped clones owned exclusively by the Runner."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent_run.runtime.process_io import TailOutput, pump
from agent_run.errors import error_message
from agent_run.errors import GitError
from agent_run.runtime.git_command import git_environment, run_git
from agent_run.config.paths import app_data_root
from agent_run.contracts.repository import normalize_repository as _normalize_repository
from agent_run.runtime.process_cleanup import terminate_process_group

_MARKER = ".agent-run-workspace.json"
_CLONE_TIMEOUT_SECONDS = 15 * 60


class ManagedWorkspaceError(GitError):
    """The Runner cannot safely open or create its independent clone."""


def normalize_repository(repository: str) -> str:
    try:
        return _normalize_repository(repository)
    except ValueError as error:
        raise ManagedWorkspaceError(error_message("cli.error.workspace_repository_format")) from error


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

    def open_path(self) -> Path:
        """Validate an existing workspace without loading the legacy runtime."""
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
            raise ManagedWorkspaceError(error_message("workspace.error.git_replaced"))
        return self.repository_root

    def _owned_document(self) -> dict[str, str]:
        _check_owned_path(self.root)
        _check_owned_path(self.repository_root / ".git" / "objects")
        _check_owned_path(self.state_root)
        marker = self.root / _MARKER
        try:
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 16384:
                raise ManagedWorkspaceError(error_message("workspace.error.unowned"))
            document = json.loads(marker.read_text(encoding="utf-8"))
            if (
                not isinstance(document, dict)
                or set(document) != {"repository", "remote_url"}
                or document["repository"] != self.repository
                or not isinstance(document["remote_url"], str)
            ):
                raise ManagedWorkspaceError(error_message("workspace.error.identity_mismatch"))
            git_directory = self.repository_root / ".git"
            if not git_directory.is_dir() or not self.state_root.is_dir():
                raise ManagedWorkspaceError(error_message("workspace.error.incomplete"))
            if (git_directory / "objects" / "info" / "alternates").exists():
                raise ManagedWorkspaceError(error_message("workspace.error.shared_objects"))
            return {"repository": self.repository, "remote_url": document["remote_url"]}
        except (OSError, ValueError) as error:
            raise ManagedWorkspaceError(error_message("workspace.error.unreadable")) from error

    def ensure_path(
        self, remote_url: str | None = None, *, identity: dict[str, str] | None = None,
        runner_identity: Mapping[str, Any] | None = None,
    ) -> Path:
        """Create once; never reset, fetch or modify an existing checkout."""
        _check_owned_path(self.root)
        if self.root.exists():
            return self.open_path()
        validate_data_root()
        _make_owned_directory(self.root.parent)
        lock = app_data_root() / "repositories" / ".locks" / (self.repository + ".lock")
        _make_owned_directory(lock.parent)
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ManagedWorkspaceError(error_message("workspace.error.creating")) from error
            _check_owned_path(self.root)
            if self.root.exists():
                return self.open_path()
            staging = Path(tempfile.mkdtemp(prefix=f".{self.root.name}-", dir=self.root.parent))
            try:
                checkout = staging / "repository"
                if remote_url is None and runner_identity and runner_identity.get("source") in {"pat", "app"}:
                    remote_url = f"https://github.com/{self.repository}.git"
                command = (
                    ["git", "clone", "--no-local", "--quiet", "--", remote_url, str(checkout)]
                    if remote_url is not None else
                    ["gh", "repo", "clone", self.repository, str(checkout), "--", "--no-local", "--quiet"]
                )
                _command(command, staging, timeout=_CLONE_TIMEOUT_SECONDS, runner_identity=runner_identity)
                for key, value in (identity or {}).items():
                    if key not in {"user.name", "user.email"}:
                        raise ManagedWorkspaceError(error_message("workspace.error.unsupported_config"))
                    _command(["git", "config", "--local", key, value], checkout)
                origin = _command(["git", "remote", "get-url", "origin"], checkout).strip()
                (staging / "state").mkdir(mode=0o700)
                (staging / _MARKER).write_text(
                    json.dumps({"repository": self.repository, "remote_url": origin}) + "\n",
                    encoding="utf-8",
                )
                _check_owned_path(self.root)
                if self.root.exists():
                    raise ManagedWorkspaceError(error_message("workspace.error.occupied"))
                staging.rename(self.root)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return self.open_path()


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
        raise ManagedWorkspaceError(error_message("workspace.error.unmanaged")) from error
    if len(parts) != 3 or parts[-1] != "repository":
        raise ManagedWorkspaceError(error_message("workspace.error.unmanaged"))
    workspace = ManagedWorkspace.for_repository("/".join(parts[:2]))
    if workspace.repository_root != repository_root:
        raise ManagedWorkspaceError(error_message("workspace.error.noncanonical"))
    workspace._owned_document()
    return workspace


def _check_owned_path(path: Path) -> None:
    base = app_data_root()
    try:
        relative = path.relative_to(base)
    except ValueError as error:
        raise ManagedWorkspaceError(error_message("workspace.error.outside_data")) from error
    current = base
    for component in ("", *relative.parts):
        current = current / component
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ManagedWorkspaceError(error_message("workspace.error.not_directory", path=current))


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
    raise ManagedWorkspaceError(error_message("workspace.error.data_in_repository"))


def _command(
    arguments: list[str], cwd: Path, *, timeout: float = 30,
    runner_identity: Mapping[str, Any] | None = None,
) -> str:
    from agent_run.adapters.auth.runner_transport import runner_git_environment

    environment = runner_git_environment(runner_identity, cwd=cwd) if runner_identity else git_environment()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        process = subprocess.Popen(
            arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except OSError as error:
        raise ManagedWorkspaceError(error_message("workspace.error.start_failed")) from error
    stdout, stderr = TailOutput(16384), TailOutput(16384)
    deadline = time.monotonic() + timeout
    cleaned = False
    assert process.stdout is not None and process.stderr is not None

    def check() -> bool:
        nonlocal cleaned
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(arguments, timeout)
        if not cleaned and process.poll() is not None:
            cleaned = True
            terminate_process_group(process)
        return True

    try:
        pump(process, stdout=stdout, stderr=stderr, check=check)
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        raise ManagedWorkspaceError(error_message("workspace.error.timeout")) from error
    finally:
        try:
            if not cleaned:
                terminate_process_group(process)
        finally:
            process.stdout.close()
            process.stderr.close()
    if process.returncode != 0:
        diagnostic = stderr.data.decode("utf-8", errors="replace").lower()
        reason_key = "workspace.error.access"
        for needle, message_key in (
            ("repository not found", "workspace.error.not_found"),
            ("does not exist", "workspace.error.missing_remote"),
            ("permission denied", "workspace.error.permission"),
            ("authentication failed", "workspace.error.authentication"),
            ("could not resolve host", "workspace.error.resolve_host"),
            ("failed to connect", "workspace.error.connect"),
            ("no space left", "workspace.error.disk_full"),
        ):
            if needle in diagnostic:
                reason_key = message_key
                break
        raise ManagedWorkspaceError(error_message(
            "workspace.error.command_failed", returncode=process.returncode,
            reason=error_message(reason_key),
        ))
    return stdout.data.decode("utf-8")
