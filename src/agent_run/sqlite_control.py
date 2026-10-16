"""Adapt bounded control views to explicit SQL relations.

Callers own the connection, transaction and lifecycle authorization. These
functions never acquire a lock, open a connection or invoke external work.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import sqlite3
from typing import Any

from agent_run.sqlite_control_schema import COLUMNS, FOREIGN_KEYS, INDEXES, KEYS, SCHEMA, TABLES


__all__ = ["COLUMNS", "FOREIGN_KEYS", "INDEXES", "KEYS", "SCHEMA", "TABLES", "MAX_CONTROL_BYTES",
           "load", "save", "observe_application", "find_executor", "save_executor", "finish_worker"]


_TASK_FIELDS = {"protocol", "task", "run_id", "run_state_dir", "next_generation",
                "action", "action_history", "executor", "updated_at"}
_ACTION_FIELDS = COLUMNS["control_actions"][1:-2]
_EXECUTOR_FIELDS = COLUMNS["control_executors"][1:-1]
_ACTION_KEYS = set(_ACTION_FIELDS) | {"payload", "target_executor"}
_EXECUTOR_KEYS = set(_EXECUTOR_FIELDS) | {"worker", "workers"}
_WORKER_KEYS = {"pid", "process_start_token", "started_at"}
_MAX_PAYLOAD_BYTES = 32 * 1024
MAX_CONTROL_BYTES = 256 * 1024


def _bounded_rows(
    connection: sqlite3.Connection, columns: tuple[str, ...], source: str,
    parameters: tuple[Any, ...], *, limit: int,
) -> sqlite3.Cursor:
    """Prove byte size before fetching text in the caller's SQL snapshot."""
    byte_size = " + ".join(f"coalesce(length(CAST({column} AS BLOB)), 0)" for column in columns)
    remaining = MAX_CONTROL_BYTES
    sizes = connection.execute(f"SELECT {byte_size} FROM {source} LIMIT ?", (*parameters, limit))
    found = False
    for (size,) in sizes:
        found = True
        remaining -= size
        if remaining < 0:
            raise ValueError("Control record exceeds its byte limit")
    if not found:
        # The exhausted cursor already proves this relation is empty in the
        # caller's snapshot. No text projection or second SELECT is needed.
        return sizes
    return connection.execute(f"SELECT {', '.join(columns)} FROM {source} LIMIT ?", (*parameters, limit))


def _check_view_size(value: Mapping[str, Any]) -> None:
    if len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")) > MAX_CONTROL_BYTES:
        raise ValueError("Control record exceeds its byte limit")


def _known(value: object, allowed: set[str], subject: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Control {subject} must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unsupported Control {subject} fields: {sorted(map(str, unknown))}")
    return value


def _boolean(value: object) -> bool:
    if type(value) not in {int, bool} or value not in (0, 1):
        raise ValueError("Control boolean must be 0 or 1")
    return bool(value)


def _executor_key(value: Mapping[str, Any]) -> tuple[str, int]:
    action_id, generation = value.get("action_id"), value.get("generation")
    if not isinstance(action_id, str) or not action_id or type(generation) is not int or generation <= 0:
        raise ValueError("Control Executor identity is invalid")
    return action_id, generation


def _workers(executor: Mapping[str, Any]) -> tuple[int, list[Mapping[str, Any]]]:
    worker, workers = executor.get("worker"), executor.get("workers")
    if worker is not None and workers is not None:
        raise ValueError("Control Executor has conflicting Worker bindings")
    if workers is not None:
        if not isinstance(workers, list) or not workers:
            raise ValueError("Control Workers must be a nonempty list")
        mode, bindings = 2, workers
    else:
        mode, bindings = (1, [worker]) if worker is not None else (0, [])
    result = []
    seen = set()
    for binding in bindings:
        binding = _known(binding, _WORKER_KEYS, "Worker")
        pid, token = binding.get("pid"), binding.get("process_start_token")
        if type(pid) is not int or pid <= 0 or pid in seen or not isinstance(token, str) or not token:
            raise ValueError("Control Worker identity is invalid")
        seen.add(pid)
        result.append(binding)
    return mode, result


def _payload(action: Mapping[str, Any]) -> tuple[str, int]:
    value = action.get("payload")
    if not isinstance(value, Mapping):
        raise ValueError("Control Action requires its immutable payload")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        raw = encoded.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("Control Action payload must be UTF-8 JSON") from error
    if len(raw) > _MAX_PAYLOAD_BYTES or hashlib.sha256(raw).hexdigest() != action.get("payload_digest"):
        raise ValueError("Control Action payload size or digest is invalid")
    return encoded, len(raw)


def _put(connection: sqlite3.Connection, table: str, values: tuple[Any, ...]) -> None:
    columns, keys = COLUMNS[table], KEYS[table]
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column not in keys)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}", values,
    )


