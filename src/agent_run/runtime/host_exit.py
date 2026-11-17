"""Compose the exact manager post-exit hook with the bounded sender.

Identity and committed fact checks live in host_exit_facts; only this hook
connects those observations to notification delivery.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys
from typing import Any

from agent_run.storage.run_scope import run_statement
from agent_run.runtime.package_lease import runner_usage_lease

from agent_run.runtime.host_recovery import HostExitUnavailable, _abnormal, host_exit_notice
from agent_run.core.kernel import Kernel
from agent_run.model import HostLaunch
from agent_run.runtime.handoff import read_exit_environment
from agent_run.storage.routing import open_run
from agent_run.storage.sqlite import Storage


def exit_properties(database: Path, action_id: str, request: HostLaunch, runner_lock: Path,
                    environment: Mapping[str, str], *, storage: Storage) -> tuple[str, ...]:
    """Arm only this Run's enabled hook, without interpreting the Host command."""
    kernel = Kernel._from_storage(open_run(database, storage=storage))
    if kernel.host_request(action_id) != request:
        raise ValueError("Host exit request binding changed")
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one(*run_statement(tx, 'SELECT id FROM runs WHERE id=?', ()))
        originals = tx.rows("SELECT subject,material_id FROM run_inputs WHERE run_id=?", (run["id"],))
        if not originals:
            return ()  # No frozen Run inputs; neither channel can be bound.
        original = originals[0]
        config = json.loads(tx.get(original["material_id"], original["subject"])).get("notifications")
    from agent_run.queries.status_comment import status_subjects
    try:
        status_subjects(database, **({"storage": storage}))
        status_enabled = True
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        status_enabled = False
    feishu_enabled = isinstance(config, dict) and config.get("enabled") is True
    if not feishu_enabled and not status_enabled:
        return ()
    if not database.is_absolute() or not runner_lock.is_absolute() or not Path(sys.executable).is_absolute():
        raise ValueError("An exact Host exit command is required")
    bootstrap = ("import sys;sys.path.insert(0,sys.argv[1]);"
                 "from agent_run.runtime.host_exit import main;raise SystemExit(main(sys.argv[2:]))")
    arguments = [sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parents[2]),
                 "--database", str(database), "--action", action_id, "--runner-lock", str(runner_lock)]
    if storage.run_id != request.run_id or "\0" in request.run_id:
        raise ValueError("Host exit Run identity changed")
    arguments.append("--run-id=" + request.run_id)
    library = environment.get("LD_LIBRARY_PATH", "")
    if "\0" in library:
        raise ValueError("Invalid captured library path")
    if library:
        arguments = ["/usr/bin/env", "LD_LIBRARY_PATH=" + library, *arguments]
    rendered = " ".join(json.dumps(value.replace("%", "%%"), ensure_ascii=False) for value in arguments)
    return ("ExecStopPost=-:" + rendered, "TimeoutStopSec=70s" if feishu_enabled else "TimeoutStopSec=5s",
            "TimeoutStopFailureMode=kill", "OOMPolicy=continue")


def main(arguments: Sequence[str] | None = None) -> int:
    """Consume one manager exit, never claim startup or change execution state."""
    parser = argparse.ArgumentParser(description="Exact native Host post-exit notification")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--action", required=True)
    parser.add_argument("--runner-lock", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(arguments)
    result, code, status, invocation = (os.environ.get(key, "") for key in
                                       ("SERVICE_RESULT", "EXIT_CODE", "EXIT_STATUS", "INVOCATION_ID"))
    abnormal = _abnormal(result, code, status)
    if not abnormal and (result, code, status) != ("success", "exited", "0"):
        return 0
    try:
        credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if (not args.database.is_absolute() or not args.runner_lock.is_absolute()
                or not credential_directory or not Path(credential_directory).is_absolute()):
            raise HostExitUnavailable("Host exit credential is unavailable")
        kernel = Kernel(args.database, run_id=args.run_id)
        options: dict[str, Any] = {"storage": kernel.storage}
        request = kernel.host_request(args.action)
        with runner_usage_lease(args.runner_lock):
            inherited = read_exit_environment(request, runner_lock=args.runner_lock,
                                               credential=Path(credential_directory) / "agent-run-environment")
            os.environ.clear()
            os.environ.update(inherited.environment)
            notice = None
            if abnormal:
                notice = host_exit_notice(args.database, args.action, inherited.receipt,
                                          service_result=result, exit_code=code, exit_status=status,
                                          invocation_id=invocation, **options)
                if notice is not None:
                    from agent_run.notifications.sender import deliver_host_exit_notice
                    deliver_host_exit_notice(args.database, args.database.parent, notice, **options)
            if notice is None:
                from agent_run.queries.status_comment import status_subjects
                try:
                    status_subjects(args.database, **options)
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    return 0
                from agent_run.adapters.systemd import SystemdHost
                manager = SystemdHost(systemctl=shutil.which("systemctl") or "systemctl",
                                      systemd_run="systemd-run", environment=os.environ)
                if manager.exit_identity(request, invocation):
                    # Stop-post still belongs to the unit. Refresh committed
                    # facts, never infer empty cgroup or released Runner usage.
                    from agent_run.notifications.sender import notify_committed
                    notify_committed(args.database, **options)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # Credentials, arbitrary CLI output and native metadata stay private.
        print("Host exit notification observation unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
