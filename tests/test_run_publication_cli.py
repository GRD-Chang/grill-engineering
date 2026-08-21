from __future__ import annotations

import json
from pathlib import Path


from agent_run.github_fixture import FixtureGitHubPublisher

from test_cli import run_internal_stage, run_cli, stdout_json

from run_publication_test_support import RunPublicationAgents, _accepted_run

def test_public_cli_publish_then_approve_is_an_end_to_end_user_flow(
    git_repo: Path,
) -> None:
    state, _states, _git, _publisher = _accepted_run(git_repo)
    agents = git_repo / "run-publication-agents.json"
    artifact = RunPublicationAgents().run_publication({"run_id": state["run_id"]})
    agents.write_text(json.dumps({"run_publications": [artifact]}), encoding="utf-8")

    published = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "publish-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert published.returncode == 0, published.stderr
    assert stdout_json(published)["status"] == "run_approval_pending"
    approved = run_cli(
        git_repo, git_repo / "github.json", "approve", str(state["run_id"])
    )
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    assert (
        FixtureGitHubPublisher(git_repo / "github.json", _git).live_pull_request(1)[
            "state"
        ]
        == "MERGED"
    )
