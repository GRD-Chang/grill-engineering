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

from agent_run.bounded_command import CommandCapture, capture_command

from .execution import _start_token
from .model import HostLaunch, HostObservation


_OUTPUT_LIMIT = 16 * 1024
_PROPERTIES = (
    "Id", "Description", "LoadState", "ActiveState", "SubState", "InvocationID",
    "ControlGroup", "MainPID", "ControlPID", "ExecMainCode", "ExecMainStatus",
    "ExecMainExitTimestamp", "ExecMainExitTimestampMonotonic",
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

    def launch(self, request: HostLaunch, *, dispatch: Callable[[], bool]) -> HostObservation | None:
        """Consume exactly one Kernel launch grant; never retry an unknown start."""
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
                "--property=RemainAfterExit=yes", "--property=TimeoutStopSec=5s",
                f"--property=LoadCredential=agent-run-environment:{request.environment_carrier}",
                "--", *request.command,
            ])
            if result.returncode != 0:
                return HostObservation("unknown", request.unit_name, evidence="launch_response_unknown")
        except (OSError, ValueError, HostUnavailable):
            return HostObservation("unknown", request.unit_name, evidence="launch_not_confirmed")
        return self.observe(request)

    def _fields(self, unit: str) -> dict[str, str]:
        result = self._run([self.systemctl, "--user", "show", unit, "--all", "--timestamp=unix",
                            "--property=" + ",".join(_PROPERTIES)])
        if result.returncode != 0:
            raise ValueError("Unit observation unavailable")
        fields: dict[str, str] = {}
        for line in result.stdout.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in fields:
                raise ValueError("Ambiguous unit observation")
            fields[key] = value
        if not set(_PROPERTIES) <= fields.keys():
            raise ValueError("Incomplete unit observation")
        return fields

    def observe(self, request: HostLaunch) -> HostObservation:
        """Read twice around physical evidence; this never mutates the Kernel."""
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
