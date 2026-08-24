from __future__ import annotations

from agent_run.resume_audit import (
    MAX_RESUME_AUDIT_EVENTS,
    append_explicit_resume_audit,
)


def test_resume_audit_compacts_without_creating_a_resume_limit() -> None:
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

    for _ in range(MAX_RESUME_AUDIT_EVENTS + 1):
        append_explicit_resume_audit(
            state,
            new_thread=False,
            human_response_supplied=False,
        )

    audit = state["resume_audit"]
    history = audit["history"]
    assert audit["total"] == MAX_RESUME_AUDIT_EVENTS + 1
    assert audit["compacted"] == 1
    assert len(history) == MAX_RESUME_AUDIT_EVENTS
    assert history[0]["sequence"] == 2
    assert history[-1]["sequence"] == MAX_RESUME_AUDIT_EVENTS + 1
    assert history[-1]["failure_code"] == "supervision_timeout"
    assert "private diagnostic content" not in str(audit)