def find_executor(
    connection: sqlite3.Connection, fingerprint: str, action_id: str, generation: int,
) -> dict[str, Any] | None:
    """Read one exact Executor, including an old Stop target, without authority."""
    row = _bounded_rows(
        connection, COLUMNS["control_executors"][1:],
        "control_executors WHERE task_id=? AND action_id=? AND generation=?",
        (fingerprint, action_id, generation), limit=1,
    ).fetchone()
    if row is None:
        return None
    executor = dict(zip(_EXECUTOR_FIELDS, row[:-1], strict=True))
    for field in ("runner_binding", "ticket_resume_closed"):
        if executor[field] is None:
            del executor[field]
    if "ticket_resume_closed" in executor:
        executor["ticket_resume_closed"] = _boolean(executor["ticket_resume_closed"])
    workers = []
    for pid, token, started_at in _bounded_rows(
        connection, ("pid", "process_start_token", "started_at"),
        "control_workers WHERE task_id=? AND action_id=? AND generation=? ORDER BY position",
        (fingerprint, action_id, generation), limit=4097,
    ):
        binding = {"pid": pid, "process_start_token": token}
        if started_at is not None:
            binding["started_at"] = started_at
        workers.append(binding)
    mode = row[-1]
    if len(workers) > 4096 or mode not in (0, 1, 2) or (mode == 0 and workers) or (mode == 1 and len(workers) != 1) or (mode == 2 and not workers):
        raise ValueError("Control Executor Worker relations are invalid")
    if mode == 1:
        executor["worker"] = workers[0]
    elif mode == 2:
        executor["workers"] = workers
    _check_view_size(executor)
    return executor


