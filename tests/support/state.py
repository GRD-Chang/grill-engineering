"""真实 Run 存储查询及原有负向/精确历史 fixture 的写入边界。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_run.state import StateStore
from agent_run.state_limits import MAX_QUERY_BYTES
from agent_run.sqlite_state import SQLiteStateRepository


RunRef = tuple[Path, str]


def current_query_fixture(
    state: Mapping[str, Any], *, language: str | None = "zh",
) -> dict[str, Any]:
    """补齐只读查询场景的当前协议输入，不替代执行场景的 Controller 准备。"""
    from copy import deepcopy
    from agent_run.delivery_policy import DELIVERY_POLICY_PROTOCOL, DeliveryPolicy
    from agent_run.prompt_resources import builtin_resources
    from agent_run.task_control import TASK_CONTROL_PROTOCOL

    result = deepcopy(dict(state))
    if language is not None:
        result.setdefault("language", language)
    graph = result.get("ticket_graph")
    if isinstance(graph, dict) and isinstance(graph.get("tickets"), dict):
        numbers = graph["tickets"]
        if all(isinstance(number, str) and number.isdecimal() and int(number) > 0 for number in numbers):
            graph.setdefault("ordered_ticket_numbers", sorted(int(number) for number in numbers))
    defaults = {
        "prompt_resources": builtin_resources(result.get("language", "zh")),
        "branch_authority_protocol": 3, "review_budget_protocol": 2,
        "delivery_policy_protocol": DELIVERY_POLICY_PROTOCOL,
        "multi_ticket_protocol": 4, "semantic_attempt_protocol": 1,
        "human_response_audit_protocol": 1,
        "lifecycle_action_protocol": TASK_CONTROL_PROTOCOL,
        "policy_snapshot": DeliveryPolicy().snapshot(),
    }
    return defaults | result


def resume_audit_for_successors(
    run_id: str, invocations: list[dict[str, Any]],
) -> dict[str, Any]:
    """安排真实身份、摘要与顺序，关联已执行的 Resume 后继调用。"""
    from agent_run.resume_audit_contract import (
        resume_event_digest, resume_history_digest, resume_identity,
    )

    events = []
    for sequence, invocation in enumerate(invocations, 1):
        attempt = invocation.get("semantic_attempt") or {}
        event = {
            "sequence": sequence, "requested_at": "2026-10-15T00:00:00+00:00",
            "kind": "operator_stopped", "source_status": "operator_stopped",
            "failure_code": None, "work_subject": invocation["work_subject"],
            "generation": invocation.get("generation", 1),
            "semantic_attempt_id": attempt.get("attempt_id"),
            "source_invocation_started_at": None, "source_invocation_status": None,
            "thread_id": invocation.get("reported_thread_id"), "new_thread": False,
            "human_response_supplied": False,
            "successor_invocation_started_at": invocation["started_at"],
        }
        event["resume_id"] = resume_identity(run_id, event)
        event["event_digest"] = resume_event_digest(event)
        invocation["resume_id"] = event["resume_id"]
        events.append(event)
    return {"total": len(events), "compacted": 0,
            "rolling_digest": resume_history_digest(events), "history": events}


class _FixtureStateRepository(SQLiteStateRepository):
    def _observe_application(
        self, connection: sqlite3.Connection, run_id: str, state: Mapping[str, Any],
    ) -> None:
        """原始负向 fixture 不执行业务 Action 的 application receipt 对账。"""


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


def _complete_fixture_timelines(state: dict[str, Any], previous: Mapping[str, Any] | None) -> None:
    """将声明未读的历史补成原始 fixture 输入，不补造故意缺失的字段。"""
    from copy import deepcopy
    from agent_run.object_projection import ObjectProjection

    if not isinstance(state, ObjectProjection):
        return
    for field in state.timelines.keys() - state.keys():
        if previous is None or field not in previous:
            raise ValueError("Fixture requires its actual unread Timeline")
        state[field] = deepcopy(previous[field])
    state.timelines.clear()


def write_run_fixture(reference: RunRef, state: dict[str, Any]) -> None:
    """在准备阶段设置原始故障/历史 fixture，保留故意非法的输入。

    这是测试专用的准确场景替换，不代表生产提交行为。删除和重建只作用于
    本例指定的 Run，处于同一真实事务；生产的首次创建拒绝覆盖已有身份。
    """
    root, run_id = reference
    repository = _FixtureStateRepository(root, max_bytes=MAX_QUERY_BYTES)
    with repository.atomic():
        timelines = getattr(state, "timelines", {})
        if timelines:
            previous = repository.load_fields(run_id, set(timelines))
            _complete_fixture_timelines(state, previous)
        connection = repository._local.connection
        released = [row[0] for row in connection.execute(
            "SELECT DISTINCT material_id FROM work_evidence WHERE run_id=? AND material_id IS NOT NULL", (run_id,),
        )]
        connection.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        repository.save(run_id, state)
        # Replacing test preparation must not accumulate orphaned evidence.
        # Shared materials still referenced by any other Run remain intact.
        connection.executemany(
            "DELETE FROM work_evidence_materials WHERE material_id=? AND NOT EXISTS "
            "(SELECT 1 FROM work_evidence WHERE material_id=work_evidence_materials.material_id)",
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
    _complete_fixture_timelines(state, previous)
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
