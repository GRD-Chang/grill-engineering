"""Own a test's Ticket dispatcher until all of its worker threads exit."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import copy_context
from threading import Thread
from types import TracebackType
from typing import Any

import pytest

from agent_run.executor_admission import (
    close_executor_admission,
    executor_cancellation_scope,
)
from agent_run.run_orchestration import DeliveryRunEngine


class _TicketFrontierReturned(Exception):
    pass


def deliver_through_ticket_frontier(
    engine: DeliveryRunEngine, run_id: str, state: dict[str, Any],
) -> None:
    """Keep the Driver's real callbacks, ending after its pool has drained."""
    frontier = engine.tickets.develop_frontier

    def finish_frontier(*args: Any, **kwargs: Any) -> bool:
        assert frontier(*args, **kwargs), "The scenario needs a live Ticket frontier"
        # Only a successfully returned stage reaches this boundary. A graph
        # drift or execution failure still follows the real Driver's path.
        raise _TicketFrontierReturned()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(engine.tickets, "develop_frontier", finish_frontier)
        try:
            engine.deliver_from_state(run_id, state)
        except _TicketFrontierReturned:
            pass


class TicketExecution:
    def __init__(
        self, target: Callable[[], None], *, release: Callable[[], None], name: str,
    ) -> None:
        self.errors: list[BaseException] = []
        self._release = release
        self._finished = False
        # Separate Context copies share this execution's cancellation signal;
        # the cleanup copy can be entered while the dispatcher is running.
        with executor_cancellation_scope():
            execution_context = copy_context()
            self._cleanup_context = copy_context()

        def run() -> None:
            try:
                target()
            except BaseException as error:
                self.errors.append(error)

        self.thread = Thread(target=execution_context.run, args=(run,), name=name)

    def __enter__(self) -> TicketExecution:
        self.thread.start()
        return self

    def finish(self, timeout: float) -> None:
        """Require normal exit before failure cleanup is allowed to cancel."""
        self._release()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "Ticket dispatcher did not finish normally"
        assert not self.errors, self.errors
        self._finished = True

    def __exit__(
        self, exc_type: type[BaseException] | None, _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self.thread.is_alive():
            self._cleanup_context.run(close_executor_admission)
            self._release()
            # Cancellation closes admission before releasing controlled calls.
            # The dispatcher's pool context joins every in-flight worker. Do
            # not let fixture teardown race a still-running Git/SQLite writer.
            self.thread.join()
        assert not self.thread.is_alive()
        if exc_type is None:
            assert self._finished, "Normal Ticket exit was not checked before cleanup"
