from __future__ import annotations

import pytest

from agent_run.delivery_progress import history_progress_view
from agent_run.resume_audit import (
    append_explicit_resume_audit,
    bind_resume_to_successor,
)
from agent_run.semantic_attempt import allocate_semantic_attempt
from agent_run.state_contract import (
    IncompatibleRunStateError,
    _require_human_response_audit,
)


def test_resume_audit_keeps_every_bounded_fact_without_a_resume_limit() -> None:
    state = {
        "run_id": "run-1",
        "status": "supervision_timeout",
        "diagnostics": [
            {
                "code": "supervision_timeout",
                "message": "private diagnostic content must not be copied",
            }
        ],
    }

    for _ in range(129):
        append_explicit_resume_audit(
            state,
            new_thread=False,
            human_response_supplied=False,
        )

    audit = state["resume_audit"]
    history = audit["history"]
    assert audit["total"] == 129
    assert audit["compacted"] == 0
    assert len(history) == 129
    assert history[0]["sequence"] == 1
    assert history[-1]["sequence"] == 129
    assert history[-1]["failure_code"] == "supervision_timeout"
    assert "private diagnostic content" not in str(audit)


def test_successor_binding_keeps_the_persisted_resume_identity() -> None:
    owner: dict[str, object] = {}
    attempt = allocate_semantic_attempt(
        owner,
        role="development",
        work_subject="ticket:2",
        generation=1,
        currentness_boundary={"base_sha": "base-1"},
        ordinal=1,
        budget_window=1,
    )
    invocation = {
        "status": "failed",
        "work_subject": "ticket:2",
        "generation": 1,
        "started_at": "2026-08-24T00:00:00+00:00",
        "semantic_attempt": attempt,
    }
    state = {
        "run_id": "run-1",
        "status": "execution_failed",
        "active_ticket_job": owner,
        "active_agent_invocation": invocation,
        "diagnostics": [{"code": "agent_invocation_failed"}],
    }
    event = append_explicit_resume_audit(
        state,
        new_thread=False,
        human_response_supplied=False,
    )

    bound = bind_resume_to_successor(
        state,
        semantic_attempt=attempt,
        successor_started_at="2026-08-24T00:01:00+00:00",
    )

    assert bound == (event["resume_id"], 1)
    assert state["resume_audit"]["history"][0]["resume_id"] == event["resume_id"]


def test_human_response_audit_survives_job_generation_cleanup() -> None:
    job = {
        "ticket_number": 2,
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_response_generation": 1,
        "human_response_history": [
            {
                "generation": 1,
                "human_blockers": ["Need a decision."],
                "response": "Use the bounded option.",
            }
        ],
    }
    attempt = allocate_semantic_attempt(
        job,
        role="development",
        work_subject="ticket:2",
        generation=1,
        currentness_boundary={"base_sha": "base-1"},
        ordinal=1,
        budget_window=1,
    )
    state = {
        "run_id": "run-1-1234567890abcdef",
        "status": "ready_for_human",
        "active_ticket_job": job,
        "ticket_jobs": {"2": job},
        "active_agent_invocation": {
            "status": "completed",
            "work_subject": "ticket:2",
            "generation": 1,
            "semantic_attempt": attempt,
        },
        "diagnostics": [],
    }

    event = append_explicit_resume_audit(
        state,
        new_thread=False,
        human_response_supplied=True,
    )
    job.pop("human_response_history")

    progress = history_progress_view(
        state,
        {
            "timeline": [],
            "agent_invocations": [],
            "agent_resumes": [event],
            "semantic_agent_attempts": [],
        },
    )

    assert progress["events"][0]["details"] == ["Use the bounded option."]


@pytest.mark.parametrize(
    "response_audit",
    [
        {},
        {"resume-with-response": "Use the bounded option.", "unexpected": "extra"},
    ],
)
def test_human_response_audit_requires_exact_resume_coverage(
    response_audit: dict[str, str],
) -> None:
    state = {
        "human_response_audit_protocol": 1,
        "resume_audit": {
            "history": [
                {
                    "resume_id": "resume-with-response",
                    "human_response_supplied": True,
                },
                {
                    "resume_id": "resume-without-response",
                    "human_response_supplied": False,
                },
            ]
        },
        "human_response_audit": response_audit,
    }

    with pytest.raises(
        IncompatibleRunStateError,
        match="incomplete Human Response audit facts",
    ):
        _require_human_response_audit(state)


def test_legacy_state_without_human_response_audit_remains_readable() -> None:
    state = {
        "resume_audit": {
            "history": [
                {
                    "resume_id": "legacy-resume",
                    "human_response_supplied": True,
                }
            ]
        }
    }

    _require_human_response_audit(state)


def test_current_response_audit_protocol_requires_the_audit_mapping() -> None:
    state = {
        "human_response_audit_protocol": 1,
        "resume_audit": {"history": []},
    }

    with pytest.raises(
        IncompatibleRunStateError,
        match="missing canonical human_response_audit",
    ):
        _require_human_response_audit(state)
