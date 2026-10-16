"""控制事实快照与安排阶段的真实 SQL fixture 写入。"""

from __future__ import annotations

import json
import fcntl
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_run.state_limits import MAX_QUERY_BYTES
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.task_control import TaskControlStore, TaskKey


def control_state_text(control: TaskControlStore, task: TaskKey) -> str:
    """稳定编码持久事实，不读取旧控制 JSON 文件。"""
    return json.dumps(control.load(task), ensure_ascii=False, sort_keys=True)


def snapshot_control(control: TaskControlStore, task: TaskKey) -> bytes:
    return control_state_text(control, task).encode("utf-8")


@contextmanager
def hold_control_lock(control: TaskControlStore, task: TaskKey) -> Iterator[None]:
    """只模拟另一参与者持有真实 flock，不进入当前线程的可重入 SQL 事务。"""
    control.directory.mkdir(parents=True, exist_ok=True)
    path = control.directory / f".{task.fingerprint}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def persist_control_fixture(
    control: TaskControlStore, task: TaskKey, record: Mapping[str, Any],
) -> None:
    """仅用于 arrange；被测 Action 必须调用真实生产入口。"""
    from agent_run import sqlite_control

    repository = SQLiteStateRepository(control.root, max_bytes=MAX_QUERY_BYTES)
    with repository.atomic():
        sqlite_control.save(repository._local.connection, task.fingerprint, record)


def delete_control_fixture(control: TaskControlStore, task: TaskKey) -> None:
    """构造控制记录缺失；不移除仍然存在的业务 Run。"""
    with control._repository.atomic():
        control._repository._local.connection.execute(
            "DELETE FROM control_tasks WHERE task_id=?", (task.fingerprint,),
        )


def corrupt_control_fixture(control: TaskControlStore, task: TaskKey) -> None:
    """用真实关系行构造不受支持的控制协议。"""
    with control._repository.atomic():
        control._repository._local.connection.execute(
            "UPDATE control_tasks SET protocol=-1 WHERE task_id=?", (task.fingerprint,),
        )


def raw_control_snapshot(
    control: TaskControlStore, task: TaskKey,
) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """损坏输入的只读事实快照，不要求其能通过生产视图校验。"""
    from agent_run.sqlite_control_schema import COLUMNS, KEYS

    with control._repository._reading() as connection:
        if connection is None:
            return {}
        return {
            table: tuple(tuple(row) for row in connection.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE task_id=? "
                f"ORDER BY {', '.join(KEYS[table])}", (task.fingerprint,),
            ))
            for table, columns in COLUMNS.items()
        }


def control_refs(root: Path) -> list[tuple[TaskControlStore, TaskKey]]:
    """查询本例数据库中的真实 Task 身份，替代控制文件 glob。"""
    control = TaskControlStore(root)
    with control._repository._reading() as connection:
        if connection is None:
            return []
        identities = list(connection.execute(
            "SELECT workspace, repository, parent_number FROM control_tasks ORDER BY task_id",
        ))
    return [(control, TaskKey(Path(workspace), repository, parent))
            for workspace, repository, parent in identities]


def only_control(root: Path) -> tuple[TaskControlStore, TaskKey]:
    references = control_refs(root)
    assert len(references) == 1, references
    return references[0]


def load_only_control(root: Path) -> dict[str, Any]:
    control, task = only_control(root)
    record = control.load(task)
    assert record is not None
    return record
