"""Read-only diagnostics for the installed Runner and its host dependencies."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

from agent_run.config.defaults import UserDefaultsStore
from agent_run.install.readiness import identity_checks, next_steps, skills_check
from agent_run.storage.sqlite_runtime import require_wal_runtime
from agent_run.install.layout import InstallPaths, InstallerError, read_active
from agent_run.install.diagnostics import run_bounded_probe
from agent_run.adapters.systemd import HostUnavailable, SystemdHost
from agent_run.runtime.process_cleanup import child_subreaper

def run(*, as_json: bool = False, local: bool = False, online: bool = False,
        mode: str = "background", repository: str | None = None,
        frozen: Mapping[str, Any] | None = None) -> int:
    """Print a diagnostic report without changing any local state."""

    report = collect(local=local, online=online, mode=mode, repository=repository, frozen=frozen)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(report)
    return 0


def collect(*, local: bool = False, online: bool = False, mode: str = "background",
            repository: str | None = None, frozen: Mapping[str, Any] | None = None) -> dict[str, object]:
    """Collect the public, non-secret doctor report."""

    with child_subreaper():
        return _collect(local=local, online=online, mode=mode, repository=repository, frozen=frozen)


def _collect(*, local: bool, online: bool, mode: str, repository: str | None,
             frozen: Mapping[str, Any] | None) -> dict[str, object]:
    if local and online or mode not in {"foreground", "background"}:
        raise ValueError("Choose --local or --online and a supported mode name")
    if repository is not None:
        from agent_run.adapters.workspace import normalize_repository
        repository = normalize_repository(repository)
    try:
        document = dict(frozen) if frozen is not None else UserDefaultsStore().describe()
        github = document["github"]
        if not isinstance(github, Mapping):
            raise ValueError("Selected GitHub identity facts are unavailable")
        identities = identity_checks(github, online=False, repository=repository)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError):
        document = {}
        identities = {role: {"source": "unknown", "status": "invalid", "authentication": "not_verified"}
                      for role in ("runner", "worker")}
    host_selected = any(check["source"] == "host" for check in identities.values())
    checks = {
        "python": _python_check(),
        "sqlite": _sqlite_check(),
        "git": _git_check(),
        "codex": _codex_check(authenticate=not local),
        "openssl": _tool_check("openssl", version_arguments=("version",)),
        "bubblewrap": _bubblewrap_check(),
        "github": _github_check(authenticate=host_selected and not local and not online),
        "active_runner": _active_runner_check(),
        "path": _path_check(),
        "worker_read_provider": {"provider": identities["worker"]["source"], "status": identities["worker"]["status"]},
        "runner_identity": {"provider": identities["runner"]["source"], "status": identities["runner"]["status"]},
    }
    repository_read: dict[str, object] = {"status": "not_verified"}
    if online and _check_is_ok(checks["github"]) and document:
        identities = identity_checks(document["github"], online=True, repository=repository)
        for role, key in (("worker", "worker_read_provider"), ("runner", "runner_identity")):
            checks[key] = {"provider": identities[role]["source"], "status": identities[role]["status"]}
        if repository is not None and identities["runner"]["authentication"] == "verified":
            from agent_run.adapters.github import GhGitHubReader
            try:
                observed = GhGitHubReader(repository, runner_identity=document["github"]["runner"]).repository()
                if observed.name_with_owner.casefold() != repository.casefold():
                    raise ValueError("Repository identity changed")
                repository_read = {"status": "verified", "repository": observed.name_with_owner}
            except (ValueError, OSError, RuntimeError):
                repository_read = {"status": "unavailable", "reason": "无法用所选 Runner 读取仓库及默认分支；检查授权与网络"}
    installation_keys = ("python", "sqlite", "active_runner", "path")
    installation = {
        "status": (
            "ready"
            if all(_check_is_ok(checks[key]) for key in installation_keys)
            else "issues"
        ),
        "checks": list(installation_keys),
    }
    background = _execution_readiness()
    foreground = _foreground_readiness()
    execution = dict(background if mode == "background" else foreground)
    execution["mode"] = mode
    execution_keys: tuple[str, ...] = (
        "sqlite", "git", "codex", "openssl", "bubblewrap", "github", "worker_read_provider", "runner_identity",
    )
    if online and repository is not None:
        checks["repository_read"] = {"status": "ok" if repository_read["status"] == "verified" else "unavailable"}
        execution_keys += ("repository_read",)
    execution["checks"] = list(execution_keys)
    missing = [key for key in execution_keys if not _check_is_ok(checks[key])]
    if execution.get("status") in {"ok", "ready"} and missing:
        execution["status"] = "unavailable"
        execution["reason"] = "执行依赖不可用：" + ", ".join(missing)
    return {
        "result": "doctor",
        "status": (
            "ready"
            if all(_check_is_ok(check) for check in checks.values())
            and execution.get("status") in {"ok", "ready"}
            else "issues"
        ),
        "checks": checks,
        "installation_readiness": installation,
        "execution_readiness": execution,
        "identities": identities,
        "configuration": {"scope": "run" if frozen is not None else "future-runs",
                          "run_id": document.get("run_id"), "source": document.get("source")},
        "verification": {"scope": "online" if online else "local" if local else "legacy-host-check",
                         "authentication": "verified" if all(value["authentication"] == "verified" for value in identities.values())
                         else "unavailable" if any(value["status"] != "ok" for value in identities.values()) else "not_verified",
                         "model_call": False, "repository_permissions": "not_verified",
                         "worker_environment": "not_verified", "repository_read": repository_read},
        "modes": {"background": background, "foreground": foreground},
        "skills": skills_check(),
        "next_steps": next_steps(),
    }


def _foreground_readiness() -> dict[str, object]:
    from agent_run.runtime.foreground_host import ForegroundHost
    try:
        ForegroundHost(environment=os.environ).preflight()
        return {"status": "ready", "host": "foreground", "business_execution": "not_verified"}
    except (OSError, ValueError, RuntimeError):
        return {"status": "unavailable", "host": "foreground",
                "reason": "前台 Host 的进程域观察能力不可用；不会降级或绕过隔离",
                "business_execution": "not_verified"}


def _sqlite_check() -> dict[str, object]:
    try:
        require_wal_runtime()
    except ValueError:
        return {"status": "unsupported", "version": sqlite3.sqlite_version,
                "reason": "Runner 解释器实际加载的 SQLite 缺少支持的 WAL-reset 修复；见状态存储文档"}
    return {"status": "ok", "version": sqlite3.sqlite_version}


def _execution_readiness() -> dict[str, object]:
    """Use the same concrete admission probe as the native execution Host."""
    host = SystemdHost(systemctl=shutil.which("systemctl") or "systemctl",
                       systemd_run=shutil.which("systemd-run") or "systemd-run",
                       environment=os.environ)
    try:
        host.preflight()
    except HostUnavailable as error:
        return {"status": "unavailable", "host": "systemd-user", "linger_required": False,
                "reason": str(error)}
    return {"status": "ok", "host": "systemd-user", "linger_required": False, "reason": None}


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


def _tool_check(
    command: str, *, version_arguments: tuple[str, ...] = ("--version",)
) -> dict[str, object]:
    executable = shutil.which(command)
    if executable is None:
        return {"status": "missing", "path": None}
    returncode, timed_out, _ = run_bounded_probe([executable, *version_arguments], max_output_bytes=None)
    if returncode is None:
        return {
            "status": "timeout" if timed_out else "unavailable",
            "path": executable,
        }
    return {
        "status": "ok" if returncode == 0 else "unavailable",
        "path": executable,
    }


def _git_check() -> dict[str, object]:
    executable = shutil.which("git")
    if executable is None:
        return {"status": "missing", "path": None}
    tool: dict[str, object] = {"status": "ok", "path": executable}
    code, timed_out, output = run_bounded_probe(
        [executable, "--version"], input_data=b""
    )
    if code != 0:
        return {**tool, "status": "timeout" if timed_out else "unavailable"}
    match = re.match(rb"git version (\d{1,4})\.(\d{1,4})(?:\.|\s|$)", output)
    if code != 0 or match is None or tuple(map(int, match.groups())) < (2, 40):
        return {**tool, "status": "unsupported", "reason": "需要 Git 2.40 或更新版本"}
    return tool


def _codex_check(*, authenticate: bool = True) -> dict[str, object]:
    tool = _tool_check("codex")
    executable = tool.get("path")
    if tool["status"] != "ok" or not isinstance(executable, str):
        return tool
    required = (b"--json", b"--output-last-message", b"--output-schema",
                b"--dangerously-bypass-approvals-and-sandbox", b"--model", b"--config")
    for command in (["exec", "--help"], ["exec", "resume", "--help"]):
        code, timed_out, output = run_bounded_probe(
            [executable, *command], input_data=b"", max_output_bytes=16 * 1024
        )
        flags = required + ((b"--cd", b"--color") if command == ["exec", "--help"] else ())
        if code != 0 or any(flag not in output for flag in flags):
            return {**tool, "status": "timeout" if timed_out else "unsupported",
                    "reason": "Codex exec/resume 缺少生产调用选项；请更新 Codex"}
    if not authenticate:
        return {**tool, "logged_in": None, "authentication": "not_checked"}
    code, timed_out, _ = run_bounded_probe([executable, "login", "status"], max_output_bytes=None)
    return {**tool, "status": "ok" if code == 0 else (
        "timeout" if timed_out else "not_logged_in"), "logged_in": code == 0,
        "reason": None if code == 0 else "请自行执行 codex login，然后重跑 doctor"}


def _bubblewrap_check() -> dict[str, object]:
    tool = _tool_check("bwrap")
    executable = tool.get("path")
    if tool["status"] != "ok" or not isinstance(executable, str):
        return tool
    # Exercise the mount, device and PID namespace operations used by Workers,
    # without a model call or writes to the host filesystem.
    code, timed_out, _ = run_bounded_probe([
        executable, "--die-with-parent", "--ro-bind", "/", "/",
        "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--",
        "/bin/true",
    ], max_output_bytes=None)
    return {**tool, "status": "ok" if code == 0 else (
        "timeout" if timed_out else "unavailable"), "reason": None if code == 0 else
        "bubblewrap 无法创建 Worker 所需 mount/device/PID namespace；请检查宿主策略"}


def _github_check(*, authenticate: bool = True) -> dict[str, object]:
    tool = _tool_check("gh")
    if tool.get("status") != "ok":
        return {**tool, "logged_in": False}
    executable = tool.get("path")
    if not isinstance(executable, str):
        return {"status": "unavailable", "logged_in": False}
    code, timed_out, output = run_bounded_probe(
        [executable, "api", "--help"], input_data=b"", max_output_bytes=32 * 1024,
    )
    if code != 0 or any(flag not in output for flag in (b"--paginate", b"--slurp", b"--method", b"--header")):
        return {**tool, "status": "timeout" if timed_out else "unsupported",
                "logged_in": False, "reason": "gh api 缺少生产调用选项；请更新 GitHub CLI"}
    if not authenticate:
        return {**tool, "logged_in": None, "authentication": "not_checked"}
    returncode, timed_out, _ = run_bounded_probe([executable, "auth", "status"], max_output_bytes=None)
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
        current, previous, generation = read_active(paths)
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


def _check_is_ok(value: object) -> bool:
    return isinstance(value, dict) and value.get("status") == "ok"


def _print_human(report: dict[str, object]) -> None:
    checks = report["checks"]
    if not isinstance(checks, dict):
        raise ValueError("doctor checks must be an object")
    labels = (
        ("python", "Python"),
        ("sqlite", "SQLite"),
        ("git", "Git"),
        ("codex", "Codex"),
        ("openssl", "OpenSSL"),
        ("bubblewrap", "bubblewrap"),
        ("github", "GitHub CLI / 宿主登录（仅所选 host）"),
        ("active_runner", "Active Runner"),
        ("path", "PATH"),
        ("worker_read_provider", "Worker GitHub identity"),
        ("runner_identity", "Runner GitHub identity"),
    )
    print("agent-run doctor（只读诊断）")
    for key, label in labels:
        check = checks.get(key)
        if not isinstance(check, dict):
            print(f"{label}: unavailable")
            continue
        detail: object = None
        if key == "github":
            detail = "未验证登录" if check.get("logged_in") is None else "已登录" if check.get("logged_in") else "未登录"
        elif key in {"worker_read_provider", "runner_identity"}:
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
        if check.get("reason"):
            print(f"  {check['reason']}")
    installation = report.get("installation_readiness")
    execution = report.get("execution_readiness")
    if isinstance(installation, dict):
        print(f"安装就绪度: {installation.get('status')}")
    if isinstance(execution, dict):
        print(
            f"执行就绪度: {execution.get('status')} "
            f"({execution.get('host')})"
        )
        if execution.get("reason"):
            print(f"  {execution['reason']}")
    print(f"总体: {report.get('status')}")
    verification = report.get("verification")
    if isinstance(verification, dict):
        print(f"验证范围: {verification['scope']}；所选身份认证: {verification['authentication']}")
    skills = report.get("skills")
    if isinstance(skills, dict):
        print(f"用户 Skills 入口: {skills['status']}；引用依赖与 Worker 发现尚未验证")
    print("此结果不是仓库写权限、Worker 内部认证或真实交付证明；未调用模型。")
    for step in next_steps():
        print(f"  {step}")
