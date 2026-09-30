"""Execution-local cancellation, checked by the owner of a Worker process."""

from contextvars import ContextVar
from typing import Callable


worker_control: ContextVar[Callable[[], None] | None] = ContextVar(
    "worker_control", default=None,
)


def check_worker_control() -> None:
    check = worker_control.get()
    if check is not None:
        check()
