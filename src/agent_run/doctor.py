"""Read-only diagnostics for the installed Runner and its host dependencies."""

from __future__ import annotations

import json
import os
import platform
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from agent_run.github_auth_profile import (
    GitHubAppProfileStore,
    GitHubAuthProfileError,
)
from agent_run.runner_installer import (
    InstallPaths,
    InstallerError,
    _read_active,
)
from agent_run.process_cleanup import (
    capture_process_scope,
    child_subreaper,
    terminate_process_group as _terminate_process_group,
)

_COMMAND_TIMEOUT_SECONDS = 3
_MAX_SIGNATURE_OUTPUT_BYTES = 4096


def run(*, as_json: bool = False) -> int:
    """Print a diagnostic report without changing any local state."""

    report = collect()
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(report)
    return 0


def collect() -> dict[str, object]:
    """Collect the public, non-secret doctor report."""

    with child_subreaper():
        return _collect()


def _collect() -> dict[str, object]:
    checks = {
        "python": _python_check(),
        "git": _tool_check("git"),
        "codex": _tool_check("codex"),
        "openssl": _tool_check("openssl"),
        "bubblewrap": _tool_check("bwrap"),
        "github": _github_check(),
        "active_runner": _active_runner_check(),
        "path": _path_check(),
        "worker_read_provider": _provider_check(),
    }
    return {
        "result": "doctor",
        "status": (
            "ready"
            if all(_check_is_ok(check) for check in checks.values())
            else "issues"
        ),
        "checks": checks,
    }


def _python_check() -> dict[str, object]:
    implementation = platform.python_implementation()
    version = platform.python_version()
    supported = implementation == "CPython" and sys.version_info >= (3, 11)
    return {
        "status": "ok" if supported else "unsupported",
        "implementation": implementation,
        "version": version,
        "path": str(Path(sys.executable).resolve()),
    }


def _tool_check(command: str) -> dict[str, object]:
    executable = shutil.which(command)
    if executable is None:
        return {"status": "missing", "path": None}
    returncode, timed_out = _run_bounded_probe([executable, "--version"])
    if returncode is None:
        return {
            "status": "timeout" if timed_out else "unavailable",
            "path": executable,
        }
    return {
        "status": "ok" if returncode == 0 else "unavailable",
        "path": executable,
    }


def _run_bounded_probe(
    arguments: list[str], *, input_data: bytes | None = None
) -> tuple[int | None, bool]:
    process: subprocess.Popen[Any] | None = None
    adopted_baseline = capture_process_scope()
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if input_data is None:
            returncode = process.wait(timeout=_COMMAND_TIMEOUT_SECONDS)
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
            return returncode, False
        process.communicate(input=input_data, timeout=_COMMAND_TIMEOUT_SECONDS)
        returncode = process.returncode
        _terminate_process_group(process, adopted_baseline=adopted_baseline)
        return returncode, False
    except subprocess.TimeoutExpired:
        if process is not None:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
        return None, True
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
        return None, False
    except BaseException:
        if process is not None:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
        raise


def _github_check() -> dict[str, object]:
    tool = _tool_check("gh")
    if tool.get("status") != "ok":
        return {**tool, "logged_in": False}
    executable = tool.get("path")
    if not isinstance(executable, str):
        return {"status": "unavailable", "logged_in": False}
    returncode, timed_out = _run_bounded_probe([executable, "auth", "status"])
    if returncode is None:
        return {
            **tool,
            "status": "timeout" if timed_out else "unavailable",
            "logged_in": False,
        }
    return {
        **tool,
        "status": "ok" if returncode == 0 else "not_logged_in",
        "logged_in": returncode == 0,
    }


def _active_runner_check() -> dict[str, object]:
    paths = InstallPaths.from_environment()
    try:
        current, previous, generation = _read_active(paths)
    except (InstallerError, OSError, RuntimeError, ValueError):
        return {"status": "invalid"}
    if current is None or generation is None:
        return {"status": "missing"}
    result: dict[str, object] = {
        "status": "ok",
        "current": current.name,
        "generation": generation.name,
    }
    if previous is not None:
        result["previous"] = previous.name
    return result


def _path_check() -> dict[str, object]:
    paths = InstallPaths.from_environment()
    entries = {
        os.path.normpath(os.path.expanduser(entry))
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry
    }
    executable = shutil.which("agent-run")
    stable_entry = os.path.normpath(str(paths.stable_entry))
    user_bin = os.path.normpath(str(paths.user_bin))
    stable_entry_present = paths.stable_entry.exists() or paths.stable_entry.is_symlink()
    managed_entry = _is_managed_entry(paths)
    if executable is not None and os.path.normpath(executable) != stable_entry:
        status = "conflict"
    elif not stable_entry_present:
        status = "missing"
    elif not managed_entry:
        status = "invalid"
    elif user_bin not in entries:
        status = "needs_refresh"
    elif executable is None:
        status = "needs_refresh"
    else:
        status = "ok"
    return {
        "status": status,
        "agent_run": executable,
        "user_bin_on_path": user_bin in entries,
    }


