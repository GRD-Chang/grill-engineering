"""Bounded stop-hook evidence that survives unit and cgroup collection.

The hook records a final census while it is the sole remaining cgroup process.
It releases the manager's named lease first, and keeps its own usage lease until
exit. A later observer must prove that exact process exited before consuming the
witness; a missing unit or PID is never the entire proof.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any
import uuid

from agent_run.adapters.systemd import HostUnavailable, SystemdHost
from agent_run.core.kernel import Kernel
from agent_run.model import HostLaunch, HostObservation
from agent_run.runtime.handoff import (
    HandoffError, _BOOT, _digest, _directory, _encode, _guardian_exited, _runner_witness, _unlink_owned,
)
from agent_run.runtime.package_lease import release_systemd_exit_lease
from agent_run.runtime.process_identity import _process_start_token


_LIMIT = 16 * 1024


def witness_path(request: HostLaunch) -> Path:
    """One private, bounded witness per original Host allocation."""
    if not request.environment_carrier.is_absolute() or not request.nonce.isalnum():
        raise ValueError("Invalid stop-hook witness location")
    return request.environment_carrier.with_name("exit-" + request.nonce + ".json")


def _only_hook(manager: SystemdHost, group: str | None) -> None:
    """Census every child cgroup after all external observation commands return."""
    # A notification thread with unproven cleanup could fork after the census.
    # cgroup.procs lists processes, so also require the hook's single OS thread.
    with os.scandir("/proc/self/task") as tasks:
        if {entry.name for entry in tasks} != {str(os.getpid())}:
            raise HostUnavailable("The stop hook still has background execution")
    selected = PurePosixPath(group or "")
    if not selected.is_absolute() or str(selected) == "/" or ".." in selected.parts:
        raise HostUnavailable("The stop-hook cgroup is unavailable")
    membership = Path("/proc/self/cgroup").read_text().splitlines()
    if "0::" + str(selected) not in membership:
        raise HostUnavailable("The stop hook moved to another cgroup")
    pending = [manager.cgroup_root / str(selected).lstrip("/")]
    pids: set[int] = set()
    count = 0
    while pending:
        current = pending.pop()
        count += 1
        if count > 128 or current.is_symlink():
            raise HostUnavailable("The stop-hook cgroup census exceeds its bound")
        with (current / "cgroup.procs").open("rb") as source:
            raw = source.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise HostUnavailable("The stop-hook process census exceeds its bound")
        pids.update(int(value) for value in raw.splitlines())
        if pids - {os.getpid()}:
            raise HostUnavailable("Other Host execution processes remain")
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    if len(pending) > 128:
                        raise HostUnavailable("The stop-hook cgroup census exceeds its bound")
    if pids != {os.getpid()}:
        raise HostUnavailable("The exact stop-hook process is unavailable")


def _process_exited(receipt: dict[str, Any], pid: int | None, token: str | None) -> bool:
    return _guardian_exited({**receipt, "guardian": {"pid": pid, "start_token": token}})


def _persist(request: HostLaunch, value: dict[str, Any]) -> None:
    target = witness_path(request)
    raw = _encode(value)
    if len(raw) > _LIMIT:
        raise ValueError("The stop-hook witness exceeds its bound")
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex)
    identity = None
    with _directory(target.parent) as directory:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            # Never replace another hook's evidence, even for the same pathname.
            os.link(temporary, target, follow_symlinks=False)
            _unlink_owned(temporary, identity)
            os.fsync(directory)
        finally:
            _unlink_owned(temporary, identity)


def record_exit_witness(kernel: Kernel, action_id: str, manager: SystemdHost,
                        receipt: dict[str, Any], *, invocation_id: str, notify_socket: str) -> None:
    """Run last in ExecStopPost: no child processes may be spawned afterwards."""
    request, original = kernel.host_request(action_id), kernel.host_usage(action_id)
    if (original is None or not original.usage_ready or original.usage_released
            or original.backend != "systemd" or json.loads(original.evidence) != receipt
            or receipt.get("request_digest") != _digest(request)
            or receipt.get("nonce") != request.nonce
            or receipt.get("runner") != _runner_witness(Path(receipt["runner"]["lock"]))
            or not _process_exited(receipt, original.main_pid, original.start_token)):
        raise HostUnavailable("The original ready Host and guardian exit are unproven")
    observation = manager.stop_post_observation(request, original, invocation_id)
    release_systemd_exit_lease(notify_socket)
    settled = manager.stop_post_observation(request, original, invocation_id, lease_released=True)
    if observation != settled:
        raise HostUnavailable("The original manager lease release is unproven")
    token = _process_start_token(os.getpid())
    if token is None:
        raise HostUnavailable("The exact stop-hook process is unavailable")
    # This is the final external-resource census. Only bounded file/JSON I/O
    # follows it; no sender, systemctl subprocess or new execution is allowed.
    _only_hook(manager, original.cgroup)
    _persist(request, {"version": 1, "request_digest": _digest(request),
        "binding": [request.run_id, action_id, request.epoch, request.owner, request.nonce],
        "receipt_digest": hashlib.sha256(_encode(receipt)).hexdigest(),
        "observation": asdict(observation), "hook": {"pid": os.getpid(), "start_token": token},
        "manager_lease_released": True, "cgroup_only_hook": True})


def _read(request: HostLaunch) -> tuple[dict[str, Any], tuple[int, int]]:
    path = witness_path(request)
    with _directory(path.parent):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_mode & 0o077):
                raise ValueError("The stop-hook witness is not private and owned")
            raw = source.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError("The stop-hook witness exceeds its bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("The stop-hook witness is invalid")
    return value, (info.st_dev, info.st_ino)


def _runner_lease_matches(runner: object) -> bool:
    """Match the retained lease without pinning recovery to an old Snapshot.

    The original package/interpreter remain bound by the persisted receipt
    digest. Only the management lock path and filesystem identity must still
    match when another same-protocol Active Runner consumes that proof.
    """
    if (not isinstance(runner, dict)
            or set(runner) != {"package_root", "python", "lock", "device", "inode"}
            or any(not isinstance(runner[key], str) or not Path(runner[key]).is_absolute()
                   for key in ("package_root", "python", "lock"))
            or any(type(runner[key]) is not int for key in ("device", "inode"))):
        return False
    current = _runner_witness(Path(runner["lock"]))
    return all(runner[key] == current[key] for key in ("lock", "device", "inode"))


def reconcile_exit_witness(kernel: Kernel, action_id: str, manager: SystemdHost) -> HostObservation | None:
    """Consume only a complete proof; failed checks leave all original state."""
    request, original = kernel.host_request(action_id), kernel.host_usage(action_id)
    if original is None or not original.usage_ready or original.usage_released:
        return None
    try:
        value, identity = _read(request)
        receipt = json.loads(original.evidence)
        if (value.get("version") != 1 or value.get("request_digest") != _digest(request)
                or value.get("binding") != [request.run_id, action_id, request.epoch, request.owner, request.nonce]
                or value.get("receipt_digest") != hashlib.sha256(_encode(receipt)).hexdigest()
                or value.get("manager_lease_released") is not True or value.get("cgroup_only_hook") is not True
                or receipt.get("request_digest") != _digest(request) or receipt.get("nonce") != request.nonce
                or not _runner_lease_matches(receipt.get("runner"))
                or receipt.get("boot_id") != _BOOT.read_text().strip()
                or receipt.get("pid_namespace") != os.readlink("/proc/self/ns/pid")):
            return None
        observation = HostObservation(**value["observation"])
        expected = replace(original, disposition="exited", exit_status=observation.exit_status,
                           ended_at=observation.ended_at)
        ended = observation.ended_at
        if (observation != expected or type(observation.exit_status) is not int
                or not isinstance(ended, (int, float)) or isinstance(ended, bool)
                or not math.isfinite(ended) or ended <= 0
                or not _guardian_exited(receipt)
                or not _process_exited(receipt, original.main_pid, original.start_token)
                or not _process_exited(receipt, value["hook"]["pid"], value["hook"]["start_token"])
                or not manager.retained_exit_matches(request, observation)):
            return None
        released = replace(observation, usage_released=True, exit_evidence=_encode(value).decode())
        if kernel.observe_host(request, released) != "exited":
            return None
        # SQLite now retains the complete bounded proof. Only this exact file
        # is removed; no historical carrier directory or unrelated state is pruned.
        with _directory(witness_path(request).parent) as directory:
            _unlink_owned(witness_path(request), identity)
            os.fsync(directory)
        return released
    except (OSError, ValueError, KeyError, TypeError, HostUnavailable, HandoffError):
        return None
