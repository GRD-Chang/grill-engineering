"""Save admission, not work selection or a second business authority.

Only business writes queue. Explicit control writes share SQLite's existing
arbitration. Tickets contain no business facts; the original caller executes
the original transaction once, revalidating authority after its turn arrives.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
import ctypes
import math
import os
from pathlib import Path
import stat
from threading import Event, Lock, get_ident, get_native_id
from time import get_clock_info, monotonic
from typing import Any, Callable, Iterator, Literal, TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from agent_run.storage.sqlite import Transaction

from agent_run.adapters.foreground import DomainUnknown
from agent_run.errors import KernelError
from agent_run.storage.preflight import UnsupportedState

WRITE_CONTENTION_SECONDS = 1.0
MAX_PENDING_WRITES = 128  # Includes the active business save, never its payload.


_owner_lock = Lock()
_owners: dict[tuple[int, int], set[int]] = {}


def _after_fork() -> None:
    global _owner_lock, _owners
    _owner_lock, _owners = Lock(), {}


os.register_at_fork(after_in_child=_after_fork)


def database_identity(path: Path) -> tuple[int, int]:
    """Ancestor aliases may name the same ordinary, single-link database."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise UnsupportedState(path, "unreadable_state") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UnsupportedState(path, "uncertain_state")
    return metadata.st_dev, metadata.st_ino


def remaining(deadline: float) -> float:
    seconds = deadline - monotonic()
    if seconds <= 0:
        raise KernelError("storage_busy", "SQLite save contention deadline expired; retry the command")
    return seconds


