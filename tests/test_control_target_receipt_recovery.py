from __future__ import annotations

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
from conftest import seed_run, write_fixture
from test_run_lifecycle import _isolated_environment
from cli_run_supervision_support import _parent_only_agents
from test_ticket_194_stop_abandon import _bind_running_executor, _ControlReceiptHost


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("failure_mode", ["ownership", "confirmation"])
def test_failed_stop_missing_control_keeps_target_until_confirmed_exit(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_case: str,
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
    expected_target = {
        key: original_target[key]
        for key in (
            "status",
            "action_id",
            "run_id",
            "generation",
            "runner_binding",
            "pid",
            "process_start_token",
        )
        if key in original_target
    }
    expected_target["worker"] = {
        key: original_target["worker"][key] for key in ("pid", "process_start_token")
    }
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
            if spec.generation > 2:
                assert worker.poll() is not None
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
        if control_case == "missing":
            control.path_for(task).unlink()
        else:
            control.path_for(task).write_text("broken json", encoding="utf-8")
        state_before = states.load_run(run_id)
        assert (
            state_before["action_application_receipt"]["target_executor"]
            == expected_target
        )
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
            assert worker.poll() is None
            assert states.load_run(run_id) == state_before
            assert fixture.read_bytes() == fixture_before
        assert len(rejected) == 3
        assert host.observe_count >= 2
        if failure_mode == "confirmation":
            assert signals == [(worker.pid, signal.SIGKILL)] * 3
        monkeypatch.setattr(host_module.os, "killpg", real_killpg)
        monkeypatch.setattr(host_module, "_wait_for_process_exit", real_wait)
        host.fail_target = False
        # The legal successor is admitted only after the real exact process
        # termination path confirms that the old Worker has exited.
        code = cli.main(
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
        output = json.loads(capsys.readouterr().out)
        assert code == 0, output
        assert worker.wait(timeout=3) == -signal.SIGKILL
        recovered = control.load(task)
        assert recovered is not None
        assert recovered["action"]["kind"] == "resume"
        assert recovered["action"]["executor_generation"] == generation + 1
        assert host.ensure_count == 2
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
def test_control_receipt_rejects_missing_or_inconsistent_target_evidence(
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
        with pytest.raises(ActionReconciliationError, match="target"):
            store.reconcile_from_run(
                task, {"run_id": "run-1", "action_application_receipt": receipt}
            )
        assert not store.path_for(task).exists()


def test_only_receipt_process_identity_survives_durable_sanitization() -> None:
    from agent_run.state import _sanitize_durable_errors

    original = {
        "action_application_receipt": {
            "target_executor": {
                "process_start_token": "12345",
                "binding_token": "control-capability",
                "worker": {"process_start_token": "67890", "api_token": "secret"},
            },
        },
        "diagnostics": [{"process_start_token": "secret"}],
        "other": {"target_executor": {"process_start_token": "secret"}},
    }
    saved = _sanitize_durable_errors(original)
    target = saved["action_application_receipt"]["target_executor"]
    assert target["process_start_token"] == "12345"
    assert target["worker"]["process_start_token"] == "67890"
    assert target["binding_token"] == "[REDACTED]"
    assert target["worker"]["api_token"] == "[REDACTED]"
    assert saved["diagnostics"][0]["process_start_token"] == "[REDACTED]"
    assert saved["other"]["target_executor"]["process_start_token"] == "[REDACTED]"


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
    assert state["action_application_receipt"]["target_executor"] is None
    store.path_for(task).unlink()
    rebuilt = store.reconcile_from_run(task, state, payload={"parent": 1})
    assert rebuilt is not None
    assert rebuilt["action"]["status"] == "accepted"
    assert rebuilt["executor"]["reconciliation_required"] is True
    assert "target_executor" not in rebuilt["action"]
