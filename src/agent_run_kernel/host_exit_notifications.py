"""Exact post-exit notification facts; no Host retirement or business writes.

The service manager owns the main-process exit evidence. The existing Kernel
owns quiescence; the existing notification sender owns every delivery attempt.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from typing import Any

from agent_run.notification_event_facts import event_copy, event_envelope
from agent_run.task_identity import TaskKey
from agent_run.runner_lease import runner_usage_lease

from .kernel import Kernel
from .host_adapter import HostUnavailable, SystemdHost
from .model import HostLaunch, KernelError
from .native_host import _BOOT, _digest, _runner_witness, read_exit_environment
from .task_location import read_task, task_path


_RESULTS = {"signal", "core-dump", "oom-kill", "exit-code", "timeout", "success"}


class HostExitUnavailable(RuntimeError):
    """Currentness cannot be read; preserve the original notification budget."""


def exit_properties(database: Path, action_id: str, request: HostLaunch, runner_lock: Path,
                    environment: Mapping[str, str]) -> tuple[str, ...]:
    """Arm only this Run's enabled hook, without interpreting the Host command."""
    kernel = Kernel(database)
    if kernel.host_request(action_id) != request:
        raise ValueError("Host exit request binding changed")
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT id FROM runs")
        originals = tx.rows("SELECT subject,material_id FROM run_inputs WHERE run_id=?", (run["id"],))
        if not originals:
            return ()  # No frozen opt-in; do not arm a notification hook.
        original = originals[0]
        config = json.loads(tx.get(original["material_id"], original["subject"])).get("notifications")
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return ()
    if not database.is_absolute() or not runner_lock.is_absolute() or not Path(sys.executable).is_absolute():
        raise ValueError("An exact Host exit command is required")
    bootstrap = ("import sys;sys.path.insert(0,sys.argv[1]);"
                 "from agent_run_kernel.host_exit_notifications import main;raise SystemExit(main(sys.argv[2:]))")
    arguments = [sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parents[1]),
                 "--database", str(database), "--action", action_id, "--runner-lock", str(runner_lock)]
    library = environment.get("LD_LIBRARY_PATH", "")
    if "\0" in library:
        raise ValueError("Invalid captured library path")
    if library:
        arguments = ["/usr/bin/env", "LD_LIBRARY_PATH=" + library, *arguments]
    rendered = " ".join(json.dumps(value.replace("%", "%%"), ensure_ascii=False) for value in arguments)
    return ("ExecStopPost=-:" + rendered, "TimeoutStopSec=70s", "TimeoutStopFailureMode=kill", "OOMPolicy=continue")


def _abnormal(result: object, code: object, status: object) -> bool:
    if result not in _RESULTS or not isinstance(status, str):
        return False
    if code == "exited":
        return status.isascii() and status.isdecimal() and 0 < int(status) <= 255
    return code in {"killed", "dumped"} and bool(re.fullmatch(r"[A-Z][A-Z0-9+-]{0,31}", status))


def host_exit_current(database: Path, notice: Mapping[str, Any]) -> bool:
    """Recheck the original Task, Host and Runner without recording any state."""
    try:
        if (database.is_symlink() or not _abnormal(notice.get("service_result"), notice.get("exit_code"),
                                                  notice.get("exit_status"))):
            return False
        kernel = Kernel(database)
        with kernel.storage.transaction(write=False) as tx:
            if not tx.rows("SELECT action_id FROM hosts WHERE action_id=?", (notice["action_id"],)):
                return False
        request = kernel.host_request(notice["action_id"])
        usage = kernel.host_usage(notice["action_id"])
        if usage is None:
            return False
        receipt = json.loads(usage.evidence)
        runner = notice["runner"]
        if (not isinstance(runner, dict) or receipt.get("runner") != runner
                or runner != _runner_witness(Path(runner["lock"]))
                or receipt.get("request_digest") != notice["request_digest"]
                or notice["request_digest"] != _digest(request)
                or receipt.get("nonce") != request.nonce
                or receipt.get("boot_id") != notice["boot_id"]
                or notice["boot_id"] != _BOOT.read_text().strip()
                or receipt.get("pid_namespace") != notice["pid_namespace"]
                or notice["pid_namespace"] != os.readlink("/proc/self/ns/pid")):
            return False
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            host = tx.one("SELECT * FROM hosts WHERE action_id=?", (notice["action_id"],))
            task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
            if (read_task(task_path(task)) != (run["id"], database.resolve())
                    or task.identity != {"workspace": notice["workspace"], "repository": notice["repository"],
                                         "parent": notice["parent"]}
                    or (run["id"], run["epoch"], run["executor"])
                    != (notice["run_id"], notice["epoch"], notice["owner"])
                    or (request.run_id, request.epoch, request.owner, request.nonce, request.unit_name)
                    != (notice["run_id"], notice["epoch"], notice["owner"], notice["nonce"], notice["unit_name"])
                    or host["dispatched_at"] is None
                    or host["status"] == "exited" or run["stopped"] or run["language"] not in {"zh", "en"}
                    or tx.rows("SELECT id FROM actions WHERE kind='abandon' LIMIT 1")):
                return False
            startup = host["invocation_id"] is None
            if startup:
                if (host["usage_ready"] or any(host[key] is not None for key in ("main_pid", "start_token", "cgroup", "boot_id"))
                        or any(notice[key] is not None for key in ("pid", "process_start_token", "cgroup"))):
                    return False
            elif (not host["usage_ready"]
                  or (host["invocation_id"], host["main_pid"], host["start_token"], host["cgroup"], host["boot_id"])
                  != (notice["invocation_id"], notice["pid"], notice["process_start_token"], notice["cgroup"], notice["boot_id"])):
                return False
            inputs = tx.one("SELECT subject,material_id FROM run_inputs WHERE run_id=?", (run["id"],))
            config = json.loads(tx.get(inputs["material_id"], inputs["subject"]))["notifications"]
            if not isinstance(config, dict) or config.get("enabled") is not True:
                return False
        if startup:
            manager = SystemdHost(systemctl=shutil.which("systemctl") or "systemctl",
                                  systemd_run="systemd-run", environment=os.environ)
            if not manager.exit_identity(request, notice["invocation_id"]):
                return False
        scheduling = kernel.scheduling()
        return not (not scheduling["active"] and scheduling["disposition"] in {"complete", "quiescent"})
    except (KernelError, OSError, ValueError, HostUnavailable, sqlite3.Error) as error:
        raise HostExitUnavailable("Host exit currentness is unavailable") from error
    except (KeyError, TypeError):
        return False


