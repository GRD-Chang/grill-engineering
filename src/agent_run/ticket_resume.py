"""Admit a targeted Resume into an existing Executor's Ticket work queue."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from agent_run.controller import Controller, apply_resume_to_state, _bind_resume_message
from agent_run.delivery_policy import parse_policy_snapshot
from agent_run.final_approval_operation import has_final_approval
from agent_run.resume_audit import append_explicit_resume_audit
from agent_run.resume_intent import ResumeIntentError, validate_resume_intent
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.ticket_queue import DEVELOPMENT_WORKER_PHASES
from agent_run.ticket_state import RUN_STOP_STATUSES, _demand_identity
from agent_run.ticket_work import enqueue


def _require_admission(state: dict[str, Any], number: int) -> None:
    if (state['status'] in RUN_STOP_STATUSES | {'run_approval_pending', 'parent_approval_pending'} or has_final_approval(state)
            or state.get('terminal_kind') == 'permanent_blocked'
            or state.get('github_refresh_pending')):
        raise ResumeIntentError('当前 Run 全部门禁不允许运行中定向恢复')
    if state.get('delivery_type') != 'ticket_run' or str(number) not in state['ticket_jobs']:
        raise ResumeIntentError('指定 Ticket 没有当前 Job')
    if str(number) in state['ticket_pause_requests']:
        raise ResumeIntentError('指定 Ticket 的 Stop 尚未收口')


def _replay(state: dict[str, Any], number: int, payload: Mapping[str, Any]) -> bool:
    job = state['ticket_jobs'][str(number)]
    receipt = job.get('last_ticket_resume')
    return (
        isinstance(receipt, dict) and receipt.get('payload') == payload
        and job.get('phase') not in {'blocked', 'publication_pending'}
    )


def admit_ticket_resume(
    controller: Controller, control: TaskControlStore, task: TaskKey,
    executor: Mapping[str, Any], payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Check remote facts unlocked, then patch only the target into latest state.

    The existing Task Control fence protects the commit. No new Lifecycle
    Action or Executor is created, and no Agent/Git/network work holds a lock.
    """
    run_id, number = str(payload['run_id']), int(payload['ticket_number'])
    states = StateStore(controller.states.root)
    states._set_write_guard(None, transaction=lambda: control.ticket_resume_transaction(
        task, action_id=str(executor['action_id']), generation=int(executor['generation']),
        run_id=run_id,
    ))
    initial = states.load_current_run(run_id)
    if initial is None:
        raise ResumeIntentError('Delivery Run 已不存在')
    _require_admission(initial, number)
    if _replay(initial, number, payload):
        # A live process may already be draining; duplicate receipts must use
        # the same admission fence as new work and recheck the latest Ticket.
        with control.ticket_resume_transaction(
            task, action_id=str(executor['action_id']),
            generation=int(executor['generation']), run_id=run_id,
        ), states.locked():
            latest = states.load_current_run(run_id)
            if latest is None:
                raise ResumeIntentError('Delivery Run 已不存在')
            _require_admission(latest, number)
            if _replay(latest, number, payload):
                return latest, True
            initial = latest
    policy = parse_policy_snapshot(payload['policy']).snapshot()
    frozen = initial['ticket_jobs'][str(number)]['policy_snapshot']
    if any(policy[key] != frozen[key] for key in policy if key not in {
        'ticket_review_rounds', 'invocation_deadlines', 'development_thread_policy',
    }):
        raise ResumeIntentError('运行中定向恢复不能修改 Run 并发或其他有效预算')
    intent = payload['resume_intent']
    validate_resume_intent(initial, intent)
    if any(item['ticket_number'] == number and item['lease_id'] is not None
           for item in initial['ticket_work_queue']):
        raise ResumeIntentError('指定 Ticket 的当前工作尚未释放名额')
    controller._load_bound_run(run_id, state=initial)
    controller._require_ticket_labels(initial, ticket_number=number)
    probe = deepcopy(initial)
    probe['active_ticket_job'] = probe['ticket_jobs'][str(number)]
    probe['active_agent_invocation'] = probe['ticket_agent_invocations'].get(str(number))
    refreshed = controller._refresh(probe, int(probe['parent']['number']))
    _require_admission(refreshed, number)
    validate_resume_intent(refreshed, intent)
    original_ticket = initial['ticket_graph']['tickets'][str(number)]
    refreshed_ticket = refreshed['ticket_graph']['tickets'][str(number)]
    # Maintainers may have repaired qualification labels while this Ticket
    # was paused. Accept those observed facts, never changed requirements.
    if ({key: value for key, value in original_ticket.items() if key not in {'labels', 'eligibility'}}
            != {key: value for key, value in refreshed_ticket.items() if key not in {'labels', 'eligibility'}}):
        raise ResumeIntentError('Ticket 需求已变化；请检查状态后重新提交')

    def commit() -> tuple[dict[str, Any], bool]:
        latest = states.load_current_run(run_id)
        if latest is None:
            raise ResumeIntentError('Delivery Run 已不存在')
        _require_admission(latest, number)
        if _replay(latest, number, payload):
            return latest, True
        validate_resume_intent(latest, intent)
        if _demand_identity(latest, str(number)) != _demand_identity(initial, str(number)):
            raise ResumeIntentError('Ticket 需求在恢复准入期间发生变化')
        if any(item['ticket_number'] == number and item['lease_id'] is not None
               for item in latest['ticket_work_queue']):
            raise ResumeIntentError('指定 Ticket 的当前工作尚未释放名额')
        latest['ticket_graph']['tickets'][str(number)] = deepcopy(refreshed_ticket)
        selected = latest.get('active_ticket_job')
        selected_number = selected.get('ticket_number') if isinstance(selected, dict) else None
        projection = {key: deepcopy(latest.get(key)) for key in (
            'status', 'terminal_kind', 'diagnostics', 'active_agent_invocation',
            'supervision_window', 'supervision_wait', 'credential_availability',
        )}
        latest['active_ticket_job'] = latest['ticket_jobs'][str(number)]
        latest['active_agent_invocation'] = latest['ticket_agent_invocations'].get(str(number))
        if payload['message'] is not None:
            _bind_resume_message(
                latest, payload['message'], ticket_number=number,
                human_blocker=intent['authorization'] == 'human_response',
            )
        append_explicit_resume_audit(
            latest, new_thread=payload['new_thread'],
            human_response_supplied=payload['message'] is not None, ticket_number=number,
        )
        apply_resume_to_state(
            latest, explicit_resume=True,
            resume_human_blocker=intent['authorization'] == 'human_response',
            new_thread=payload['new_thread'], human_response=payload['message'],
            resume_budget_checkpoint=payload['resume_budget_checkpoint'],
            budget_policy=parse_policy_snapshot(payload['policy']),
            ticket_number=number, source_status=latest['status'],
        )
        job = latest['ticket_jobs'][str(number)]
        invocation = latest.get('active_agent_invocation')
        if isinstance(invocation, dict):
            latest['ticket_agent_invocations'][str(number)] = deepcopy(invocation)
        job['last_ticket_resume'] = {
            'payload': deepcopy(payload), 'accepted_at': datetime.now(UTC).isoformat(),
            'executor_action_id': executor['action_id'],
            'executor_generation': executor['generation'],
            'job_generation': job['ticket_branch_generation'],
        }
        if job['phase'] in DEVELOPMENT_WORKER_PHASES:
            enqueue(latest, number, 'development')
        # Preserve a sibling's selected supervision and all its local gates.
        if (selected_number is not None and selected_number != number
                and latest['ticket_jobs'][str(selected_number)].get('phase') not in {'blocked', 'completed', 'publication_pending'}):
            latest['active_ticket_job'] = latest['ticket_jobs'][str(selected_number)]
            for key, value in projection.items():
                if value is None and key in {'supervision_window', 'supervision_wait', 'credential_availability'}:
                    latest.pop(key, None)
                else:
                    latest[key] = value
        states._save_run_and_notify_unlocked(run_id, latest)
        return latest, False

    return states._commit_run(commit)


