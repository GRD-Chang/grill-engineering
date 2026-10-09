"""持久返回损坏时关闭准入；完成后不在 Attempt 历史复制产物。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from agent_run.agent_return import clear_blocked_agent_return, store_agent_return
from agent_run.agents import DevelopmentResult, HumanBlockerResult
from agent_run.semantic_attempt import (
    allocate_semantic_attempt, close_semantic_attempt, require_semantic_attempt_record,
)
from agent_run.work_facts import work_role


def _work() -> tuple[dict[str, Any], dict[str, Any]]:
    owner: dict[str, Any] = {"phase": "developing"}
    attempt = allocate_semantic_attempt(
        owner, role="development", work_subject="ticket:3", generation=1,
        currentness_boundary={"base_sha": "base"}, ordinal=1,
    )
    attempt["returned_result"] = store_agent_return(DevelopmentResult("developer", "完成修复"), attempt)
    return owner, attempt


@pytest.mark.parametrize("damage", ["kind", "thread", "summary", "extra", "role"])
def test_attempt_rejects_damaged_durable_agent_return(damage: str) -> None:
    owner, attempt = _work()
    result = attempt["returned_result"]
    if damage == "kind":
        result["kind"] = "publication"
    elif damage == "thread":
        result["thread_id"] = ""
    elif damage == "summary":
        result["summary"] = None
    elif damage == "extra":
        result["untrusted"] = True
    else:
        attempt["returned_result"] = store_agent_return(
            HumanBlockerResult("developer", ("需要权限",)), attempt,
        )
        result = attempt["returned_result"]
        result["kind"] = "review"
    with pytest.raises(ValueError):
        require_semantic_attempt_record(attempt, expected_status="pending")


def test_saved_success_is_deterministic_continuation_and_not_cleared_by_resume() -> None:
    owner, attempt = _work()
    saved = deepcopy(attempt)
    assert work_role(owner) is None
    clear_blocked_agent_return(owner)
    assert attempt == saved
    close_semantic_attempt(owner, attempt, outcome="candidate", result={"development_summary": "完成修复"})
    assert "returned_result" not in owner["semantic_attempt_history"][0]
    assert owner["semantic_attempt_history"][0]["development_summary"] == "完成修复"


def test_explicit_human_continuation_clears_only_blocked_return() -> None:
    owner, attempt = _work()
    attempt["returned_result"] = store_agent_return(HumanBlockerResult("developer", ("需要权限",)), attempt)
    assert work_role(owner) == "development"
    identity = attempt["attempt_id"]
    clear_blocked_agent_return(owner)
    assert "returned_result" not in attempt
    assert attempt["attempt_id"] == identity
