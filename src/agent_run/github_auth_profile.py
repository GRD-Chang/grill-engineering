"""Persistent, non-secret selection for the Worker GitHub read provider."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


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
    """Atomically manage the user-level GitHub App profile."""

    _FILE_NAME = "github-app.json"
    _DIRECTORY_NAME = "agent-run"
    _DOCUMENT_KEYS = frozenset({"app_id", "installation_id", "private_key_path"})

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self.default_path()

    @classmethod
    def default_path(cls) -> Path:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home:
            root = Path(config_home).expanduser()
        else:
            root = Path.home() / ".config"
        return (root / cls._DIRECTORY_NAME / cls._FILE_NAME).resolve()

    def load(self) -> GitHubAppProfile | None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise GitHubAuthProfileError("无法读取 GitHub App profile") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise GitHubAuthProfileError("GitHub App profile 不是普通文件")
        self._require_secure_directory()
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise GitHubAuthProfileError("GitHub App profile 权限必须为 600")
        try:
            loaded: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubAuthProfileError("GitHub App profile 无法解析") from error
        return self._parse(loaded)

    def configure(
        self, *, app_id: str, installation_id: str, private_key_path: str
    ) -> GitHubAppProfile:
        profile = self._validate_values(app_id, installation_id, private_key_path)
        _validate_private_key_signature(profile.private_key_path)
        self._ensure_directory()
        document = {
            "app_id": profile.app_id,
            "installation_id": profile.installation_id,
            "private_key_path": str(profile.private_key_path),
        }
        previous_bytes: bytes | None = None
        previous_mode: int | None = None
        try:
            metadata = self.path.lstat()
            if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                previous_bytes = self.path.read_bytes()
                previous_mode = stat.S_IMODE(metadata.st_mode)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise GitHubAuthProfileError("无法读取旧的 GitHub App profile") from error

        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=".github-app.",
                suffix=".tmp",
                text=True,
            )
        except OSError as error:
            raise GitHubAuthProfileError("无法创建 GitHub App profile 临时文件") from error
        temporary_path = Path(temporary_name)

        replaced = False
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                descriptor = -1
                json.dump(document, temporary_file, ensure_ascii=False, sort_keys=True)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
            replaced = True
            self._fsync_directory()
        except BaseException as error:
            if descriptor != -1:
                os.close(descriptor)
            _remove_temporary_file(temporary_path)
            if replaced:
                try:
                    self._restore_previous(previous_bytes, previous_mode)
                except OSError as restore_error:
                    raise GitHubAuthProfileError(
                        "无法保存 GitHub App profile，且无法恢复旧配置"
                    ) from restore_error
            if isinstance(error, GitHubAuthProfileError):
                raise
            if isinstance(error, OSError):
                raise GitHubAuthProfileError("无法原子保存 GitHub App profile") from error
            raise
        return profile

    def _restore_previous(
        self, previous_bytes: bytes | None, previous_mode: int | None
    ) -> None:
        if previous_bytes is None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=".github-app.restore.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, previous_mode or 0o600)
            with os.fdopen(descriptor, "wb") as temporary_file:
                descriptor = -1
                temporary_file.write(previous_bytes)
                temporary_file.flush()
            os.replace(temporary_path, self.path)
        finally:
            if descriptor != -1:
                os.close(descriptor)
            _remove_temporary_file(temporary_path)

    def remove(self) -> bool:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise GitHubAuthProfileError("无法读取 GitHub App profile") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise GitHubAuthProfileError("GitHub App profile 不是普通文件")
        try:
            self.path.unlink()
            self._fsync_directory()
        except OSError as error:
            raise GitHubAuthProfileError("无法删除 GitHub App profile") from error
        return True

    def _ensure_directory(self) -> None:
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        except OSError as error:
            raise GitHubAuthProfileError("无法创建 GitHub App 配置目录") from error

    def _require_secure_directory(self) -> None:
        try:
            metadata = self.path.parent.stat()
        except OSError as error:
            raise GitHubAuthProfileError("无法读取 GitHub App 配置目录") from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise GitHubAuthProfileError("GitHub App 配置目录权限必须为 700")

    def _fsync_directory(self) -> None:
        try:
            descriptor = os.open(self.path.parent, os.O_RDONLY)
        except OSError as error:
            raise GitHubAuthProfileError("无法持久化 GitHub App profile") from error
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise GitHubAuthProfileError("无法持久化 GitHub App profile") from error
        finally:
            os.close(descriptor)

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


def load_github_app_profile() -> GitHubAppProfile | None:
    return GitHubAppProfileStore().load()


def _validate_private_key_signature(private_key_path: Path) -> None:
    """Validate App signing locally without contacting GitHub or storing a key."""

    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(private_key_path)],
            input=b"agent-run GitHub App profile validation\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitHubAuthProfileError("私钥无法完成本地签名校验") from error
    if result.returncode != 0 or not result.stdout:
        raise GitHubAuthProfileError("私钥无法完成本地签名校验")


def _is_inside_git_repository(path: Path) -> bool:
    current = path.parent
    for directory in (current, *current.parents):
        git_marker = directory / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return True
    return False


def _remove_temporary_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