def active_ticket_resume(
    controller: Controller, control: TaskControlStore, task: TaskKey,
    host: Any, current: dict[str, Any], number: int,
    make_payload: Callable[[dict[str, Any], Mapping[str, Any] | None], dict[str, Any]],
) -> tuple[dict[str, Any], bool] | None:
    """Return None to use the ordinary lifecycle when no Executor is active."""
    from agent_run.executor_host import ExecutorSpec, ExecutorStartUnknownError

    record = control.inspect_run(task, current)
    executor = record.get('executor') if isinstance(record, dict) else None
    if not isinstance(executor, dict) or executor.get('status') not in {'running', 'starting'}:
        return None
    action = record.get('action') if isinstance(record, dict) else None
    if not isinstance(action, dict) or action.get('status') != 'completed':
        raise ResumeIntentError('当前 Lifecycle Action 尚未完成；不能接受 Ticket 恢复')
    spec = ExecutorSpec(task=task, action_id=executor['action_id'],
                        generation=executor['generation'], run_id=current['run_id'],
                        cwd=task.workspace, state_root=controller.states.root,
                        runner_binding=executor.get('runner_binding'))
    observed = host.observe(spec, control)
    if observed.status != 'running':
        raise ExecutorStartUnknownError('无法确认原 Executor 正在执行；请先核对执行状态')
    _require_admission(current, number)
    receipt = current['ticket_jobs'][str(number)].get('last_ticket_resume')
    previous = (receipt.get('payload') if isinstance(receipt, dict)
                and current['ticket_jobs'][str(number)].get('phase') not in {'blocked', 'publication_pending'}
                else None)
    payload = make_payload(current, previous)
    return admit_ticket_resume(controller, control, task, executor, payload)
