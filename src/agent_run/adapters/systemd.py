"""Concrete user-systemd facts; Kernel owns admission and every launch decision.

An absent unit is unknown, never permission to launch again. Retained service
facts and an empty cgroup are required for exit; a vanished PID is insufficient.
No service is restarted here, and there is no detached-process fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess

from agent_run.runtime.command import CommandCapture, capture_command
from agent_run.runtime.process_identity import _process_start_token as _start_token

from agent_run.model import HostLaunch, HostObservation


_OUTPUT_LIMIT = 16 * 1024
_PROPERTIES = (
    "Id", "Description", "LoadState", "ActiveState", "SubState", "InvocationID",
    "ControlGroup", "MainPID", "ControlPID", "ExecMainCode", "ExecMainStatus",
    "ExecMainExitTimestamp", "ExecMainExitTimestampMonotonic",
    "NFileDescriptorStore",
)


class HostUnavailable(RuntimeError):
    """Preflight failed; the caller must not accept a new lifecycle Action."""


class SystemdHost:
    def __init__(
        self, *, systemctl: str, systemd_run: str, environment: Mapping[str, str],
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        self.systemctl, self.systemd_run = systemctl, systemd_run
        self.environment = {**environment, "LC_ALL": "C", "SYSTEMD_PAGER": "cat"}
        self.cgroup_root = cgroup_root

    def _run(self, arguments: list[str]) -> CommandCapture:
        process = subprocess.Popen(arguments, env=self.environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        result = capture_command(process, timeout=5, max_output=_OUTPUT_LIMIT)
        if result.timed_out or len(result.stdout) >= _OUTPUT_LIMIT or len(result.stderr) >= _OUTPUT_LIMIT:
            raise HostUnavailable("Bounded user systemd observation was unavailable")
        return result

    def preflight(self) -> None:
        """Readiness is checked before Kernel admission; no environment is read."""
        try:
            result = self._run([self.systemctl, "--user", "show", "--property=Version", "--value"])
            version = re.match(rb"([0-9]+)", result.stdout.strip())
            if (result.returncode != 0 or version is None or int(version[1]) < 254
                    or shutil.which(self.systemd_run, path=self.environment.get("PATH")) is None):
                raise HostUnavailable("A compatible user systemd manager is unavailable")
        except OSError as error:
            raise HostUnavailable("The user systemd manager cannot be contacted") from error

    def launch(self, request: HostLaunch, *, dispatch: Callable[[], bool],
               exit_properties: tuple[str, ...] = ()) -> HostObservation | None:
        """Consume exactly one Kernel launch grant; never retry an unknown start."""
        if request.backend != "systemd" or request.unit_name is None:
            raise HostUnavailable("A systemd Host request is required")
        if not dispatch():
            return None
        try:
            self.preflight()
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.service", request.unit_name):
                raise ValueError("Invalid allocated service name")
            carrier = request.environment_carrier.lstat()
            if (not stat.S_ISREG(carrier.st_mode) or carrier.st_uid != os.geteuid()
                    or carrier.st_nlink != 1 or carrier.st_mode & 0o077 or carrier.st_size > 1024 * 1024):
                raise ValueError("Environment carrier is not a private owned file")
            if not request.command or not Path(request.command[0]).is_absolute():
                raise ValueError("An absolute Executor command is required")
            result = self._run([
                self.systemd_run, "--user", f"--unit={request.unit_name}",
                f"--description={request.description}", "--expand-environment=no",
                "--property=Type=exec", "--property=Restart=no", "--property=KillMode=control-group",
                "--property=RemainAfterExit=no",
                *(("--property=TimeoutStopSec=5s",) if not exit_properties else ()),
                "--property=FileDescriptorStoreMax=1", "--property=FileDescriptorStorePreserve=restart",
                "--property=NotifyAccess=exec",
                f"--property=LoadCredential=agent-run-environment:{request.environment_carrier}",
                *("--property=" + value for value in exit_properties),
                "--", *request.command,
            ])
            if result.returncode != 0:
                return HostObservation("unknown", request.unit_name, evidence="launch_response_unknown")
        except (OSError, ValueError, HostUnavailable):
            return HostObservation("unknown", request.unit_name, evidence="launch_not_confirmed")
        return self.observe(request)

    def _fields(self, unit: str, *, allow_missing: bool = False) -> dict[str, str]:
        result = self._run([self.systemctl, "--user", "show", unit, "--all", "--timestamp=unix",
                            "--property=" + ",".join(_PROPERTIES)])
        fields: dict[str, str] = {}
        for line in result.stdout.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in fields:
                raise ValueError("Ambiguous unit observation")
            fields[key] = value
        if not set(_PROPERTIES) <= fields.keys():
            raise ValueError("Incomplete unit observation")
        if result.returncode != 0 and not (allow_missing and fields["LoadState"] == "not-found"):
            raise ValueError("Unit observation unavailable")
        return fields

    def exit_identity(self, request: HostLaunch, invocation_id: str) -> bool:
        """Read the exact unit identity while its post-exit hook is still alive.

        ControlPID/cgroup settlement is deliberately irrelevant here: this
        proves neither whole-Host exit nor release of its retained lease.
        """
        if request.backend != "systemd" or request.unit_name is None:
            return False
        if not re.fullmatch(r"[0-9a-f]{32}", invocation_id) or int(invocation_id, 16) == 0:
            return False
        try:
            keys = ("Id", "Description", "InvocationID", "LoadState")
            expected = (request.unit_name, request.description, invocation_id, "loaded")
            first = self._fields(request.unit_name)
            if (first["LoadState"] != "loaded" or not re.fullmatch(r"[0-9a-f]{32}", first["InvocationID"])
                    or int(first["InvocationID"], 16) == 0):
                raise HostUnavailable("Host exit identity is unavailable")
            if tuple(first[key] for key in keys) != expected:
                return False
            second = self._fields(request.unit_name)
            if (second["LoadState"] != "loaded" or not re.fullmatch(r"[0-9a-f]{32}", second["InvocationID"])
                    or int(second["InvocationID"], 16) == 0):
                raise HostUnavailable("Host exit identity is unavailable")
            return tuple(second[key] for key in keys) == expected
        except (OSError, ValueError) as error:
            raise HostUnavailable("Host exit identity is unavailable") from error

    def observe(self, request: HostLaunch) -> HostObservation:
        """Read twice around physical evidence; this never mutates the Kernel."""
        if request.backend != "systemd" or request.unit_name is None:
            raise HostUnavailable("A systemd Host request is required")
        try:
            fields = self._fields(request.unit_name)
            if (fields["Id"] != request.unit_name or fields["Description"] != request.description
                    or fields["LoadState"] != "loaded"
                    or not re.fullmatch(r"[0-9a-f]{32}", fields["InvocationID"])
                    or int(fields["InvocationID"], 16) == 0):
                raise ValueError("Unit launch identity is not established")
            group = PurePosixPath(fields["ControlGroup"])
            if not group.is_absolute() or ".." in group.parts:
                raise ValueError("Unit cgroup identity is unavailable")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            pid, control_pid = int(fields["MainPID"]), int(fields["ControlPID"])
            active, substate = fields["ActiveState"], fields["SubState"]
            if active == "active" and substate == "running" and pid > 1:
                token = _start_token(pid)
                memberships = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                if token is None or f"0::{group}" not in memberships or _start_token(pid) != token:
                    raise ValueError("Main process identity is unavailable")
                observation = HostObservation("running", request.unit_name, fields["InvocationID"],
                                              str(group), boot, pid, token)
            elif ((active, substate) in {("active", "exited"), ("inactive", "dead"), ("failed", "failed")}
                  and pid == 0 and control_pid == 0):
                events = (self.cgroup_root / str(group).lstrip("/") / "cgroup.events").read_text().splitlines()
                ended = fields["ExecMainExitTimestamp"]
                if ("populated 0" not in events or "populated 1" in events or not ended.startswith("@")
                        or int(fields["ExecMainExitTimestampMonotonic"]) <= 0):
                    raise ValueError("Executor and descendants exit is unproven")
                ended_at = float(ended[1:])
                code, status = int(fields["ExecMainCode"]), int(fields["ExecMainStatus"])
                if not math.isfinite(ended_at) or ended_at <= 0 or code not in {1, 2, 3} or status < 0:
                    raise ValueError("Exit status is unavailable")
                observation = HostObservation("exited", request.unit_name, fields["InvocationID"],
                                              str(group), boot, exit_status=status if code == 1 else -status,
                                              ended_at=ended_at)
            else:
                raise ValueError("Unit state has not settled")
            if self._fields(request.unit_name) != fields:
                raise ValueError("Unit changed during observation")
            return observation
        except (OSError, ValueError, HostUnavailable):
            return HostObservation("unknown", request.unit_name, evidence="host_identity_or_exit_unproven")

    def stored_descriptors(self, request: HostLaunch, observation: HostObservation) -> int | None:
        """Read the store count only while the exact observed unit stays fixed."""
        if request.backend != "systemd" or request.unit_name is None or observation.disposition not in {"running", "exited"}:
            return None
        try:
            fields = self._fields(request.unit_name)
            count = int(fields["NFileDescriptorStore"])
            if count < 0 or self.observe(request) != observation or self._fields(request.unit_name) != fields:
                return None
            return count
        except (OSError, ValueError, HostUnavailable):
            return None

    def stop_post_observation(self, request: HostLaunch, original: HostObservation,
                              invocation_id: str, *, lease_released: bool = False) -> HostObservation:
        """Bind the manager's main exit to this exact, still-running stop hook.

        This does not claim whole-Host exit: the hook itself still owns a lease.
        Its final cgroup census and subsequent process exit complete the proof.
        """
        from dataclasses import replace
        fields = self._fields(request.unit_name or "")
        if (request.backend != "systemd" or not original.usage_ready or original.usage_released
                or not original.main_pid or not original.start_token
                or (fields["Id"], fields["Description"], fields["LoadState"], fields["InvocationID"],
                    fields["ControlGroup"], fields["MainPID"], fields["ControlPID"],
                    fields["ActiveState"], fields["SubState"])
                != (request.unit_name, request.description, "loaded", invocation_id, original.cgroup,
                    "0", str(os.getpid()), "deactivating", "stop-post")
                or original.invocation_id != invocation_id
                or original.boot_id != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                or lease_released and fields["NFileDescriptorStore"] != "0"):
            raise HostUnavailable("The original Host stop hook is not established")
        ended, code, status = fields["ExecMainExitTimestamp"], int(fields["ExecMainCode"]), int(fields["ExecMainStatus"])
        if (not ended.startswith("@") or int(fields["ExecMainExitTimestampMonotonic"]) <= 0
                or code not in {1, 2, 3} or status < 0):
            raise HostUnavailable("The main process exit is unavailable")
        ended_at = float(ended[1:])
        if not math.isfinite(ended_at) or ended_at <= 0 or self._fields(request.unit_name or "") != fields:
            raise HostUnavailable("The main process exit changed")
        return replace(original, disposition="exited", exit_status=status if code == 1 else -status,
                       ended_at=ended_at)

    def retained_exit_matches(self, request: HostLaunch, original: HostObservation) -> bool:
        """Fence a retained proof against a presently loaded foreign/restarted unit.

        A missing unit is acceptable only to a caller that already holds the
        independent stop-hook/process/lease evidence. It never proves exit here.
        """
        try:
            fields = self._fields(request.unit_name or "", allow_missing=True)
            if fields["LoadState"] == "not-found":
                return (fields["Id"] == request.unit_name
                        and self._fields(request.unit_name or "", allow_missing=True) == fields)
            if ((fields["Id"], fields["Description"], fields["InvocationID"],
                 fields["LoadState"], fields["MainPID"], fields["ControlPID"], fields["NFileDescriptorStore"])
                    != (request.unit_name, request.description, original.invocation_id,
                        "loaded", "0", "0", "0")
                    or fields["ControlGroup"] not in {"", original.cgroup}
                    or (fields["ActiveState"], fields["SubState"]) not in {
                        ("inactive", "dead"), ("failed", "failed"), ("active", "exited")}):
                return False
            code, status = int(fields["ExecMainCode"]), int(fields["ExecMainStatus"])
            return (code in {1, 2, 3} and (status if code == 1 else -status) == original.exit_status
                    and fields["ExecMainExitTimestamp"].startswith("@")
                    and float(fields["ExecMainExitTimestamp"][1:]) == original.ended_at
                    and self._fields(request.unit_name or "") == fields)
        except (OSError, ValueError, HostUnavailable):
            return False
