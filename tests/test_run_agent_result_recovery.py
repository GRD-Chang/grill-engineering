"""Run 角色返回后的持久化中断经公开 run 消费原成果。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run import cli
from agent_run.agents import ReviewResult
from agent_run.executor_host import FakeExecutorHost
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.inprocess_cli import invoke_cli_inprocess
from support.run_artifacts import _passing_artifact
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run, _completed_run
from support.seeding import seed_idle_control
from support.workspace import prepare_workspace


@pytest.mark.parametrize("role", ["reviewer", "publication"])
def test_public_run_consumes_durable_run_agent_return_after_save_interruption(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    if role == "reviewer":
        state, states, git = _completed_run(repo, state_root=workspace.state_root)
        publisher = FixtureGitHubPublisher(repo / "github.json", git)
    else:
        state, states, git, publisher = _accepted_run(repo, state_root=workspace.state_root)
    run_id = state["run_id"]

    class Agents(RunPublicationAgents):
        reviews: list[dict[str, Any]] = []

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.reviews.append(request)
            return ReviewResult("run-review-return-thread", _passing_artifact())

    agents = Agents()
    original_save = states.save_run
    interrupted = False
    subject_key = "run_acceptance" if role == "reviewer" else "run_publication"

    def interrupt_after_save(identifier: str, current: dict[str, Any]) -> None:
        nonlocal interrupted
        original_save(identifier, current)
        attempt = current.get(subject_key, {}).get("pending_semantic_attempt", {})
        if not interrupted and "returned_result" in attempt:
            interrupted = True
            raise RuntimeError("interrupted after durable Agent return")

    monkeypatch.setattr(states, "save_run", interrupt_after_save)
    engine = (
        RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher)
        if role == "reviewer" else RunPublicationEngine(
            git=git, states=states, agents=agents, github=publisher,
            default_branch="main", default_head_sha=git.resolve("main"),
        )
    )
    with pytest.raises(RuntimeError, match="after durable Agent return"):
        (engine.accept if role == "reviewer" else engine.publish)(run_id)
    durable = StateStore(states.root).load_current_run(run_id)
    attempt = durable[subject_key]["pending_semantic_attempt"]
    returned = attempt["returned_result"]
    original_attempt_id = attempt["attempt_id"]
    assert len(agents.reviews if role == "reviewer" else agents.requests) == 1
    request = (agents.reviews if role == "reviewer" else agents.requests)[0]
    assert request["_prompt_resources"] == state["prompt_resources"]
    if role == "reviewer":
        assert request["run_id"] == run_id
        assert request["acceptance_scope"] == "run"
        assert request["ticket_completion_records"]
        assert request["review_context"]["facts"]["current_review_identity"]["run_head_sha"] == git.resolve(state["run_branch"])
    else:
        assert request["acceptance_artifact"] == state["run_acceptance"]["acceptance_artifact"]

    seed_idle_control(TaskControlStore(states.root), TaskKey(repo, state["repository"], 1), run_id)
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(cli, "_agent_backend", lambda *args, **kwargs: agents)
    result = invoke_cli_inprocess(
        git_repo, repo / "github.json", "run", "1",
        "--state-dir", str(states.root), "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    completed = StateStore(states.root).load_current_run(run_id)
    assert completed["status"] == "run_approval_pending"
    assert len(agents.reviews if role == "reviewer" else agents.requests) == 1
    closed = next(item for item in completed[subject_key]["semantic_attempt_history"]
                  if item["attempt_id"] == original_attempt_id)
    assert closed["status"] == "completed"
    if role == "reviewer":
        assert completed["run_acceptance"]["acceptance_record"]["artifact"] == returned["artifact"]
    else:
        for key in ("commit_message", "pr_title", "pr_body_markdown"):
            assert completed["run_publication"]["artifact"][key] == returned["artifact"][key]
    assert completed["run_acceptance"]["review_budget"]["reviewer_invocations"] == 1
    assert completed["run_publication"]["publication_attempts"] == 1
    live = json.loads((repo / "github.json").read_text())["delivery"]["pull_requests"]
    assert len(live) == 1
    assert live[0]["title"] == "feat(run): publish the completed delivery"


def test_public_resume_reuses_publication_return_after_remote_currentness_timeout(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.github import GitHubReadError
    from agent_run.external_supervision import ExternalSupervisor

    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    state, states, git, publisher = _accepted_run(repo, state_root=workspace.state_root)
    run_id = state["run_id"]

    class Reader(FixtureGitHubReader):
        unavailable = False

        def repository(self):
            if self.unavailable:
                raise GitHubReadError("github_timeout", "post-return authority read timed out")
            return super().repository()

    reader = Reader(repo / "github.json")

    class Agents(RunPublicationAgents):
        def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
            result = super().run_publication(request)
            result["_thread_id"] = "returned-run-publisher"
            reader.unavailable = True
            return result

    agents = Agents()
    paused = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
        currentness_reader=reader,
    ).publish(run_id)
    assert paused["run_publication"]["phase"] == "waiting_external"
    durable = StateStore(states.root).load_current_run(run_id)
    attempt_id = durable["run_publication"]["pending_semantic_attempt"]["attempt_id"]
    assert durable["run_publication"]["pending_semantic_attempt"]["returned_result"]["artifact"]["_thread_id"] == "returned-run-publisher"
    assert "artifact" not in durable["run_publication"]
    assert len(agents.requests) == 1
    clock = [0.0]
    supervisor = ExternalSupervisor(now=lambda: clock[0])
    window = supervisor.observe(paused)
    assert window is not None
    clock[0] = float(window["deadline"]) + 1
    assert not supervisor.before_retry(paused)
    assert paused["status"] == "supervision_timeout"
    states.save_run(run_id, paused)
    seed_idle_control(TaskControlStore(states.root), TaskKey(repo, state["repository"], 1), run_id)
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(cli, "_agent_backend", lambda *args, **kwargs: agents)
    resumed = invoke_cli_inprocess(
        git_repo, repo / "github.json", "resume", run_id, "--state-dir", str(states.root), "--json",
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    completed = StateStore(states.root).load_current_run(run_id)
    assert completed["status"] == "run_approval_pending"
    assert completed["run_publication"]["thread_id"] == "returned-run-publisher"
    assert completed["run_publication"]["semantic_attempt_history"][-1]["attempt_id"] == attempt_id
    assert completed["run_publication"]["publication_attempts"] == 1
    assert len(agents.requests) == 1
    live = json.loads((repo / "github.json").read_text())["delivery"]["pull_requests"]
    assert len(live) == 1
