"""One-shot local lease transfer; Kernel alone authorizes Host and Agent work."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
import time
from typing import Any, TYPE_CHECKING

from agent_run.process_binding import _process_start_token as _start_token
from agent_run.runner_lease import RunnerLease, retain_lease_for_systemd_exit, runner_usage_lease

from .execution import _cgroup
from .model import HostLaunch, HostObservation

if TYPE_CHECKING:
    from .host_adapter import SystemdHost
    from .kernel import Kernel


_LIMIT = 1024 * 1024
_BOOT = Path("/proc/sys/kernel/random/boot_id")


class HandoffError(RuntimeError):
    """No execution permission follows an unproven local lease transfer."""


@dataclass(frozen=True)
class Handoff:
    process: subprocess.Popen[bytes]
    receipt: dict[str, Any]


@dataclass(frozen=True)
class HostEnvironment:
    environment: dict[str, str]
    receipt: dict[str, Any]


def launch_handoff(kernel: Kernel, action_id: str, manager: SystemdHost, handoff: Handoff, *,
                   exit_properties: tuple[str, ...] = ()) -> HostObservation | None:
    """Persist the original guardian receipt before the sole authorized launch."""
    request = kernel.host_request(action_id)
    if handoff.receipt.get("request_digest") != _digest(request):
        raise HandoffError("Guardian belongs to another Host request")

    def dispatch() -> bool:
        if not kernel.dispatch_host(request):
            return False
        kernel.observe_host(request, HostObservation(
            "unknown", request.unit_name, evidence=_encode(handoff.receipt).decode(), usage_pending=True,
        ))
        return True

    observed = (manager.launch(request, dispatch=dispatch, exit_properties=exit_properties) if exit_properties
                else manager.launch(request, dispatch=dispatch))
    # Pending already records the uncertain launch. A late CLI reply cannot
    # downgrade a newer readiness fact reported by the actual Host process.
    if observed is not None and observed.disposition != "unknown":
        kernel.observe_host(request, observed)
    return observed


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(request: HostLaunch) -> str:
    return hashlib.sha256(_encode({**asdict(request), "environment_carrier": str(request.environment_carrier)})).hexdigest()


def _environment(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not key or "=" in key or "\0" in key
        or not isinstance(item, str) or "\0" in item for key, item in value.items()
    ):
        raise HandoffError("Invalid captured environment")
    return dict(value)


def _runner_witness(runner_lock: Path) -> dict[str, Any]:
    held = runner_lock.stat()
    return {"package_root": str(Path(__file__).resolve().parents[1]),
            "python": sys.executable, "lock": str(runner_lock.resolve()),
            "device": held.st_dev, "inode": held.st_ino}


def _unlink_owned(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity:
        path.unlink()


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise HandoffError("Handoff directory is not private")
        yield descriptor
    finally:
        os.close(descriptor)


def prepare_handoff(
    request: HostLaunch, environment: Mapping[str, str], *, runner_lock: Path,
    terminal_lease: RunnerLease, ttl_seconds: float = 30,
) -> Handoff:
    """Inherit the caller's existing shared lease before any systemd dispatch."""
    if not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 30:
        raise HandoffError("Invalid handoff deadline")
    captured = _environment(dict(environment))
    carrier = request.environment_carrier
    if not carrier.is_absolute() or carrier.exists():
        raise HandoffError("Handoff carrier must be a new absolute path")
    held, selected = os.fstat(terminal_lease.fileno()), runner_lock.stat()
    if (held.st_dev, held.st_ino) != (selected.st_dev, selected.st_ino):
        raise HandoffError("Inherited lease does not protect the selected Runner")
    with _directory(carrier.parent):
        pass
    deadline = time.monotonic() + ttl_seconds
    endpoint = carrier.with_name("guardian-" + request.nonce + ".sock")
    receipt: dict[str, Any] = {
        "nonce": request.nonce, "request_digest": _digest(request), "deadline": deadline,
        "socket": str(endpoint), "boot_id": _BOOT.read_text().strip(),
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
        "runner": _runner_witness(runner_lock),
    }
    body = {"version": 1, "receipt": receipt, "runner_lock": str(runner_lock.resolve()), "environment": captured}
    if len(_encode(body)) > _LIMIT - 4096:
        raise HandoffError("Captured environment exceeds the handoff limit")
    # Reserve ownership before starting another process; the environment is
    # written only after the guardian acknowledges its own bound socket.
    carrier_fd = os.open(carrier, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    info = os.fstat(carrier_fd)
    carrier_identity = (info.st_dev, info.st_ino)
    receipt["carrier_identity"] = [info.st_dev, info.st_ino]
    endpoint_identity: tuple[int, int] | None = None
    read_fd = write_fd = -1
    process: subprocess.Popen[bytes] | None = None
    try:
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        # The already loaded package location is the trusted Runner source;
        # isolated Python ignores terminal PYTHONPATH and user site hooks.
        bootstrap = "import sys;sys.path.insert(0,sys.argv[1]);from agent_run_kernel.native_host import _guardian;raise SystemExit(_guardian(sys.argv[2:]))"
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parents[1]),
             str(carrier), json.dumps(receipt), str(terminal_lease.fileno()), str(write_fd)],
            pass_fds=(terminal_lease.fileno(), write_fd), start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath},
        )
        os.close(write_fd)
        write_fd = -1
        if not select.select([read_fd], [], [], min(5, ttl_seconds))[0]:
            raise HandoffError("Guardian startup acknowledgement unavailable")
        observed = json.loads(os.read(read_fd, 4096))
        if (not isinstance(observed, dict) or observed.get("pid") != process.pid
                or not observed.get("start_token") or observed["start_token"] != _start_token(process.pid)):
            raise HandoffError("Guardian identity was not established")
        socket_identity = observed.get("socket_identity")
        if (not isinstance(socket_identity, list) or len(socket_identity) != 2
                or any(type(value) is not int for value in socket_identity)):
            raise HandoffError("Guardian socket ownership was not established")
        endpoint_identity = (socket_identity[0], socket_identity[1])
        receipt.update(guardian=observed)
        with os.fdopen(carrier_fd, "wb") as target:
            carrier_fd = -1
            target.write(_encode(body))
            target.flush()
            os.fsync(target.fileno())
        if time.monotonic() >= deadline or process.poll() is not None:
            raise HandoffError("Guardian expired before dispatch")
        return Handoff(process, receipt)
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        # Reap the child before removing its acknowledged instance: its own
        # cleanup cannot then race this parent or a subsequent preparer.
        _unlink_owned(carrier, carrier_identity)
        _unlink_owned(endpoint, endpoint_identity)
        raise
    finally:
        if carrier_fd >= 0:
            os.close(carrier_fd)
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def _guardian(arguments: list[str]) -> int:
    carrier, receipt = Path(arguments[0]), json.loads(arguments[1])
    lease_fd, ack_fd = int(arguments[2]), int(arguments[3])
    endpoint = Path(receipt["socket"])
    deadline = float(receipt["deadline"])
    committed = False
    endpoint_identity: tuple[int, int] | None = None
    carrier_identity = (int(receipt["carrier_identity"][0]), int(receipt["carrier_identity"][1]))
    try:
        with _directory(endpoint.parent) as directory, socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            listener.bind(f"/proc/self/fd/{directory}/{endpoint.name}")
            info = endpoint.lstat()
            endpoint_identity = (info.st_dev, info.st_ino)
            listener.listen(1)
            os.write(ack_fd, _encode({"pid": os.getpid(), "start_token": _start_token(os.getpid()),
                                     "socket_identity": endpoint_identity}))
            os.close(ack_fd)
            ack_fd = -1
            while time.monotonic() < deadline:
                listener.settimeout(max(0.001, deadline - time.monotonic()))
                with listener.accept()[0] as channel:
                    channel.settimeout(max(0.001, deadline - time.monotonic()))
                    pid, uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    message = json.loads(channel.recv(1024))
                    if (uid != os.geteuid() or pid <= 1 or message != {
                        "nonce": receipt["nonce"], "request_digest": receipt["request_digest"],
                    }):
                        continue
                    if time.monotonic() >= deadline:
                        break
                    _unlink_owned(carrier, carrier_identity)
                    listener.close()  # Exactly one claimant; credential copies cannot replay.
                    channel.sendall(b"claimed")
                    if channel.recv(16) != b"ready" or time.monotonic() >= deadline:
                        return 1
                    channel.sendall(b"committed")
                    committed = True
                    return 0
        return 1
    except (OSError, ValueError):
        return 1
    finally:
        _unlink_owned(carrier, carrier_identity)
        _unlink_owned(endpoint, endpoint_identity)
        if not committed:
            # A missing acknowledgement cannot shorten the originally sealed
            # handoff interval, even when the claimant has already disappeared.
            time.sleep(max(0, deadline - time.monotonic()))
        os.close(lease_fd)
        if ack_fd >= 0:
            os.close(ack_fd)


