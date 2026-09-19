from __future__ import annotations

import os
import sys
from copy import deepcopy
from dataclasses import replace
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from support.workspace import managed_repo, managed_state

from agent_run.executor_host import ExecutorSpec, ExecutorStartUnknownError
from agent_run.cli_presentation import human_next_action_for_state
from agent_run.git import GitRepository
from agent_run.review_budget import new_budget
from agent_run.run_lifecycle import (
    LifecycleRequest,
    RunLifecycle,
    prepare_action_application_receipt,
)
from agent_run.state import StateStore
from agent_run.systemd_executor_host import (
    FakeSystemdTransport,
    SystemdUnitObservation,
    SystemdUserExecutorHost,
)
from agent_run.task_control import ActionReconciliationError, TaskControlStore, TaskKey
from conftest import seed_run, write_fixture
from test_run_lifecycle import _file_snapshot, _isolated_environment


@pytest.fixture
def completed_action(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[StateStore, TaskControlStore, ExecutorSpec, SystemdUserExecutorHost, FakeSystemdTransport]:
    for name, value in _isolated_environment(tmp_path / "environment").items():
        monkeypatch.setenv(name, value)
    fixture = write_fixture(git_repo / "github.json", issues={})
    assert seed_run(git_repo, fixture).returncode == 0
    states = StateStore(managed_state(git_repo))
    state = states.find_unfinished_runs("example/project", 1)[0]
    assert state["status"] == "parent_delivery_pending"
    run_id = state["run_id"]
    # Host reconciliation observes existing work; no failed Agent invocation is
    # needed to establish it. Keep a real checkout and nonempty job to protect.
    state["parent_job"] = {
        "run_id": run_id,
        "parent_branch": state["parent_branch"],
        "base_sha": state["base"]["sha"],
        "effective_revision": state["parent"]["revision"],
        "parent_generation": 1,
        "phase": "developing",
        "development_thread_id": "parent-development",
        "development_thread_history": [],
        "reviewer_thread_ids": [],
        "modification_attempts": 1,
        "validation_attempts": 0,
        "acceptance_artifact": None,
        "policy_snapshot": deepcopy(state["policy_snapshot"]),
        "review_budget": {**new_budget(), "development_attempts": 1},
        "review_budget_history": [],
    }
    checkout = states.root / "worktrees" / run_id / "parent"
    GitRepository(managed_repo(git_repo)).prepare_ticket_checkout(
        branch=state["parent_branch"], base_sha=state["base"]["sha"], checkout=checkout,
    )
    (checkout / "partial-work.txt").write_text("preserve the unfinished work\n")
    task = TaskKey(managed_repo(git_repo), "example/project", 1)
    control = TaskControlStore(states.root)
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, run_id)
    record = control.load(task)
    assert record is not None
    spec = ExecutorSpec(
        task=task, run_id=run_id, action_id=claim.action_id,
        generation=record["action"]["executor_generation"], state_root=states.root,
        command=("run", "1"), cwd=managed_repo(git_repo),
    )
    transport = FakeSystemdTransport()
    host = SystemdUserExecutorHost(
        transport=transport, runtime_directory=tmp_path / "runtime",
        environment=dict(os.environ), executor_python=Path(sys.executable),
    )
    assert host.ensure(spec, control).status == "starting"
    control.mark_process_started(
        task, action_id=spec.action_id, generation=spec.generation,
        pid=os.getpid(), process_start_token=None,
    )
    control.mark_handshake(
        task, action_id=spec.action_id, generation=spec.generation,
        pid=os.getpid(), process_start_token=None,
    )
    record = control.load(task)
    assert record is not None
    prepare_action_application_receipt(state, record["action"])
    states.save_run(run_id, state)
    control.record_application(
        task, action_id=spec.action_id, run_id=run_id, generation=spec.generation,
        payload_digest=record["action"]["payload_digest"],
    )
    control.complete_action(
        task, action_id=spec.action_id, generation=spec.generation,
        result_status=state["status"],
    )
    transport.unit = SystemdUnitObservation(
        "running", str(transport.launches[0]["description"]), os.getpid(), None
    )
    assert host.inspect(spec, control).status == "running"
    return states, control, spec, host, transport


