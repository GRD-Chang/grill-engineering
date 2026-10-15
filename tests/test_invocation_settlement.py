"""跨解释器迟到结果只能收口准确 Invocation，不能推进阶段。"""
from __future__ import annotations

from copy import deepcopy
from functools import partial
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_run.executor_host import _process_start_token
from agent_run.object_state import object_store
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state import SimulatedProcessCrash
from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.worker_control import worker_lease
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("revocation", ["stop", "lease"])
def test_late_return_settles_exact_attempt_without_stage_or_budget_change(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, revocation: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    control = TaskControlStore(states.root)
    task = TaskKey(git.root, state["repository"], 1)
    claim = control.claim_action(task, kind="run", payload={})
    action_id = claim.action_id
    generation = control.begin_executor(task, action_id=action_id, run_id=run_id).generation
    control.mark_handshake(task, action_id=action_id, generation=generation,
                           pid=os.getpid(), process_start_token=_process_start_token(os.getpid()))
    objects = object_store(states)
    current = objects.load_current_run(run_id)
    prepare_action_application_receipt(current, claim.action)
    objects.save_run(run_id, current)
    control.complete_action(task, action_id=action_id, generation=generation)
    states._set_write_guard(None, transaction=partial(
        control._executor_current_transaction, task, action_id=action_id,
        generation=generation, run_id=run_id,
    ))
    captured = []
    leases = []
    real_save = states._save_work_and_notify_unlocked

    def before_return_commit(saved_run, value, baseline, **kwargs):
        pending = value["ticket_jobs"].get("2", {}).get("pending_semantic_attempt", {})
        if "returned_result" in pending:
            captured.append(deepcopy(value))
            leases.append(worker_lease.get())
            raise SimulatedProcessCrash("cut before returned_result commit")
        return real_save(saved_run, value, baseline, **kwargs)

    with monkeypatch.context() as cut:
        cut.setattr(states, "_save_work_and_notify_unlocked", before_return_commit)
        with pytest.raises(SimulatedProcessCrash):
            ticket_engine(git, states, fixture, TicketAgents(git_repo, (2,), lambda _: None),
                          development_only=True).deliver_ticket(run_id, 2)
    assert len(captured) == 1
    lease = leases[0]
    assert lease is not None
    proposed = captured[0]
    source = git_repo / "late-return.json"
    source.write_text(json.dumps(proposed), encoding="utf-8")
    if revocation == "stop":
        stop = control.claim_control_action(
            task, kind="stop", payload={}, run_id=run_id, state_dir=states.root,
        )
        control.begin_executor(task, action_id=stop.action_id, run_id=run_id)
        # This independent caller owns the new Stop generation.
        from agent_run.state import StateStore
        stopped = object_store(StateStore(states.root))
        latest = stopped.load_current_run(run_id)
        latest.update(status="operator_stopped", terminal_kind="operator_stopped")
        latest["ticket_work_queue"] = []
        prepare_action_application_receipt(latest, stop.action)
        stopped.save_run(run_id, latest)
    before = states.load_current_run(run_id)
    assert "returned_result" not in before["ticket_jobs"]["2"]["pending_semantic_attempt"]
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[7])
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.ticket_state import TicketState
from agent_run.worker_control import worker_lease
from agent_run.invocation_settlement import settlement_scope
from agent_run.object_state import save_owned
from agent_run.task_control import ActionReconciliationError, TaskControlBusyError
from agent_run.state import ConcurrentRunStateError
from copy import deepcopy
root, workspace, source, action_id, generation, lease = sys.argv[1:7]
proposed = json.loads(Path(source).read_text())
run_id = proposed["run_id"]
states = StateStore(Path(root))
control = TaskControlStore(Path(root))
task = TaskKey(Path(workspace), proposed["repository"], 1)
states._set_write_guard(None, transaction=lambda: control._executor_current_transaction(
    task, action_id=action_id, generation=int(generation), run_id=run_id))
lane = TicketState(states, run_id, 2)
lane.load_current_run(run_id)
worker_lease.set(lease)
try:
    lane.save_run(run_id, proposed)
except (ActionReconciliationError, TaskControlBusyError):
    pass
else:
    raise AssertionError("normal advancement retained revoked permission")
if control.load(task)["action"]["action_id"] != action_id:
    try:
        with settlement_scope(proposed):
            save_owned(states, run_id, {"status": "active"}, fields={"status"})
    except ActionReconciliationError:
        pass
    else:
        raise AssertionError("settlement granted generic business mutation")
lane = TicketState(states, run_id, 2)
lane.load_current_run(run_id)
wrong = deepcopy(proposed)
wrong["active_agent_invocation"]["generation"] += 1
try:
    with settlement_scope(wrong):
        lane.save_run(run_id, wrong)
except ConcurrentRunStateError:
    pass
else:
    raise AssertionError("a different Invocation identity was accepted")
lane = TicketState(states, run_id, 2)
lane.load_current_run(run_id)
with settlement_scope(proposed):
    lane.save_run(run_id, proposed)
# The same factual closeout is idempotent; subsequent advancement still fails.
with settlement_scope(proposed):
    lane.save_run(run_id, proposed)
proposed["active_ticket_job"]["phase"] = "committing_candidate"
try:
    lane.save_run(run_id, proposed)
except (ActionReconciliationError, TaskControlBusyError):
    pass
else:
    raise AssertionError("settlement authorized a later stage")
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(states.root), str(git.root), str(source),
         action_id, str(generation), lease, str(Path(__file__).parents[1] / "src")],
        capture_output=True, text=True, timeout=20,
    )
    assert child.returncode == 0, child.stderr
    after = states.load_current_run(run_id)
    expected = deepcopy(before)
    result = proposed["ticket_jobs"]["2"]["pending_semantic_attempt"]["returned_result"]
    expected["ticket_jobs"]["2"]["pending_semantic_attempt"]["returned_result"] = result
    if expected["active_ticket_job"]["ticket_number"] == 2:
        expected["active_ticket_job"]["pending_semantic_attempt"]["returned_result"] = result
    # Persistence may update the observation timeline, never semantic facts.
    expected["timeline"] = after["timeline"]
    assert after == expected
    assert after["ticket_agent_invocations"]["2"]["reported_thread_id"] == "developer-2"
    assert (states.root / "worktrees" / run_id / "ticket-2" / "ticket-2.txt").read_text() == "done\n"
