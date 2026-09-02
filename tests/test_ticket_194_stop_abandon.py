from __future__ import annotations

import os
import json
import signal
import select
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import seed_run, write_fixture
from cli_run_supervision_support import _parent_only_agents
from test_run_publication import RunPublicationAgents, _accepted_run
from agent_run import cli as cli_module
from agent_run import cli_surface
from agent_run import executor_host as executor_host_module
from agent_run import run_driver as run_driver_module
from agent_run.agent_invocation import record_operator_stop
from agent_run.executor_host import (
    ExecutorHostError,
    ExecutorSpec,
    ExecutorStartUnknownError,
    FakeExecutorHost,
    HostObservation,
)
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.git import GitRepository
from agent_run.operator_gate import operator_gate_subjects
from agent_run.run_lifecycle import (
    LifecycleRequest,
    RunLifecycle,
    prepare_action_application_receipt,
)
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from agent_run.worker_sandbox import run_worker_process
from agent_run.task_control import (
    ActionBusyError,
    ActionReconciliationError,
    TaskControlError,
    TaskControlStore,
    TaskKey,
)


def _run_cli(
    repo: Path, fixture: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_run",
            *arguments,
            "--json",
            "--github-fixture",
            str(fixture),
        ],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class _ControlReceiptHost(FakeExecutorHost):
    """Complete the fake Executor record while keeping observer semantics."""

    def __init__(
        self,
        *,
        before_failed_receipt: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.before_failed_receipt = before_failed_receipt

    def ensure(
        self,
        spec: ExecutorSpec,
        control: TaskControlStore,
        execute: Callable[[], Mapping[str, Any]] | None = None,
        recover: bool = False,
    ) -> HostObservation:
        del recover
        assert execute is not None
        reservation = control.begin_executor(
            spec.task,
            action_id=spec.action_id,
            run_id=spec.run_id,
        )
        control.mark_process_started(
            spec.task,
            action_id=spec.action_id,
            generation=reservation.generation,
            pid=os.getpid(),
            process_start_token=None,
        )
        control.mark_handshake(
            spec.task,
            action_id=spec.action_id,
            generation=reservation.generation,
            pid=os.getpid(),
            process_start_token=None,
        )
        try:
            result = execute()
        except BaseException as error:
            if self.before_failed_receipt is not None:
                self.before_failed_receipt()
            control.finish_executor(
                spec.task,
                action_id=spec.action_id,
                generation=reservation.generation,
                failure=str(error),
            )
        else:
            result_status = result.get("status")
            control.finish_executor(
                spec.task,
                action_id=spec.action_id,
                generation=reservation.generation,
                result_status=(
                    result_status if isinstance(result_status, str) else None
                ),
            )
        return HostObservation("running", reservation.generation, os.getpid(), True)


def _bind_running_executor(
    repo: Path, run_id: str
) -> tuple[TaskControlStore, TaskKey, subprocess.Popen[bytes]]:
    states = StateStore(repo / ".agent-run")
    task = TaskKey(repo, "example/project", 1)
    control = TaskControlStore(repo / ".agent-run")
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    reservation = control.begin_executor(
        task, action_id=claim.action_id, run_id=None, runner_binding="b" * 16
    )
    token = (
        Path(f"/proc/{os.getpid()}/stat")
        .read_text(encoding="ascii")
        .rsplit(")", 1)[1]
        .split()[19]
    )
    control.mark_process_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=token,
    )
    control.mark_handshake(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=token,
    )
    control.bind_run(
        task,
        claim.action_id,
        run_id,
        generation=reservation.generation,
        state_dir=states.root,
    )
    run = states.load_run(run_id)
    assert run is not None
    control.record_application(
        task,
        action_id=claim.action_id,
        run_id=run_id,
        generation=reservation.generation,
        payload_digest=str(claim.action["payload_digest"]),
    )
    record = control.load(task)
    assert record is not None and isinstance(record.get("action"), dict)
    prepare_action_application_receipt(run, record["action"])
    states.save_run(run_id, run)
    control.complete_action(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        result_status=str(run["status"]),
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    worker_token = (
        Path(f"/proc/{worker.pid}/stat")
        .read_text(encoding="ascii")
        .rsplit(")", 1)[1]
        .split()[19]
    )
    control.mark_worker_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=worker.pid,
        process_start_token=worker_token,
    )
    return control, task, worker


