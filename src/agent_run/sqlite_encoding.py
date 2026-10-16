"""JSON 仅用于原格式材料和查询身份；容器复制保留明确工作范围。"""

from __future__ import annotations
import json
from typing import Any

_CONTAINER_TYPES = (dict, list)


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def object_path(encoded: str) -> tuple[str | int, ...]:
    path = json.loads(encoded)
    if not isinstance(path, list) or any(type(part) not in (str, int) for part in path):
        raise ValueError("Invalid SQLite query ownership path")
    return tuple(path)


def copy_json_tree(value: Any) -> Any:
    """Copy a decoded JSON tree's containers, sharing only immutable scalars.

    SQLite projections have already passed JSON decoding and tree assembly;
    they contain no arbitrary Python objects, mutable scalars or cycles.
    """
    from agent_run.work_projection import JobProjection, JobHistoryAppend, RepairBudgetProjection, ResumeHistory

    if isinstance(value, dict):
        copied: dict[str, Any] = value.copy() if isinstance(value, (JobProjection, RepairBudgetProjection)) else {}
        copied.update(
            {
                key: copy_json_tree(child) if isinstance(child, _CONTAINER_TYPES) else child
                for key, child in value.items()
            }
        )
        return copied
    if isinstance(value, list):
        items = [copy_json_tree(child) if isinstance(child, _CONTAINER_TYPES) else child for child in value]
        if isinstance(value, JobHistoryAppend):
            return JobHistoryAppend(items, base_bytes=value.base_bytes, base_count=value.base_count)
        if isinstance(value, ResumeHistory):
            return ResumeHistory(items, scope=value.scope)
        return items
    return value
