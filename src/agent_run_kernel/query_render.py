"""One factual document rendered as machine JSON, plain text or safe Rich text."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
import json
import re
from typing import Any, Literal, TextIO
import unicodedata

from .query import QueryDocument, QueryError
from .storage import QUERY_LIMIT


_COPY = {
    "zh": {"run": "交付运行", "status": "状态", "phase": "阶段", "subject": "工作对象", "unknown": "未记录",
           "next": "下一步", "model": "模型", "effort": "推理强度", "history": "工作历程", "budget": "累计用量",
           "findings": "当前问题", "evidence": "证据", "duration": "任务历时（秒）", "agent": "Agent",
           "development": "开发 Agent", "review": "验收 Agent", "publication": "发布 Agent", "gates": "人工待办",
           "started": "已记录启动；当前活性未确认", "cleanup": "资源清理", "pending": "待清理", "complete": "完成",
           "time": "显示时区", "incomplete": "耗时记录不完整", "more": "后续历史页", "scope": "影响范围",
           "no_runs": "无匹配运行", "active": "进行中", "completed": "业务已完成", "operator_stopped": "已主动停止",
           "blocked": "有阻塞", "observe": "继续观察", "inspect": "核对原始事实与诊断", "resume": "显式恢复原工作",
           "approve": "核验最终 PR 后批准", "requeue": "确认最新需求后重新排队", "local": "该 Ticket 及依赖工作",
           "global": "整个交付运行", "unknown_live": "执行者当前活性未确认；查询不会恢复或停止任务"},
    "en": {"run": "Delivery Run", "status": "Status", "phase": "Phase", "subject": "Work subject", "unknown": "Not recorded",
           "next": "Next action", "model": "Model", "effort": "Reasoning effort", "history": "Work history", "budget": "Cumulative usage",
           "findings": "Current findings", "evidence": "Evidence", "duration": "Task elapsed seconds", "agent": "Agent",
           "development": "Development Agent", "review": "Review Agent", "publication": "Publication Agent", "gates": "Operator actions",
           "started": "Recorded started; current liveness unconfirmed", "cleanup": "Resource cleanup", "pending": "Pending cleanup", "complete": "Complete",
           "time": "Display timezone", "incomplete": "Execution timing incomplete", "more": "More history", "scope": "Impact scope",
           "no_runs": "No matching Runs", "active": "Active", "completed": "Business completed", "operator_stopped": "Operator stopped",
           "blocked": "Blocked", "observe": "Continue observing", "inspect": "Inspect original facts and diagnostics", "resume": "Explicitly resume original work",
           "approve": "Verify and approve the final PR", "requeue": "Confirm new requirements and requeue", "local": "This Ticket and dependent work",
           "global": "Whole Delivery Run", "unknown_live": "Executor liveness is unconfirmed; querying does not resume or stop work"},
}


def _safe(value: object) -> str:
    content = str(value)
    content = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", content)
    content = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", content)
    return "".join(character for character in content if character in {"\n", "\t"}
                   or unicodedata.category(character) not in {"Cc", "Cf"})


def _time(value: float | None) -> tuple[str | None, str]:
    if value is None:
        return None, "UTC"
    stamp = datetime.fromtimestamp(value, UTC)
    try:
        local = stamp.astimezone()
        return local.isoformat(timespec="seconds"), str(local.tzinfo)
    except (OSError, ValueError, OverflowError):
        return stamp.isoformat(timespec="seconds"), "UTC (fallback)"


def _body(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _findings(value: Any) -> Iterator[str]:
    if not isinstance(value, Mapping):
        return
    if value.get("human_blockers"):
        yield _body(value["human_blockers"])
    for lane in value.get("checks", {}).values():
        if isinstance(lane, Mapping) and lane.get("status") in {"fail", "blocked"}:
            for finding in lane.get("findings", []):
                yield _body(finding)


def _lines(document: QueryDocument, details: bool) -> Iterator[str]:
    copy = _COPY[document.language]
    data = document.data
    if document.kind == "runs":
        if not data["runs"]:
            yield copy["no_runs"]
        for run in data["runs"]:
            stamp, zone = _time(run["created_at"])
            yield f"{run['repository']} Parent #{run['parent']} {run['run_id']}"
            yield f"  {run['repository_root'] or copy['unknown']} | {stamp or copy['unknown']} ({zone}) | {', '.join(run['phases'])}"
        return
    yield f"{copy['run']}: {data['repository']} Parent #{data['parent']['number']}"
    if data["parent"]["title"]:
        yield data["parent"]["title"]
    yield f"{copy['status']}: {copy.get(data['status'], data['status'])} | {copy['phase']}: {data['phase']}"
    yield f"{copy['duration']}: {data['elapsed_seconds'] if data['elapsed_seconds'] is not None else copy['incomplete']}"
    yield copy["unknown_live"]
    # Put actionable current boundaries before long Findings/evidence.
    for action in data["operator_actions"]:
        scope = copy["local"] if action["scope"] == "ticket" else copy["global"]
        target = f"#{action['issue']}" if action.get("issue") else data["repository"]
        yield f"{copy['gates']}: {target} — {action['reason']} ({copy['scope']}: {scope})"
        command = f"agent-run {action['action']} {data['parent']['number']} --repo {data['repository']}"
        if action["scope"] == "ticket" and action["action"] == "resume":
            command += f" --ticket {action['issue']}"
        yield f"{copy['next']}: {copy[action['action']]}" + (f" — {command}" if action["action"] != "inspect" else "")
        if action["evidence"] is not None:
            yield _body(action["evidence"])
    if not data["operator_actions"]:
        yield f"{copy['next']}: {copy.get(data['next_action'], data['next_action']) or copy['complete']}"
    for job in data["subjects"]:
        yield f"{copy['subject']}: #{job['issue']} {job['title'] or ''} — {job['phase']} ({job['purpose']})"
        pending = job["pending_attempt"]
        if pending:
            yield f"{copy['agent']}: {copy.get(pending['role'], pending['role'])} #{pending['ordinal']}"
            for invocation in pending["invocations"]:
                yield f"  {copy['model']}: {invocation['model']} | {copy['effort']}: {invocation['effort']}"
                if invocation["started_at"] is not None:
                    yield f"  {copy['started']}"
                yield f"  {copy['duration']}: {invocation['elapsed_seconds'] if invocation['elapsed_seconds'] is not None else copy['incomplete']}"
        for budget in job["budgets"]:
            review_limit = budget["review_limit"] if budget["review_limit"] is not None else copy["unknown"]
            yield (f"  {copy['budget']}: {budget['purpose']} W{budget['window']} "
                   f"D {budget['developments_used']}/{budget['development_limit']} R {budget['reviews_used']}/{review_limit}")
        if job["delivery_cleanup"]:
            cleanup = job["delivery_cleanup"]
            yield f"  {copy['cleanup']}: {copy['complete'] if cleanup['state'] == 'complete' else copy['pending']}"
        review = job["review"]
        if review and review["outcome"] != "pass":
            for finding in _findings(review["artifact"]):
                yield f"{copy['findings']}: {finding}"
    if document.kind == "history":
        yield copy["history"]
        for event in data["timeline"]:
            stamp, zone = _time(event["created_at"])
            yield f"{stamp or copy['unknown']} [{zone}] {event['subject']} {event['kind']}"
            evidence = event["evidence"]
            if event["kind"] in {"human_response", "paused"} and evidence is not None:
                content = _body(evidence)
                yield content if details or len(content) <= 1000 else content[:1000] + " [truncated; --details]"
            elif details and evidence is not None:
                yield f"  {copy['evidence']}: {_body(evidence)}"
        if details:
            for attempt in data["semantic_agent_attempts"]:
                yield f"{attempt['subject']} {copy.get(attempt['role'], attempt['role'])} #{attempt['ordinal']}: {attempt['status']}"
                if attempt["artifact"] is not None:
                    yield _body(attempt["artifact"])
        if data["history_page"]["has_more"]:
            yield f"{copy['more']}: --after {data['history_page']['next_after']}"


def render(document: QueryDocument, *, style: Literal["json", "plain", "rich"], details: bool, stream: TextIO) -> None:
    if style == "json":
        encoded = json.dumps(document.data, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > QUERY_LIMIT:
            raise QueryError("query_limit", "Public response exceeds its bounded limit")
        print(encoded, file=stream)
        return
    if style == "plain":
        for line in _lines(document, details):
            print(_safe(line), file=stream)
        return
    from rich.console import Console
    from rich.text import Text
    console = Console(file=stream, markup=False, highlight=False)
    for line in _lines(document, details):
        console.print(Text(_safe(line)))
