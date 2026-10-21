"""Execute one allocated call, with no role, budget or recovery decisions.

Linux process mechanics adapted from worker_sandbox.run_worker_process and
executor_host._process_start_token. Those modules are not imported: their
local imports would bring the legacy State/TaskControl ownership back here.
This is a local process adapter, not a systemd/cgroup or live Codex adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
import os
from pathlib import Path
import select
import selectors
import signal
import subprocess
import sys
import time
from types import MappingProxyType
from typing import TYPE_CHECKING

from agent_run.error_safety import bounded_error

from .model import AgentCall
from .storage import MATERIAL_LIMIT

if TYPE_CHECKING:
    from .kernel import Kernel


_BOOTSTRAP = """\
import os, sys
gate = int(sys.argv[1])
try:
    released = os.read(gate, 1)
finally:
    os.close(gate)
if released != b"1":
    raise SystemExit(125)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ProcessExit:
    identity: Mapping[str, int | str]
    returncode: int
    raw: bytes
    stderr: str


class ProcessExitUnknown(RuntimeError):
    """No lease may be released on this unproven process observation."""


def _start_token(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    fields = raw[closing + 2:].split() if closing >= 0 else []
    return fields[19] if len(fields) > 19 else None


def _identity(pid: int) -> dict[str, int | str]:
    host = os.getpid()
    start, host_start = _start_token(pid), _start_token(host)
    if start is None or host_start is None or os.getpgid(pid) != pid:
        raise ProcessExitUnknown("Worker process identity is unavailable")
    return {
        "schema": "linux-process-v1", "pid": pid, "start_token": start,
        "pgid": pid, "host_pid": host, "host_start_token": host_start,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }


def _terminate(process: subprocess.Popen[bytes], identity: Mapping[str, int | str]) -> None:
    def require_identity() -> None:
        current = _start_token(process.pid)
        if (current is not None and current != identity["start_token"]
                or current is None and process.poll() is None):
            raise ProcessExitUnknown("Worker identity changed; no group signal sent")

    require_identity()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=1)
        return
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        require_identity()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=1)


def _drain(
    process: subprocess.Popen[bytes], prompt: str, timeout: float,
    kernel: Kernel, call: AgentCall, identity: Mapping[str, int | str],
) -> tuple[bytes, str]:
    raw, errors = bytearray(), bytearray()
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    remaining_input = memoryview(prompt.encode("utf-8"))
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        for stream, label in ((process.stdout, "raw"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)
        if remaining_input:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while selector.get_map() or process.poll() is None:
            if process.poll() is None:
                kernel.guard_agent(call)
                if time.monotonic() >= deadline:
                    raise TimeoutError("Worker process exceeded its execution deadline")
            else:
                # Reap/stop descendants that would otherwise retain our pipes.
                _terminate(process, identity)
            for key, _ in selector.select(timeout=0.05):
                if key.data == "stdin":
                    try:
                        written = os.write(key.fd, remaining_input[:_CHUNK])
                        remaining_input = remaining_input[written:]
                    except BrokenPipeError:
                        remaining_input = memoryview(b"")
                    if not remaining_input:
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                    continue
                chunk = os.read(key.fd, _CHUNK)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == "raw":
                    if len(raw) + len(chunk) > MATERIAL_LIMIT:
                        raise ValueError("Worker return exceeds the material limit; no truncated return saved")
                    raw.extend(chunk)
                else:
                    errors.extend(chunk)
                    del errors[:-_CHUNK]
    return bytes(raw), bounded_error(errors.decode("utf-8", errors="replace"))


def execute(
    kernel: Kernel, call: AgentCall, *, arguments: Sequence[str], cwd: Path,
    prompt: str, environment: Mapping[str, str], timeout: float,
    authorize_source: Callable[[], None] | None = None,
    credential_file: Path | None = None,
) -> ProcessExit | None:
    """Run once; a repeated sent/unknown call is observed through Kernel only.

    Real-source drivers must supply their reader -> Kernel.refresh guard.
    None is reserved for explicitly synthetic inputs. No online identity is
    discovered, changed, or retried here; credential_file is an existing
    selected Worker credential reference, never its secret contents.
    """
    if not kernel.dispatch_agent(call):
        return None
    if credential_file is not None:
        try:
            with credential_file.open("rb"):
                pass
        except OSError:
            kernel.agent_unavailable(call, "worker_credentials_unavailable", "Selected Worker credentials are unavailable")
            return None
    try:
        if not arguments or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Worker arguments and positive finite timeout are required")
        if not hasattr(os, "pidfd_open"):
            raise OSError("The local process adapter requires Linux pidfd")
        if authorize_source is not None:
            authorize_source()
        kernel.guard_agent(call)
    except BaseException as error:
        kernel.agent_unavailable(call, "worker_preparation_failed", {"error_type": type(error).__name__})
        raise
    gate_read, gate_write = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", _BOOTSTRAP, str(gate_read), *arguments],
            cwd=cwd, env=dict(environment), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, pass_fds=(gate_read,),
        )
    except OSError as error:
        os.close(gate_write)
        kernel.agent_unavailable(call, "worker_start_failed", {"errno": error.errno})
        return None
    except BaseException:
        os.close(gate_write)
        kernel.agent_start_unknown(call, {"boundary": "Popen"})
        raise
    finally:
        os.close(gate_read)
    identity: dict[str, int | str] | None = None
    pidfd: int | None = None
    bound = False
    raw, stderr = b"", ""
    failure: BaseException | None = None
    try:
        pidfd = os.pidfd_open(process.pid)
        identity = _identity(process.pid)
        kernel.agent_started(call, identity)
        bound = True
        if authorize_source is not None:
            authorize_source()
        kernel.guard_agent(call)
        os.write(gate_write, b"1")
        os.close(gate_write)
        gate_write = -1
        raw, stderr = _drain(process, prompt, timeout, kernel, call, identity)
    except BaseException as error:
        failure = error
    finally:
        if gate_write >= 0:
            os.close(gate_write)
        try:
            if identity is None:
                # Without release, EOF makes the bootstrap exit without argv.
                process.wait(timeout=1)
            else:
                _terminate(process, identity)
            if pidfd is None or not select.select([pidfd], [], [], 1)[0]:
                raise ProcessExitUnknown("Worker exit has not been observed through its pidfd")
        except (OSError, subprocess.TimeoutExpired, ProcessExitUnknown) as error:
            failure = ProcessExitUnknown("Worker exit remains unproven")
            failure.__cause__ = error
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            if pidfd is not None:
                os.close(pidfd)
    if isinstance(failure, ProcessExitUnknown) or identity is None:
        kernel.agent_start_unknown(call, {"boundary": "process binding/exit", "pid": process.pid})
        raise failure or ProcessExitUnknown("Worker binding remains unproven")
    if not bound:
        # A failed binding commit may have succeeded before its response was
        # lost. Report the observed facts, never authorize a second process.
        kernel.agent_start_unknown(call, {
            "boundary": "durable binding", "process_identity": identity,
            "observed_exit": process.returncode,
        })
        raise failure or ProcessExitUnknown("Worker binding commit is unknown")
    if failure is None and process.returncode == 0:
        # Actual process exit was observed above. Recording raw is permitted
        # even if Stop/epoch retirement happens before this separate commit.
        kernel.receive(call, raw)
    assert process.returncode is not None
    kernel.agent_exited(
        call, identity, process.returncode,
        failure=None if failure is None else type(failure).__name__,
    )
    if failure is not None:
        raise failure
    return ProcessExit(MappingProxyType(identity), process.returncode, raw, stderr)
