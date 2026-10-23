"""Read legacy App metadata for explicit migration and validate key references."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from agent_run.config.paths import app_config_root


class GitHubAuthProfileError(ValueError):
    """The local GitHub App profile is missing, invalid, or unsafe to use."""


@dataclass(frozen=True)
class GitHubAppProfile:
    app_id: str
    installation_id: str
    private_key_path: Path


_MAX_NUMERIC_IDENTIFIER_DIGITS = 64


def _is_positive_decimal_identifier(value: str) -> bool:
    """在不进行无界整数转换的情况下校验 GitHub 数字标识。"""
    if not value or len(value) > _MAX_NUMERIC_IDENTIFIER_DIGITS:
        return False
    if not value.isascii() or not value.isdigit():
        return False
    return any(character != "0" for character in value)


class GitHubAppProfileStore:
    """Read the legacy profile without modifying it or its referenced key."""

    _FILE_NAME = "github-app.json"
    _DOCUMENT_KEYS = frozenset({"app_id", "installation_id", "private_key_path"})

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self.default_path()

    @classmethod
    def default_path(cls) -> Path:
        try:
            return app_config_root() / cls._FILE_NAME
        except ValueError as error:
            raise GitHubAuthProfileError(str(error)) from error

    def load(self) -> GitHubAppProfile | None:
        if self._profile_directory_metadata() is None:
            return None
        metadata = self._profile_metadata()
        if metadata is None:
            return None
        self._require_secure_directory()
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise GitHubAuthProfileError("GitHub App profile 权限必须为 600")
        try:
            loaded: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubAuthProfileError("GitHub App profile 无法解析") from error
        return self._parse(loaded)

    def _profile_metadata(self) -> os.stat_result | None:
        """Read only the profile file entry, never a symlink target."""
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise GitHubAuthProfileError("无法读取 GitHub App profile") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise GitHubAuthProfileError("GitHub App profile 不是普通文件")
        return metadata

    def _profile_directory_metadata(self) -> os.stat_result | None:
        """Read only the profile directory entry, never follow its symlink."""
        try:
            metadata = self.path.parent.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise GitHubAuthProfileError("无法读取 GitHub App 配置目录") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GitHubAuthProfileError("GitHub App 配置目录不是普通目录")
        return metadata

    def _require_secure_directory(self) -> None:
        try:
            metadata = self.path.parent.stat()
        except OSError as error:
            raise GitHubAuthProfileError("无法读取 GitHub App 配置目录") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise GitHubAuthProfileError("GitHub App 配置目录权限必须为 700")

    @classmethod
    def _parse(cls, value: object) -> GitHubAppProfile:
        if not isinstance(value, dict) or set(value) != cls._DOCUMENT_KEYS:
            raise GitHubAuthProfileError("GitHub App profile 字段不完整或包含未知字段")
        app_id = value.get("app_id")
        installation_id = value.get("installation_id")
        private_key_path = value.get("private_key_path")
        if not (
            isinstance(app_id, str)
            and isinstance(installation_id, str)
            and isinstance(private_key_path, str)
        ):
            raise GitHubAuthProfileError("GitHub App profile 字段类型无效")
        return cls._validate_values(app_id, installation_id, private_key_path)

    @staticmethod
    def _validate_values(
        app_id: str, installation_id: str, private_key_path: str
    ) -> GitHubAppProfile:
        normalized_app_id = app_id.strip()
        normalized_installation_id = installation_id.strip()
        if not _is_positive_decimal_identifier(normalized_app_id):
            raise GitHubAuthProfileError("App ID 必须是正整数")
        if not _is_positive_decimal_identifier(normalized_installation_id):
            raise GitHubAuthProfileError("Installation ID 必须是正整数")
        candidate = Path(private_key_path).expanduser()
        if not candidate.is_absolute():
            raise GitHubAuthProfileError("私钥路径必须是绝对路径")
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except (OSError, RuntimeError) as error:
            raise GitHubAuthProfileError("私钥文件不存在或不可读取") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise GitHubAuthProfileError("私钥路径必须指向普通文件")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise GitHubAuthProfileError("私钥文件必须只允许当前用户访问")
        if _is_inside_git_repository(resolved):
            raise GitHubAuthProfileError("私钥文件必须位于 Git 仓库外")
        if not os.access(resolved, os.R_OK):
            raise GitHubAuthProfileError("私钥文件不可读取")
        return GitHubAppProfile(
            app_id=normalized_app_id,
            installation_id=normalized_installation_id,
            private_key_path=resolved,
        )


def _is_inside_git_repository(path: Path) -> bool:
    current = path.parent
    for directory in (current, *current.parents):
        git_marker = directory / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return True
        if _looks_like_bare_git_repository(directory):
            return True
    return False


def _looks_like_bare_git_repository(directory: Path) -> bool:
    """Recognize a bare repository without relying on the caller's cwd."""
    return (
        (directory / "HEAD").is_file()
        and (directory / "config").is_file()
        and (directory / "objects").is_dir()
        and (directory / "refs").is_dir()
    )
