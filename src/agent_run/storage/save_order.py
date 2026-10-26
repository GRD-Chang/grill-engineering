"""Save admission, not work selection or a second business authority.

Only business writes queue. Explicit control writes share SQLite's existing
arbitration. Tickets contain no business facts; the original caller executes
the original transaction once, revalidating authority after its turn arrives.
"""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat
from threading import Condition, Event, Lock, get_ident
from time import get_clock_info, monotonic
from typing import Any, Iterator, TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from agent_run.storage.sqlite import Transaction

from agent_run.adapters.foreground import DomainUnknown
from agent_run.errors import KernelError
from agent_run.storage.preflight import UnsupportedState

WRITE_CONTENTION_SECONDS = 1.0
MAX_PENDING_WRITES = 128  # Includes the active business save, never its payload.


class _Ticket:
    def __init__(self) -> None:
        self.owner = get_ident()
        self.active = False


class _Gate:
    def __init__(self) -> None:
        self.queue: deque[_Ticket] = deque()
        self.requests: set[_Ticket] = set()


_condition = Condition(Lock())
_gates: dict[tuple[int, int], _Gate] = {}


def _after_fork() -> None:
    global _condition, _gates
    _condition, _gates = Condition(Lock()), {}


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
def save_turn(path: Path, *, ordered: bool, deadline: float,
              cancelled: Event | None) -> Iterator[tuple[int, int]]:
    identity, ticket = database_identity(path), _Ticket()
    gate: _Gate | None = None
    try:
        with _condition:
            gate = _gates.get(identity)
            if gate is None:
                gate = _Gate()
                _gates[identity] = gate
            if any(item.owner == ticket.owner and item.active for item in gate.requests):
                raise KernelError("storage_busy", "Nested writes to the same database are not supported")
            if ordered and len(gate.queue) >= MAX_PENDING_WRITES:
                raise KernelError("storage_busy", "SQLite save admission is full; retry the command")
            gate.requests.add(ticket)
            if ordered:
                gate.queue.append(ticket)
            while True:
                check_cancelled(cancelled)
                seconds = remaining(deadline)
                if not ordered or gate.queue[0] is ticket:
                    ticket.active = True
                    break
                _condition.wait(min(seconds, 0.05) if cancelled is not None else seconds)
        yield identity
    finally:
        if gate is not None:
            with _condition:
                gate.requests.discard(ticket)
                try:
                    gate.queue.remove(ticket)
                except ValueError:
                    pass
                if not gate.requests and _gates.get(identity) is gate:
                    del _gates[identity]
                _condition.notify_all()


class SaveNotHead(RuntimeError):
    """Internal pre-body wait, never an application transaction retry."""


def _bounded_text(path: Path, limit: int) -> str:
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Clock metadata exceeds its bound')
    return data.decode('ascii').strip()


def _clock_fields(descriptor: int) -> tuple[str, str, int, int, int, int]:
    from agent_run.adapters.foreground import require_proc_view
    require_proc_view()
    info = get_clock_info('monotonic')
    if (info.implementation != 'clock_gettime(CLOCK_MONOTONIC)'
            or not info.monotonic or info.adjustable):
        raise ValueError('Comparable monotonic provider is unavailable')
    before = os.stat('/proc/self/ns/time')
    pinned = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise ValueError('Current time namespace changed')
    boot = _bounded_text(Path('/proc/sys/kernel/random/boot_id'), 64)
    if str(UUID(boot)) != boot:
        raise ValueError('Kernel boot identity is unavailable')
    offsets: dict[str, tuple[int, int]] = {}
    for line in _bounded_text(Path('/proc/self/timens_offsets'), 256).splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] not in {'monotonic', 'boottime'} or fields[0] in offsets:
            raise ValueError('Clock offsets are ambiguous')
        seconds, nanos = int(fields[1]), int(fields[2])
        if not 0 <= nanos <= 999999999:
            raise ValueError('Clock offsets are invalid')
        offsets[fields[0]] = (seconds, nanos)
    if set(offsets) != {'monotonic', 'boottime'}:
        raise ValueError('Clock offsets are incomplete')
    after = os.stat('/proc/self/ns/time')
    if (after.st_dev, after.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise ValueError('Current time namespace changed')
    return boot, info.implementation, pinned.st_dev, pinned.st_ino, *offsets['monotonic']


class SaveClock:
    """A pinned current time namespace, not time_for_children or an exit proof."""
    def __init__(self) -> None:
        self.descriptor = os.open('/proc/self/ns/time', os.O_RDONLY | os.O_CLOEXEC)
        try:
            self.fields = _clock_fields(self.descriptor)
            self.domain = json.dumps(['CLOCK_MONOTONIC', *self.fields], separators=(',', ':'))
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
        # Do not compare even apparently old numbers from another domain.
        if any(row['clock_domain'] != self.clock.domain for row in rows):
            raise KernelError('storage_clock_domain', 'Save clock domains are not comparable; preserve original state')
        return rows

    def _own(self, rows: list[Any]) -> Any:
        matches = [row for row in rows if row['request'] == self.request]
        if (len(matches) != 1 or matches[0]['run_id'] != self.run_id
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
        live = [row for row in rows if row['deadline'] > now]
        if len(live) >= MAX_PENDING_WRITES:
            raise KernelError('storage_busy', 'SQLite save admission is full; retry the command')
        tx.execute('DELETE FROM save_admissions WHERE deadline<=?', (now,))
        if self.run_id is not None:
            tx.one('SELECT id FROM runs WHERE id=?', (self.run_id,))
        cursor = tx.execute('INSERT INTO save_admissions(request,run_id,clock_domain,deadline) VALUES (?,?,?,?)',
            (self.request, self.run_id, self.clock.domain, self.deadline))
        self.position = cursor.lastrowid

    def admit(self, tx: 'Transaction') -> None:
        rows = self._rows(tx)
        remaining(self.deadline)
        own = self._own(rows)
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
