"""真实 Run 存储查询及原有负向/精确历史 fixture 的写入边界。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_run.state import MAX_RUN_STATE_BYTES, StateStore
from agent_run.sqlite_state import SQLiteStateRepository


RunRef = tuple[Path, str]


def only_run_ref(root: Path) -> RunRef:
    run_ids = StateStore(root).list_run_ids()
    assert len(run_ids) == 1, run_ids
    return root, run_ids[0]


def run_refs(root: Path) -> list[RunRef]:
    return [(root, run_id) for run_id in StateStore(root).list_run_ids()]


def read_run(reference: RunRef) -> dict[str, Any]:
    root, run_id = reference
    state = StateStore(root).load_run(run_id)
    assert state is not None, run_id
    return state


def has_run(reference: RunRef) -> bool:
    root, run_id = reference
    return StateStore(root).has_run(run_id)


def run_state_text(reference: RunRef) -> str:
    """稳定序列化真实持久记录，比较查询前后的完整业务事实。"""
    return json.dumps(read_run(reference), ensure_ascii=False, sort_keys=True)


def snapshot_run(reference: RunRef) -> bytes:
    """逻辑快照包含所有持久字段，不依赖 SQLite 文件或 journal 的布局。"""
    return run_state_text(reference).encode("utf-8")


def snapshot_runs(root: Path) -> dict[str, bytes]:
    return {reference[1]: snapshot_run(reference) for reference in run_refs(root)}


def write_run_fixture(reference: RunRef, state: dict[str, Any]) -> None:
    """写入原始故障/历史 fixture，保留故意不满足当前合同的输入。

    普通合法场景继续调用 StateStore.save_run。本入口替代原有直接写 JSON 的
    故障注入和精确 timeline fixture；不执行 StateStore 的队列与历史归一化。
    repository 负责真实 SQLite 事务与数据库版本，不在测试中复制 SQL schema。
    """
    root, run_id = reference
    SQLiteStateRepository(root, max_bytes=MAX_RUN_STATE_BYTES).save(run_id, state)


def include_in_state_snapshot(path: Path) -> bool:
    """排除 SQLite 可变协调索引与空 WAL，保留全部持久数据文件。"""
    if path.name == "state.sqlite3-shm":
        return not path.is_file()
    if path.name == "state.sqlite3-wal" and path.is_file():
        return path.stat().st_size > 0
    return True