def check_cancelled(cancelled: Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("SQLite save wait cancelled")


@contextmanager
def write_owner(path: Path, *, deadline: float, cancelled: Event | None) -> Iterator[tuple[int, int]]:
    """Local reentry/after-fork guard only; SQLite owns save ordering and capacity."""
    identity, owner, process = database_identity(path), get_ident(), os.getpid()
    registered: set[int] | None = None
    try:
        with _owner_lock:
            check_cancelled(cancelled)
            remaining(deadline)
            if owner in _owners.get(identity, ()):
                raise KernelError("storage_busy", "Nested writes to the same database are not supported")
            registered = _owners.setdefault(identity, set())
            registered.add(owner)
        yield identity
    finally:
        if registered is not None and os.getpid() == process:
            with _owner_lock:
                if _owners.get(identity) is registered:
                    registered.remove(owner)
                    if not registered:
                        del _owners[identity]


class SaveNotHead(RuntimeError):
    """Internal pre-body wait, never an application transaction retry."""


_PROCFS_MAGIC = 0x9fa0
_NSFS_MAGIC = 0x6e736673
# v1 did not prove offsets belonged to the current task; never adopt its tickets.
_SAVE_CLOCK_PROTOCOL = 'native-linux-local-wal-save/2'
_MONOTONIC_PROVIDER = 'clock_gettime(CLOCK_MONOTONIC)'


def _filesystem_type(descriptor: int) -> int:
    # Linux statfs begins with native __fsword_t f_type. Use aligned bounded
    # storage large enough for the supported Linux32/64 ABI's remaining fields;
    # no platform-dependent tail is interpreted as an identity claim.
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, 'fstatfs', None)
    if function is None or ctypes.sizeof(ctypes.c_long) not in {4, 8}:
        raise ValueError('Native Linux filesystem observation is unavailable')
    function.argtypes = [ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    words = (ctypes.c_long * 64)()
    if function(descriptor, ctypes.byref(words)) != 0:
        raise OSError(ctypes.get_errno(), 'Native filesystem observation failed')
    return int(words[0])


def _source_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('Clock source must be an original ordinary proc file')
    return metadata.st_dev, metadata.st_ino


def _bounded_text(path: Path, limit: int) -> str:
    before = _source_identity(path)  # No final symlink, FIFO or device open.
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        pinned = os.fstat(descriptor)
        if (not stat.S_ISREG(pinned.st_mode) or (pinned.st_dev, pinned.st_ino) != before
                or _filesystem_type(descriptor) != _PROCFS_MAGIC):
            raise ValueError('Clock source is not original native procfs')
        data = bytearray()
        while len(data) <= limit:
            block = os.read(descriptor, limit + 1 - len(data))
            if not block:
                break
            data.extend(block)
        if len(data) > limit:
            raise ValueError('Clock metadata exceeds its bound')
        after = os.fstat(descriptor)
        if ((after.st_dev, after.st_ino) != before or _source_identity(path) != before
                or _filesystem_type(descriptor) != _PROCFS_MAGIC):
            raise ValueError('Clock source changed during observation')
        return data.decode('ascii').strip()
    finally:
        os.close(descriptor)


@contextmanager
def _offset_namespace(descriptor: int) -> Iterator[Callable[[], None]]:
    """timens_offsets describes time_for_children: accept only current==children."""
    children = os.open('/proc/thread-self/ns/time_for_children', os.O_RDONLY | os.O_CLOEXEC)
    original: BaseException | None = None
    try:
        def same_namespace() -> None:
            observed = (os.fstat(descriptor), os.fstat(children), os.stat('/proc/thread-self/ns/time'),
                        os.stat('/proc/thread-self/ns/time_for_children'))
            identities = {(item.st_dev, item.st_ino) for item in observed}
            if (len(identities) != 1 or _filesystem_type(descriptor) != _NSFS_MAGIC
                    or _filesystem_type(children) != _NSFS_MAGIC):
                raise ValueError('Clock offsets are not bound to the current time namespace')
        same_namespace()
        yield same_namespace
        same_namespace()
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            os.close(children)
        except OSError as cleanup:
            if original is None:
                raise
            original.add_note('Time namespace cleanup unavailable: ' + type(cleanup).__name__)


def _clock_fields(descriptor: int) -> tuple[str, str, int, int, int, int,
                                           tuple[int, int], tuple[int, int], tuple[int, int]]:
    from agent_run.adapters.foreground import require_proc_view
    require_proc_view()
    info = get_clock_info('monotonic')
    if (info.implementation != _MONOTONIC_PROVIDER or not info.monotonic or info.adjustable
            or _filesystem_type(descriptor) != _NSFS_MAGIC):
        raise ValueError('Comparable native monotonic provider is unavailable')
    before = os.stat('/proc/thread-self/ns/time')
    pinned = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise ValueError('Current time namespace changed')
    with _offset_namespace(descriptor) as same_namespace:
        boot_path = Path('/proc/sys/kernel/random/boot_id')
        # The kernel exposes offsets in the top-level PID/TID directory, not
        # /proc/<pid>/task/<tid> (thread-self). Read this caller, not its leader.
        offsets_path = Path(f'/proc/{get_native_id()}/timens_offsets')
        boot_source, offsets_source = _source_identity(boot_path), _source_identity(offsets_path)
        boot = _bounded_text(boot_path, 64)
        if str(UUID(boot)) != boot:
            raise ValueError('Kernel boot identity is unavailable')
        offsets: dict[str, tuple[int, int]] = {}
        same_namespace()  # proc offsets uses this same task's children namespace.
        text = _bounded_text(offsets_path, 256)
        same_namespace()
        for line in text.splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[0] not in {'monotonic', 'boottime'} or fields[0] in offsets:
                raise ValueError('Clock offsets are ambiguous')
            seconds, nanos = int(fields[1]), int(fields[2])
            if (str(seconds) != fields[1] or str(nanos) != fields[2]
                    or not -(1 << 63) <= seconds < (1 << 63) or not 0 <= nanos <= 999999999):
                raise ValueError('Clock offsets are invalid')
            offsets[fields[0]] = (seconds, nanos)
        if set(offsets) != {'monotonic', 'boottime'}:
            raise ValueError('Clock offsets are incomplete')
        after = os.stat('/proc/thread-self/ns/time')
        if ((after.st_dev, after.st_ino) != (pinned.st_dev, pinned.st_ino)
                or _filesystem_type(descriptor) != _NSFS_MAGIC
                or _source_identity(boot_path) != boot_source or _source_identity(offsets_path) != offsets_source):
            raise ValueError('Current clock source changed')
        return (boot, info.implementation, pinned.st_dev, pinned.st_ino, *offsets['monotonic'],
                offsets['boottime'], boot_source, offsets_source)


def _domain_fields(value: str) -> list[Any]:
    try:
        if type(value) is not str or len(value.encode('utf-8')) > 512:
            raise ValueError('Invalid save clock record')
        fields = json.loads(value)
        if (not isinstance(fields, list) or len(fields) != 7 or fields[0] != _SAVE_CLOCK_PROTOCOL
                or type(fields[1]) is not str or str(UUID(fields[1])) != fields[1]
                or fields[2] != _MONOTONIC_PROVIDER
                or any(type(item) is not int for item in fields[3:])
                or not 0 <= fields[3] < (1 << 64) or not 0 < fields[4] < (1 << 64)
                or not -(1 << 63) <= fields[5] < (1 << 63)
                or not 0 <= fields[6] <= 999999999
                or json.dumps(fields, separators=(',', ':')) != value):
            raise ValueError('Invalid save clock record')
        return fields
    except (ValueError, TypeError, AttributeError) as error:
        raise KernelError('storage_clock_domain', 'Native save clock source is unproven; preserve original state') from error


def _domain_kind(current: str, recorded: str) -> Literal['same_clock', 'previous_boot']:
    now, old = _domain_fields(current), _domain_fields(recorded)
    # Only legal records of the native-source producer can enter this branch.
    # The deployment contract excludes multiple kernels writing this local WAL.
    if now[1] != old[1]:
        return 'previous_boot'
    if now[2] != old[2] or now[5:] != old[5:]:
        raise KernelError('storage_clock_domain', 'Save clock domains are not comparable; preserve original state')
    return 'same_clock'  # Namespace inodes pin each request, not every producer.


class SaveClock:
    """Pinned calling-task clock, never an exit proof.

    Trusted owner code, including signal callbacks, must not mutate namespaces
    during a request. Endpoint observations cannot prove absence of an ABA.
    """
    def __init__(self) -> None:
        self.descriptor = os.open('/proc/thread-self/ns/time', os.O_RDONLY | os.O_CLOEXEC)
        try:
            self.fields = _clock_fields(self.descriptor)
            self.domain = json.dumps([_SAVE_CLOCK_PROTOCOL, *self.fields[:6]], separators=(',', ':'))
            self.require()
        except BaseException:
            os.close(self.descriptor)
            raise

    def require(self) -> None:
        try:
            if _clock_fields(self.descriptor) != self.fields:
                raise ValueError('Clock domain changed')
        except (OSError, ValueError, DomainUnknown) as error:
            raise KernelError('storage_clock_domain', 'Comparable save clock is unavailable') from error

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass
class SaveCommit:
    attempted: bool = False
    confirmed: bool = False


class RepositorySave:
    """One immutable registration; caller owns all connections and transactions.

    These are bounded metadata operations on the original owner's connection.
    No method opens SQLite, executes a business callback or decides its outcome.
    """
    def __init__(self, clock: SaveClock, run_id: str | None, deadline: float) -> None:
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError('Finite save deadline required')
        if run_id is not None and (type(run_id) is not str or not run_id):
            raise KernelError('storage_save_binding')
        self.clock, self.run_id, self.deadline = clock, run_id, deadline
        self.request = uuid4().hex
        self.owner = os.getpid(), get_ident()
        self.commit = SaveCommit()
        self.body_entered = False
        self.position: int | None = None

    def require(self) -> None:
        if (os.getpid(), get_ident()) != self.owner:
            raise KernelError('storage_save_owner', 'Original save owner changed')
        self.clock.require()

    def _rows(self, tx: 'Transaction') -> list[Any]:
        self.require()
        rows = tx.rows('SELECT position,request,run_id,clock_domain,deadline FROM save_admissions ORDER BY position',
                       limit=MAX_PENDING_WRITES)
        self.require()  # Revalidate the calling task after the bounded row observation.
        # Classify the complete set before any mutation: an invalid later row
        # must not leave an earlier previous-boot deletion behind.
        for row in rows:
            _domain_kind(self.clock.domain, row['clock_domain'])
        return rows

    def _own(self, rows: list[Any]) -> Any:
        matches = [row for row in rows if row['request'] == self.request]
        if (len(matches) != 1 or matches[0]['run_id'] != self.run_id
                or matches[0]['clock_domain'] != self.clock.domain
                or matches[0]['deadline'] != self.deadline
                or self.position is not None and matches[0]['position'] != self.position):
            raise KernelError('storage_registration_unknown', 'Original save registration is unproven')
        return matches[0]

    def reconcile(self, tx: 'Transaction') -> None:
        row = self._own(self._rows(tx))
        self.position = row['position']

    def register(self, tx: 'Transaction') -> None:
        rows = self._rows(tx)
        if any(row['request'] == self.request for row in rows):
            self.reconcile(tx)
            return
        now = monotonic()
        remaining(self.deadline)
        retired = [row for row in rows if _domain_kind(self.clock.domain, row['clock_domain']) == 'previous_boot'
                   or row['deadline'] <= now]
        if len(rows) - len(retired) >= MAX_PENDING_WRITES:
            raise KernelError('storage_busy', 'SQLite save admission is full; retry the command')
        for row in retired:
            changed = tx.execute('DELETE FROM save_admissions WHERE position=? AND request=? AND run_id IS ? AND clock_domain=? AND deadline=?',
                (row['position'], row['request'], row['run_id'], row['clock_domain'], row['deadline']))
            if changed.rowcount != 1:
                raise KernelError('storage_registration_unknown')
        if self.run_id is not None:
            tx.one('SELECT id FROM runs WHERE id=?', (self.run_id,))
        cursor = tx.execute('INSERT INTO save_admissions(request,run_id,clock_domain,deadline) VALUES (?,?,?,?)',
            (self.request, self.run_id, self.clock.domain, self.deadline))
        self.position = cursor.lastrowid

    def admit(self, tx: 'Transaction') -> None:
        rows = self._rows(tx)
        remaining(self.deadline)
        own = self._own(rows)
        if any(_domain_kind(self.clock.domain, row['clock_domain']) != 'same_clock' for row in rows):
            raise KernelError('storage_clock_domain', 'Only registration may retire previous-boot metadata')
        live = [row for row in rows if row['deadline'] > monotonic()]
        if not live or own not in live:
            raise KernelError('storage_busy', 'SQLite save contention deadline expired')
        if live[0]['request'] != self.request:
            raise SaveNotHead('Earlier effective save registration')
        tx.execute('DELETE FROM save_admissions WHERE deadline<=?', (monotonic(),))

    def finish(self, tx: 'Transaction') -> None:
        # No deadline check here: admission contention is not a body timeout.
        self._own(self._rows(tx))
        changed = tx.execute('DELETE FROM save_admissions WHERE request=?', (self.request,))
        if changed.rowcount != 1:
            raise KernelError('storage_registration_unknown')

    def discard(self, tx: 'Transaction') -> None:
        rows = self._rows(tx)
        if any(row['request'] == self.request for row in rows):
            self._own(rows)
            tx.execute('DELETE FROM save_admissions WHERE request=?', (self.request,))