def load(
    connection: sqlite3.Connection, fingerprint: str, identity: Mapping[str, object],
) -> dict[str, Any] | None:
    """Build one bounded view; repeated Executor references share one dict."""
    row = _bounded_rows(
        connection, COLUMNS["control_tasks"][1:], "control_tasks WHERE task_id=?",
        (fingerprint,), limit=1,
    ).fetchone()
    if row is None:
        return None
    (protocol, workspace, repository, parent, run_id, state_dir, next_generation,
     current_action, executor_action, executor_generation, updated_at) = row
    task_identity = {"workspace": workspace, "repository": repository, "parent": parent}
    if task_identity != identity:
        raise ValueError("Control Task identity does not match the checkout")
    executors: dict[tuple[str, int], dict[str, Any]] = {}

    def executor_for(action_id: str | None, generation: int | None) -> dict[str, Any] | None:
        if action_id is None and generation is None:
            return None
        if not isinstance(action_id, str) or type(generation) is not int:
            raise ValueError("Control Executor reference is incomplete")
        key = action_id, generation
        if key not in executors:
            value = find_executor(connection, fingerprint, *key)
            if value is None:
                raise ValueError("Control Executor reference is missing")
            executors[key] = value
        return executors[key]

    actions: dict[str, dict[str, Any]] = {}
    for values in _bounded_rows(
        connection, COLUMNS["control_actions"][1:], "control_actions WHERE task_id=?",
        (fingerprint,), limit=6,
    ):
        action = dict(zip(_ACTION_FIELDS, values[:-2], strict=True))
        action["application_observed"] = _boolean(action["application_observed"])
        approval = action.pop("final_approval_operation")
        if approval is not None:
            action["final_approval_operation"] = _boolean(approval)
        target = executor_for(values[-2], values[-1])
        if target is not None:
            action["target_executor"] = target
        material = connection.execute(
            "SELECT substr(CAST(value AS BLOB), 1, ?), byte_length, length(CAST(value AS BLOB)), "
            "typeof(value), typeof(byte_length) FROM control_payloads WHERE task_id=? AND action_id=?",
            (_MAX_PAYLOAD_BYTES + 1, fingerprint, action["action_id"]),
        ).fetchone()
        if material is None or material[3:] != ("text", "integer"):
            raise ValueError("Control Action payload material is missing or invalid")
        raw, declared, actual = material[:3]
        if declared != actual or actual > _MAX_PAYLOAD_BYTES or hashlib.sha256(raw).hexdigest() != action["payload_digest"]:
            raise ValueError("Control Action payload material size or digest is invalid")
        try:
            action["payload"] = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise ValueError("Control Action payload material is invalid JSON") from error
        if not isinstance(action["payload"], dict):
            raise ValueError("Control Action payload material must be an object")
        actions[action["action_id"]] = action
    if len(actions) > 5:
        raise ValueError("Control Action history exceeds its bound")

    def action_for(action_id: str | None) -> dict[str, Any] | None:
        if action_id is None:
            return None
        if action_id not in actions:
            raise ValueError("Control Action reference is missing")
        return actions[action_id]

    history = []
    for old_run, old_dir, action_id, old_executor, old_generation in _bounded_rows(
        connection, ("run_id", "state_dir", "action_id", "executor_action_id", "executor_generation"),
        "control_action_history WHERE task_id=? ORDER BY position", (fingerprint,), limit=5,
    ):
        entry = {"run_id": old_run, "action": action_for(action_id), "executor": executor_for(old_executor, old_generation)}
        if old_dir is not None:
            entry["state_dir"] = old_dir
        history.append(entry)
    if len(history) > 4:
        raise ValueError("Control Action history exceeds its bound")
    result = {"protocol": protocol, "task": task_identity, "run_id": run_id, "run_state_dir": state_dir,
              "next_generation": next_generation, "action": action_for(current_action), "action_history": history,
              "executor": executor_for(executor_action, executor_generation), "updated_at": updated_at}
    _check_view_size(result)
    return result


def _save_executor(connection: sqlite3.Connection, fingerprint: str, executor: Mapping[str, Any]) -> None:
    _check_view_size(executor)
    mode, workers = _workers(executor)
    _put(connection, "control_executors", (fingerprint, *(executor.get(field) for field in _EXECUTOR_FIELDS), mode))
    action_id, generation = _executor_key(executor)
    connection.execute("DELETE FROM control_workers WHERE task_id=? AND action_id=? AND generation=?",
                       (fingerprint, action_id, generation))
    connection.executemany(
        "INSERT INTO control_workers(task_id, action_id, generation, position, pid, process_start_token, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(fingerprint, action_id, generation, position, worker["pid"], worker["process_start_token"], worker.get("started_at"))
         for position, worker in enumerate(workers)],
    )


