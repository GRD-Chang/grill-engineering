"""Small notification projection scenarios; no transport or persistent fixtures."""
from __future__ import annotations

from typing import Any


def state_with_round(role: str = "development", outcome: str = "candidate") -> dict[str, Any]:
    attempt = {"attempt_id": "a1", "role": role, "work_subject": "run:r",
               "ordinal": 1, "generation": 1, "status": "completed", "outcome": outcome,
               "development_summary": "已实现输入校验"}
    return {"language": "zh", "run_id": "r", "repository": "o/repo", "parent": {"number": 241, "title": "飞书通知"},
            "status": "run_review_pending", "semantic_agent_attempts": [attempt],
            "agent_invocation_history": [{"semantic_attempt": attempt.copy(), "status": "completed", "reported_thread_id": "thread-1",
                                          "started_at": "2026-01-01T00:00:00+00:00",
                                          "ended_at": "2026-01-01T00:00:09+00:00"}]}

