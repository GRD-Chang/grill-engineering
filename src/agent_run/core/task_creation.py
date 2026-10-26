"""Inactive repository Task creation on the existing Storage transaction owner.

No file creation, locator publication, credentials or Host dispatch. The caller
must already own a validated repository Storage once that protocol is enabled.
Until then only staged transaction tests call this seam. An uncertain request
is recovered by reconcile_repository_task with its ORIGINAL proposal Run ID;
reconciliation never allocates. There is no creation retry loop or second log.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
from time import monotonic
from typing import TYPE_CHECKING

from agent_run.contracts.identity import validate_identity_snapshot
from agent_run.contracts.prompt import validate_resources
from agent_run.contracts.task import TaskKey
from agent_run.errors import KernelError
from agent_run.model import CreationInputs, Requirements, RunSpec
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.save_order import WRITE_CONTENTION_SECONDS, remaining
from agent_run.storage.sqlite import Transaction

if TYPE_CHECKING:
    from agent_run.core.kernel import Kernel


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


def _task_row(owner: Transaction, task: TaskKey) -> sqlite3.Row | None:
    rows = owner.rows("SELECT * FROM tasks WHERE id=?", (task.fingerprint,), limit=1)
    if not rows:
        return None
    row = rows[0]
    if (row["repository"], row["repository_root"], row["parent_issue"]) != (
            task.repository, str(task.workspace), task.parent_number):
        raise KernelError("task_location_identity")
    return row


def ensure_idle_task(owner: Transaction, task: TaskKey) -> None:
    """Persist identity only; never erase a binding or manufacture a Run."""
    _writer(owner)
    if _task_row(owner, task) is None:
        owner.execute("INSERT INTO tasks(id,repository,repository_root,parent_issue) VALUES (?,?,?,?)",
            (task.fingerprint, task.repository, str(task.workspace), task.parent_number))


def _binding(kernel: "Kernel", owner: Transaction, task: TaskKey, run_id: str) -> RunTransaction:
    link = owner.one("SELECT * FROM task_runs WHERE run_id=?", (run_id,))
    if (link["task_id"], link["repository"], link["repository_root"], link["parent_issue"]) != (
            task.fingerprint, task.repository, str(task.workspace), task.parent_number):
        raise KernelError("task_location_identity")
    selected = RunTransaction(owner, run_id)
    run = selected.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if kernel._task(run) != task:
        raise KernelError("task_location_identity")
    # A Task binding is published only after a complete frozen creation. These
    # reads also validate material digest/ownership with the original read budget.
    profiles = kernel._profile_document(selected, 1)
    inputs = kernel._inputs(selected, run)
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


def _reconcile(kernel: "Kernel", owner: Transaction, task: TaskKey, run_id: str,
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
    _binding(kernel, owner, task, run_id)
    if row["current_run"] != run_id:
        _binding(kernel, owner, task, row["current_run"])
    return TaskRun(run_id, row["current_run"], created)


def reconcile_repository_task(kernel: "Kernel", task: TaskKey, proposal_run_id: str,
                              *, deadline: float | None = None) -> TaskRun:
    """Read-only business reconciliation under an original bounded writer.

    This explicit operation may use a fresh allowance on a later user request;
    automatic post-COMMIT readback below always passes the original deadline.
    No absence/error path creates a Run, even after the current binding advances.
    """
    if type(proposal_run_id) is not str or not proposal_run_id:
        raise KernelError("task_creation_identity")
    with kernel.storage.transaction(ordered=False, deadline=deadline) as owner:
        return _reconcile(kernel, owner, task, proposal_run_id)


def create_repository_task(kernel: "Kernel", task: TaskKey, spec: RunSpec,
                           requirements: Requirements) -> TaskRun:
    """Freeze and bind once; post-COMMIT uncertainty can only trigger readback.

    The proposal ID is chosen before entry and retained by the caller. After a
    lost reply callers MUST reconcile that ID, not submit a fresh proposal. A
    losing request may reuse the winner; its unallocated ID is not a new Run.
    """
    deadline = monotonic() + WRITE_CONTENTION_SECONDS
    if (type(spec.run_id) is not str or not spec.run_id or spec.inputs is None
            or spec.repository_root is None
            or TaskKey(Path(spec.repository_root), spec.repository, spec.parent_issue) != task):
        raise KernelError("task_creation_identity")
    profiles = kernel._creation_profiles(spec)
    policy_values = kernel._policy_values(spec.policy)
    spec = replace(spec, repository=task.repository, repository_root=str(task.workspace))
    result = None
    try:
        with kernel.storage.transaction(ordered=False, deadline=deadline) as owner:
            _writer(owner)
            ensure_idle_task(owner, task)
            row = _task_row(owner, task)
            assert row is not None
            # An ID previously committed for this Task is immutable, including
            # when a legitimate successor has since become current.
            if owner.rows("SELECT id FROM runs WHERE id=?", (spec.run_id,), limit=1):
                result = _reconcile(kernel, owner, task, spec.run_id)
            elif row["current_run"] is not None and not kernel._replaceable_in(
                    _binding(kernel, owner, task, row["current_run"]), task, row["current_run"]):
                result = TaskRun(row["current_run"], row["current_run"], False)
            else:
                kernel._create_run(owner, spec, profiles, requirements=requirements,
                    scoped=True, policy_values=policy_values)
                owner.execute("INSERT INTO task_runs VALUES (?,?,?,?,?)",
                    (task.fingerprint, spec.run_id, task.repository, str(task.workspace), task.parent_number))
                changed = owner.execute("UPDATE tasks SET current_run=? WHERE id=? AND current_run IS ?",
                    (spec.run_id, task.fingerprint, row["current_run"]))
                if changed.rowcount != 1:
                    raise KernelError("task_location_identity")
                _binding(kernel, owner, task, spec.run_id)
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
        with kernel.storage.transaction(ordered=False, deadline=deadline) as owner:
            return _reconcile(kernel, owner, task, result.run_id, created=result.created)