def save(connection: sqlite3.Connection, fingerprint: str, record: Mapping[str, Any]) -> None:
    """Save a Task view while rejecting contradictory aliases before writing."""
    _known(record, _TASK_FIELDS, "Task")
    _check_view_size(record)
    identity = _known(record.get("task"), {"workspace", "repository", "parent"}, "Task identity")
    history = record.get("action_history", [])
    if not isinstance(history, list) or len(history) > 4:
        raise ValueError("Control Action history is invalid")
    actions: dict[str, Mapping[str, Any]] = {}
    executors: dict[tuple[str, int], Mapping[str, Any]] = {}
    payloads: dict[str, tuple[str, int]] = {}

    def add_executor(value: object) -> tuple[str | None, int | None]:
        if value is None:
            return None, None
        executor = _known(value, _EXECUTOR_KEYS, "Executor")
        key = _executor_key(executor)
        _workers(executor)
        if key in executors and executors[key] != executor:
            raise ValueError("Control Executor references have contradictory facts")
        executors[key] = executor
        return key

    def add_action(value: object) -> str | None:
        if value is None:
            return None
        action = _known(value, _ACTION_KEYS, "Action")
        action_id = action.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("Control Action identity is invalid")
        if action_id in actions and actions[action_id] != action:
            raise ValueError("Control Action references have contradictory facts")
        actions[action_id] = action
        payloads[action_id] = _payload(action)
        _boolean(action.get("application_observed"))
        add_executor(action.get("target_executor"))
        return action_id

    current_action = add_action(record.get("action"))
    current_executor = add_executor(record.get("executor"))
    history_rows = []
    for position, raw_entry in enumerate(history):
        entry = _known(raw_entry, {"run_id", "state_dir", "action", "executor"}, "history")
        action_id = add_action(entry.get("action"))
        if action_id is None:
            raise ValueError("Control history requires its Action")
        history_rows.append((fingerprint, position, entry.get("run_id"), entry.get("state_dir"),
                             action_id, *add_executor(entry.get("executor"))))
    previous_identity = _bounded_rows(
        connection, ("workspace", "repository", "parent_number"), "control_tasks WHERE task_id=?",
        (fingerprint,), limit=1,
    ).fetchone()
    task_values = identity.get("workspace"), identity.get("repository"), identity.get("parent")
    if previous_identity is not None and previous_identity != task_values:
        raise ValueError("Control Task identity cannot change")
    for action_id, material in payloads.items():
        existing = _bounded_rows(
            connection, ("value", "byte_length"), "control_payloads WHERE task_id=? AND action_id=?",
            (fingerprint, action_id), limit=1,
        ).fetchone()
        if existing is not None and existing != material:
            raise ValueError("Control Action payload material is immutable")
    _put(connection, "control_tasks", (
        fingerprint, record.get("protocol"), *task_values, record.get("run_id"), record.get("run_state_dir"),
        record.get("next_generation", 1), current_action, *current_executor, record.get("updated_at"),
    ))
    for executor in executors.values():
        _save_executor(connection, fingerprint, executor)
    for action_id, action in actions.items():
        target = action.get("target_executor")
        target_key = _executor_key(target) if isinstance(target, Mapping) else (None, None)
        _put(connection, "control_actions", (fingerprint, *(action.get(field) for field in _ACTION_FIELDS), *target_key))
        connection.execute("INSERT OR IGNORE INTO control_payloads(task_id, action_id, value, byte_length) VALUES (?, ?, ?, ?)",
                           (fingerprint, action_id, *payloads[action_id]))
    connection.execute("DELETE FROM control_action_history WHERE task_id=?", (fingerprint,))
    connection.executemany("INSERT INTO control_action_history VALUES (?, ?, ?, ?, ?, ?, ?)", history_rows)
    action_filter = f" AND action_id NOT IN ({', '.join('?' for _ in actions)})" if actions else ""
    connection.execute(f"DELETE FROM control_actions WHERE task_id=?{action_filter}", (fingerprint, *actions))
    executor_filter = (f" AND (action_id, generation) NOT IN ({', '.join('(?, ?)' for _ in executors)})"
                       if executors else "")
    connection.execute(f"DELETE FROM control_executors WHERE task_id=?{executor_filter}",
                       (fingerprint, *(part for key in executors for part in key)))


def save_executor(connection: sqlite3.Connection, fingerprint: str, executor: Mapping[str, Any]) -> None:
    """Update an existing exact Executor without changing current ownership."""
    _known(executor, _EXECUTOR_KEYS, "Executor")
    key = _executor_key(executor)
    if find_executor(connection, fingerprint, *key) is None:
        raise ValueError("Control Executor does not exist")
    _save_executor(connection, fingerprint, executor)


def finish_worker(
    connection: sqlite3.Connection, fingerprint: str, action_id: str, generation: int,
    pid: int, process_start_token: str,
) -> dict[str, Any]:
    """Remove only an exact Worker binding, including after Stop revoked work."""
    if type(pid) is not int or pid <= 0 or not isinstance(process_start_token, str) or not process_start_token:
        raise ValueError("Control Worker completion identity is invalid")
    executor = find_executor(connection, fingerprint, action_id, generation)
    if executor is None:
        raise ValueError("Control Executor does not exist")
    mode, workers = _workers(executor)
    matching = [worker for worker in workers if worker["pid"] == pid]
    if not matching:
        return executor
    if matching[0]["process_start_token"] != process_start_token:
        raise ValueError("Control Worker completion binding does not match")
    remaining = [worker for worker in workers if worker["pid"] != pid]
    executor.pop("worker", None)
    executor.pop("workers", None)
    if remaining:
        executor["workers" if mode == 2 else "worker"] = remaining if mode == 2 else remaining[0]
    _save_executor(connection, fingerprint, executor)
    return executor


