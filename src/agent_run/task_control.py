"""Durable, task-scoped control state for lifecycle actions.

The Delivery Run JSON remains the business state.  This module only stores the
small amount of coordination state needed to admit one action and one
executor for a repository/Parent pair.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_run.error_safety import bounded_error

TASK_CONTROL_PROTOCOL = 1
MAX_TASK_CONTROL_BYTES = 256 * 1024
_ACTIVE_ACTION_STATUSES = frozenset({"accepted", "applying"})
_TERMINAL_ACTION_STATUSES = frozenset({"completed", "failed"})
_ACTIVE_EXECUTOR_STATUSES = frozenset({"starting", "running"})
_MAX_ACTION_HISTORY = 4
_RECONCILABLE_FINAL_RUN_STATUSES = frozenset(
    {
        "execution_failed",
        "operator_stopped",
        "progress_exhausted",
        "ready_for_human",
        "run_approval_pending",
        "parent_approval_pending",
        "requeue_required",
        "unsupported_scope_change",
        "deterministic_contradiction",
        "abandonment_pending",
        "abandoned",
        "completed",
        "blocked",
    }
)


class TaskControlError(ValueError):
    """The task control record is unavailable or violates its contract."""


class TaskControlBusyError(TaskControlError):
    """Another process owns the bounded Task Control transaction."""


class ActionBusyError(TaskControlError):
    """Another, non-identical lifecycle action currently occupies the slot."""

    def __init__(
        self, message: str, *, action: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.action = dict(action) if action is not None else None


class ActionReconciliationError(TaskControlError):
    """The control record cannot be repaired from deterministic Run evidence."""


@dataclass(frozen=True)
class TaskKey:
    """Stable identity of one local Delivery Task."""

    workspace: Path
    repository: str
    parent_number: int

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).resolve()
        if not workspace.is_absolute():  # pragma: no cover - resolve is absolute
            raise TaskControlError("Delivery Task workspace must be absolute")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise TaskControlError("Delivery Task repository must be non-empty")
        if type(self.parent_number) is not int or self.parent_number <= 0:
            raise TaskControlError("Delivery Task Parent Issue must be positive")
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "repository", self.repository.strip())

    @property
    def identity(self) -> dict[str, object]:
        return {
            "workspace": str(self.workspace),
            "repository": self.repository,
            "parent": self.parent_number,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ActionClaim:
    """Result of the short admission transaction."""

    action: dict[str, Any] | None
    attached: bool
    executor_active: bool
    record: dict[str, Any]

    @property
    def action_id(self) -> str | None:
        value = self.action.get("action_id") if self.action is not None else None
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class ExecutorReservation:
    """Whether this caller created the executor slot or joined an existing one."""

    created: bool
    generation: int
    record: dict[str, Any]


class TaskControlStore:
    """One bounded JSON record and one non-blocking lock per :class:`TaskKey`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / "task-control"

    def path_for(self, task: TaskKey) -> Path:
        return self.directory / f"{task.fingerprint}.json"

    def load(self, task: TaskKey) -> dict[str, Any] | None:
        path = self.path_for(task)
        if not path.exists():
            return None
        return self._read_path(path, task)

    def inspect_run(
        self, task: TaskKey, run_state: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Read and validate one control record without reconciliation writes."""

        record = self.load(task)
        if record is None:
            return None
        _verify_record_matches_run(record, run_state)
        return record

    def claim_action(
        self,
        task: TaskKey,
        *,
        kind: str,
        payload: Mapping[str, Any],
        before_create: Callable[[], None] | None = None,
    ) -> ActionClaim:
        if not isinstance(kind, str) or not kind.strip():
            raise TaskControlError("Lifecycle Action kind must be non-empty")
        normalized_payload = _bounded_payload(payload)
        payload_digest = _payload_digest(normalized_payload)

        def existing_claim(record: dict[str, Any]) -> ActionClaim | None:
            current = record.get("action")
            if (
                isinstance(current, dict)
                and current.get("status") in _ACTIVE_ACTION_STATUSES
            ):
                if (
                    current.get("kind") == kind
                    and current.get("payload_digest") == payload_digest
                ):
                    return ActionClaim(
                        action=deepcopy(current),
                        attached=True,
                        executor_active=False,
                        record=deepcopy(record),
                    )
                raise ActionBusyError(
                    "当前 Delivery Task 已有未完成 Lifecycle Action；不会等待或排队",
                    action=current,
                )

            executor = record.get("executor")
            if (
                isinstance(executor, dict)
                and executor.get("status") in _ACTIVE_EXECUTOR_STATUSES
            ):
                same_executor_action = (
                    isinstance(current, dict)
                    and current.get("action_id") == executor.get("action_id")
                    and current.get("kind") == kind
                    and current.get("payload_digest") == payload_digest
                )
                if same_executor_action:
                    return ActionClaim(
                        action=None,
                        attached=True,
                        executor_active=True,
                        record=deepcopy(record),
                    )
                raise ActionBusyError(
                    "当前 Delivery Task 已有活动 Executor；不会提交新的 Lifecycle Action",
                    action=current if isinstance(current, dict) else None,
                )
            return None

        def claim_during_contention(error: TaskControlBusyError) -> ActionClaim:
            """Attach from an atomic snapshot without extending the lock wait."""

            try:
                record = self.load(task)
            except TaskControlError:
                raise error
            if record is not None:
                claim = existing_claim(record)
                if claim is not None:
                    return claim
            raise error

        snapshot = self.load(task)
        if snapshot is not None:
            claim = existing_claim(snapshot)
            if claim is not None:
                return claim

        try:
            with self._locked(task):
                record = self._read_unlocked(task) or _new_record(task)
                claim = existing_claim(record)
                if claim is not None:
                    return claim
        except TaskControlBusyError as error:
            return claim_during_contention(error)

        # Session preparation can inspect the environment and perform other
        # external work. It must not extend the Task Control transaction.
        if before_create is not None:
            before_create()

        try:
            with self._locked(task):
                # Another process may have claimed the Task while the session was
                # prepared. Revalidate ownership before creating the Action.
                record = self._read_unlocked(task) or _new_record(task)
                claim = existing_claim(record)
                if claim is not None:
                    return claim
                current = record.get("action")
                executor = record.get("executor")
                generation = _positive_integer(record.get("next_generation", 1))
                if isinstance(executor, dict) and executor.get("status") in {
                    "exited",
                    "absent",
                }:
                    generation = max(
                        generation,
                        _positive_integer(executor.get("generation")) + 1,
                    )
                if isinstance(current, dict) and current.get("status") in {
                    "completed",
                    "failed",
                }:
                    _archive_terminal_action(record, current)
                    # The successor has no Run or Executor binding until its own
                    # application receipt is durably written.  Keep the
                    # predecessor in bounded history instead of letting its
                    # pointers look like ownership of the new Action.
                    record["run_id"] = None
                    record["executor"] = None
                record["next_generation"] = generation + 1
                action = {
                    "action_id": uuid.uuid4().hex,
                    "kind": kind,
                    "payload": normalized_payload,
                    "payload_digest": payload_digest,
                    "status": "accepted",
                    "run_id": None,
                    "executor_generation": generation,
                    "application_observed": False,
                    "submitted_at": _now(),
                    "completed_at": None,
                    "result_status": None,
                    "failure": None,
                }
                record["action"] = action
                record["updated_at"] = _now()
                self._write_unlocked(task, record)
                return ActionClaim(
                    action=deepcopy(action),
                    attached=False,
                    executor_active=False,
                    record=deepcopy(record),
                )
        except TaskControlBusyError as error:
            return claim_during_contention(error)

    def reconcile_from_run(
        self,
        task: TaskKey,
        run_state: Mapping[str, Any] | None,
        *,
        payload: Mapping[str, Any] | None = None,
        state_dir: Path | None = None,
    ) -> dict[str, Any] | None:
        """Repair a missing/corrupt record only when its Run receipt is exact."""

        try:
            existing = self.load(task)
        except TaskControlError:
            existing = None
        if existing is not None:
            _verify_record_matches_run(existing, run_state)
            return existing

        receipt = _action_receipt(run_state)
        if receipt is None:
            if self.path_for(task).exists():
                raise ActionReconciliationError(
                    "Task Control Record 损坏，且 Delivery Run 没有可验证的 Action Application Receipt"
                )
            return None
        normalized_payload = _bounded_payload(payload) if payload is not None else {}
        if _payload_digest(normalized_payload) != receipt["payload_digest"]:
            raise ActionReconciliationError(
                "Delivery Run Receipt 与待对账 Action payload 不一致"
            )
        record = _new_record(task)
        if state_dir is not None:
            record["run_state_dir"] = str(Path(state_dir).resolve())
        final_run = (
            isinstance(run_state, Mapping)
            and run_state.get("status") in _RECONCILABLE_FINAL_RUN_STATUSES
        )
        action = {
            "action_id": receipt["action_id"],
            "kind": receipt["kind"],
            "payload": normalized_payload,
            "payload_digest": receipt["payload_digest"],
            "status": "completed" if final_run else "accepted",
            "run_id": receipt["run_id"],
            "executor_generation": receipt.get("executor_generation", 1),
            "application_observed": True,
            "submitted_at": receipt.get("applied_at", _now()),
            "completed_at": _now() if final_run else None,
            "result_status": (
                run_state.get("status")
                if final_run and isinstance(run_state, Mapping)
                else None
            ),
            "failure": None,
        }
        record["run_id"] = receipt["run_id"]
        record["action"] = action
        if not final_run:
            record["executor"] = {
                "status": "absent",
                "action_id": action["action_id"],
                "run_id": action["run_id"],
                "generation": action["executor_generation"],
                "binding_token": "reconciliation-required",
                "pid": None,
                "process_start_token": None,
                "handshake_at": None,
                "started_at": action["submitted_at"],
                "exited_at": None,
                "failure": (
                    "Task Control Record was missing; Executor ownership is unknown"
                ),
                "reconciliation_required": True,
            }
        record["next_generation"] = max(
            2, _positive_integer(action["executor_generation"]) + 1
        )
        record["updated_at"] = _now()
        with self._locked(task):
            try:
                current = self._read_unlocked(task)
            except TaskControlError:
                current = None
            if current is not None:
                _verify_record_matches_run(current, run_state)
                return current
            self._write_unlocked(task, record)
        return deepcopy(record)

    def bind_run(
        self,
        task: TaskKey,
        action_id: str,
        run_id: str,
        *,
        generation: int | None = None,
        state_dir: Path | None = None,
    ) -> dict[str, Any]:
        if not run_id:
            raise TaskControlError("Lifecycle Action is missing its Delivery Run ID")
        with self._locked(task):
            record = self._require_unlocked(task)
            action = _require_active_action(record, action_id)
            if (
                generation is not None
                and _positive_integer(action.get("executor_generation"))
                != generation
            ):
                raise ActionReconciliationError(
                    "Lifecycle Action generation changed before Run binding"
                )
            if action.get("run_id") not in {None, run_id}:
                raise ActionReconciliationError(
                    "Lifecycle Action is already bound to another Delivery Run"
                )
            if action.get("run_id") is not None and record.get("run_id") not in {
                None,
                run_id,
            }:
                raise ActionReconciliationError(
                    "Delivery Task is already bound to another Delivery Run"
                )
            action["run_id"] = run_id
            record["run_id"] = run_id
            if state_dir is not None:
                record["run_state_dir"] = str(Path(state_dir).resolve())
            executor = record.get("executor")
            if isinstance(executor, dict) and executor.get("action_id") == action_id:
                if (
                    generation is not None
                    and executor.get("generation") != generation
                ):
                    raise ActionReconciliationError(
                        "Executor generation changed before Run binding"
                    )
                if executor.get("run_id") not in {None, run_id}:
                    raise ActionReconciliationError(
                        "Executor is already bound to another Delivery Run"
                    )
                executor["run_id"] = run_id
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def record_application(
        self,
        task: TaskKey,
        *,
        action_id: str,
        run_id: str,
        payload_digest: str,
        generation: int | None = None,
        state_dir: Path | None = None,
    ) -> dict[str, Any]:
        with self._locked(task):
            record = self._require_unlocked(task)
            executor: dict[str, Any] | None = None
            if generation is not None:
                executor = _require_executor(record, action_id, generation)
                if executor.get("status") not in _ACTIVE_EXECUTOR_STATUSES:
                    raise ActionReconciliationError(
                        "Executor ownership is no longer active during Action application"
                    )
            action = _require_active_action(record, action_id)
            if action.get("payload_digest") != payload_digest:
                raise ActionReconciliationError(
                    "Lifecycle Action payload digest changed"
                )
            if action.get("run_id") not in {None, run_id}:
                raise ActionReconciliationError(
                    "Lifecycle Action is bound to another Run"
                )
            action["run_id"] = run_id
            action["application_observed"] = True
            record["run_id"] = run_id
            if state_dir is not None:
                record["run_state_dir"] = str(Path(state_dir).resolve())
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def require_mutation_available(self, task: TaskKey) -> None:
        """Reject a legacy mutation from one short control transaction."""

        if not self.path_for(task).exists():
            return
        with self._locked(task):
            record = self._read_unlocked(task)
            if record is not None:
                action = record.get("action")
                if isinstance(action, dict) and action.get("status") in {
                    "accepted",
                    "applying",
                }:
                    raise ActionBusyError(
                        "当前 Delivery Task 已有未完成 Lifecycle Action；不会绕过 Action Admission Gate",
                        action=action,
                    )
                executor = record.get("executor")
                if isinstance(executor, dict) and executor.get("status") in {
                    "starting",
                    "running",
                }:
                    raise ActionBusyError(
                        "当前 Delivery Task 已有活动 Executor；不会绕过 Executor 控制脊柱",
                        action=action if isinstance(action, dict) else None,
                    )

    def begin_executor(
        self,
        task: TaskKey,
        *,
        action_id: str,
        run_id: str | None,
        reclaim: bool = False,
        runner_binding: str | None = None,
        before_create: Callable[[], None] | None = None,
    ) -> ExecutorReservation:

        def existing_reservation(
            record: dict[str, Any]
        ) -> ExecutorReservation | None:
            action = _require_active_action(record, action_id)
            if action.get("run_id") not in {None, run_id}:
                raise ActionReconciliationError(
                    "Executor Action is bound to another Run"
                )
            existing = record.get("executor")
            if (
                isinstance(existing, dict)
                and existing.get("status") in _ACTIVE_EXECUTOR_STATUSES
            ):
                same_executor = (
                    existing.get("action_id") == action_id
                    and existing.get("run_id") == run_id
                )
                if same_executor:
                    return ExecutorReservation(
                        created=False,
                        generation=_positive_integer(existing.get("generation")),
                        record=deepcopy(record),
                    )
                raise ActionBusyError(
                    "当前 Delivery Task 已有归属不明或活动 Executor；不会启动第二个 Executor",
                    action=action,
                )
            if (
                isinstance(existing, dict)
                and existing.get("status") in {"exited", "absent"}
                and existing.get("action_id") == action_id
                and not reclaim
            ):
                raise ActionReconciliationError(
                    "原 Executor 已退出；必须显式恢复，不能重放原业务意图"
                )
            return None

        with self._locked(task):
            record = self._require_unlocked(task)
            reservation = existing_reservation(record)
            if reservation is not None:
                return reservation

        # Environment capture and other launch preparation are external work;
        # do it before the short reservation transaction and revalidate after.
        if before_create is not None:
            before_create()

        with self._locked(task):
            record = self._require_unlocked(task)
            reservation = existing_reservation(record)
            if reservation is not None:
                return reservation
            action = _require_active_action(record, action_id)
            existing = record.get("executor")
            generation = _positive_integer(action.get("executor_generation"))
            if (
                isinstance(existing, dict)
                and existing.get("status") in {"exited", "absent"}
                and existing.get("action_id") == action_id
            ):
                generation = max(
                    generation + 1, _positive_integer(record.get("next_generation", 1))
                )
                action["executor_generation"] = generation
                record["next_generation"] = generation + 1
            binding_token = uuid.uuid4().hex
            record["executor"] = {
                "status": "starting",
                "action_id": action_id,
                "run_id": run_id,
                "generation": generation,
                "binding_token": binding_token,
                "pid": None,
                "process_start_token": None,
                "handshake_at": None,
                "started_at": _now(),
                "exited_at": None,
                "failure": None,
            }
            if runner_binding is not None:
                _validate_runner_binding(runner_binding)
                record["executor"]["runner_binding"] = runner_binding
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return ExecutorReservation(
                created=True, generation=generation, record=deepcopy(record)
            )

    def mark_executor_absent(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int,
    ) -> dict[str, Any]:
        """Record a host observation before an explicit Executor recovery."""

        with self._locked(task):
            record = self._require_unlocked(task)
            executor = _require_executor(record, action_id, generation)
            if executor.get("status") in _ACTIVE_EXECUTOR_STATUSES:
                executor["status"] = "absent"
                executor["exited_at"] = _now()
                executor["failure"] = "host proved the recorded process absent"
                record["updated_at"] = _now()
                self._write_unlocked(task, record)
            elif executor.get("status") not in {"exited", "absent"}:
                raise ActionReconciliationError(
                    "Executor 不是可恢复的 active/exited/absent 状态"
                )
            return deepcopy(record)

    def record_reconciled_executor_exit(
        self,
        task: TaskKey,
        *,
        action_id: str,
        run_id: str,
        generation: int,
        observed_status: str,
        observed_generation: int | None,
        observed_runner_binding: str,
    ) -> dict[str, Any]:
        """Persist an exact Host exit proof while retaining the Action gate."""

        with self._locked(task):
            record = self._require_unlocked(task)
            action = _require_active_action(record, action_id)
            executor = _require_executor(record, action_id, generation)
            if not (
                observed_status == "exited"
                and observed_generation == generation
                and record.get("run_id") == run_id
                and action.get("run_id") == run_id
                and action.get("executor_generation") == generation
                and executor.get("run_id") == run_id
                and executor.get("status") == "absent"
                and executor.get("binding_token") == "reconciliation-required"
                and executor.get("reconciliation_required") is True
                and executor.get("pid") is None
                and executor.get("process_start_token") is None
                and executor.get("handshake_at") is None
            ):
                raise ActionReconciliationError(
                    "Host 证据无法恢复准确的 receipt-only Executor ownership"
                )
            try:
                runner_binding = _validate_runner_binding(observed_runner_binding)
            except TaskControlError as error:
                raise ActionReconciliationError(
                    "Host 证据缺少准确的 receipt-only Runner binding"
                ) from error
            executor.update(
                {
                    "status": "exited",
                    "exited_at": _now(),
                    "failure": "host proved the receipt-bound Executor exited",
                }
            )
            executor["runner_binding"] = runner_binding
            executor.pop("reconciliation_required", None)
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def assert_executor_current(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int,
        run_id: str | None = None,
    ) -> None:
        """Revalidate the exact Executor fence before an external side effect."""

        with self._executor_current_transaction(
            task,
            action_id=action_id,
            generation=generation,
            run_id=run_id,
        ):
            return

    @contextmanager
    def _executor_current_transaction(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int,
        run_id: str | None = None,
    ) -> Iterator[None]:
        """Hold ownership through one short external/state commit boundary."""

        with self._locked(task):
            record = self._require_unlocked(task)
            executor = _require_executor(record, action_id, generation)
            if executor.get("status") not in _ACTIVE_EXECUTOR_STATUSES:
                raise ActionReconciliationError(
                    "Executor ownership is no longer active at Run state commit"
                )
            if run_id is not None and executor.get("run_id") not in {None, run_id}:
                raise ActionReconciliationError(
                    "Executor is bound to another Delivery Run"
                )
            action = record.get("action")
            if not isinstance(action, dict) or action.get("action_id") != action_id:
                raise ActionReconciliationError(
                    "Executor Action is no longer the current Task Action"
                )
            if action.get("status") not in _ACTIVE_ACTION_STATUSES | {"completed"}:
                raise ActionReconciliationError(
                    "Executor Action is no longer authorized"
                )
            yield

    def mark_process_started(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int,
        pid: int,
        process_start_token: str | None,
    ) -> dict[str, Any]:
        with self._locked(task):
            record = self._require_unlocked(task)
            executor = _require_executor(record, action_id, generation)
            if executor.get("status") == "starting":
                executor["pid"] = pid
                executor["process_start_token"] = process_start_token
                record["updated_at"] = _now()
                self._write_unlocked(task, record)
            return deepcopy(record)

    def mark_handshake(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int,
        pid: int,
        process_start_token: str | None,
    ) -> dict[str, Any]:
        with self._locked(task):
            record = self._require_unlocked(task)
            executor = _require_executor(record, action_id, generation)
            if executor.get("pid") not in {None, pid}:
                raise ActionReconciliationError(
                    "Executor handshake PID does not match binding"
                )
            executor.update(
                {
                    "status": "running",
                    "pid": pid,
                    "process_start_token": process_start_token,
                    "handshake_at": _now(),
                }
            )
            action = _require_active_action(record, action_id)
            action["status"] = "applying"
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def finish_executor(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int,
        result_status: str | None = None,
        failure: str | None = None,
    ) -> dict[str, Any]:
        with self._locked(task):
            record = self._require_unlocked(task)
            executor = _require_executor(record, action_id, generation)
            action = record.get("action")
            if not isinstance(action, dict) or action.get("action_id") != action_id:
                raise ActionReconciliationError(
                    "Executor Action 不再是当前 Task Action"
                )
            executor["status"] = "exited"
            executor["exited_at"] = _now()
            if failure is not None:
                executor["failure"] = _bounded_text(failure)
            if action.get("status") in _ACTIVE_ACTION_STATUSES:
                action["status"] = "failed" if failure is not None else "completed"
                action["completed_at"] = _now()
                action["result_status"] = result_status
                action["failure"] = (
                    _bounded_text(failure) if failure is not None else None
                )
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def complete_action(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int | None = None,
        result_status: str | None = None,
    ) -> dict[str, Any]:
        """Release admission after handshake and durable intent application."""

        with self._locked(task):
            record = self._require_unlocked(task)
            action = _require_active_action(record, action_id)
            executor = record.get("executor")
            if generation is not None:
                _require_executor(record, action_id, generation)
            if (
                not isinstance(executor, dict)
                or executor.get("action_id") != action_id
                or executor.get("status") != "running"
                or not isinstance(executor.get("handshake_at"), str)
            ):
                raise ActionReconciliationError(
                    "Lifecycle Action 只能在准确 Executor 握手后收口"
                )
            if action.get("application_observed") is not True:
                raise ActionReconciliationError(
                    "Lifecycle Action 只能在业务意图持久应用后收口"
                )
            action["status"] = "completed"
            action["completed_at"] = _now()
            action["result_status"] = result_status
            action["failure"] = None
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def complete_action_from_application_receipt(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int,
        application_receipt: Mapping[str, Any],
        result_status: str | None = None,
    ) -> dict[str, Any]:
        """Close an applied Action after the exact Executor is proven gone."""

        with self._locked(task):
            record = self._require_unlocked(task)
            action_value = record.get("action")
            if (
                not isinstance(action_value, dict)
                or action_value.get("action_id") != action_id
                or action_value.get("status")
                not in _ACTIVE_ACTION_STATUSES | {"failed"}
            ):
                raise ActionReconciliationError(
                    "Lifecycle Action 不再是可对账的当前 Task Action"
                )
            action = action_value
            executor = _require_executor(record, action_id, generation)
            if executor.get("reconciliation_required") is True:
                raise ActionReconciliationError(
                    "Executor Session ownership 仅由 Run Receipt 合成；"
                    "未证明原 Executor 已退出"
                )
            if executor.get("status") not in {"exited", "absent"}:
                raise ActionReconciliationError(
                    "Lifecycle Action 崩溃对账前未证明 Executor 已退出"
                )
            expected = {
                "action_id": action_id,
                "kind": action.get("kind"),
                "payload_digest": action.get("payload_digest"),
                "run_id": action.get("run_id"),
                "executor_generation": generation,
            }
            if any(
                application_receipt.get(key) != value
                for key, value in expected.items()
            ):
                raise ActionReconciliationError(
                    "Delivery Run Receipt 与崩溃 Action ownership 不一致"
                )
            action["application_observed"] = True
            action["status"] = "completed"
            action["completed_at"] = _now()
            action["result_status"] = result_status
            action["failure"] = None
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def fail_session_from_application_receipt(
        self,
        task: TaskKey,
        *,
        action_id: str,
        generation: int,
        application_receipt: Mapping[str, Any],
        persist_run_failure: Callable[[], None],
    ) -> dict[str, Any]:
        """Atomically fence a proven dead Executor around its Run failure save."""

        with self._locked(task):
            record = self._require_unlocked(task)
            action_value = record.get("action")
            if (
                not isinstance(action_value, dict)
                or action_value.get("action_id") != action_id
                or action_value.get("status")
                not in _ACTIVE_ACTION_STATUSES | _TERMINAL_ACTION_STATUSES
            ):
                raise ActionReconciliationError(
                    "Lifecycle Action 不再是可对账的当前 Task Action"
                )
            action = action_value
            executor = _require_executor(record, action_id, generation)
            if executor.get("reconciliation_required") is True:
                raise ActionReconciliationError(
                    "Executor Session ownership 仅由 Run Receipt 合成；"
                    "未证明原 Executor 已退出"
                )
            if executor.get("status") not in {"exited", "absent"}:
                raise ActionReconciliationError(
                    "Executor Session 对账前未证明 Executor 已退出"
                )
            receipt_run_id = application_receipt.get("run_id")
            expected = {
                "action_id": action_id,
                "kind": action.get("kind"),
                "payload_digest": action.get("payload_digest"),
                "executor_generation": generation,
            }
            if (
                not isinstance(receipt_run_id, str)
                or not receipt_run_id
                or any(
                    application_receipt.get(key) != value
                    for key, value in expected.items()
                )
                or action.get("run_id") not in {None, receipt_run_id}
                or executor.get("run_id") not in {None, receipt_run_id}
                or record.get("run_id") not in {None, receipt_run_id}
            ):
                raise ActionReconciliationError(
                    "Delivery Run Receipt 与中断 Session ownership 不一致"
                )

            # Hold the Task Control transaction across the one local Run-state
            # commit so no successor Action can pass admission in between.
            persist_run_failure()
            action["run_id"] = receipt_run_id
            action["application_observed"] = True
            if action.get("status") in _ACTIVE_ACTION_STATUSES:
                action["status"] = "failed"
                action["completed_at"] = _now()
                action["result_status"] = "execution_failed"
                action["failure"] = "session_interrupted"
            record["run_id"] = receipt_run_id
            executor.update(
                {
                    "status": "exited",
                    "run_id": receipt_run_id,
                    "exited_at": _now(),
                    "failure": "session_interrupted",
                }
            )
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def fail_action(
        self, task: TaskKey, *, action_id: str, failure: str
    ) -> dict[str, Any]:
        with self._locked(task):
            record = self._require_unlocked(task)
            action = _require_active_action(record, action_id)
            action["status"] = "failed"
            action["completed_at"] = _now()
            action["failure"] = _bounded_text(failure)
            record["updated_at"] = _now()
            self._write_unlocked(task, record)
            return deepcopy(record)

    def snapshot(
        self, task: TaskKey, action_id: str | None = None
    ) -> dict[str, Any] | None:
        record = self.load(task)
        if record is None:
            return None
        if action_id is not None:
            action = record.get("action")
            if isinstance(action, dict) and action.get("action_id") == action_id:
                return record
            history = record.get("action_history")
            if isinstance(history, list):
                for entry in reversed(history):
                    if not isinstance(entry, dict):
                        continue
                    historical_action = entry.get("action")
                    if not isinstance(historical_action, dict):
                        continue
                    if historical_action.get("action_id") != action_id:
                        continue
                    historical_record = deepcopy(record)
                    historical_record["run_id"] = entry.get(
                        "run_id", historical_action.get("run_id")
                    )
                    historical_record["run_state_dir"] = entry.get(
                        "state_dir", record.get("run_state_dir")
                    )
                    historical_record["action"] = deepcopy(historical_action)
                    historical_record["executor"] = deepcopy(entry.get("executor"))
                    return historical_record
            return None
        return record

    @contextmanager
    def _locked(self, task: TaskKey) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / f".{task.fingerprint}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(
                    lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except BlockingIOError as error:
                raise TaskControlBusyError(
                    "Delivery Task Control 正在提交短事务；本次未等待且未写入，请重试"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self, task: TaskKey) -> dict[str, Any] | None:
        path = self.path_for(task)
        if not path.exists():
            return None
        return self._read_path(path, task)

    def _require_unlocked(self, task: TaskKey) -> dict[str, Any]:
        record = self._read_unlocked(task)
        if record is None:
            raise ActionReconciliationError("Delivery Task Control Record 不存在")
        return record

    def _read_path(self, path: Path, task: TaskKey) -> dict[str, Any]:
        try:
            with path.open("rb") as source:
                data = source.read(MAX_TASK_CONTROL_BYTES + 1)
        except OSError as error:
            raise TaskControlError(f"无法读取 Task Control Record: {path}") from error
        if len(data) > MAX_TASK_CONTROL_BYTES:
            raise TaskControlError("Task Control Record 超过大小上限")
        try:
            loaded: object = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TaskControlError("Task Control Record 不是有效 JSON") from error
        if not isinstance(loaded, dict):
            raise TaskControlError("Task Control Record 必须是对象")
        _validate_record(loaded, task)
        return loaded

    def _write_unlocked(self, task: TaskKey, record: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            record, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")
        if len(encoded) > MAX_TASK_CONTROL_BYTES:
            raise TaskControlError("Task Control Record 超过大小上限")
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.path_for(task)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.directory,
            prefix=f".{task.fingerprint}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(encoded)
                temporary_file.write(b"\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def _new_record(task: TaskKey) -> dict[str, Any]:
    return {
        "protocol": TASK_CONTROL_PROTOCOL,
        "task": task.identity,
        "run_id": None,
        "run_state_dir": None,
        "next_generation": 1,
        "action": None,
        "action_history": [],
        "executor": None,
        "updated_at": _now(),
    }


def _archive_terminal_action(record: dict[str, Any], action: Mapping[str, Any]) -> None:
    history = record.setdefault("action_history", [])
    if not isinstance(history, list):
        raise TaskControlError("Task Control Action history is invalid")
    history.append(
        {
            "run_id": record.get("run_id"),
            "state_dir": record.get("run_state_dir"),
            "action": deepcopy(dict(action)),
            "executor": deepcopy(record.get("executor")),
        }
    )
    if len(history) > _MAX_ACTION_HISTORY:
        del history[: len(history) - _MAX_ACTION_HISTORY]


def _validate_record(record: Mapping[str, Any], task: TaskKey) -> None:
    if record.get("protocol") != TASK_CONTROL_PROTOCOL:
        raise TaskControlError("Task Control Record protocol is incompatible")
    identity = record.get("task")
    if not isinstance(identity, dict) or identity != task.identity:
        raise TaskControlError(
            "Task Control Record identity does not match the checkout"
        )
    if record.get("run_id") is not None and not isinstance(record.get("run_id"), str):
        raise TaskControlError("Task Control Record Run ID is invalid")
    run_state_dir = record.get("run_state_dir")
    if run_state_dir is not None and (
        not isinstance(run_state_dir, str) or not Path(run_state_dir).is_absolute()
    ):
        raise TaskControlError("Task Control Record state directory is invalid")
    _positive_integer(record.get("next_generation", 1))
    history = record.get("action_history", [])
    if not isinstance(history, list) or len(history) > _MAX_ACTION_HISTORY:
        raise TaskControlError("Task Control Action history is invalid")
    for entry in history:
        if not isinstance(entry, dict) or set(entry) not in (
            {"run_id", "action", "executor"},
            {"run_id", "state_dir", "action", "executor"},
        ):
            raise TaskControlError("Task Control Action history is invalid")
        historical_run_id = entry.get("run_id")
        if historical_run_id is not None and not isinstance(historical_run_id, str):
            raise TaskControlError("Task Control Action history Run ID is invalid")
        historical_state_dir = entry.get("state_dir")
        if historical_state_dir is not None and (
            not isinstance(historical_state_dir, str)
            or not Path(historical_state_dir).is_absolute()
        ):
            raise TaskControlError(
                "Task Control Action history state directory is invalid"
            )
        historical_action = entry.get("action")
        if not isinstance(historical_action, dict):
            raise TaskControlError("Task Control Action history Action is invalid")
        historical_record: dict[str, Any] = {
            "protocol": TASK_CONTROL_PROTOCOL,
            "task": task.identity,
            "run_id": historical_run_id,
            "run_state_dir": historical_state_dir or record.get("run_state_dir"),
            "next_generation": 1,
            "action": historical_action,
            "executor": entry.get("executor"),
            "action_history": [],
            "updated_at": record.get("updated_at"),
        }
        _validate_record(historical_record, task)
    action = record.get("action")
    if action is not None:
        if not isinstance(action, dict):
            raise TaskControlError("Task Control Record Action is invalid")
        if not isinstance(action.get("action_id"), str) or not action["action_id"]:
            raise TaskControlError("Task Control Record Action ID is invalid")
        if (
            action.get("status")
            not in _ACTIVE_ACTION_STATUSES | _TERMINAL_ACTION_STATUSES
        ):
            raise TaskControlError("Task Control Record Action status is invalid")
        if not isinstance(action.get("kind"), str) or not action["kind"]:
            raise TaskControlError("Task Control Record Action kind is invalid")
        if (
            not isinstance(action.get("payload_digest"), str)
            or not action["payload_digest"]
        ):
            raise TaskControlError("Task Control Record Action digest is invalid")
        if not isinstance(action.get("payload"), dict):
            raise TaskControlError("Task Control Record Action payload is invalid")
        _positive_integer(action.get("executor_generation"))
        if action.get("run_id") is not None and not isinstance(
            action.get("run_id"), str
        ):
            raise TaskControlError("Task Control Record Action Run ID is invalid")
        try:
            normalized_payload = _bounded_payload(action["payload"])
        except TaskControlError as error:
            raise TaskControlError(
                "Task Control Record Action payload is invalid"
            ) from error
        if _payload_digest(normalized_payload) != action["payload_digest"]:
            raise TaskControlError(
                "Task Control Record Action payload digest is invalid"
            )
        if (
            record.get("run_id") is not None
            and action.get("run_id") is not None
            and record.get("run_id") != action.get("run_id")
        ):
            raise TaskControlError(
                "Task Control Record Action Run ID pointer is invalid"
            )
    executor = record.get("executor")
    if executor is not None:
        if not isinstance(executor, dict):
            raise TaskControlError("Task Control Record Executor is invalid")
        if executor.get("status") not in _ACTIVE_EXECUTOR_STATUSES | {
            "exited",
            "absent",
        }:
            raise TaskControlError("Task Control Record Executor status is invalid")
        if not isinstance(executor.get("action_id"), str) or not executor["action_id"]:
            raise TaskControlError("Task Control Record Executor Action ID is invalid")
        if executor.get("run_id") is not None and not isinstance(
            executor.get("run_id"), str
        ):
            raise TaskControlError("Task Control Record Executor Run ID is invalid")
        _positive_integer(executor.get("generation"))
        pid = executor.get("pid")
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise TaskControlError("Task Control Record Executor PID is invalid")
        runner_binding = executor.get("runner_binding")
        if runner_binding is not None:
            _validate_runner_binding(runner_binding)
        if isinstance(action, dict):
            if executor.get("action_id") != action.get("action_id"):
                raise TaskControlError(
                    "Task Control Record Action/Executor pointer is invalid"
                )
            if executor.get("generation") != action.get("executor_generation"):
                raise TaskControlError(
                    "Task Control Record Action/Executor generation is invalid"
                )
            if (
                executor.get("run_id") is not None
                and action.get("run_id") is not None
                and executor.get("run_id") != action.get("run_id")
            ):
                raise TaskControlError(
                    "Task Control Record Action/Executor Run ID is invalid"
                )


def _verify_record_matches_run(
    record: Mapping[str, Any], run_state: Mapping[str, Any] | None
) -> None:
    run_id = run_state.get("run_id") if isinstance(run_state, Mapping) else None
    receipt = _action_receipt(run_state)
    action = record.get("action")
    executor = record.get("executor")
    if isinstance(run_id, str) and receipt is not None:
        if receipt["run_id"] != run_id:
            raise ActionReconciliationError("Delivery Run Receipt 与 Run ID 不一致")
        for key, label in (
            ("run_id", "Run ID pointer"),
            ("action", "Action Run ID"),
            ("executor", "Executor Run ID"),
        ):
            value = record.get(key)
            if key == "action" and isinstance(value, Mapping):
                value = value.get("run_id")
            elif key == "executor" and isinstance(value, Mapping):
                value = value.get("run_id")
            if value is not None and value != run_id:
                raise ActionReconciliationError(
                    f"Task Control Record {label} 与 Run ID 不一致"
                )
    elif isinstance(run_id, str):
        # A completed record may legitimately belong to the preceding Run of
        # the same task.  Active ownership, however, can never cross Run IDs.
        action_status = action.get("status") if isinstance(action, Mapping) else None
        executor_status = (
            executor.get("status") if isinstance(executor, Mapping) else None
        )
        if action_status in _ACTIVE_ACTION_STATUSES or executor_status in {
            "starting",
            "running",
        }:
            for key, label in (
                ("run_id", "Run ID pointer"),
                ("action", "Action Run ID"),
                ("executor", "Executor Run ID"),
            ):
                value = record.get(key)
                if key == "action" and isinstance(value, Mapping):
                    value = value.get("run_id")
                elif key == "executor" and isinstance(value, Mapping):
                    value = value.get("run_id")
                if value is not None and value != run_id:
                    raise ActionReconciliationError(
                        f"Task Control Record {label} 与 Run ID 不一致"
                    )
    if receipt is None:
        return
    if not isinstance(action, Mapping):
        raise ActionReconciliationError("Task Control Record 缺少已应用 Action")
    if any(
        action.get(key) != receipt[key]
        for key in ("action_id", "kind", "payload_digest")
    ):
        if not _is_unbound_successor_of_receipt(record, action, receipt):
            raise ActionReconciliationError(
                "Task Control Record 与 Run Application Receipt 不一致"
            )
        return
    if action.get("run_id") not in {None, receipt["run_id"]}:
        raise ActionReconciliationError("Task Control Record 与 Run ID 不一致")
    if action.get("executor_generation") != receipt["executor_generation"]:
        raise ActionReconciliationError(
            "Task Control Record 与 Run Executor generation 不一致"
        )
    if (
        isinstance(executor, Mapping)
        and executor.get("generation") != receipt["executor_generation"]
    ):
        raise ActionReconciliationError(
            "Task Control Executor 与 Run Receipt generation 不一致"
        )
    if record.get("run_id") not in {None, receipt["run_id"]}:
        raise ActionReconciliationError("Task Control Record 与 Run ID 指针不一致")


def _action_receipt(state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    raw = state.get("action_application_receipt")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ActionReconciliationError("Delivery Run Action Application Receipt 无效")
    required = ("action_id", "kind", "payload_digest", "run_id")
    if not all(isinstance(raw.get(key), str) and raw.get(key) for key in required):
        raise ActionReconciliationError(
            "Delivery Run Action Application Receipt 缺少可验证字段"
        )
    state_run_id = state.get("run_id")
    if state_run_id is not None and (
        not isinstance(state_run_id, str) or raw.get("run_id") != state_run_id
    ):
        raise ActionReconciliationError("Delivery Run Receipt 与 Run ID 不一致")
    if raw.get("protocol") != TASK_CONTROL_PROTOCOL:
        raise ActionReconciliationError(
            "Delivery Run Action Application Receipt protocol 不兼容"
        )
    if (
        type(raw.get("executor_generation")) is not int
        or raw["executor_generation"] <= 0
    ):
        raise ActionReconciliationError(
            "Delivery Run Action Application Receipt generation 无效"
        )
    return {key: raw[key] for key in raw}


def _is_unbound_successor_of_receipt(
    record: Mapping[str, Any],
    action: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    if (
        action.get("status") not in _ACTIVE_ACTION_STATUSES
        or action.get("run_id") is not None
        or action.get("application_observed") is not False
    ):
        return False
    history = record.get("action_history")
    if not isinstance(history, list):
        return False
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        predecessor = entry.get("action")
        if not isinstance(predecessor, Mapping):
            continue
        if all(
            predecessor.get(key) == receipt.get(key)
            for key in ("action_id", "kind", "payload_digest")
        ) and predecessor.get("run_id") == receipt.get("run_id"):
            return True
    return False


def _require_active_action(record: Mapping[str, Any], action_id: str) -> dict[str, Any]:
    action = record.get("action")
    if not isinstance(action, dict) or action.get("action_id") != action_id:
        raise ActionReconciliationError("Lifecycle Action 不再是当前 Task Action")
    if action.get("status") not in _ACTIVE_ACTION_STATUSES:
        raise ActionReconciliationError("Lifecycle Action 已经收口")
    return action


def _require_executor(
    record: Mapping[str, Any], action_id: str, generation: int
) -> dict[str, Any]:
    executor = record.get("executor")
    if not isinstance(executor, dict):
        raise ActionReconciliationError("Executor ownership record 不存在")
    if (
        executor.get("action_id") != action_id
        or executor.get("generation") != generation
    ):
        raise ActionReconciliationError("Executor ownership/generation 不匹配")
    return executor


def _bounded_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TaskControlError("Lifecycle Action payload must be an object")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise TaskControlError("Lifecycle Action payload must be JSON") from error
    if len(encoded.encode("utf-8")) > 32 * 1024:
        raise TaskControlError("Lifecycle Action payload exceeds the size bound")
    loaded: object = json.loads(encoded)
    if not isinstance(
        loaded, dict
    ):  # pragma: no cover - Mapping input is object-shaped
        raise TaskControlError("Lifecycle Action payload must be an object")
    return loaded


def _payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the admission digest for a bounded JSON semantic payload."""

    return _payload_digest(_bounded_payload(payload))


def action_receipt_matches(
    state: Mapping[str, Any], action: Mapping[str, Any]
) -> bool:
    """Match the identity fields shared by Run receipts and Task Actions."""

    receipt = state.get("action_application_receipt")
    return isinstance(receipt, Mapping) and all(
        receipt.get(key) == action.get(key)
        for key in ("action_id", "kind", "payload_digest", "run_id")
    )


def _positive_integer(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise TaskControlError("Task Control generation must be a positive integer")
    return value


def _validate_runner_binding(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 16
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TaskControlError("Task Control Runner binding is invalid")
    return value


def _bounded_text(value: str) -> str:
    text = bounded_error(str(value))
    encoded = text.encode("utf-8")
    if len(encoded) <= 4096:
        return text
    return encoded[:4093].decode("utf-8", errors="ignore") + "..."


def _now() -> str:
    return datetime.now(UTC).isoformat()
