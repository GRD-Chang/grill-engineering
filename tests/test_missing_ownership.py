"""生命周期 CLI 不把缺失的 ownership 证据当作空 Executor 槽。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_run import cli as cli_module
from agent_run.delivery_policy import policy_snapshot_for_state
from agent_run.executor_host import ExecutorSpec, HostObservation
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.git import Publisher
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey, payload_digest
from cli_fixtures import run_agents
from conftest import seed_run, write_fixture
from test_cli import _canonical_run_budget, load_only_run_state
from test_receipt_successors import _install_host
from test_run_lifecycle import _isolated_environment
from test_ticket_194_stop_abandon import _bind_running_executor


@pytest.mark.parametrize(
    "command", ["run", "resume", "approve", "revise", "requeue", "stop", "abandon"]
)
@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("receipt_case", ["absent", "invalid", "exact"])
@pytest.mark.parametrize("host_status", ["exited", "running", "unknown", "conflict"])
def test_cli_requires_exact_ownership_before_lifecycle_admission(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    control_case: str,
    receipt_case: str,
    host_status: str,
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
        states = StateStore(git_repo / ".agent-run")
        state = states.load_run(run_id)
        assert state is not None
        receipt = dict(state["action_application_receipt"])
        if command == "run":
            receipt["payload_digest"] = payload_digest(
                {
                    "parent": 1,
                    "policy": policy_snapshot_for_state(state),
                    "profile": {"preset": None, "overrides": {}},
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
        states.save_run(run_id, state)
        path = control.path_for(task)
        if control_case == "missing":
            path.unlink()
        else:
            path.write_text("not json", encoding="utf-8")
        control_before = path.read_bytes() if path.exists() else None
        state_path = states.runs_directory / f"{run_id}.json"
        state_before = state_path.read_bytes()
        fixture_before = fixture.read_bytes()
        agents_before = agents.read_bytes()
        host = _install_host(git_repo, fixture, monkeypatch, host_status)
        monkeypatch.setattr(
            cli_module,
            "GhGitHubReader",
            lambda _repo=None, *, working_directory: FixtureGitHubReader(fixture),
        )
        prepared: list[object] = []
        observed: list[ExecutorSpec] = []
        monkeypatch.setattr(host, "prepare_environment", prepared.append)

        def observe(spec: ExecutorSpec, _store: TaskControlStore) -> HostObservation:
            observed.append(spec)
            assert spec.action_id == receipt["action_id"]
            assert spec.generation == receipt["executor_generation"]
            assert spec.run_id == run_id
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                spec.generation,
                None,
                False,
                runner_binding="b" * 16 if host_status == "exited" else None,
            )

        monkeypatch.setattr(host, "observe", observe)
        arguments = [command, "1", "--repo", "example/project", "--json"]
        if command not in {"stop", "abandon"}:
            arguments.extend(["--agent-fixture", str(agents)])
        if command == "revise":
            arguments.extend(["--message", "exact revision"])
        proven_exited = receipt_case == "exact" and host_status == "exited"
        if proven_exited:
            worker.kill()
            worker.wait(timeout=3)
        for attempt in range(2):
            code = cli_module.main(arguments)
            output = json.loads(capsys.readouterr().out)
            if proven_exited and command == "run":
                reconciled_state = states.load_run(run_id)
                assert reconciled_state is not None
                assert reconciled_state["status"] == "execution_failed"
                assert reconciled_state["action_application_receipt"] == receipt
                assert (
                    reconciled_state["diagnostics"][-1]["code"] == "session_interrupted"
                )
            else:
                assert state_path.read_bytes() == state_before
            assert fixture.read_bytes() == fixture_before
            assert agents.read_bytes() == agents_before
            if not proven_exited:
                assert code == 2, output
                assert host.start_count == 0
                assert prepared == []
                assert worker.poll() is None
                if receipt_case != "exact":
                    assert observed == []
                    assert (
                        path.read_bytes() if path.exists() else None
                    ) == control_before
                else:
                    recovered = control.load(task)
                    assert recovered is not None
                    assert recovered["action"]["action_id"] == receipt["action_id"]
                    assert (
                        recovered["next_generation"]
                        == receipt["executor_generation"] + 1
                    )
            else:
                recovered = control.load(task)
                assert recovered is not None
                if command in {"run", "stop"}:
                    assert host.start_count == 0, output
                    assert recovered["action"]["action_id"] == receipt["action_id"]
                    assert recovered["action"]["status"] == (
                        "failed" if command == "run" else "completed"
                    )
                else:
                    assert host.start_count == 1, output
                    assert len(prepared) == 1
                    assert recovered["action"]["kind"] == command
                    assert (
                        recovered["action"]["executor_generation"]
                        == receipt["executor_generation"] + 1
                    )
                    assert (
                        sum(
                            entry["action"]["action_id"] == receipt["action_id"]
                            for entry in recovered["action_history"]
                        )
                        == 1
                    )
                assert worker.poll() is not None
            after = path.read_bytes() if path.exists() else None
            if attempt == 0:
                after_first = after
                state_after_first = state_path.read_bytes()
            else:
                assert after == after_first
                assert state_path.read_bytes() == state_after_first
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
        git_repo / ".agent-run"
        if state_case == "canonical"
        else git_repo.parent / "custom-state"
    )
    assert (
        seed_run(git_repo, fixture, "1", "--state-dir", str(state_root)).returncode == 0
    )
    states = StateStore(state_root)
    state = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(state["run_id"])
    control = TaskControlStore(git_repo / ".agent-run")
    task = TaskKey(git_repo, "example/project", 1)
    control.claim_action(task, kind="run", payload={"parent": 1})
    worker = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    try:
        state["status"] = status
        state["terminal_kind"] = "operator_stopped" if command == "stop" else None
        state.pop("action_application_receipt", None)
        states.save_run(run_id, state)
        path = control.path_for(task)
        if control_case == "missing":
            path.unlink()
        else:
            path.write_text("not json", encoding="utf-8")
        before_control = path.read_bytes() if path.exists() else None
        state_path = states.runs_directory / f"{run_id}.json"
        before_state = state_path.read_bytes()
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
            assert output["diagnostics"][0]["code"] == "task_control", output
            assert host.start_count == 0
            assert prepared == []
            assert published == []
            assert worker.poll() is None
            assert state_path.read_bytes() == before_state
            assert fixture.read_bytes() == before_fixture
            assert (path.read_bytes() if path.exists() else None) == before_control
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)