def _lifecycle(
    states: StateStore, control: TaskControlStore, spec: ExecutorSpec,
    host: SystemdUserExecutorHost,
) -> RunLifecycle:
    def forbidden(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("Host observation must not create or execute new work")

    return RunLifecycle(
        states=states, control=control, task=spec.task, host=host,
        preflight=lambda: states.load_current_run(str(spec.run_id)),
        select_run=forbidden, initialize_profile=None,
        executor_spec=lambda run_id, action_id, generation: replace(
            spec, run_id=run_id, action_id=action_id, generation=generation
        ),
        execute=forbidden, prepare_executor_session=forbidden,
        clock=count().__next__, sleep=forbidden, startup_timeout=0,
    )


@pytest.mark.parametrize("native_status", ["exited", "absent"])
def test_systemd_completed_action_exit_closes_only_its_executor(
    completed_action: tuple, native_status: str, git_repo: Path,
) -> None:
    states, control, spec, host, transport = completed_action
    initial = control.load(spec.task)
    before = states.load_current_run(spec.run_id)
    assert initial is not None and before is not None
    worktrees = _file_snapshot(states.root / "worktrees")
    fixture = git_repo / "github.json"
    fixture_before = fixture.read_bytes()
    transport.unit = replace(
        transport.unit, status=native_status, pid=None,
        description=transport.unit.description if native_status == "exited" else None,
        reason="Main process exited, code=killed, status=9/KILL",
    )
    lifecycle = _lifecycle(states, control, spec, host)
    request = LifecycleRequest(task=spec.task, kind="run", payload={"parent": 1})
    result, _, receipt = lifecycle.submit(request)
    assert result["status"] == "execution_failed"
    assert result["diagnostics"][0]["code"] == "session_interrupted"
    assert human_next_action_for_state(result) == "agent-run resume 1 --repo example/project"
    assert result["parent_job"] == before["parent_job"]
    assert receipt.status == "completed"
    record = control.load(spec.task)
    assert record["action"] == initial["action"]
    assert record["executor"]["status"] == "exited"
    assert record["executor"]["failure"] == "session_interrupted"
    assert record["executor"]["generation"] == spec.generation
    assert record["executor"]["runner_binding"] == initial["executor"]["runner_binding"]
    control_before = control.path_for(spec.task).read_bytes()
    states_before = _file_snapshot(states.root / "runs")
    host.inspect(spec, control)
    again, _, _ = lifecycle.submit(request)
    assert again == result
    assert control.path_for(spec.task).read_bytes() == control_before
    assert _file_snapshot(states.root / "runs") == states_before
    assert _file_snapshot(states.root / "worktrees") == worktrees
    assert fixture.read_bytes() == fixture_before
    assert transport.start_count == 1
    assert not list(host.runtime_directory.glob("*.json"))


@pytest.mark.parametrize("final_status", ["completed", "run_approval_pending", "ready_for_human"])
def test_systemd_completed_action_normal_boundary_is_preserved(
    completed_action: tuple, final_status: str,
) -> None:
    states, control, spec, host, transport = completed_action
    final = states.load_current_run(spec.run_id)
    final["status"] = final_status
    final["terminal_kind"] = "completed" if final_status == "completed" else "waiting_human"
    states.save_run(spec.run_id, final)
    state_before = _file_snapshot(states.root / "runs")
    action_before = control.load(spec.task)["action"]
    transport.unit = replace(transport.unit, status="exited", pid=None)
    assert host.inspect(spec, control).status == "exited"
    assert control.load(spec.task)["executor"]["status"] == "exited"
    result, _, receipt = _lifecycle(states, control, spec, host).submit(
        LifecycleRequest(task=spec.task, kind="run", payload={"parent": 1})
    )
    assert result["status"] == final_status and receipt.status == "completed"
    assert control.load(spec.task)["action"] == action_before
    assert _file_snapshot(states.root / "runs") == state_before
    assert transport.start_count == 1


@pytest.mark.parametrize("native_status", ["running", "unknown", "conflict"])
def test_systemd_completed_action_without_exit_proof_is_not_closed(
    completed_action: tuple, native_status: str,
) -> None:
    states, control, spec, host, transport = completed_action
    if native_status == "unknown":
        transport.unit = SystemdUnitObservation("unknown", None, None, "manager unavailable")
    elif native_status == "conflict":
        transport.unit = replace(transport.unit, description="another Executor")
    control_before = control.path_for(spec.task).read_bytes()
    state_before = _file_snapshot(states.root / "runs")
    lifecycle = _lifecycle(states, control, spec, host)
    request = LifecycleRequest(task=spec.task, kind="run", payload={"parent": 1})
    if native_status == "running":
        result, _, receipt = lifecycle.submit(request)
        assert result["status"] == "parent_delivery_pending" and receipt.attached
    else:
        with pytest.raises(ExecutorStartUnknownError):
            lifecycle.submit(request)
    assert control.path_for(spec.task).read_bytes() == control_before
    assert _file_snapshot(states.root / "runs") == state_before
    assert transport.start_count == 1


@pytest.mark.parametrize("binding", ["action", "run", "generation", "runner", "state_root"])
def test_systemd_completed_action_rejects_foreign_native_exit_binding(
    completed_action: tuple, binding: str,
) -> None:
    states, control, spec, host, transport = completed_action
    foreign = {
        "action": replace(spec, action_id="another-action"),
        "run": replace(spec, run_id="another-run"),
        "generation": replace(spec, generation=spec.generation + 1),
        "runner": replace(spec, runner_binding="f" * 16),
        "state_root": replace(spec, state_root=states.root / "another-state"),
    }[binding]
    transport.unit = SystemdUnitObservation(
        "exited", host._description(foreign), None, "signal 9"
    )
    before = control.path_for(spec.task).read_bytes()
    assert host.inspect(spec, control).status == "conflict"
    assert control.path_for(spec.task).read_bytes() == before
    assert transport.start_count == 1


@pytest.mark.parametrize("binding", ["run", "runner"])
@pytest.mark.parametrize("already_exited", [False, True])
def test_host_exit_evidence_is_checked_before_atomic_finish_or_noop(
    completed_action: tuple, binding: str, already_exited: bool,
) -> None:
    _states, control, spec, _host, _transport = completed_action
    if already_exited:
        control.finish_executor(
            spec.task, action_id=spec.action_id, generation=spec.generation,
            failure="session_interrupted",
        )
    record = control.load(spec.task)
    before = control.path_for(spec.task).read_bytes()
    with pytest.raises(ActionReconciliationError, match="Run/Runner binding"):
        control.finish_executor(
            spec.task, action_id=spec.action_id, generation=spec.generation,
            run_id="another-run" if binding == "run" else spec.run_id,
            runner_binding=(
                "f" * 16 if binding == "runner" else record["executor"]["runner_binding"]
            ),
            failure="late Host proof",
        )
    assert control.path_for(spec.task).read_bytes() == before
