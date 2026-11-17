"""Coordinate admitted Host handoffs, attachment, leases and control receipts."""
from __future__ import annotations
import argparse
from collections.abc import Mapping
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, TYPE_CHECKING
from agent_run.errors import KernelError
from agent_run.storage.run_scope import run_statement
from agent_run.runtime.foreground_host import ForegroundHost
from agent_run.model import Delivery, HostLaunch, HostObservation, ResourceInspection, World
if TYPE_CHECKING:
    from agent_run.core.kernel import Kernel
    from agent_run.adapters.systemd import SystemdHost
    from agent_run.runtime.package_lease import RunnerLease

def _runtime_directory(action_id: str, environment: Mapping[str, str], *, run_partition: str) -> Path:
    from agent_run.config.paths import app_runtime_root
    if not re.fullmatch(r"[A-Za-z0-9_-]+", action_id):
        raise ValueError("Expected an exact Action identity")
    if type(run_partition) is not str or not re.fullmatch(r"[a-f0-9]{64}", run_partition):
        raise ValueError("Expected an exact Run carrier partition")
    root = app_runtime_root(environment)
    host_root = root / "native-host"
    directories = [root, host_root]
    host_root = host_root / run_partition
    directories.append(host_root)
    for directory in directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("Host runtime directory must be private and owned")
    directory = host_root / action_id
    directory.mkdir(mode=0o700)
    return directory


def notify_committed(kernel: Kernel) -> None:
    try:
        from agent_run.notifications.sender import notify_committed
        notify_committed(kernel.storage.path, storage=kernel.storage)
    except Exception:
        try:
            print("Notification observation unavailable; business continues", file=sys.stderr)
        except (OSError, ValueError):
            pass  # Diagnostic output cannot replace the business result.


def notify_after_usage(kernel: Kernel, observed: HostObservation | None) -> None:
    """Only a returned settlement proves this caller finished physical cleanup."""
    if observed is not None and observed.disposition == "exited" and observed.usage_released:
        notify_committed(kernel)


def submit(kernel: Kernel, kind: str, *, action_id: str, command: tuple[str, ...],
           manager: SystemdHost | ForegroundHost, environment: Mapping[str, str], runner_lock: Path,
           subject: str | None = None, world: World | None = None,
           message: str | None = None, new_thread: bool = False,
           policy_overrides: Mapping[str, Any] | None = None,
           replacement: Delivery | None = None,
           resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False,
           continuation_of: str | None = None) -> dict[str, Any]:
    """Submit one explicit Action, retaining its physical lease through settlement."""
    from agent_run.runtime.package_lease import runner_usage_lease
    observed: HostObservation | None = None
    try:
        with runner_usage_lease(runner_lock) as terminal:
            request = admit(kernel, kind, action_id=action_id, command=command,
                manager=manager, environment=environment, subject=subject, world=world,
                message=message, new_thread=new_thread, policy_overrides=policy_overrides,
                replacement=replacement, resource_inspections=resource_inspections,
                discard_worktree=discard_worktree, continuation_of=continuation_of)
            result, observed = launch_admitted(kernel, request, manager=manager, environment=environment,
                                       runner_lock=runner_lock, terminal=terminal, action_id=action_id, kind=kind)
            return result
    finally:
        if isinstance(manager, ForegroundHost):
            notify_after_usage(kernel, observed)


def admit(kernel: Kernel, kind: str, *, action_id: str, command: tuple[str, ...],
           manager: SystemdHost | ForegroundHost, environment: Mapping[str, str],
           subject: str | None = None, world: World | None = None,
           message: str | None = None, new_thread: bool = False,
           policy_overrides: Mapping[str, Any] | None = None,
           replacement: Delivery | None = None,
           resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False,
           continuation_of: str | None = None) -> HostLaunch:
    manager.preflight()  # Before creating a carrier directory or accepting Action.
    locations = kernel.run_paths()
    directory = _runtime_directory(action_id, environment, run_partition=locations.host_partition)
    carrier = directory / "environment.json"
    return kernel.admit_host(action_id, kind=kind, command=command, environment_carrier=carrier,
                                preflight=manager.preflight, subject=subject, world=world,
                                message=message, new_thread=new_thread, policy_overrides=policy_overrides,
                                replacement=replacement,
                                resource_inspections=resource_inspections, discard_worktree=discard_worktree,
                                continuation_of=continuation_of,
                                backend="foreground" if isinstance(manager, ForegroundHost) else "systemd")


