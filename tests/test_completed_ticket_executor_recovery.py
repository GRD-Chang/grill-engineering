from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.executor_host import ExecutorSpec, FakeExecutorHost
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_lifecycle import LifecycleRequest, RunLifecycle, prepare_action_application_receipt
from agent_run.state_contract import require_current_run_state
from agent_run.task_control import TaskControlStore, TaskKey
from run_acceptance_test_support import _completed_run


def test_completed_ticket_survives_exited_executor_and_can_continue(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    state["status"] = "ticket_completed"
    state["terminal_kind"] = None
    state["active_ticket_job"] = deepcopy(state["ticket_jobs"]["2"])
    require_current_run_state(state)
    run_id = str(state["run_id"])
    states.save_run(run_id, state)
    task = TaskKey(git_repo, "example/project", 1)
    control = TaskControlStore(states.root)
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    assert claim.action_id is not None
    control.bind_run(task, claim.action_id, run_id)
    spec = ExecutorSpec(
        task=task, action_id=claim.action_id, run_id=run_id, generation=1,
        state_root=states.root,
    )

    def interrupted_after_completion() -> dict[str, Any]:
        record = control.load(task)
        assert record is not None
        prepare_action_application_receipt(state, record["action"])
        states.save_run(run_id, state)
        control.record_application(
            task, action_id=spec.action_id, run_id=run_id, generation=1,
            payload_digest=record["action"]["payload_digest"],
        )
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        FakeExecutorHost().ensure(spec, control, execute=interrupted_after_completion)
    completed_job = deepcopy(state["active_ticket_job"])
    host = FakeExecutorHost()
    controller = Controller(FixtureGitHubReader(git_repo / "github.json"), git, states)

    def continue_run(action: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        current, resumed = controller.resume(run_id)
        prepare_action_application_receipt(current, action)
        states.save_run(run_id, current)
        return current, resumed

    lifecycle = RunLifecycle(
        states=states, control=control, task=task, host=host,
        preflight=lambda: states.load_current_run(run_id),
        select_run=continue_run, initialize_profile=None,
        executor_spec=lambda selected_run_id, action_id, generation: ExecutorSpec(
            task=task, action_id=action_id, run_id=selected_run_id,
            generation=generation, state_root=states.root,
        ),
        execute=lambda _run_id: states.load_current_run(run_id) or {},
    )

    recovered, _, _ = lifecycle.submit(
        LifecycleRequest(task=task, kind="run", payload={"parent": 1})
    )

    require_current_run_state(states.load_current_run(run_id))
    assert recovered["status"] == "run_acceptance_pending"
    assert recovered["ticket_jobs"]["2"] == completed_job
    assert not any(d["code"] == "session_interrupted" for d in recovered["diagnostics"])
    assert host.start_count == 1
