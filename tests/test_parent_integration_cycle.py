"""Parent-only public continuation retains cumulative integration work facts."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.agent_profiles import AgentProfileStore, ProfiledAgentBackend
from agent_run.controller import Controller
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.executor_host import FakeExecutorHost
from agent_run.git import GitError, GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.development_prompts import development_prompt
from agent_run.reviewer_prompts import review_prompt
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from support.delivery_agents import PassAgents
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import seed_idle_control, write_fixture
from support.workspace import prepare_workspace

from support.state import snapshot_run


@pytest.mark.parametrize("failure_step", ["integration", "fetch"])
def test_git_and_findings_share_parent_allowance_through_public_resume(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, failure_step: str,
) -> None:
    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    fixture = write_fixture(repo / "github.json", issues={})
    git = GitRepository(repo)
    states = StateStore(workspace.state_root)
    policy = DeliveryPolicy(parent_only_paired_rounds=1, run_repair_rounds=1,
                            ticket_review_rounds=1)
    state, _ = Controller(FixtureGitHubReader(fixture), git, states,
                          delivery_policy=policy).start(1)
    run_id = state["run_id"]
    publisher = FixtureGitHubPublisher(fixture, git)
    checkout = states.root / "worktrees" / run_id / "parent"
    profiles = AgentProfileStore(states.root)
    profiles.initialize(run_id)
    original_integrate = git.integrate_remote_heads
    original_fetch = git.fetch_remote_commit
    failed = False

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal failed
        if not failed:
            failed = True
            raise GitError("controlled Git check failure", evidence={
                "operation": "fetch_remote_commit" if failure_step == "fetch" else "integrate_remote_heads",
                "head_sha": git.checkout_head(repo if failure_step == "fetch" else checkout),
                "merge_head_sha": None, "stderr_json": '"controlled raw output\\n"',
            })
        return (original_fetch if failure_step == "fetch" else original_integrate)(*args, **kwargs)

    monkeypatch.setattr(git, "fetch_remote_commit" if failure_step == "fetch" else "integrate_remote_heads", fail_once)

    class Agents(PassAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("repair_source") != "integration":
                return super().develop(request)
            self.events.append("integration-develop")
            self.development_requests.append(request)
            thread = str(request.get("thread_id") or "integration-worker")
            event = request["_invocation_event"]
            event("started", requested_thread_id=request.get("thread_id"), attempt_count=1)
            event("thread_started", reported_thread_id=thread)
            prompt = development_prompt(request)
            assert "准确源/目标" in prompt
            if "controlled Git check failure" in request["integration_evidence"]:
                assert "controlled raw output" in prompt
                (checkout / "git-fix.txt").write_text("fixed Git scene\n")
            else:
                assert "combined result regression" in prompt
                (checkout / "finding-fix.txt").write_text("fixed combination\n")
            event("completed", reported_thread_id=thread)
            return DevelopmentResult(thread_id=thread,
                                     summary="Validated current integration work")

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.events.append("review")
            self.review_requests.append(request)
            result = super().review(request)
            if self.review_count > 1:
                prompt = review_prompt(request)
                assert "当前整体需求形成完整结论" in prompt
                assert request["integration_record"]["target_sha"] in prompt
                assert "previous_acceptance_artifact" in request
            if self.review_count == 2:
                result.artifact["checks"]["e2e"].update(
                    status="fail", evidence="combined result regression",
                    findings=["combined result regression: repair current combination"],
                )
            return result

    agents = Agents(checkout)
    backend = ProfiledAgentBackend(agents, profiles, run_id=run_id)
    engine = ParentDeliveryEngine(git=git, states=states, github=publisher, agents=backend)
    paused = engine.deliver(run_id)
    job = paused["parent_job"]
    assert job["blocked_reason"] == "integration_development_exhausted"
    assert agents.events == ["develop", "review", "integration-develop", "review"]
    assert job["integration_development_budget"]["used"] == 1
    assert job["review_budget"]["development_attempts"] == 1
    assert job["review_budget"]["reviewer_invocations"] == 1
    retained = deepcopy({key: job[key] for key in ("review_budget", "ci_recovery", "integration_resync")})
    before = snapshot_run((states.root, str(run_id)))
    for query in ("status", "history"):
        result = invoke_cli_inprocess(repo, None, query, run_id, "--state-dir", str(states.root), "--json")
        assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot_run((states.root, str(run_id))) == before
    seed_idle_control(TaskControlStore(states.root), TaskKey(repo, state["repository"], 1), run_id)
    import agent_run.cli as cli
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(cli, "_agent_backend", lambda *args, **kwargs: backend)
    result = invoke_cli_inprocess(repo, fixture, "resume", run_id,
                                  "--state-dir", str(states.root), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    resumed = states.load_current_run(run_id)
    assert resumed is not None
    job = resumed["parent_job"]
    assert job["phase"] == "ready_for_approval"
    assert job["integration_development_budget"]["used"] == 2
    # Public resume uses current defaults for the new grant (default 3),
    # retaining the original frozen one-unit allowance and usage.
    assert job["integration_development_budget"]["limit"] == 4
    assert job["integration_development_budget"]["window"] == 2
    assert {key: job[key] for key in retained} == retained
    assert agents.events == ["develop", "review", "integration-develop", "review",
                             "integration-develop", "review", "publication"]
    repeated = engine.deliver(run_id)
    assert repeated["parent_job"]["integration_development_budget"] == job["integration_development_budget"]
    completed = ParentDeliveryEngine(git=git, states=states,
        github=FixtureGitHubPublisher(fixture, git), agents=backend).approve(run_id)
    assert completed["status"] == "completed"


def test_second_conflict_checkpoint_closes_source_attempt_before_public_resume(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import subprocess

    workspace = prepare_workspace(git_repo)
    repo = workspace.repository_root
    fixture = write_fixture(repo / "github.json", issues={})
    git = GitRepository(repo)
    states = StateStore(workspace.state_root)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    run_id = state["run_id"]
    base = state["base"]["sha"]
    publisher = FixtureGitHubPublisher(fixture, git)
    checkout = states.root / "worktrees" / run_id / "parent"
    collaborator = states.root / "two-conflict-collaborator"

    def command(root: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()

    command(repo, "worktree", "add", "--detach", str(collaborator), base)
    try:
        (collaborator / "delivered.txt").write_text("source contribution\n")
        command(collaborator, "add", "delivered.txt")
        command(collaborator, "commit", "-m", "source contribution")
        source = command(collaborator, "rev-parse", "HEAD")
        command(collaborator, "reset", "--hard", base)
        (collaborator / "delivered.txt").write_text("target contribution\n")
        command(collaborator, "add", "delivered.txt")
        command(collaborator, "commit", "-m", "target contribution")
        target = command(collaborator, "rev-parse", "HEAD")
    finally:
        command(repo, "worktree", "remove", "--force", str(collaborator))

    class Agents(PassAgents):
        stages: list[str] = []

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("repair_source") != "integration":
                return super().develop(request)
            scene = json.loads(request["integration_evidence"])
            self.stages.append(scene["stage"])
            thread = str(request["thread_id"])
            event = request["_invocation_event"]
            event("started", requested_thread_id=thread, attempt_count=1)
            event("thread_started", reported_thread_id=thread)
            contents = "local and source\n" if scene["stage"] == "source" else "local and source and target\n"
            (checkout / "delivered.txt").write_text(contents)
            event("completed", reported_thread_id=thread)
            return DevelopmentResult(thread_id=thread, summary="Resolved " + scene["stage"])

        def review(self, request: dict[str, Any]) -> ReviewResult:
            result = super().review(request)
            if self.review_count == 1:
                refs = publisher.data["delivery"]["published_branches"]
                refs[state["parent_branch"]] = source
                refs[state["base"]["branch"]] = target
                publisher._save()
            return result

    profiles = AgentProfileStore(states.root)
    profiles.initialize(run_id)
    agents = Agents(checkout)
    backend = ProfiledAgentBackend(agents, profiles, run_id=run_id)
    save = states.save_run
    interrupted = False

    def interrupt_checkpoint(current_run: str, current: dict[str, Any]) -> Any:
        nonlocal interrupted
        job = current.get("parent_job", {})
        scene = job.get("parent_integration_conflict", {})
        if not interrupted and scene.get("stage") == "target" and scene.get("attempts") == 0:
            # Before the fix this save contained the previous source Attempt.
            assert job.get("pending_semantic_attempt") is None
            interrupted = True
            save(current_run, current)
            raise OSError("interrupted after second conflict checkpoint")
        return save(current_run, current)

    monkeypatch.setattr(states, "save_run", interrupt_checkpoint)
    with pytest.raises(OSError, match="second conflict checkpoint"):
        ParentDeliveryEngine(git=git, states=states, github=publisher, agents=backend).deliver(run_id)
    monkeypatch.setattr(states, "save_run", save)
    loaded = StateStore(states.root).load_current_run(run_id)
    assert loaded is not None
    job = loaded["parent_job"]
    assert job["integration_development_budget"]["used"] == 1
    assert job.get("pending_semantic_attempt") is None
    assert agents.stages == ["source"]
    assert controller.record_execution_failure(run_id, "interrupted after second conflict checkpoint")
    seed_idle_control(TaskControlStore(states.root), TaskKey(repo, state["repository"], 1), run_id)
    import agent_run.cli as cli
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: FakeExecutorHost())
    monkeypatch.setattr(cli, "_agent_backend", lambda *args, **kwargs: backend)
    result = invoke_cli_inprocess(repo, fixture, "resume", run_id,
                                  "--state-dir", str(states.root), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    final = StateStore(states.root).load_current_run(run_id)
    assert final is not None
    job = final["parent_job"]
    assert job["phase"] == "ready_for_approval"
    assert agents.stages == ["source", "target"]
    assert job["integration_development_budget"]["used"] == 2
    charged = job["integration_development_budget"]["attempt_ids"]
    assert len(charged) == len(set(charged)) == 2
    assert [record["stage"] for record in job["integration_work_history"]] == ["source", "target"]
    assert all(git.is_ancestor(sha, job["candidate_sha"]) for sha in (source, target))
