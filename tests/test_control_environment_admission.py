from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import seed_idle_control, seed_run, write_fixture
from test_ticket_194_stop_abandon import (
    _bind_running_executor,
    _running_executor,
    _control_lifecycle,
)
from test_receipt_successors import _install_host
from test_run_lifecycle import _isolated_environment
from agent_run import cli as cli_module
from agent_run.executor_host import ExecutorSpec
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_lifecycle import LifecycleRequest, prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.systemd_executor_host import (
    FakeSystemdTransport,
    SystemdUnitObservation,
    SystemdUserExecutorHost,
)
from agent_run.task_control import (
    ActionBusyError,
    ActionReconciliationError,
    TaskControlStore,
    TaskKey,
)


@pytest.mark.parametrize(
    "kind,active",
    [("stop", True), ("abandon", True), ("abandon", False), ("stop", False)],
)
def test_control_environment_rejected_before_ownership_changes(
    git_repo: Path,
    tmp_path: Path,
    kind: str,
    active: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    request: pytest.FixtureRequest,
) -> None:
    for key, value in _isolated_environment(tmp_path / "observer").items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    worker = None
    if active:
        control, task, worker = _bind_running_executor(git_repo, run_id)

        def cleanup_worker() -> None:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=3)

        request.addfinalizer(cleanup_worker)
    else:
        control = TaskControlStore(states.root)
        task = TaskKey(git_repo, "example/project", 1)
        seed_idle_control(control, task, run_id, state_dir=states.root)
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "x" * 1024},
        executor_python=Path("/usr/bin/python3"),
        max_environment_bytes=128,
    )
    _install_host(git_repo, fixture, monkeypatch, "running")
    monkeypatch.setattr(cli_module, "SystemdUserExecutorHost", lambda **_options: host)
    monkeypatch.setattr(
        cli_module,
        "GhGitHubReader",
        lambda *args, **kwargs: FixtureGitHubReader(fixture),
    )
    record = control.load(task)
    assert record is not None
    if active:
        executor = record["executor"]
        spec = ExecutorSpec(
            task=task,
            run_id=run_id,
            action_id=executor["action_id"],
            generation=executor["generation"],
            runner_binding=executor["runner_binding"],
            state_root=states.root,
        )
        transport.unit = SystemdUnitObservation(
            "running",
            host._description(spec),
            executor["pid"],
            executor["process_start_token"],
        )
    state_path = states.runs_directory / f"{run_id}.json"
    before_state = state_path.read_bytes()
    before_control = control.path_for(task).read_bytes()
    before_fixture = fixture.read_bytes()
    for _ in range(2):
        code = cli_module.main([kind, "1", "--json"])
        output = json.loads(capsys.readouterr().out)
        assert code == 2, output
        assert "发起终端环境过大" in json.dumps(output, ensure_ascii=False), output
        assert control.path_for(task).read_bytes() == before_control
        assert state_path.read_bytes() == before_state
        assert fixture.read_bytes() == before_fixture
        assert transport.start_count == 0
        assert not list((tmp_path / "runtime").glob("environment-*.json"))
        if worker is not None:
            assert worker.poll() is None


@pytest.mark.parametrize("kind", ["stop", "abandon"])
def test_control_admission_rechecks_after_environment_preparation(
    tmp_path: Path, kind: str
) -> None:
    control, task, _ = _running_executor(tmp_path)
    competing = None
    other_kind = "abandon" if kind == "stop" else "stop"

    def prepare() -> None:
        nonlocal competing
        # A second store can acquire the lock: preparation is outside the transaction.
        other = TaskControlStore(control.root)
        competing = other.claim_control_action(
            task,
            kind=other_kind,
            payload={"parent": 194},
            run_id="run-194",
            state_dir=tmp_path / "state",
        )

    with pytest.raises(ActionBusyError):
        control.claim_control_action(
            task,
            kind=kind,
            payload={"parent": 194},
            run_id="run-194",
            state_dir=tmp_path / "state",
            before_create=prepare,
        )
    assert competing is not None
    final = control.load(task)
    assert final["action"]["action_id"] == competing.action_id
    assert final["next_generation"] == 3


@pytest.mark.parametrize("kind", ["stop", "abandon"])
def test_identical_control_attach_does_not_prepare_observer_environment(
    tmp_path: Path, kind: str
) -> None:
    control, task, _ = _running_executor(tmp_path)
    first = control.claim_control_action(
        task,
        kind=kind,
        payload={"parent": 194},
        run_id="run-194",
        state_dir=tmp_path / "state",
    )
    before = control.path_for(task).read_bytes()
    duplicate = control.claim_control_action(
        task,
        kind=kind,
        payload={"parent": 194},
        run_id="run-194",
        state_dir=tmp_path / "state",
        before_create=lambda: pytest.fail(
            "attach must not capture observer environment"
        ),
    )
    assert duplicate.attached
    assert duplicate.action_id == first.action_id
    assert control.path_for(task).read_bytes() == before


