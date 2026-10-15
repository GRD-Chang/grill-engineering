"""生命周期 CLI 不把缺失的 ownership 证据当作空 Executor 槽。"""

from __future__ import annotations

from support.control_state import (
    corrupt_control_fixture, delete_control_fixture, raw_control_snapshot,
)

import json
import subprocess
import sys
from pathlib import Path

import pytest

from support.workspace import managed_repo, managed_state

from agent_run import cli as cli_module
from agent_run.delivery_policy import policy_snapshot_for_state
from agent_run.ci_recovery_budget import new_ci_recovery_budget
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.git import Publisher
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey, payload_digest
from support.cli_fixtures import run_agents
from support.seeding import seed_run, write_fixture
from support.cli import load_only_run_state
from support.run_artifacts import _canonical_run_budget
from support.receipt_host import _install_host
from support.lifecycle import _isolated_environment
from support.control import _bind_running_executor

from support.state import snapshot_run
from support.state import persist_run_fixture


@pytest.mark.parametrize(
    "command", ["run", "resume", "approve", "revise", "requeue", "stop", "abandon"]
)
@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("receipt_case", ["absent", "invalid", "exact"])
def test_cli_requires_exact_ownership_before_lifecycle_admission(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    control_case: str,
    receipt_case: str,
) -> None:
    for key, value in _isolated_environment(git_repo.parent / "user-env").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(git_repo.parent / "runtime"))
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = run_agents(git_repo / "agents.json")
    assert seed_run(git_repo, fixture).returncode == 0
    state = load_only_run_state(git_repo)
    run_id = str(state["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    try:
        states = StateStore(managed_state(git_repo))
        state = states.load_run(run_id)
        assert state is not None
        receipt = dict(state["action_application_receipt"])
        if command == "run":
            receipt["payload_digest"] = payload_digest(
                {
                    "parent": 1,
                    "policy": policy_snapshot_for_state(state),
                    "profile": {"preset": None, "overrides": {}},
                    "github_identity": state["github_identity"],
                    "runner_github_identity": state["runner_github_identity"],
                }
            )
        if command in {"resume", "approve", "revise", "requeue"}:
            state["status"] = {
                "resume": "execution_failed",
                "approve": "run_approval_pending",
                "revise": "ready_for_human",
                "requeue": "requeue_required",
            }[command]
            state["terminal_kind"] = "execution_failed" if command == "resume" else None
        if command == "requeue":
            state["parent_job"] = {
                "parent_generation": 1,
                "phase": "pending",
                "review_budget": _canonical_run_budget(),
                "review_budget_history": [],
                "ci_recovery": new_ci_recovery_budget(),
            }
            state["requeue_required"] = {
                "work_subject": f"parent-only:{run_id}",
                "generation": 1,
                "reason": "parent_requirements_changed",
            }
            state["diagnostics"] = [
                {"code": "parent_requirements_changed", "message": "changed"}
            ]
        if receipt_case == "absent":
            state.pop("action_application_receipt", None)
        else:
            state["action_application_receipt"] = receipt
            if receipt_case == "invalid":
                # Syntactically valid evidence referring to a different Run.
                receipt["run_id"] = "run-unrelated"
        persist_run_fixture(states, run_id, state)
        if control_case == "missing":
            delete_control_fixture(control, task)
        else:
            corrupt_control_fixture(control, task)
        control_before = raw_control_snapshot(control, task)
        state_ref = (states.root, str(run_id))
        state_before = snapshot_run(state_ref)
        fixture_before = fixture.read_bytes()
        agents_before = agents.read_bytes()
        host = _install_host(git_repo, fixture, monkeypatch, "exited")
        monkeypatch.setattr(
            cli_module,
            "GhGitHubReader",
            lambda _repo=None, *, working_directory: FixtureGitHubReader(fixture),
        )
        prepared: list[object] = []
        monkeypatch.setattr(host, "prepare_environment", prepared.append)

        def observe(*_args: object) -> None:
            pytest.fail("丢失控制权威时，业务 receipt 不能授权 Host 对账")

        monkeypatch.setattr(host, "observe", observe)
        arguments = [command, "1", "--repo", "example/project", "--json"]
        if command not in {"stop", "abandon"}:
            arguments.extend(["--agent-fixture", str(agents)])
        if command == "revise":
            arguments.extend(["--message", "exact revision"])
        for _attempt in range(2):
            code = cli_module.main(arguments)
            output = json.loads(capsys.readouterr().out)
            assert code == 2, output
            assert snapshot_run(state_ref) == state_before
            assert fixture.read_bytes() == fixture_before
            assert agents.read_bytes() == agents_before
            assert host.start_count == 0
            assert prepared == []
            assert worker.poll() is None
            assert raw_control_snapshot(control, task) == control_before
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


@pytest.mark.parametrize("state_case", ["canonical", "custom"])
@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize(
    "command,status", [("stop", "operator_stopped"), ("run", "run_approval_pending")]
)
def test_missing_receipt_does_not_make_stop_noop_or_refresh_approval(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_case: str,
    state_case: str,
    command: str,
    status: str,
) -> None:
    for key, value in _isolated_environment(git_repo.parent / "user-env").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(git_repo.parent / "runtime"))
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = run_agents(git_repo / "agents.json")
    state_root = (
        managed_state(git_repo)
        if state_case == "canonical"
        else git_repo.parent / "custom-state"
    )
    assert (
        seed_run(git_repo, fixture, "1", "--state-dir", str(state_root)).returncode == 0
    )
    states = StateStore(state_root)
    state = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(state["run_id"])
    control = TaskControlStore(managed_state(git_repo))
    task = TaskKey(managed_repo(git_repo), "example/project", 1)
    control.claim_action(task, kind="run", payload={"parent": 1})
    worker = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    try:
        state["status"] = status
        state["terminal_kind"] = "operator_stopped" if command == "stop" else None
        state.pop("action_application_receipt", None)
        persist_run_fixture(states, run_id, state)
        if control_case == "missing":
            delete_control_fixture(control, task)
        else:
            corrupt_control_fixture(control, task)
        before_control = raw_control_snapshot(control, task)
        state_ref = (states.root, str(run_id))
        before_state = snapshot_run(state_ref)
        before_fixture = fixture.read_bytes()
        host = _install_host(git_repo, fixture, monkeypatch, "exited")
        monkeypatch.setattr(
            cli_module,
            "GhGitHubReader",
            lambda _repo=None, *, working_directory: FixtureGitHubReader(fixture),
        )
        published: list[tuple[str, str | None]] = []
        original_resolve = Publisher.resolve_base

        def resolve_base(publisher: Publisher, branch: str, sha: str | None) -> str:
            published.append((branch, sha))
            return original_resolve(publisher, branch, sha)

        monkeypatch.setattr(Publisher, "resolve_base", resolve_base)
        prepared: list[object] = []
        monkeypatch.setattr(host, "prepare_environment", prepared.append)
        arguments = [command, "1", "--repo", "example/project", "--json"]
        if command not in {"stop", "abandon"}:
            arguments.extend(["--agent-fixture", str(agents)])
        if command == "stop" and state_case == "custom":
            arguments.extend(["--state-dir", str(state_root)])
        # Ordinary run must discover its custom directory through the locator.
        for _ in range(2):
            code = cli_module.main(arguments)
            output = json.loads(capsys.readouterr().out)
            assert code == 2, output
            if command == "stop" and state_case == "custom":
                assert output["diagnostics"][0]["code"] == "command_failed", output
                assert "统一状态目录" in output["diagnostics"][0]["message"]
            else:
                assert output["diagnostics"][0]["code"] == "task_control", output
            assert host.start_count == 0
            assert prepared == []
            assert published == []
            assert worker.poll() is None
            assert snapshot_run(state_ref) == before_state
            assert fixture.read_bytes() == before_fixture
            assert raw_control_snapshot(control, task) == before_control
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)