def _running_executor(
    tmp_path: Path,
) -> tuple[TaskControlStore, TaskKey, dict[str, object]]:
    task = TaskKey(tmp_path / "repo", "owner/repo", 194)
    control = TaskControlStore(tmp_path / "control")
    claim = control.claim_action(task, kind="run", payload={"parent": 194})
    assert claim.action_id is not None
    reservation = control.begin_executor(
        task,
        action_id=claim.action_id,
        run_id=None,
        runner_binding="a" * 16,
    )
    control.mark_process_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.mark_handshake(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.bind_run(
        task,
        claim.action_id,
        "run-194",
        generation=reservation.generation,
        state_dir=tmp_path / "state",
    )
    control.record_application(
        task,
        action_id=claim.action_id,
        run_id="run-194",
        generation=reservation.generation,
        payload_digest=str(claim.action["payload_digest"]),
    )
    control.complete_action(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        result_status="active",
    )
    record = control.load(task)
    assert record is not None
    return control, task, record


def _control_lifecycle(
    *,
    states: StateStore,
    control: TaskControlStore,
    task: TaskKey,
    current: dict[str, object],
    host: FakeExecutorHost,
) -> RunLifecycle:
    run_id = str(current["run_id"])
    return RunLifecycle(
        states=states,
        control=control,
        host=host,
        task=task,
        preflight=lambda: states.load_current_run(run_id),
        select_run=lambda _action: (current, True),
        initialize_profile=None,
        executor_spec=lambda bound_run_id, action_id, generation: ExecutorSpec(
            task=task,
            action_id=action_id,
            run_id=bound_run_id,
            generation=generation,
            cwd=task.workspace,
            state_root=states.root,
        ),
        execute=lambda _run_id: current,
        execute_with_binding=lambda _run_id, _action_id, _generation: current,
    )


def test_control_action_atomically_fences_the_previous_executor(tmp_path: Path) -> None:
    control, task, previous = _running_executor(tmp_path)
    executor = previous["executor"]
    assert isinstance(executor, dict)

    claim = control.claim_control_action(
        task,
        kind="stop",
        payload={"parent": 194, "run_id": "run-194"},
        run_id="run-194",
        state_dir=tmp_path / "state",
    )

    assert not claim.attached
    assert claim.target_executor == executor
    with pytest.raises(ActionReconciliationError, match="不存在"):
        control.assert_executor_current(
            task,
            action_id=str(executor["action_id"]),
            generation=int(executor["generation"]),
            run_id="run-194",
        )
    current = control.load(task)
    assert current is not None
    assert current["executor"] is None
    assert current["action"]["kind"] == "stop"
    assert current["action"]["status"] == "applying"


def test_identical_unresolved_stop_attaches_without_a_second_action(
    tmp_path: Path,
) -> None:
    control, task, _previous = _running_executor(tmp_path)
    payload = {"parent": 194, "run_id": "run-194"}
    first = control.claim_control_action(
        task,
        kind="stop",
        payload=payload,
        run_id="run-194",
        state_dir=tmp_path / "state",
    )
    first_record = control.load(task)
    assert first_record is not None
    before = control.path_for(task).read_bytes()

    repeated = control.claim_control_action(
        task,
        kind="stop",
        payload=payload,
        run_id="run-194",
        state_dir=tmp_path / "state",
    )

    assert repeated.attached
    assert repeated.action_id == first.action_id
    assert repeated.target_executor == first.target_executor
    assert control.path_for(task).read_bytes() == before
    record = control.load(task)
    assert record is not None
    assert record["action"]["action_id"] == first.action_id
    assert record["action_history"] == first_record["action_history"]
    assert all(
        entry["action"]["action_id"] != first.action_id
        for entry in record["action_history"]
    )


def test_stop_rejects_action_accepted_before_executor(tmp_path: Path) -> None:
    task = TaskKey(tmp_path / "repo", "owner/repo", 194)
    control = TaskControlStore(tmp_path / "control")
    old = control.claim_action(task, kind="run", payload={"parent": 194})
    assert old.action_id is not None
    before = control.path_for(task).read_bytes()

    with pytest.raises(ActionBusyError, match="不会等待或排队"):
        control.claim_control_action(
            task,
            kind="stop",
            payload={"parent": 194, "run_id": "run-194"},
            run_id="run-194",
            state_dir=tmp_path / "state",
        )

    record = control.load(task)
    assert record is not None
    assert control.path_for(task).read_bytes() == before
    assert record["action"]["action_id"] == old.action_id
    assert record["action"]["status"] == "accepted"
    assert record["action_history"] == []


@pytest.mark.parametrize("pending_kind", ["run", "resume"])
def test_stop_rejects_pending_action_at_pre_executor_barrier(
    git_repo: Path,
    pending_kind: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    if pending_kind == "resume":
        current.update(
            {"status": "operator_stopped", "terminal_kind": "operator_stopped"}
        )
        states.save_run(run_id, current)
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    pending = control.claim_action(
        task,
        kind=pending_kind,
        payload={"parent": 1, "run_id": run_id},
    )
    assert pending.action_id is not None
    host = FakeExecutorHost()
    lifecycle = _control_lifecycle(
        states=states,
        control=control,
        task=task,
        current=current,
        host=host,
    )
    state_path = states.runs_directory / f"{run_id}.json"
    before_state = state_path.read_bytes()
    before_control = control.path_for(task).read_bytes()
    entered = threading.Barrier(2)
    release = threading.Event()
    original_preflight = lifecycle.preflight

    def preflight_at_barrier() -> dict[str, Any] | None:
        entered.wait()
        assert release.wait(timeout=2)
        return original_preflight()

    lifecycle.preflight = preflight_at_barrier
    failures: list[BaseException] = []

    def submit_stop() -> None:
        try:
            lifecycle.submit_control(
                LifecycleRequest(
                    task=task,
                    kind="stop",
                    payload={"parent": 1, "run_id": run_id},
                )
            )
        except BaseException as error:
            failures.append(error)

    contender = threading.Thread(target=submit_stop)
    contender.start()
    entered.wait()
    assert state_path.read_bytes() == before_state
    assert control.path_for(task).read_bytes() == before_control
    release.set()
    contender.join(timeout=2)

    assert not contender.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ActionBusyError)
    assert state_path.read_bytes() == before_state
    assert control.path_for(task).read_bytes() == before_control
    assert host.start_count == 0
    record = control.load(task)
    assert record is not None
    assert record["action"]["action_id"] == pending.action_id
    assert record["action"]["status"] == "accepted"
    assert record["action_history"] == []


def test_stop_terminates_only_the_exact_recorded_worker_group(tmp_path: Path) -> None:
    worker = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    try:
        token = (
            Path(f"/proc/{worker.pid}/stat")
            .read_text(encoding="ascii")
            .rsplit(")", 1)[1]
            .split()[19]
        )
        target = {
            "action_id": "action-1",
            "run_id": "run-194",
            "generation": 1,
            "pid": os.getpid(),
            "process_start_token": None,
            "worker": {
                "pid": worker.pid,
                "process_start_token": token,
            },
        }

        FakeExecutorHost().terminate_control_target(target)

        assert worker.wait(timeout=3) == -signal.SIGKILL
        assert unrelated.poll() is None
    finally:
        for process in (worker, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_public_stop_is_immediate_and_repeat_is_read_only(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    seeded = seed_run(git_repo, fixture)
    assert seeded.returncode == 0, seeded.stderr
    states = StateStore(git_repo / ".agent-run")
    run = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(run["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)

    stopped = _run_cli(git_repo, fixture, "stop", run_id)

    assert stopped.returncode == 0, stopped.stderr
    assert worker.wait(timeout=2) == -signal.SIGKILL
    payload = json.loads(stopped.stdout)
    assert payload["status"] == "operator_stopped"
    assert payload["action"]["operation"] == "stop"
    durable = states.load_run(run_id)
    assert durable is not None
    assert durable["status"] == "operator_stopped"
    assert durable["action_application_receipt"]["kind"] == "stop"
    state_path = states.runs_directory / f"{run_id}.json"
    before_state = state_path.read_bytes()
    before_control = control.path_for(task).read_bytes()

    repeated = _run_cli(git_repo, fixture, "stop", run_id)

    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["status"] == "operator_stopped"
    assert state_path.read_bytes() == before_state
    assert control.path_for(task).read_bytes() == before_control

    assert cli_surface._resume_is_ready(durable)
    resumed_result = _run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(_parent_only_agents(git_repo / "resume-agents.json")),
    )
    assert resumed_result.returncode == 0, resumed_result.stderr
    resumed = states.load_run(run_id)
    assert resumed is not None
    assert resumed["status"] != "operator_stopped"
    assert "operator_stop" not in resumed
    assert resumed["resume_audit"]["history"][-1]["kind"] == "operator_stopped"
    resumed_state = state_path.read_bytes()
    resumed_control = control.path_for(task).read_bytes()

    no_active_executor = _run_cli(git_repo, fixture, "stop", run_id)

    assert no_active_executor.returncode == 0, no_active_executor.stderr
    no_active_payload = json.loads(no_active_executor.stdout)
    assert no_active_payload["result"] == "no_active_executor"
    assert no_active_payload["diagnostics"][-1]["code"] == "no_active_executor"
    assert no_active_payload["status"] == resumed["status"]
    assert "action" not in no_active_payload
    assert state_path.read_bytes() == resumed_state
    assert control.path_for(task).read_bytes() == resumed_control


@pytest.mark.parametrize(
    ("kind", "failure_text"),
    [
        ("stop", "Worker ownership cannot be confirmed"),
        ("abandon", "tracked modifications"),
    ],
)
def test_detached_control_failure_is_reported_and_blocks_ordinary_run(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    failure_text: str,
) -> None:
    status_before_failed_receipt: list[str] = []

    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    dirty_checkout: Path | None = None
    mutations_before_failure: list[object] | None = None
    if kind == "abandon":
        prepared = _run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(_parent_only_agents(git_repo / "dirty-abandon-agents.json")),
        )
        assert prepared.returncode == 0, prepared.stderr
        current = states.load_run(run_id)
        assert current is not None
        parent_job = current["parent_job"]
        assert isinstance(parent_job, dict)
        dirty_checkout = git_repo / ".agent-run" / "worktrees" / run_id / "parent"
        GitRepository(git_repo).prepare_ticket_checkout(
            branch=str(parent_job["parent_branch"]),
            base_sha=str(parent_job["base_sha"]),
            checkout=dirty_checkout,
        )
        (dirty_checkout / "README.md").write_text(
            "unsaved delivery\n", encoding="utf-8"
        )
        mutations_before_failure = list(
            json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
                "mutations"
            ]
        )
    control, task, worker = _bind_running_executor(git_repo, run_id)
    monkeypatch.chdir(git_repo)

    def record_status_before_receipt() -> None:
        durable = StateStore(git_repo / ".agent-run").load_run(run_id)
        assert durable is not None
        status_before_failed_receipt.append(str(durable["status"]))

    monkeypatch.setattr(
        cli_module,
        "FakeExecutorHost",
        lambda **_options: _ControlReceiptHost(
            before_failed_receipt=record_status_before_receipt
        ),
    )

    if kind == "stop":

        def reject_unknown_target(
            _host: object,
            _target: Mapping[str, Any],
            *,
            timeout: float = 1.0,
        ) -> None:
            del timeout
            raise ExecutorStartUnknownError(failure_text)

        monkeypatch.setattr(
            _ControlReceiptHost,
            "terminate_control_target",
            reject_unknown_target,
        )
    try:
        return_code = cli_module.main(
            [kind, run_id, "--json", "--github-fixture", str(fixture)]
        )

        assert return_code == 2
        output = json.loads(capsys.readouterr().out)
        assert output["result"] == "error"
        assert "action" in output, output
        assert output["action"]["status"] == "failed"
        assert output["action_audit"]["status"] == "failed"
        actual_failure = output["action_audit"]["failure"]
        assert failure_text in actual_failure
        assert output["diagnostics"][-1] == {
            "code": "lifecycle_action_failed",
            "message": actual_failure,
        }
        record = control.load(task)
        assert record is not None
        assert record["action"]["status"] == "failed"
        durable = states.load_run(run_id)
        assert durable is not None
        expected_safe_status = (
            "execution_failed" if kind == "stop" else "parent_approval_pending"
        )
        assert status_before_failed_receipt == [expected_safe_status]
        assert durable["status"] == expected_safe_status

        state_before_run = states.load_run(run_id)
        assert state_before_run is not None
        control_before_run = control.path_for(task).read_bytes()
        ordinary_run = _run_cli(git_repo, fixture, "run", "1")
        assert ordinary_run.returncode == 2
        assert json.loads(ordinary_run.stdout)["status"] == expected_safe_status
        state_after_run = states.load_run(run_id)
        assert state_after_run is not None
        for field in ("status", "parent_job", "active_agent_invocation"):
            assert state_after_run.get(field) == state_before_run.get(field)
        assert control.path_for(task).read_bytes() == control_before_run

        if kind == "abandon":
            assert dirty_checkout is not None and dirty_checkout.exists()
            assert mutations_before_failure is not None
            assert (
                json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
                    "mutations"
                ]
                == mutations_before_failure
            )
            discarded = _run_cli(
                git_repo, fixture, "abandon", run_id, "--discard-worktree"
            )
            assert discarded.returncode == 0, discarded.stderr
            assert json.loads(discarded.stdout)["status"] == "abandoned"
            assert not dirty_checkout.exists()
            mutations = json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
                "mutations"
            ]
            assert mutations.count(
                {"action": "close_parent_pr", "pr_number": 1}
            ) == 1
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


