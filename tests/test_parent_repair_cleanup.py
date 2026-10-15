from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from support.run_scenarios import _completed_run


@pytest.mark.parametrize("external_head", [False, True])
def test_parent_repair_cleanup_is_local_and_preserves_later_work(
    git_repo: Path, external_head: bool,
) -> None:
    state, states, git = _completed_run(git_repo)
    branch = f"agent-run-repair/{state['run_id']}/1"
    base = git.resolve(str(state["run_branch"]))
    checkout = states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    git.prepare_ticket_checkout(branch=branch, base_sha=base, checkout=checkout)
    candidate = base
    if external_head:
        (checkout / "later.txt").write_text("preserve later work\n")
        subprocess.run(["git", "add", "later.txt"], cwd=checkout, check=True)
        subprocess.run(["git", "commit", "-m", "later work"], cwd=checkout,
                       check=True, capture_output=True)
    publisher = FixtureGitHubPublisher(git_repo / "github.json", git)
    before = (git_repo / "github.json").read_bytes()
    result = DeliveryCleanupEngine(git=git, states=states, github=publisher).complete_run_repair(
        state, {"phase": "completed", "repair_branch": branch,
                "publication_sha": candidate, "integrated_sha": candidate},
    )
    assert (git_repo / "github.json").read_bytes() == before
    if external_head:
        assert result["delivery_cleanup"]["status"] == "cleanup_pending"
        assert (checkout / "later.txt").read_text() == "preserve later work\n"
        assert git.managed_branch_head(branch) != candidate
    else:
        assert result["delivery_cleanup"]["status"] == "completed"
        assert not checkout.exists()
        assert git.managed_branch_head(branch) is None
