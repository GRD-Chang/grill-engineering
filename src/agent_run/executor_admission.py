"""Close Resume and report global failure before existing work drains."""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from agent_run.error_safety import bounded_error
from agent_run.state import ConcurrentRunStateError, SimulatedProcessCrash
from agent_run.revisions import TicketGraphDriftError

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
    close = _close_admission.get()
    if close is not None:
        close(error)


def failure_recorded(error: BaseException) -> bool:
    return any(item is error for item in (_recorded_failures.get() or []))


@contextmanager
def executor_failure_scope(record: Callable[[BaseException], bool]) -> Iterator[None]:
    recorded: list[BaseException] = []
    attempted: list[BaseException] = []

    def report(error: BaseException) -> None:
        if any(item is error for item in attempted):
            return
        attempted.append(error)
        try:
            if record(error):
                recorded.append(error)
        except Exception as recording_error:
            error.add_note(f"Executor failure recording failed: {bounded_error(str(recording_error))}")

    report_token = _report_failure.set(report)
    recorded_token = _recorded_failures.set(recorded)
    try:
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
    except (SimulatedProcessCrash, ConcurrentRunStateError, TicketGraphDriftError):
        raise
    except BaseException as error:
        try:
            close_executor_admission(error)
        except Exception as closing_error:
            error.add_note(f"Executor admission close failed: {bounded_error(str(closing_error))}")
        report_executor_failure(error)
        raise


@contextmanager
def executor_admission_scope(close: Callable[[BaseException | None], None]) -> Iterator[None]:
    closed = False

    def close_once(error: BaseException | None) -> None:
        nonlocal closed
        if not closed:
            close(error)
            closed = True

    token = _close_admission.set(close_once)
    try:
        with close_admission_on_failure():
            yield
    finally:
        _close_admission.reset(token)