def _read_carrier(request: HostLaunch, credential: Path, runner_lock: Path, *,
                  check_expiry: bool = True) -> tuple[dict[str, str], dict[str, Any]]:
    descriptor = os.open(credential, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise HandoffError("Environment credential is not private and owned")
        raw = source.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise HandoffError("Environment credential exceeds the limit")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("version") != 1
            or value.get("runner_lock") != str(runner_lock.resolve()) or not isinstance(value.get("receipt"), dict)):
        raise HandoffError("Environment credential binding is invalid")
    receipt = value["receipt"]
    deadline = receipt.get("deadline")
    if (receipt.get("nonce") != request.nonce or receipt.get("request_digest") != _digest(request)
            or receipt.get("boot_id") != _BOOT.read_text().strip()
            or receipt.get("pid_namespace") != os.readlink("/proc/self/ns/pid")
            or receipt.get("runner") != _runner_witness(runner_lock)
            or type(deadline) not in {int, float} or not math.isfinite(deadline)
            or (check_expiry and time.monotonic() >= deadline)):
        raise HandoffError("Environment credential is expired or bound elsewhere")
    return _environment(value.get("environment")), receipt


def read_exit_environment(request: HostLaunch, *, runner_lock: Path, credential: Path) -> HostEnvironment:
    """Read the original manager credential after expiry, without claiming startup.

    The post-exit caller already owns the original Runner's shared lease. Its
    manager-owned credential survives the one-shot source carrier and guardian.
    All identity and private-file checks remain the startup reader's checks.
    """
    environment, receipt = _read_carrier(request, credential, runner_lock, check_expiry=False)
    return HostEnvironment(environment, receipt)


