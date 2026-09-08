from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run import cli
from agent_run.controller import Controller
from agent_run.executor_host import FakeExecutorHost
from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import parent_round_agents


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, directory in (
        ("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(key, str(tmp_path / directory))


def _checkpoint(repo: Path) -> tuple[Path, Path, dict[str, Any]]:
    fixture = write_fixture(repo / "github.json", issues={})
    agents = repo / "agents.json"
    agents.write_text(json.dumps(parent_round_agents(1, passing_last=False)))
    result = run_cli(
        repo, fixture, "run", "1", "--parent-only-paired-rounds", "1",
        "--agent-fixture", str(agents),
    )
    assert result.returncode == 2, result.stdout
    return fixture, agents, load_only_run_state(repo)


def test_budget_resume_receipt_records_explicit_window_authority(git_repo: Path) -> None:
    fixture, agents, state = _checkpoint(git_repo)
    result = run_cli(
        git_repo, fixture, "resume", state["run_id"],
        "--agent-fixture", str(agents),
    )
    output = stdout_json(result)
    assert output["action"]["resume_authorization"] == "new_budget_window"
    assert output["action_audit"]["resume_intent"]["pause_reason"] == "budget_checkpoint"
    control_path = next((git_repo / ".agent-run/task-control").glob("*.json"))
    action = json.loads(control_path.read_text())["action"]
    assert action["payload"]["resume_intent"] == output["action_audit"]["resume_intent"]
    assert load_only_run_state(git_repo)["parent_job"]["review_budget"]["window"] == 2


@pytest.mark.parametrize("drift_at", ["executor_start", "refresh"])
def test_resume_rejects_changed_pause_target_before_applying_authority(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    drift_at: str,
) -> None:
    fixture, agents, initial = _checkpoint(git_repo)
    state_path = next((git_repo / ".agent-run/runs").glob("*.json"))
    original_refresh = Controller._refresh

    class DriftingHost(FakeExecutorHost):
        def __init__(self, **kwargs):
            super().__init__(separate_process=False)

        def ensure(self, *args, **kwargs):
            if drift_at == "executor_start":
                state = json.loads(state_path.read_text())
                state["parent_job"]["blocked_reason"] = "modification_budget_exhausted"
                state["parent_job"]["review_budget"]["checkpoint_reason"] = "modification_budget_exhausted"
                state_path.write_text(json.dumps(state))
            return super().ensure(*args, **kwargs)

    def refresh(self, state, parent_number):
        refreshed = original_refresh(self, state, parent_number)
        if drift_at == "refresh":
            refreshed["parent_job"]["blocked_reason"] = "modification_budget_exhausted"
            refreshed["parent_job"]["review_budget"]["checkpoint_reason"] = "modification_budget_exhausted"
        return refreshed

    monkeypatch.setattr(cli, "FakeExecutorHost", DriftingHost)
    monkeypatch.setattr(Controller, "_refresh", refresh)
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / ".agent-run-test-state"))
    code = cli.main([
        "resume", initial["run_id"], "--github-fixture", str(fixture),
        "--agent-fixture", str(agents), "--json",
    ])
    output = capsys.readouterr()
    assert code == 2
    assert "原 Action 不能重新解释" in output.out + output.err
    final = load_only_run_state(git_repo)
    assert final["parent_job"]["review_budget"]["window"] == 1
    assert len(final["agent_invocation_history"]) == len(initial["agent_invocation_history"])


def test_repeated_pending_resume_keeps_original_intent_and_generation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture, agents, initial = _checkpoint(git_repo)
    host = FakeExecutorHost(start_outcome="unknown")
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: host)
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / ".agent-run-test-state"))
    arguments = [
        "resume", initial["run_id"], "--github-fixture", str(fixture),
        "--agent-fixture", str(agents), "--json",
    ]
    assert cli.main(arguments) == 2
    capsys.readouterr()
    control_path = next((git_repo / ".agent-run/task-control").glob("*.json"))
    first = json.loads(control_path.read_text())
    state_path = next((git_repo / ".agent-run/runs").glob("*.json"))
    state = json.loads(state_path.read_text())
    state["parent_job"]["blocked_reason"] = "modification_budget_exhausted"
    state["parent_job"]["review_budget"]["checkpoint_reason"] = "modification_budget_exhausted"
    state_path.write_text(json.dumps(state))
    assert cli.main(arguments) == 2
    capsys.readouterr()
    repeated = json.loads(control_path.read_text())
    assert repeated["action"]["action_id"] == first["action"]["action_id"]
    assert repeated["action"]["payload"] == first["action"]["payload"]
    assert repeated["executor"]["generation"] == first["executor"]["generation"]
    assert host.start_count == 1
    assert load_only_run_state(git_repo)["parent_job"]["review_budget"]["window"] == 1


def test_execution_resume_cannot_be_reinterpreted_as_budget_authority(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "agents.json"
    agents.write_text(json.dumps({
        "developments": [{"thread_id": "original", "error_after_writes": "worker exited"}],
        "reviews": [], "publications": [],
    }))
    result = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert result.returncode == 2
    initial = load_only_run_state(git_repo)
    assert initial["status"] == "execution_failed"
    original_refresh = Controller._refresh

    def refresh(self, state, parent_number):
        refreshed = original_refresh(self, state, parent_number)
        refreshed["status"] = "blocked"
        job = refreshed["parent_job"]
        job["phase"] = "blocked"
        job["pending_semantic_attempt"] = None
        job["blocked_reason"] = "review_budget_exhausted"
        job["review_budget"]["checkpoint_reason"] = "review_budget_exhausted"
        return refreshed

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(Controller, "_refresh", refresh)
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **kwargs: FakeExecutorHost())
    assert cli.main([
        "resume", initial["run_id"], "--github-fixture", str(fixture),
        "--agent-fixture", str(agents), "--json",
    ]) == 2
    assert "原 Action 不能重新解释" in capsys.readouterr().out
    final = load_only_run_state(git_repo)
    assert final["parent_job"]["review_budget"] == initial["parent_job"]["review_budget"]
    assert final["agent_invocation_history"] == initial["agent_invocation_history"]
