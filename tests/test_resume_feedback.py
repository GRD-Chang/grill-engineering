"""Outcome facts, independent of translated cards or command exit status."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.notifications import Notifications
from agent_run.resume_feedback import ResumeFeedback


@pytest.mark.parametrize("status,kind,expected", [
    ("active", "progress", "started"),
    ("waiting_external", "external_wait", "started"),
    ("completed", "terminal_completion", "started"),
    ("ready_for_human", "human_gate", "human_action"),
    ("execution_failed", "execution_failure", "failed"),
])
def test_resume_outcome_requires_actual_controller_result(tmp_path: Path, status: str,
                                                         kind: str, expected: str) -> None:
    state: dict[str, Any] = {"run_id": "run", "status": "execution_failed"}
    feedback = ResumeFeedback(tmp_path, state, lambda: state)
    feedback.observe(deepcopy(state))
    assert feedback.result is None  # Old failure and received request prove nothing.
    feedback.progressed({**state, "status": status}, kind)
    assert feedback.result is not None
    assert feedback.result["outcome"] == expected


def test_invocation_preparation_is_not_actual_resume_start(tmp_path: Path) -> None:
    state: dict[str, Any] = {"run_id": "run", "status": "active"}
    feedback = ResumeFeedback(tmp_path, state, lambda: state)
    invocation = {"status": "running", "started_at": "new", "requested_thread_id": "old-thread"}
    feedback.observe({**state, "active_agent_invocation": invocation})
    assert feedback.result is None
    feedback.observe({**state, "active_agent_invocation": {**invocation, "reported_thread_id": "old-thread"}})
    assert feedback.result is not None
    assert feedback.result["evidence"] == "worker_started"


def test_reused_old_running_invocation_is_not_resume_progress(tmp_path: Path) -> None:
    state: dict[str, Any] = {"run_id": "run", "status": "active", "active_agent_invocation": {
        "status": "running", "started_at": "old", "reported_thread_id": "thread",
    }}
    feedback = ResumeFeedback(tmp_path, state, lambda: state)
    feedback.observe(deepcopy(state))
    assert feedback.result is None


def test_immediate_human_boundary_takes_priority_over_start(tmp_path: Path) -> None:
    state: dict[str, Any] = {"run_id": "run", "status": "active"}
    feedback = ResumeFeedback(tmp_path, state, lambda: state)
    feedback.progressed({**state, "status": "ready_for_human"}, "progress")
    assert feedback.result is not None
    assert feedback.result["outcome"] == "human_action"


def test_attached_frontend_does_not_open_sender(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, Any] = {"run_id": "run", "status": "active"}
    feedback = ResumeFeedback(tmp_path, state, lambda: state)
    feedback.failure("late frontend error")
    feedback.suppressed = True
    monkeypatch.setattr(Notifications, "__init__", lambda *args: pytest.fail("attached Resume sent a notification"))
    feedback.close()
