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
    invocation = {"status": "running", "started_at": "2026-09-01T01:00:00+00:00", "requested_thread_id": "old-thread"}
    feedback.observe({**state, "active_agent_invocation": invocation})
    assert feedback.result is None
    feedback.observe({**state, "active_agent_invocation": {**invocation, "reported_thread_id": "old-thread"}})
    assert feedback.result is not None
    assert feedback.result["evidence"] == "worker_started"


def test_reused_old_running_invocation_is_not_resume_progress(tmp_path: Path) -> None:
    state: dict[str, Any] = {"run_id": "run", "status": "active", "active_agent_invocation": {
        "status": "running", "started_at": "2026-09-01T00:00:00+00:00", "reported_thread_id": "thread",
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


def _parallel_state() -> dict[str, Any]:
    return {
        "run_id": "run", "repository": "example/project", "language": "en",
        "parent": {"number": 1}, "status": "active", "multi_ticket_protocol": 4,
        "delivery_type": "ticket_run", "ticket_graph": {"tickets": {"2": {}, "3": {}}},
        "ticket_jobs": {
            "2": {"ticket_number": 2, "ticket_branch_generation": 1, "phase": "reviewing",
                  "last_ticket_resume": {"accepted_at": "2026-09-01T01:00:00+00:00",
                                         "executor_action_id": "action", "job_generation": 1}},
            "3": {"ticket_number": 3, "ticket_branch_generation": 1, "phase": "developing"},
        },
    }


def test_targeted_feedback_ignores_sibling_progress_and_failure(tmp_path: Path) -> None:
    state = _parallel_state()
    feedback = ResumeFeedback(tmp_path, state, lambda: state, ticket_number=2)
    sibling = {"status": "running", "started_at": "2026-09-01T01:01:00+00:00",
               "reported_thread_id": "sibling", "work_subject": "ticket:3"}
    observed = {**state, "active_agent_invocation": sibling, "ticket_agent_invocations": {"3": sibling}}
    feedback.progressed(observed, "progress")
    assert feedback.result is None
    failed = deepcopy(observed)
    failed["status"] = "execution_failed"
    failed["ticket_jobs"]["3"].update(phase="blocked", blocked_reason="ticket_execution_failed")
    failed["diagnostics"] = [{"operator_gate": {
        "work_subject": "ticket:3", "action_kind": "execution_failure", "phase": "developing",
        "reason": "ticket_execution_failed",
    }}]
    feedback.observe(failed)
    assert feedback.result is None
    target = {**sibling, "work_subject": "ticket:2", "reported_thread_id": "target"}
    feedback.observe({**observed, "ticket_agent_invocations": {"2": target, "3": sibling}})
    assert feedback.result is not None
    assert feedback.result["outcome"] == "started"
    assert feedback.result["ticket_number"] == 2


@pytest.mark.parametrize("reason,outcome", [
    ("ticket_execution_failed", "failed"),
    ("reviewer_requires_human", "human_action"),
])
def test_noncurrent_ticket_resume_gate_reports_target(tmp_path: Path, reason: str, outcome: str) -> None:
    state = _parallel_state()
    feedback = ResumeFeedback(tmp_path, state, lambda: state, ticket_number=2)
    updated = deepcopy(state)
    updated["ticket_jobs"]["2"].update(phase="blocked", blocked_reason=reason)
    feedback.observe(updated)
    assert feedback.result is not None
    assert feedback.result["outcome"] == outcome
    assert feedback.result["ticket_number"] == 2


@pytest.mark.parametrize("thread,started", [(None, "2026-09-01T01:01:00+00:00"), (" ", "2026-09-01T01:01:00+00:00"), ("thread", "invalid")])
def test_resume_requires_proven_thread_and_timestamp(tmp_path: Path, thread: str | None, started: str) -> None:
    state = _parallel_state()
    feedback = ResumeFeedback(tmp_path, state, lambda: state)
    feedback.observe({**state, "active_agent_invocation": {
        "status": "running", "started_at": started, "reported_thread_id": thread,
    }})
    assert feedback.result is None


def test_active_ticket_receipt_needs_subsequent_target_work() -> None:
    from agent_run.notification_resume import apply_resume_result

    state = _parallel_state()
    invocation = {"status": "running", "started_at": "2026-09-01T01:01:00+00:00",
                  "work_subject": "ticket:3", "generation": 1, "reported_thread_id": "sibling"}
    state["ticket_agent_invocations"] = {"3": invocation}
    assert apply_resume_result(state, []) == []
    target = {**invocation, "work_subject": "ticket:2", "reported_thread_id": None}
    state["ticket_agent_invocations"]["2"] = target
    assert apply_resume_result(state, []) == []
    target["reported_thread_id"] = "target"
    events = apply_resume_result(state, [])
    assert len(events) == 1
    assert events[0]["resume_outcome"] == "started"
    assert events[0]["task_number"] == 2
    assert apply_resume_result(deepcopy(state), []) == events
    state["resume_audit"] = {"history": [{
        "work_subject": "ticket:2", "requested_at": "2026-09-01T01:02:00+00:00",
    }]}
    assert apply_resume_result(state, []) == [], "A newer manual Resume supersedes the old receipt"


@pytest.mark.parametrize("phase,reason,outcome", [
    ("blocked", "ticket_execution_failed", "failed"),
    ("blocked", "reviewer_requires_human", "human_action"),
    ("completed", None, "started"),
])
def test_active_ticket_result_does_not_absorb_a_sibling_boundary(phase: str, reason: str | None, outcome: str) -> None:
    from agent_run.notification_resume import apply_resume_result

    state = _parallel_state()
    state["ticket_jobs"]["2"].update(phase=phase, blocked_reason=reason)
    sibling = {"id": "sibling-gate", "kind": "boundary", "current": True,
               "task_number": 3, "status_code": "ready_for_human"}
    projected = apply_resume_result(state, [sibling])
    assert projected[0] == sibling
    result = projected[1]
    assert result["task_number"] == 2
    assert result["resume_outcome"] == outcome
    if phase == "completed":
        assert result["live"] is False
    else:
        assert result["next_step"].endswith("--ticket 2")
    state["ticket_jobs"]["2"].update(phase="reviewing", blocked_reason=None)
    assert apply_resume_result(state, [sibling]) == [sibling], "Resolved gates must not be replayed"


@pytest.mark.parametrize("status,outcome", [("requeue_required", "human_action"), ("execution_failed", "failed")])
def test_targeted_resume_reports_global_preflight_gate(tmp_path: Path, status: str, outcome: str) -> None:
    state = _parallel_state()
    feedback = ResumeFeedback(tmp_path, state, lambda: state, ticket_number=2)
    feedback.observe({**state, "status": status}, outcome_observed=True)
    assert feedback.result is not None
    assert feedback.result["outcome"] == outcome
    assert feedback.result["ticket_number"] is None


@pytest.mark.parametrize("phase", ["waiting_checks", "waiting_merge"])
def test_targeted_resume_reports_actual_controller_observation(tmp_path: Path, phase: str) -> None:
    from agent_run.notification_resume import ticket_resume_results

    state = _parallel_state()
    state["ticket_jobs"]["2"]["phase"] = phase
    feedback = ResumeFeedback(tmp_path, state, lambda: state, ticket_number=2)
    feedback.progressed(state, "external_wait")
    assert feedback.result is None
    assert ticket_resume_results(state) == []
    # Merely restoring the phase or reusing a pre-Resume read is not progress.
    state["ticket_jobs"]["2"]["publication_observed_at"] = "2026-09-01T00:59:00+00:00"
    assert ticket_resume_results(state) == []
    state["ticket_jobs"]["2"]["publication_observed_at"] = "2026-09-01T01:01:00+00:00"
    feedback.progressed(state, "external_wait")
    assert feedback.result is not None
    assert feedback.result["evidence"] == "controller_check"
    result = ticket_resume_results(state)
    assert len(result) == 1 and result[0]["evidence"] == "controller_check"
