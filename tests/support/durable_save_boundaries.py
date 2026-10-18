"""原 15 点的落盘事实；不构造历史，也不把保存后异常当作进程死亡。"""
from __future__ import annotations

from typing import Any


def assert_durable_ticket_boundary(state: dict[str, Any], number: int, run_id: str) -> None:
    """在辅助入口记录 execution_failed 之前检查独立重读的真实提交。"""
    assert state["run_id"] == run_id
    assert state["status"] == "active"
    job = state["ticket_jobs"]["2"]
    queue = state["ticket_work_queue"]
    invocation = state.get("active_agent_invocation")
    assert job["ticket_number"] == 2
    assert job.get("candidate_sha") is None
    assert job.get("acceptance_artifact") is None

    if number == 1:
        assert queue == []
    else:
        assert len(queue) == 1
        assert {key: queue[0][key] for key in ("ticket_number", "generation", "role")} == {
            "ticket_number": 2, "generation": 1, "role": "development",
        }
        assert bool(queue[0]["lease_id"]) is (number >= 7)

    if number <= 2:
        assert "phase" not in job
        assert "review_budget" not in job
    else:
        assert job["phase"] == "developing"
        assert job["ticket_branch"] == f"agent-run/{run_id}/ticket-2"
        assert job["base_sha"]
        assert job["effective_revision"]
        assert job["review_budget"]["window"] == 1
        assert job["review_budget"]["development_attempts"] == int(number >= 8)
        assert job["review_budget"]["reviewer_invocations"] == 0
        assert bool(job.get("ticket_branch_initialized")) is (number >= 6)
    if number == 4:
        intent = job["ticket_write_intent"]
        assert intent["action"] == "ensure_change_branch"
        assert intent["authority"]["branch"] == job["ticket_branch"]
        assert intent["authority"]["base_sha"] == job["base_sha"]
    else:
        assert "ticket_write_intent" not in job

    if number >= 8:
        attempt = job["pending_semantic_attempt"]
        assert {key: attempt[key] for key in (
            "work_subject", "generation", "role", "ordinal", "budget_window", "status",
        )} == {
            "work_subject": "ticket:2", "generation": 1, "role": "development",
            "ordinal": 1, "budget_window": 1, "status": "pending",
        }
        assert ("returned_result" in attempt) is (number == 15)
        assert job["candidate_commit_intent"]
    else:
        assert "pending_semantic_attempt" not in job

    if number < 10:
        assert invocation is None
        assert state["agent_invocation_history"] == []
        return
    assert invocation["work_subject"] == "ticket:2"
    assert invocation["role"] == "development"
    assert invocation["phase"] == "developing"
    assert invocation["status"] == ("completed" if number >= 14 else "running")
    assert invocation["semantic_attempt"]["attempt_id"] == attempt["attempt_id"]
    assert invocation.get("reported_thread_id") == ("developer-2" if number >= 12 else None)
    segments = invocation["execution_segments"]
    if number == 10:
        assert segments == []
    else:
        assert len(segments) == 1
        assert segments[0]["started_at"]
        assert bool(segments[0]["ended_at"]) is (number >= 13)
