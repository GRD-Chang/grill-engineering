"""Production CLI admission with unavailable Control and a durable Run receipt."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

import agent_run.cli as cli_module
from agent_run.executor_host import (
    ExecutorSpec,
    ExecutorStartUnknownError,
    HostObservation,
)
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state_contract import require_current_run_state
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey, payload_digest
from conftest import seed_run, write_fixture
from test_cli import _canonical_run_budget, load_only_run_state


@pytest.fixture(autouse=True)
def isolated_environment(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_run_lifecycle import _isolated_environment

    for key, value in _isolated_environment(git_repo.parent / "home-env").items():
        monkeypatch.setenv(key, value)


def _install_host(
    git_repo: Path,
    fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_status: str,
    *,
    launch_unknown: bool = True,
) -> cli_module.FakeExecutorHost:
    class UsageLease:
        def fileno(self) -> int:
            return 1

    class ReceiptHost(cli_module.FakeExecutorHost):
        def __init__(self) -> None:
            super().__init__(start_outcome="unknown" if launch_unknown else "accepted")
            self.observed: list[ExecutorSpec] = []

        def check_readiness(self) -> None:
            pass

        def prepare_environment(self, _arguments: object) -> None:
            pass

        def observe(
            self, spec: ExecutorSpec, _control: TaskControlStore
        ) -> HostObservation:
            self.observed.append(spec)
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                spec.generation,
                None,
                False,
                runner_binding="0" * 16 if host_status == "exited" else None,
            )

        def ensure(
            self,
            spec: ExecutorSpec,
            control: TaskControlStore,
            execute: Callable[[], Mapping[str, Any]] | None = None,
            recover: bool = False,
        ) -> HostObservation:
            # The matrix loses the successor launch response after admission.
            # The smoke tests run the real Executor callback to completion.
            result = super().ensure(spec, control, execute=execute, recover=recover)
            if launch_unknown:
                raise ExecutorStartUnknownError("successor launch response lost")
            return result

    host = ReceiptHost()
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(git_repo / "runtime"))
    monkeypatch.setattr(
        cli_module, "runner_usage_lease", lambda _path: nullcontext(UsageLease())
    )
    monkeypatch.setattr(cli_module, "_running_active_runner", lambda: True)
    monkeypatch.setattr(cli_module, "SystemdUserExecutorHost", lambda **_options: host)
    monkeypatch.setattr(
        cli_module,
        "GhGitHubReader",
        lambda _repo, *, working_directory: FixtureGitHubReader(fixture),
    )
    monkeypatch.setattr(
        cli_module,
        "GhGitHubPublisher",
        lambda _repo, git: FixtureGitHubPublisher(fixture, git),
    )
    return host


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("host_status", ["exited", "unknown", "conflict"])
@pytest.mark.parametrize("command", ["approve", "revise", "requeue"])
@pytest.mark.parametrize("intent", ["repeat", "repeat_ready", "successor"])
def test_dedicated_actions_reconcile_exact_receipt_at_cli_boundary(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_case: str,
    host_status: str,
    command: str,
    intent: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    state = load_only_run_state(git_repo)
    state_root = git_repo / ".agent-run"
    run_id = str(state["run_id"])
    control = TaskControlStore(state_root)
    task = TaskKey(git_repo, "example/project", 1)
    original = control.claim_action(task, kind="run", payload={"parent": 1})
    prepare_action_application_receipt(state, original.action)
    state.update({"status": "execution_failed", "terminal_kind": "execution_failed"})
    receipt = state["action_application_receipt"]
    payload: dict[str, Any] = {"parent": 1, "run_id": run_id}
    if command == "revise":
        payload["message"] = "apply this exact revision"
    if intent.startswith("repeat"):
        # Model a durable application followed by an interrupted automatic
        # interval: the original command is no longer a legal new intent.
        receipt.update({"kind": command, "payload_digest": payload_digest(payload)})
    if intent != "repeat":
        state["status"] = {
            "approve": "run_approval_pending",
            "revise": "ready_for_human",
            "requeue": "requeue_required",
        }[command]
        state["terminal_kind"] = None
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
                {
                    "code": "parent_requirements_changed",
                    "message": "requirements changed",
                }
            ]
    require_current_run_state(state)
    StateStore(state_root).save_run(run_id, state)
    old_generation = receipt["executor_generation"]
    control_path = control.path_for(task)
    if control_case == "missing":
        control_path.unlink()
    else:
        control_path.write_text("not json", encoding="utf-8")

    host = _install_host(git_repo, fixture, monkeypatch, host_status)
    arguments = [command, run_id, "--repo", "example/project", "--json"]
    if command == "revise":
        arguments.extend(["--message", "apply this exact revision"])
    state_path = state_root / "runs" / f"{run_id}.json"
    state_before = state_path.read_bytes()
    fixture_before = fixture.read_bytes()
    control_before = control_path.read_bytes() if control_path.exists() else None

    # Ordinary run cannot turn the dedicated boundary into new authorization.
    if intent == "successor":
        cli_module.main(["run", "1", "--repo", "example/project", "--json"])
        capsys.readouterr()
        assert host.start_count == 0
        assert state_path.read_bytes() == state_before
        # A read-only refusal may leave the missing/corrupt evidence intact.
        assert (
            control_path.read_bytes() if control_path.exists() else None
        ) == control_before

    code = cli_module.main(arguments)
    output = json.loads(capsys.readouterr().out)
    assert host.observed, output
    assert all(
        spec.action_id == receipt["action_id"] and spec.generation == old_generation
        for spec in host.observed
    )
    assert state_path.read_bytes() == state_before
    assert fixture.read_bytes() == fixture_before
    repaired = control.load(task)
    assert repaired is not None
    if host_status != "exited":
        assert code == 2, output
        assert host.start_count == 0
        assert repaired["action"]["action_id"] == receipt["action_id"]
        assert repaired["next_generation"] == old_generation + 1
        before_retry = control_path.read_bytes()
        assert cli_module.main(arguments) == 2
        capsys.readouterr()
        assert host.start_count == 0
        assert len(host.observed) == 2
        assert control_path.read_bytes() == before_retry
        assert state_path.read_bytes() == state_before
        assert fixture.read_bytes() == fixture_before
        return

    if intent.startswith("repeat"):
        assert code == (
            0 if intent == "repeat_ready" and command == "approve" else 2
        ), output
        assert output["action"]["submission"] == "attached"
        assert host.start_count == 0
        assert repaired["action"]["action_id"] == receipt["action_id"]
        assert repaired["action"]["status"] == "completed"
    else:
        assert code == 2, output
        assert host.start_count == 1
        assert repaired["action"]["kind"] == command
        assert repaired["action"]["executor_generation"] == old_generation + 1
        assert repaired["action"]["payload"] == payload
        predecessors = [
            entry["action"]
            for entry in repaired["action_history"]
            if entry["action"]["action_id"] == receipt["action_id"]
        ]
        assert len(predecessors) == 1
        assert predecessors[0]["status"] == "completed"
    before_retry = control_path.read_bytes()
    assert cli_module.main(arguments) == (
        0 if intent == "repeat_ready" and command == "approve" else 2
    )
    capsys.readouterr()
    assert host.start_count == (1 if intent == "successor" else 0)
    assert control_path.read_bytes() == before_retry
    assert state_path.read_bytes() == state_before
    assert fixture.read_bytes() == fixture_before


@pytest.mark.parametrize("command", ["approve", "revise", "requeue"])
def test_recovered_receipt_successor_executes_the_real_cli_action(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    from cli_fixtures import run_agents
    from test_cli import run_cli
    from test_cli_delivery import ticket
    from test_ticket_192_cli_actions import _parent_agents, _revision_agents

    fixture = write_fixture(
        git_repo / "github.json", issues={} if command == "requeue" else {"3": ticket()}
    )
    agents = (
        _parent_agents(git_repo / "agents.json")
        if command == "requeue"
        else run_agents(git_repo / "agents.json")
    )
    started = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert started.returncode == 0, started.stdout
    if command == "requeue":
        data = json.loads(fixture.read_text(encoding="utf-8"))
        data["parent"]["body"] = "Changed parent requirement."
        fixture.write_text(json.dumps(data), encoding="utf-8")
        stale = run_cli(git_repo, fixture, "approve", "1")
        assert stale.returncode == 2, stale.stdout
        assert load_only_run_state(git_repo)["status"] == "requeue_required"
    elif command == "revise":
        agents = _revision_agents(git_repo / "revision-agents.json")
    state = load_only_run_state(git_repo)
    receipt = state["action_application_receipt"]
    control = TaskControlStore(git_repo / ".agent-run")
    task = TaskKey(git_repo, "example/project", 1)
    control.path_for(task).write_text("not json", encoding="utf-8")
    host = _install_host(git_repo, fixture, monkeypatch, "exited", launch_unknown=False)
    arguments = [
        command,
        str(state["run_id"]),
        "--repo",
        "example/project",
        "--agent-fixture",
        str(agents),
        "--json",
    ]
    if command == "revise":
        arguments.extend(["--message", "apply this exact revision"])
    code = cli_module.main(arguments)
    output = json.loads(capsys.readouterr().out)
    assert code == 0, output
    assert output["action"]["submission"] == "started"
    assert host.start_count == 1
    durable = load_only_run_state(git_repo)
    assert durable["action_application_receipt"]["kind"] == command
    assert (
        durable["action_application_receipt"]["executor_generation"]
        == receipt["executor_generation"] + 1
    )
    assert (
        durable["status"]
        == {
            "approve": "completed",
            "revise": "run_approval_pending",
            "requeue": "parent_approval_pending",
        }[command]
    )
    repaired = control.load(task)
    assert repaired is not None
    assert (
        len(
            [
                entry
                for entry in repaired["action_history"]
                if entry["action"]["action_id"] == receipt["action_id"]
            ]
        )
        == 1
    )
    # A repeat after successor application only returns its receipt when the
    # resulting state no longer grants this same command a new intent.
    if command != "revise":
        state_before = json.dumps(durable, sort_keys=True)
        fixture_before = fixture.read_bytes()
        assert cli_module.main(arguments) == 0
        capsys.readouterr()
        assert host.start_count == 1
        assert json.dumps(load_only_run_state(git_repo), sort_keys=True) == state_before
        assert fixture.read_bytes() == fixture_before


@pytest.mark.parametrize("original_command", ["approve", "revise", "requeue"])
def test_original_retry_unknown_then_different_successor_reconciles_again(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    original_command: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    state = load_only_run_state(git_repo)
    run_id = str(state["run_id"])
    payload = {"parent": 1, "run_id": run_id}
    if original_command == "revise":
        payload["message"] = "original revision"
    control = TaskControlStore(git_repo / ".agent-run")
    task = TaskKey(git_repo, "example/project", 1)
    original = control.claim_action(task, kind=original_command, payload=payload)
    prepare_action_application_receipt(state, original.action)
    state.update({"status": "run_approval_pending", "terminal_kind": None})
    StateStore(git_repo / ".agent-run").save_run(run_id, state)
    control.path_for(task).unlink()
    first_host = _install_host(git_repo, fixture, monkeypatch, "unknown")
    arguments = [original_command, run_id, "--repo", "example/project", "--json"]
    if original_command == "revise":
        arguments.extend(["--message", "original revision"])
    assert cli_module.main(arguments) == 2
    capsys.readouterr()
    assert first_host.start_count == 0
    recovered = control.load(task)
    assert recovered is not None
    assert recovered["action"]["payload"] == payload
    assert recovered["action"]["action_id"] == original.action["action_id"]
    successor = "approve" if original_command == "revise" else "revise"
    successor_arguments = [successor, run_id, "--repo", "example/project", "--json"]
    if successor == "revise":
        successor_arguments.extend(["--message", "authorized next revision"])
    host = _install_host(git_repo, fixture, monkeypatch, "exited")
    assert cli_module.main(successor_arguments) == 2
    output = json.loads(capsys.readouterr().out)
    assert host.start_count == 1, output
    repaired = control.load(task)
    assert repaired is not None
    assert repaired["action"]["kind"] == successor
    assert (
        repaired["action"]["executor_generation"]
        == original.action["executor_generation"] + 1
    )
    before_retry = control.path_for(task).read_bytes()
    assert cli_module.main(successor_arguments) == 2
    capsys.readouterr()
    assert host.start_count == 1
    assert control.path_for(task).read_bytes() == before_retry