@pytest.mark.parametrize(
    ("kind", "failure_mode", "failure_text", "becomes_terminal"),
    [
        ("stop", "ownership", "Worker ownership cannot be confirmed", False),
        (
            "stop",
            "confirmation",
            "已发送终止信号，但进程退出结果无法确认",
            True,
        ),
        ("abandon", "ownership", "Worker ownership cannot be confirmed", False),
        (
            "abandon",
            "confirmation",
            "已发送终止信号，但进程退出结果无法确认",
            False,
        ),
    ],
)
def test_failed_control_retries_the_unresolved_target_before_success(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    failure_mode: str,
    failure_text: str,
    becomes_terminal: bool,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    if kind == "abandon":
        prepared = _run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(_parent_only_agents(git_repo / "retry-abandon-agents.json")),
        )
        assert prepared.returncode == 0, prepared.stderr
    control, task, worker = _bind_running_executor(git_repo, run_id)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    worker_token = (
        Path(f"/proc/{worker.pid}/stat")
        .read_text(encoding="ascii")
        .rsplit(")", 1)[1]
        .split()[19]
    )
    expected_target = (worker.pid, worker_token)
    rejected_targets: list[tuple[int, str]] = []
    recovered_targets: list[tuple[int, str]] = []
    failed_action_ids: list[str] = []
    real_terminate = FakeExecutorHost.terminate_control_target
    real_killpg = executor_host_module.os.killpg
    real_wait_for_exit = executor_host_module._wait_for_process_exit
    sent_signals: list[tuple[int, int]] = []

    def target_identity(target: Mapping[str, Any]) -> tuple[int, str]:
        target_worker = target.get("worker")
        assert isinstance(target_worker, Mapping)
        return (
            int(target_worker["pid"]),
            str(target_worker["process_start_token"]),
        )

    def reject_target(
        _host: object,
        target: Mapping[str, Any],
        *,
        timeout: float = 1.0,
    ) -> None:
        del timeout
        rejected_targets.append(target_identity(target))
        raise ExecutorStartUnknownError(failure_text)

    def record_signal_without_exit(pid: int, sent_signal: int) -> None:
        sent_signals.append((pid, sent_signal))

    def reject_exit_confirmation(_pid: int, _token: str, *, timeout: float) -> None:
        del timeout
        raise ExecutorStartUnknownError(failure_text)

    def terminate_without_confirmation(
        host: FakeExecutorHost,
        target: Mapping[str, Any],
        *,
        timeout: float = 1.0,
    ) -> None:
        rejected_targets.append(target_identity(target))
        real_terminate(host, target, timeout=timeout)

    def recover_target(
        host: FakeExecutorHost,
        target: Mapping[str, Any],
        *,
        timeout: float = 1.0,
    ) -> None:
        recovered_targets.append(target_identity(target))
        real_terminate(host, target, timeout=timeout)

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(
        cli_module,
        "FakeExecutorHost",
        lambda **_options: _ControlReceiptHost(),
    )
    if failure_mode == "ownership":
        monkeypatch.setattr(
            _ControlReceiptHost, "terminate_control_target", reject_target
        )
    else:
        monkeypatch.setattr(
            executor_host_module.os, "killpg", record_signal_without_exit
        )
        monkeypatch.setattr(
            executor_host_module,
            "_wait_for_process_exit",
            reject_exit_confirmation,
        )
        monkeypatch.setattr(
            _ControlReceiptHost,
            "terminate_control_target",
            terminate_without_confirmation,
        )
    try:
        for attempt in range(2):
            return_code = cli_module.main(
                [kind, run_id, "--json", "--github-fixture", str(fixture)]
            )
            output = json.loads(capsys.readouterr().out)
            assert return_code == 2
            assert output["result"] == "error"
            assert output["action"]["status"] == "failed"
            assert failure_text in output["action_audit"]["failure"]
            failed_action_ids.append(str(output["action_audit"]["action_id"]))
            assert worker.poll() is None
            assert unrelated.poll() is None
            if becomes_terminal and attempt == 0:
                terminal = states.load_run(run_id)
                assert terminal is not None
                terminal.update({"status": "completed", "terminal_kind": "completed"})
                states.save_run(run_id, terminal)

        assert rejected_targets == [expected_target, expected_target]
        if failure_mode == "confirmation":
            assert sent_signals == [
                (worker.pid, signal.SIGKILL),
                (worker.pid, signal.SIGKILL),
            ]
        failed = control.load(task)
        assert failed is not None
        failed_action = failed["action"]
        assert failed_action["status"] == "failed"
        assert target_identity(failed_action["target_executor"]) == expected_target

        monkeypatch.setattr(executor_host_module.os, "killpg", real_killpg)
        monkeypatch.setattr(
            executor_host_module,
            "_wait_for_process_exit",
            real_wait_for_exit,
        )
        monkeypatch.setattr(
            _ControlReceiptHost, "terminate_control_target", recover_target
        )
        recovered_code = cli_module.main(
            [kind, run_id, "--json", "--github-fixture", str(fixture)]
        )
        recovered = json.loads(capsys.readouterr().out)

        assert recovered_code == 0
        expected_status = (
            "completed"
            if becomes_terminal
            else "operator_stopped"
            if kind == "stop"
            else "abandoned"
        )
        assert recovered["status"] == expected_status
        assert worker.wait(timeout=3) == -signal.SIGKILL
        assert unrelated.poll() is None
        assert recovered_targets == [expected_target]
        completed = control.load(task)
        assert completed is not None
        assert completed["action"]["status"] == "completed"
        assert target_identity(completed["action"]["target_executor"]) == expected_target
        history = completed["action_history"]
        failed_history = {
            entry["action"]["action_id"]: entry["action"]
            for entry in history
            if entry["action"]["action_id"] in failed_action_ids
        }
        assert set(failed_history) == set(failed_action_ids)
        for failed_action_id in failed_action_ids:
            historical = failed_history[failed_action_id]
            assert historical["status"] == "failed"
            assert failure_text in historical["failure"]
            assert target_identity(historical["target_executor"]) == expected_target
        if kind == "abandon":
            mutations = json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
                "mutations"
            ]
            assert mutations.count(
                {"action": "close_parent_pr", "pr_number": 1}
            ) == 1
    finally:
        for process in (worker, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_unresolved_target_blocks_successors_and_run_id_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    try:
        claimed = control.claim_control_action(
            task,
            kind="stop",
            payload={"parent": 1, "run_id": run_id},
            run_id=run_id,
            state_dir=states.root,
        )
        assert claimed is not None
        record = control.load(task)
        assert record is not None
        action = record["action"]
        action.update(
            {
                "status": "failed",
                "completed_at": "2026-09-02T00:00:00+00:00",
                "failure": "Worker ownership cannot be confirmed",
            }
        )
        control.path_for(task).write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )

        with pytest.raises(ActionReconciliationError, match="目标 ownership 尚未收口"):
            control.claim_action(
                task,
                kind="resume",
                payload={"parent": 1, "run_id": run_id},
            )

        action["target_executor"]["run_id"] = "run-from-another-generation"
        control.path_for(task).write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )

        with pytest.raises(TaskControlError, match="target Executor Run ID"):
            control.claim_control_action(
                task,
                kind="stop",
                payload={"parent": 1, "run_id": run_id},
                run_id=run_id,
                state_dir=states.root,
            )
        assert worker.poll() is None
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)


def test_public_abandon_can_permanently_close_an_operator_stopped_run(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    run = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(run["run_id"])
    prepared = _run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(_parent_only_agents(git_repo / "abandon-agents.json")),
    )
    assert prepared.returncode == 0, prepared.stderr
    control, task, worker = _bind_running_executor(git_repo, run_id)

    stopped = _run_cli(git_repo, fixture, "stop", run_id)
    assert stopped.returncode == 0, stopped.stderr
    assert worker.wait(timeout=2) == -signal.SIGKILL
    assert json.loads(stopped.stdout)["status"] == "operator_stopped"

    abandoned = _run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    output = json.loads(abandoned.stdout)
    assert output["status"] == "abandoned"
    assert output["action"]["operation"] == "abandon"
    record = control.load(task)
    assert record is not None
    assert record["action"]["status"] == "completed"
    assert states.load_run(run_id)["status"] == "abandoned"


