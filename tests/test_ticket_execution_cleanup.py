"""A failed test must drain Ticket workers without manufacturing success."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event, Thread, current_thread

import pytest

from agent_run.executor_admission import check_executor_cancelled
from agent_run.task_control import TaskControlBusyError
from support.ticket_execution import TicketExecution


@pytest.mark.parametrize("failure", ["assertion", "unfinished"])
def test_failure_cancels_before_gate_release_and_joins_worker_pool(failure: str) -> None:
    ready, release, cancelled, exited = (Event() for _ in range(4))
    workers: list[Thread] = []

    def work() -> None:
        workers.append(current_thread())
        ready.set()
        assert release.wait(5)
        try:
            if failure == "assertion":
                check_executor_cancelled()
                raise AssertionError("gate released before cancellation")
            while True:
                check_executor_cancelled()
                cancelled.wait(0.001)
        except TaskControlBusyError:
            cancelled.set()
            raise
        finally:
            exited.set()

    def dispatch() -> None:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ticket-cleanup") as pool:
            pool.submit(copy_context().run, work).result()

    execution = TicketExecution(dispatch, release=release.set, name="ticket-cleanup-dispatch")
    message = "controlled target assertion" if failure == "assertion" else "did not finish normally"
    with pytest.raises(AssertionError, match=message):
        with execution:
            assert ready.wait(5)
            if failure == "assertion":
                raise AssertionError(message)
            # This worker can only exit through cancellation. finish must
            # still fail, even though __exit__ then drains it successfully.
            execution.finish(timeout=0)

    assert cancelled.is_set() and exited.is_set()
    assert not execution.thread.is_alive()
    assert workers and all(not worker.is_alive() for worker in workers)
    assert len(execution.errors) == 1
    assert isinstance(execution.errors[0], TaskControlBusyError)
