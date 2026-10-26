"""One locked Task -> Run location; all replacement decisions stay in Kernel."""


from contextlib import contextmanager
import fcntl
import json
import os
import sqlite3
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Callable, Iterator

from agent_run.config.paths import app_data_root
from agent_run.contracts.task import TaskKey

from agent_run.errors import KernelError
from agent_run.model import wire_json

if TYPE_CHECKING:
    from agent_run.storage.sqlite import Transaction


def read_repository_task(owner: "Transaction", task: TaskKey) -> sqlite3.Row | None:
    """Canonical staged Task metadata, without JSON publication or Run inference."""
    rows = owner.rows("SELECT * FROM tasks WHERE id=?", (task.fingerprint,), limit=1)
    if not rows:
        return None
    row = rows[0]
    if (row["repository"], row["repository_root"], row["parent_issue"]) != (
            task.repository, str(task.workspace), task.parent_number):
        raise KernelError("task_location_identity")
    return row


def task_path(task: TaskKey) -> Path:
    """Name the existing identity pointer without creating any directory."""
    return app_data_root() / "native-task-locations" / (task.fingerprint + ".json")


@contextmanager
def task_lock(task: TaskKey, *, wait: bool = True) -> Iterator[Callable[[], None]]:
    """Own the Task lock; the yielded callback transfers it to process lifetime."""
    path = task_path(task)
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


@contextmanager
def locked_task(task: TaskKey, *, wait: bool = True) -> Iterator[Path]:
    with task_lock(task, wait=wait):
        yield task_path(task)


def read_task(path: Path) -> tuple[str, Path] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        body = source.read(8193)
    if len(body) > 8192:
        raise KernelError("task_location_invalid")
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError) as error:
        raise KernelError("task_location_invalid") from error
    if (not isinstance(value, dict) or set(value) != {"run_id", "database"}
            or not isinstance(value["run_id"], str) or not value["run_id"]
            or not isinstance(value["database"], str) or not Path(value["database"]).is_absolute()):
        raise KernelError("task_location_invalid")
    return value["run_id"], Path(value["database"])


def read_task_bindings() -> Iterator[tuple[str, Path]]:
    """Enumerate valid pointers without letting unrelated damaged files block reads.

    A valid pointer to a damaged database is still yielded for caller diagnosis.
    Exact Task admission uses read_task directly and continues to fail closed.
    """
    try:
        with os.scandir(app_data_root() / "native-task-locations") as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    try:
                        binding = read_task(Path(entry.path))
                    except (KernelError, OSError):
                        continue
                    if binding is not None:
                        yield binding
    except FileNotFoundError:
        return


def bind_task(path: Path, run_id: str, database: Path) -> None:
    body = wire_json({"run_id": run_id, "database": str(database.resolve())})
    if len(body) > 8192:
        raise KernelError("task_location_invalid")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
        temporary = Path(target.name)
        try:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