def test_worker_binding_failure_does_not_leak_the_process_group(tmp_path: Path) -> None:
    started: list[int] = []

    def reject_binding(pid: int) -> None:
        started.append(pid)
        raise ActionReconciliationError("old Executor was fenced")

    with pytest.raises(ActionReconciliationError, match="fenced"):
        run_worker_process(
            [sys.executable, "-c", "import signal; signal.pause()"],
            cwd=tmp_path,
            prompt="",
            environment=os.environ.copy(),
            timeout=5,
            on_process_started=reject_binding,
        )

    assert len(started) == 1
    assert not Path(f"/proc/{started[0]}").exists()


def test_operator_stop_binds_the_invocation_subject_after_ticket_completion(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "3": {
                "number": 3,
                "title": "Completed ticket",
                "body": "",
                "state": "OPEN",
                "labels": ["ready-for-agent"],
                "blocked_by": [],
            }
        },
    )
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    state = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(state["run_id"])
    state["active_ticket_job"]["phase"] = "completed"
    state["ticket_jobs"]["3"]["phase"] = "completed"
    state["run_acceptance"] = {"phase": "reviewing"}
    state["active_agent_invocation"] = {
        "status": "running",
        "started_at": "2026-09-01T00:00:00+00:00",
        "work_subject": f"run-acceptance:{run_id}",
    }

    record_operator_stop(state, save=lambda _state: None)

    assert operator_gate_subjects(state) == [
        ("run_acceptance", state["run_acceptance"])
    ]


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="requires Linux pidfd")
def test_worker_cannot_execute_before_its_binding_is_durable(tmp_path: Path) -> None:
    """Executor death in the Popen-to-binding window releases no Worker work."""

    side_effect = tmp_path / "worker-ran"
    worker_pid_read, worker_pid_write = os.pipe()
    executor_pid = os.fork()
    if executor_pid == 0:  # pragma: no branch - child is terminated at the barrier
        os.close(worker_pid_read)

        def wait_at_binding(pid: int) -> None:
            os.write(worker_pid_write, f"{pid}\n".encode("ascii"))
            os.close(worker_pid_write)
            signal.pause()

        try:
            run_worker_process(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path(r'%s').write_text('ran')"
                    % side_effect,
                ],
                cwd=tmp_path,
                prompt="",
                environment=os.environ.copy(),
                timeout=30,
                on_process_started=wait_at_binding,
            )
        finally:
            os._exit(0)

    os.close(worker_pid_write)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    worker_pid: int | None = None
    worker_pid_fd: int | None = None
    try:
        pid_ready, _, _ = select.select([worker_pid_read], [], [], 3)
        assert pid_ready == [worker_pid_read]
        worker_pid = int(os.read(worker_pid_read, 32).strip())
        worker_pid_fd = os.pidfd_open(worker_pid)
        os.kill(executor_pid, signal.SIGKILL)
        os.waitpid(executor_pid, 0)

        ready, _, _ = select.select([worker_pid_fd], [], [], 3)

        assert ready == [worker_pid_fd]
        assert not side_effect.exists()
        assert unrelated.poll() is None
    finally:
        os.close(worker_pid_read)
        if worker_pid_fd is not None:
            os.close(worker_pid_fd)
        try:
            os.kill(executor_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(executor_pid, 0)
        except ChildProcessError:
            pass
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=3)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="requires Linux pidfd")
def test_abandon_executor_survives_observer_exit_with_canonical_receipt(
    git_repo: Path,
) -> None:
    """The observing terminal owns no accepted control-action execution."""

    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    effect = git_repo / ".agent-run" / "abandon-effect"
    observer_pid = os.fork()
    if observer_pid == 0:  # pragma: no branch - explicit observer process
        os.close(ready_read)
        os.close(release_write)

        def execute_control(
            bound_run_id: str, _action_id: str, _generation: int
        ) -> dict[str, object]:
            os.write(ready_write, b"1")
            os.close(ready_write)
            assert os.read(release_read, 1) == b"1"
            os.close(release_read)
            descriptor = os.open(effect, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            result = states.load_current_run(bound_run_id)
            assert result is not None
            result.update({"status": "abandoned", "terminal_kind": "abandoned"})
            states.save_run(bound_run_id, result)
            return result

        lifecycle = RunLifecycle(
            states=states,
            control=control,
            host=FakeExecutorHost(separate_process=True),
            task=task,
            preflight=lambda: states.load_current_run(run_id),
            select_run=lambda _action: (current, True),
            initialize_profile=None,
            executor_spec=lambda bound_run_id, action_id, generation: ExecutorSpec(
                task=task,
                action_id=action_id,
                run_id=bound_run_id,
                generation=generation,
                cwd=git_repo,
                state_root=states.root,
            ),
            execute=lambda _run_id: current,
            execute_with_binding=execute_control,
        )
        try:
            lifecycle.submit_control(
                LifecycleRequest(
                    task=task,
                    kind="abandon",
                    payload={
                        "parent": 1,
                        "run_id": run_id,
                        "discard_worktree": True,
                    },
                )
            )
        except KeyboardInterrupt:
            os._exit(130)
        except BaseException:
            os._exit(1)
        os._exit(0)

    os.close(ready_write)
    os.close(release_read)
    executor_pid_fd: int | None = None
    try:
        ready_descriptors, _, _ = select.select([ready_read], [], [], 3)
        assert ready_descriptors == [ready_read]
        assert os.read(ready_read, 1) == b"1"
        applying = control.load(task)
        assert applying is not None
        executor = applying["executor"]
        assert isinstance(executor, dict)
        executor_pid = int(executor["pid"])
        executor_pid_fd = os.pidfd_open(executor_pid)

        os.kill(observer_pid, signal.SIGINT)
        _, observer_status = os.waitpid(observer_pid, 0)
        assert os.WIFEXITED(observer_status)
        assert os.WEXITSTATUS(observer_status) == 130
        os.write(release_write, b"1")
        os.close(release_write)

        ready, _, _ = select.select([executor_pid_fd], [], [], 3)
        assert ready == [executor_pid_fd]
        record = control.load(task)
        assert record is not None
        assert record["action"]["status"] == "completed"
        assert record["executor"]["status"] == "exited"
        final = states.load_run(run_id)
        assert final is not None
        assert final["status"] == "abandoned"
        assert final.get("execution_failure") is None
        receipt = RunLifecycle.receipt_from_record(
            record,
            action_id=str(record["action"]["action_id"]),
            attached=False,
        )
        assert receipt.status == "completed"
        assert receipt.executor_generation == record["executor"]["generation"]
        assert receipt.handshake is True
        assert effect.exists()
    finally:
        os.close(ready_read)
        if executor_pid_fd is not None:
            os.close(executor_pid_fd)
        try:
            os.close(release_write)
        except OSError:
            pass
        try:
            os.kill(observer_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(observer_pid, 0)
        except ChildProcessError:
            pass


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="requires Linux pidfd")
def test_public_abandon_observer_exit_does_not_cancel_the_executor(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    run_id = str(states.find_unfinished_runs("example/project", 1)[0]["run_id"])
    prepared = _run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(_parent_only_agents(git_repo / "observer-agents.json")),
    )
    assert prepared.returncode == 0, prepared.stderr
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    attached_read, attached_write = os.pipe()
    interrupted_output = git_repo / "interrupted.json"
    attached_output = git_repo / "attached.json"
    observer_pid = os.fork()
    reobserver_pid: int | None = None
    executor_pid_fd: int | None = None
    if observer_pid == 0:  # pragma: no branch - exits after public CLI returns
        os.close(ready_read)
        os.close(release_write)
        os.close(attached_read)
        os.close(attached_write)
        sys.stdout = interrupted_output.open("w", encoding="utf-8")
        original_abandon = run_driver_module.ParentDeliveryEngine.abandon

        def abandon_at_barrier(
            engine: object,
            selected_run_id: str,
            *,
            discard_worktree: bool = False,
        ) -> dict[str, object]:
            os.write(ready_write, b"1")
            os.close(ready_write)
            assert os.read(release_read, 1) == b"1"
            os.close(release_read)
            return original_abandon(
                engine,  # type: ignore[arg-type]
                selected_run_id,
                discard_worktree=discard_worktree,
            )

        run_driver_module.ParentDeliveryEngine.abandon = abandon_at_barrier  # type: ignore[method-assign]
        os.chdir(git_repo)
        result = cli_module.main(
            [
                "abandon",
                run_id,
                "--json",
                "--github-fixture",
                str(fixture),
            ]
        )
        sys.stdout.flush()
        os._exit(result)

    os.close(ready_write)
    os.close(release_read)
    try:
        ready_descriptors, _, _ = select.select([ready_read], [], [], 3)
        assert ready_descriptors == [ready_read]
        assert os.read(ready_read, 1) == b"1"
        applying = control.load(task)
        assert applying is not None and isinstance(applying.get("executor"), dict)
        executor_pid_fd = os.pidfd_open(int(applying["executor"]["pid"]))

        os.kill(observer_pid, signal.SIGINT)
        _, observer_status = os.waitpid(observer_pid, 0)
        assert os.WIFEXITED(observer_status)
        assert os.WEXITSTATUS(observer_status) == 130
        interrupted = json.loads(interrupted_output.read_text(encoding="utf-8"))
        assert interrupted["diagnostics"][0]["code"] == "observation_interrupted"

        reobserver_pid = os.fork()
        if reobserver_pid == 0:  # pragma: no branch - public attach observer
            os.close(attached_read)
            os.close(release_write)
            sys.stdout = attached_output.open("w", encoding="utf-8")
            original_observe = cli_module.RunLifecycle._observe_action

            def observe_after_attach(lifecycle: object, *args: object, **kwargs: object):
                os.write(attached_write, b"1")
                os.close(attached_write)
                return original_observe(
                    lifecycle, *args, **kwargs  # type: ignore[arg-type]
                )

            cli_module.RunLifecycle._observe_action = (  # type: ignore[method-assign]
                observe_after_attach
            )
            os.chdir(git_repo)
            result = cli_module.main(
                [
                    "abandon",
                    run_id,
                    "--json",
                    "--github-fixture",
                    str(fixture),
                ]
            )
            sys.stdout.flush()
            os._exit(result)

        os.close(attached_write)
        attached_descriptors, _, _ = select.select([attached_read], [], [], 3)
        assert attached_descriptors == [attached_read]
        assert os.read(attached_read, 1) == b"1"
        os.write(release_write, b"1")
        os.close(release_write)

        _, reobserver_status = os.waitpid(reobserver_pid, 0)
        assert os.WIFEXITED(reobserver_status)
        assert os.WEXITSTATUS(reobserver_status) == 0
        completed_output = json.loads(attached_output.read_text(encoding="utf-8"))
        audit = completed_output["action_audit"]
        record = control.load(task)
        assert record is not None
        assert completed_output["status"] == "abandoned"
        assert completed_output["action"]["submission"] == "attached"
        assert audit["action_id"] == record["action"]["action_id"]
        assert audit["executor_generation"] == record["executor"]["generation"]
        assert audit["handshake"] is True
        final = states.load_run(run_id)
        assert final is not None and final["status"] == "abandoned"
        assert not any(
            item.get("code") == "execution_failed"
            for item in final.get("diagnostics", [])
        )
        mutations = json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
            "mutations"
        ]
        assert len(
            [item for item in mutations if item.get("action") == "close_parent_pr"]
        ) == 1
        executor_ready, _, _ = select.select([executor_pid_fd], [], [], 3)
        assert executor_ready == [executor_pid_fd]
    finally:
        for descriptor in (ready_read, attached_read, release_write):
            try:
                os.close(descriptor)
            except OSError:
                pass
        for pid in (observer_pid, reobserver_pid):
            if pid is None:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        if executor_pid_fd is not None:
            os.close(executor_pid_fd)


