from __future__ import annotations

from support.workspace import managed_repo, managed_state, prepare_workspace

import json
from pathlib import Path


from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.github_fixture import FixtureGitHubPublisher

from conftest import seed_idle_control
from test_cli import run_internal_stage, run_cli, stdout_json

from run_publication_test_support import RunPublicationAgents, _accepted_run

def test_public_cli_publish_then_approve_is_an_end_to_end_user_flow(
    git_repo: Path,
) -> None:
    workspace = prepare_workspace(git_repo)
    state, states, _git, _publisher = _accepted_run(
        workspace.repository_root, state_root=workspace.state_root
    )
    fixture = workspace.repository_root / "github.json"
    seed_idle_control(
        TaskControlStore(states.root),
        TaskKey(managed_repo(git_repo), str(state["repository"]), 1),
        str(state["run_id"]),
        state_dir=states.root,
    )
    agents = git_repo / "run-publication-agents.json"
    artifact = RunPublicationAgents().run_publication({"run_id": state["run_id"]})
    agents.write_text(json.dumps({"run_publications": [artifact]}), encoding="utf-8")

    published = run_internal_stage(
        git_repo,
        fixture,
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert published.returncode == 0, published.stderr
    assert stdout_json(published)["status"] == "run_approval_pending"
    approved = run_cli(
        git_repo, fixture, "approve", str(state["run_id"])
    )
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    assert (
        FixtureGitHubPublisher(fixture, _git).live_pull_request(1)[
            "state"
        ]
        == "MERGED"
    )
