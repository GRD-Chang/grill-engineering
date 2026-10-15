"""Bounded FIFO admission for synchronous local state commits."""
from __future__ import annotations

import threading
import weakref
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic


MAX_PENDING_COMMITS = 128
COMMIT_WAIT_SECONDS = 60.0


class StateCommitBusyError(RuntimeError):
    """The bounded local commit queue cannot admit this request."""


class CommitCoordinator:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._requests: deque[object] = deque()
        self._owner: int | None = None

    @contextmanager
    def admitted(self) -> Iterator[None]:
        owner = threading.get_ident()
        with self._condition:
            nested = self._owner == owner
            if not nested:
                if len(self._requests) >= MAX_PENDING_COMMITS:
                    raise StateCommitBusyError("local state commit queue is full")
                request = object()
                self._requests.append(request)
                deadline = monotonic() + COMMIT_WAIT_SECONDS
                try:
                    while self._requests[0] is not request:
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            raise StateCommitBusyError("local state commit queue wait expired")
                        self._condition.wait(remaining)
                    self._owner = owner
                except BaseException:
                    self._requests.remove(request)
                    self._condition.notify_all()
                    raise
        try:
            yield
        finally:
            if not nested:
                with self._condition:
                    self._owner = None
                    self._requests.popleft()
                    self._condition.notify_all()


_coordinators: weakref.WeakValueDictionary[Path, CommitCoordinator] = weakref.WeakValueDictionary()
_registry_lock = threading.Lock()


def coordinator_for(root: Path) -> CommitCoordinator:
    with _registry_lock:
        key = root.resolve()
        coordinator = _coordinators.get(key)
        if coordinator is None:
            coordinator = CommitCoordinator()
            _coordinators[key] = coordinator
        return coordinator
