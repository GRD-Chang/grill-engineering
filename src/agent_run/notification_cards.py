"""Fixed Feishu Card 2.0 compact template; no model-generated formatting."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


def _text(value: object, limit: int = 260) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _markdown(value: object, limit: int = 260) -> str:
    return re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", _text(value, limit))


def card(event: dict[str, Any]) -> dict[str, Any]:
    """Build a narrow, single-column card with URL-only navigation."""
    number = event.get("task_number") or "?"
    repository = _text(event.get("repository", "未知仓库"), 100)
    duration = event.get("duration_seconds")
    elapsed = f"{duration} 秒" if type(duration) is int and duration >= 0 else "未知"
    phase = _markdown(event.get("phase", "整体交付"))
    if event.get("object"):
        phase = f"{_markdown(event['object'], 80)} · {phase}"
    ordinal = event.get("round")
    lines = [f"**任务**：{_markdown(event.get('task_title', '未知'), 160)}",
             f"**阶段**：{phase} · 第 {ordinal} 轮" if ordinal else f"**阶段**：{phase}",
             f"**结果**：{_markdown(event.get('summary', '结果未知'))}"]
    if event.get("kind") == "stage_end":
        lines.append(f"**耗时**：{elapsed}")
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
