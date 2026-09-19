"""Persistent, non-secret selection for the Worker GitHub read provider."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from agent_run.paths import app_config_root


class GitHubAuthProfileError(ValueError):
    """The local GitHub App profile is missing, invalid, or unsafe to use."""


@dataclass(frozen=True)
class GitHubAppProfile:
    app_id: str
    installation_id: str
    private_key_path: Path


_MAX_NUMERIC_IDENTIFIER_DIGITS = 64
_REMOVE_RECOVERY_TIMEOUT_SECONDS = 1.0


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

    def configure(
        self, *, app_id: str, installation_id: str, private_key_path: str
    ) -> GitHubAppProfile:
        self._profile_directory_metadata()
        self._profile_metadata()
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
        metadata = self._profile_metadata()
        if metadata is not None:
            previous_bytes = self.path.read_bytes()
            previous_mode = stat.S_IMODE(metadata.st_mode)

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
        if self._profile_directory_metadata() is None:
            return False
        metadata = self._profile_metadata()
        if metadata is None:
            return False
        try:
            previous_bytes = self.path.read_bytes()
        except OSError as error:
            raise GitHubAuthProfileError("无法读取 GitHub App profile") from error
        previous_mode = stat.S_IMODE(metadata.st_mode)
        temporary_path: Path | None = None
        recovery_path: Path | None = None
        descriptor = -1
        swap_started = False
        try:
            recovery_path = self._create_remove_recovery_link()
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=".github-app.remove.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.close(descriptor)
            descriptor = -1

            # Keep the original inode in a same-directory tombstone until the
            # directory entry change has been persisted.  This makes the
            # delete reversible when either rename, unlink, or fsync fails.
            swap_started = True
            os.replace(self.path, temporary_path)
            self._fsync_directory()

            os.unlink(temporary_path)
            self._fsync_directory()
            os.unlink(recovery_path)
            recovery_path = None
        except (OSError, GitHubAuthProfileError) as error:
            # The rename may have completed its filesystem change before
            # reporting an error, so recover every transaction that reached
            # the swap call.  If it did not move, the snapshot already matches
            # the profile and recovery is a no-op apart from directory sync.
            restore_path = recovery_path if recovery_path is not None else temporary_path
            if swap_started and restore_path is not None:
                try:
                    self._recover_removed_profile(
                        restore_path, previous_bytes, previous_mode
                    )
                except (OSError, GitHubAuthProfileError) as restore_error:
                    raise GitHubAuthProfileError(
                        "无法删除 GitHub App profile，且无法恢复旧配置"
                    ) from restore_error
            if isinstance(error, GitHubAuthProfileError):
                raise
            raise GitHubAuthProfileError("无法删除 GitHub App profile") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary_path is not None and (
                not swap_started
                or self._profile_matches_snapshot(previous_bytes, previous_mode)
            ):
                _remove_temporary_file(temporary_path)
            if recovery_path is not None and (
                not swap_started
                or self._profile_matches_snapshot(previous_bytes, previous_mode)
            ):
                _remove_temporary_file(recovery_path)
        return True

    def _create_remove_recovery_link(self) -> Path:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=".github-app.recovery.",
            suffix=".tmp",
        )
        recovery_path = Path(temporary_name)
        try:
            os.close(descriptor)
            descriptor = -1
            os.unlink(recovery_path)
            os.link(self.path, recovery_path)
            self._fsync_directory()
        except (OSError, GitHubAuthProfileError):
            if descriptor != -1:
                os.close(descriptor)
            _remove_temporary_file(recovery_path)
            raise
        return recovery_path

    def _recover_removed_profile(
        self, temporary_path: Path, previous_bytes: bytes, previous_mode: int
    ) -> None:
        last_error: OSError | GitHubAuthProfileError | None = None
        deadline = time.monotonic() + _REMOVE_RECOVERY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._profile_matches_snapshot(previous_bytes, previous_mode):
                break
            try:
                os.replace(temporary_path, self.path)
            except OSError as error:
                last_error = error
                try:
                    os.rename(temporary_path, self.path)
                except OSError as rename_error:
                    last_error = rename_error
            if self._profile_matches_snapshot(previous_bytes, previous_mode):
                break
            try:
                self._restore_profile_snapshot(previous_bytes, previous_mode)
            except (OSError, GitHubAuthProfileError) as error:
                last_error = error
                try:
                    self._restore_profile_snapshot_direct(previous_bytes, previous_mode)
                except OSError as direct_error:
                    last_error = direct_error
                    try:
                        self._restore_profile_snapshot_with_exclusive_open(
                            previous_bytes, previous_mode
                        )
                    except OSError as open_error:
                        last_error = open_error
            if not self._profile_matches_snapshot(previous_bytes, previous_mode):
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        if not self._profile_matches_snapshot(previous_bytes, previous_mode):
            if last_error is not None:
                raise last_error
            raise OSError("无法恢复旧 GitHub App profile")
        self._fsync_directory()

    def _profile_matches_snapshot(self, previous_bytes: bytes, previous_mode: int) -> bool:
        try:
            metadata = self.path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return False
            if stat.S_IMODE(metadata.st_mode) != previous_mode:
                return False
            return self.path.read_bytes() == previous_bytes
        except OSError:
            return False

    def _restore_profile_snapshot(self, previous_bytes: bytes, previous_mode: int) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=".github-app.restore.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, previous_mode)
            with os.fdopen(descriptor, "wb") as temporary_file:
                descriptor = -1
                temporary_file.write(previous_bytes)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            try:
                # link() creates the restored entry without overwriting a
                # concurrently-created profile or following a symlink.
                os.link(temporary_path, self.path)
            except FileExistsError:
                if not self._profile_matches_snapshot(previous_bytes, previous_mode):
                    self._restore_profile_snapshot_in_place(previous_bytes, previous_mode)
        finally:
            if descriptor != -1:
                os.close(descriptor)
            _remove_temporary_file(temporary_path)

    def _restore_profile_snapshot_in_place(
        self, previous_bytes: bytes, previous_mode: int
    ) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", 0)

        def open_no_follow(path: str, flags: int) -> int:
            return os.open(path, flags | no_follow)

        with open(self.path, "r+b", opener=open_no_follow) as profile_file:
            metadata = os.fstat(profile_file.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("无法覆盖非普通 GitHub App profile")
            os.fchmod(profile_file.fileno(), previous_mode)
            profile_file.seek(0)
            profile_file.truncate()
            profile_file.write(previous_bytes)
            profile_file.flush()
            os.fsync(profile_file.fileno())

    def _restore_profile_snapshot_direct(
        self, previous_bytes: bytes, previous_mode: int
    ) -> None:
        """Last-resort recovery when no same-directory tombstone remains."""
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                previous_mode,
            )
        except FileExistsError:
            if self._profile_matches_snapshot(previous_bytes, previous_mode):
                return
            raise

        complete = False
        try:
            os.fchmod(descriptor, previous_mode)
            offset = 0
            while offset < len(previous_bytes):
                written = os.write(descriptor, previous_bytes[offset:])
                if written <= 0:
                    raise OSError("无法恢复旧 GitHub App profile")
                offset += written
            complete = True
            os.fsync(descriptor)
        except OSError:
            if not complete:
                _remove_temporary_file(self.path)
            raise
        finally:
            os.close(descriptor)

    def _restore_profile_snapshot_with_exclusive_open(
        self, previous_bytes: bytes, previous_mode: int
    ) -> None:
        """Restore without a temporary entry when recovery storage is unavailable."""
        complete = False
        try:
            with self.path.open("xb") as profile_file:
                os.fchmod(profile_file.fileno(), previous_mode)
                profile_file.write(previous_bytes)
                profile_file.flush()
                complete = True
                os.fsync(profile_file.fileno())
        except FileExistsError:
            if self._profile_matches_snapshot(previous_bytes, previous_mode):
                return
            raise
        except OSError:
            if not complete:
                _remove_temporary_file(self.path)
            raise

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


def _remove_temporary_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
