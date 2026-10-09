"""Public Run approval refreshes its accepted combination before a new grant."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from support.cli import load_only_run_state, run_cli, stdout_json
from support.cli_payloads import passing_acceptance
from support.inprocess_cli import invoke_cli_inprocess
from support.parent_approval_update import append_remote, control_record
from support.published_run import prepare_published_run
from support.run_approval_update import paused_update, ready_two_ticket_run, update_agents
from support.workspace import managed_repo, managed_state


def test_public_approve_updates_run_heads_and_requires_new_approval(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import cli
    fixture, before = ready_two_ticket_run(git_repo)
    assert set(before["ticket_jobs"]) == {"3", "4"}
    assert all(job["phase"] == "completed" for job in before["ticket_jobs"].values())
    original = before["run_publication"]
    source = append_remote(git_repo, fixture, before["run_branch"], "source.txt", "source\n")
    target = append_remote(git_repo, fixture, "main", "target.txt", "target\n")
    agents = update_agents(git_repo)

    updated = run_cli(git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents))

    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert stdout_json(updated)["status"] == "run_approval_pending"
    assert stdout_json(updated)["action"]["status"] == "updating"
    state = load_only_run_state(git_repo)
    final_pr = state["run_publication"]
    run = state["run_acceptance"]
    assert state["run_id"] == before["run_id"]
    assert state["run_branch"] == before["run_branch"]
    assert state["ticket_jobs"] == before["ticket_jobs"]
    assert final_pr["pr_number"] == original["pr_number"]
    assert final_pr.get("approval_grant") is None
    assert run["run_integration"]["source_sha"] == source
    assert run["run_integration"]["target_sha"] == target
    assert run["review_budget"]["development_attempts"] == 0
    assert run["review_budget"]["reviewer_invocations"] == 2
    assert run["ci_recovery"] == before["run_acceptance"]["ci_recovery"]
    assert run["integration_resync"]["used"] == 1
    assert run["acceptance_record"]["reviewed_default_base_sha"] == target
    assert run["acceptance_record"]["reviewed_head_sha"] == final_pr["published_head_sha"]
    git = GitRepository(managed_repo(git_repo))
    assert all(git.is_ancestor(sha, final_pr["published_head_sha"])
               for sha in (source, target, original["published_head_sha"]))
    control = control_record(git_repo)
    assert control["action"]["status"] == "completed"
    assert control["action"]["result_status"] == "updating"
    assert control["executor"]["status"] == "exited"
    assert control["executor"]["generation"] == control["action"]["executor_generation"]
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 3
    assert delivery["pull_requests"][-1]["state"] == "OPEN"
    assert 1 not in delivery["closed_issues"]

    class LostMergeResponse(FixtureGitHubPublisher):
        def normal_merge(self, **authority: Any) -> str:
            super().normal_merge(**authority)
            raise SystemExit("Run update merged before its response was saved")

    with monkeypatch.context() as patch:
        patch.setattr(cli, "FixtureGitHubPublisher", LostMergeResponse)
        interrupted = invoke_cli_inprocess(git_repo, fixture, "approve", before["run_id"], "--json")
    assert interrupted.returncode == 2, interrupted.stdout + interrupted.stderr
    pending = load_only_run_state(git_repo)
    grant = pending["run_publication"]["approval_grant"]
    assert json.loads(fixture.read_text())["delivery"]["pull_requests"][-1]["state"] == "MERGED"
    resumed = run_cli(git_repo, fixture, "resume", before["run_id"])
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    final = load_only_run_state(git_repo)
    assert final["status"] == "completed"
    assert final["run_publication"]["approval_grant"] == grant
    assert final["run_publication"]["approval_grant"]["head_sha"] == final_pr["published_head_sha"]
    assert final["run_publication"]["merge_intent"]["attempts"] == 1
    assert final["run_acceptance"]["integration_resync"] == run["integration_resync"]
    assert final["agent_invocation_history"] == state["agent_invocation_history"]
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 3
    assert delivery["pull_requests"][-1]["state"] == "MERGED"
    assert delivery["closed_issues"].count(1) == 1


@pytest.mark.parametrize("boundary", ["synced", "conflict", "review", "checks"])
def test_run_update_stop_resume_preserves_attempts_and_invalidates_old_grant(
    git_repo: Path, boundary: str,
) -> None:
    fixture, before = prepare_published_run(git_repo)
    conflict = boundary == "conflict"
    if conflict:
        source = append_remote(
            git_repo, fixture, before["run_branch"], "README.md", "Run contribution\n",
        )
    else:
        source = before["run_publication"]["published_head_sha"]
    target = append_remote(
        git_repo, fixture, "main", "README.md" if conflict else "target.txt",
        "Default contribution\n",
    )
    agents = update_agents(git_repo, conflict=conflict)
    if boundary == "checks":
        data = json.loads(fixture.read_text())
        data["delivery"].update(required_checks=["pending"], check_position=0)
        fixture.write_text(json.dumps(data))

    with paused_update(git_repo, fixture, agents, before["run_id"], boundary) as process:
        in_progress = load_only_run_state(git_repo)
        active = in_progress["run_acceptance"]
        assert in_progress["run_publication"].get("approval_grant") is None
        control = control_record(git_repo)
        assert control["action"]["status"] == "completed"
        assert control["action"]["result_status"] == "updating"
        assert control["executor"]["status"] == "running"
        assert control["executor"]["generation"] == control["action"]["executor_generation"]
        if conflict:
            assert active["run_integration_conflict"]["paths"] == ["README.md"]
        stopped = run_cli(git_repo, fixture, "stop", before["run_id"])
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert stdout_json(stopped)["status"] == "operator_stopped"
        process.communicate(timeout=10)
        assert process.returncode is not None

    paused = load_only_run_state(git_repo)
    saved = paused["run_acceptance"]
    assert paused["run_publication"].get("approval_grant") is None
    assert paused["ticket_jobs"] == before["ticket_jobs"]
    assert saved["review_budget"] == active["review_budget"]
    assert saved["ci_recovery"] == before["run_acceptance"]["ci_recovery"]
    if boundary in {"conflict", "review"}:
        assert saved["pending_semantic_attempt"] == active["pending_semantic_attempt"]
    if conflict:
        checkout = managed_state(git_repo) / "worktrees" / before["run_id"] / "run-integration"
        assert (checkout / "partial.txt").read_text() == "preserved Run conflict work\n"
        agents = update_agents(git_repo, conflict=True, resumed=True)
    if boundary == "checks":
        data = json.loads(fixture.read_text())
        data["delivery"].update(required_checks=["pass"], check_position=0)
        fixture.write_text(json.dumps(data))

    resumed = run_cli(git_repo, fixture, "resume", before["run_id"], "--agent-fixture", str(agents))
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert stdout_json(resumed)["status"] == "run_approval_pending"
    final = load_only_run_state(git_repo)
    run = final["run_acceptance"]
    assert final["run_publication"].get("approval_grant") is None
    assert final["run_publication"]["pr_number"] == before["run_publication"]["pr_number"]
    assert final["ticket_jobs"] == before["ticket_jobs"]
    assert run["review_budget"]["development_attempts"] == (1 if conflict else 0)
    assert run["review_budget"]["reviewer_invocations"] == 2
    assert run["ci_recovery"] == before["run_acceptance"]["ci_recovery"]
    assert run["acceptance_record"]["reviewed_default_base_sha"] == target
    assert run["integration_resync"]["used"] == 1
    if conflict:
        git = GitRepository(managed_repo(git_repo))
        assert all(git.is_ancestor(sha, final["run_publication"]["published_head_sha"])
                   for sha in (source, target))
        assert run["run_integration"]["conflict_paths"] == ["README.md"]
    if boundary in {"conflict", "review"}:
        attempt_id = saved["pending_semantic_attempt"]["attempt_id"]
        assert sum(item["attempt_id"] == attempt_id for item in run["semantic_attempt_history"]) == 1
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 2
    assert delivery["pull_requests"][-1]["state"] == "OPEN"


def test_run_update_reviews_latest_target_after_another_append(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import cli
    from agent_run.agent_fixture import FixtureAgentBackend
    from agent_run.agents import ReviewResult
    from agent_run.notification_events import events
    from agent_run.notifications import Notifications

    fixture, before = prepare_published_run(git_repo)
    append_remote(git_repo, fixture, "main", "first.txt", "first update\n")
    agents = update_agents(git_repo)
    script = json.loads(agents.read_text())
    script["run_reviews"].append(passing_acceptance("latest-run-reviewer", "Latest Run passed"))
    agents.write_text(json.dumps(script))
    publishers: list[FixtureGitHubPublisher] = []
    notice_order = git_repo / "update-notice-order.jsonl"
    observe = Notifications.observe
    integrate = GitRepository.integrate_remote_heads

    def observe_update(self: Any, state: dict[str, Any], **kwargs: Any) -> None:
        for event in events(state):
            if event["kind"] == "approval_update":
                # StateStore notifies only after the update fact is durable.
                persisted = load_only_run_state(git_repo)
                assert persisted["run_publication"]["approval_update"] == state["run_publication"]["approval_update"]
                with notice_order.open("a") as stream:
                    stream.write(json.dumps({"kind": "notice", "id": event["id"]}) + "\n")
        observe(self, state, **kwargs)

    def integrate_after_notice(self: Any, *args: Any, **kwargs: Any) -> Any:
        assert notice_order.exists(), "Run synchronized before submitting its update notice"
        with notice_order.open("a") as stream:
            stream.write(json.dumps({"kind": "synchronize"}) + "\n")
        return integrate(self, *args, **kwargs)

    class LivePublisher(FixtureGitHubPublisher):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            publishers.append(self)

    class MovingTargetAgents(FixtureAgentBackend):
        changed = False

        def review(self, request: dict[str, Any]) -> ReviewResult:
            result = super().review(request)
            if not self.changed:
                append_remote(git_repo, fixture, "main", "later.txt", "later update\n")
                for publisher in publishers:
                    publisher.data = json.loads(fixture.read_text())
                self.changed = True
            return result

    backend = MovingTargetAgents(agents)
    monkeypatch.setattr(cli, "FixtureGitHubPublisher", LivePublisher)
    monkeypatch.setattr(cli, "_agent_backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(Notifications, "observe", observe_update)
    monkeypatch.setattr(GitRepository, "integrate_remote_heads", integrate_after_notice)
    result = invoke_cli_inprocess(git_repo, fixture, "approve", before["run_id"], "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    assert stdout_json(result)["action"]["status"] == "updating"
    order = [json.loads(line)["kind"] for line in notice_order.read_text().splitlines()]
    assert order[0] == "notice"
    assert "synchronize" in order
    final = load_only_run_state(git_repo)
    run = final["run_acceptance"]
    target = json.loads(fixture.read_text())["default_head_sha"]
    assert final["status"] == "run_approval_pending"
    assert final["run_publication"].get("approval_grant") is None
    assert final["run_publication"]["pr_number"] == before["run_publication"]["pr_number"]
    assert final["ticket_jobs"] == before["ticket_jobs"]
    assert run["acceptance_record"]["reviewed_default_base_sha"] == target
    assert run["integration_resync"]["used"] == 2
    assert run["review_budget"]["development_attempts"] == 0
    assert run["review_budget"]["reviewer_invocations"] == 3
    assert GitRepository(managed_repo(git_repo)).is_ancestor(
        target, final["run_publication"]["published_head_sha"],
    )


def test_run_update_resume_reuses_returned_reviewer_after_readback_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.run_acceptance import RunAcceptanceEngine

    fixture, before = prepare_published_run(git_repo)
    target = append_remote(git_repo, fixture, "main", "target.txt", "new target\n")
    agents = update_agents(git_repo)
    refresh = RunAcceptanceEngine._refresh_run_currentness

    def fail_readback(self: Any, state: dict[str, Any]) -> bool:
        if state["run_acceptance"].get("pending_review_result"):
            raise OSError("Run Reviewer result readback unavailable")
        return refresh(self, state)

    with monkeypatch.context() as patch:
        patch.setattr(RunAcceptanceEngine, "_refresh_run_currentness", fail_readback)
        failed = invoke_cli_inprocess(
            git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents), "--json",
        )
    assert failed.returncode == 2, failed.stdout + failed.stderr
    paused = load_only_run_state(git_repo)
    run = paused["run_acceptance"]
    assert run["pending_review_result"]["record"]["reviewer_thread_id"] == "updated-run-reviewer"
    assert run["review_budget"]["reviewer_invocations"] == 2
    attempt = run["pending_semantic_attempt"]["attempt_id"]
    script = json.loads(agents.read_text())
    script["run_reviews"] = []
    agents.write_text(json.dumps(script))

    resumed = run_cli(git_repo, fixture, "resume", before["run_id"], "--agent-fixture", str(agents))

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    final = load_only_run_state(git_repo)
    run = final["run_acceptance"]
    assert final["status"] == "run_approval_pending"
    assert final["run_publication"].get("approval_grant") is None
    assert final["run_publication"]["pr_number"] == before["run_publication"]["pr_number"]
    assert run["acceptance_record"]["reviewed_default_base_sha"] == target
    assert run["review_budget"]["reviewer_invocations"] == 2
    assert "pending_review_result" not in run
    assert "pending_semantic_attempt" not in run
    assert sum(item["attempt_id"] == attempt for item in run["semantic_attempt_history"]) == 1
