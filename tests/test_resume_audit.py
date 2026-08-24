from __future__ import annotations

from agent_run.resume_audit import (
    append_explicit_resume_audit,
    bind_resume_to_successor,
)
from agent_run.semantic_attempt import allocate_semantic_attempt


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