def host_exit_notice(database: Path, action_id: str, receipt: Mapping[str, Any], *,
                     service_result: str, exit_code: str, exit_status: str,
                     invocation_id: str) -> dict[str, Any] | None:
    """Project an exact manager exit and original durable receipt for the sender."""
    if not _abnormal(service_result, exit_code, exit_status):
        return None
    kernel = Kernel(database)
    request, usage = kernel.host_request(action_id), kernel.host_usage(action_id)
    if usage is None or json.loads(usage.evidence) != receipt:
        return None
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT * FROM runs")
        host = tx.one("SELECT * FROM hosts WHERE action_id=?", (action_id,))
        notice = {"workspace": run["repository_root"], "repository": run["repository"], "parent": run["parent_issue"],
                  "run_id": request.run_id, "action_id": action_id, "epoch": request.epoch,
                  "owner": request.owner, "nonce": request.nonce, "unit_name": request.unit_name,
                  "invocation_id": invocation_id, "request_digest": receipt.get("request_digest"),
                  "runner": receipt.get("runner"), "boot_id": receipt.get("boot_id"),
                  "pid_namespace": receipt.get("pid_namespace"), "pid": host["main_pid"],
                  "process_start_token": host["start_token"], "cgroup": host["cgroup"],
                  "service_result": service_result, "exit_code": exit_code, "exit_status": exit_status,
                  "observed_at": datetime.now(UTC).isoformat()}
    return notice if host_exit_current(database, notice) else None


def host_exit_event(database: Path, notice: Mapping[str, Any]) -> dict[str, Any] | None:
    """Use the original notification envelope and stable Host exit identity."""
    if not host_exit_current(database, notice):
        return None
    with Kernel(database).storage.transaction(write=False) as tx:
        run = tx.one("SELECT id,repository,parent_issue,parent_revision,language,created_at FROM runs")
        title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                        (run["parent_issue"], run["parent_revision"]))
        state = {"run_id": run["id"], "repository": run["repository"], "language": run["language"],
                 "created_at": run["created_at"],
                 "parent": {"number": run["parent_issue"], "title": title[0][0] if title else ""}}
    return event_envelope(state, "executor_exit", [notice["action_id"], notice["epoch"], notice["nonce"], notice["runner"]],
                          event_copy(state, "executor_exit"), "red", important=True,
                          generation=notice["epoch"], exit_evidence=dict(notice),
                          summary=event_copy(state, "executor_exit_summary"),
                          next_step=event_copy(state, "execution_next"))


def main(arguments: Sequence[str] | None = None) -> int:
    """Consume one manager exit, never claim startup or change execution state."""
    parser = argparse.ArgumentParser(description="Exact native Host post-exit notification")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--runner-lock", type=Path, required=True)
    args = parser.parse_args(arguments)
    result, code, status, invocation = (os.environ.get(key, "") for key in
                                       ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS", "INVOCATION_ID"))
    if not _abnormal(result, code, status):
        return 0
    try:
        credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if (not args.database.is_absolute() or not args.runner_lock.is_absolute()
                or not credential_directory or not Path(credential_directory).is_absolute()):
            raise HostExitUnavailable("Host exit credential is unavailable")
        kernel = Kernel(args.database)
        request = kernel.host_request(args.action)
        with runner_usage_lease(args.runner_lock):
            inherited = read_exit_environment(request, runner_lock=args.runner_lock,
                                               credential=Path(credential_directory) / "agent-run-environment")
            os.environ.clear()
            os.environ.update(inherited.environment)
            notice = host_exit_notice(args.database, args.action, inherited.receipt,
                                      service_result=result, exit_code=code, exit_status=status,
                                      invocation_id=invocation)
            if notice is not None:
                from .notifications import deliver_host_exit_notice
                deliver_host_exit_notice(args.database, args.database.parent, notice)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # Credentials, arbitrary CLI output and native metadata stay private.
        print("Host exit notification observation unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