def launch_admitted(kernel: Kernel, request: HostLaunch, *, manager: SystemdHost | ForegroundHost,
            environment: Mapping[str, str], runner_lock: Path,
            terminal: RunnerLease, action_id: str, kind: str) -> tuple[dict[str, Any], HostObservation | None]:
    from agent_run.runtime.handoff import handoff_scope, launch_handoff
    observed: HostObservation | None
    if isinstance(manager, ForegroundHost):
        observed = manager.launch(kernel, request, lock=runner_lock, lease=terminal)
    else:
        from agent_run.runtime.host_exit import exit_properties
        properties = exit_properties(kernel.storage.path, action_id, request, runner_lock, environment,
            **({"storage": kernel.storage}))
        with handoff_scope(request, environment, runner_lock=runner_lock, terminal_lease=terminal) as handoff:
            observed = launch_handoff(kernel, action_id, manager, handoff, exit_properties=properties)
    with kernel.storage.transaction(write=False) as tx:
        action_status = tx.one(*run_statement(tx, 'SELECT status FROM actions WHERE run_id=? AND id=?', (action_id,)))[0]
    return {"result": "accepted", "status": "action_" + action_status, "run_id": request.run_id,
            "action_id": action_id, "action": kind,
            "host_action_id": action_id,
            "host": "unknown" if observed is None else observed.disposition,
            "host_unit": request.unit_name, "host_backend": request.backend}, observed

def attach(kernel: Kernel, *, requested_action: str, manager: SystemdHost | ForegroundHost,
           environment: Mapping[str, str], runner_lock: Path) -> dict[str, Any] | None:
    """Observe the original Host without applying the requested control Action."""
    from agent_run.runtime.package_lease import runner_usage_lease
    observed: HostObservation | None = None
    empty_foreground = False
    try:
        with runner_usage_lease(runner_lock) as terminal:
            selected = select_existing(kernel, manager, runner_lock)
            empty_foreground = selected is None and isinstance(manager, ForegroundHost)
            result, observed = attach_selected(kernel, selected, requested_action=requested_action,
                manager=manager, environment=environment, runner_lock=runner_lock, terminal=terminal)
            return result
    finally:
        if empty_foreground:
            notify_committed(kernel)  # A prior retirement may precede its first outbox write.
        else:
            notify_after_usage(kernel, observed)


def select_existing(kernel: Kernel, manager: SystemdHost | ForegroundHost,
                    runner_lock: Path) -> tuple[str, HostLaunch] | None:
    if isinstance(manager, ForegroundHost):
        manager.reconcile_closed(kernel, runner_lock)
    action_id = kernel.active_host_action()
    if action_id is None:
        return None
    manager.preflight()
    request = kernel.host_request(action_id)
    if (request.backend == "foreground") != isinstance(manager, ForegroundHost):
        raise KernelError("host_backend_changed")
    return action_id, request


def attach_selected(kernel: Kernel, selected: tuple[str, HostLaunch] | None, *, requested_action: str,
            manager: SystemdHost | ForegroundHost, environment: Mapping[str, str],
            runner_lock: Path, terminal: RunnerLease) -> tuple[dict[str, Any] | None, HostObservation | None]:
    from agent_run.runtime.handoff import handoff_scope, launch_handoff, reconcile_handoff
    if selected is None:
        return None, None
    action_id, request = selected
    observed: HostObservation | None
    if isinstance(manager, ForegroundHost):
        observed = (manager.observe(kernel, request, runner_lock) if kernel.host_dispatched(action_id)
                    else manager.launch(kernel, request, lock=runner_lock, lease=terminal))
    elif kernel.host_dispatched(action_id):
        observed = reconcile_handoff(kernel, action_id, manager)
    else:
        from agent_run.runtime.host_exit import exit_properties
        properties = exit_properties(kernel.storage.path, action_id, request, runner_lock, environment,
            **({"storage": kernel.storage}))
        with handoff_scope(request, environment, runner_lock=runner_lock, terminal_lease=terminal) as handoff:
            observed = launch_handoff(kernel, action_id, manager, handoff, exit_properties=properties)
    with kernel.storage.transaction(write=False) as tx:
        action = tx.one(*run_statement(tx, 'SELECT kind,status FROM actions WHERE run_id=? AND id=?', (action_id,)))
    return {"result": "attached", "status": "action_" + action["status"], "run_id": request.run_id,
            "action_id": action_id, "action": action["kind"],
            "requested_action": requested_action, "requested_action_applied": False,
            "host": "unknown" if observed is None else observed.disposition,
            "host_unit": request.unit_name, "host_backend": request.backend}, observed


def manager_for(kernel: Kernel | None, parsed: argparse.Namespace,
                 environment: Mapping[str, str]) -> SystemdHost | ForegroundHost:
    import shutil
    from agent_run.adapters.systemd import SystemdHost
    requested = getattr(parsed, "foreground", None)
    backend = "foreground" if requested else "systemd"
    if kernel is not None:
        action = kernel.active_host_action()
        if action is not None:
            original = kernel.host_request(action).backend
            if requested and original != "foreground":
                raise KernelError("host_backend_changed")
            backend = original
    if backend == "foreground":
        return ForegroundHost(environment=environment)
    return SystemdHost(systemctl=shutil.which("systemctl") or "systemctl",
                       systemd_run=shutil.which("systemd-run") or "systemd-run", environment=environment)



def has_unknown_host(tx: Any) -> bool:
    """Read only the selected Run before any Host manager is constructed."""
    return bool(tx.rows(*run_statement(tx,
        "SELECT owner FROM hosts WHERE run_id=? AND (status!='exited' OR usage_released=0) AND status!='running' LIMIT 1", ())))
