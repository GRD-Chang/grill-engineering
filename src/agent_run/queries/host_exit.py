"""Read-only original Host exit evidence and committed notification facts.

This leaf verifies identity and projects facts; it never calls a sender.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any

from agent_run.storage.run_scope import run_statement, transaction_scope
from agent_run.contracts.task import TaskKey

from agent_run.core.kernel import Kernel
from agent_run.adapters.systemd import HostUnavailable, SystemdHost
from agent_run.errors import KernelError
from agent_run.runtime.handoff import _BOOT, _digest, _runner_witness
from agent_run.storage.tasks import run_is_current
from agent_run.storage.routing import open_run
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.model import HostObservation


_RESULTS = {"signal", "core-dump", "oom-kill", "exit-code", "timeout", "success"}


class HostExitUnavailable(RuntimeError):
    """Currentness cannot be read; preserve the original notification budget."""


def _abnormal(result: object, code: object, status: object) -> bool:
    if result not in _RESULTS or not isinstance(status, str):
        return False
    if code == "exited":
        return status.isascii() and status.isdecimal() and 0 < int(status) <= 255
    return code in {"killed", "dumped"} and bool(re.fullmatch(r"[A-Z][A-Z0-9+-]{0,31}", status))


def host_exit_current(database: Path, notice: Mapping[str, Any], *, storage: Storage) -> bool:
    """Recheck original authority; foreground also confirms deletion durability."""
    if notice.get("run_id") != storage.run_id:
        return False
    options: dict[str, Any] = {"storage": storage}
    if notice.get('backend') == 'foreground':
        current = foreground_exit_notice(database, str(notice.get('action_id')), Path(str(notice.get('physical_record'))), **options)
        if current is None or current != notice:
            return False
        # Outside the SQLite snapshot: absence alone does not prove the prior
        # unlink's directory sync succeeded. Never retry deletion here.
        from agent_run.runtime.foreground_host import _confirm_record_removed
        from agent_run.adapters.foreground import DomainUnknown
        try:
            _confirm_record_removed(Path(current['physical_record']))
        except (OSError, ValueError, DomainUnknown) as error:
            raise HostExitUnavailable('Foreground record removal is unavailable') from error
        return foreground_exit_notice(database, str(current['action_id']), Path(current['physical_record']), **options) == current
    try:
        if (database.is_symlink() or not _abnormal(notice.get("service_result"), notice.get("exit_code"),
                                                  notice.get("exit_status"))):
            return False
        kernel = Kernel._from_storage(open_run(database, storage=storage))
        with kernel.storage.transaction(write=False) as tx:
            if not tx.rows(*run_statement(tx, 'SELECT action_id FROM hosts WHERE run_id=? AND action_id=?', (notice["action_id"],))):
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
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?', ()))
            host = tx.one(*run_statement(tx, 'SELECT * FROM hosts WHERE run_id=? AND action_id=?', (notice["action_id"],)))
            task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
            if (not run_is_current(tx)
                    or task.identity != {"workspace": notice["workspace"], "repository": notice["repository"],
                                         "parent": notice["parent"]}
                    or (run["id"], run["epoch"], run["executor"])
                    != (notice["run_id"], notice["epoch"], notice["owner"])
                    or (request.run_id, request.epoch, request.owner, request.nonce, request.unit_name)
                    != (notice["run_id"], notice["epoch"], notice["owner"], notice["nonce"], notice["unit_name"])
                    or host["dispatched_at"] is None
                    or host["status"] == "exited" or run["stopped"] or run["language"] not in {"zh", "en"}
                    or tx.rows(*run_statement(tx, "SELECT id FROM actions WHERE run_id=? AND kind='abandon' LIMIT 1", ()))):
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
                     invocation_id: str, storage: Storage) -> dict[str, Any] | None:
    """Project an exact manager exit and original durable receipt for the sender."""
    if not _abnormal(service_result, exit_code, exit_status):
        return None
    kernel = Kernel._from_storage(open_run(database, storage=storage))
    request, usage = kernel.host_request(action_id), kernel.host_usage(action_id)
    if usage is None or json.loads(usage.evidence) != receipt:
        return None
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?', ()))
        host = tx.one(*run_statement(tx, 'SELECT * FROM hosts WHERE run_id=? AND action_id=?', (action_id,)))
        notice = {"workspace": run["repository_root"], "repository": run["repository"], "parent": run["parent_issue"],
                  "run_id": request.run_id, "action_id": action_id, "epoch": request.epoch,
                  "owner": request.owner, "nonce": request.nonce, "unit_name": request.unit_name,
                  "invocation_id": invocation_id, "request_digest": receipt.get("request_digest"),
                  "runner": receipt.get("runner"), "boot_id": receipt.get("boot_id"),
                  "pid_namespace": receipt.get("pid_namespace"), "pid": host["main_pid"],
                  "process_start_token": host["start_token"], "cgroup": host["cgroup"],
                  "service_result": service_result, "exit_code": exit_code, "exit_status": exit_status,
                  "observed_at": datetime.now(UTC).isoformat()}
    return notice if host_exit_current(database, notice, **({"storage": storage})) else None


def foreground_exit_notice(database: Path, action_id: str, path: Path, *, storage: Storage) -> dict[str, Any] | None:
    """Read the exact physical receipt or its accepted retired Host facts.

    No cgroup/unit facts are invented. Missing executor status remains unknown;
    it is not silently turned into a successful command or a guessed signal.
    """
    from agent_run.adapters.foreground import DomainUnknown, read_record, record_root
    try:
        kernel = Kernel._from_storage(open_run(database, storage=storage))
        request, usage = kernel.host_request(action_id), kernel.host_usage(action_id)
        if request.backend != 'foreground' or usage is None:
            return None
        if usage.disposition == 'exited' and usage.usage_released:
            with kernel.storage.transaction(write=False) as tx:
                notice = retired_foreground_exit_notice(tx, action_id=action_id)
            return notice if notice is not None and notice['physical_record'] == str(path) else None
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
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?', ()))
            host = tx.one(*run_statement(tx, 'SELECT * FROM hosts WHERE run_id=? AND action_id=?', (action_id,)))
            if (not run_is_current(tx) or run['stopped']
                    or (run['id'], run['executor'], run['epoch']) != (request.run_id, request.owner, request.epoch)
                    or host['status'] == 'exited' or tx.rows(*run_statement(tx, "SELECT id FROM actions WHERE run_id=? AND kind='abandon' LIMIT 1", ()))):
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


def retired_foreground_exit_notice(tx: Transaction, *, action_id: str | None = None,
                                   nonce: str | None = None) -> dict[str, Any] | None:
    """Project an accepted original exit; no physical record or PID is needed.

    The interruption event records applicability before retirement changed the
    leases, gates or integration seat. Current state only fences its delivery.
    """
    from agent_run.contracts.host import valid_foreground_domain

    selected = transaction_scope(tx)
    hosts = tx.rows('SELECT * FROM hosts WHERE run_id=? AND ' +
                    ('action_id=?' if action_id is not None else 'nonce=?'),
                    (selected.run_id, action_id if action_id is not None else nonce), limit=1)
    if not hosts:
        return None
    host = hosts[0]
    if (host['backend'] != 'foreground' or host['status'] != 'exited'
            or not host['usage_released'] or host['dispatched_at'] is None
            or host['observation_id'] is None or host['domain_id'] is None):
        return None
    request = Kernel._host_launch(tx, host)
    observation = HostObservation(**json.loads(tx.get(host['observation_id'], host['subject'])))
    domain = json.loads(tx.get(host['domain_id'], host['subject']))
    ended = observation.ended_at
    if (not isinstance(ended, (int, float)) or isinstance(ended, bool)
            or not math.isfinite(ended) or ended <= 0):
        return None
    if (request.backend != 'foreground' or observation.backend != 'foreground' or observation.disposition != 'exited'
            or not observation.usage_released or observation.exit_status == 0
            or not valid_foreground_domain(domain, request.nonce) or observation.domain != domain
            or observation.exit_evidence not in {'retained_namespace_pidfd', 'same_observer_init_exit'}
            or any(value is not None and type(value) is not int for value in
                   (observation.exit_status, observation.namespace_exit_status))
            or (host['exit_status'], host['ended_at']) != (observation.exit_status, ended)
            or observation.boot_id != domain['observer']['boot_id']
            or (observation.unit_name, observation.invocation_id, observation.cgroup) != (None, None, None)):
        return None
    executor = domain['executor']
    identity = (None, None) if executor is None else (executor['pid'], executor['start_token'])
    if ((observation.main_pid, observation.start_token) != identity
            or (host['main_pid'], host['start_token']) != identity
            or host['boot_id'] != observation.boot_id
            or executor is None and observation.exit_status is not None):
        return None
    run = tx.one('SELECT * FROM runs WHERE id=?', (selected.run_id,))
    if (not run_is_current(tx) or run['stopped'] or run['language'] not in {'zh', 'en'}
            or run['executor'] is not None or run['epoch'] != request.epoch + 1
            or (host['run_id'], host['owner'], host['epoch'], host['nonce']) !=
               (request.run_id, request.owner, request.epoch, request.nonce)
            or tx.rows("SELECT id FROM actions WHERE run_id=? AND kind='abandon' LIMIT 1", (selected.run_id,))
            or tx.rows('SELECT owner FROM hosts WHERE run_id=? AND epoch>? LIMIT 1',
                       (selected.run_id, request.epoch))
            or not tx.rows("SELECT owner FROM retired_executors WHERE run_id=? AND owner=? AND epoch=? AND status='exited'",
                           (selected.run_id, request.owner, request.epoch))
            or not tx.rows("SELECT position FROM history WHERE run_id=? AND subject=? AND kind='host_interrupted' "
                           'AND identity=? AND material_id=? LIMIT 1',
                           (selected.run_id, host['subject'], request.nonce, host['observation_id']))):
        return None
    receipt = json.loads(observation.evidence)
    runner = receipt['runner']
    lock = Path(runner['lock'])
    if (receipt['request_digest'] != _digest(request) or receipt['nonce'] != request.nonce
            or runner != _runner_witness(lock) or receipt['boot_id'] != domain['observer']['boot_id']
            or receipt['pid_namespace'] != domain['observer']['pid_namespace']):
        return None
    inputs = tx.one('SELECT subject,material_id FROM run_inputs WHERE run_id=?', (selected.run_id,))
    config = json.loads(tx.get(inputs['material_id'], inputs['subject'])).get('notifications')
    if not isinstance(config, dict) or config.get('enabled') is not True:
        return None
    return {'backend': 'foreground', 'physical_record': str(lock.parent / 'foreground-hosts' / (request.nonce + '.json')),
            'action_id': host['action_id'], 'run_id': request.run_id, 'owner': request.owner,
            'epoch': request.epoch, 'nonce': request.nonce, 'runner': runner,
            'request_digest': receipt['request_digest'], 'executor_exit_code': observation.exit_status,
            'ended_at': ended, 'domain': domain}
