"""把通知降级捕获的测试错误留给调用方独立核验。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock


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
