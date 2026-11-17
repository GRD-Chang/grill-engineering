"""Atomic repository Task creation on the existing Storage transaction owner.

No file creation, locator publication, credentials or Host dispatch. The caller
must already own a validated repository metadata Storage. An uncertain request
is recovered by reconcile_repository_task with its ORIGINAL proposal Run ID;
reconciliation never allocates. There is no creation retry loop or second log.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
import json
import time
from time import monotonic
from typing import Callable

from agent_run.contracts.identity import validate_identity_snapshot
from agent_run.contracts.prompt import validate_resources
from agent_run.contracts.task import TaskKey
from agent_run.errors import KernelError
from agent_run.model import CreationInputs, Requirements, RunSpec
from agent_run.storage.run_scope import RunTransaction, run_statement, transaction_scope
from agent_run.storage.save_order import WRITE_CONTENTION_SECONDS, remaining
from agent_run.storage.sqlite import Storage, Transaction
from agent_run.core.run_creation import creation_profiles, create_run, encode_policy
from agent_run.storage.tasks import read_repository_task as _task_row

@dataclass(frozen=True)
class TaskRun:
    """Selected immutable Run and independently observed current Task binding."""
    run_id: str
    current_run: str
    created: bool


def _writer(owner: Transaction) -> None:
    # Task metadata precedes any Run selection. The global transaction_scope
    # guard correctly refuses a bare owner on the repository layout; it must
    # not be used to authorize this distinct, bounded metadata seam.
    if type(owner) is not Transaction or not owner.connection.in_transaction:
        raise KernelError("task_creation_transaction_required")
    if owner.failed:
        raise KernelError("transaction_aborted")
    if (owner.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or owner.execute("PRAGMA query_only").fetchone()[0] != 0):
        raise KernelError("task_creation_transaction_required")
    # Inspect only bounded schema metadata; no arbitrary Run is inferred, and
    # subsequent business reads/writes still require an explicit RunTransaction.
    columns = owner.execute("PRAGMA table_info(material)").fetchmany(32)
    primary = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
    task_columns = owner.execute("PRAGMA table_info(tasks)").fetchmany(16)
    link_columns = owner.execute("PRAGMA table_info(task_runs)").fetchmany(16)
    if (primary != ["run_id", "id"]
            or [row["name"] for row in task_columns] != ["id", "repository", "repository_root", "parent_issue", "current_run"]
            or [row["name"] for row in link_columns] != ["task_id", "run_id", "repository", "repository_root", "parent_issue"]):
        raise KernelError("task_creation_schema_required")


def ensure_idle_task(owner: Transaction, task: TaskKey) -> None:
    """Persist identity only; never erase a binding or manufacture a Run."""
    _writer(owner)
    if _task_row(owner, task) is None:
        owner.execute("INSERT INTO tasks(id,repository,repository_root,parent_issue) VALUES (?,?,?,?)",
            (task.fingerprint, task.repository, str(task.workspace), task.parent_number))


def capture_binding(owner: Transaction, task: TaskKey, run_id: str) -> RunTransaction:
    link = owner.one("SELECT * FROM task_runs WHERE run_id=?", (run_id,))
    if (link["task_id"], link["repository"], link["repository_root"], link["parent_issue"]) != (
            task.fingerprint, task.repository, str(task.workspace), task.parent_number):
        raise KernelError("task_location_identity")
    selected = RunTransaction(owner, run_id)
    run = selected.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if task_from_run(run) != task:
        raise KernelError("task_location_identity")
    # A Task binding is published only after a complete frozen creation. These
    # reads also validate material digest/ownership with the original read budget.
    profile = selected.one(*run_statement(selected, 'SELECT * FROM profile_revisions WHERE id=1 AND run_id=?', (), run_last=True))
    profiles = dict(json.loads(selected.get(profile["material_id"], profile["subject"])))
    records = selected.rows("SELECT * FROM run_inputs WHERE run_id=?", (run_id,))
    inputs = {} if not records else dict(json.loads(selected.get(records[0]["material_id"], records[0]["subject"])))
    if (run["source_sequence"] is None or not inputs
            or not selected.rows("SELECT id FROM subjects WHERE run_id=? LIMIT 1", (run_id,))):
        raise KernelError("task_creation_incomplete")
    try:
        frozen = CreationInputs(profiles=profiles, **inputs)
    except (TypeError, ValueError) as error:
        raise KernelError("task_creation_incomplete") from error
    # Validate frozen references only, never resolve current personal defaults.
    validate_identity_snapshot(frozen.worker_identity)
    validate_identity_snapshot(frozen.runner_identity)
    validate_resources(dict(frozen.prompt_resources))
    return selected


def _reconcile(owner: Transaction, task: TaskKey, run_id: str,
               *, created: bool = False) -> TaskRun:
    _writer(owner)
    row = _task_row(owner, task)
    links = owner.rows("SELECT task_id FROM task_runs WHERE run_id=?", (run_id,), limit=1)
    # A recorded foreign owner is a known identity conflict even when the
    # requested Task is absent or idle. It is not an uncertain commit outcome.
    if links and links[0]["task_id"] != task.fingerprint:
        raise KernelError("task_location_identity")
    # A missing proposal is NOT proof that a failed COMMIT rolled back. Refuse
    # rather than create, replace the winner, or adopt an unbound header.
    if row is None or row["current_run"] is None or not links:
        raise KernelError("task_creation_unknown")
    capture_binding(owner, task, run_id)
    if row["current_run"] != run_id:
        capture_binding(owner, task, row["current_run"])
    return TaskRun(run_id, row["current_run"], created)


def reconcile_repository_task(storage: Storage, task: TaskKey, proposal_run_id: str,
                              *, deadline: float | None = None) -> TaskRun:
    """Read-only business reconciliation under an original bounded writer.

    This explicit operation may use a fresh allowance on a later user request;
    automatic post-COMMIT readback below always passes the original deadline.
    No absence/error path creates a Run, even after the current binding advances.
    """
    if type(proposal_run_id) is not str or not proposal_run_id:
        raise KernelError("task_creation_identity")
    with storage.transaction(ordered=False, deadline=deadline) as owner:
        return _reconcile(owner, task, proposal_run_id)


def create_repository_task(storage: Storage, task: TaskKey, spec: RunSpec,
                           requirements: Requirements, *, clock: Callable[[], float] | None = None) -> TaskRun:
    """Freeze and bind once; post-COMMIT uncertainty can only trigger readback.

    The proposal ID is chosen before entry and retained by the caller. After a
    lost reply callers MUST reconcile that ID, not submit a fresh proposal. A
    losing request may reuse the winner; its unallocated ID is not a new Run.
    """
    clock = time.time if clock is None else clock
    deadline = monotonic() + WRITE_CONTENTION_SECONDS
    if (type(spec.run_id) is not str or not spec.run_id or spec.inputs is None
            or spec.repository_root is None
            or TaskKey(Path(spec.repository_root), spec.repository, spec.parent_issue) != task):
        raise KernelError("task_creation_identity")
    profiles = creation_profiles(spec)
    policy_values = encode_policy(spec.policy)
    spec = replace(spec, repository=task.repository, repository_root=str(task.workspace))
    result = None
    try:
        with storage.transaction(ordered=False, deadline=deadline) as owner:
            _writer(owner)
            ensure_idle_task(owner, task)
            row = _task_row(owner, task)
            assert row is not None
            # An ID previously committed for this Task is immutable, including
            # when a legitimate successor has since become current.
            if owner.rows("SELECT id FROM runs WHERE id=?", (spec.run_id,), limit=1):
                result = _reconcile(owner, task, spec.run_id)
            elif row["current_run"] is not None and not replaceable_in(
                    capture_binding(owner, task, row["current_run"]), task, row["current_run"]):
                result = TaskRun(row["current_run"], row["current_run"], False)
            else:
                create_run(owner, spec, profiles, requirements=requirements,
                    policy_values=policy_values, clock=clock)
                owner.execute("INSERT INTO task_runs VALUES (?,?,?,?,?)",
                    (task.fingerprint, spec.run_id, task.repository, str(task.workspace), task.parent_number))
                changed = owner.execute("UPDATE tasks SET current_run=? WHERE id=? AND current_run IS ?",
                    (spec.run_id, task.fingerprint, row["current_run"]))
                if changed.rowcount != 1:
                    raise KernelError("task_location_identity")
                capture_binding(owner, task, spec.run_id)
                result = TaskRun(spec.run_id, spec.run_id, True)
            remaining(deadline)
        assert result is not None
        return result
    except (OSError, sqlite3.Error) as error:
        if result is None or isinstance(error, InterruptedError):
            raise
        # The body completed, but commit/close may have succeeded before an I/O
        # exception. Never rerun it. Storage receives exactly the old deadline.
        remaining(deadline)
        with storage.transaction(ordered=False, deadline=deadline) as owner:
            return _reconcile(owner, task, result.run_id, created=result.created)


def task_from_run(run: sqlite3.Row) -> TaskKey:
    if run["repository_root"] is None:
        raise KernelError("task_workspace_unknown")
    return TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])


def replaceable_in(tx: Transaction, task: TaskKey, run_id: str) -> bool:
    """The original predicate, also usable inside an atomic Task writer."""
    selected = transaction_scope(tx)
    if selected.run_id != run_id:
        raise KernelError("task_location_identity")
    run = tx.one(*run_statement(tx, "SELECT * FROM runs WHERE id=?", (), run_last=True))
    if run["id"] != run_id or task_from_run(run) != task:
        raise KernelError("task_location_identity")
    actions = tx.rows(*run_statement(tx, "SELECT * FROM actions WHERE kind='abandon' AND run_id=?", (), run_last=True), limit=1)
    abandoned = actions[0] if actions else None
    completed_abandonment = bool(abandoned is not None and abandoned["status"] == "complete")
    return bool(run['executor'] is None and (completed_abandonment or not tx.rows(*run_statement(tx, "SELECT id FROM subjects WHERE phase!='complete' AND run_id=? LIMIT 1", (), run_last=True))) and (completed_abandonment or not tx.rows(*run_statement(tx, "SELECT id FROM final_target WHERE run_id=? AND id NOT IN (SELECT id FROM subjects WHERE run_id=? AND kind='final_run' AND phase='complete') LIMIT 1", (run_id,)))) and (not tx.rows(*run_statement(tx, "SELECT id FROM leases WHERE run_id=? LIMIT 1", (), run_last=True))) and (not tx.rows(*run_statement(tx, "SELECT scope_id FROM waits WHERE kind='agent_preparation' AND run_id=? LIMIT 1", (), run_last=True))) and (not tx.rows(*run_statement(tx, "SELECT owner FROM retired_executors WHERE status!='exited' AND run_id=? LIMIT 1", (), run_last=True))) and (not tx.rows(*run_statement(tx, "SELECT owner FROM hosts WHERE (status!='exited' OR usage_released=0) AND run_id=? LIMIT 1", (), run_last=True))) and (not tx.rows(*run_statement(tx, "SELECT id FROM effects WHERE status IN ('prepared','sent','observed') AND operation!='issue_link' AND run_id=? LIMIT 1", (), run_last=True))) and (completed_abandonment or not tx.rows(*run_statement(tx, "SELECT subject FROM resource_cleanup WHERE state!='complete' AND run_id=? LIMIT 1", (), run_last=True))))
