"""Public Resume drives a real Parent conflict through snapshot closeout."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.agent_profiles import AgentProfileStore, ProfiledAgentBackend
from agent_run.controller import Controller
from agent_run.executor_host import FakeExecutorHost
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.state import StateStore
from agent_run.state_errors import MissingRunMaterialsError
from agent_run.task_control import TaskControlStore, TaskKey
from support.delivery_agents import ScriptedAgents
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import seed_idle_control, write_fixture
from support.workspace import prepare_workspace


@pytest.mark.parametrize("interruption", ["worker", "commit", "cli-commit", "wrong-message", "wrong-parents", "bad-delta"])
def test_public_resume_closes_parent_conflict_and_dispatches_integration_review(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    fixture = write_fixture(repo / "github.json", issues={})
    git = GitRepository(repo)
    states = StateStore(workspace.state_root)
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = state["run_id"]
    publisher = FixtureGitHubPublisher(fixture, git)
    checkout = states.root / "worktrees" / run_id / "parent"
    profiles = AgentProfileStore(states.root)
    profiles.initialize(run_id)

    def command(root: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

    collaborator = tmp_path / "parent-collaborator"
    command(repo, "worktree", "add", "--detach", str(collaborator), "HEAD")
    try:
        (collaborator / "delivered.txt").write_text("human contribution\n")
        command(collaborator, "add", "delivered.txt")
        command(collaborator, "commit", "-m", "human contribution")
        source = command(collaborator, "rev-parse", "HEAD")
    finally:
        command(repo, "worktree", "remove", "--force", str(collaborator))

    class ConflictAgents(ScriptedAgents):
        resume_conflict = False
        successor_seen = False
        integration_calls = 0

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("repair_source") != "integration":
                return super().develop(request)
            self.integration_calls += 1
            assert "acceptance_artifact" not in request
            assert "ci_evidence" not in request
            event = request["_invocation_event"]
            event("started", requested_thread_id="development-thread-1", attempt_count=1)
            event("thread_started", reported_thread_id="development-thread-1")
            current = StateStore(states.root).load_current_run(run_id)
            assert current is not None
            assert current["active_agent_invocation"]["phase"] == "integration_conflict"
            assert current["parent_job"]["repair_source"] == "acceptance"
            if not self.resume_conflict and interruption == "worker":
                (checkout / "preserved.txt").write_text("partial conflict work\n")
                event("failed", error="controlled Parent conflict interruption", return_code=1)
                raise RuntimeError("controlled Parent conflict interruption")
            if interruption == "worker":
                assert request["_invocation_mode"] == "resume"
                assert current["parent_job"]["pending_semantic_attempt"] == attempt
                assert current["active_agent_invocation"]["thread_execution_binding"] == binding
                assert current["parent_job"]["candidate_sha"] == candidate
                assert current["parent_job"]["review_budget"] == budget
                assert current["parent_job"]["acceptance_record"] == acceptance
                assert (checkout / "preserved.txt").read_text() == "partial conflict work\n"
            else:
                (checkout / "preserved.txt").write_text("partial conflict work\n")
            (checkout / "delivered.txt").write_text("first attempt\nrepair applied\nhuman contribution\n")
            event("completed", reported_thread_id="development-thread-1")
            assert StateStore(states.root).load_current_run(run_id) is not None
            return DevelopmentResult(thread_id="development-thread-1", summary="Resolved both contributions")

        def review(self, request: dict[str, Any]) -> ReviewResult:
            current = states.load_current_run(run_id)
            assert current is not None
            if self.resume_conflict:
                job = current["parent_job"]
                assert "parent_integration_conflict" not in job
                assert job["parent_integration"]["conflict_paths"] == ["delivered.txt"]
                assert git.is_ancestor(source, job["candidate_sha"])
                assert git.is_ancestor(candidate, job["candidate_sha"])
                assert job["previous_parent_acceptance_record"] == acceptance
                assert job["review_budget"] == budget
                assert job["ci_recovery"] == ci_budget
                assert any(item["attempt_id"] == attempt["attempt_id"] and item["outcome"] == "integration_conflict_resolved"
                           for item in job["semantic_attempt_history"])
                assert (checkout / "delivered.txt").read_text() == "first attempt\nrepair applied\nhuman contribution\n"
                self.successor_seen = True
                raise RuntimeError("stopped at integration review boundary")
            publisher.data["delivery"]["published_branches"][current["parent_job"]["parent_branch"]] = source
            publisher._save()
            return super().review(request)

    agents = ConflictAgents(checkout)
    profiled = ProfiledAgentBackend(agents, profiles, run_id=run_id)
    original_commit = git.commit_integration_resolution
    if interruption != "worker":
        def commit_then_interrupt(*args: Any, **kwargs: Any) -> str:
            original_commit(*args, **kwargs)
            raise RuntimeError("controlled Parent conflict commit interruption")
        monkeypatch.setattr(git, "commit_integration_resolution", commit_then_interrupt)
    with pytest.raises(RuntimeError, match="controlled Parent conflict") as interrupted:
        ParentDeliveryEngine(git=git, states=states, github=publisher, agents=profiled).deliver(run_id)
    if interruption == "cli-commit":
        # The production Executor records a closeout exception before exposing resume.
        assert Controller(FixtureGitHubReader(fixture), git, states).record_execution_failure(
            run_id, str(interrupted.value),
        )
    failed = StateStore(states.root).load_current_run(run_id)
    assert failed is not None
    job = failed["parent_job"]
    attempt = deepcopy(job["pending_semantic_attempt"])
    binding = (deepcopy(failed["active_agent_invocation"]["thread_execution_binding"])
               if interruption == "worker" else None)
    candidate = job["candidate_sha"]
    budget = deepcopy(job["review_budget"])
    ci_budget = deepcopy(job["ci_recovery"])
    acceptance = deepcopy(job["acceptance_record"])
    committed = git.checkout_head(checkout)
    integration_budget = deepcopy(job["integration_development_budget"])
    if interruption != "worker":
        monkeypatch.setattr(git, "commit_integration_resolution", original_commit)
        assert "ticket_number" not in job
        assert job["parent_integration_conflict"]["development_summary"] == "Resolved both contributions"
        assert git.commit_parents(committed) == [candidate, source]
        assert git.recovered_integration_commit_matches(
            checkout, expected_head_sha=candidate, expected_merge_sha=source,
            message="chore: resolve Parent integration conflict",
        )
        if interruption == "wrong-message":
            command(checkout, "commit", "--amend", "-m", "chore(ticket-0): resolve integration conflict")
        elif interruption == "wrong-parents":
            command(checkout, "commit", "--allow-empty", "-m", "chore: resolve Parent integration conflict")
        elif interruption == "bad-delta":
            (checkout / "invalid.csv").write_bytes(b"key,value\r\n1,2\r\n")
            command(checkout, "add", "invalid.csv")
            command(checkout, "commit", "--amend", "--no-edit")
        if interruption in {"wrong-message", "wrong-parents", "bad-delta"}:
            before = (states.runs_directory / f"{run_id}.json").read_bytes()
            wrong_head = git.checkout_head(checkout)
            with pytest.raises(MissingRunMaterialsError, match="HEAD 不符"):
                Controller(FixtureGitHubReader(fixture), git, states).resume(run_id, explicit_resume=True)
            assert (states.runs_directory / f"{run_id}.json").read_bytes() == before
            assert git.checkout_head(checkout) == wrong_head
            assert (checkout / "preserved.txt").read_text() == "partial conflict work\n"
            assert agents.integration_calls == 1
            assert not agents.publication_requests
            return
    seed_idle_control(TaskControlStore(states.root), TaskKey(repo, state["repository"], 1), run_id)
    agents.resume_conflict = True
    import agent_run.cli as cli
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(cli, "_agent_backend", lambda *args, **kwargs: profiled)
    if interruption == "commit":
        Controller(FixtureGitHubReader(fixture), git, states).resume(run_id, explicit_resume=True)
        with pytest.raises(RuntimeError, match="stopped at integration review boundary"):
            ParentDeliveryEngine(git=git, states=states, github=publisher, agents=profiled).deliver(run_id)
    else:
        with pytest.raises(RuntimeError, match="stopped at integration review boundary"):
            result = invoke_cli_inprocess(repo, fixture, "resume", run_id, "--state-dir", str(states.root), "--json")
            assert result.returncode == 0, result.stdout + result.stderr
    assert agents.successor_seen
    loaded = StateStore(states.root).load_current_run(run_id)
    assert loaded is not None
    assert loaded["parent_job"]["integration_development_budget"] == integration_budget
    assert agents.integration_calls == (2 if interruption == "worker" else 1)
    if interruption in {"commit", "cli-commit"}:
        assert git.is_ancestor(committed, loaded["parent_job"]["candidate_sha"])
    assert any(item["phase"] == "integration_conflict" and item["status"] == "completed"
               for item in loaded["agent_invocation_history"])
    before = (states.runs_directory / f"{run_id}.json").read_bytes()
    for query in ("status", "history"):
        result = invoke_cli_inprocess(repo, None, query, run_id, "--state-dir", str(states.root), "--json")
        assert result.returncode == 0, result.stdout + result.stderr
    assert (states.runs_directory / f"{run_id}.json").read_bytes() == before
