"""Resume routing across shared operator facts and parallel Ticket results."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.notification_events import events
from agent_run.notification_resume import apply_resume_result, ticket_resume_results
from agent_run.resume_feedback import ResumeFeedback


def _state() -> dict[str, Any]:
    return {
        "run_id": "run", "repository": "example/project", "language": "en",
        "parent": {"number": 1}, "status": "active", "delivery_type": "ticket_run",
        "ticket_jobs": {
            str(number): {"ticket_number": number, "ticket_branch_generation": 1, "phase": "developing"}
            for number in (2, 3)
        },
    }


def _receipt(state: dict[str, Any], number: int) -> None:
    state["ticket_jobs"][str(number)]["last_ticket_resume"] = {
        "executor_action_id": f"resume-{number}", "job_generation": 1,
        "accepted_at": "2026-09-01T01:00:00+00:00",
    }


def test_sibling_progress_does_not_repeat_an_unchanged_target_gate(tmp_path: Path) -> None:
    state = _state()
    state["ticket_jobs"]["2"].update(phase="blocked", blocked_reason="reviewer_requires_human")
    feedback = ResumeFeedback(tmp_path, state, lambda: state, ticket_number=2)
    observed = deepcopy(state)
    observed["ticket_jobs"]["3"]["phase"] = "reviewing"
    feedback.progressed(observed, "progress")
    assert feedback.result is None


@pytest.mark.parametrize("receipt", [False, True])
def test_resume_uses_shared_contradiction_classification(tmp_path: Path, receipt: bool) -> None:
    state = _state()
    feedback = ResumeFeedback(tmp_path, state, lambda: state, ticket_number=2)
    if receipt:
        _receipt(state, 2)
    state["ticket_jobs"]["2"].update(phase="blocked", blocked_reason="published_head_mismatch")
    feedback.observe(state)
    result = ticket_resume_results(state)[0] if receipt else feedback.result
    assert result is not None
    assert (result["outcome"], result["action_kind"], result["work_subject"]) == (
        "failed", "deterministic_contradiction", "ticket:2",
    )


@pytest.mark.parametrize("receipt", [False, True])
def test_resume_keeps_local_pause_when_same_ticket_has_global_failure(tmp_path: Path, receipt: bool) -> None:
    state = _state()
    state["ticket_jobs"]["2"].update(
        phase="blocked", blocked_reason="reviewer_requires_human", human_blockers=["Local reviewer request"],
    )
    if receipt:
        _receipt(state, 2)
    feedback = ResumeFeedback(tmp_path, state, lambda: state, ticket_number=2)
    state.update(status="execution_failed", diagnostics=[{
        "message": "Runner credential refresh failed", "operator_gate": {
            "work_subject": "ticket:2", "action_kind": "execution_failure",
            "phase": "reviewing", "reason": "runner_credential_failed",
        },
    }])
    feedback.observe(state)
    projected = events(state if receipt else feedback.project(state))
    boundaries = [event for event in projected if event["kind"] == "boundary"]
    assert len(boundaries) == 2
    local, = [event for event in boundaries if event["scope"] == "local"]
    failure, = [event for event in boundaries if event["scope"] == "global"]
    assert "resume_id" not in local
    assert "Local reviewer request" in local["summary"]
    assert local["next_step"].endswith("--ticket 2")
    assert failure["resume_outcome"] == "failed"
    assert "--ticket" not in failure["next_step"]
    assert not any(event["kind"] == "resume" for event in projected)


def test_receipts_preserve_each_ticket_boundary_and_original_reason() -> None:
    state = _state()
    for number in (2, 3):
        _receipt(state, number)
        state["ticket_jobs"][str(number)].update(
            phase="blocked", blocked_reason="reviewer_requires_human",
            human_blockers=[f"Original reviewer request for {number}"],
        )
    before = deepcopy(state)
    boundaries = [event for event in events(state) if event["kind"] == "boundary"]
    assert len(boundaries) == 2
    assert len({event["id"] for event in boundaries}) == 2
    for event in boundaries:
        number = event["task_number"]
        assert event["resume_outcome"] == "human_action"
        assert event["work_subject"] == f"ticket:{number}"
        assert event["next_step"].endswith(f"--ticket {number}")
        assert f"Original reviewer request for {number}" in event["summary"]
    assert state == before


@pytest.mark.parametrize("explicit_subject", [False, True])
def test_resume_deduplication_preserves_sibling_round_failure(explicit_subject: bool) -> None:
    state = _state()
    state["ticket_jobs"]["2"].update(phase="blocked", blocked_reason="ticket_execution_failed")
    boundary, = [event for event in events(state) if event["kind"] == "boundary"]
    stages = [{"id": f"failed-{number}", "kind": "stage_end", "status_code": "execution_failed",
               "work_subject": f"ticket:{number}"} for number in (2, 3)]
    state["_manual_resume_result"] = {"id": "manual", "outcome": "failed", "evidence": "execution"}
    if explicit_subject:
        state["_manual_resume_result"]["work_subject"] = "ticket:2"
    projected = apply_resume_result(state, [*stages, boundary])
    assert stages[1] in projected
    assert stages[0] not in projected
    updated, = [event for event in projected if event["kind"] == "boundary"]
    assert updated["id"] == boundary["id"]
    assert updated["supersedes"] == ["failed-2"]
    assert updated["resume_outcome"] == "failed"


def test_ambiguous_resume_does_not_take_a_ticket_boundary() -> None:
    state = _state()
    for job in state["ticket_jobs"].values():
        job.update(phase="blocked", blocked_reason="ticket_execution_failed")
    boundaries = [event for event in events(state) if event["kind"] == "boundary"]
    state["_manual_resume_result"] = {"id": "manual", "outcome": "failed", "evidence": "preflight"}
    projected = apply_resume_result(state, boundaries)
    assert projected[:2] == boundaries
    assert projected[2]["kind"] == "resume"
    assert projected[2]["work_subject"] is None