def test_abandon_executor_recovers_api_response_loss_without_duplicate_effect(
    git_repo: Path,
) -> None:
    """The control Action preserves Publisher dispatch/readback idempotence."""

    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    published = RunPublicationEngine(
        git=git,
        states=states,
        agents=RunPublicationAgents(),
        github=publisher,
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(run_id)
    pr_number = int(published["run_publication"]["pr_number"])
    fixture_path = git_repo / "github.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["delivery"]["crash_after_abandon_run_pr_once"] = True
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    task = TaskKey(git_repo, str(state["repository"]), int(state["parent"]["number"]))
    control = TaskControlStore(git_repo / ".agent-run")

    def execute_abandon(
        bound_run_id: str, _action_id: str, _generation: int
    ) -> dict[str, object]:
        fresh_publisher = FixtureGitHubPublisher(fixture_path, git)
        return RunPublicationEngine(
            git=git,
            states=states,
            agents=RunPublicationAgents(),
            github=fresh_publisher,
            default_branch="main",
            default_head_sha=git.resolve("main"),
        ).abandon(bound_run_id)

    def lifecycle() -> RunLifecycle:
        current = states.load_current_run(run_id)
        assert current is not None
        return RunLifecycle(
            states=states,
            control=control,
            host=FakeExecutorHost(separate_process=True),
            task=task,
            preflight=lambda: states.load_current_run(run_id),
            select_run=lambda _action: (current, True),
            initialize_profile=None,
            executor_spec=lambda bound_run_id, action_id, generation: ExecutorSpec(
                task=task,
                action_id=action_id,
                run_id=bound_run_id,
                generation=generation,
                cwd=git_repo,
                state_root=states.root,
            ),
            execute=lambda _run_id: current,
            execute_with_binding=execute_abandon,
        )

    request = LifecycleRequest(
        task=task,
        kind="abandon",
        payload={"parent": task.parent_number, "run_id": run_id, "discard_worktree": False},
    )
    with pytest.raises(ExecutorHostError):
        lifecycle().submit_control(request)

    failed = control.load(task)
    assert failed is not None
    assert failed["action"]["status"] == "failed"
    interrupted = states.load_run(run_id)
    assert interrupted is not None
    assert interrupted["status"] == "abandonment_pending"
    first_mutations = json.loads(fixture_path.read_text(encoding="utf-8"))["delivery"][
        "mutations"
    ]
    assert first_mutations.count(
        {"action": "close_final_run_pr", "pr_number": pr_number}
    ) == 1

    recovered, _resumed, receipt = lifecycle().submit_control(request)

    assert recovered["status"] == "abandoned"
    assert receipt is not None
    assert receipt.status == "completed"
    final_mutations = json.loads(fixture_path.read_text(encoding="utf-8"))["delivery"][
        "mutations"
    ]
    assert final_mutations.count(
        {"action": "close_final_run_pr", "pr_number": pr_number}
    ) == 1


@pytest.mark.parametrize(
    "phase",
    [
        "external_wait",
        "api_before_dispatch",
        "api_in_flight",
        "readback_before",
    ],
)
def test_stop_fences_executor_across_external_and_api_barriers(
    git_repo: Path, phase: str
) -> None:
    ticket = {
        "number": 3,
        "title": "Barrier ticket",
        "body": "",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }
    fixture_path = write_fixture(git_repo / "github.json", issues={"3": ticket})
    assert seed_run(git_repo, fixture_path).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    barrier_read, barrier_write = os.pipe()

    class BarrierPublisher(FixtureGitHubPublisher):
        barrier_triggered = False

        def _pause(self) -> None:
            if self.barrier_triggered:
                return
            self.barrier_triggered = True
            os.write(barrier_write, b"1")
            os.close(barrier_write)
            signal.pause()

        def _save(self) -> None:
            super()._save()
            closed = self._delivery().get("closed_issues")
            if phase == "api_in_flight" and closed == [3]:
                self._pause()

        def ticket_close_ownership(self, **arguments: object) -> dict[str, object] | None:
            if phase == "readback_before":
                self._pause()
            return super().ticket_close_ownership(**arguments)  # type: ignore[arg-type]

    old_executor_pid = os.fork()
    if old_executor_pid == 0:  # pragma: no branch - stopped at the barrier
        os.close(barrier_read)
        try:
            if phase == "external_wait":
                os.write(barrier_write, b"1")
                os.close(barrier_write)
                signal.pause()
            else:
                def pause_before_dispatch() -> None:
                    os.write(barrier_write, b"1")
                    os.close(barrier_write)
                    signal.pause()

                BarrierPublisher(
                    fixture_path, GitRepository(git_repo)
                ).close_primary_ticket(
                    ticket_number=3,
                    run_id=run_id,
                    pr_number=7,
                    integrated_sha="a" * 40,
                    before_dispatch=(
                        pause_before_dispatch
                        if phase == "api_before_dispatch"
                        else None
                    ),
                )
        finally:
            os._exit(0)

    os.close(barrier_write)
    try:
        ready, _, _ = select.select([barrier_read], [], [], 3)
        assert ready == [barrier_read]
        assert os.read(barrier_read, 1) == b"1"
        token = (
            Path(f"/proc/{old_executor_pid}/stat")
            .read_text(encoding="ascii")
            .rsplit(")", 1)[1]
            .split()[19]
        )
        old = control.claim_action(task, kind="run", payload={"parent": 1})
        assert old.action_id is not None
        reservation = control.begin_executor(
            task, action_id=old.action_id, run_id=None
        )
        control.mark_process_started(
            task,
            action_id=old.action_id,
            generation=reservation.generation,
            pid=old_executor_pid,
            process_start_token=token,
        )
        control.mark_handshake(
            task,
            action_id=old.action_id,
            generation=reservation.generation,
            pid=old_executor_pid,
            process_start_token=token,
        )
        control.bind_run(
            task,
            old.action_id,
            run_id,
            generation=reservation.generation,
            state_dir=states.root,
        )
        control.record_application(
            task,
            action_id=old.action_id,
            run_id=run_id,
            generation=reservation.generation,
            payload_digest=str(old.action["payload_digest"]),
        )
        control.complete_action(
            task,
            action_id=old.action_id,
            generation=reservation.generation,
            result_status=str(current["status"]),
        )

        def execute_stop(
            bound_run_id: str, action_id: str, _generation: int
        ) -> dict[str, object]:
            record = control.snapshot(task, action_id)
            action = record.get("action") if isinstance(record, dict) else None
            target = action.get("target_executor") if isinstance(action, dict) else None
            assert isinstance(target, dict)
            FakeExecutorHost().terminate_control_target(target)
            stopped = states.load_current_run(bound_run_id)
            assert stopped is not None
            record_operator_stop(
                stopped,
                save=lambda value: states.save_run(bound_run_id, value),
            )
            return stopped

        lifecycle = _control_lifecycle(
            states=states,
            control=control,
            task=task,
            current=current,
            host=FakeExecutorHost(),
        )
        lifecycle.execute_with_binding = execute_stop
        stopped, _resumed, receipt = lifecycle.submit_control(
            LifecycleRequest(
                task=task,
                kind="stop",
                payload={"parent": 1, "run_id": run_id},
            )
        )

        _, status = os.waitpid(old_executor_pid, 0)
        assert os.WIFSIGNALED(status)
        assert os.WTERMSIG(status) in {signal.SIGTERM, signal.SIGKILL}
        assert stopped["status"] == "operator_stopped"
        assert receipt is not None and receipt.status == "completed"
        delivery = json.loads(fixture_path.read_text(encoding="utf-8")).get(
            "delivery", {}
        )
        close_mutations = [
            item
            for item in delivery.get("mutations", [])
            if item.get("action") == "close_issue"
        ]
        expected_effects = 1 if phase in {"api_in_flight", "readback_before"} else 0
        assert len(close_mutations) == expected_effects
        assert len(delivery.get("closed_issues", [])) == expected_effects
        if expected_effects:
            FixtureGitHubPublisher(fixture_path, GitRepository(git_repo)).close_primary_ticket(
                ticket_number=3,
                run_id=run_id,
                pr_number=7,
                integrated_sha="a" * 40,
            )
            recovered_delivery = json.loads(
                fixture_path.read_text(encoding="utf-8")
            )["delivery"]
            assert len(
                [
                    item
                    for item in recovered_delivery["mutations"]
                    if item.get("action") == "close_issue"
                ]
            ) == 1
    finally:
        os.close(barrier_read)
        try:
            os.kill(old_executor_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(old_executor_pid, 0)
        except ChildProcessError:
            pass


def test_public_stop_rejects_unknown_ownership_without_mutating_run(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    seeded = seed_run(git_repo, fixture)
    assert seeded.returncode == 0, seeded.stderr
    states = StateStore(git_repo / ".agent-run")
    run = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(run["run_id"])
    state_path = states.runs_directory / f"{run_id}.json"
    before = state_path.read_bytes()

    rejected = _run_cli(git_repo, fixture, "stop", run_id)

    assert rejected.returncode == 2
    output = json.loads(rejected.stdout)
    assert output["diagnostics"][0]["code"] == "executor_start_unknown"
    assert state_path.read_bytes() == before

    run.update({"status": "completed", "terminal_kind": "completed"})
    states.save_run(run_id, run)
    terminal_before = state_path.read_bytes()

    terminal = _run_cli(git_repo, fixture, "stop", run_id)

    assert terminal.returncode == 0, terminal.stderr
    assert json.loads(terminal.stdout)["status"] == "completed"
    assert state_path.read_bytes() == terminal_before


@pytest.mark.parametrize("mode", ["recorded_absent", "pid_gone", "token_mismatch"])
def test_stop_proven_no_active_observations_are_strictly_read_only(
    git_repo: Path, mode: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    assert claim.action_id is not None
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=None)
    process: subprocess.Popen[bytes] | None = None
    if mode == "pid_gone":
        process = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            start_new_session=True,
        )
        pid = process.pid
        token = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="ascii")
            .rsplit(")", 1)[1]
            .split()[19]
        )
    else:
        pid = os.getpid()
        token = "definitely-not-this-process" if mode == "token_mismatch" else None
    control.mark_process_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=pid,
        process_start_token=token,
    )
    control.mark_handshake(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=pid,
        process_start_token=token,
    )
    control.bind_run(
        task,
        claim.action_id,
        run_id,
        generation=reservation.generation,
        state_dir=states.root,
    )
    control.record_application(
        task,
        action_id=claim.action_id,
        run_id=run_id,
        generation=reservation.generation,
        payload_digest=str(claim.action["payload_digest"]),
    )
    control.complete_action(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        result_status=str(current["status"]),
    )
    if mode == "recorded_absent":
        control.mark_executor_absent(
            task,
            action_id=claim.action_id,
            generation=reservation.generation,
        )
    elif process is not None:
        process.kill()
        process.wait(timeout=3)
    state_path = states.runs_directory / f"{run_id}.json"
    control_path = control.path_for(task)
    before_state = state_path.read_bytes()
    before_control = control_path.read_bytes()

    final, _resumed, receipt = _control_lifecycle(
        states=states,
        control=control,
        task=task,
        current=current,
        host=FakeExecutorHost(),
    ).submit_control(
        LifecycleRequest(
            task=task,
            kind="stop",
            payload={"parent": 1, "run_id": run_id},
        )
    )

    assert final["status"] == current["status"]
    assert receipt is None
    assert state_path.read_bytes() == before_state
    assert control_path.read_bytes() == before_control


