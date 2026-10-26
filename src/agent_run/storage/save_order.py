"""Process-local save admission, not work selection or a second durable queue.

Only business writes queue. Explicit control writes share SQLite's existing
arbitration. Tickets contain no business facts; the original caller executes
the original transaction once, revalidating authority after its turn arrives.
"""
from collections import deque
from contextlib import contextmanager
import os
from pathlib import Path
import stat
from threading import Condition, Event, Lock, get_ident
from time import monotonic
from typing import Iterator

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