@pytest.mark.parametrize("kind", ["stop", "abandon"])
def test_running_control_attaches_with_oversize_observer_environment(
    git_repo: Path,
    tmp_path: Path,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    for key, value in _isolated_environment(tmp_path / "observer").items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)

    def cleanup_worker() -> None:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)

    request.addfinalizer(cleanup_worker)
    transport = FakeSystemdTransport()
    normal_host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PROJECT_SETTING": "preserved"},
        executor_python=Path("/usr/bin/python3"),
    )
    before = control.path_for(task).read_bytes()
    prepared = []

    def prepare() -> None:
        assert control.path_for(task).read_bytes() == before
        normal_host.prepare_environment((kind, "1"))
        prepared.append(True)

    payload = {"parent": 1, "run_id": run_id}
    first = control.claim_control_action(
        task,
        kind=kind,
        payload=payload,
        run_id=run_id,
        state_dir=states.root,
        before_create=prepare,
    )
    assert prepared == [True]
    assert first is not None
    reservation = control.begin_executor(
        task, action_id=first.action_id, run_id=run_id, runner_binding="b" * 16
    )
    control.mark_process_started(
        task,
        action_id=first.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.mark_handshake(
        task,
        action_id=first.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    host = SystemdUserExecutorHost(
        transport=transport,
        runtime_directory=tmp_path / "runtime",
        environment={"PATH": "x" * 1024},
        executor_python=Path("/usr/bin/python3"),
        max_environment_bytes=128,
    )
    lifecycle = _control_lifecycle(
        states=states,
        control=control,
        task=task,
        current=states.load_run(run_id),
        host=host,
    )
    lifecycle.prepare_executor_session = lambda: host.prepare_environment((kind, "1"))
    spec = ExecutorSpec(
        task=task,
        action_id=first.action_id,
        run_id=run_id,
        generation=reservation.generation,
        runner_binding="b" * 16,
        state_root=states.root,
    )
    transport.unit = SystemdUnitObservation(
        "running", host._description(spec), os.getpid(), None
    )
    current = states.load_run(run_id)
    assert current is not None
    prepare_action_application_receipt(current, first.action)
    states.save_run(run_id, current)
    control.record_application(
        task,
        action_id=first.action_id,
        run_id=run_id,
        generation=reservation.generation,
        payload_digest=first.action["payload_digest"],
    )
    before_control = control.path_for(task).read_bytes()
    state_path = states.runs_directory / f"{run_id}.json"
    before_run = state_path.read_bytes()

    def detach_observer(_seconds: float) -> None:
        observation = host.observe(spec, control)
        assert observation.status == "running"
        raise InterruptedError(
            "observer detached while the original Action is applying"
        )

    lifecycle.sleep = detach_observer
    for _ in range(2):
        with pytest.raises(InterruptedError, match="observer detached"):
            lifecycle.submit_control(
                LifecycleRequest(task=task, kind=kind, payload=payload)
            )
        assert control.path_for(task).read_bytes() == before_control
        assert state_path.read_bytes() == before_run
        assert transport.start_count == 0
        assert worker.poll() is None
        assert not list((tmp_path / "runtime").glob("environment-*.json"))


@pytest.mark.parametrize("kind", ["stop", "abandon"])
@pytest.mark.parametrize("successor_executor", ["absent", "exited"])
def test_control_admission_rejects_run_changed_during_preparation(
    tmp_path: Path,
    kind: str,
    successor_executor: str,
) -> None:
    control, task, previous = _running_executor(tmp_path)
    other_bytes = None

    def prepare() -> None:
        nonlocal other_bytes
        other = TaskControlStore(control.root)
        executor = previous["executor"]
        other.finish_executor(
            task, action_id=executor["action_id"], generation=executor["generation"]
        )
        successor = other.claim_action(task, kind="run", payload={"parent": 194})
        other.bind_run(
            task, successor.action_id, "run-successor", state_dir=tmp_path / "state"
        )
        if successor_executor == "exited":
            reservation = other.begin_executor(
                task,
                action_id=successor.action_id,
                run_id="run-successor",
                runner_binding="a" * 16,
            )
            other.mark_process_started(
                task,
                action_id=successor.action_id,
                generation=reservation.generation,
                pid=os.getpid(),
                process_start_token=None,
            )
            other.mark_handshake(
                task,
                action_id=successor.action_id,
                generation=reservation.generation,
                pid=os.getpid(),
                process_start_token=None,
            )
            other.record_application(
                task,
                action_id=successor.action_id,
                run_id="run-successor",
                payload_digest=successor.action["payload_digest"],
            )
            other.complete_action(
                task, action_id=successor.action_id, result_status="completed"
            )
            other.finish_executor(
                task, action_id=successor.action_id, generation=reservation.generation
            )
        else:
            other.fail_action(
                task,
                action_id=successor.action_id,
                failure="before Executor reservation",
            )
        other_bytes = other.path_for(task).read_bytes()

    with pytest.raises(ActionReconciliationError, match="Delivery Run"):
        control.claim_control_action(
            task,
            kind=kind,
            payload={"parent": 194},
            run_id="run-194",
            state_dir=tmp_path / "state",
            before_create=prepare,
        )
    assert other_bytes is not None
    assert control.path_for(task).read_bytes() == other_bytes
    assert control.load(task)["run_id"] == "run-successor"
