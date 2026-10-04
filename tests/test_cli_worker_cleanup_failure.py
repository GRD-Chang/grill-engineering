"""Worker cleanup distinguishes revoked ownership from failed persistence."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_run import cli
from agent_run.task_control import ActionReconciliationError, TaskControlBusyError, TaskControlStore
from support.cli import load_only_run_state
from support.cli_fixtures import run_agents
from support.cli_payloads import ticket
from support.seeding import write_fixture


@pytest.mark.parametrize("revoked", [False, True])
def test_worker_cleanup_preserves_persistence_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, revoked: bool,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    monkeypatch.chdir(git_repo)
    returned = git_repo / "cleanup-returned"

    def finish(*args, **kwargs):
        if revoked:
            raise ActionReconciliationError("Executor ownership revoked")
        raise TaskControlBusyError("Worker cleanup lock deadline exhausted")

    def driver_factory(*args, **kwargs):
        class Driver:
            def advance(self, state):
                kwargs["on_worker_started"](os.getpid())
                kwargs["on_worker_finished"](os.getpid())
                returned.write_text("cleanup returned")
                return state
        return Driver()

    monkeypatch.setattr(TaskControlStore, "mark_worker_finished", finish)
    monkeypatch.setattr(cli, "_run_driver", driver_factory)
    cli.main([
        "run", "1", "--github-fixture", str(fixture),
        "--agent-fixture", str(agents), "--json", "--no-notifications",
    ])
    if revoked:
        assert returned.exists()
    else:
        assert not returned.exists()
        state = load_only_run_state(git_repo)
        assert "Worker cleanup lock deadline exhausted" in str(state["diagnostics"])
