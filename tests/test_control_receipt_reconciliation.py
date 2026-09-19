from __future__ import annotations

import json
from pathlib import Path

import pytest

from support.workspace import managed_state

from agent_run import cli as cli_module
from agent_run.executor_host import ExecutorSpec, FakeExecutorHost, HostObservation
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore
from conftest import seed_run, write_fixture
from test_run_lifecycle import _isolated_environment, ticket
from test_ticket_194_stop_abandon import _bind_running_executor


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("host_status", ["running", "exited", "unknown", "conflict"])
@pytest.mark.parametrize("kind", ["stop", "abandon"])
def test_control_command_reconciles_receipt_before_admission(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_case: str,
    host_status: str,
    kind: str,
) -> None:
    for key, value in _isolated_environment(git_repo.parent / "user-env").items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(managed_state(git_repo))
    state = states.find_unfinished_runs("example/project", 1)[0]
    run_id = state["run_id"]
    control, task, worker = _bind_running_executor(git_repo, run_id)
    previous = control.load(task)
    assert previous is not None
    action_id = previous["action"]["action_id"]
    generation = previous["executor"]["generation"]
    state_path = states.runs_directory / f"{run_id}.json"
    state_before = state_path.read_bytes()
    fixture_before = fixture.read_bytes()
    path = control.path_for(task)
    if control_case == "missing":
        path.unlink()
    else:
        path.write_text("not json", encoding="utf-8")

    class ReceiptHost(FakeExecutorHost):
        def __init__(self) -> None:
            super().__init__()
            self.observe_count = 0

        def observe(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            self.observe_count += 1
            assert spec.action_id == action_id
            assert spec.generation == generation
            assert spec.run_id == run_id
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                generation,
                None,
                False,
                runner_binding="b" * 16 if host_status == "exited" else None,
            )

    host = ReceiptHost()
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(cli_module, "FakeExecutorHost", lambda **_options: host)
    command = [kind, "1", "--github-fixture", str(fixture), "--json"]
    try:
        if host_status == "exited":
            # This observation represents the accurate old execution ending.
            worker.kill()
            worker.wait(timeout=3)
        for attempt in range(2):
            result = cli_module.main(command)
            output = json.loads(capsys.readouterr().out)
            repaired = control.load(task)
            assert repaired is not None
            if host_status != "exited":
                assert result == 2, output
                assert host.start_count == 0
                assert worker.poll() is None
                assert repaired["action"]["action_id"] == action_id
                assert repaired["next_generation"] == generation + 1
                assert state_path.read_bytes() == state_before
                assert fixture.read_bytes() == fixture_before
                assert host.observe_count == attempt + 1
            else:
                assert result == 0, output
                assert host.start_count == 1
                assert repaired["action"]["kind"] == kind
                assert repaired["action"]["executor_generation"] == generation + 1
                assert states.load_run(run_id)["status"] == (
                    "operator_stopped" if kind == "stop" else "abandoned"
                )
                assert fixture.read_bytes() == fixture_before
                predecessors = [
                    entry["action"]
                    for entry in repaired["action_history"]
                    if entry["action"]["action_id"] == action_id
                ]
                assert len(predecessors) == 1
                assert predecessors[0]["status"] == "completed"
            if attempt == 0:
                after_first = path.read_bytes()
            else:
                assert path.read_bytes() == after_first
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("kind", ["stop", "abandon"])
def test_control_receipt_crash_before_business_effect_preserves_the_intent(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_case: str,
    kind: str,
) -> None:
    from test_ticket_194_stop_abandon import _ControlReceiptHost

    for key, value in _isolated_environment(git_repo.parent / "user-env").items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(managed_state(git_repo))
    state = states.find_unfinished_runs("example/project", 1)[0]
    run_id = state["run_id"]
    control, task, worker = _bind_running_executor(git_repo, run_id)

    class CrashRecoveryHost(_ControlReceiptHost):
        ensure_count = 0

        def ensure(self, *args: object, **kwargs: object) -> HostObservation:
            self.ensure_count += 1
            return super().ensure(*args, **kwargs)  # type: ignore[arg-type]

        def observe(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            if spec.generation == 1:
                return super().observe(spec, store)
            return HostObservation(
                "exited", spec.generation, None, False, runner_binding="b" * 16
            )

        def terminate_control_target(self, target: object, **kwargs: object) -> None:
            if worker.poll() is None:
                super().terminate_control_target(target, **kwargs)  # type: ignore[arg-type]
                worker.wait(timeout=3)

    host = CrashRecoveryHost()
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(cli_module, "FakeExecutorHost", lambda **_options: host)
    command = [kind, "1", "--github-fixture", str(fixture), "--json"]
    try:
        assert cli_module.main([*command, "--crash-after-save", "1"]) == 2
        capsys.readouterr()
        crashed = states.load_run(run_id)
        assert crashed["status"] == state["status"]
        original = crashed["action_application_receipt"]
        assert original["kind"] == kind
        assert worker.poll() is None
        path = control.path_for(task)
        if control_case == "missing":
            path.unlink()
        else:
            path.write_text("not json", encoding="utf-8")
        assert cli_module.main(command) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == (
            "operator_stopped" if kind == "stop" else "abandoned"
        )
        assert worker.poll() is not None
        recovered = control.load(task)
        assert recovered is not None
        assert recovered["action"]["kind"] == kind
        assert recovered["action"]["status"] == "completed"
        assert (
            recovered["action"]["executor_generation"]
            == original["executor_generation"] + 1
        )
        predecessors = [
            entry["action"]
            for entry in recovered["action_history"]
            if entry["action"]["action_id"] == original["action_id"]
        ]
        assert len(predecessors) == 1
        assert predecessors[0]["status"] == "failed"
        assert host.ensure_count == 2
        durable = path.read_bytes()
        assert cli_module.main(command) == 0
        capsys.readouterr()
        assert host.ensure_count == 2
        assert path.read_bytes() == durable
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)