def test_stop_unknown_host_observation_rejects_without_partial_mutation(
    git_repo: Path,
) -> None:
    class UnknownFakeHost(FakeExecutorHost):
        def observe(
            self, spec: ExecutorSpec, control: TaskControlStore
        ) -> HostObservation:
            del control
            return HostObservation(
                "unknown",
                spec.generation,
                spec.task.parent_number,
                False,
                "permission denied while observing Executor",
            )

    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    assert claim.action_id is not None
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=None)
    control.mark_process_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.mark_handshake(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.bind_run(
        task,
        claim.action_id,
        run_id,
        generation=reservation.generation,
        state_dir=states.root,
    )
    control.record_application(
        task,
        action_id=claim.action_id,
        run_id=run_id,
        generation=reservation.generation,
        payload_digest=str(claim.action["payload_digest"]),
    )
    control.complete_action(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        result_status=str(current["status"]),
    )
    state_path = states.runs_directory / f"{run_id}.json"
    control_path = control.path_for(task)
    before_state = state_path.read_bytes()
    before_control = control_path.read_bytes()

    with pytest.raises(ExecutorStartUnknownError, match="permission denied"):
        _control_lifecycle(
            states=states,
            control=control,
            task=task,
            current=current,
            host=UnknownFakeHost(),
        ).submit_control(
            LifecycleRequest(
                task=task,
                kind="stop",
                payload={"parent": 1, "run_id": run_id},
            )
        )

    assert state_path.read_bytes() == before_state
    assert control_path.read_bytes() == before_control


def test_stop_admission_rechecks_executor_exit_after_running_observation(
    git_repo: Path,
) -> None:
    class RunningBarrierHost(FakeExecutorHost):
        def observe(
            self, spec: ExecutorSpec, control: TaskControlStore
        ) -> HostObservation:
            del control
            observed.set()
            assert release.wait(3)
            return HostObservation(
                "running",
                spec.generation,
                os.getpid(),
                True,
            )

    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    record = control.load(task)
    assert record is not None
    executor = record["executor"]
    assert isinstance(executor, dict)
    observed = threading.Event()
    release = threading.Event()
    result: list[tuple[dict[str, object], bool, object]] = []
    failure: list[BaseException] = []

    def submit_stop() -> None:
        try:
            result.append(
                _control_lifecycle(
                    states=states,
                    control=control,
                    task=task,
                    current=current,
                    host=RunningBarrierHost(),
                ).submit_control(
                    LifecycleRequest(
                        task=task,
                        kind="stop",
                        payload={"parent": 1, "run_id": run_id},
                    )
                )
            )
        except BaseException as error:
            failure.append(error)

    state_path = states.runs_directory / f"{run_id}.json"
    control_path = control.path_for(task)
    worker.kill()
    worker.wait(timeout=3)
    submitter = threading.Thread(target=submit_stop)
    submitter.start()
    try:
        assert observed.wait(3)
        terminal = states.load_current_run(run_id)
        assert terminal is not None
        terminal.update({"status": "completed", "terminal_kind": "completed"})
        states.save_run(run_id, terminal)
        control.mark_executor_absent(
            task,
            action_id=str(executor["action_id"]),
            generation=int(executor["generation"]),
        )
        after_exit_state = state_path.read_bytes()
        after_exit_control = control_path.read_bytes()
        after_exit_record = control.load(task)
        assert after_exit_record is not None
        after_exit_history = after_exit_record.get("action_history")
        after_exit_action = after_exit_record.get("action")
        release.set()
        submitter.join(timeout=3)
        assert not submitter.is_alive()

        assert failure == []
        final, _resumed, receipt = result[0]
        assert receipt is None
        assert final["status"] == "completed"
        assert cli_module.cli_presentation._next_action(final) == "无"
        assert state_path.read_bytes() == after_exit_state
        assert control_path.read_bytes() == after_exit_control
        final_record = control.load(task)
        assert final_record is not None
        assert final_record.get("action") == after_exit_action
        assert final_record.get("action_history") == after_exit_history
    finally:
        release.set()
        submitter.join(timeout=3)


@pytest.mark.parametrize(
    ("observation_status", "becomes_terminal"),
    [("running", True), ("absent", True), ("absent", False)],
)
@pytest.mark.parametrize("as_json", [True, False])
def test_public_stop_reloads_state_after_executor_exit(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    observation_status: str,
    becomes_terminal: bool,
    as_json: bool,
) -> None:
    observed = threading.Event()
    release = threading.Event()

    class ExitBarrierHost:
        def __init__(self, **_options: object) -> None:
            pass

        def check_readiness(self) -> None:
            pass

        def observe(
            self, spec: ExecutorSpec, control: TaskControlStore
        ) -> HostObservation:
            del control
            observed.set()
            assert release.wait(3)
            return HostObservation(
                observation_status,
                spec.generation,
                None,
                observation_status == "running",
            )

    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / "runner-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    record = control.load(task)
    assert record is not None
    executor = record["executor"]
    assert isinstance(executor, dict)
    state_path = states.runs_directory / f"{run_id}.json"
    control_path = control.path_for(task)
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(cli_module, "_running_active_runner", lambda: True)
    monkeypatch.setattr(cli_module, "SystemdUserExecutorHost", ExitBarrierHost)
    result: list[int] = []
    arguments = ["stop", "1", "--repo", "example/project"]
    if as_json:
        arguments.append("--json")
    submitter = threading.Thread(target=lambda: result.append(cli_module.main(arguments)))
    submitter.start()
    try:
        assert observed.wait(3)
        worker.kill()
        worker.wait(timeout=3)
        if becomes_terminal:
            terminal = states.load_current_run(run_id)
            assert terminal is not None
            terminal.update({"status": "completed", "terminal_kind": "completed"})
            states.save_run(run_id, terminal)
        if observation_status == "running":
            control.mark_executor_absent(
                task,
                action_id=str(executor["action_id"]),
                generation=int(executor["generation"]),
            )
        after_exit_state = state_path.read_bytes()
        after_exit_control = control_path.read_bytes()
        release.set()
        submitter.join(timeout=3)
        assert not submitter.is_alive()
        assert result == [0]

        captured = capsys.readouterr()
        assert captured.err == ""
        if as_json:
            output = json.loads(captured.out)
            if becomes_terminal:
                assert output["result"] == "resumed"
                assert output["status"] == "completed"
                assert output["next_action"] == "无"
            else:
                assert output["result"] == "no_active_executor"
                assert output["status"] == current["status"]
                assert output["next_action"] == "agent-run run 1"
            assert "action" not in output
        elif becomes_terminal:
            assert "当前没有正在运行的 Agent" not in captured.out
            assert "交付状态: 整个交付已完成" in captured.out
            assert "下一步: 无" in captured.out
        else:
            assert "当前没有正在运行的 Agent" in captured.out
            assert "下一步: agent-run run 1 --repo example/project" in captured.out
        assert state_path.read_bytes() == after_exit_state
        assert control_path.read_bytes() == after_exit_control
    finally:
        release.set()
        submitter.join(timeout=3)
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)


