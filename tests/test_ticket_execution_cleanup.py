"""A failed test must drain Ticket workers without manufacturing success."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import Mock

import pytest

from agent_run.executor_admission import check_executor_cancelled
from agent_run.task_control import TaskControlBusyError
from support.ticket_execution import TicketExecution
from support.background_delivery import background_delivery
from support.ticket_process import isolated_ticket_test


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


@pytest.mark.parametrize("release_fails", [False, True])
def test_cleanup_timeout_keeps_original_error_and_requires_bounded_join(
    monkeypatch: pytest.MonkeyPatch, release_fails: bool,
) -> None:
    ready, stop = Event(), Event()

    def dispatch() -> None:
        ready.set()
        stop.wait()

    def release() -> None:
        if release_fails:
            raise RuntimeError("controlled release failure")

    execution = TicketExecution(
        dispatch, release=release, name="noncooperative-ticket", cleanup_timeout=0,
    )
    original_join = execution.thread.join
    joins: list[float | None] = []

    def bounded_join(timeout: float | None = None) -> None:
        joins.append(timeout)
        # Make the old unbounded implementation fail instead of hanging the
        # regression. The real worker remains held until the finalizer below.
        assert timeout is not None, "cleanup attempted an unbounded join"
        original_join(timeout)

    monkeypatch.setattr(execution.thread, "join", bounded_join)
    original = AssertionError("controlled test failure")
    try:
        with pytest.raises(AssertionError) as raised:
            with execution:
                assert ready.wait(5)
                raise original
        assert raised.value is original
        assert joins == [0]
        assert execution.thread.is_alive(), "cleanup must not manufacture normal exit"
        notes = "\n".join(original.__notes__)
        assert "did not exit during bounded cleanup" in notes
        if release_fails:
            assert "controlled release failure" in notes
    finally:
        stop.set()
        original_join(timeout=5)
        assert not execution.thread.is_alive()


def test_finish_proves_normal_exit_before_cancellation() -> None:
    ready, release = Event(), Event()

    def dispatch() -> None:
        ready.set()
        assert release.wait(5)
        check_executor_cancelled()

    with TicketExecution(dispatch, release=release.set, name="normal-ticket") as execution:
        assert ready.wait(5)
        execution.finish(timeout=5)
        assert not execution.thread.is_alive()
        assert not execution.errors


@pytest.mark.parametrize("failure", ["admission", "terminate", "sender"])
@pytest.mark.parametrize("primary_failure", [False, True])
def test_background_cleanup_preserves_original_error_and_releases_dependencies(
    monkeypatch: pytest.MonkeyPatch, failure: str, primary_failure: bool,
) -> None:
    ready, release = Event(), Event()
    threads: list[Thread] = []
    control, process, sender = Mock(), Mock(), Mock()
    control.load.return_value = {"executor": {"action_id": "action", "generation": 1}}
    process.poll.return_value = None
    cleanup_error = RuntimeError(f"controlled {failure} cleanup failure")
    if failure == "admission":
        control.close_ticket_resume_admission.side_effect = cleanup_error
    if failure == "terminate":
        process.terminate.side_effect = cleanup_error

    def close_sender() -> None:
        assert threads and all(not thread.is_alive() for thread in threads)
        if failure == "sender":
            raise cleanup_error

    sender.close.side_effect = close_sender
    monkeypatch.setattr(
        "support.background_delivery._bind_running_executor",
        lambda _repo, _run_id: (control, Mock(), process),
    )

    def dispatch() -> None:
        threads.append(current_thread())
        ready.set()
        assert release.wait(5)

    original = AssertionError("controlled background test failure")
    try:
        with pytest.raises(AssertionError if primary_failure else RuntimeError) as raised:
            with background_delivery(Mock(), "run", dispatch, sender, [release]):
                assert ready.wait(5)
                if primary_failure:
                    raise original
        assert raised.value is (original if primary_failure else cleanup_error)
        if primary_failure:
            assert str(cleanup_error) in "\n".join(original.__notes__)
        assert release.is_set() and all(not thread.is_alive() for thread in threads)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        sender.close.assert_called_once_with()
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()


@isolated_ticket_test
def _noncooperative_probe(identity: Path, helper: str) -> None:
    import os
    identity.write_text(str(os.getpid()))
    ready, never_released = Event(), Event()

    def unresponsive() -> None:
        ready.set()
        never_released.wait(60)

    if helper == "ticket":
        with TicketExecution(unresponsive, release=lambda: None,
                             name="noncooperative-test", cleanup_timeout=0):
            assert ready.wait(5)
            raise AssertionError("original isolated failure")
    else:
        import support.background_delivery as delivery
        control, process, sender = Mock(), Mock(), Mock()
        control.load.return_value = {"executor": {"action_id": "a", "generation": 1}}
        process.poll.return_value = 0
        original_join = Thread.join
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(delivery, "_bind_running_executor", lambda *_: (control, Mock(), process))
            patch.setattr(Thread, "join", lambda self, timeout=None: original_join(self, 0))
            with background_delivery(Mock(), "run", unresponsive, sender, []):
                assert ready.wait(5)
                raise AssertionError("original isolated failure")


@pytest.mark.parametrize("helper", ["ticket", "background"])
def test_noncooperative_test_cannot_outlive_fixture_process(tmp_path: Path, helper: str) -> None:
    import subprocess
    import sys
    identity = tmp_path / "isolated-test.pid"
    unrelated = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
    try:
        with pytest.raises(pytest.fail.Exception, match="original isolated failure"):
            _noncooperative_probe(identity, helper)
        assert not Path(f"/proc/{int(identity.read_text())}").exists()
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


@isolated_ticket_test
def _incomplete_success_probe(identity: Path, resource: str) -> None:
    import os
    import subprocess
    import sys
    if resource == "thread":
        identity.write_text(str(os.getpid()))
        Thread(target=lambda: Event().wait(60), name="leaked-ticket-thread").start()
    elif resource == "thread-error":
        identity.write_text(str(os.getpid()))
        def broken() -> None:
            raise ValueError("controlled background failure")
        worker = Thread(target=broken)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
    else:
        child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
        identity.write_text(str(child.pid))


@pytest.mark.parametrize("resource", ["thread", "process", "thread-error"])
def test_successful_return_does_not_hide_live_resources(tmp_path: Path, resource: str) -> None:
    identity = tmp_path / "leaked.pid"
    expected = {"thread": "live threads", "process": "live process",
                "thread-error": "controlled background failure"}[resource]
    with pytest.raises(pytest.fail.Exception, match=expected):
        _incomplete_success_probe(identity, resource)
    assert not Path(f"/proc/{int(identity.read_text())}").exists()
