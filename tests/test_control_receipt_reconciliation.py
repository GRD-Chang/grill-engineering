from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run import cli as cli_module
from agent_run.executor_host import ExecutorSpec, FakeExecutorHost, HostObservation
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore
from support.cli_payloads import ticket
from support.control import _bind_running_executor
from support.control_state import (
    corrupt_control_fixture,
    delete_control_fixture,
    raw_control_snapshot,
)
from support.lifecycle import _isolated_environment
from support.seeding import seed_run, write_fixture
from support.state import snapshot_run
from support.workspace import managed_state


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("host_status", ["running", "exited", "unknown", "conflict"])
@pytest.mark.parametrize("kind", ["stop", "abandon"])
def test_control_command_rejects_untrusted_control_before_host_observation(
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
    generation = previous["executor"]["generation"]
    state_ref = (states.root, str(run_id))
    state_before = snapshot_run(state_ref)
    fixture_before = fixture.read_bytes()
    if control_case == "missing":
        delete_control_fixture(control, task)
    else:
        corrupt_control_fixture(control, task)
    control_before = raw_control_snapshot(control, task)

    class ReceiptHost(FakeExecutorHost):
        observe_count = 0

        def observe(self, spec: ExecutorSpec, store: TaskControlStore) -> HostObservation:
            self.observe_count += 1
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
        for _attempt in range(2):
            result = cli_module.main(command)
            output = json.loads(capsys.readouterr().out)
            assert result == 2, output
            assert host.start_count == host.observe_count == 0
            assert worker.poll() is None
            assert raw_control_snapshot(control, task) == control_before
            assert snapshot_run(state_ref) == state_before
            assert fixture.read_bytes() == fixture_before
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("kind", ["stop", "abandon"])
def test_receipt_after_crash_cannot_recreate_control_authority(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_case: str,
    kind: str,
) -> None:
    from support.control import _ControlReceiptHost

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

        def observe(self, spec: ExecutorSpec, store: TaskControlStore) -> HostObservation:
            if spec.generation == 1:
                return super().observe(spec, store)
            return HostObservation(
                "exited", spec.generation, None, False, runner_binding="b" * 16,
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
        assert crashed["action_application_receipt"]["kind"] == kind
        assert worker.poll() is None
        assert host.ensure_count == 1
        if control_case == "missing":
            delete_control_fixture(control, task)
        else:
            corrupt_control_fixture(control, task)
        control_before = raw_control_snapshot(control, task)
        run_before = snapshot_run((states.root, str(run_id)))
        for _attempt in range(2):
            assert cli_module.main(command) == 2
            capsys.readouterr()
            assert host.ensure_count == 1
            assert worker.poll() is None
            assert raw_control_snapshot(control, task) == control_before
            assert snapshot_run((states.root, str(run_id))) == run_before
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)
