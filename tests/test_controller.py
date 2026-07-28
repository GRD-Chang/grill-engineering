from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from conftest import write_fixture


def _issue(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "body": "Implement it.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def test_initial_state_failure_happens_before_branch_creation(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_path = write_fixture(
        git_repo / "github.json", issues={"2": _issue(2)}
    )
    store = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture_path),
        GitRepository(git_repo),
        store,
    )

    def fail_save(_run_id: str, _state: dict[str, Any]) -> None:
        raise OSError("simulated first-write interruption")

    monkeypatch.setattr(store, "save_run", fail_save)

    with pytest.raises(OSError, match="first-write interruption"):
        controller.start(1)

    branches = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-run/"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert branches == []
    assert not list((git_repo / ".agent-run" / "runs").glob("*.json"))
