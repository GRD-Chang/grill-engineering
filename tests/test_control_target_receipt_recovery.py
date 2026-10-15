from __future__ import annotations

from copy import deepcopy
import json
import signal
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from support.workspace import managed_state

from agent_run import cli
from agent_run import executor_host as host_module
from agent_run.executor_host import (
    ExecutorSpec,
    ExecutorStartUnknownError,
    FakeExecutorHost,
    HostObservation,
)
from agent_run.state import StateStore
from agent_run.task_control import ActionReconciliationError, TaskControlStore, TaskKey
from support.seeding import seed_run, write_fixture
from support.lifecycle import _isolated_environment
from support.cli_supervision import _parent_only_agents
from support.control import _bind_running_executor, _ControlReceiptHost
from support.control_state import delete_control_fixture, raw_control_snapshot, snapshot_control


@pytest.mark.parametrize("failure_mode", ["ownership", "confirmation"])
def test_failed_stop_keeps_authoritative_target_until_confirmed_exit(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_mode: str,
) -> None:
    for key, value in _isolated_environment(git_repo.parent / "user-env").items():
        monkeypatch.setenv(key, value)
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = _parent_only_agents(git_repo / "agents.json")
    seeded = seed_run(git_repo, fixture)
    assert seeded.returncode == 0, seeded.stderr
    states = StateStore(managed_state(git_repo))
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    original = control.load(task)
    assert original is not None
    original_target = original["executor"]
    expected_target = deepcopy(original_target)
    real_terminate = FakeExecutorHost.terminate_control_target
    real_killpg = host_module.os.killpg
    real_wait = host_module._wait_for_process_exit
    rejected: list[dict[str, Any]] = []
    signals: list[tuple[int, int]] = []

    class RecoveryHost(_ControlReceiptHost):
        fail_target = True
        observe_count = 0
        ensure_count = 0

        def ensure(
            self,
            spec: ExecutorSpec,
            store: TaskControlStore,
            execute: Callable[[], Mapping[str, Any]] | None = None,
            recover: bool = False,
        ) -> HostObservation:
            self.ensure_count += 1
            return super().ensure(spec, store, execute, recover)

        def observe(
            self, spec: ExecutorSpec, store: TaskControlStore
        ) -> HostObservation:
            if spec.generation == 1:
                return super().observe(spec, store)
            self.observe_count += 1
            return HostObservation(
                "exited", spec.generation, None, False, runner_binding="b" * 16
            )

        def terminate_control_target(
            self, target: Mapping[str, Any], *, timeout: float = 1.0
        ) -> None:
            assert target["worker"]["pid"] == expected_target["worker"]["pid"]
            assert (
                target["worker"]["process_start_token"]
                == expected_target["worker"]["process_start_token"]
            )
            rejected.append(dict(target))
            if self.fail_target and failure_mode == "ownership":
                raise ExecutorStartUnknownError("Worker ownership cannot be confirmed")
            real_terminate(self, target, timeout=timeout)

    host = RecoveryHost()
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(cli, "FakeExecutorHost", lambda **_kwargs: host)
    if failure_mode == "confirmation":
        def record_target_signal(pid: int, sig: int) -> None:
            if pid == worker.pid:
                signals.append((pid, sig))
            else:
                real_killpg(pid, sig)

        monkeypatch.setattr(host_module.os, "killpg", record_target_signal)

        def unknown_exit(_pid: int, _token: str, *, timeout: float) -> None:
            raise ExecutorStartUnknownError("kill confirmation unknown")

        monkeypatch.setattr(host_module, "_wait_for_process_exit", unknown_exit)
    try:
        assert (
            cli.main(["stop", run_id, "--json", "--github-fixture", str(fixture)]) == 2
        )
        capsys.readouterr()
        failed = control.load(task)
        assert failed is not None and failed["action"]["status"] == "failed"
        predecessor_id = failed["action"]["action_id"]
        generation = failed["action"]["executor_generation"]
        state_before = states.load_run(run_id)
        assert "target_executor" not in state_before["action_application_receipt"]
        assert failed["action"]["target_executor"] == expected_target
        control_before = snapshot_control(control, task)
        fixture_before = fixture.read_bytes()
        for _attempt in range(2):
            assert (
                cli.main(
                    [
                        "resume",
                        run_id,
                        "--json",
                        "--github-fixture",
                        str(fixture),
                        "--agent-fixture",
                        str(agents),
                    ]
                )
                == 2
            )
            capsys.readouterr()
            retained = control.load(task)
            assert retained is not None
            assert retained["action"]["action_id"] == predecessor_id
            assert retained["action"]["target_executor"] == expected_target
            assert retained["next_generation"] == generation + 1
            assert host.ensure_count == 1
            assert snapshot_control(control, task) == control_before
            assert worker.poll() is None
            assert states.load_run(run_id) == state_before
            assert fixture.read_bytes() == fixture_before
        assert len(rejected) == 1
        assert host.observe_count >= 2
        if failure_mode == "confirmation":
            assert signals == [(worker.pid, signal.SIGKILL)]
        # Resume 不授权终止旧 target；明确重试 Stop 才能继续同一真实控制动作。
        assert cli.main(["stop", run_id, "--json", "--github-fixture", str(fixture)]) == 2
        capsys.readouterr()
        retried = control.load(task)
        assert retried is not None
        assert retried["action"]["target_executor"] == expected_target
        assert retried["action"]["executor_generation"] == generation + 1
        assert host.ensure_count == 2 and worker.poll() is None
        assert len(rejected) == 2
        monkeypatch.setattr(host_module.os, "killpg", real_killpg)
        monkeypatch.setattr(host_module, "_wait_for_process_exit", real_wait)
        host.fail_target = False
        # 准确 target 的真实终止和退出确认仍在事务外完成。
        code = cli.main(
            [
                "stop",
                run_id,
                "--json",
                "--github-fixture",
                str(fixture),
            ]
        )
        output = json.loads(capsys.readouterr().out)
        assert code == 0, output
        assert worker.wait(timeout=3) == -signal.SIGKILL
        recovered = control.load(task)
        assert recovered is not None
        assert recovered["action"]["kind"] == "stop"
        assert recovered["action"]["executor_generation"] == generation + 2
        assert states.load_run(run_id)["status"] == "operator_stopped"
        assert host.ensure_count == 3
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


