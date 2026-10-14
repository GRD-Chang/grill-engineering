"""Parent 阶段重开消费已保存返回，日常提交不进入整 Run 保存桥。"""
from __future__ import annotations

from copy import deepcopy
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.codex import _thread_line_callback
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.git import GitRepository
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from support.delivery_agents import PassAgents
from support.run_agents import ScriptedRunAgents
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run, _completed_run
from support.seeding import write_fixture


def report_thread_from_reader(request: dict[str, Any], thread: str) -> None:
    errors: list[BaseException] = []
    consume = _thread_line_callback(
        expected=request.get("thread_id"),
        callback=lambda reported: request["_invocation_event"](
            "thread_started", reported_thread_id=reported, attempt_count=1,
        ),
    )

    def read_stdout() -> None:
        try:
            consume(json.dumps({"type": "thread.started", "thread_id": thread}))
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=read_stdout, name="agent-run-worker-stdout-test")
    reader.start()
    reader.join(timeout=5)
    assert not reader.is_alive(), "Invocation stdout callback did not finish"
    if errors:
        raise errors[0]


class RecordedRunAgents(ScriptedRunAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        request["_invocation_event"]("started", requested_thread_id=request.get("thread_id"))
        result = super().develop(request)
        RecordedParentAgents._completed(request, result.thread_id)
        return result

    def review(self, request: dict[str, Any]) -> ReviewResult:
        request["_invocation_event"]("started", requested_thread_id=request.get("thread_id"))
        result = super().review(request)
        RecordedParentAgents._completed(request, result.thread_id)
        return result


class RecordedRunPublicationAgents(RunPublicationAgents):
    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
        request["_invocation_event"]("started", requested_thread_id=request.get("thread_id"))
        result = super().run_publication(request)
        RecordedParentAgents._completed(request, "final-publication-thread")
        result["_thread_id"] = "final-publication-thread"
        return result


class RecordedParentAgents(PassAgents):
    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        request["_invocation_event"]("started", requested_thread_id=request.get("thread_id"))
        result = super().develop(request)
        self._completed(request, result.thread_id)
        return result

    def review(self, request: dict[str, Any]) -> ReviewResult:
        request["_invocation_event"]("started", requested_thread_id=request.get("thread_id"))
        result = super().review(request)
        self._completed(request, result.thread_id)
        return result

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        request["_invocation_event"]("started", requested_thread_id=request.get("thread_id"))
        result = super().publication(request)
        thread = str(request.get("thread_id") or "parent-publication-thread")
        self._completed(request, thread)
        return result

    @staticmethod
    def _completed(request: dict[str, Any], thread: str) -> None:
        event = request["_invocation_event"]
        report_thread_from_reader(request, thread)
        event("completed", reported_thread_id=thread, attempt_count=1)


@pytest.mark.parametrize("role", ["development", "reviewer", "publication"])
def test_parent_only_reopens_saved_return_without_repeating_work(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    initial, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = initial["run_id"]
    agents = RecordedParentAgents(states.root / "worktrees" / run_id / "parent")
    publisher = FixtureGitHubPublisher(fixture, git)
    saved_attempt: dict[str, Any] = {}

    def interrupt_return() -> None:
        current = StateStore(states.root).load_current_run(run_id)
        assert current is not None
        attempt = current.get("parent_job", {}).get("pending_semantic_attempt", {})
        if attempt.get("role") == role and "returned_result" in attempt:
            saved_attempt.update(attempt)
            raise RuntimeError("interrupted after durable Parent return")

    def reject_bridge(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ordinary Parent save entered whole-Run bridge")

    with monkeypatch.context() as interruption:
        interruption.setattr(states, "_after_run_saved", interrupt_return)
        interruption.setattr(states, "_save_run_unlocked", reject_bridge)
        with pytest.raises(RuntimeError, match="durable Parent return"):
            ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents).deliver(run_id)
    before = (len(agents.development_requests), agents.review_count, len(agents.publication_requests))
    reopened = StateStore(states.root)
    monkeypatch.setattr(reopened, "_save_run_unlocked", reject_bridge)
    engine = ParentDeliveryEngine(git=git, states=reopened, github=publisher, agents=agents)
    result = engine.deliver(run_id)
    assert result["parent_job"]["phase"] == "ready_for_approval"
    after = (len(agents.development_requests), agents.review_count, len(agents.publication_requests))
    index = ["development", "reviewer", "publication"].index(role)
    assert after[index] == before[index] == 1
    job = StateStore(states.root).load_current_run(run_id)["parent_job"]
    closed = next(record for record in job["semantic_attempt_history"]
                  if record["attempt_id"] == saved_attempt["attempt_id"])
    assert closed["status"] == "completed"
    assert job["review_budget"]["development_attempts"] == 1
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert len(publisher.data["delivery"]["pull_requests"]) == 1
    completed = engine.approve(run_id)
    assert completed["status"] == "completed"
    assert StateStore(states.root).load_current_run(run_id)["status"] == "completed"


@pytest.mark.parametrize("history_size", [0, 128])
def test_full_parent_acceptance_and_publication_use_object_commits_after_reload(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, history_size: int,
) -> None:
    initial, states, git = _completed_run(git_repo)
    run_id = initial["run_id"]
    ticket_before = initial["ticket_jobs"]["2"]
    publisher = FixtureGitHubPublisher(git_repo / "github.json", git)

    def reject_bridge(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("ordinary Parent save entered whole-Run bridge")

    monkeypatch.setattr(states, "_save_run_unlocked", reject_bridge)
    accepted = RunAcceptanceEngine(
        git=git, states=states, github=publisher, agents=RecordedRunAgents(),
    ).accept(run_id)
    assert accepted["run_acceptance"]["phase"] == "accepted"
    reopened = StateStore(states.root)
    monkeypatch.setattr(reopened, "_save_run_unlocked", reject_bridge)
    agents = RecordedRunPublicationAgents()
    engine = RunPublicationEngine(
        git=git, states=reopened, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    published = engine.publish(run_id)
    assert published["status"] == "run_approval_pending"
    repeated = engine.publish(run_id)
    assert repeated["run_publication"]["publication_attempts"] == 1
    assert len(agents.requests) == 1
    fixture_store = StateStore(states.root)
    expanded = fixture_store.load_current_run(run_id)
    assert expanded is not None
    retained_history = deepcopy(expanded["agent_invocation_history"])
    invocation = retained_history[-1]
    archive = []
    for index in range(history_size):
        prior = deepcopy(invocation)
        prior["started_at"] = f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00"
        prior["ended_at"] = prior["started_at"]
        archive.append(prior)
    expanded["agent_invocation_history"] = archive + expanded["agent_invocation_history"]
    fixture_store.save_run(run_id, expanded)
    # Reopen after historical growth: the public stage consumes only current
    # invocation facts while the full query retains every archived result.
    engine = RunPublicationEngine(
        git=git, states=reopened, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    completed = engine.approve(run_id)
    durable = StateStore(states.root).load_current_run(run_id)
    assert len(durable["agent_invocation_history"]) == history_size + len(retained_history)
    assert durable["agent_invocation_history"][:history_size] == archive
    assert completed["status"] == durable["status"] == "completed"
    assert durable["ticket_jobs"]["2"] == ticket_before
    assert "review_budget" not in durable["run_acceptance"].get("repair_job", {})
    assert len(publisher.data["delivery"]["pull_requests"]) == 1


def test_reader_callback_keeps_its_loaded_owner_after_other_thread_refresh(
    git_repo: Path,
) -> None:
    from agent_run.object_state import object_store
    from agent_run.state import ConcurrentRunStateError

    initial, states, _git, _publisher = _accepted_run(git_repo)
    run_id = initial["run_id"]
    stage_store = object_store(states, parent_scope=True)
    old = stage_store.load_current_run(run_id)
    assert old is not None
    # A controller replaces the Parent Acceptance generation after the reader
    # received the original work view. Refreshing the main thread's cache must
    # never turn that old reader view into an authorized result for the new Job.
    latest_store = StateStore(states.root)
    latest = latest_store.load_current_run(run_id)
    assert latest is not None
    from agent_run.run_currentness import invalidate_run_acceptance
    invalidate_run_acceptance(latest)
    latest.update(status="run_acceptance_pending", terminal_kind="run_acceptance_stale")
    latest_store.save_run(run_id, latest)
    stage_store.load_current_run(run_id)
    old["run_acceptance"]["phase"] = "reviewing"
    failures: list[BaseException] = []

    def reader_save() -> None:
        try:
            stage_store.save_run(run_id, old)
        except BaseException as error:
            failures.append(error)

    reader = threading.Thread(target=reader_save, name="agent-run-stale-reader-test")
    reader.start()
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ConcurrentRunStateError)
    durable = StateStore(states.root).load_current_run(run_id)
    assert durable["run_acceptance"] == latest["run_acceptance"]


def test_parent_retirement_keeps_archive_and_closed_pr_after_reopen(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    initial, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = initial["run_id"]
    publisher = FixtureGitHubPublisher(fixture, git)
    agents = RecordedParentAgents(states.root / "worktrees" / run_id / "parent")

    def reject_bridge(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Parent retirement entered the whole-Run save bridge")

    monkeypatch.setattr(states, "_save_run_unlocked", reject_bridge)
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    before = engine.deliver(run_id)
    original_job = deepcopy(before["parent_job"])
    pr_number = original_job["pr_number"]
    assert publisher.live_pull_request(pr_number)["state"] == "OPEN"

    engine.retire_for_child_flow(run_id)

    durable = StateStore(states.root).load_current_run(run_id)
    assert durable is not None
    assert "parent_job" not in durable
    assert durable["retired_parent_job"] == {
        **original_job, "phase": "abandoned_for_structure_change",
    }
    assert git.resolve(original_job["candidate_sha"]) == original_job["candidate_sha"]
    reopened_publisher = FixtureGitHubPublisher(fixture, git)
    assert reopened_publisher.live_pull_request(pr_number)["state"] == "CLOSED"
    closes = [mutation for mutation in reopened_publisher.data["delivery"]["mutations"]
              if mutation["action"] == "close_parent_pr"]
    assert closes == [{"action": "close_parent_pr", "pr_number": pr_number}]
    # Repeated retirement reads the committed absence of the current Parent;
    # it cannot close another PR or replace the retained original evidence.
    engine.retire_for_child_flow(run_id)
    assert StateStore(states.root).load_current_run(run_id) == durable
    assert FixtureGitHubPublisher(fixture, git).data["delivery"]["mutations"] == reopened_publisher.data["delivery"]["mutations"]
