"""Cancellation stays bound to its exact Executor context.

The real shared-storage/Worker regression now continues through public resume
in test_shared_storage_recovery.py, using one independent Executor scenario.
"""
from __future__ import annotations

from contextvars import copy_context

import pytest

from agent_run.executor_admission import (
    check_executor_cancelled, close_executor_admission, executor_cancellation_scope,
)
from agent_run.task_control import TaskControlBusyError


def test_cancellation_is_scoped_to_the_exact_execution_context() -> None:
    with executor_cancellation_scope():
        older = copy_context()
    with executor_cancellation_scope():
        current = copy_context()
        close_executor_admission()
        with pytest.raises(TaskControlBusyError, match="Executor stopped"):
            current.run(check_executor_cancelled)
        older.run(check_executor_cancelled)
    check_executor_cancelled()
