"""Canonical Task relations and the existing physical admission mutex."""


from contextlib import contextmanager
import fcntl
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

from agent_run.config.paths import app_data_root
from agent_run.contracts.task import TaskKey

from agent_run.errors import KernelError

if TYPE_CHECKING:
    from agent_run.storage.sqlite import Transaction


def read_repository_task(owner: "Transaction", task: TaskKey) -> sqlite3.Row | None:
    """Canonical Task metadata, without JSON publication or Run inference."""
    try:
        rows = owner.rows("SELECT * FROM tasks WHERE id=?", (task.fingerprint,), limit=1)
    except (OSError, sqlite3.Error) as error:
        raise KernelError("task_location_unavailable", "Canonical Task observation is unavailable") from error
    if not rows:
        return None
    row = rows[0]
    if (row["repository"], row["repository_root"], row["parent_issue"]) != (
            task.repository, str(task.workspace), task.parent_number):
        raise KernelError("task_location_identity")
    if row["current_run"] is None and owner.rows(
            "SELECT run_id FROM task_runs WHERE task_id=? LIMIT 1", (task.fingerprint,), limit=1):
        raise KernelError("task_location_identity", "An existing Run cannot become an idle Task")
    return row


def run_is_current(owner: "Transaction") -> bool:
    """Recheck publication from facts in the original selected snapshot.

    Only canonical Task and Run relations choose the current binding. Display
    bodies, directory names and external notices never select or authorize it.
    """
    from agent_run.storage.run_scope import run_statement, transaction_scope
    selected = transaction_scope(owner)
    run = owner.one(*run_statement(owner, "SELECT id,repository,repository_root,parent_issue FROM runs WHERE id=?"))
    task = TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])
    row = read_repository_task(owner, task)
    links = owner.rows("SELECT task_id,repository,repository_root,parent_issue FROM task_runs WHERE run_id=?",
        (selected.run_id,), limit=1)
    if (row is None or len(links) != 1
            or tuple(links[0]) != (task.fingerprint, task.repository, str(task.workspace), task.parent_number)):
        raise KernelError("task_location_identity")
    return bool(row["current_run"] == selected.run_id)


def task_path(task: TaskKey) -> Path:
    """Name retired-publication residue; never create or decode a pointer."""
    return app_data_root() / "native-task-locations" / (task.fingerprint + ".json")


def refuse_retired_task(task: TaskKey) -> None:
    """Retired publication can only refuse admission; its contents select nothing."""
    path = task_path(task)
    if path.exists() or path.is_symlink():
        from agent_run.storage.preflight import UnsupportedState
        raise UnsupportedState(path, "retired_task_publication")


@contextmanager
def task_lock(task: TaskKey, *, wait: bool = True) -> Iterator[Callable[[], None]]:
    """Own the Task lock; the yielded callback transfers it to process lifetime."""
    path = task_path(task)
    refuse_retired_task(task)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    process_owned = False
    def retain_until_exit() -> None:
        nonlocal process_owned
        process_owned = True
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise KernelError("task_admission_busy") from error
        yield retain_until_exit
    finally:
        if not process_owned:
            os.close(descriptor)
