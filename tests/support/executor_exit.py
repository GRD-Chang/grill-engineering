"""Minimal persisted Run and real Task Control for host exit observations."""
from __future__ import annotations

import os
from pathlib import Path

from agent_run.delivery_policy import DELIVERY_POLICY_PROTOCOL, DeliveryPolicy
from agent_run.executor_host import ExecutorSpec, _process_start_token
from agent_run.prompt_resources import builtin_resources
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.state import StateStore
from agent_run.task_control import TASK_CONTROL_PROTOCOL, TaskControlStore, TaskKey


def create_exit_run(
    tmp_path: Path, *, enabled: bool = True, reserved: bool = True,
) -> tuple[StateStore, TaskControlStore, ExecutorSpec]:
    """Create one legal idle-work Run; reserve its live Executor only on request."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    states = StateStore(tmp_path / "state")
    run_id = "run-exit"
    state = {
        "run_id": run_id, "language": "zh", "repository": "example/project",
        "parent": {"number": 1, "title": "退出告警", "revision": "parent-v1"},
        "base": {"branch": "main", "sha": "a" * 40},
        "prompt_resources": builtin_resources("zh"),
        "branch_authority_protocol": 3, "review_budget_protocol": 2,
        "delivery_policy_protocol": DELIVERY_POLICY_PROTOCOL,
        "multi_ticket_protocol": 4, "semantic_attempt_protocol": 1,
        "human_response_audit_protocol": 1,
        "lifecycle_action_protocol": TASK_CONTROL_PROTOCOL,
        "policy_snapshot": DeliveryPolicy().snapshot(),
        "ticket_graph": {"revision": "graph-v1", "ordered_ticket_numbers": [], "tickets": {}},
        "ticket_jobs": {}, "ticket_agent_invocations": {}, "ticket_pause_requests": {},
        "retired_ticket_generations": {}, "pending_ticket_retirements": {},
        "ticket_integration_queue": [], "ticket_integration_holder": None,
        "ticket_work_queue": [], "frontier": [], "timeline": [],
        "agent_invocation_history": [], "active_agent_invocation": None,
        "active_ticket_job": None,
        "accepted_ticket_graph_revision": "graph-v1",
        "accepted_parent_spec_revision": "parent-v1",
        "resume_audit": {"total": 0, "compacted": 0, "rolling_digest": None, "history": []},
        "human_response_audit": {}, "diagnostics": [], "status": "active",
        "notifications": {"enabled": enabled, "profile": "work", "open_id": "ou_test", "app_id": "cli_test"},
    }
    states.save_run(run_id, state)
    assert states.load_current_run(run_id) is not None
    control = TaskControlStore(states.root)
    task = TaskKey(workspace, "example/project", 1)
    claim = control.claim_action(task, kind="run", payload={"run_id": run_id})
    assert claim.action_id is not None and claim.action is not None
    generation = claim.action["executor_generation"]
    control.bind_run(task, claim.action_id, run_id, generation=generation, state_dir=states.root)
    spec = ExecutorSpec(task, claim.action_id, run_id, generation,
                        state_root=states.root, runner_binding="0123456789abcdef")
    if reserved:
        control.begin_executor(task, action_id=spec.action_id, run_id=run_id,
                               runner_binding=spec.runner_binding)
        control.mark_handshake(task, action_id=spec.action_id, generation=generation,
                               pid=os.getpid(), process_start_token=_process_start_token(os.getpid()))
        prepare_action_application_receipt(state, claim.action)
        states.save_run(run_id, state)
        control.record_application(task, action_id=spec.action_id, run_id=run_id,
                                   generation=generation, payload_digest=claim.action["payload_digest"])
        control.complete_action(task, action_id=spec.action_id, generation=generation,
                                result_status="active")
    return states, control, spec
