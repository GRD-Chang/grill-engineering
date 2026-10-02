from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from agent_run.delivery_history import history_records as presented_history_records
from agent_run.history_facts import history_records
from agent_run.messages import presentation_language


@pytest.mark.parametrize("render", [history_records, presented_history_records])
def test_invocation_identity_backfill_does_not_mutate_persisted_attempt(render: Any) -> None:
    attempt = {"attempt_id": "attempt-1", "status": "completed", "outcome": "candidate"}
    audit = {
        "semantic_agent_attempts": [attempt],
        "agent_invocations": [{
            "semantic_attempt": {
                "attempt_id": "attempt-1", "role": "development",
                "work_subject": "ticket:3", "generation": 1, "ordinal": 1,
            },
            "started_at": "2026-09-21T00:00:00+00:00",
            "reported_thread_id": "actual-thread",
            "ended_at": "2026-09-21T00:01:00+00:00",
            "execution_segments": [{"started_at": "2026-09-21T00:00:00+00:00",
                                    "ended_at": "2026-09-21T00:01:00+00:00"}],
        }],
    }
    state = {"run_id": "run", "ticket_jobs": {"3": {"semantic_attempt_history": [attempt]}}}
    before = deepcopy((state, audit))

    record = render(state, audit)[0]

    assert record["attempt"]["role"] == "development"
    assert record["work_subject"] == "ticket:3"
    assert record["status_code"] == "candidate"
    assert record["execution_seconds"] == 60
    assert (state, audit) == before


def test_semantic_history_is_independent_of_display_language() -> None:
    audit = {"semantic_agent_attempts": [{
        "attempt_id": "attempt-1", "role": "reviewer", "work_subject": "ticket:3",
        "status": "completed", "outcome": "acceptance_artifact",
        "acceptance_artifact": {"checks": {"spec": {"status": "fail", "findings": ["原始 finding"]}}},
    }]}
    with presentation_language("zh"):
        chinese = history_records({}, audit)
    with presentation_language("en"):
        english = history_records({}, audit)
    assert chinese == english
    assert chinese[0]["status_code"] == "review_failed"
    assert chinese[0]["findings"] == ["原始 finding"]
    assert "status_text" not in chinese[0]
