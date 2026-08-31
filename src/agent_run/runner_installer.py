"""Thin, user-level source installer for immutable Runner Snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence, cast

if __name__ == "__main__":
    # The public source-tree entry point must not create its own bytecode files
    # before provenance is captured.
    sys.dont_write_bytecode = True

try:
    from agent_run.process_cleanup import (
        capture_process_scope,
        child_subreaper,
        terminate_process_group as _terminate_process_group,
    )
    from agent_run.runner_runtime import RuntimeTreeError, find_runtime_package
    from agent_run.runner_lease import (
        RunnerLeaseBusy,
        RunnerLeaseError,
        runner_management_lease,
    )
except ModuleNotFoundError:  # pragma: no cover - used by the source-tree script
    from process_cleanup import (  # type: ignore[import-not-found, no-redef]
        capture_process_scope,
        child_subreaper,
        terminate_process_group as _terminate_process_group,
    )
    from runner_runtime import (  # type: ignore[import-not-found, no-redef]
        RuntimeTreeError,
        find_runtime_package,
    )
    from runner_lease import (  # type: ignore[import-not-found, no-redef]
        RunnerLeaseBusy,
        RunnerLeaseError,
        runner_management_lease,
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
_CANDIDATE_PROBE_TIMEOUT_SECONDS = 150.0
_MAX_CANDIDATE_PROBE_OUTPUT_BYTES = 16 * 1024
_SAFE_CANDIDATE_PROBE_ERRORS = frozenset(
    {
        "Codex executable is not available on PATH",
        "Codex Compatibility Check timed out",
        "could not start Codex Compatibility Check",
        "Codex Compatibility Check returned a non-zero exit",
        "Codex Compatibility Check produced no final output",
        "Codex Compatibility Check final output is too large",
        "Codex Compatibility Check returned invalid JSON",
        "Codex Compatibility Check rejected the required schema",
        "candidate Runner has an invalid runtime tree",
        "candidate Runner runtime escapes its Snapshot",
        "candidate Runner runtime contains a symlink outside its Snapshot",
    }
)


class InstallerError(RuntimeError):
    """A bounded operational error from the source installer."""


class InstallerInterrupted(BaseException):
    """A signal-triggered interruption that still runs installer cleanup."""


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
    with child_subreaper():
        return _main(arguments)


def _main(arguments: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        parsed = parser.parse_args(list(arguments) if arguments is not None else None)
    except SystemExit as error:
        code = error.code
        return code if isinstance(code, int) else int(code) if isinstance(code, str) else 1
    try:
        source = Path(parsed.source).resolve()
        paths = InstallPaths.from_environment()
        _reject_managed_paths_inside_source(paths, source)
        with _handle_sigterm():
            with _management_lock(paths):
                if parsed.rollback:
                    result = _rollback(paths)
                elif parsed.uninstall:
                    result = _uninstall(paths)
                else:
                    result = _install(paths, source)
    except (InstallerInterrupted, KeyboardInterrupt):
        print("agent-run install: interrupted", file=sys.stderr)
        return 1
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


def _raise_interrupted(_signum: int, _frame: Any) -> None:
    raise InstallerInterrupted()


@contextmanager
def _handle_sigterm() -> Iterator[None]:
    previous = signal.signal(signal.SIGTERM, _raise_interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def _management_lock(paths: InstallPaths) -> Iterator[None]:
    try:
        with runner_management_lease(paths.lock):
            yield
    except RunnerLeaseBusy as error:
        raise InstallerError(
            "存在活动 Executor，或另一个 install、rollback 或 uninstall 正在运行"
        ) from error
    except RunnerLeaseError as error:
        raise InstallerError("无法创建用户级安装锁") from error


def _install(paths: InstallPaths, source: Path) -> dict[str, object]:
    try:
        return _install_transaction(paths, source)
    finally:
        _clear_staging(paths.staging)


def _install_transaction(paths: InstallPaths, source: Path) -> dict[str, object]:
    _check_source(source)
    source_provenance = _source_provenance(source)
    _check_managed_entry(paths, for_uninstall=False)
    old_current, old_previous, old_generation = _read_active(paths)
    entry_was_present = paths.stable_entry.exists() or paths.stable_entry.is_symlink()
    profile_backup = _profile_backup(paths.profile)
    pre_cleanup_warnings: list[str] = []
    if old_current is not None and old_generation is not None:
        pre_cleanup_warnings = _cleanup_retired(
            paths, old_current, old_previous, old_generation
        )
        _emit_warnings(pre_cleanup_warnings)
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
            if old_current != snapshot:
                _run_candidate_probe(snapshot)
        else:
            _run_candidate_probe(candidate)
            _write_manifest(
                candidate,
                {
                    "content_identity": identity,
                    "python": python_version,
                    "source_provenance": source_provenance,
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
                warnings = [
                    *pre_cleanup_warnings,
                    *_cleanup_retired(paths, snapshot, old_previous, old_generation),
                ]
                return _install_result(
                    paths, snapshot, old_previous, warnings, idempotent=True
                )
            except BaseException:
                _restore_profile(paths.profile, profile_backup)
                if not entry_was_present and _is_managed_entry(paths):
                    paths.stable_entry.unlink(missing_ok=True)
                raise

        generation: Path | None = None
        activation_warnings: list[str] = []
        try:
            _ensure_profile(paths)
            _ensure_stable_entry(paths)
            generation, activation_warnings = _activate(
                paths,
                current=snapshot,
                previous=old_current,
                old_generation=old_generation,
            )
            try:
                warnings = [
                    *pre_cleanup_warnings,
                    *activation_warnings,
                    *_cleanup_retired(paths, snapshot, old_current, generation),
                ]
                return _install_result(
                    paths, snapshot, old_current, warnings, idempotent=False
                )
            except (InstallerInterrupted, KeyboardInterrupt):
                return _install_result(
                    paths,
                    snapshot,
                    old_current,
                    [
                        *pre_cleanup_warnings,
                        *activation_warnings,
                        "安装已完成，已保留新的 Active Runner",
                    ],
                    idempotent=False,
                )
        except BaseException:
            _restore_profile(paths.profile, profile_backup)
            if generation is not None:
                _restore_active(paths, old_generation, generation)
            if not entry_was_present and _is_managed_entry(paths):
                paths.stable_entry.unlink(missing_ok=True)
            if (
                created_snapshot
                and snapshot is not None
                and snapshot.exists()
                and not _active_references_snapshot(paths, snapshot)
            ):
                _remove_path(snapshot)
            raise
    finally:
        if candidate.exists() or candidate.is_symlink():
            _remove_path(candidate)


def _rollback(paths: InstallPaths) -> dict[str, object]:
    _check_managed_entry(paths, for_uninstall=False)
    current, previous, generation = _read_active(paths)
    if current is None or previous is None or generation is None:
        raise InstallerError("没有可用的 previous Snapshot，Active Runner 未改变")
    new_generation, activation_warnings = _activate(
        paths,
        current=previous,
        previous=current,
        old_generation=generation,
    )
    warnings = [
        *activation_warnings,
        *_cleanup_retired(paths, previous, current, new_generation),
    ]
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


def _reject_managed_paths_inside_source(paths: InstallPaths, source: Path) -> None:
    for managed_path in (paths.data_root, paths.user_bin, paths.profile):
        if managed_path.is_relative_to(source):
            raise InstallerError("受管 Runner 路径不能位于源码目录内")


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
    build_source = Path(
        tempfile.mkdtemp(prefix="build-source-", dir=str(candidate.parent))
    )
    try:
        try:
            shutil.copytree(
                source,
                build_source,
                dirs_exist_ok=True,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", ".agent-run"),
            )
        except (OSError, shutil.Error) as error:
            raise InstallerError("无法准备隔离 Python package build source") from error
        return_code = _run_pip_install(python, build_source)
    finally:
        try:
            shutil.rmtree(build_source)
        except OSError:
            pass
    if return_code != 0:
        raise InstallerError("Python package build or non-editable installation failed")
    package = _runtime_package(candidate)
    identity = _runtime_identity(package)
    return identity, f"{sys.version_info.major}.{sys.version_info.minor}"


def _run_pip_install(python: Path, source: Path) -> int:
    adopted_baseline = capture_process_scope()
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
        try:
            return_code = process.wait(timeout=15 * 60)
        except subprocess.TimeoutExpired as error:
            raise InstallerError(
                "Python package build or non-editable installation timed out"
            ) from error
        return return_code
    finally:
        _terminate_process_group(process, adopted_baseline=adopted_baseline)


def _run_candidate_probe(candidate: Path) -> None:
    python = candidate / "bin" / "python"
    if not python.is_file():
        raise InstallerError("隔离环境缺少 Runner Compatibility Check 入口")
    with tempfile.TemporaryDirectory(prefix="agent-run-candidate-probe-") as temporary_name:
        empty_directory = Path(temporary_name)
        probe_environment = os.environ.copy()
        for variable in ("PYTHONHOME", "PYTHONPATH"):
            probe_environment.pop(variable, None)
        probe_environment["TMPDIR"] = str(empty_directory)
        probe_environment["AGENT_RUN_PROBE_INHERIT_PROCESS_GROUP"] = "1"
        adopted_baseline = capture_process_scope()
        try:
            process = subprocess.Popen(
                [str(python), "-m", "agent_run.runner_probe", str(candidate)],
                cwd=empty_directory,
                env=probe_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise InstallerError("无法启动候选 Runner Compatibility Check") from error
        try:
            output, error_output = _read_candidate_probe_output(
                process,
                timeout_seconds=_CANDIDATE_PROBE_TIMEOUT_SECONDS,
                adopted_baseline=adopted_baseline,
            )
            if process.returncode != 0:
                raise InstallerError(_safe_candidate_probe_error(error_output))
            try:
                loaded: object = json.loads(output)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InstallerError("Runner Compatibility Check produced invalid output") from error
            if loaded != {"result": "passed"}:
                raise InstallerError("Runner Compatibility Check produced an invalid result")
        finally:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)


def _read_candidate_probe_output(
    process: subprocess.Popen[Any],
    *,
    timeout_seconds: float,
    adopted_baseline: set[int] | None = None,
) -> tuple[bytes, bytes]:
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    if any(stream is None for stream in streams.values()):
        _terminate_process_group(process, adopted_baseline=adopted_baseline)
        raise InstallerError("Runner Compatibility Check failed")
    deadline = time.monotonic() + timeout_seconds
    buffers = {name: bytearray() for name in streams}
    try:
        with selectors.DefaultSelector() as selector:
            for stream in streams.values():
                assert stream is not None
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process_group(process, adopted_baseline=adopted_baseline)
                    raise InstallerError("Runner Compatibility Check timed out")
                events = selector.select(timeout=remaining)
                if not events:
                    _terminate_process_group(process, adopted_baseline=adopted_baseline)
                    raise InstallerError("Runner Compatibility Check timed out")
                for key, _mask in events:
                    stream = cast(Any, key.fileobj)
                    chunk = os.read(stream.fileno(), 4096)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    name = next(
                        stream_name
                        for stream_name, candidate_stream in streams.items()
                        if candidate_stream is stream
                    )
                    buffers[name].extend(chunk)
                    if len(buffers[name]) > _MAX_CANDIDATE_PROBE_OUTPUT_BYTES:
                        _terminate_process_group(process, adopted_baseline=adopted_baseline)
                        raise InstallerError("Runner Compatibility Check output is too large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
            raise InstallerError("Runner Compatibility Check timed out")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
            raise InstallerError("Runner Compatibility Check timed out") from error
        return bytes(buffers["stdout"]), bytes(buffers["stderr"])
    except (OSError, subprocess.SubprocessError) as error:
        _terminate_process_group(process, adopted_baseline=adopted_baseline)
        raise InstallerError("Runner Compatibility Check failed") from error
    except BaseException:
        _terminate_process_group(process, adopted_baseline=adopted_baseline)
        raise
    finally:
        for stream in streams.values():
            if stream is not None:
                stream.close()


def _safe_candidate_probe_error(error_output: bytes) -> str:
    try:
        detail = error_output.decode("utf-8").strip()
    except UnicodeDecodeError:
        return "Runner Compatibility Check returned a non-zero exit"
    prefix = "runner probe: "
    if detail.startswith(prefix) and detail.removeprefix(prefix) in _SAFE_CANDIDATE_PROBE_ERRORS:
        return detail.removeprefix(prefix)
    return "Runner Compatibility Check returned a non-zero exit"


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
        or _git_has_changes(source) is True
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


def _git_has_changes(source: Path) -> bool | None:
    adopted_baseline = capture_process_scope()
    try:
        process = subprocess.Popen(
            [
                "git",
                "-C",
                str(source),
                "status",
                "--porcelain",
                "--untracked-files=normal",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    output = process.stdout
    if output is None:
        process.wait()
        return None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(output, selectors.EVENT_READ)
            if not selector.select(timeout=3):
                _terminate_process_group(process)
                return None
            if os.read(output.fileno(), 1):
                _terminate_process_group(process)
                return True
        try:
            return False if process.wait(timeout=3) == 0 else None
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            return None
    except (OSError, subprocess.SubprocessError):
        _terminate_process_group(process)
        return None
    finally:
        output.close()
        _terminate_process_group(process, adopted_baseline=adopted_baseline)


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
    old_path = os.fsencode(str(old_candidate))
    new_path = os.fsencode(str(snapshot))
    for entry in bin_directory.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        content = entry.read_bytes()
        rewritten = content.replace(old_path, new_path)
        if rewritten != content:
            entry.write_bytes(rewritten)


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
    old_generation: Path | None,
) -> tuple[Path, list[str]]:
    paths.generations.mkdir(parents=True, exist_ok=True)
    transaction = uuid.uuid4().hex
    temporary_generation = paths.generations / f".generation-{transaction}.tmp"
    generation = paths.generations / transaction
    temporary_active = paths.data_root / f".active-{transaction}.tmp"
    temporary_generation.mkdir()
    active_switched = False
    try:
        os.symlink(current, temporary_generation / "current")
        if previous is not None:
            os.symlink(previous, temporary_generation / "previous")
        os.replace(temporary_generation, generation)
        os.symlink(generation, temporary_active)
        os.replace(temporary_active, paths.active)
        active_switched = True
        _sync_directory(paths.generations)
        _sync_directory(paths.data_root)
    except BaseException:
        if temporary_active.exists() or temporary_active.is_symlink():
            temporary_active.unlink(missing_ok=True)
        if temporary_generation.exists() or temporary_generation.is_symlink():
            _remove_path(temporary_generation)
        if active_switched or _active_points_to(paths.active, generation):
            if not _restore_active(paths, old_generation, generation):
                if not _complete_active_generation(paths, generation):
                    if generation.exists() and not _active_points_to(paths.active, generation):
                        _remove_path(generation)
                    raise InstallerError(
                        "Active Runner 恢复失败且新 generation 不完整，未能安全激活"
                    )
                return generation, ["Active Runner 恢复失败，已保留完整新 generation"]
        elif generation.exists():
            _remove_path(generation)
        raise
    return generation, []


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


def _emit_warnings(warnings: Sequence[str]) -> None:
    for warning in warnings:
        print(f"agent-run install: warning: {warning}", file=sys.stderr)


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
    paths: InstallPaths,
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
        "entry": str(paths.stable_entry),
        "path_notice": "已更新用户级 PATH；请重新打开登录 shell 后使用 agent-run",
        "idempotent": idempotent,
        "warning": warnings or None,
    }


def _active_points_to(active: Path, generation: Path) -> bool:
    try:
        return active.is_symlink() and active.resolve() == generation.resolve()
    except (OSError, RuntimeError):
        return False


def _active_matches_generation(active: Path, generation: Path | None) -> bool:
    if generation is None:
        return not active.exists() and not active.is_symlink()
    return _active_points_to(active, generation)


def _active_references_snapshot(paths: InstallPaths, snapshot: Path) -> bool:
    if not paths.active.is_symlink():
        return False
    try:
        generation = paths.active.resolve()
        for link_name in ("current", "previous"):
            link = generation / link_name
            if link.is_symlink() and link.resolve() == snapshot.resolve():
                return True
    except (OSError, RuntimeError):
        return True
    return False


def _restore_active(
    paths: InstallPaths,
    previous_generation: Path | None,
    failed_generation: Path,
) -> bool:
    temporary: Path | None = None
    restored = False
    try:
        try:
            if previous_generation is None:
                if paths.active.is_symlink() or paths.active.exists():
                    paths.active.unlink()
                _sync_directory(paths.data_root)
            else:
                temporary = paths.data_root / f".active-restore-{uuid.uuid4().hex}.tmp"
                os.symlink(previous_generation, temporary)
                os.replace(temporary, paths.active)
                _sync_directory(paths.data_root)
            restored = _active_matches_generation(paths.active, previous_generation)
        except OSError:
            restored = _active_matches_generation(paths.active, previous_generation)
    finally:
        if temporary is not None:
            if temporary.is_symlink() or temporary.exists():
                temporary.unlink(missing_ok=True)
        if restored and (
            failed_generation.exists() or failed_generation.is_symlink()
        ) and not _active_points_to(paths.active, failed_generation):
            _remove_path(failed_generation)
    return restored


def _complete_active_generation(paths: InstallPaths, generation: Path) -> bool:
    if not _active_points_to(paths.active, generation) or not generation.is_dir():
        return False
    try:
        current = _generation_link(generation / "current")
        _validate_snapshot(current, _manifest_identity(current))
        previous_link = generation / "previous"
        if previous_link.exists() or previous_link.is_symlink():
            previous = _generation_link(previous_link)
            _validate_snapshot(previous, _manifest_identity(previous))
    except (InstallerError, OSError, RuntimeError):
        return False
    return True


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