def observe_application(connection: sqlite3.Connection, receipt: Mapping[str, Any], state_root: str) -> None:
    """Recheck and bind an existing Action with its business commit, once."""
    _known(receipt, {"protocol", "action_id", "kind", "payload_digest", "run_id", "executor_generation",
                     "applied_at", "qualified_ticket_number"},
           "application receipt")
    run_id, action_id, generation = receipt.get("run_id"), receipt.get("action_id"), receipt.get("executor_generation")
    if (not isinstance(run_id, str) or not run_id or not isinstance(action_id, str) or not action_id
            or type(generation) is not int or generation <= 0 or type(receipt.get("protocol")) is not int
            or not isinstance(receipt.get("kind"), str) or not isinstance(receipt.get("payload_digest"), str)
            or not isinstance(state_root, str) or not state_root
            or ("applied_at" in receipt and not isinstance(receipt["applied_at"], str))
            or ("qualified_ticket_number" in receipt and (
                type(receipt["qualified_ticket_number"]) is not int
                or receipt["qualified_ticket_number"] <= 0
                or receipt.get("kind") not in {"resume", "requeue"}
            ))):
        raise ValueError("Control application receipt identity is invalid")
    rows = _bounded_rows(
        connection,
        ("t.task_id", "t.protocol", "t.repository", "t.parent_number", "t.run_id", "t.run_state_dir",
         "t.current_action_id", "t.current_executor_action_id", "t.current_executor_generation", "a.kind",
         "a.payload_digest", "a.run_id", "a.executor_generation", "a.status", "a.application_observed",
         "e.run_id", "e.status", "t.updated_at"),
        "control_actions a JOIN control_tasks t ON t.task_id=a.task_id "
        "LEFT JOIN control_executors e ON e.task_id=t.task_id AND e.action_id=t.current_executor_action_id "
        "AND e.generation=t.current_executor_generation WHERE a.action_id=?", (action_id,), limit=2,
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("Control application requires one existing exact Action")
    (task_id, protocol, repository, parent, task_run, directory, current_action, executor_action, executor_generation,
     kind, digest, action_run, action_generation, action_status, applied, executor_run, executor_status, updated_at) = rows[0]
    run = connection.execute("SELECT repository,parent_number FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if (run is None or not isinstance(run[0], str) or run[0].lower() != repository.lower() or run[1] != parent
            or receipt.get("protocol") != protocol or receipt.get("kind") != kind or receipt.get("payload_digest") != digest
            or current_action != action_id or executor_action != action_id
            or executor_generation != generation or action_generation != generation
            or action_status not in {"accepted", "applying", "completed", "failed"}
            or executor_status not in {"starting", "running", "exited", "absent"} or directory not in {None, state_root}
            or any(binding not in {None, run_id} for binding in (task_run, action_run, executor_run))):
        raise ValueError("Control application receipt does not match Task/Action/Executor/Run ownership")
    if _boolean(applied) and task_run == action_run == executor_run == run_id and directory == state_root:
        return
    if action_status not in {"accepted", "applying"} or executor_status not in {"starting", "running"}:
        raise ValueError("Control application requires active Action and Executor ownership")
    connection.execute("UPDATE control_tasks SET run_id=?,run_state_dir=?,updated_at=? WHERE task_id=?",
                       (run_id, state_root, receipt.get("applied_at") or updated_at, task_id))
    connection.execute("UPDATE control_actions SET run_id=?,application_observed=1,status='applying' WHERE task_id=? AND action_id=?",
                       (run_id, task_id, action_id))
    connection.execute("UPDATE control_executors SET run_id=? WHERE task_id=? AND action_id=? AND generation=?",
                       (run_id, task_id, action_id, generation))
