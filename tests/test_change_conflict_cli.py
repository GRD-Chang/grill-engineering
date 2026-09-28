"""Public query/resume boundaries for persisted Change Job conflict calls."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult
from agent_run.agent_profiles import AgentProfileStore, ProfiledAgentBackend
from agent_run.executor_host import FakeExecutorHost
from agent_run.run_driver import RunDriver
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.conflict_invocations import prepared_conflict, run_conflict
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import seed_idle_control
from support.workspace import prepare_workspace


def _job(state: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "ticket":
        return state["ticket_jobs"]["3"]
    if kind == "parent_only":
        return state["parent_job"]
    return state["run_acceptance"]["repair_job"]


def _query(repo: Path, store: StateStore, run_id: str, status: str) -> None:
    before = {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    for command in ("status", "history"):
        result = invoke_cli_inprocess(repo, None, command, run_id, "--state-dir", str(store.root), "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        if command == "history" or status != "completed":
            assert "integration_conflict" in result.stdout
            assert status in result.stdout
    assert {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("kind", ["ticket", "parent_only", "run_repair"])
def test_public_resume_preserves_conflict_attempt_and_queries(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, store, job, stage = prepared_conflict(repo, kind, state_root=workspace.state_root)
    job["policy_snapshot"] = dict(state["policy_snapshot"])
    stage.save(state)
    fixture = repo / "github.json"
    run_id = state["run_id"]
    seed_idle_control(TaskControlStore(store.root), TaskKey(repo, state["repository"], 1), run_id)
    profiles = AgentProfileStore(store.root)
    profiles.initialize(run_id)
    budget = deepcopy(job["review_budget"])
    ci_budget = deepcopy(job["ci_recovery"])
    candidate = job["candidate_sha"]
    preserved = repo / "conflict-work.txt"
    preserved.write_text("work already completed\n")

    def fail(request: dict[str, Any]) -> DevelopmentResult:
        event = request["_invocation_event"]
        event("started", requested_thread_id="conflict-thread", attempt_count=1)
        event("thread_started", reported_thread_id="conflict-thread")
        _query(repo, store, run_id, "running")
        event("failed", error="controlled conflict interruption", return_code=1)
        _query(repo, store, run_id, "failed")
        raise InterruptedError("controlled conflict interruption")

    stage.agents = ProfiledAgentBackend(SimpleNamespace(develop=fail), profiles, run_id=run_id)
    with pytest.raises(InterruptedError):
        run_conflict(stage, state, job, repo)
    attempt = deepcopy(job["pending_semantic_attempt"])
    binding = deepcopy(state["active_agent_invocation"]["thread_execution_binding"])
    assert binding["thread_id"] == "conflict-thread"
    acceptance = deepcopy(job.get("acceptance_record"))
    # Keep the public admission, command intent and Controller refresh real;
    # stop the Driver at dispatch so this matrix does not run full delivery.
    import agent_run.cli as cli
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(RunDriver, "advance", lambda self, current, **kwargs: current)
    result = invoke_cli_inprocess(repo, fixture, "resume", run_id, "--state-dir", str(store.root), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    resumed = StateStore(store.root).load_current_run(run_id)
    assert resumed is not None
    resumed_job = _job(resumed, kind)
    if kind == "ticket":
        resumed["active_ticket_job"] = resumed_job
    assert resumed["active_agent_invocation"]["status"] == "resuming"
    assert resumed["active_agent_invocation"]["thread_execution_binding"] == binding
    _query(repo, store, run_id, "resuming")
    assert resumed_job["pending_semantic_attempt"] == attempt
    assert resumed_job["candidate_sha"] == candidate
    assert resumed_job.get("acceptance_record") == acceptance
    assert resumed_job["review_budget"] == budget
    assert resumed_job["ci_recovery"] == ci_budget
    assert resumed_job["development_thread_id"] == "conflict-thread"
    assert preserved.read_text() == "work already completed\n"

    def complete(request: dict[str, Any]) -> DevelopmentResult:
        assert request["thread_id"] == "conflict-thread"
        assert request["_invocation_mode"] == "resume"
        event = request["_invocation_event"]
        event("started", requested_thread_id="conflict-thread", attempt_count=1)
        assert resumed["active_agent_invocation"]["semantic_attempt"] == attempt
        assert resumed["active_agent_invocation"]["thread_execution_binding"] == binding
        event("completed", reported_thread_id="conflict-thread")
        _query(repo, store, run_id, "completed")
        return DevelopmentResult(thread_id="conflict-thread", summary="Conflict resolved")

    stage.agents = ProfiledAgentBackend(SimpleNamespace(develop=complete), profiles, run_id=run_id)
    assert run_conflict(stage, resumed, resumed_job, repo)
    assert "pending_semantic_attempt" not in resumed_job
    assert resumed_job["review_budget"] == budget
    assert resumed_job["ci_recovery"] == ci_budget
    resumed_job.pop(stage.conflict_key)
    stage.save(resumed)
    _query(repo, store, run_id, "completed")
