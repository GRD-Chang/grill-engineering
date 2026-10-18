"""真实交付测试的线程生命周期；占位进程仅代表 CLI 可见的所有权。"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen
import sys
from threading import Event, Thread
from typing import Any

from agent_run.executor_admission import close_executor_admission, executor_admission_scope
from agent_run.notifications import Notifications
from agent_run.task_control import TaskControlStore, TaskKey
from support.control import _bind_running_executor


@dataclass
class BackgroundDelivery:
    control: TaskControlStore
    task: TaskKey
    ownership: dict[str, Any]
    ownership_process: Popen[bytes]
    thread: Thread
    returned: Event
    errors: list[BaseException]


@contextmanager
def background_delivery(
    repo: Path, run_id: str, deliver: Callable[[], object],
    sender: Notifications, gates: Iterable[Event],
) -> Iterator[BackgroundDelivery]:
    """失败时先关闭真实执行的准入并回收线程，最后关闭依赖资源。"""
    worker = None
    thread = None
    cleanup_errors: list[BaseException] = []
    try:
        control, task, worker = _bind_running_executor(repo, run_id)
        ownership = control.load(task)
        assert ownership is not None
        executor = ownership["executor"]
        returned = Event()
        errors: list[BaseException] = []

        def close(error: BaseException | None) -> None:
            control.close_ticket_resume_admission(
                task, action_id=executor["action_id"], generation=executor["generation"],
                run_id=run_id, can_exit=lambda: True,
                failure=str(error) if error is not None else None,
            )

        def run() -> None:
            try:
                deliver()
            except BaseException as error:
                errors.append(error)
            finally:
                returned.set()

        with executor_admission_scope(close):
            # 调度器的 Worker 复制此上下文，测试清理共享同一个取消信号。
            context = copy_context()
            thread = Thread(target=context.run, args=(run,), name="active-ticket-resume-test")
            try:
                thread.start()
                yield BackgroundDelivery(control, task, ownership, worker, thread, returned, errors)
            finally:
                try:
                    close_executor_admission(sys.exception())
                except BaseException as error:
                    cleanup_errors.append(error)
                finally:
                    for gate in gates:
                        gate.set()
                    if thread.ident is not None:
                        try:
                            thread.join(15)
                        except BaseException as error:
                            cleanup_errors.append(error)
                        if thread.is_alive():
                            cleanup_errors.append(AssertionError(f"交付取消后未退出：{errors!r}"))
    finally:
        original_error = sys.exception()
        try:
            if worker is not None:
                if worker.poll() is None:
                    try:
                        worker.terminate()
                    except BaseException as error:
                        cleanup_errors.append(error)
                try:
                    worker.wait(timeout=5)
                except BaseException as error:
                    cleanup_errors.append(error)
        finally:
            # 线程仍存活时不能关闭它还在使用的通知依赖。
            if thread is None or not thread.is_alive():
                try:
                    sender.close()
                except BaseException as error:
                    cleanup_errors.append(error)
        if cleanup_errors:
            if original_error is None:
                raise cleanup_errors[0]
            for error in cleanup_errors:
                original_error.add_note(f"Background delivery cleanup failed: {error!r}")