@contextmanager
def host_environment(
    request: HostLaunch, *, runner_lock: Path, credential: Path, notify_socket: str,
) -> Iterator[HostEnvironment]:
    """Own SH before consuming; yield only after the real FDSTORE barrier ACK."""
    with runner_usage_lease(runner_lock) as lease:
        environment, receipt = _read_carrier(request, credential, runner_lock)
        endpoint = Path(receipt["socket"])
        with _directory(endpoint.parent) as directory, socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
            channel.settimeout(max(0.001, receipt["deadline"] - time.monotonic()))
            channel.connect(f"/proc/self/fd/{directory}/{endpoint.name}")
            pid, uid, _ = struct.unpack("3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            guardian = receipt.get("guardian", {})
            if (uid != os.geteuid() or pid != guardian.get("pid")
                    or _start_token(pid) != guardian.get("start_token")):
                raise HandoffError("Guardian identity changed")
            channel.sendall(_encode({key: receipt[key] for key in ("nonce", "request_digest")}))
            if channel.recv(16) != b"claimed":
                raise HandoffError("Environment credential was not claimed")
            retain_lease_for_systemd_exit(lease, notify_socket)
            if time.monotonic() >= receipt["deadline"]:
                raise HandoffError("Handoff deadline expired before manager acknowledgement")
            channel.sendall(b"ready")
            if channel.recv(16) != b"committed":
                raise HandoffError("Guardian handoff acknowledgement unavailable")
        yield HostEnvironment(environment, receipt)


@contextmanager
def managed_host_environment(
    kernel: Kernel, action_id: str, manager: SystemdHost, *,
    runner_lock: Path, credential: Path, notify_socket: str,
) -> Iterator[HostEnvironment]:
    """Report real usage readiness before yielding to the existing driver."""
    request = kernel.host_request(action_id)
    with host_environment(request, runner_lock=runner_lock, credential=credential, notify_socket=notify_socket) as inherited:
        original = kernel.host_usage(action_id)
        if original is None or original.evidence != _encode(inherited.receipt).decode():
            raise HandoffError("Original guardian receipt is unavailable")
        manager.environment = {**inherited.environment, "LC_ALL": "C", "SYSTEMD_PAGER": "cat"}
        observation = manager.observe(request)
        if (observation.disposition != "running" or observation.main_pid != os.getpid()
                or observation.start_token != _start_token(os.getpid())
                or observation.boot_id != _BOOT.read_text().strip() or observation.cgroup != _cgroup(os.getpid())
                or manager.stored_descriptors(request, observation) != 1):
            raise HandoffError("Manager has not proven this Host and its retained lease")
        ready = replace(observation, usage_ready=True, evidence=original.evidence)
        if kernel.observe_host(request, ready) != "running":
            raise HandoffError("Kernel rejected the exact Host readiness")
        yield inherited


def _guardian_exited(receipt: Mapping[str, Any]) -> bool:
    if (receipt.get("boot_id") != _BOOT.read_text().strip()
            or receipt.get("pid_namespace") != os.readlink("/proc/self/ns/pid")):
        return False
    guardian = receipt.get("guardian")
    if (not isinstance(guardian, dict) or type(guardian.get("pid")) is not int
            or guardian["pid"] <= 1 or not isinstance(guardian.get("start_token"), str)):
        return False
    try:
        # Unlike a generic token-read failure, ENOENT in the original namespace
        # proves this original process is gone. A matching zombie stays unknown.
        Path(f"/proc/{guardian['pid']}/stat").read_bytes()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    token = _start_token(guardian["pid"])
    return token is not None and token != guardian["start_token"]


def reconcile_handoff(kernel: Kernel, action_id: str, manager: SystemdHost) -> HostObservation:
    """Record an exact automatic release; this never starts, stops or cleans a unit."""
    request, original = kernel.host_request(action_id), kernel.host_usage(action_id)
    if original is not None and original.usage_released:
        return original
    observation = manager.observe(request)
    accepted = kernel.observe_host(request, observation)
    if accepted != "exited" or observation.disposition != "exited" or original is None:
        return replace(observation, disposition="unknown") if accepted == "unknown" else observation
    receipt = json.loads(original.evidence)
    if (not isinstance(receipt, dict) or receipt.get("nonce") != request.nonce
            or receipt.get("request_digest") != _digest(request) or not _guardian_exited(receipt)
            or manager.stored_descriptors(request, observation) != 0):
        return observation
    released = replace(observation, usage_released=True, evidence=original.evidence)
    kernel.observe_host(request, released)
    return released
