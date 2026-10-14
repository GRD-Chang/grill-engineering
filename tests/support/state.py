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
    """在准备阶段设置原始故障/历史 fixture，保留故意非法的输入。

    这是测试专用的准确场景替换，不代表生产提交行为。删除和重建只作用于
    本例指定的 Run，处于同一真实事务；生产的首次创建拒绝覆盖已有身份。
    """
    root, run_id = reference
    repository = SQLiteStateRepository(root, max_bytes=MAX_RUN_STATE_BYTES)
    with repository.atomic():
        connection = repository._local.connection
        released = [row[0] for row in connection.execute(
            "SELECT material_id FROM run_material_refs WHERE run_id=?", (run_id,),
        )]
        connection.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        repository.save(run_id, state)
        # Replacing test preparation must not accumulate orphaned evidence.
        # Shared materials still referenced by any other Run remain intact.
        connection.executemany(
            "DELETE FROM run_materials WHERE material_id=? AND NOT EXISTS "
            "(SELECT 1 FROM run_material_refs WHERE material_id=run_materials.material_id)",
            ((identity,) for identity in released),
        )


def persist_run_fixture(store: StateStore, run_id: str, state: dict[str, Any]) -> None:
    """只用于 arrange：设置完整合法场景，业务动作须调用生产对象入口。

    历史事件与队列归一化保持原 fixture 意义；读取及完整复制的成本只属于
    测试准备，不得用本函数测量日常保存、授权、并发或故障回滚。
    """
    from copy import deepcopy
    from agent_run.state import _append_timeline_event, _sanitize_durable_errors
    from agent_run.state_retention import compact_invocation_details
    from agent_run.ticket_state import capture_ticket_wait
    from agent_run.ticket_work import reconcile

    if not store.has_run(run_id):
        store.save_run(run_id, state)
        return
    previous = store.load_run(run_id)
    invocation, job = state.get("active_agent_invocation"), state.get("active_ticket_job")
    if (isinstance(invocation, dict) and isinstance(job, dict)
            and invocation.get("work_subject") == f"ticket:{job.get('ticket_number')}"):
        state.setdefault("ticket_agent_invocations", {})[str(job["ticket_number"])] = deepcopy(invocation)
    capture_ticket_wait(state)
    if "ticket_work_queue" in state:
        reconcile(state)
    durable = _sanitize_durable_errors(state)
    compact_invocation_details(durable)
    if "run_id" in durable:
        _append_timeline_event(durable, previous)
    write_run_fixture((store.root, run_id), durable)
    for field in ("agent_invocation_history", "timeline", "timeline_continuation", "timeline_at_capacity"):
        if field in durable:
            state[field] = deepcopy(durable[field])


def include_in_state_snapshot(path: Path) -> bool:
    """排除 SQLite 可变协调索引与空 WAL，保留全部持久数据文件。"""
    if path.name == "state.sqlite3-shm":
        return not path.is_file()
    if path.name == "state.sqlite3-wal" and path.is_file():
        return path.stat().st_size > 0
    return True
