"""Close Resume and report global failure before existing work drains."""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from threading import Event, Lock

from agent_run.error_safety import bounded_error
from agent_run.state import ConcurrentRunStateError, SimulatedProcessCrash
from agent_run.task_control import TaskControlBusyError

_cancelled: ContextVar[Event | None] = ContextVar("executor_cancelled", default=None)


@contextmanager
def executor_cancellation_scope() -> Iterator[None]:
    """Share a stop signal with this execution's copied Worker contexts only."""
    if _cancelled.get() is not None:
        yield
        return
    token = _cancelled.set(Event())
    try:
        yield
    finally:
        _cancelled.reset(token)


def check_executor_cancelled() -> None:
    stopped = _cancelled.get()
    if stopped is not None and stopped.is_set():
        raise TaskControlBusyError("Executor stopped; no new work is authorized")


def _record_gap(error: BaseException, message: str) -> None:
    message = bounded_error(message)
    error.add_note(message)
    # The host captures stderr/journal even when neither durable store accepts
    # diagnostics. This reports a gap, never a persisted failure/exit fact.
    logging.getLogger(__name__).error("%s", message)

_close_admission: ContextVar[Callable[[BaseException | None], None] | None] = ContextVar(
    "executor_failure_admission", default=None,
)
_report_failure: ContextVar[Callable[[BaseException], None] | None] = ContextVar(
    "executor_failure_reporter", default=None,
)
_recorded_failures: ContextVar[list[BaseException] | None] = ContextVar(
    "executor_recorded_failures", default=None,
)


def close_executor_admission(error: BaseException | None = None) -> None:
    stopped = _cancelled.get()
    if stopped is not None:
        stopped.set()
    close = _close_admission.get()
    if close is not None:
        close(error)


def failure_recorded(error: BaseException) -> bool:
    return any(item is error for item in (_recorded_failures.get() or []))


@contextmanager
def executor_failure_scope(record: Callable[[BaseException], bool]) -> Iterator[None]:
    recorded: list[BaseException] = []
    attempted: list[BaseException] = []
    lock = Lock()

    def report(error: BaseException) -> None:
        with lock:
            if any(item is error for item in attempted):
                return
            attempted.append(error)
        try:
            if record(error):
                recorded.append(error)
        except Exception as recording_error:
            _record_gap(error, f"Executor failure recording failed; durable failure/exit unknown: {recording_error}")

    report_token = _report_failure.set(report)
    recorded_token = _recorded_failures.set(recorded)
    try:
        with executor_cancellation_scope():
            yield
    finally:
        _recorded_failures.reset(recorded_token)
        _report_failure.reset(report_token)


def report_executor_failure(error: BaseException) -> None:
    report = _report_failure.get()
    if report is not None:
        report(error)


@contextmanager
def close_admission_on_failure() -> Iterator[None]:
    try:
        yield
    except (SimulatedProcessCrash, ConcurrentRunStateError):
        raise
    except BaseException as error:
        try:
            close_executor_admission(error)
        except Exception as closing_error:
            _record_gap(error, f"Executor admission close failed; durable admission unknown: {closing_error}")
        report_executor_failure(error)
        raise


@contextmanager
def executor_admission_scope(close: Callable[[BaseException | None], None]) -> Iterator[None]:
    closed = False
    lock = Lock()

    def close_once(error: BaseException | None) -> None:
        nonlocal closed
        with lock:
            if closed:
                return
            closed = True
        close(error)

    token = _close_admission.set(close_once)
    try:
        with executor_cancellation_scope(), close_admission_on_failure():
            yield
    finally:
        _close_admission.reset(token)
