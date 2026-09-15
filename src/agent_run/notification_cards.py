"""Fixed Feishu Card 2.0 compact template; no model-generated formatting."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from agent_run.presentation_helpers import execution_duration


def _text(value: object, limit: int = 260) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _markdown(value: object, limit: int = 260) -> str:
    return re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", _text(value, limit))


def card(event: dict[str, Any]) -> dict[str, Any]:
    """Build a narrow, single-column card with URL-only navigation."""
    number = event.get("task_number") or "?"
    repository = _text(event.get("repository", "未知仓库"), 100)
    lines = []
    if event.get("task_title"):
        lines.append(_markdown(event["task_title"], 160))
    if event.get("kind") in {"stage_start", "stage_end"} or event.get("trigger_role"):
        role = _markdown(event.get("trigger_role") or event.get("phase", ""))
        ordinal = event.get("round")
        lines.append(f"{role} · 第 {ordinal} 轮" if ordinal else role)
    if event.get("summary"):
        lines.append(_markdown(event["summary"]))
    checks = event.get("checks") or {}
    labels = {"e2e": "功能验证", "standards": "工程审查", "review": "工程审查", "spec": "需求核对"}
    results = {"pass": "通过", "fail": "未通过", "blocked": "受阻"}
    for name, result in checks.items():
        lines.append(f"{labels.get(name, name)}：{results.get(result, '尚未确认')}")
    if event.get("started_at"):
        from datetime import datetime
        try:
            started = datetime.fromisoformat(event["started_at"]).astimezone()
            lines.append(f"开始时间：{started:%Y-%m-%d %H:%M:%S %Z}")
        except ValueError:
            pass
    for field, label in (("duration_seconds", "本轮执行耗时"), ("total_seconds", "累计 Agent 执行耗时"),
                         ("elapsed_seconds", "任务历时")):
        value = event.get(field)
        if type(value) is int and value >= 0:
            duration = execution_duration(value)
            lines.append(f"**{label}**：{duration}")
    if event.get("next_step"):
        lines.append(f"**下一步**：{_markdown(event['next_step'])}")
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(lines)}]
    if any(len(str(event.get(key) or "")) > 260 for key in ("summary", "next_step")):
        elements.append({"tag": "markdown", "content": f"完整记录：`{_text(event.get('query', ''), 160).replace('`', '')}`"})
    url = event.get("url")
    if isinstance(url, str) and urlparse(url).scheme == "https" and urlparse(url).hostname == "github.com":
        elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "打开 PR" if "/pull/" in url else "打开 Issue"},
                         "type": "default", "behaviors": [{"type": "open_url", "default_url": url}]})
    return {
        "schema": "2.0",
        "config": {"width_mode": "compact", "summary": {"content": _text(f"{repository} #{number} · {event['title']}", 160)}},
        "header": {"title": {"tag": "plain_text", "content": _text(event["title"], 100)},
                   "subtitle": {"tag": "plain_text", "content": f"{repository} · #{number}"},
                   "template": event.get("color", "blue")},
        "body": {"direction": "vertical", "padding": "12px", "elements": elements},
    }
