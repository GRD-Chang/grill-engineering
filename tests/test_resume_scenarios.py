"""拒绝矩阵的有限前置仍具备真实工作现场、预算和匹配的终止回执。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.git import GitRepository
from agent_run.resume_intent import bind_resume_intent
from agent_run.resume_materials import require_resume_checkout
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey, action_receipt_matches
from support.resume import _prepare_terminal_resume_boundary
from support.seeding import write_fixture
from support.workspace import managed_repo, managed_state


@pytest.mark.parametrize("boundary", [
    "execution_failed", "ready_for_human", "operator_stopped",
])
def test_terminal_resume_scenario_preserves_its_real_work_and_control_identity(
    git_repo: Path, boundary: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    _prepare_terminal_resume_boundary(git_repo, fixture, boundary)

    states = StateStore(managed_state(git_repo))
    run_id, = states.list_run_ids()
    durable = states.load_run(run_id)
    assert durable is not None
    git = GitRepository(managed_repo(git_repo))
    task = TaskKey(git.root, "example/project", 1)
    control = TaskControlStore(states.root)
    record = control.load(task)
    assert record is not None
    action, executor = record["action"], record["executor"]
    assert action_receipt_matches(durable, action)
    assert action["application_observed"] is True
    assert action["status"] == ("failed" if boundary == "execution_failed" else "completed")
    assert executor["status"] == "exited"
    assert executor["run_id"] == record["run_id"] == run_id
    assert executor["generation"] == action["executor_generation"] == (
        durable["action_application_receipt"]["executor_generation"]
    )
    assert durable["status"] == boundary
    assert durable["terminal_kind"] == (
        "waiting_human" if boundary == "ready_for_human" else boundary
    )
    assert durable["resume_audit"]["total"] == 0

    # 恢复必须能通过真实现场核验；损坏 Control 不是绕过前置缺损的假阳性。
    require_resume_checkout(durable, states.root, git)
    intent = bind_resume_intent(durable)
    assert intent["authorization"] == (
        "human_response" if boundary == "ready_for_human" else "continue_existing_work"
    )
    job = durable["parent_job"]
    budget = job["review_budget"]
    assert budget["window"] == budget["development_attempts"] == 1
    assert budget["reviewer_invocations"] == (1 if boundary == "operator_stopped" else 0)
    assert job.get("approval_grant") is None
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    if boundary == "operator_stopped":
        assert durable["operator_stop"]["resume_status"] == "parent_approval_pending"
        assert job["phase"] == "ready_for_approval"
        assert git.resolve(f"{job['candidate_sha']}^{{tree}}") == (
            job["acceptance_record"]["reviewed_candidate_tree"]
        )
        assert len(delivery["pull_requests"]) == 1
    else:
        checkout = states.root / "worktrees" / run_id / "parent"
        assert git.checkout_head(checkout) == job["managed_checkout_head"]
        assert durable["active_agent_invocation"]["reported_thread_id"] == "parent-development"
        assert job["development_thread_id"] == (
            "parent-development" if boundary == "ready_for_human" else None
        )
        assert job["pending_semantic_attempt"]["role"] == "development"
        assert job["pending_semantic_attempt"]["status"] == "pending"
        assert delivery.get("pull_requests", []) == []
    assert delivery.get("normal_merges", []) == []
    assert delivery.get("mutations", []) == []