@pytest.mark.parametrize("kind", ["stop", "abandon"])
@pytest.mark.parametrize(
    "target_case",
    [
        "missing",
        "wrong_run",
        "same_generation",
        "invalid_worker",
        "redacted_worker",
        "redacted_executor",
    ],
)
def test_control_receipt_target_cannot_replace_missing_authority(
    tmp_path: Path,
    kind: str,
    target_case: str,
) -> None:
    task = TaskKey(tmp_path / "repo", "example/project", 1)
    store = TaskControlStore(tmp_path / "state")
    receipt: dict[str, Any] = {
        "protocol": 1,
        "action_id": "control-action",
        "kind": kind,
        "payload_digest": "digest",
        "run_id": "run-1",
        "executor_generation": 2,
    }
    target: dict[str, Any] = {
        "status": "running",
        "action_id": "worker-action",
        "generation": 1,
        "run_id": "run-1",
        "pid": 1234,
        "process_start_token": "token",
    }
    if target_case != "missing":
        receipt["target_executor"] = target
    if target_case == "wrong_run":
        target["run_id"] = "run-other"
    elif target_case == "same_generation":
        target["generation"] = 2
    elif target_case == "invalid_worker":
        target["worker"] = {"pid": 1234}
    elif target_case == "redacted_worker":
        target["worker"] = {"pid": 1234, "process_start_token": "[REDACTED]"}
    elif target_case == "redacted_executor":
        target["process_start_token"] = "[REDACTED]"
    for _attempt in range(2):
        with pytest.raises(ActionReconciliationError, match="Task Control"):
            store.reconcile_from_run(
                task, {"run_id": "run-1", "action_application_receipt": receipt}
            )
        assert store.load(task) is None


def test_target_process_identity_stays_in_control_without_a_receipt_copy(tmp_path: Path) -> None:
    from agent_run.run_lifecycle import prepare_action_application_receipt

    task = TaskKey(tmp_path / "repo", "example/project", 1)
    store = TaskControlStore(tmp_path / "state")
    action = store.claim_action(task, kind="run", payload={"parent": 1})
    owner = store.begin_executor(task, action_id=action.action_id, run_id="run-1")
    binding = {"action_id": action.action_id, "generation": owner.generation}
    store.mark_handshake(task, **binding, pid=1234, process_start_token="12345")
    store.bind_run(task, action.action_id, "run-1", generation=owner.generation)
    store.record_application(task, **binding, run_id="run-1",
                             payload_digest=action.action["payload_digest"])
    store.complete_action(task, **binding, result_status="active")
    store.mark_worker_started(task, **binding, pid=5678, process_start_token="67890")
    stopped = store.claim_control_action(task, kind="stop", payload={}, run_id="run-1",
                                         state_dir=store.root)
    assert stopped is not None
    target = TaskControlStore(store.root).load(task)["action"]["target_executor"]
    assert target["process_start_token"] == "12345"
    assert target["worker"]["process_start_token"] == "67890"
    state: dict[str, Any] = {"run_id": "run-1"}
    prepare_action_application_receipt(state, stopped.action)
    assert "target_executor" not in state["action_application_receipt"]


@pytest.mark.parametrize(
    "status", ["completed", "operator_stopped", "abandoned", "run_approval_pending"]
)
def test_final_run_receipt_still_requires_executor_exit_proof(
    tmp_path: Path, status: str
) -> None:
    from agent_run.run_lifecycle import prepare_action_application_receipt

    task = TaskKey(tmp_path / "repo", "example/project", 1)
    store = TaskControlStore(tmp_path / "state")
    claim = store.claim_action(task, kind="abandon", payload={"parent": 1})
    state: dict[str, Any] = {"run_id": "run-1", "status": status}
    prepare_action_application_receipt(state, claim.action)
    assert "target_executor" not in state["action_application_receipt"]
    delete_control_fixture(store, task)
    before = raw_control_snapshot(store, task)
    for _attempt in range(2):
        with pytest.raises(ActionReconciliationError, match="Task Control"):
            store.reconcile_from_run(task, state, payload={"parent": 1})
        assert raw_control_snapshot(store, task) == before
