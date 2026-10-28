"""把通知降级捕获的测试错误留给调用方独立核验。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Lock
from time import monotonic


class HeldCompletion:
    """A completion is evidence only while the original synthetic Worker waits."""

    def __init__(self) -> None:
        self.completed = Event()
        self.returned = Event()
        self._lock = Lock()
        self._held = False
        self._timed_out = False
        self._deadline: float | None = None

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._lock:
            if self._held or self._timed_out or self.completed.is_set():
                raise AssertionError('The original Worker hold cannot be reused')
            self._held = True
        try:
            yield
        finally:
            with self._lock:
                self._held = False

    def wait(self, event: Event, timeout: float) -> bool:
        with self._lock:
            assert self._held and not self._timed_out and not self.returned.is_set()
            self._deadline = monotonic() + timeout
        # Never hold the state lock while waiting or doing Core/Git work.
        ready = event.wait(timeout)
        with self._lock:
            if not ready or monotonic() >= self._deadline:
                self._timed_out = True
                return False
            self._deadline = None
            return True

    def complete_if_held(self) -> bool:
        """Call after the observer's real assertions, outside any I/O lock."""
        with self._lock:
            if (not self._held or self._timed_out or self.returned.is_set()
                    or (self._deadline is not None and monotonic() >= self._deadline)):
                return False
            self.completed.set()
            return True

    def mark_returned(self) -> None:
        with self._lock:
            assert self._held and not self._timed_out and self.completed.is_set()
            assert not self.returned.is_set()
            self.returned.set()

    def snapshot(self) -> dict[str, bool]:
        with self._lock:
            return {'held': self._held, 'timed_out': self._timed_out,
                    'completed': self.completed.is_set(), 'returned': self.returned.is_set()}


class ObserverErrors:
    def __init__(self) -> None:
        self._errors: list[BaseException] = []
        self._lock = Lock()

    @contextmanager
    def capture(self) -> Iterator[None]:
        try:
            yield
        except BaseException as error:
            with self._lock:
                self._errors.append(error)
            raise

    def raise_if_any(self) -> None:
        """调用方先关闭 sender；错误不能随业务恢复或取消被清除。"""
        with self._lock:
            errors = self._errors.copy()
        if errors:
            raise errors[0]
