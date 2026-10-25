"""Physical foreground PID namespace; lifecycle permission belongs to Core.

A dedicated, single-domain supervisor owns these handles under ``subreaper``.
Only bubblewrap's reported child, independently verified as namespace PID 1,
can prove domain exit. This module has no Core, auth, budget or routing imports.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Iterator, Mapping, Sequence


class DomainUnknown(RuntimeError):
    pass


def observer_anchor() -> dict[str, str]:
    require_proc_view()
    anchor = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'pid_namespace': os.readlink('/proc/self/ns/pid'), 'pid1_start': _stat(1)[2]}
    if not all(anchor.values()):
        raise DomainUnknown('Observer domain is unavailable')
    return anchor


def recover_init(domain: Mapping[str, Any]) -> str:
    """Only an authenticated original namespace-init and stable observer suffice.

    No signal is sent here. A new process at a reused PID is never adopted.
    Permission/resource failures and incomparable observer domains stay unknown.
    """
    descriptor = -1
    try:
        anchor, original = domain['observer'], domain['init']
        if (observer_anchor() != anchor or original['namespace_pids'][-1] != 1
                or len(original['namespace_pids']) < 2 or original['boot_id'] != anchor['boot_id']
                or original['observer_namespace'] != anchor['pid_namespace']
                or original['pid_namespace'] == anchor['pid_namespace']):
            return 'unknown'
        try:
            descriptor = os.pidfd_open(original['pid'])
        except ProcessLookupError:
            return 'exited' if observer_anchor() == anchor else 'unknown'
        current = _stat(original['pid'])
        if observer_anchor() != anchor:
            return 'unknown'
        if current[2] != original['start_token'] or select.select([descriptor], [], [], 0)[0]:
            return 'exited'
        if os.readlink(f"/proc/{original['pid']}/ns/pid") != original['pid_namespace']:
            return 'unknown'
        return 'running'
    except (OSError, ValueError, KeyError, TypeError, IndexError, DomainUnknown):
        return 'unknown'
    finally:
        if descriptor >= 0:os.close(descriptor)


def record_root(lock: Path, *, create: bool = False) -> Path:
    root = lock.parent / 'foreground-hosts'
    if create:root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise DomainUnknown('Foreground protection directory is not private and owned')
    return root


def read_record(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 32768):
            raise DomainUnknown('Foreground protection record is not private and owned')
        value = json.loads(stream.read(32769))
    if not isinstance(value, dict):raise DomainUnknown('Invalid foreground protection record')
    return value


def write_record(path: Path, value: Mapping[str, Any], *, create: bool = False) -> None:
    """Durable physical protection before any possible process creation."""
    body = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    if len(body) > 32768:raise DomainUnknown('Foreground protection record exceeds its limit')
    temporary = path.with_suffix('.pending')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            read_record(path)
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


BOOTSTRAP = """import os,sys
fd=int(sys.argv[1]); ready=int(sys.argv[2])
if ready >= 0:
    os.write(ready,b'R'); os.close(ready)
