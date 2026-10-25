"""Read-only original Host exit evidence and committed notification facts.

This leaf verifies identity and projects facts; it never calls a sender.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any

from agent_run.contracts.task import TaskKey

from agent_run.core.kernel import Kernel
from agent_run.adapters.systemd import HostUnavailable, SystemdHost
from agent_run.errors import KernelError
from agent_run.runtime.handoff import _BOOT, _digest, _runner_witness
from agent_run.storage.tasks import read_task, task_path


_RESULTS = {"signal", "core-dump", "oom-kill", "exit-code", "timeout", "success"}


class HostExitUnavailable(RuntimeError):
    """Currentness cannot be read; preserve the original notification budget."""


def _abnormal(result: object, code: object, status: object) -> bool:
    if result not in _RESULTS or not isinstance(status, str):
        return False
    if code == "exited":
        return status.isascii() and status.isdecimal() and 0 < int(status) <= 255
    return code in {"killed", "dumped"} and bool(re.fullmatch(r"[A-Z][A-Z0-9+-]{0,31}", status))


def host_exit_current(database: Path, notice: Mapping[str, Any]) -> bool:
    """Recheck the original Task, Host and Runner without recording any state."""
    if notice.get('backend') == 'foreground':
        current = foreground_exit_notice(database, str(notice.get('action_id')), Path(str(notice.get('physical_record'))))
        return current is not None and current == notice
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
        state = kernel.operation_state()
        return not (not state.active and state.disposition in {"complete", "quiescent"})
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


def foreground_exit_notice(database: Path, action_id: str, path: Path) -> dict[str, Any] | None:
    """Read the exact persisted physical exit before Core retires this Host.

    No cgroup/unit facts are invented. Missing executor status remains unknown;
    it is not silently turned into a successful command or a guessed signal.
    """
    from agent_run.adapters.foreground import DomainUnknown, read_record, record_root
    try:
        kernel = Kernel(database)
        request, usage = kernel.host_request(action_id), kernel.host_usage(action_id)
        if request.backend != 'foreground' or usage is None:
            return None
        receipt = json.loads(usage.evidence)
        lock = Path(receipt['runner']['lock'])
        if path != record_root(lock) / (request.nonce + '.json'):
            return None
        record = read_record(path)
        from agent_run.contracts.host import valid_foreground_domain
        domain = record['domain']
        if (not isinstance(domain, dict) or not valid_foreground_domain(domain, request.nonce)
                or (domain != usage.domain if usage.domain is not None else domain['executor'] is not None)
                or record['phase'] != 'exited' or record['receipt'] != receipt
                or receipt['request_digest'] != _digest(request) or receipt['runner'] != _runner_witness(lock)
                or record['binding'] != [request.run_id, request.owner, request.epoch, request.nonce]
                or record.get('executor_exit_code') == 0):
            return None
        with kernel.storage.transaction(write=False) as tx:
            run = tx.one('SELECT * FROM runs')
            host = tx.one('SELECT * FROM hosts WHERE action_id=?', (action_id,))
            task = TaskKey(Path(run['repository_root']), run['repository'], run['parent_issue'])
            if (read_task(task_path(task)) != (run['id'], database.resolve()) or run['stopped']
                    or (run['id'], run['executor'], run['epoch']) != (request.run_id, request.owner, request.epoch)
                    or host['status'] == 'exited' or tx.rows("SELECT id FROM actions WHERE kind='abandon' LIMIT 1")):
                return None
            originals = tx.rows('SELECT subject,material_id FROM run_inputs WHERE run_id=?', (run['id'],))
            if not originals:return None
            inputs = originals[0]
            config = json.loads(tx.get(inputs['material_id'], inputs['subject'])).get('notifications')
            if not isinstance(config, dict) or config.get('enabled') is not True:
                return None
        state = kernel.operation_state()
        if not state.active and state.disposition in {'complete', 'quiescent'}:
            return None
        return {'backend': 'foreground', 'physical_record': str(path), 'action_id': action_id,
                'run_id': request.run_id, 'owner': request.owner, 'epoch': request.epoch, 'nonce': request.nonce,
                'runner': receipt['runner'], 'request_digest': receipt['request_digest'],
                'executor_exit_code': record.get('executor_exit_code'), 'ended_at': record['ended_at'],
                'domain': record['domain']}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, KernelError, DomainUnknown) as error:
        raise HostExitUnavailable('Foreground exit currentness is unavailable') from error
