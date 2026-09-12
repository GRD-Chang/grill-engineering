"""The narrow lifecycle spine for public Executor-backed commands."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_run.agent_invocation import (
    record_session_interruption,
    session_interruption_is_persisted,
)
from agent_run.executor_host import (
    ExecutorHost,
    ExecutorLostError,
    ExecutorSpec,
    ExecutorStartUnknownError,
    HostObservation,
)
from agent_run.operator_gate import has_run_operator_gate
from agent_run.state import StateStore
from agent_run.resume_intent import action_resume_intent
from agent_run.task_control import (
    ActionClaim,
    ActionBusyError,
    ActionReconciliationError,
    TaskControlError,
    TaskControlStore,
    TaskKey,
    TASK_CONTROL_PROTOCOL,
    action_receipt_matches,
    payload_digest,
    unresolved_control_target,
)


@dataclass(frozen=True)
class LifecycleRequest:
    """A validated, closed lifecycle intent."""

    task: TaskKey
    kind: str
    payload: Mapping[str, Any]
    allow_terminal_successor: bool = False


@dataclass(frozen=True)
class ActionReceipt:
    """Internal result of one lifecycle action submission."""

    action_id: str | None
    kind: str
    run_id: str | None
    status: str
    attached: bool
    executor_status: str | None
    executor_generation: int | None
    handshake: bool
    payload_digest: str | None
    failure: str | None = None
    resume_intent: dict[str, Any] | None = None


class _ExecutorDispatch:
    """Keep one Executor callback bound to its reserved identity."""

    def __init__(
        self,
        callback: Callable[[int], Mapping[str, Any]],
        *,
        action_id: str,
        generation: int,
        run_id: str | None,
    ) -> None:
        self._callback = callback
        self._action_id = action_id
        self._generation = generation
        self._run_id = run_id
        self._bound = False

    def bind_executor(
        self, action_id: str, generation: int, run_id: str | None
    ) -> None:
        binding = (action_id, generation, run_id)
        current = (self._action_id, self._generation, self._run_id)
        if self._bound:
            if binding != current:
                raise ActionReconciliationError(
                    "Executor callback received a second, different binding"
                )
            return
        if action_id != self._action_id:
            raise ActionReconciliationError("Executor callback Action binding 不匹配")
        if self._run_id is not None and run_id != self._run_id:
            raise ActionReconciliationError(
                "Executor callback Delivery Run binding 不匹配"
            )
        self._generation = generation
        self._run_id = run_id
        self._bound = True

    def __call__(self) -> Mapping[str, Any]:
        return self._callback(self._generation)

    def bind_run(self, run_id: str) -> None:
        if self._run_id is not None and self._run_id != run_id:
            raise ActionReconciliationError(
                "Executor callback Delivery Run binding 不匹配"
            )
        self._run_id = run_id


def prepare_action_application_receipt(
    state: dict[str, Any], action: Mapping[str, Any]
) -> None:
    """Place the bounded receipt in a Run before its Executor is handed work."""

    state["action_application_receipt"] = {
        "protocol": TASK_CONTROL_PROTOCOL,
        "action_id": _string_field(action, "action_id"),
        "kind": _string_field(action, "kind"),
        "payload_digest": _string_field(action, "payload_digest"),
        "run_id": _string_field(state, "run_id"),
        "executor_generation": _positive_integer(action.get("executor_generation")),
        "applied_at": datetime.now(UTC).isoformat(),
    }
    if action.get("kind") in {"stop", "abandon"}:
        # The control Executor and its original target have separate ownership.
        # Explicit null proves that this control action had no target.
        target = action.get("target_executor")
        target_receipt = None
        if isinstance(target, Mapping):
            target_receipt = {
                key: target[key]
                for key in (
                    "status", "action_id", "run_id", "generation", "runner_binding",
                    "pid", "process_start_token",
                )
                if key in target
            }
            worker = target.get("worker")
            if isinstance(worker, Mapping):
                target_receipt["worker"] = {
                    key: worker[key]
                    for key in ("pid", "process_start_token")
                    if key in worker
                }
        state["action_application_receipt"]["target_executor"] = target_receipt


class RunLifecycle:
    """Admit one lifecycle action and give its business work to one Executor.

    The callbacks are deliberately supplied by the CLI composition root.  The
    lifecycle module owns admission, receipts and host hand-off; it does not
    know which Delivery Engine implements a Driver step.
    """

    def __init__(
        self,
        *,
        states: StateStore,
        control: TaskControlStore,
        host: ExecutorHost,
        task: TaskKey,
        preflight: Callable[[], dict[str, Any] | None],
        select_run: Callable[[Mapping[str, Any]], tuple[dict[str, Any], bool]],
        initialize_profile: Callable[[dict[str, Any], bool], None] | None,
        executor_spec: Callable[[str | None, str, int], ExecutorSpec],
        execute: Callable[[str], Mapping[str, Any]],
        execute_with_binding: Callable[
            [str, str, int], Mapping[str, Any]
        ] | None = None,
        prepare_executor_session: Callable[[], None] | None = None,
        poll_interval: float = 0.01,
        startup_timeout: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.states = states
        self.control = control
        self.host = host
        self.task = task
        self.preflight = preflight
        self.select_run = select_run
        self.initialize_profile = initialize_profile
        self.executor_spec = executor_spec
        self.execute = execute
        self.execute_with_binding = execute_with_binding
        self.prepare_executor_session = prepare_executor_session
        self.poll_interval = max(0.001, poll_interval)
        self.startup_timeout = max(self.poll_interval, startup_timeout)
        self.sleep = sleep
        self.clock = clock

    def _set_executor_state_fence(
        self,
        store: StateStore,
        *,
        action_id: str,
        generation: int,
        run_id: str | None,
    ) -> None:
        set_write_guard = getattr(store, "_set_write_guard", None)
        if not callable(set_write_guard):
            return

        def executor_write_transaction() -> Any:
            return self.control._executor_current_transaction(
                self.task,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            )

        set_write_guard(
            lambda: self.control.assert_executor_current(
                self.task,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            ),
            transaction=executor_write_transaction,
        )

    def submit(
        self, request: LifecycleRequest
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        if request.task != self.task:
            raise ActionReconciliationError(
                "Lifecycle Request 不属于当前 Delivery Task"
            )
        if request.kind not in {"run", "resume", "approve", "revise", "requeue"}:
            raise ValueError("unsupported Executor-backed Lifecycle Action")

        current = self.preflight()
        try:
            existing_control = self.control.load(self.task)
        except TaskControlError:
            # Reconciliation below may reconstruct a corrupt control record
            # from the exact Run receipt.  Without a receipt it will fail
            # closed rather than treating the corrupt record as absent.
            existing_control = None
        receipt = (
            current.get("action_application_receipt")
            if isinstance(current, Mapping)
            else None
        )
        existing_executor = (
            existing_control.get("executor")
            if isinstance(existing_control, Mapping)
            else None
        )
        if (
            request.kind in {"approve", "revise", "requeue"}
            and isinstance(receipt, Mapping)
            and receipt.get("kind") == request.kind
            and receipt.get("payload_digest") == payload_digest(request.payload)
            and (
                existing_control is None
                or (
                    isinstance(existing_executor, Mapping)
                    and existing_executor.get("binding_token") == "reconciliation-required"
                )
            )
        ):
            # Reconstructing the same explicit intent is receipt recovery, even
            # if the Run still advertises the original command's ready state.
            request = replace(request, allow_terminal_successor=False)
        reconciliation_payload: Mapping[str, Any] | None = request.payload
        if (
            existing_control is None
            and isinstance(receipt, Mapping)
            and _has_run_receipt(current)
        ):
            requested_digest = payload_digest(request.payload)
            request_differs_from_receipt = (
                receipt.get("kind") != request.kind
                or receipt.get("payload_digest") != requested_digest
            )
            terminal_successor = (
                request.kind in {"resume", "approve", "revise", "requeue"}
                and request.allow_terminal_successor
            )
            if (
                request_differs_from_receipt
                and not terminal_successor
            ):
                raise ActionBusyError(
                    "当前 Delivery Task 的原 Action 尚未对账；不会提交不同 mutation",
                    action={
                        "action_id": receipt.get("action_id"),
                        "kind": receipt.get("kind"),
                        "payload_digest": receipt.get("payload_digest"),
                        "run_id": receipt.get("run_id"),
                        "status": "applying",
                    },
                )
            if terminal_successor:
                # A dedicated successor first closes the exact predecessor
                # named by the Run receipt.  The receipt intentionally omits
                # its semantic payload, so do not substitute the new mutation,
                # even when an earlier Resume used the same semantic payload.
                reconciliation_payload = None
        record = self._reconcile_from_run(current, reconciliation_payload)
        action = record.get("action") if isinstance(record, Mapping) else None
        executor = record.get("executor") if isinstance(record, Mapping) else None
        reconciling_receipt_predecessor = bool(
            request.kind in {"resume", "approve", "revise", "requeue"}
            and request.allow_terminal_successor
            and isinstance(current, Mapping)
            and isinstance(receipt, Mapping)
            and isinstance(action, Mapping)
            and isinstance(executor, Mapping)
            and executor.get("binding_token") == "reconciliation-required"
            and action_receipt_matches(current, action)
        )
        reconciled_receipt_only_exit = False
        if isinstance(executor, Mapping) and executor.get(
            "reconciliation_required"
        ) is True:
            record = self._reconcile_receipt_executor_exit(current, record)
            action = record.get("action")
            executor = record.get("executor")
            reconciled_receipt_only_exit = True
        if (
            reconciling_receipt_predecessor
            and isinstance(record, Mapping)
            and isinstance(current, Mapping)
            and isinstance(receipt, Mapping)
            and isinstance(action, Mapping)
            and isinstance(executor, Mapping)
            and executor.get("status") == "exited"
            and executor.get("reconciliation_required") is not True
        ):
            record = self._close_receipt_predecessor(current, record)
            action = record.get("action")
            executor = record.get("executor")
            reconciled_receipt_only_exit = True
        if (
            existing_control is None
            and isinstance(receipt, Mapping)
            and isinstance(record, Mapping)
            and isinstance(current, Mapping)
            and _restartable_after_executor_exit(current)
            and not reconciled_receipt_only_exit
        ):
            raise _actionable_executor_unknown(
                "Task Control 不可用；仅凭 Delivery Run Receipt 无法证明原 "
                "Executor 已退出。请恢复对应 Task Control 后重试。"
            )
        invocation = (
            current.get("active_agent_invocation") if current is not None else None
        )
        observing_active_invocation = (
            request.kind == "run"
            and isinstance(invocation, Mapping)
            and invocation.get("status") in {"running", "resuming"}
        )
        if (
            current is not None
            and isinstance(record, Mapping)
            and isinstance(action, Mapping)
            and isinstance(executor, Mapping)
            and executor.get("status") in {"absent", "exited"}
            and (
                action.get("kind") not in {"approve", "revise", "requeue"}
                or observing_active_invocation
            )
            and action_receipt_matches(current, action)
            and not reconciling_receipt_predecessor
            and (
                action.get("status") in {"accepted", "applying"}
                or (
                    action.get("status") in {"completed", "failed"}
                    and (
                        executor.get("status") == "absent"
                        or isinstance(executor.get("failure"), str)
                        or observing_active_invocation
                    )
                )
            )
            and not _safe_executor_recovery(current)
            and not (
                session_interruption_is_persisted(current)
                and executor.get("failure") == "session_interrupted"
            )
            and (
                current.get("status")
                not in {
                    "execution_failed",
                    "completed",
                    "abandoned",
                    "ready_for_human",
                }
                or session_interruption_is_persisted(current)
            )
        ):
            return self._record_session_interruption(
                current,
                record=record,
                action_id=_string_field(action, "action_id"),
                generation=_positive_integer(action.get("executor_generation")),
                resumed=True,
                attached=True,
            )
        replace_receipt_action_id = _receipt_owner_action_id(record, current)
        current_run_id = current.get("run_id") if isinstance(current, Mapping) else None
        if (
            current is not None
            and isinstance(record, Mapping)
            and isinstance(action, Mapping)
            and (
                action.get("status") == "completed"
                or (
                    action.get("status") == "failed"
                    and request.kind != "resume"
                    and isinstance(executor, Mapping)
                    and executor.get("failure") == "session_interrupted"
                )
            )
            and action.get("kind") == request.kind
            and action.get("payload_digest") == payload_digest(request.payload)
            and action.get("run_id") == current_run_id
            and not request.allow_terminal_successor
            and not (
                request.kind == "run"
                and isinstance(executor, Mapping)
                and executor.get("status") in {"starting", "running"}
            )
            and (
                request.kind in {"approve", "revise", "requeue"}
                or not _restartable_after_executor_exit(current)
            )
        ):
            return (
                current,
                True,
                self.receipt_from_record(
                    record, action_id=action.get("action_id"), attached=True
                ),
            )
        claim = self._claim(request)
        if claim.executor_active:
            claim_record = claim.record
            deadline = self.clock() + self.startup_timeout
            while True:
                run_id = _record_run_id(claim_record)
                state = self._load_action_run(run_id, current, record=claim_record)
                if not isinstance(run_id, str):
                    candidate = state.get("run_id")
                    if isinstance(candidate, str) and candidate:
                        run_id = candidate
                    elif self.clock() >= deadline:
                        raise ActionReconciliationError(
                            "活动 Executor 没有关联可确认的 Delivery Run"
                        )
                    else:
                        self.sleep(self.poll_interval)
                        latest = self.control.snapshot(self.task)
                        if latest is not None:
                            claim_record = latest
                        continue
                break
            executor = claim_record.get("executor")
            if not isinstance(executor, Mapping):
                raise ActionReconciliationError("活动 Executor 缺少 ownership record")
            action_id = executor.get("action_id")
            if not isinstance(action_id, str) or not action_id:
                raise ActionReconciliationError("活动 Executor 缺少 Action ID")
            generation = _positive_integer(executor.get("generation"))
            while True:
                observation = self.host.inspect(
                    self.executor_spec(run_id, action_id, generation), self.control
                )
                if observation.status == "running":
                    break
                if observation.status in {"absent", "exited"}:
                    state = self._load_action_run(run_id, state, record=claim_record)
                    if _safe_executor_recovery(state):
                        self.control.mark_executor_absent(
                            self.task,
                            action_id=action_id,
                            generation=generation,
                        )
                        return self.submit(request)
                    return self._record_session_interruption(
                        state,
                        record=claim_record,
                        action_id=action_id,
                        generation=generation,
                        resumed=True,
                        attached=True,
                    )
                if observation.status == "conflict":
                    raise _actionable_executor_unknown(
                        observation.reason or "Executor binding conflict"
                    )
                if self.clock() >= deadline:
                    raise _actionable_executor_unknown(
                        observation.reason
                        or "Executor ownership 无法确认；不会启动第二个 Executor"
                    )
                self.sleep(self.poll_interval)
            return (
                state,
                True,
                self.receipt_from_record(
                    claim_record, action_id=action_id, attached=True
                ),
            )
        action = claim.action
        if action is None or claim.action_id is None:  # pragma: no cover - defensive
            raise ActionReconciliationError(
                "Lifecycle Action admission returned no Action"
            )
        if claim.attached:
            return self._continue_action(
                action, current, replace_receipt_action_id=replace_receipt_action_id
            )
        return self._start_action(
            action,
            current,
            replace_receipt_action_id=replace_receipt_action_id,
        )

    def submit_control(
        self,
        request: LifecycleRequest,
    ) -> tuple[dict[str, Any], bool, ActionReceipt | None]:
        """Fence an old Executor and run Stop/Abandon under a new Executor.

        Admission is deliberately separate from ordinary lifecycle submission:
        Stop is read-only only for an already stopped or terminal Run. An idle
        unfinished Run still needs the durable operator pause boundary.
        """

        if request.task != self.task or request.kind not in {"stop", "abandon"}:
            raise ValueError("unsupported control Lifecycle Action")
        current = self.preflight()
        if current is None:
            raise ActionReconciliationError(
                f"{request.kind} 找不到 Delivery Run"
            )
        run_id = _string_field(current, "run_id")
        record = self._reconcile_from_run(current, None)
        action = record.get("action") if isinstance(record, Mapping) else None
        executor = record.get("executor") if isinstance(record, Mapping) else None
        if (
            isinstance(action, Mapping)
            and isinstance(executor, Mapping)
            and executor.get("binding_token") == "reconciliation-required"
        ):
            assert isinstance(record, dict)
            record = self._reconcile_receipt_executor_exit(current, record)
            action = record.get("action")
            assert isinstance(action, Mapping)
            record = self._close_receipt_predecessor(current, record)
            action = record.get("action")
        unresolved_target = unresolved_control_target(action)
        active_action = (
            action
            if isinstance(action, Mapping)
            and action.get("status") in {"accepted", "applying"}
            else None
        )
        requested_digest = payload_digest(request.payload)
        attached = bool(
            active_action is not None
            and active_action.get("kind") == request.kind
            and active_action.get("payload_digest") == requested_digest
            and active_action.get("run_id") == run_id
        )
        if active_action is not None and not attached:
            raise ActionBusyError(
                "当前 Delivery Task 已有未完成 Lifecycle Action；不会等待或排队",
                action=active_action,
            )
        if (
            (
                current.get("status") in {"completed", "abandoned"}
                or (
                    request.kind == "stop"
                    and current.get("status") == "operator_stopped"
                )
            )
            and unresolved_target is None
        ):
            if not attached:
                return current, True, None
            assert active_action is not None
            active_executor = (
                record.get("executor") if isinstance(record, Mapping) else None
            )
            completed: Mapping[str, Any] | None = None
            if not isinstance(active_executor, Mapping):
                completed = self.control.complete_control_action(
                    self.task,
                    action_id=_string_field(active_action, "action_id"),
                    result_status=_string_field(current, "status"),
                )
            elif active_executor.get("status") in {"exited", "absent"}:
                application_receipt = current.get("action_application_receipt")
                if not isinstance(application_receipt, Mapping):
                    raise ActionReconciliationError(
                        "终态 Control Action 缺少 Application Receipt"
                    )
                completed = self.control.complete_action_from_application_receipt(
                    self.task,
                    action_id=_string_field(active_action, "action_id"),
                    generation=_positive_integer(
                        active_executor.get("generation")
                    ),
                    application_receipt=application_receipt,
                    result_status=_string_field(current, "status"),
                )
            if completed is not None:
                return (
                    current,
                    True,
                    self.receipt_from_record(
                        completed,
                        action_id=_string_field(active_action, "action_id"),
                        attached=True,
                    ),
                )

        executor = record.get("executor") if isinstance(record, Mapping) else None
        if attached:
            assert active_action is not None
            claimed_action = active_action
        else:
            if (
                request.kind == "stop"
                and unresolved_target is None
            ):
                if record is None:
                    raise ExecutorStartUnknownError(
                        "Task Control ownership 缺失；不会猜测或终止进程"
                    )
                if isinstance(executor, Mapping):
                    self._observe_stop_target(executor)
                latest = self._state_store_for_record(record).load_current_run(run_id)
                if latest is None:
                    raise ActionReconciliationError("Stop 无法重新读取准确 Delivery Run")
                if latest.get("status") in {"completed", "abandoned", "operator_stopped"}:
                    return self._reload_no_active_run(run_id, record), True, None
                current = latest
            claim = self.control.claim_control_action(
                self.task,
                kind=request.kind,
                payload=request.payload,
                run_id=run_id,
                state_dir=self.states.root,
                before_create=self.prepare_executor_session,
            )
            if claim is None:
                raise ActionReconciliationError("Stop 未取得控制操作")
            claimed_action = claim.action
            attached = claim.attached

        replace_receipt_action_id = _receipt_owner_action_id(record, current)
        return self._continue_action(
            claimed_action,
            current,
            replace_receipt_action_id=replace_receipt_action_id,
            attached=attached,
        )

    def _observe_stop_target(self, executor: Mapping[str, Any]) -> None:
        if executor.get("status") in {"exited", "absent"}:
            return
        if executor.get("status") not in {"starting", "running"}:
            raise ExecutorStartUnknownError(
                "Executor ownership 无法确认；不会提交控制栅栏"
            )
        old_run_id = executor.get("run_id")
        observation = self.host.observe(
            replace(
                self.executor_spec(
                    old_run_id if isinstance(old_run_id, str) else None,
                    _string_field(executor, "action_id"),
                    _positive_integer(executor.get("generation")),
                ),
                runner_binding=(
                    executor.get("runner_binding")
                    if isinstance(executor.get("runner_binding"), str)
                    else None
                ),
            ),
            self.control,
        )
        if observation.status not in {"running", "absent", "exited"}:
            raise ExecutorStartUnknownError(
                observation.reason or "Executor ownership 无法确认；不会提交控制栅栏"
            )

    def _reload_no_active_run(
        self,
        run_id: str,
        record: Mapping[str, Any],
        *,
        host_absent_executor: tuple[str, int] | None = None,
    ) -> dict[str, Any]:
        """Reload the exact Run after proving Stop is a read-only no-op."""

        current = self._state_store_for_record(record).load_current_run(run_id)
        if current is None or current.get("run_id") != run_id:
            raise ActionReconciliationError(
                "Stop 无法重新读取准确 Delivery Run；不会返回过期状态"
            )
        parent = current.get("parent")
        if (
            current.get("repository") != self.task.repository
            or not isinstance(parent, Mapping)
            or parent.get("number") != self.task.parent_number
        ):
            raise ActionReconciliationError(
                "Stop 重新读取的 Delivery Run 与 Delivery Task 不匹配"
            )
        if not self.control.proves_read_only_stop(
            self.task,
            current,
            host_absent_executor=host_absent_executor,
        ):
            raise ActionReconciliationError(
                "Stop 的 no-active 证明在读取结果前已变化；不会返回过期状态"
            )
        return current

    def execute_claimed(
        self, *, action_id: str, generation: int
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        """Execute one Action already reserved by an external Host."""

        record = self.control.snapshot(self.task, action_id)
        if record is None:
            raise ActionReconciliationError("Executor 找不到已接受的 Action")
        action = record.get("action")
        executor = record.get("executor")
        if not isinstance(action, Mapping) or action.get("action_id") != action_id:
            raise ActionReconciliationError("Executor Action binding 不匹配")
        if (
            not isinstance(executor, Mapping)
            or executor.get("action_id") != action_id
            or executor.get("generation") != generation
            or executor.get("status") not in {"starting", "running"}
        ):
            raise ActionReconciliationError("Executor generation binding 不匹配")
        run_id = _record_run_id(record)
        current = self._load_action_run(run_id, {}, record=record)
        return self._continue_action(
            action,
            current,
            replace_receipt_action_id=_receipt_owner_action_id(record, current),
            executor_generation=generation,
        )

    def _claim(self, request: LifecycleRequest) -> ActionClaim:
        return self.control.claim_action(
            self.task,
            kind=request.kind,
            payload=request.payload,
            before_create=self.prepare_executor_session,
        )

    def _close_receipt_predecessor(
        self, current: Mapping[str, Any], record: Mapping[str, Any]
    ) -> dict[str, Any]:
        action = record.get("action")
        receipt = current.get("action_application_receipt")
        if not isinstance(action, Mapping) or not isinstance(receipt, Mapping):
            raise ActionReconciliationError("原 Action 缺少准确 Run receipt")
        if action.get("status") in {"completed", "failed"}:
            return dict(record)
        kind = action.get("kind")
        if kind in {"stop", "abandon"} and current.get("status") not in (
            {"operator_stopped", "completed", "abandoned"}
            if kind == "stop" else {"completed", "abandoned"}
        ):
            # Control receipts precede their business effect. An exited control
            # Executor with no durable outcome failed; it did not complete Stop.
            if kind == "stop" and not isinstance(action.get("target_executor"), Mapping):
                raise _actionable_executor_unknown(
                    "原 Stop 缺少 target ownership 且停止结果未形成；不会返回无需停止。"
                )
            return self.control.finish_executor(
                self.task,
                action_id=_string_field(action, "action_id"),
                generation=_positive_integer(action.get("executor_generation")),
                failure="Control Executor 已退出，但控制动作结果尚未持久形成",
            )
        return self.control.complete_action_from_application_receipt(
            self.task,
            action_id=_string_field(action, "action_id"),
            generation=_positive_integer(action.get("executor_generation")),
            application_receipt=receipt,
            result_status=_string_field(current, "status"),
        )

    def _reconcile_receipt_executor_exit(
        self,
        current: dict[str, Any] | None,
        record: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Prove all receipt-bound ownership gone before releasing admission."""

        action = record.get("action") if isinstance(record, Mapping) else None
        executor = record.get("executor") if isinstance(record, Mapping) else None
        if not (
            isinstance(current, dict)
            and isinstance(record, dict)
            and isinstance(action, Mapping)
            and isinstance(executor, Mapping)
            and action_receipt_matches(current, action)
        ):
            raise _actionable_executor_unknown(
                "Task Control 不可用；Delivery Run Receipt 无法绑定原 Action。"
            )
        if executor.get("reconciliation_required") is not True:
            if executor.get("status") != "exited":
                raise _actionable_executor_unknown("原 Executor 尚未证明退出。")
            return record
        action_id = _string_field(action, "action_id")
        generation = _positive_integer(action.get("executor_generation"))
        run_id = _string_field(current, "run_id")
        observation = self.host.observe(
            self.executor_spec(run_id, action_id, generation), self.control
        )
        if (
            observation.status != "exited"
            or observation.generation != generation
            or observation.runner_binding is None
        ):
            raise _actionable_executor_unknown(
                observation.reason
                or "Task Control 不可用；Host 未证明原 Executor 已退出。"
            )
        target = action.get("target_executor")
        if isinstance(target, Mapping):
            # The durable Stop/Abandon fence still authorizes only this target.
            # A failed termination must not become a successor's exit proof.
            self.host.terminate_control_target(target)
        return self.control.record_reconciled_executor_exit(
            self.task,
            action_id=action_id,
            run_id=run_id,
            generation=generation,
            observed_status=observation.status,
            observed_generation=observation.generation,
            observed_runner_binding=observation.runner_binding,
        )

    def _reconcile_from_run(
        self,
        current: dict[str, Any] | None,
        payload: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        return self.control.reconcile_from_run(
            self.task,
            current,
            payload=payload,
            state_dir=getattr(self.states, "root", None),
        )

    def _start_action(
        self,
        action: Mapping[str, Any],
        current: dict[str, Any] | None,
        *,
        replace_receipt_action_id: str | None = None,
        attached: bool = False,
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        action_id = _string_field(action, "action_id")
        generation = _positive_integer(action.get("executor_generation"))
        execution_context: dict[str, Any] = {}
        action_run_id = action.get("run_id")
        if not isinstance(action_run_id, str) or not action_run_id:
            action_run_id = None

        def apply(generation: int) -> Mapping[str, Any]:
            self._set_executor_state_fence(
                self.states,
                action_id=action_id,
                generation=generation,
                run_id=None,
            )
            self.control.assert_executor_current(
                self.task,
                action_id=action_id,
                generation=generation,
            )
            state, resumed = self.select_run(action)
            run_id = _string_field(state, "run_id")
            # External Executors start with an unbound Action, so its Run (and
            # predecessor receipt) may only become known during selection.
            receipt_predecessor = replace_receipt_action_id
            if receipt_predecessor is None:
                receipt_predecessor = _receipt_owner_action_id(
                    self.control.snapshot(self.task, action_id), state
                )
            self.control.bind_run(
                self.task,
                action_id,
                run_id,
                generation=generation,
                state_dir=getattr(self.states, "root", None),
            )
            execute_action.bind_run(run_id)
            self._set_executor_state_fence(
                self.states,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            )
            return self._apply_and_execute_bound_action(
                action=action,
                action_id=action_id,
                state=state,
                run_id=run_id,
                generation=generation,
                resumed=resumed,
                execution_context=execution_context,
                replace_receipt_action_id=receipt_predecessor,
            )

        execute_action = _ExecutorDispatch(
            apply,
            action_id=action_id,
            generation=generation,
            run_id=action_run_id,
        )

        return self._run_executor(
            action_id=action_id,
            run_id=action_run_id,
            attached=attached,
            resumed=attached or current is not None,
            state=current or {},
            generation=generation,
            execute=execute_action,
            execution_context=execution_context,
        )

    def _continue_action(
        self,
        action: Mapping[str, Any],
        current: dict[str, Any] | None,
        *,
        replace_receipt_action_id: str | None = None,
        executor_generation: int | None = None,
        attached: bool = True,
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        action_id = _string_field(action, "action_id")
        latest = self.control.snapshot(self.task, action_id)
        if latest is None:
            raise ActionReconciliationError("Action 在继续前丢失")
        latest_action = latest.get("action")
        if not isinstance(latest_action, Mapping):
            raise ActionReconciliationError("Action 在继续前无效")
        if (
            latest_action.get("run_id") is None
            and current is not None
            and _unbound_action_matches_run_receipt(current, latest_action)
        ):
            receipt = current["action_application_receipt"]
            reconciled_run_id = _string_field(receipt, "run_id")
            generation = _positive_integer(
                receipt.get("executor_generation")
            )
            latest = self.control.bind_run(
                self.task,
                action_id,
                reconciled_run_id,
                generation=generation,
                state_dir=getattr(self.states, "root", None),
            )
            rebound_action = latest.get("action")
            if not isinstance(rebound_action, Mapping):  # pragma: no cover - guarded
                raise ActionReconciliationError("Action Run binding 对账后丢失")
            latest_action = rebound_action
        if latest_action.get("status") in {"completed", "failed"}:
            final = self._load_action_run(
                _record_run_id(latest), current, record=latest
            )
            return (
                final,
                True,
                self.receipt_from_record(latest, action_id=action_id, attached=True),
            )
        action = latest_action
        run_id = action.get("run_id")
        if isinstance(run_id, str) and run_id:
            state_store = self._state_store_for_record(latest)
            state = state_store.load_current_run(run_id)
            if state is None:
                raise ActionReconciliationError("Action 指向的 Delivery Run 不存在")
            generation = (
                executor_generation
                if executor_generation is not None
                else _positive_integer(action.get("executor_generation"))
            )
            execution_context: dict[str, Any] = {}

            def apply(bound_generation: int) -> Mapping[str, Any]:
                self._set_executor_state_fence(
                    state_store,
                    action_id=action_id,
                    generation=bound_generation,
                    run_id=run_id,
                )
                self.control.assert_executor_current(
                    self.task,
                    action_id=action_id,
                    generation=bound_generation,
                    run_id=run_id,
                )
                return self._apply_and_execute_bound_action(
                    action=action,
                    action_id=action_id,
                    state=state,
                    run_id=run_id,
                    generation=bound_generation,
                    resumed=True,
                    execution_context=execution_context,
                    replace_receipt_action_id=replace_receipt_action_id,
                    state_store=state_store,
                )

            execute_action = _ExecutorDispatch(
                apply,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            )

            return self._run_executor(
                action_id=action_id,
                run_id=run_id,
                attached=attached,
                resumed=True,
                state=state,
                generation=generation,
                execute=execute_action,
                execution_context=execution_context,
            )
        return self._start_action(
            action,
            current,
            attached=attached,
            replace_receipt_action_id=replace_receipt_action_id,
        )

    def _apply_and_execute_bound_action(
        self,
        state: dict[str, Any],
        *,
        action: Mapping[str, Any],
        action_id: str,
        run_id: str,
        generation: int,
        resumed: bool,
        execution_context: dict[str, Any],
        replace_receipt_action_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> Mapping[str, Any]:
        self.control.assert_executor_current(
            self.task,
            action_id=action_id,
            generation=generation,
            run_id=run_id,
        )
        self._set_executor_state_fence(
            state_store or self.states,
            action_id=action_id,
            generation=generation,
            run_id=run_id,
        )
        if self.initialize_profile is not None:
            self.initialize_profile(state, resumed)
            self.control.assert_executor_current(
                self.task,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            )
        prepared = self._persist_application(
            action=action,
            run_id=run_id,
            generation=generation,
            replace_receipt_action_id=replace_receipt_action_id,
            state_store=state_store,
        )
        execution_context.update(
            {"run_id": run_id, "resumed": resumed, "state": prepared}
        )
        kind = _string_field(action, "kind")
        if kind in {"stop", "abandon"}:
            if self.execute_with_binding is None:
                raise ActionReconciliationError(
                    "Control Action 缺少绑定业务执行入口"
                )
            return self.execute_with_binding(run_id, action_id, generation)
        result_status = prepared.get("status")
        self.control.complete_action(
            self.task,
            action_id=action_id,
            generation=generation,
            result_status=(result_status if isinstance(result_status, str) else None),
        )
        self.control.assert_executor_current(
            self.task,
            action_id=action_id,
            generation=generation,
            run_id=run_id,
        )
        if self.execute_with_binding is not None:
            return self.execute_with_binding(run_id, action_id, generation)
        return self.execute(run_id)

    def _run_executor(
        self,
        *,
        action_id: str,
        run_id: str | None,
        attached: bool,
        resumed: bool,
        state: dict[str, Any],
        generation: int,
        execute: Callable[[], Mapping[str, Any]] | None = None,
        execution_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        record = self.control.snapshot(self.task, action_id)
        if record is None:
            raise ActionReconciliationError("当前 Action 在启动前丢失")
        action = record.get("action")
        expected_action = dict(action) if isinstance(action, Mapping) else None
        if isinstance(action, Mapping) and action.get("status") in {
            "completed",
            "failed",
        }:
            action_run_id = _record_run_id(record)
            state_store = self._state_store_for_record(record)
            final = (
                state_store.load_current_run(action_run_id)
                if action_run_id is not None
                else None
            )
            if final is None:
                raise ActionReconciliationError("已收口 Action 没有对应的 Delivery Run")
            return (
                final,
                resumed,
                self.receipt_from_record(
                    record, action_id=action_id, attached=attached
                ),
            )
        executor = record.get("executor")
        if (
            expected_action is not None
            and _action_matches_run_receipt(state, expected_action)
            and (
                not isinstance(executor, Mapping)
                or executor.get("action_id") != action_id
            )
        ):
            raise _actionable_executor_unknown(
                "Delivery Run Receipt 已存在但 Executor ownership 缺失或不匹配；"
                "不会启动第二个 Executor"
            )
        if (
            isinstance(executor, Mapping)
            and executor.get("reconciliation_required") is True
        ):
            raise _actionable_executor_unknown(
                "Task Control Record 曾丢失；Executor ownership 无法确认，"
                "不会启动第二个 Executor"
            )
        recover = _safe_executor_recovery(state)
        applied_at_non_replayable_boundary = (
            expected_action is not None
            and _action_matches_run_receipt(state, expected_action)
            and isinstance(state.get("run_id"), str)
            and expected_action.get("kind") in {"approve", "revise", "requeue"}
            and not recover
        )
        observation: HostObservation | None = None
        if isinstance(executor, dict) and executor.get("status") in {
            "starting",
            "running",
        }:
            spec = self.executor_spec(run_id, action_id, generation)
            deadline = self.clock() + self.startup_timeout
            while True:
                observation = self.host.inspect(spec, self.control)
                if observation.status not in {"starting", "unknown"}:
                    break
                if self.clock() >= deadline:
                    raise _actionable_executor_unknown(
                        observation.reason
                        or "Executor ownership 无法确认；不会启动第二个 Executor"
                    )
                # Keep checking this exact generation while the process
                # binding and handshake transaction is still being committed.
                self.sleep(self.poll_interval)
            if observation.status in {"absent", "exited"}:
                state = self._load_action_run(run_id, state, record=record)
                recover = _safe_executor_recovery(state)
                if applied_at_non_replayable_boundary:
                    return self._complete_applied_action_after_executor_exit(
                        state,
                        action_id=action_id,
                        generation=generation,
                        resumed=resumed,
                        attached=attached,
                    )
                if not recover:
                    return self._record_session_interruption(
                        state,
                        record=record,
                        action_id=action_id,
                        generation=generation,
                        resumed=resumed,
                        attached=attached,
                    )
                observation = None
            elif observation.status == "conflict":
                raise _actionable_executor_unknown(
                    observation.reason or "Executor binding conflict"
                )
        elif isinstance(executor, dict) and executor.get("status") in {
            "exited",
            "absent",
        }:
            if executor.get("action_id") == action_id:
                if applied_at_non_replayable_boundary:
                    return self._complete_applied_action_after_executor_exit(
                        state,
                        action_id=action_id,
                        generation=generation,
                        resumed=resumed,
                        attached=attached,
                    )
                if not recover:
                    return self._record_session_interruption(
                        state,
                        record=record,
                        action_id=action_id,
                        generation=generation,
                        resumed=resumed,
                        attached=attached,
                    )
        elif executor is not None:
            raise ActionReconciliationError("Executor ownership record 无法对账")

        spec = self.executor_spec(run_id, action_id, generation)
        if observation is None or observation.status != "running":
            if execute is None:
                if not isinstance(run_id, str):
                    raise ActionReconciliationError(
                        "Executor 启动前没有可执行的 Delivery Run"
                    )
                executor_run_id = run_id
                execute = lambda: self.execute(executor_run_id)
            observation = self.host.ensure(
                spec,
                self.control,
                execute=execute,
                recover=recover,
            )
            if (
                observation.generation is not None
                and observation.generation != spec.generation
            ):
                spec = replace(
                    spec,
                    generation=observation.generation,
                )
                generation = observation.generation
        if execution_context is not None:
            observed_run_id = execution_context.get("run_id")
            if isinstance(observed_run_id, str) and observed_run_id:
                spec = replace(spec, run_id=observed_run_id)
                run_id = observed_run_id
        final_state = self._wait_for_action(
            spec,
            state,
            action_id,
            generation,
            expected_action=expected_action,
        )
        try:
            receipt = self._receipt_for_action(action_id, attached=attached)
        except ActionReconciliationError:
            if expected_action is None or not _action_matches_run_receipt(
                final_state, expected_action
            ):
                raise
            receipt = self._receipt_from_expected_action(
                expected_action, final_state, attached=attached
            )
        if execution_context is not None and isinstance(
            execution_context.get("resumed"), bool
        ):
            resumed = execution_context["resumed"]
        return final_state, resumed, receipt

    def _record_session_interruption(
        self,
        state: dict[str, Any],
        *,
        record: Mapping[str, Any],
        action_id: str,
        generation: int,
        resumed: bool,
        attached: bool,
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        action = record.get("action")
        application_receipt = state.get("action_application_receipt")
        if not isinstance(action, Mapping) or not isinstance(
            application_receipt, Mapping
        ):
            raise ExecutorLostError(
                "Executor 已退出，但 Delivery Run Receipt 无法证明原 Action；"
                "不会改写状态或启动第二个 Executor"
            )
        if not (
            action_receipt_matches(state, action)
            or _unbound_action_matches_run_receipt(state, action)
        ):
            raise ExecutorLostError(
                "Executor 已退出，但 Delivery Run Receipt 无法证明原 Action；"
                "不会改写状态或启动第二个 Executor"
            )
        guarded_store = self._state_store_for_record(record)
        state_store = StateStore(guarded_store.root)
        run_id = _string_field(state, "run_id")
        current = state_store.load_current_run(run_id)
        current_receipt = (
            current.get("action_application_receipt")
            if isinstance(current, Mapping)
            else None
        )
        if current is None or current_receipt != application_receipt:
            raise ExecutorLostError(
                "Executor 已退出，但当前 Delivery Run 无法证明原 Action；"
                "不会改写状态或启动第二个 Executor"
            )
        latest = self.control.load(self.task)
        latest_action = latest.get("action") if isinstance(latest, Mapping) else None
        latest_executor = latest.get("executor") if isinstance(latest, Mapping) else None
        if not (
            isinstance(latest_action, Mapping)
            and isinstance(latest_executor, Mapping)
            and latest_action.get("action_id") == action_id
            and latest_action.get("executor_generation") == generation
            and latest_executor.get("action_id") == action_id
            and latest_executor.get("generation") == generation
            and (
                action_receipt_matches(current, latest_action)
                or _unbound_action_matches_run_receipt(current, latest_action)
            )
        ):
            raise ExecutorLostError(
                "Executor 已退出，但当前 Action/generation 已改变；不会改写状态"
            )
        assert latest is not None
        # Host inspection can race with the original Executor's final commit.
        # Its latest durable boundary wins over the caller's progress snapshot.
        if (
            current.get("status") in {"completed", "abandoned"}
            or (
                current.get("status") == "ticket_completed"
                and _safe_executor_recovery(current)
            )
            or has_run_operator_gate(current)
        ) and not session_interruption_is_persisted(current):
            if latest_action.get("status") != "completed":
                return self._complete_applied_action_after_executor_exit(
                    current,
                    action_id=action_id,
                    generation=generation,
                    resumed=resumed,
                    attached=attached,
                )
            return (
                current,
                resumed,
                self.receipt_from_record(latest, action_id=action_id, attached=attached),
            )
        if self.initialize_profile is not None:
            # The earliest durable receipt can precede the frozen profile
            # write.  Materialize that deterministic Action-bound profile so
            # the advertised explicit Resume remains usable after closeout.
            self.initialize_profile(current, True)
        closed = self.control.fail_session_from_application_receipt(
            self.task,
            action_id=action_id,
            generation=generation,
            application_receipt=application_receipt,
            persist_run_failure=lambda: record_session_interruption(
                current,
                save=lambda value: state_store.save_run(run_id, value),
            ),
        )
        return (
            current,
            resumed,
            self.receipt_from_record(
                closed, action_id=action_id, attached=attached
            ),
        )

    def _complete_applied_action_after_executor_exit(
        self,
        state: dict[str, Any],
        *,
        action_id: str,
        generation: int,
        resumed: bool,
        attached: bool,
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        record = self.control.snapshot(self.task, action_id)
        executor = record.get("executor") if isinstance(record, Mapping) else None
        if isinstance(executor, Mapping) and executor.get("status") in {
            "starting",
            "running",
        }:
            self.control.mark_executor_absent(
                self.task,
                action_id=action_id,
                generation=generation,
            )
        application_receipt = state.get("action_application_receipt")
        if not isinstance(application_receipt, Mapping):  # pragma: no cover - gated
            raise ActionReconciliationError(
                "Delivery Run 缺少可对账的 Action Application Receipt"
            )
        record = self.control.complete_action_from_application_receipt(
            self.task,
            action_id=action_id,
            generation=generation,
            application_receipt=application_receipt,
            result_status=(
                state.get("status") if isinstance(state.get("status"), str) else None
            ),
        )
        return (
            state,
            resumed,
            self.receipt_from_record(
                record, action_id=action_id, attached=attached
            ),
        )

    def _wait_for_action(
        self,
        spec: ExecutorSpec,
        state: dict[str, Any],
        action_id: str,
        generation: int,
        *,
        expected_action: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._observe_action(
                spec,
                state,
                action_id,
                generation,
                expected_action=expected_action,
            )
        except KeyboardInterrupt:
            self._wait_for_executor_handoff(spec, action_id)
            raise

    def _wait_for_executor_handoff(
        self, spec: ExecutorSpec, action_id: str
    ) -> None:
        deadline = self.clock() + self.startup_timeout
        while True:
            record = self.control.snapshot(self.task, action_id)
            action = record.get("action") if isinstance(record, dict) else None
            if isinstance(action, dict) and action.get("status") in {
                "completed",
                "failed",
            }:
                return
            observation = self.host.inspect(spec, self.control)
            if observation.status in {"running", "absent", "exited", "conflict"}:
                return
            if self.clock() >= deadline:
                return
            time.sleep(self.poll_interval)

    def _observe_action(
        self,
        spec: ExecutorSpec,
        state: dict[str, Any],
        action_id: str,
        generation: int,
        *,
        expected_action: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        startup_deadline = self.clock() + self.startup_timeout
        while True:
            record = self.control.snapshot(self.task, action_id)
            if record is None:
                recovered = self._recover_replaced_action(expected_action, state)
                if recovered is not None:
                    return recovered
                raise ActionReconciliationError("Action 在观察期间丢失")
            action = record.get("action")
            if not isinstance(action, dict):
                recovered = self._recover_replaced_action(expected_action, state)
                if recovered is not None:
                    return recovered
                raise ActionReconciliationError("Action 在观察期间无效")
            if action.get("action_id") != action_id:
                recovered = self._recover_replaced_action(expected_action, state)
                if recovered is not None:
                    return recovered
                raise ActionReconciliationError("Action 在观察期间被另一个 Action 替换")
            status = action.get("status")
            run_id = action.get("run_id")
            if status in {"completed", "failed"}:
                self.host.cleanup_startup(spec)
                if not isinstance(run_id, str):
                    if state.get("run_id") is not None:
                        return state
                    raise ActionReconciliationError(
                        "已收口 Action 没有关联 Delivery Run"
                    )
                final = self._state_store_for_record(record).load_current_run(run_id)
                if final is None:
                    raise ActionReconciliationError("Action 结果对应的 Run 不存在")
                return final

            observation = self.host.inspect(spec, self.control)
            if observation.status in {"absent", "exited"}:
                # Completion can be persisted after our snapshot but before
                # Host inspection. Re-read changed receipts before declaring loss.
                if self.control.snapshot(self.task, action_id) != record:
                    continue
                self.host.cleanup_startup(spec)
                raise ExecutorLostError(
                    "Executor 在 Action 完成前退出；只完成原 execution generation 对账，不自动重放 Agent"
                )
            if observation.status == "conflict":
                self.host.cleanup_startup(spec)
                raise _actionable_executor_unknown(
                    observation.reason or "Executor binding conflict"
                )
            if (
                observation.status in {"starting", "unknown"}
                and self.clock() >= startup_deadline
            ):
                self.host.cleanup_startup(spec)
                raise _actionable_executor_unknown(
                    observation.reason
                    or "Executor 未在握手窗口内完成启动；不会盲目启动第二个 Executor"
                )
            self.sleep(self.poll_interval)

    def _recover_replaced_action(
        self,
        expected_action: Mapping[str, Any] | None,
        fallback: dict[str, Any],
    ) -> dict[str, Any] | None:
        if expected_action is None:
            return None
        run_id = expected_action.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            run_id = fallback.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return None
        action_id = expected_action.get("action_id")
        historical_record = (
            self.control.snapshot(self.task, action_id)
            if isinstance(action_id, str)
            else None
        )
        final = self._state_store_for_record(
            historical_record if historical_record is not None else None
        ).load_current_run(run_id)
        if final is None or not _action_matches_run_receipt(final, expected_action):
            return None
        return final

    def _persist_application(
        self,
        *,
        action: Mapping[str, Any],
        run_id: str,
        generation: int,
        replace_receipt_action_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> dict[str, Any]:
        action_id = _string_field(action, "action_id")
        kind = _string_field(action, "kind")
        payload_digest = _string_field(action, "payload_digest")
        store = state_store or self.states
        current = store.load_current_run(run_id)
        if current is None:
            raise ActionReconciliationError("无法读取 Action 对应的 Delivery Run")
        expected = {
            "action_id": action_id,
            "kind": kind,
            "payload_digest": payload_digest,
            "run_id": run_id,
        }
        receipt = current.get("action_application_receipt")
        if receipt is not None and not isinstance(receipt, Mapping):
            raise ActionReconciliationError(
                "Delivery Run 的 Action Application Receipt 无法对账"
            )
        if isinstance(receipt, Mapping) and not action_receipt_matches(
            current, expected
        ):
            if (
                replace_receipt_action_id is None
                or receipt.get("action_id") != replace_receipt_action_id
                or receipt.get("run_id") != run_id
            ):
                raise ActionReconciliationError(
                    "Delivery Run 已绑定另一个 Lifecycle Action"
                )
        if not action_receipt_matches(current, expected) or (
            isinstance(receipt, Mapping)
            and receipt.get("executor_generation") != generation
        ):
            self.control.assert_executor_current(
                self.task,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            )
            prepare_action_application_receipt(
                current,
                {**action, "executor_generation": generation},
            )
            self._set_executor_state_fence(
                store,
                action_id=action_id,
                generation=generation,
                run_id=run_id,
            )
            store.save_run(run_id, current)
        self.control.assert_executor_current(
            self.task,
            action_id=action_id,
            generation=generation,
            run_id=run_id,
        )
        self.control.record_application(
            self.task,
            action_id=action_id,
            run_id=run_id,
            payload_digest=payload_digest,
            generation=generation,
            state_dir=getattr(store, "root", None),
        )
        return current

    def _receipt_for_action(self, action_id: str, *, attached: bool) -> ActionReceipt:
        record = self.control.snapshot(self.task, action_id)
        if record is None:
            raise ActionReconciliationError("Action Receipt 生成时 Action 不存在")
        return self.receipt_from_record(record, action_id=action_id, attached=attached)

    @staticmethod
    def _receipt_from_expected_action(
        action: Mapping[str, Any], state: Mapping[str, Any], *, attached: bool
    ) -> ActionReceipt:
        run_id = state.get("run_id")
        kind = action.get("kind")
        return ActionReceipt(
            action_id=(
                action.get("action_id")
                if isinstance(action.get("action_id"), str)
                else None
            ),
            kind=kind if isinstance(kind, str) else "run",
            run_id=run_id if isinstance(run_id, str) else None,
            status="completed",
            resume_intent=action_resume_intent(action),
            attached=attached,
            executor_status="exited",
            executor_generation=(
                action.get("executor_generation")
                if type(action.get("executor_generation")) is int
                else None
            ),
            handshake=True,
            payload_digest=(
                action.get("payload_digest")
                if isinstance(action.get("payload_digest"), str)
                else None
            ),
        )

    @staticmethod
    def receipt_from_record(
        record: Mapping[str, Any], *, action_id: str | None, attached: bool
    ) -> ActionReceipt:
        action = record.get("action")
        executor = record.get("executor")
        if not isinstance(action, Mapping):
            return ActionReceipt(
                action_id=action_id,
                kind="run",
                run_id=_record_run_id(record),
                status="executor_active",
                attached=attached,
                executor_status=(
                    executor.get("status") if isinstance(executor, Mapping) else None
                ),
                executor_generation=(
                    executor.get("generation")
                    if isinstance(executor, Mapping)
                    and type(executor.get("generation")) is int
                    else None
                ),
                handshake=isinstance(executor, Mapping)
                and isinstance(executor.get("handshake_at"), str),
                payload_digest=None,
            )
        generation = action.get("executor_generation")
        kind = action.get("kind")
        status = action.get("status")
        return ActionReceipt(
            action_id=action_id
            or (
                action.get("action_id")
                if isinstance(action.get("action_id"), str)
                else None
            ),
            kind=kind if isinstance(kind, str) else "run",
            run_id=_record_run_id(record),
            status=status if isinstance(status, str) else "unknown",
            resume_intent=action_resume_intent(action),
            attached=attached,
            executor_status=(
                executor.get("status") if isinstance(executor, Mapping) else None
            ),
            executor_generation=generation if type(generation) is int else None,
            handshake=isinstance(executor, Mapping)
            and isinstance(executor.get("handshake_at"), str),
            payload_digest=(
                action.get("payload_digest")
                if isinstance(action.get("payload_digest"), str)
                else None
            ),
            failure=(
                action.get("failure")
                if isinstance(action.get("failure"), str)
                else None
            ),
        )

    def _state_store_for_record(self, record: Mapping[str, Any] | None) -> StateStore:
        if isinstance(record, Mapping):
            raw_state_dir = record.get("run_state_dir")
            if isinstance(raw_state_dir, str) and raw_state_dir:
                state_root = Path(raw_state_dir)
                if state_root.is_absolute():
                    current_root = getattr(self.states, "root", None)
                    if (
                        current_root is not None
                        and Path(current_root).resolve() == state_root.resolve()
                    ):
                        return self.states
                    return StateStore(state_root.resolve())
        return self.states

    def _load_action_run(
        self,
        run_id: str | None,
        fallback: dict[str, Any] | None,
        *,
        record: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(run_id, str):
            loaded = self._state_store_for_record(record).load_current_run(run_id)
            if loaded is not None:
                return loaded
        if fallback is not None:
            return fallback
        raise ActionReconciliationError("活动 Executor 没有可读取的 Delivery Run")


def _record_run_id(record: Mapping[str, Any]) -> str | None:
    value = record.get("run_id")
    if isinstance(value, str) and value:
        return value
    action = record.get("action")
    if isinstance(action, Mapping):
        value = action.get("run_id")
        if isinstance(value, str) and value:
            return value
    return None


def _string_field(value: Mapping[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        raise ActionReconciliationError(f"字段 {key} 缺失或无效")
    return raw


def _positive_integer(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ActionReconciliationError("Executor generation 必须是正整数")
    return value


def _receipt_owner_action_id(
    record: Mapping[str, Any] | None,
    state: Mapping[str, Any] | None,
) -> str | None:
    """Find the Action whose receipt may be replaced by the current Action."""

    if not isinstance(record, Mapping) or not isinstance(state, Mapping):
        return None
    receipt = state.get("action_application_receipt")
    if not isinstance(receipt, Mapping):
        return None
    action = record.get("action")
    if (
        isinstance(action, Mapping)
        and action_receipt_matches(state, action)
        and isinstance(action.get("action_id"), str)
    ):
        action_id = action.get("action_id")
        return action_id if isinstance(action_id, str) else None
    history = record.get("action_history")
    if not isinstance(history, list):
        return None
    for entry in reversed(history):
        if not isinstance(entry, Mapping):
            continue
        predecessor = entry.get("action")
        if not isinstance(predecessor, Mapping):
            continue
        if (
            all(
                predecessor.get(key) == receipt.get(key)
                for key in ("action_id", "kind", "payload_digest")
            )
            and predecessor.get("run_id") == receipt.get("run_id")
            and isinstance(predecessor.get("action_id"), str)
        ):
            action_id = predecessor.get("action_id")
            return action_id if isinstance(action_id, str) else None
    return None


def _action_matches_run_receipt(
    state: Mapping[str, Any], action: Mapping[str, Any]
) -> bool:
    receipt = state.get("action_application_receipt")
    action_id = action.get("action_id")
    kind = action.get("kind")
    digest = action.get("payload_digest")
    run_id = state.get("run_id")
    return (
        isinstance(receipt, Mapping)
        and isinstance(action_id, str)
        and isinstance(kind, str)
        and isinstance(digest, str)
        and isinstance(run_id, str)
        and action_receipt_matches(state, action)
    )


def _unbound_action_matches_run_receipt(
    state: Mapping[str, Any], action: Mapping[str, Any]
) -> bool:
    """Match the durable-save window before Task Control binds the Run ID."""

    receipt = state.get("action_application_receipt")
    payload = action.get("payload")
    run_id = state.get("run_id")
    return (
        isinstance(receipt, Mapping)
        and isinstance(payload, Mapping)
        and isinstance(run_id, str)
        and action.get("run_id") is None
        and (
            payload.get("run_id") == run_id
            or (action.get("kind") == "run" and "run_id" not in payload)
        )
        and receipt.get("run_id") == run_id
        and all(
            receipt.get(key) == action.get(key)
            for key in ("action_id", "kind", "payload_digest")
        )
        and receipt.get("executor_generation")
        == action.get("executor_generation")
    )


def _actionable_executor_unknown(reason: str) -> ExecutorStartUnknownError:
    return ExecutorStartUnknownError(
        f"{reason}；请核验 Executor Host/Unit 与 Task Control 中的当前 "
        "Action/generation，确认旧 Executor 状态后原样重试命令；"
        "系统不会启动第二个 Executor"
    )


def _has_run_receipt(state: Mapping[str, Any] | None) -> bool:
    receipt = state.get("action_application_receipt") if state is not None else None
    return isinstance(receipt, Mapping) and all(
        isinstance(receipt.get(key), str) and bool(receipt.get(key))
        for key in ("action_id", "kind", "payload_digest", "run_id")
    )


def _restartable_after_executor_exit(state: Mapping[str, Any]) -> bool:
    status = state.get("status")
    if status in {"completed", "abandoned", "execution_failed", "ready_for_human"}:
        return False
    invocation = state.get("active_agent_invocation")
    if isinstance(invocation, Mapping) and invocation.get("status") in {
        "running",
        "resuming",
    }:
        return False
    return status in {
        "active",
        "starting",
        "waiting_checks",
        "waiting_external",
        "waiting_merge",
        "supervision_timeout",
        "ticket_completed",
        "parent_delivery_pending",
        "parent_closeout_pending",
        "run_acceptance_pending",
        "run_publication_pending",
        "publication_pending",
    }


def _safe_executor_recovery(state: Mapping[str, Any]) -> bool:
    """Resume proven idle boundaries without interrupting finished Agent work."""

    if state.get("status") == "ticket_completed":
        job = state.get("active_ticket_job")
        if not isinstance(job, Mapping) or job.get("phase") != "completed":
            return False
    elif state.get("status") not in {
        "waiting_checks",
        "waiting_external",
        "waiting_merge",
        "supervision_timeout",
    }:
        return False
    invocation = state.get("active_agent_invocation")
    return not (
        isinstance(invocation, Mapping)
        and invocation.get("status") in {"running", "resuming"}
    )
