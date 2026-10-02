"""Public approval updates the existing Parent delivery without reusing its grant."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.runner_credentials import RunnerCredentialError
from support.cli import load_only_run_state, run_cli, stdout_json
from support.inprocess_cli import invoke_cli_inprocess
from support.parent_approval_update import (
    append_remote, control_record, paused_update, ready_parent, update_agents,
)
from support.workspace import managed_repo, managed_state


def test_public_approve_updates_both_heads_and_requires_new_approval(git_repo: Path) -> None:
    fixture, before = ready_parent(git_repo)
    original = before["parent_job"]
    source = append_remote(git_repo, fixture, original["parent_branch"], "human.txt", "source\n")
    target = append_remote(git_repo, fixture, "main", "target.txt", "target\n")
    agents = update_agents(git_repo)

    updated = run_cli(git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents))

    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert stdout_json(updated)["status"] == "parent_approval_pending"
    assert stdout_json(updated)["action"]["status"] == "updating"
    state = load_only_run_state(git_repo)
    job = state["parent_job"]
    assert state["run_id"] == before["run_id"]
    assert job["parent_branch"] == original["parent_branch"]
    assert job["pr_number"] == original["pr_number"]
    assert job.get("approval_grant") is None
    assert job["approval_update"]["previous_head_sha"] == original["published_sha"]
    assert job["approval_update"]["source_sha"] == source
    assert job["approval_update"]["target_sha"] == target
    assert job["review_budget"]["development_attempts"] == 0
    assert job["review_budget_history"][0]["review_budget"] == original["review_budget"]
    assert job["ci_recovery"] == original["ci_recovery"]
    git = GitRepository(managed_repo(git_repo))
    assert all(git.is_ancestor(sha, job["published_sha"]) for sha in (source, target, original["published_sha"]))
    assert job["acceptance_record"]["reviewed_candidate_sha"] == job["candidate_sha"]
    assert job["acceptance_record"]["reviewed_base_sha"] == target
    assert job["integration_resync"]["used"] == 1
    control = control_record(git_repo)
    assert control["action"]["kind"] == "approve"
    assert control["action"]["status"] == "completed"
    assert control["action"]["result_status"] == "updating"
    assert control["executor"]["status"] == "exited"
    assert control["executor"]["generation"] == control["action"]["executor_generation"]
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["state"] == "OPEN"
    assert not delivery.get("closed_issues")

    # The approved merge can advance main before its response is received.
    # Recovery must reconcile it, without treating that advance as a new update.
    data = json.loads(fixture.read_text())
    data["delivery"].update(
        crash_after_normal_merge_once=True, crash_after_close_parent_issue_once=True,
    )
    fixture.write_text(json.dumps(data))
    approved = run_cli(git_repo, fixture, "approve", before["run_id"])

    assert approved.returncode == 2, approved.stdout + approved.stderr
    interrupted = load_only_run_state(git_repo)
    assert interrupted["parent_job"]["phase"] == "merging"
    assert json.loads(fixture.read_text())["delivery"]["pull_requests"][0]["state"] == "MERGED"
    grant = interrupted["parent_job"]["approval_grant"]
    resumed = run_cli(git_repo, fixture, "resume", before["run_id"])
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert stdout_json(resumed)["status"] == "completed"
    final = load_only_run_state(git_repo)
    assert final["parent_job"]["approval_grant"] == grant
    assert final["parent_job"]["approval_grant"]["head_sha"] == job["published_sha"]
    assert final["parent_job"]["review_budget"] == job["review_budget"]
    assert final["parent_job"]["integration_resync"] == job["integration_resync"]
    assert final["parent_job"]["merge_intent"]["attempts"] == 1
    assert final["agent_invocation_history"] == state["agent_invocation_history"]
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["state"] == "MERGED"
    assert delivery["closed_issues"].count(1) == 1


@pytest.mark.parametrize("gate", ["requirement", "source_rewrite", "foreign_pr", "permission"])
def test_approval_change_preserves_existing_scope_identity_and_permission_gates(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, gate: str,
) -> None:
    fixture, before = ready_parent(git_repo)
    append_remote(git_repo, fixture, "main", "target.txt", "new target\n")
    data = json.loads(fixture.read_text())
    if gate == "requirement":
        data["parent"]["body"] += "\nA different acceptance requirement."
    elif gate == "source_rewrite":
        branch = before["parent_job"]["parent_branch"]
        data["delivery"]["published_branches"][branch] = before["base"]["sha"]
        data["delivery"]["pull_requests"][0]["head_sha"] = before["base"]["sha"]
    elif gate == "foreign_pr":
        data["delivery"]["pull_requests"][0]["head_repository"] = "other/project"
    else:
        def denied(*_args: Any, **_kwargs: Any) -> Any:
            raise RunnerCredentialError("Runner repository permission was revoked")
        monkeypatch.setattr(FixtureGitHubPublisher, "live_branch_heads", denied)
    fixture.write_text(json.dumps(data))
    external_before = data["delivery"]

    result = invoke_cli_inprocess(git_repo, fixture, "approve", before["run_id"], "--json")

    assert result.returncode == 2, result.stdout + result.stderr
    state = load_only_run_state(git_repo)
    job = state["parent_job"]
    assert job["candidate_sha"] == before["parent_job"]["candidate_sha"]
    assert job["review_budget"] == before["parent_job"]["review_budget"]
    assert state["agent_invocation_history"] == before["agent_invocation_history"]
    assert "approval_update" not in job
    external_after = json.loads(fixture.read_text())["delivery"]
    for field in ("pull_requests", "published_branches", "closed_issues", "mutations"):
        assert external_after.get(field) == external_before.get(field)
    if gate == "requirement":
        assert state["status"] == "requeue_required"
    elif gate == "permission":
        assert state["status"] == "execution_failed"
    else:
        assert state["status"] == "blocked"


def test_exhausted_update_resync_pauses_and_resume_renews_only_its_allowance(git_repo: Path) -> None:
    fixture, initial = ready_parent(git_repo, "--integration-resync-limit", "1")
    agents = update_agents(git_repo)
    append_remote(git_repo, fixture, "main", "first.txt", "first target\n")
    first = invoke_cli_inprocess(
        git_repo, fixture, "approve", initial["run_id"], "--agent-fixture", str(agents), "--json",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    before = load_only_run_state(git_repo)
    target = append_remote(git_repo, fixture, "main", "target.txt", "new target\n")

    stopped = invoke_cli_inprocess(
        git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents), "--json",
    )

    assert stopped.returncode == 2, stopped.stdout + stopped.stderr
    state = load_only_run_state(git_repo)
    job = state["parent_job"]
    assert job["blocked_reason"] == "integration_resync_exhausted"
    assert job.get("approval_grant") is None
    assert job["candidate_sha"] == before["parent_job"]["candidate_sha"]
    assert job["review_budget"] == before["parent_job"]["review_budget"]
    assert job["ci_recovery"] == before["parent_job"]["ci_recovery"]
    assert state["agent_invocation_history"] == before["agent_invocation_history"]

    resumed_script = json.loads(agents.read_text())
    resumed_script["reviews"][0]["thread_id"] = "renewed-update-reviewer"
    resumed_agents = git_repo / "resumed-update-agents.json"
    resumed_agents.write_text(json.dumps(resumed_script))
    resumed = invoke_cli_inprocess(
        git_repo, fixture, "resume", before["run_id"], "--agent-fixture", str(resumed_agents),
        "--json",
    )

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    final = load_only_run_state(git_repo)["parent_job"]
    assert final["phase"] == "ready_for_approval"
    assert final.get("approval_grant") is None
    assert final["pr_number"] == job["pr_number"]
    assert final["integration_resync"]["generation"] == 2
    assert final["integration_resync"]["used"] == 1
    assert final["review_budget_history"][-1]["review_budget"] == job["review_budget"]
    assert final["ci_recovery"] == job["ci_recovery"]
    assert final["acceptance_record"]["reviewed_base_sha"] == target


def test_another_target_append_during_update_reviews_the_latest_combination(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import cli
    from agent_run.agent_fixture import FixtureAgentBackend
    from agent_run.agents import ReviewResult
    from support.cli_payloads import passing_acceptance

    fixture, before = ready_parent(git_repo)
    append_remote(git_repo, fixture, "main", "first.txt", "first update\n")
    agents = update_agents(git_repo)
    script = json.loads(agents.read_text())
    script["reviews"].append(passing_acceptance("latest-reviewer", "Latest combination passed"))
    agents.write_text(json.dumps(script))
    publishers: list[FixtureGitHubPublisher] = []

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
                # The fixture Publisher caches the whole remote document.
                # Keep its remote view aligned with this collaborator write.
                for publisher in publishers:
                    publisher.data = json.loads(fixture.read_text())
                self.changed = True
            return result

    backend = MovingTargetAgents(agents)
    monkeypatch.setattr(cli, "FixtureGitHubPublisher", LivePublisher)
    monkeypatch.setattr(cli, "_agent_backend", lambda *_args, **_kwargs: backend)

    result = invoke_cli_inprocess(git_repo, fixture, "approve", before["run_id"], "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    final = load_only_run_state(git_repo)["parent_job"]
    target = json.loads(fixture.read_text())["default_head_sha"]
    assert final["phase"] == "ready_for_approval"
    assert final.get("approval_grant") is None
    assert final["pr_number"] == before["parent_job"]["pr_number"]
    assert final["acceptance_record"]["reviewed_base_sha"] == target
    assert final["integration_resync"]["used"] == 2
    assert len(final["review_budget_history"]) == 2
    assert final["review_budget"]["development_attempts"] == 0
    assert final["review_budget"]["reviewer_invocations"] == 1
    assert GitRepository(managed_repo(git_repo)).is_ancestor(target, final["published_sha"])


@pytest.mark.parametrize("boundary", ["synced", "conflict", "review", "checks"])
def test_update_stop_resume_preserves_work_and_never_restores_grant(
    git_repo: Path, boundary: str,
) -> None:
    fixture, before = ready_parent(git_repo)
    original = before["parent_job"]
    conflict = boundary == "conflict"
    target = append_remote(
        git_repo, fixture, "main", "parent-feature.txt" if conflict else "target.txt",
        "human contribution\n",
    )
    agents = update_agents(git_repo, conflict=conflict)
    if boundary == "checks":
        data = json.loads(fixture.read_text())
        data["delivery"].update(required_checks=["pending"], check_position=0)
        fixture.write_text(json.dumps(data))

    with paused_update(git_repo, fixture, agents, before["run_id"], boundary) as process:
        in_progress = load_only_run_state(git_repo)
        active = in_progress["parent_job"]
        assert active.get("approval_grant") is None
        control = control_record(git_repo)
        assert control["action"]["status"] == "completed"
        assert control["action"]["result_status"] == "updating"
        assert control["executor"]["status"] == "running"
        assert control["executor"]["generation"] == control["action"]["executor_generation"]
        if conflict:
            assert active["parent_integration_conflict"]["paths"] == ["parent-feature.txt"]
        stopped = run_cli(git_repo, fixture, "stop", before["run_id"])
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert stdout_json(stopped)["status"] == "operator_stopped"
        process.communicate(timeout=10)
        assert process.returncode is not None

    paused = load_only_run_state(git_repo)
    saved = paused["parent_job"]
    assert saved.get("approval_grant") is None
    assert saved["candidate_sha"] == active["candidate_sha"]
    assert saved["review_budget"] == active["review_budget"]
    assert saved["ci_recovery"] == original["ci_recovery"]
    if boundary in {"conflict", "review"}:
        assert saved["pending_semantic_attempt"] == active["pending_semantic_attempt"]
    if conflict:
        checkout = managed_state(git_repo) / "worktrees" / before["run_id"] / "parent"
        assert (checkout / "partial.txt").read_text() == "preserved conflict progress\n"
    if boundary == "checks":
        data = json.loads(fixture.read_text())
        data["delivery"].update(required_checks=["pass"], check_position=0)
        fixture.write_text(json.dumps(data))

    resumed = run_cli(git_repo, fixture, "resume", before["run_id"], "--agent-fixture", str(agents))

    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert stdout_json(resumed)["status"] == "parent_approval_pending"
    final = load_only_run_state(git_repo)
    job = final["parent_job"]
    assert job.get("approval_grant") is None
    assert job["pr_number"] == original["pr_number"]
    assert job["review_budget"]["development_attempts"] == 0
    assert job["review_budget_history"][0]["review_budget"] == original["review_budget"]
    assert job["acceptance_record"]["reviewed_base_sha"] == target
    assert job["ci_recovery"] == original["ci_recovery"]
    assert job["review_budget"]["reviewer_invocations"] == 1
    if conflict:
        git = GitRepository(managed_repo(git_repo))
        assert all(git.is_ancestor(sha, job["published_sha"])
                   for sha in (target, original["published_sha"]))
        assert job["parent_integration"]["conflict_paths"] == ["parent-feature.txt"]
    if boundary in {"conflict", "review"}:
        attempt_id = saved["pending_semantic_attempt"]["attempt_id"]
        assert sum(item["attempt_id"] == attempt_id for item in job["semantic_attempt_history"]) == 1
    delivery = json.loads(fixture.read_text())["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["state"] == "OPEN"
