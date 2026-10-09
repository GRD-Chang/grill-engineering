"""真实 Change 入口在 Agent 返回后中断，重载消费同一次结果。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, HumanBlockerResult, ReviewResult, PublicationResult
from agent_run import cli
from agent_run.controller import Controller
from agent_run.executor_host import FakeExecutorHost
from agent_run.task_control import TaskControlStore, TaskKey
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import seed_idle_control
from support.workspace import prepare_workspace
from support.run_artifacts import _passing_artifact
from support.run_publication import RunPublicationAgents
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.state import SimulatedProcessCrash, StateStore
from support.delivery_agents import PassAgents, issue
from support.seeding import write_fixture


class CrashBeforeResultConsumption(StateStore):
    def __init__(self, root: Path, subject: str, role: str) -> None:
        super().__init__(root)
        self.subject = subject
        self.role = role
        self.crashed = False

    def _save_run_unlocked(self, run_id: str, state: dict[str, Any]) -> None:
        job = state.get(self.subject)
        if isinstance(job, dict):
            attempt = job.get("pending_semantic_attempt")
            consumed = (
                job.get("phase") == "committing_candidate"
                if self.role == "development"
                else "pending_review_result" in job
                if self.role == "reviewer"
                else "pending_publication_result" in job
            )
            if (
                not self.crashed and consumed and isinstance(attempt, dict)
                and attempt.get("role") == self.role
                and isinstance(attempt.get("returned_result"), dict)
            ):
                self.crashed = True
                raise SimulatedProcessCrash("Agent returned before phase result save")
        super()._save_run_unlocked(run_id, state)


@pytest.mark.parametrize(
    ("parent_only", "role"),
    [(False, "development"), (False, "reviewer"), (False, "publication"),
     (True, "development")],
)
def test_change_resume_reuses_saved_agent_result(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, parent_only: bool, role: str,
) -> None:
    workspace = prepare_workspace(git_repo)
    git_repo = workspace.repository_root
    subject = "parent_job" if parent_only else "active_ticket_job"
    fixture = write_fixture(
        git_repo / "github.json", issues={} if parent_only else {"3": issue(3)},
    )
    states = CrashBeforeResultConsumption(workspace.state_root, subject, role)
    git = GitRepository(git_repo)
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    checkout = states.root / "worktrees" / run_id / ("parent" if parent_only else "ticket-3")
    class RecordedAgents(PassAgents):
        def _return_event(self, request: dict[str, Any], thread: str) -> None:
            event = request["_invocation_event"]
            event("started", requested_thread_id=request.get("thread_id"))
            event("thread_started", reported_thread_id=thread)
            event("completed")

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            result = super().develop(request)
            self._return_event(request, result.thread_id)
            return result

        def review(self, request: dict[str, Any]) -> ReviewResult:
            if request.get("acceptance_scope") == "run":
                result = ReviewResult("overall-reviewer", _passing_artifact())
                self._return_event(request, result.thread_id)
                return result
            result = super().review(request)
            self._return_event(request, result.thread_id)
            return result

        def publication(self, request: dict[str, Any]) -> PublicationResult:
            artifact = super().publication(request)
            thread = request.get("thread_id") or "returned-publisher"
            self._return_event(request, thread)
            return PublicationResult(thread, {"result_kind": "publication", **artifact, "human_blockers": None})

        def run_publication(self, request: dict[str, Any]) -> dict[str, Any]:
            result = RunPublicationAgents().run_publication(request)
            thread = request.get("thread_id") or "overall-publisher"
            self._return_event(request, thread)
            return {**result, "_thread_id": thread}

    agents = RecordedAgents(checkout)
    engine_type = ParentDeliveryEngine if parent_only else TicketDeliveryEngine
    with pytest.raises(SimulatedProcessCrash, match="Agent returned"):
        engine_type(
            git=git, states=states, github=FixtureGitHubPublisher(fixture, git),
            agents=agents,
        ).deliver(run_id)
    restored = StateStore(states.root)
    interrupted = restored.load_current_run(run_id)
    assert interrupted is not None
    attempt = deepcopy(interrupted[subject]["pending_semantic_attempt"])
    assert attempt["role"] == role
    assert attempt["returned_result"]
    assert "pending_review_result" not in interrupted[subject]
    assert "pending_publication_result" not in interrupted[subject]
    controller = Controller(FixtureGitHubReader(fixture), GitRepository(git_repo), restored)
    assert controller.record_execution_failure(run_id, "interrupted phase result consumption")
    seed_idle_control(TaskControlStore(restored.root), TaskKey(git_repo, state["repository"], 1), run_id)
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(cli, "_agent_backend", lambda *args, **kwargs: agents)
    arguments = ["resume", run_id, "--state-dir", str(restored.root),
                 "--json", "--new-thread", "--message", "Continue saved result closeout"]
    response = invoke_cli_inprocess(git_repo, fixture, *arguments)
    assert response.returncode == 0, response.stdout + response.stderr
    completed = StateStore(restored.root).load_current_run(run_id)
    assert completed["status"] == ("parent_approval_pending" if parent_only else "run_approval_pending")
    assert len(agents.development_requests) == 1
    assert agents.review_count == 1
    assert len(agents.publication_requests) == 1
    job = completed["parent_job"] if parent_only else completed["ticket_jobs"]["3"]
    assert job["phase"] == ("ready_for_approval" if parent_only else "completed")
    closed = [item for item in job["semantic_attempt_history"] if item["role"] == role]
    assert len(closed) == 1
    assert closed[0]["attempt_id"] == attempt["attempt_id"]
    if role == "reviewer":
        assert closed[0]["review_context"] == attempt["review_context"]
    assert "returned_result" not in closed[0]
    assert job["modification_attempts"] == 1
    assert job["publication_attempts"] == 1
    final = restored.load_current_run(run_id)
    assert (final["parent_job"] if parent_only else final["ticket_jobs"]["3"])["phase"] == job["phase"]


def test_saved_human_blocker_resume_starts_new_thread_on_same_work(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    checkout = states.root / "worktrees" / run_id / "ticket-3"

    class Agents(PassAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult | HumanBlockerResult:
            if not self.development_requests:
                self.development_requests.append(request)
                return HumanBlockerResult("blocked-developer", ("Confirm available access",))
            assert request["thread_id"] is None
            assert request["_invocation_mode"] == "new-thread"
            assert request["prior_human_blockers"] == ["Confirm available access"]
            assert request["human_response_history"][-1]["response"] == "Access confirmed"
            return super().develop(request)

    agents = Agents(checkout)
    blocked = TicketDeliveryEngine(
        git=git, states=states, github=FixtureGitHubPublisher(fixture, git), agents=agents,
    ).deliver(run_id)
    assert blocked["status"] == "ready_for_human"
    old_attempt = deepcopy(blocked["active_ticket_job"]["pending_semantic_attempt"])
    assert old_attempt["returned_result"]["kind"] == "human_blocker"
    restored = StateStore(states.root)
    resumed, _ = Controller(FixtureGitHubReader(fixture), GitRepository(git_repo), restored).resume(
        run_id, resume_human_blocker=True, new_thread=True, human_response="Access confirmed",
    )
    resumed_attempt = resumed["active_ticket_job"]["pending_semantic_attempt"]
    assert resumed_attempt["attempt_id"] == old_attempt["attempt_id"]
    assert "returned_result" not in resumed_attempt
    resumed_git = GitRepository(git_repo)
    completed = TicketDeliveryEngine(
        git=resumed_git, states=restored,
        github=FixtureGitHubPublisher(fixture, resumed_git), agents=agents,
    ).deliver(run_id)
    assert completed["status"] == "ticket_completed", completed.get("diagnostics")
    assert len(agents.development_requests) == 2
    closed = next(
        item for item in completed["active_ticket_job"]["semantic_attempt_history"]
        if item["role"] == "development"
    )
    assert closed["attempt_id"] == old_attempt["attempt_id"]
    assert completed["active_ticket_job"]["modification_attempts"] == 1
