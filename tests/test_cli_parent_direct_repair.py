from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher
from support.cli import load_only_run_state, run_cli, stdout_json
from support.cli_supervision import _resume_repair_agents
from support.inprocess_cli import invoke_cli_inprocess
from support.published_run import prepare_accepted_run
from support.workspace import managed_repo


@pytest.mark.parametrize("failure", ["permission", "lost_response"])
def test_parent_repair_run_branch_permission_resume_reuses_saved_results(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    fixture, _ = prepare_accepted_run(
        git_repo, delivery={"required_checks": ["fail", "pass"]},
    )
    agents = _resume_repair_agents(git_repo / "agents.json", expected_thread_id=None)

    publish_branch = FixtureGitHubPublisher.publish_branch

    def interrupt_write(
        self: FixtureGitHubPublisher, branch: str, head_sha: str,
        *, expected_remote_sha: str,
    ) -> Any:
        if failure == "permission":
            raise GitHubReadError("github_forbidden", "Run Branch rules denied this write")
        publish_branch(self, branch, head_sha, expected_remote_sha=expected_remote_sha)
        raise OSError("push response lost")

    # The public CLI fixture Executor forks, preserving the controllable
    # external boundary. The resume below uses a fresh real CLI process.
    with monkeypatch.context() as scoped:
        scoped.setattr(FixtureGitHubPublisher, "publish_branch", interrupt_write)
        rejected = invoke_cli_inprocess(
            git_repo, fixture, "run", "1", "--agent-fixture", str(agents), "--json",
        )
    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    paused = load_only_run_state(git_repo)
    assert paused["status"] == "ready_for_human"
    job = paused["run_acceptance"]["repair_job"]
    assert job["blocked_reason"] == "run_branch_write_failed"
    assert paused["diagnostics"][0]["message"] == (
        "Run Branch rules denied this write" if failure == "permission" else "push response lost"
    )
    assert job["pause_resume_phase"] == "accepted"
    for command in ("status", "history"):
        queried = invoke_cli_inprocess(git_repo, fixture, command, str(paused["run_id"]), "--json")
        assert queried.returncode == 0, queried.stdout + queried.stderr
        actions = stdout_json(queried)["operator_actions"]
        repair_action = next(action for action in actions if action["location"] == "run_repair")
        assert repair_action["phase"] == "accepted"
        assert repair_action["role"] is None
        assert repair_action["reason"] == "run_branch_write_failed"
    assert load_only_run_state(git_repo) == paused
    checkout = Path(job["repair_checkout"])
    assert checkout.exists()
    assert (checkout / "run-repair.txt").read_text(encoding="utf-8") == "recovered\n"
    candidate = job["candidate_sha"]
    assert job["run_branch_write_intent"]["candidate_sha"] == candidate
    assert job["acceptance_record"]["reviewed_candidate_sha"] == candidate
    git = GitRepository(managed_repo(git_repo))
    assert git.resolve(paused["run_branch"]) == job["base_sha"]
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["published_branches"][paused["run_branch"]] == (
        job["base_sha"] if failure == "permission" else candidate
    )
    repair_invocations = [
        invocation for invocation in paused["agent_invocation_history"]
        if invocation.get("work_subject") == f"run-repair:{paused['run_id']}"
    ]
    assert [item["role"] for item in repair_invocations] == ["development", "fresh_acceptance"]
    budget = paused["run_acceptance"]["ci_recovery"]

    # No Development or Review results remain available in the new process.
    recovery = git_repo / "resume-agents.json"
    recovery.write_text(json.dumps({
        "run_publications": json.loads(agents.read_text(encoding="utf-8"))["run_publications"],
    }), encoding="utf-8")
    resumed = run_cli(
        git_repo, fixture, "resume", str(paused["run_id"]),
        "--message", "Run Branch write permission has been restored.",
        "--agent-fixture", str(recovery),
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_approval_pending"
    assert state["run_acceptance"]["ci_recovery"] == budget
    assert [
        invocation for invocation in state["agent_invocation_history"]
        if invocation.get("work_subject") == f"run-repair:{state['run_id']}"
    ] == repair_invocations
    assert state["run_publication"]["required_checks_evidence"]["head_sha"] == candidate
    assert not checkout.exists()
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert not any(pull.get("scope") == "run_repair" for pull in delivery["pull_requests"])
    assert state["ticket_jobs"]["3"]["phase"] == "completed"

    approved = run_cli(git_repo, fixture, "approve", str(state["run_id"]))
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    delivered = load_only_run_state(git_repo)
    git = GitRepository(managed_repo(git_repo))
    assert git.resolve("main") == delivered["run_publication"]["integrated_sha"]
    assert git.is_ancestor(candidate, git.resolve("main"))
