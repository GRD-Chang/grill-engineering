"""The narrow lifecycle spine for the public ``run`` command."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_run.executor_host import (
    ExecutorHost,
    ExecutorLostError,
    ExecutorSpec,
    ExecutorStartUnknownError,
    HostObservation,
)
from agent_run.state import StateStore
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
)


@dataclass(frozen=True)
class LifecycleRequest:
    """A validated, closed lifecycle intent."""

    task: TaskKey
    kind: str
    payload: Mapping[str, Any]


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


class RunLifecycle:
    """Admit one ``run`` action and give its business work to one Executor.

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
        self.poll_interval = max(0.001, poll_interval)
        self.startup_timeout = max(self.poll_interval, startup_timeout)
        self.sleep = sleep
        self.clock = clock

    def submit(
        self, request: LifecycleRequest
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        if request.task != self.task:
            raise ActionReconciliationError(
                "Lifecycle Request 不属于当前 Delivery Task"
            )
        if request.kind != "run":
            raise ValueError("当前 Ticket 只实现 run Lifecycle Action")

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
        if (
            existing_control is None
            and isinstance(receipt, Mapping)
            and _has_run_receipt(current)
        ):
            requested_digest = payload_digest(request.payload)
            if (
                receipt.get("kind") != request.kind
                or receipt.get("payload_digest") != requested_digest
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
        record = self._reconcile_from_run(current, request.payload)
        action = record.get("action") if isinstance(record, Mapping) else None
        replace_receipt_action_id = _receipt_owner_action_id(record, current)
        current_run_id = current.get("run_id") if isinstance(current, Mapping) else None
        if (
            current is not None
            and isinstance(record, Mapping)
            and isinstance(action, Mapping)
            and action.get("status") in {"completed", "failed"}
            and action.get("kind") == request.kind
            and action.get("payload_digest") == payload_digest(request.payload)
            and action.get("run_id") == current_run_id
            and not _restartable_after_executor_exit(current)
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
                if observation.status == "absent":
                    if _safe_supervision_recovery(state):
                        self.control.mark_executor_absent(
                            self.task,
                            action_id=action_id,
                            generation=generation,
                        )
                        return self.submit(request)
                    raise ExecutorLostError(
                        "活动 Executor 已退出；不会盲目启动第二个 Executor"
                    )
                if self.clock() >= deadline:
                    raise ExecutorStartUnknownError(
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

    def _claim(self, request: LifecycleRequest) -> ActionClaim:
        return self.control.claim_action(
            self.task,
            kind=request.kind,
            payload=request.payload,
        )

    def _reconcile_from_run(
        self,
        current: dict[str, Any] | None,
        payload: Mapping[str, Any],
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

        def execute_action() -> Mapping[str, Any]:
            state, resumed = self.select_run(action)
            run_id = _string_field(state, "run_id")
            self.control.bind_run(
                self.task,
                action_id,
                run_id,
                state_dir=getattr(self.states, "root", None),
            )
            return self._apply_and_execute_bound_action(
                action=action,
                action_id=action_id,
                state=state,
                run_id=run_id,
                resumed=resumed,
                execution_context=execution_context,
                replace_receipt_action_id=replace_receipt_action_id,
            )

        action_run_id = action.get("run_id")
        if not isinstance(action_run_id, str) or not action_run_id:
            action_run_id = None
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
    ) -> tuple[dict[str, Any], bool, ActionReceipt]:
        action_id = _string_field(action, "action_id")
        latest = self.control.snapshot(self.task, action_id)
        if latest is None:
            raise ActionReconciliationError("Action 在继续前丢失")
        latest_action = latest.get("action")
        if not isinstance(latest_action, Mapping):
            raise ActionReconciliationError("Action 在继续前无效")
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
            generation = _positive_integer(action.get("executor_generation"))
            execution_context: dict[str, Any] = {}

            def execute_action() -> Mapping[str, Any]:
                return self._apply_and_execute_bound_action(
                    action=action,
                    action_id=action_id,
                    state=state,
                    run_id=run_id,
                    resumed=True,
                    execution_context=execution_context,
                    state_store=state_store,
                )

            return self._run_executor(
                action_id=action_id,
                run_id=run_id,
                attached=True,
                resumed=True,
                state=state,
                generation=generation,
                execute=execute_action,
                execution_context=execution_context,
            )
        return self._start_action(
            action,
            current,
            attached=True,
            replace_receipt_action_id=replace_receipt_action_id,
        )

    def _apply_and_execute_bound_action(
        self,
        state: dict[str, Any],
        *,
        action: Mapping[str, Any],
        action_id: str,
        run_id: str,
        resumed: bool,
        execution_context: dict[str, Any],
        replace_receipt_action_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> Mapping[str, Any]:
        if self.initialize_profile is not None:
            self.initialize_profile(state, resumed)
        prepared = self._persist_application(
            state,
            action_id=action_id,
            kind=_string_field(action, "kind"),
            payload_digest=_string_field(action, "payload_digest"),
            run_id=run_id,
            generation=self._current_action_generation(action_id),
            replace_receipt_action_id=replace_receipt_action_id,
            state_store=state_store,
        )
        execution_context.update(
            {"run_id": run_id, "resumed": resumed, "state": prepared}
        )
        result_status = prepared.get("status")
        self.control.complete_action(
            self.task,
            action_id=action_id,
            result_status=(result_status if isinstance(result_status, str) else None),
        )
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
            raise ExecutorStartUnknownError(
                "Delivery Run Receipt 已存在但 Executor ownership 缺失或不匹配；"
                "不会启动第二个 Executor"
            )
        if (
            isinstance(executor, Mapping)
            and executor.get("reconciliation_required") is True
        ):
            raise ExecutorStartUnknownError(
                "Task Control Record 曾丢失；Executor ownership 无法确认，"
                "不会启动第二个 Executor"
            )
        recover = _safe_supervision_recovery(state)
        if (
            state.get("run_id") is not None
            and not _restartable_after_executor_exit(state)
            and state.get("status")
            not in {"run_approval_pending", "parent_approval_pending"}
            and not recover
        ):
            # A prior Executor may have reached a terminal/operator boundary
            # immediately before its process disappeared.  The Run state is
            # then the authoritative result; replaying the Driver would turn
            # a lost receipt into a second business intent.
            if isinstance(executor, Mapping) and executor.get("status") in {
                "starting",
                "running",
            }:
                # A concurrent writer may have moved the Run to a boundary
                # while this Executor is still alive.  Do not close its
                # Action from an unverified observation.
                return (
                    state,
                    resumed,
                    self.receipt_from_record(
                        record, action_id=action_id, attached=attached
                    ),
                )
            record = self.control.fail_action(
                self.task,
                action_id=action_id,
                failure="Delivery Run reached a non-replayable boundary",
            )
            return (
                state,
                resumed,
                self.receipt_from_record(
                    record, action_id=action_id, attached=attached
                ),
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
                    raise ExecutorStartUnknownError(
                        observation.reason
                        or "Executor ownership 无法确认；不会启动第二个 Executor"
                    )
                # Keep checking this exact generation while the process
                # binding and handshake transaction is still being committed.
                self.sleep(self.poll_interval)
            if observation.status == "absent":
                if not recover:
                    raise ExecutorLostError(
                        "Executor 已退出；只完成原 execution generation 对账，不自动重放 Agent"
                    )
                observation = None
        elif isinstance(executor, dict) and executor.get("status") in {
            "exited",
            "absent",
        }:
            if executor.get("action_id") == action_id:
                if not recover:
                    raise ExecutorLostError(
                        "Executor 已收口但 Action 未完成；不会盲目重放业务意图"
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

    def _wait_for_action(
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
            if observation.status == "absent":
                raise ExecutorLostError(
                    "Executor 在 Action 完成前退出；只完成原 execution generation 对账，不自动重放 Agent"
                )
            if (
                observation.status in {"starting", "unknown"}
                and self.clock() >= startup_deadline
            ):
                raise ExecutorStartUnknownError(
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
        state: dict[str, Any],
        *,
        action_id: str,
        kind: str,
        payload_digest: str,
        run_id: str,
        generation: int,
        replace_receipt_action_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> dict[str, Any]:
        del state
        store = state_store or self.states
        with store.locked():
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
                prepare_action_application_receipt(
                    current,
                    {
                        "action_id": action_id,
                        "kind": kind,
                        "payload_digest": payload_digest,
                        "executor_generation": generation,
                    },
                )
                store.save_run(run_id, current)
        self.control.record_application(
            self.task,
            action_id=action_id,
            run_id=run_id,
            payload_digest=payload_digest,
            state_dir=getattr(store, "root", None),
        )
        return current

    def _current_action_generation(self, action_id: str) -> int:
        record = self.control.snapshot(self.task, action_id)
        if record is None:
            raise ActionReconciliationError("Action 在 Executor 应用前丢失")
        action = record.get("action")
        if not isinstance(action, Mapping):
            raise ActionReconciliationError("Action 在 Executor 应用前无效")
        return _positive_integer(action.get("executor_generation"))

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


def _safe_supervision_recovery(state: Mapping[str, Any]) -> bool:
    """Allow a new host generation only from a persisted, non-agent wait."""

    if state.get("status") not in {
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