def test_stop_noop_reload_fails_closed_after_successor_admission(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = threading.Event()
    release_observation = threading.Event()
    reload_started = threading.Event()
    release_reload = threading.Event()

    class RunningBarrierHost(FakeExecutorHost):
        def observe(
            self, spec: ExecutorSpec, control: TaskControlStore
        ) -> HostObservation:
            del control
            observed.set()
            assert release_observation.wait(3)
            return HostObservation("running", spec.generation, os.getpid(), True)

    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    record = control.load(task)
    assert record is not None
    executor = record["executor"]
    assert isinstance(executor, dict)
    original_load = states.load_current_run
    load_count = 0

    def load_with_reload_barrier(bound_run_id: str) -> dict[str, object] | None:
        nonlocal load_count
        load_count += 1
        if load_count == 2:
            reload_started.set()
            assert release_reload.wait(3)
        return original_load(bound_run_id)

    monkeypatch.setattr(states, "load_current_run", load_with_reload_barrier)
    result: list[tuple[dict[str, object], bool, object]] = []
    failure: list[BaseException] = []

    def submit_stop() -> None:
        try:
            result.append(
                _control_lifecycle(
                    states=states,
                    control=control,
                    task=task,
                    current=current,
                    host=RunningBarrierHost(),
                ).submit_control(
                    LifecycleRequest(
                        task=task,
                        kind="stop",
                        payload={"parent": 1, "run_id": run_id},
                    )
                )
            )
        except BaseException as error:
            failure.append(error)

    worker.kill()
    worker.wait(timeout=3)
    submitter = threading.Thread(target=submit_stop)
    submitter.start()
    try:
        assert observed.wait(3)
        control.mark_executor_absent(
            task,
            action_id=str(executor["action_id"]),
            generation=int(executor["generation"]),
        )
        release_observation.set()
        assert reload_started.wait(3)
        resume = control.claim_action(
            task,
            kind="resume",
            payload={"parent": 1, "run_id": run_id},
        )
        assert resume.action_id is not None
        release_reload.set()
        submitter.join(timeout=3)
        assert not submitter.is_alive()

        assert result == []
        assert len(failure) == 1
        assert isinstance(failure[0], ActionReconciliationError)
        latest = control.load(task)
        assert latest is not None
        assert latest["action"]["action_id"] == resume.action_id
        assert latest["action"]["kind"] == "resume"
    finally:
        release_observation.set()
        release_reload.set()
        submitter.join(timeout=3)


def test_read_only_stop_rechecks_action_admission_in_one_transaction(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    record = control.load(task)
    assert record is not None
    executor = record["executor"]
    assert isinstance(executor, dict)
    worker.kill()
    worker.wait(timeout=3)
    control.mark_executor_absent(
        task,
        action_id=str(executor["action_id"]),
        generation=int(executor["generation"]),
    )
    proof_started = threading.Event()
    release_proof = threading.Event()
    original_proof = TaskControlStore.proves_read_only_stop

    def barrier_proof(
        store: TaskControlStore,
        proof_task: TaskKey,
        run_state: dict[str, object],
    ) -> bool:
        proof_started.set()
        assert release_proof.wait(3)
        return original_proof(store, proof_task, run_state)

    monkeypatch.setattr(TaskControlStore, "proves_read_only_stop", barrier_proof)
    result: list[dict[str, object] | None] = []
    checker = threading.Thread(
        target=lambda: result.append(
            cli_module._read_only_stop_result(
                SimpleNamespace(run_id=run_id),
                states,
                GitRepository(git_repo),
            )
        )
    )
    checker.start()
    try:
        assert proof_started.wait(3)
        resume = control.claim_action(
            task,
            kind="resume",
            payload={"parent": 1, "run_id": run_id},
        )
        assert resume.action_id is not None
        release_proof.set()
        checker.join(timeout=3)
        assert not checker.is_alive()
        assert result == [None]
        state_path = states.runs_directory / f"{run_id}.json"
        before_state = state_path.read_bytes()
        before_control = control.path_for(task).read_bytes()

        with pytest.raises(ActionBusyError, match="不会等待或排队"):
            _control_lifecycle(
                states=states,
                control=control,
                task=task,
                current=current,
                host=FakeExecutorHost(),
            ).submit_control(
                LifecycleRequest(
                    task=task,
                    kind="stop",
                    payload={"parent": 1, "run_id": run_id},
                )
            )

        assert state_path.read_bytes() == before_state
        assert control.path_for(task).read_bytes() == before_control
        reservation = control.begin_executor(
            task, action_id=resume.action_id, run_id=run_id
        )
        assert reservation.created
    finally:
        release_proof.set()
        checker.join(timeout=3)


@pytest.mark.parametrize(
    ("executor_proof", "unavailable_dependency"),
    [
        ("absent", "active_runner"),
        ("exited", "systemd"),
        ("terminal", "profile"),
        ("terminal_running", "active_runner"),
    ],
)
def test_production_read_only_stop_skips_execution_dependencies(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    executor_proof: str,
    unavailable_dependency: str,
) -> None:
    class AvailableSystemdHost:
        def __init__(self, **_options: object) -> None:
            pass

        def check_readiness(self) -> None:
            pass

    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / "runner-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    record = control.load(task)
    assert record is not None
    executor = record["executor"]
    assert isinstance(executor, dict)
    worker_still_running = executor_proof == "terminal_running"
    if not worker_still_running:
        worker.kill()
        worker.wait(timeout=3)
        if executor_proof == "absent":
            control.mark_executor_absent(
                task,
                action_id=str(executor["action_id"]),
                generation=int(executor["generation"]),
            )
        else:
            control.finish_executor(
                task,
                action_id=str(executor["action_id"]),
                generation=int(executor["generation"]),
                result_status=str(current["status"]),
            )
    if executor_proof in {"terminal", "terminal_running"}:
        terminal = states.load_run(run_id)
        assert terminal is not None
        terminal.update({"status": "completed", "terminal_kind": "completed"})
        states.save_run(run_id, terminal)

    profile_path = git_repo / ".agent-run" / "profiles" / f"{run_id}.json"
    if unavailable_dependency == "profile":
        profile_path.unlink()
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(
        cli_module,
        "_running_active_runner",
        lambda: unavailable_dependency != "active_runner",
    )
    if unavailable_dependency == "systemd":
        monkeypatch.setattr(
            cli_module,
            "SystemdUserExecutorHost",
            lambda **_options: pytest.fail(
                "read-only Stop must not construct a systemd Host"
            ),
        )
    else:
        monkeypatch.setattr(
            cli_module, "SystemdUserExecutorHost", AvailableSystemdHost
        )
    state_path = states.runs_directory / f"{run_id}.json"
    control_path = control.path_for(task)
    before_state = state_path.read_bytes()
    before_control = control_path.read_bytes()

    try:
        return_code = cli_module.main(
            ["stop", "1", "--repo", "example/project", "--json"]
        )

        assert return_code == 0
        output = json.loads(capsys.readouterr().out)
        if executor_proof in {"terminal", "terminal_running"}:
            assert output["status"] == "completed"
            assert output["result"] == "resumed"
        else:
            assert output["result"] == "no_active_executor"
            assert output["diagnostics"][-1]["code"] == "no_active_executor"
        assert "action" not in output
        assert state_path.read_bytes() == before_state
        assert control_path.read_bytes() == before_control
    finally:
        if worker_still_running:
            worker.kill()
            worker.wait(timeout=3)


def test_production_stop_with_active_executor_still_requires_readiness(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / "runner-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    control, task, worker = _bind_running_executor(git_repo, run_id)
    state_path = states.runs_directory / f"{run_id}.json"
    control_path = control.path_for(task)
    before_state = state_path.read_bytes()
    before_control = control_path.read_bytes()
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(cli_module, "_running_active_runner", lambda: False)
    try:
        return_code = cli_module.main(
            ["stop", "1", "--repo", "example/project", "--json"]
        )

        assert return_code == 2
        output = json.loads(capsys.readouterr().out)
        assert output["result"] == "error"
        assert state_path.read_bytes() == before_state
        assert control_path.read_bytes() == before_control
    finally:
        worker.kill()
        worker.wait(timeout=3)


@pytest.mark.parametrize("pending_kind", ["run", "resume"])
def test_production_stop_rejects_pending_action_before_execution_readiness(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pending_kind: str,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / "runner-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    control.claim_action(task, kind=pending_kind, payload={"parent": 1})
    state_path = states.runs_directory / f"{run_id}.json"
    control_path = control.path_for(task)
    before_state = state_path.read_bytes()
    before_control = control_path.read_bytes()
    readiness_checks: list[bool] = []
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(
        cli_module,
        "_running_active_runner",
        lambda: readiness_checks.append(True) or False,
    )

    return_code = cli_module.main(
        ["stop", "1", "--repo", "example/project", "--json"]
    )

    assert return_code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][-1]["code"] == "action_busy"
    assert readiness_checks == []
    assert state_path.read_bytes() == before_state
    assert control_path.read_bytes() == before_control


def test_terminal_abandon_replay_completes_its_applying_control_action(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    seeded = seed_run(git_repo, fixture)
    assert seeded.returncode == 0, seeded.stderr
    states = StateStore(git_repo / ".agent-run")
    run = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(run["run_id"])
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    claim = control.claim_control_action(
        task,
        kind="abandon",
        payload={"parent": 1, "run_id": run_id, "discard_worktree": False},
        run_id=run_id,
        state_dir=states.root,
    )
    run.update({"status": "abandoned", "terminal_kind": "abandoned"})
    states.save_run(run_id, run)
    before_state = (states.runs_directory / f"{run_id}.json").read_bytes()

    replayed = _run_cli(git_repo, fixture, "abandon", run_id)

    assert replayed.returncode == 0, replayed.stderr
    assert json.loads(replayed.stdout)["status"] == "abandoned"
    assert (states.runs_directory / f"{run_id}.json").read_bytes() == before_state
    record = control.load(task)
    assert record is not None
    assert record["action"]["action_id"] == claim.action_id
    assert record["action"]["status"] == "completed"


@pytest.mark.parametrize(
    ("kind", "durable_status"),
    [("stop", "operator_stopped"), ("abandon", "abandoned")],
)
def test_control_action_reconciles_durable_outcome_after_executor_exit(
    git_repo: Path, kind: str, durable_status: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(git_repo / ".agent-run")
    current = states.find_unfinished_runs("example/project", 1)[0]
    run_id = str(current["run_id"])
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(git_repo / ".agent-run")
    payload = {"parent": 1, "run_id": run_id}
    if kind == "abandon":
        payload["discard_worktree"] = True
    else:
        old = control.claim_action(task, kind="run", payload={"parent": 1})
        assert old.action_id is not None
        old_reservation = control.begin_executor(
            task, action_id=old.action_id, run_id=None
        )
        control.mark_process_started(
            task,
            action_id=old.action_id,
            generation=old_reservation.generation,
            pid=os.getpid(),
            process_start_token=None,
        )
        control.mark_handshake(
            task,
            action_id=old.action_id,
            generation=old_reservation.generation,
            pid=os.getpid(),
            process_start_token=None,
        )
        control.bind_run(
            task,
            old.action_id,
            run_id,
            generation=old_reservation.generation,
            state_dir=states.root,
        )
        control.record_application(
            task,
            action_id=old.action_id,
            run_id=run_id,
            generation=old_reservation.generation,
            payload_digest=str(old.action["payload_digest"]),
        )
        control.complete_action(
            task,
            action_id=old.action_id,
            generation=old_reservation.generation,
            result_status=str(current["status"]),
        )
    claim = control.claim_control_action(
        task,
        kind=kind,
        payload=payload,
        run_id=run_id,
        state_dir=states.root,
    )
    reservation = control.begin_executor(
        task, action_id=claim.action_id, run_id=run_id
    )
    control.mark_process_started(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    control.mark_handshake(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
        pid=os.getpid(),
        process_start_token=None,
    )
    current = states.load_run(run_id)
    assert current is not None
    prepare_action_application_receipt(current, claim.action)
    current.update({"status": durable_status, "terminal_kind": durable_status})
    states.save_run(run_id, current)
    control.record_application(
        task,
        action_id=claim.action_id,
        run_id=run_id,
        generation=reservation.generation,
        payload_digest=str(claim.action["payload_digest"]),
    )
    control.mark_executor_absent(
        task,
        action_id=claim.action_id,
        generation=reservation.generation,
    )
    state_path = states.runs_directory / f"{run_id}.json"
    before = state_path.read_bytes()

    final, _resumed, receipt = _control_lifecycle(
        states=states,
        control=control,
        task=task,
        current=current,
        host=FakeExecutorHost(),
    ).submit_control(LifecycleRequest(task=task, kind=kind, payload=payload))

    assert final["status"] == durable_status
    assert state_path.read_bytes() == before
    assert receipt is not None
    assert receipt.status == "completed"
    record = control.load(task)
    assert record is not None
    assert record["action"]["status"] == "completed"