try: permitted=os.read(fd,1)
finally: os.close(fd)
if permitted != b'1': raise SystemExit(125)
os.execvpe(sys.argv[3],sys.argv[3:],os.environ)
"""


def require_proc_view() -> None:
    # pidfd_open/SCM_CREDENTIALS use the caller's PID namespace. A procfs mounted
    # for an ancestor namespace uses different numeric PIDs. Never combine them.
    try:
        if int(os.readlink('/proc/self')) != os.getpid():
            raise DomainUnknown('Procfs PID view does not match the observing process namespace')
    except (OSError, ValueError) as error:
        raise DomainUnknown('Cannot establish the observing procfs PID view') from error


def _stat(pid: int) -> tuple[str, int, str]:
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return fields[0], int(fields[1]), fields[19]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent: int
    start_token: str
    boot_id: str
    observer_namespace: str
    pid_namespace: str
    namespace_pids: tuple[int, ...]

    @classmethod
    def read(cls, pid: int) -> ProcessIdentity:
        require_proc_view()
        if type(pid) is not int or pid <= 1:
            raise DomainUnknown('Invalid process identifier')
        try:
            before = _stat(pid)
            namespace = os.readlink(f'/proc/{pid}/ns/pid')
            rows = Path(f'/proc/{pid}/status').read_text().splitlines()
            mapped = [row.split()[1:] for row in rows if row.startswith('NSpid:')]
            if len(mapped) != 1 or not mapped[0]:
                raise DomainUnknown('Kernel NSpid evidence is unavailable')
            identity = cls(pid, before[1], before[2], Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                           os.readlink('/proc/self/ns/pid'), namespace, tuple(map(int, mapped[0])))
            if _stat(pid)[1:] != before[1:] or not identity.boot_id:
                raise DomainUnknown('Process changed during identity capture')
            return identity
        except (OSError, ValueError, IndexError) as error:
            raise DomainUnknown('Process identity is unavailable') from error

    def wire(self) -> dict[str, Any]:
        return {**asdict(self), 'namespace_pids': list(self.namespace_pids)}


class ProcessPin:
    def __init__(self, identity: ProcessIdentity, descriptor: int, namespace_fd: int) -> None:
        self.identity, self.descriptor, self.namespace_fd = identity, descriptor, namespace_fd

    @classmethod
    def open(cls, pid: int, expected: Mapping[str, Any] | None = None) -> ProcessPin:
        descriptor = namespace_fd = -1
        try:
            before = ProcessIdentity.read(pid)
            if expected is not None and ({key: value for key, value in before.wire().items() if key != 'parent'}
                                         != {key: value for key, value in expected.items() if key != 'parent'}):
                raise DomainUnknown('Original process binding changed')
            descriptor = os.pidfd_open(pid)
            namespace_fd = os.open(f'/proc/{pid}/ns/pid', os.O_RDONLY | os.O_CLOEXEC)
            if (ProcessIdentity.read(pid) != before
                    or os.fstat(namespace_fd).st_ino != int(before.pid_namespace.split('[')[1][:-1])):
                raise DomainUnknown('Process changed while pinning its identity')
            return cls(before, descriptor, namespace_fd)
        except BaseException as error:
            if namespace_fd >= 0:os.close(namespace_fd)
            if descriptor >= 0:os.close(descriptor)
            if isinstance(error, (OSError, ValueError, IndexError)):
                raise DomainUnknown('Stable process handle unavailable') from error
            raise

    def observe(self) -> str:
        return 'exited' if select.select([self.descriptor], [], [], 0)[0] else 'running'

    def wait_exit(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('A finite observation deadline is required')
        if not select.select([self.descriptor], [], [], seconds)[0]:
            raise DomainUnknown('The pinned process has not exited')

    def kill(self) -> None:
        try:
            signal.pidfd_send_signal(self.descriptor, signal.SIGKILL)
        except ProcessLookupError:
            if self.observe() != 'exited':
                raise DomainUnknown('Process signal result is unknown')

    def reap(self) -> int | None:
        """Wait only this exact handle; ECHILD never invents a zero exit code."""
        try:
            result = os.waitid(os.P_PIDFD, self.descriptor, os.WEXITED | os.WNOHANG)
        except ChildProcessError:
            return None
        if result is None:
            raise DomainUnknown('Owned child is not ready for reaping')
        return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status

    def close(self) -> None:
        for name in ('descriptor', 'namespace_fd'):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)


def reopen(witness: Mapping[str, Any]) -> ProcessPin | None:
    """Absent, reused or unobservable identity is unknown, never proof of exit."""
    try:
        return ProcessPin.open(witness['pid'], witness)
    except (DomainUnknown, KeyError):
        return None


def subreaper_value() -> int:
    value = ctypes.c_int()
    if ctypes.CDLL(None, use_errno=True).prctl(37, ctypes.byref(value), 0, 0, 0):
        raise DomainUnknown('Cannot inspect the current subreaper state')
    return value.value


@contextmanager
def subreaper() -> Iterator[None]:
    previous = subreaper_value()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) or subreaper_value() != 1:
        raise DomainUnknown('A real child subreaper is required')
    try:
        yield
    finally:
        if libc.prctl(36, previous, 0, 0, 0):
            raise DomainUnknown('Could not restore the original subreaper state')


class Status:
    def __init__(self) -> None:
        self.buffer = b''
        self.bytes = 0
        self.child: tuple[int, int] | None = None
        self.exit_code: int | None = None

    def feed(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        if self.bytes > 16384:
            raise DomainUnknown('Bubblewrap status exceeded its bounded limit')
        self.buffer += chunk
        while b'\n' in self.buffer:
            raw, self.buffer = self.buffer.split(b'\n', 1)
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise DomainUnknown('Malformed bubblewrap status') from error
            if not isinstance(row, dict):
                raise DomainUnknown('Invalid bubblewrap status object')
            if 'child-pid' in row:
                pid, namespace = row['child-pid'], row.get('pid-namespace')
                if (type(pid) is not int or pid <= 1 or type(namespace) is not int or namespace <= 1
                        or self.child is not None and self.child != (pid, namespace)):
                    raise DomainUnknown('Missing or changed bubblewrap namespace identity')
                self.child = (pid, namespace)
            if 'exit-code' in row:
                code = row['exit-code']
                if type(code) is not int or not 0 <= code <= 255 or self.exit_code is not None and code != self.exit_code:
                    raise DomainUnknown('Missing or changed executor exit status')
                self.exit_code = code


def require_namespace_init(identity: ProcessIdentity, monitor_pid: int, outer_namespace: str, reported_namespace: int) -> None:
    if (identity.parent != monitor_pid or identity.pid_namespace == outer_namespace
            or len(identity.namespace_pids) < 2 or identity.namespace_pids[-1] != 1
            or identity.pid_namespace != f'pid:[{reported_namespace}]'):
        raise DomainUnknown('Reported child is not the actual new namespace init')


class NamespaceProcess:
    """One bounded physical domain; explicit bootstrap release is separate."""
    def __init__(self, command: Sequence[str], environment: Mapping[str, str], *, pass_fds: tuple[int, ...] = ()) -> None:
        require_proc_view()
        if not command or not Path(command[0]).is_absolute() or subreaper_value() != 1:
            raise DomainUnknown('A dedicated subreaper and absolute command are required')
        executable = shutil.which('bwrap', path=environment.get('PATH'))
        if executable is None:
            raise DomainUnknown('bubblewrap is unavailable')
        self.status = Status()
        self.init: ProcessPin | None = None
        self.monitor_pin: ProcessPin | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.result: dict[str, Any] | None = None
        self.read_fd, status_write = os.pipe2(os.O_CLOEXEC)
        gate_read, self.gate = os.pipe2(os.O_CLOEXEC)
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
        self.arguments = [executable, '--die-with-parent', '--bind', '/', '/', '--dev', '/dev',
                          '--unshare-pid', '--proc', '/proc',
                          '--json-status-fd', str(status_write), '--', sys.executable, '-I', '-c', BOOTSTRAP,
                          str(gate_read), str(ready_write), *command]
        try:
            deadline = time.monotonic() + 3
            self.process = subprocess.Popen(self.arguments, env=dict(environment), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
                pass_fds=(*pass_fds, gate_read, status_write, ready_write))
            assert self.process.stderr is not None
            os.set_blocking(self.process.stderr.fileno(), False)
            os.set_blocking(self.read_fd, False)
            os.close(status_write)
            status_write = -1
            os.close(gate_read)
            gate_read = -1
            os.close(ready_write)
            ready_write = -1
            self.monitor_pin = ProcessPin.open(self.process.pid)
            while self.status.child is None:
                if time.monotonic() >= deadline or not self._read_status(deadline):
                    raise DomainUnknown('No namespace-init evidence: ' + self.diagnostic())
            pid, namespace = self.status.child
            candidate = ProcessPin.open(pid)
            try:
                require_namespace_init(candidate.identity, self.process.pid, os.readlink('/proc/self/ns/pid'), namespace)
            except BaseException:
                candidate.close()
                raise
            self.init = candidate
            if (not select.select([ready_read], [], [], max(0, deadline-time.monotonic()))[0]
                    or os.read(ready_read, 1) != b'R'):
                raise DomainUnknown('Original bootstrap did not become ready: ' + self.diagnostic())
        except BaseException as error:
            try:
                detail = self.diagnostic()
                if detail:error.add_note('bubblewrap setup: ' + detail)
            except BaseException as diagnostic:
                error.add_note('Diagnostic unavailable: ' + str(diagnostic))
            finally:
                try:self.close()
                except BaseException as cleanup:error.add_note('Cleanup: ' + str(cleanup))
            raise
        finally:
            if gate_read >= 0:os.close(gate_read)
            if status_write >= 0:os.close(status_write)
            os.close(ready_read)
            if ready_write >= 0:os.close(ready_write)

    def diagnostic(self) -> str:
        if self.process is None or self.process.stderr is None:return ''
        try:
            descriptor = self.process.stderr.fileno()
            # Even failure to configure the pipe must never block cleanup.
            if os.get_blocking(descriptor):return ''
            return os.read(descriptor, 4096).decode(errors='replace')
        except (OSError, ValueError):return ''

    def _read_status(self, deadline: float) -> bool:
        if not select.select([self.read_fd], [], [], max(0, deadline-time.monotonic()))[0]:
            return False
        data = os.read(self.read_fd, 16384)
        self.status.feed(data)
        return bool(data)

    def release(self) -> None:
        """Release the outer bootstrap; the executor still requires its Core grant."""
        if self.gate < 0 or self.init is None or self.init.observe() != 'running':
            raise DomainUnknown('No live original namespace to authorize')
        os.write(self.gate, b'1')
        os.close(self.gate)
        self.gate = -1

    def finish(self, seconds: float = 3) -> dict[str, Any]:
        if self.init is None or self.process is None:
            raise DomainUnknown('Namespace identity was never established')
        self.init.wait_exit(seconds)
        monitor_code = self.process.wait(timeout=seconds)
        deadline = time.monotonic() + seconds
        while self._read_status(deadline):
            pass
        init_code = self.init.reap()
        self.result = {'namespace_exited': True, 'namespace_exit_code': init_code,
                       'executor_exit_code': self.status.exit_code, 'monitor_exit_code': monitor_code}
        return self.result

    def close(self) -> None:
        """Close every owned handle even if an independent cleanup step fails."""
        errors: list[str] = []
        deadline = time.monotonic() + 3
        if self.gate >= 0:
            os.close(self.gate)
            self.gate = -1
        pins = [pin for pin in (self.init, self.monitor_pin) if pin is not None and pin.descriptor >= 0]
        # On setup failure only, capture direct children of our still-owned
        # monitor. No global /proc scan, process-group kill or arbitrary wait.
        if self.init is None and self.process is not None:
            try:
                children = Path(f'/proc/{self.process.pid}/task/{self.process.pid}/children').read_text().split()
                for child in children:
                    pin = ProcessPin.open(int(child))
                    if pin.identity.parent == self.process.pid:pins.insert(0, pin)
                    else:pin.close()
            except (OSError, DomainUnknown):
                pass
        for pin in pins:
            try:
                if pin.observe() != 'exited':pin.kill()
                pin.wait_exit(max(0, deadline-time.monotonic()))
            except (OSError, DomainUnknown) as error:errors.append(str(error))
        if self.process is not None:
            try:
                # Popen still owns this unreaped direct child even if opening
                # its pidfd failed. This is cleanup, never domain-exit proof.
                self.process.kill()
                self.process.wait(timeout=max(0, deadline-time.monotonic()))
            except (OSError, subprocess.TimeoutExpired) as error:errors.append(str(error))
        for pin in pins:
            try:
                if pin.observe() == 'exited':pin.reap()
            except (OSError, DomainUnknown) as error:errors.append(str(error))
            finally:pin.close()
        if self.read_fd >= 0:
            os.close(self.read_fd)
            self.read_fd = -1
        if self.process is not None and self.process.stderr is not None:self.process.stderr.close()
        if errors:raise DomainUnknown('; '.join(errors))