def _is_managed_entry(paths: InstallPaths) -> bool:
    try:
        if not paths.stable_entry.is_symlink():
            return False
        target = Path(os.readlink(paths.stable_entry))
    except (OSError, RuntimeError):
        return False
    if not target.is_absolute():
        target = paths.stable_entry.parent / target
    expected = paths.active / "current" / "bin" / "agent-run"
    return (
        os.path.normpath(str(target)) == os.path.normpath(str(expected))
        and expected.is_file()
        and os.access(expected, os.X_OK)
    )


def _provider_check() -> dict[str, object]:
    try:
        profile = GitHubAppProfileStore().load()
        if profile is None:
            return {"provider": "host", "status": "ok"}
        if not _private_key_is_usable(profile.private_key_path):
            return {"provider": "app", "status": "invalid"}
    except (GitHubAuthProfileError, OSError, RuntimeError):
        return {"provider": "app", "status": "invalid"}
    return {"provider": "app", "status": "ok"}


def _private_key_is_usable(path: Path) -> bool:
    executable = shutil.which("openssl")
    if executable is None:
        return False
    returncode, timed_out, signature = _run_bounded_output_probe(
        [executable, "dgst", "-sha256", "-sign", str(path)],
        input_data=b"agent-run GitHub App profile doctor check\n",
    )
    return returncode == 0 and not timed_out and bool(signature)


def _run_bounded_output_probe(
    arguments: list[str], *, input_data: bytes
) -> tuple[int | None, bool, bytes]:
    """Run a tiny probe while keeping captured output and descendants bounded."""

    process: subprocess.Popen[Any] | None = None
    stdout = None
    output = bytearray()
    adopted_baseline = capture_process_scope()
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        stdout = process.stdout
        if process.stdin is None or stdout is None:
            return None, False, b""
        process.stdin.write(input_data)
        process.stdin.close()
        deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process_group(process, adopted_baseline=adopted_baseline)
                    return None, True, b""
                events = selector.select(timeout=remaining)
                if not events:
                    _terminate_process_group(process, adopted_baseline=adopted_baseline)
                    return None, True, b""
                for key, _mask in events:
                    stream = cast(Any, key.fileobj)
                    chunk = os.read(stream.fileno(), 4096)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    output.extend(chunk)
                    if len(output) > _MAX_SIGNATURE_OUTPUT_BYTES:
                        _terminate_process_group(process, adopted_baseline=adopted_baseline)
                        return None, False, b""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
            return None, True, b""
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
            return None, True, b""
        return returncode, False, bytes(output)
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
        return None, False, b""
    finally:
        if process is not None:
            _terminate_process_group(process, adopted_baseline=adopted_baseline)
        if process is not None and process.stdin is not None:
            process.stdin.close()
        if stdout is not None:
            stdout.close()


def _check_is_ok(value: object) -> bool:
    return isinstance(value, dict) and value.get("status") == "ok"


def _print_human(report: dict[str, object]) -> None:
    checks = report["checks"]
    if not isinstance(checks, dict):
        raise ValueError("doctor checks must be an object")
    labels = (
        ("python", "Python"),
        ("git", "Git"),
        ("codex", "Codex"),
        ("openssl", "OpenSSL"),
        ("bubblewrap", "bubblewrap"),
        ("github", "宿主 gh 登录"),
        ("active_runner", "Active Runner"),
        ("path", "PATH"),
        ("worker_read_provider", "Worker read provider"),
    )
    print("agent-run doctor（只读诊断）")
    for key, label in labels:
        check = checks.get(key)
        if not isinstance(check, dict):
            print(f"{label}: unavailable")
            continue
        detail: object = None
        if key == "github":
            detail = "已登录" if check.get("logged_in") else "未登录"
        elif key == "worker_read_provider":
            detail = check.get("provider")
        elif key == "path":
            detail = {
                "ok": "agent-run 可用",
                "needs_refresh": "需要刷新登录 shell",
                "conflict": "检测到非受管同名入口",
                "invalid": "受管入口无效",
                "missing": "未找到 agent-run 入口",
            }.get(str(check.get("status")), "无法确认 agent-run 入口")
        print(f"{label}: {check.get('status')}" + (f" ({detail})" if detail else ""))
    print(f"总体: {report.get('status')}")
