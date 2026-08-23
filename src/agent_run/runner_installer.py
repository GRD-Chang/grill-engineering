"""Thin, user-level source installer for immutable Runner Snapshots."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

try:
    from agent_run.runner_probe import RunnerProbeBackend, RunnerProbeError
    from agent_run.runner_runtime import RuntimeTreeError, find_runtime_package
except ModuleNotFoundError:  # pragma: no cover - used by the source-tree script
    from runner_probe import (  # type: ignore[import-not-found, no-redef]
        RunnerProbeBackend,
        RunnerProbeError,
    )
    from runner_runtime import (  # type: ignore[import-not-found, no-redef]
        RuntimeTreeError,
        find_runtime_package,
    )


PATH_BLOCK_START = "# >>> agent-run managed PATH >>>"
PATH_BLOCK_END = "# <<< agent-run managed PATH <<<"
PATH_BLOCK = (
    f"{PATH_BLOCK_START}\n"
    'export PATH="$HOME/.local/bin:$PATH"\n'
    f"{PATH_BLOCK_END}\n"
)
_PATH_BLOCK_RE = re.compile(
    rf"(?ms)^{re.escape(PATH_BLOCK_START)}\n.*?^{re.escape(PATH_BLOCK_END)}\n?"
)
_MAX_GIT_FIELD_BYTES = 512


class InstallerError(RuntimeError):
    """A bounded operational error from the source installer."""


@dataclass(frozen=True)
class InstallPaths:
    data_root: Path
    snapshots: Path
    generations: Path
    staging: Path
    active: Path
    lock: Path
    user_bin: Path
    stable_entry: Path
    profile: Path

    @classmethod
    def from_environment(cls) -> "InstallPaths":
        data_home = os.environ.get("XDG_DATA_HOME")
        if not data_home:
            data_home = str(Path.home() / ".local" / "share")
        data_root = (Path(data_home).expanduser() / "agent-run").resolve()
        user_bin = (Path.home() / ".local" / "bin").resolve()
        return cls(
            data_root=data_root,
            snapshots=data_root / "snapshots",
            generations=data_root / "generations",
            staging=data_root / "staging",
            active=data_root / "active",
            lock=data_root / "install.lock",
            user_bin=user_bin,
            stable_entry=user_bin / "agent-run",
            profile=(Path.home() / ".profile").resolve(),
        )


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        parsed = parser.parse_args(list(arguments) if arguments is not None else None)
    except SystemExit as error:
        code = error.code
        return code if isinstance(code, int) else int(code) if isinstance(code, str) else 1
    source = Path(parsed.source).resolve()
    paths = InstallPaths.from_environment()
    try:
        with _management_lock(paths):
            if parsed.rollback:
                result = _rollback(paths)
            elif parsed.uninstall:
                result = _uninstall(paths)
            else:
                result = _install(paths, source)
    except InstallerError as error:
        print(f"agent-run install: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(
            f"agent-run install: operation failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get("incomplete") is True else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.sh",
        description="将当前源码目录安装为用户级 Active Runner",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--rollback", action="store_true", help="回退到紧邻上一个 Snapshot")
    modes.add_argument("--uninstall", action="store_true", help="卸载受管 Runner")
    parser.add_argument("--source", required=True, help=argparse.SUPPRESS)
    return parser


@contextmanager
def _management_lock(paths: InstallPaths) -> Iterator[None]:
    paths.data_root.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise InstallerError("无法创建用户级安装锁") from error
    with os.fdopen(descriptor, "a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallerError("另一个 install、rollback 或 uninstall 正在运行") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _install(paths: InstallPaths, source: Path) -> dict[str, object]:
    _check_source(source)
    _check_managed_entry(paths, for_uninstall=False)
    old_current, old_previous, old_generation = _read_active(paths)
    entry_was_present = paths.stable_entry.exists() or paths.stable_entry.is_symlink()
    profile_backup = _profile_backup(paths.profile)
    _check_prerequisites(source)
    paths.snapshots.mkdir(parents=True, exist_ok=True)
    paths.generations.mkdir(parents=True, exist_ok=True)
    paths.staging.mkdir(parents=True, exist_ok=True)
    _clear_staging(paths.staging)

    candidate = Path(tempfile.mkdtemp(prefix="candidate-", dir=paths.staging))
    snapshot: Path | None = None
    created_snapshot = False
    try:
        identity, python_version = _build_candidate(candidate, source)
        snapshot = paths.snapshots / identity
        if snapshot.exists() or snapshot.is_symlink():
            if not snapshot.is_dir() or snapshot.is_symlink():
                raise InstallerError("已有 Snapshot 路径不是受管目录")
            _validate_snapshot(snapshot, identity)
            _remove_path(candidate)
        else:
            RunnerProbeBackend().check(candidate)
            _write_manifest(
                candidate,
                {
                    "content_identity": identity,
                    "python": python_version,
                    "source_provenance": _source_provenance(source),
                    "built_at_utc": _now(),
                    "compatibility_check": {"result": "passed"},
                },
            )
            os.replace(candidate, snapshot)
            created_snapshot = True
            try:
                _rewrite_snapshot_entrypoints(snapshot, candidate)
            except BaseException:
                _remove_path(snapshot)
                raise
        assert snapshot is not None
        if old_current is not None and old_current == snapshot:
            try:
                _ensure_profile(paths)
                _ensure_stable_entry(paths)
            except BaseException:
                _restore_profile(paths.profile, profile_backup)
                if not entry_was_present and _is_managed_entry(paths):
                    paths.stable_entry.unlink(missing_ok=True)
                raise
            warnings = _cleanup_retired(paths, snapshot, old_previous, old_generation)
            return _install_result(snapshot, old_previous, warnings, idempotent=True)

        activated = False
        generation: Path | None = None
        try:
            generation = _activate(
                paths,
                current=snapshot,
                previous=old_current,
            )
            activated = True
            _ensure_profile(paths)
            _ensure_stable_entry(paths)
        except BaseException:
            _restore_profile(paths.profile, profile_backup)
            if activated and generation is not None:
                _restore_active(paths, old_generation, generation)
            if not entry_was_present and _is_managed_entry(paths):
                paths.stable_entry.unlink(missing_ok=True)
            if created_snapshot and snapshot is not None and snapshot.exists():
                _remove_path(snapshot)
            raise
        warnings = _cleanup_retired(paths, snapshot, old_current, generation)
        return _install_result(snapshot, old_current, warnings, idempotent=False)
    except RunnerProbeError as error:
        raise InstallerError(str(error)) from error
    finally:
        if candidate.exists() or candidate.is_symlink():
            _remove_path(candidate)
        _clear_staging(paths.staging)


def _rollback(paths: InstallPaths) -> dict[str, object]:
    _check_managed_entry(paths, for_uninstall=False)
    current, previous, generation = _read_active(paths)
    if current is None or previous is None or generation is None:
        raise InstallerError("没有可用的 previous Snapshot，Active Runner 未改变")
    new_generation = _activate(paths, current=previous, previous=current)
    warnings = _cleanup_retired(paths, previous, current, new_generation)
    return {
        "result": "rolled_back",
        "active_snapshot": _manifest_identity(previous),
        "previous_snapshot": _manifest_identity(current),
        "warning": warnings or None,
    }


def _uninstall(paths: InstallPaths) -> dict[str, object]:
    _check_managed_entry(paths, for_uninstall=True)
    warnings: list[str] = []
    if paths.stable_entry.is_symlink() and _is_managed_entry(paths):
        try:
            paths.stable_entry.unlink()
        except OSError:
            warnings.append("用户级 agent-run 入口未能删除")
    for directory in (paths.active, paths.generations, paths.snapshots, paths.staging):
        if directory.exists() or directory.is_symlink():
            try:
                _remove_path(directory)
            except OSError:
                warnings.append(f"清理受管目录失败：{directory.name}")
    try:
        _remove_profile_block(paths.profile)
    except OSError:
        warnings.append("~/.profile 中的受管 PATH 块未能删除")
    if (paths.stable_entry.exists() or paths.stable_entry.is_symlink()) and not _is_managed_entry(paths):
        warnings.append("用户替换了 agent-run 入口，已保留用户内容")
    return {
        "result": "uninstalled",
        "warning": warnings or None,
        "incomplete": bool(warnings),
    }


def _check_source(source: Path) -> None:
    if not source.is_dir():
        raise InstallerError("源码目录不存在或不可读")
    if not (source / "pyproject.toml").is_file():
        raise InstallerError("源码目录缺少 pyproject.toml")


def _check_prerequisites(source: Path) -> None:
    if platform.python_implementation() != "CPython" or sys.version_info < (3, 11):
        raise InstallerError("需要 CPython 3.11 或更高版本")
    try:
        import venv
    except ImportError as error:
        raise InstallerError("当前 Python 缺少 venv") from error
    try:
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired as error:
        raise InstallerError("检查 pip 超时") from error
    except OSError as error:
        raise InstallerError("当前 Python 缺少可用 pip") from error
    if pip.returncode != 0:
        raise InstallerError("当前 Python 缺少可用 pip")
    try:
        import tomllib

        document = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise InstallerError("pyproject.toml 无法读取") from error
    build_system = document.get("build-system")
    if not isinstance(build_system, dict) or not isinstance(
        build_system.get("build-backend"), str
    ):
        raise InstallerError("pyproject.toml 缺少声明的 Python build backend")
    if venv is None:  # keeps the import an explicit prerequisite check
        raise InstallerError("当前 Python 缺少 venv")


def _build_candidate(candidate: Path, source: Path) -> tuple[str, str]:
    import venv

    try:
        venv.EnvBuilder(with_pip=True, clear=True, symlinks=True).create(candidate)
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise InstallerError("无法创建隔离 Python 环境") from error
    python = candidate / "bin" / "python"
    if not python.exists():
        raise InstallerError("隔离环境缺少 Python 入口")
    return_code = _run_pip_install(python, source)
    if return_code != 0:
        raise InstallerError("Python package build or non-editable installation failed")
    package = _runtime_package(candidate)
    identity = _runtime_identity(package)
    return identity, f"{sys.version_info.major}.{sys.version_info.minor}"


def _run_pip_install(python: Path, source: Path) -> int:
    try:
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-cache-dir",
                str(source),
            ],
            cwd=source,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise InstallerError("Python package build or non-editable installation failed") from error
    try:
        return_code = process.wait(timeout=15 * 60)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise InstallerError("Python package build or non-editable installation timed out") from error
    return return_code


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _runtime_package(candidate: Path) -> Path:
    try:
        return find_runtime_package(candidate)
    except RuntimeTreeError as error:
        raise InstallerError(str(error)) from error


def _runtime_identity(package: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D")
        else:
            raise InstallerError("runtime tree 包含不支持的文件类型")
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _source_provenance(source: Path) -> dict[str, object]:
    commit = _git_field(source, "rev-parse", "--verify", "HEAD")
    ref = _git_field(source, "symbolic-ref", "--short", "-q", "HEAD")
    if commit is None and ref is None:
        return {"kind": "source-directory"}
    dirty = (
        _git_status(source, "diff", "--quiet") is False
        or _git_status(source, "diff", "--cached", "--quiet") is False
    )
    return {"kind": "git", "commit": commit, "ref": ref, "dirty": dirty}


def _git_field(source: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout[:_MAX_GIT_FIELD_BYTES].decode("utf-8", "replace").strip()
    return value if result.returncode == 0 and value else None


def _git_status(source: Path, *arguments: str) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), *arguments],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def _write_manifest(snapshot: Path, manifest: dict[str, object]) -> None:
    destination = snapshot / "manifest.json"
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o600)


def _validate_snapshot(snapshot: Path, identity: str) -> None:
    if _manifest_identity(snapshot) != identity:
        raise InstallerError("已有 Snapshot manifest 与目录身份不一致")
    if _runtime_identity(_runtime_package(snapshot)) != identity:
        raise InstallerError("Snapshot runtime tree 与 manifest identity 不一致")


def _manifest_identity(snapshot: Path) -> str:
    try:
        loaded: object = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallerError("Snapshot manifest 无法读取") from error
    identity = loaded.get("content_identity") if isinstance(loaded, dict) else None
    if not isinstance(identity, str):
        raise InstallerError("Snapshot manifest 格式无效")
    return identity


def _rewrite_snapshot_entrypoints(snapshot: Path, old_candidate: Path) -> None:
    bin_directory = snapshot / "bin"
    for entry in bin_directory.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        content = entry.read_bytes()
        old_prefix = f"#!{old_candidate}".encode()
        new_prefix = f"#!{snapshot}".encode()
        if content.startswith(old_prefix):
            entry.write_bytes(new_prefix + content[len(old_prefix) :])


def _read_active(paths: InstallPaths) -> tuple[Path | None, Path | None, Path | None]:
    if not paths.active.exists() and not paths.active.is_symlink():
        return None, None, None
    if not paths.active.is_symlink():
        raise InstallerError("Active Runner 不是受管 generation symlink")
    generation = paths.active.resolve()
    if not generation.is_dir() or generation.parent != paths.generations.resolve():
        raise InstallerError("Active Runner generation 无效")
    current = _generation_link(generation / "current")
    previous = (
        _generation_link(generation / "previous")
        if (generation / "previous").exists() or (generation / "previous").is_symlink()
        else None
    )
    _validate_snapshot(current, _manifest_identity(current))
    if previous is not None:
        _validate_snapshot(previous, _manifest_identity(previous))
    return current, previous, generation


def _generation_link(link: Path) -> Path:
    if not link.is_symlink():
        raise InstallerError("generation 缺少完整 Snapshot symlink")
    target = link.resolve()
    if not target.is_dir():
        raise InstallerError("generation 指向不存在的 Snapshot")
    return target


def _activate(
    paths: InstallPaths,
    *,
    current: Path,
    previous: Path | None,
) -> Path:
    paths.generations.mkdir(parents=True, exist_ok=True)
    transaction = uuid.uuid4().hex
    temporary_generation = paths.generations / f".generation-{transaction}.tmp"
    generation = paths.generations / transaction
    temporary_active = paths.data_root / f".active-{transaction}.tmp"
    temporary_generation.mkdir()
    try:
        os.symlink(current, temporary_generation / "current")
        if previous is not None:
            os.symlink(previous, temporary_generation / "previous")
        os.replace(temporary_generation, generation)
        os.symlink(generation, temporary_active)
        os.replace(temporary_active, paths.active)
        _sync_directory(paths.generations)
        _sync_directory(paths.data_root)
    except BaseException:
        if temporary_active.exists() or temporary_active.is_symlink():
            temporary_active.unlink(missing_ok=True)
        if temporary_generation.exists() or temporary_generation.is_symlink():
            _remove_path(temporary_generation)
        if generation.exists() and not _active_points_to(paths.active, generation):
            _remove_path(generation)
        raise
    return generation


def _cleanup_retired(
    paths: InstallPaths,
    current: Path,
    previous: Path | None,
    active_generation: Path | None,
) -> list[str]:
    warnings: list[str] = []
    keep_generations = {active_generation.resolve()} if active_generation is not None else set()
    if paths.generations.exists():
        try:
            children = list(paths.generations.iterdir())
        except OSError:
            warnings.append("旧 generation 清理失败")
        else:
            for child in children:
                try:
                    if child.resolve() in keep_generations:
                        continue
                    _remove_path(child)
                except OSError:
                    warnings.append("旧 generation 清理失败")
    keep_snapshots = {current.resolve()}
    if previous is not None:
        keep_snapshots.add(previous.resolve())
    if paths.snapshots.exists():
        try:
            children = list(paths.snapshots.iterdir())
        except OSError:
            warnings.append("旧 Snapshot 清理失败")
        else:
            for child in children:
                try:
                    if child.resolve() in keep_snapshots:
                        continue
                    _remove_path(child)
                except OSError:
                    warnings.append("旧 Snapshot 清理失败")
    return warnings


def _clear_staging(staging: Path) -> None:
    if not staging.exists():
        return
    try:
        children = list(staging.iterdir())
    except OSError:
        print(
            "agent-run install: warning: staging cleanup failed; will retry",
            file=sys.stderr,
        )
        return
    for child in children:
        try:
            _remove_path(child)
        except OSError:
            print(
                "agent-run install: warning: staging cleanup failed; will retry",
                file=sys.stderr,
            )


def _check_managed_entry(paths: InstallPaths, *, for_uninstall: bool) -> None:
    if not paths.stable_entry.exists() and not paths.stable_entry.is_symlink():
        return
    if _is_managed_entry(paths):
        return
    if for_uninstall:
        return
    raise InstallerError("~/.local/bin/agent-run 已被非受管内容占用，未覆盖")


def _is_managed_entry(paths: InstallPaths) -> bool:
    if not paths.stable_entry.is_symlink():
        return False
    try:
        target = Path(os.readlink(paths.stable_entry))
    except OSError:
        return False
    if not target.is_absolute():
        target = paths.stable_entry.parent / target
    expected = paths.active / "current" / "bin" / "agent-run"
    return os.path.normpath(str(target)) == os.path.normpath(str(expected))


def _ensure_stable_entry(paths: InstallPaths) -> None:
    paths.user_bin.mkdir(parents=True, exist_ok=True)
    if paths.stable_entry.exists() or paths.stable_entry.is_symlink():
        if not _is_managed_entry(paths):
            raise InstallerError("~/.local/bin/agent-run 已被非受管内容占用，未覆盖")
        return
    temporary = paths.user_bin / f".agent-run-{uuid.uuid4().hex}.tmp"
    try:
        os.symlink(paths.active / "current" / "bin" / "agent-run", temporary)
        os.replace(temporary, paths.stable_entry)
    finally:
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink(missing_ok=True)


def _ensure_profile(paths: InstallPaths) -> None:
    original = paths.profile.read_text(encoding="utf-8") if paths.profile.exists() else ""
    without = _PATH_BLOCK_RE.sub("", original)
    desired = without
    if desired and not desired.endswith("\n"):
        desired += "\n"
    desired += PATH_BLOCK
    if desired == original:
        return
    _atomic_write(paths.profile, desired, mode=0o600 if not paths.profile.exists() else None)


def _remove_profile_block(profile: Path) -> None:
    if not profile.exists():
        return
    original = profile.read_text(encoding="utf-8")
    desired = _PATH_BLOCK_RE.sub("", original)
    if desired != original:
        _atomic_write(profile, desired, mode=None)


def _profile_backup(profile: Path) -> tuple[bool, bytes, int | None]:
    if not profile.exists():
        return False, b"", None
    return True, profile.read_bytes(), profile.stat().st_mode & 0o777


def _restore_profile(profile: Path, backup: tuple[bool, bytes, int | None]) -> None:
    existed, content, mode = backup
    if not existed:
        profile.unlink(missing_ok=True)
        return
    _atomic_write(profile, content, mode=mode)


def _atomic_write(path: Path, content: str | bytes, *, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and path.exists():
        mode = path.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if mode is not None:
            os.chmod(temporary, mode)
        data = content.encode("utf-8") if isinstance(content, str) else content
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _install_result(
    snapshot: Path,
    previous: Path | None,
    warnings: list[str],
    *,
    idempotent: bool,
) -> dict[str, object]:
    return {
        "result": "installed",
        "content_identity": _manifest_identity(snapshot),
        "active_snapshot": _manifest_identity(snapshot),
        "previous_snapshot": _manifest_identity(previous) if previous is not None else None,
        "entry": str(snapshot.parents[1] / "active" / "current" / "bin" / "agent-run"),
        "idempotent": idempotent,
        "warning": warnings or None,
    }


def _active_points_to(active: Path, generation: Path) -> bool:
    return active.is_symlink() and active.resolve() == generation.resolve()


def _restore_active(
    paths: InstallPaths,
    previous_generation: Path | None,
    failed_generation: Path,
) -> None:
    if previous_generation is None:
        if paths.active.is_symlink() or paths.active.exists():
            paths.active.unlink()
    else:
        temporary = paths.data_root / f".active-restore-{uuid.uuid4().hex}.tmp"
        try:
            os.symlink(previous_generation, temporary)
            os.replace(temporary, paths.active)
            _sync_directory(paths.data_root)
        finally:
            if temporary.is_symlink() or temporary.exists():
                temporary.unlink(missing_ok=True)
    if failed_generation.exists() or failed_generation.is_symlink():
        _remove_path(failed_generation)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _sync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
