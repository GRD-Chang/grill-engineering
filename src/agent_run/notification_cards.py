"""Fixed Feishu Card 2.0 compact template; no model-generated formatting."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from agent_run.messages import text


def _text(value: object, limit: int = 260) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _markdown(value: object, limit: int = 260) -> str:
    return re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", _text(value, limit))


def card(event: dict[str, Any]) -> dict[str, Any]:
    """Build a narrow, single-column card with URL-only navigation."""
    language = event["language"]

    def copy(key: str, **values: object) -> str:
        return text("card." + key, language=language, **values)

    def duration(value: int) -> str:
        minutes, seconds = divmod(value, 60)
        hours, minutes = divmod(minutes, 60)
        unit = "hours" if hours else "minutes" if minutes else "seconds"
        return copy("duration." + unit, hours=hours, minutes=minutes, seconds=seconds)

    number = event.get("task_number") or "?"
    repository = _text(event.get("repository", copy("unknown_repository")), 100)
    lines = []
    if event.get("task_title"):
        lines.append(_markdown(event["task_title"], 160))
    if event.get("kind") in {"stage_start", "stage_end"} or event.get("trigger_role"):
        role = _markdown(event.get("trigger_role") or event.get("phase", ""))
        ordinal = event.get("round")
        lines.append(copy("round", role=role, ordinal=ordinal) if ordinal else role)
    if event.get("summary"):
        lines.append(_markdown(event["summary"]))
    checks = event.get("checks") or {}
    labels = {name: copy("check." + name) for name in ("e2e", "standards", "review", "spec")}
    results = {result: copy("result." + result) for result in ("pass", "fail", "blocked")}
    for name, result in checks.items():
        lines.append(copy("field", label=labels.get(name, name), value=results.get(result, copy("unconfirmed"))))
    if event.get("started_at"):
        from datetime import datetime
        try:
            started = datetime.fromisoformat(event["started_at"]).astimezone()
            lines.append(copy("field", label=copy("started_at"), value=f"{started:%Y-%m-%d %H:%M:%S %Z}"))
        except ValueError:
            pass
    for field in ("duration_seconds", "total_seconds", "elapsed_seconds"):
        value = event.get(field)
        if type(value) is int and value >= 0:
            lines.append(copy("field", label=f"**{copy(field)}**", value=duration(value)))
    effort = event.get("effort") or {}
    for role in ("development", "review"):
        facts = effort.get(role) or {}
        parts = []
        if "rounds" in facts:
            parts.append(copy("effort.rounds", count=facts["rounds"]))
        if "execution_seconds" in facts:
            parts.append(copy("effort.execution", duration=duration(facts["execution_seconds"])))
        configurations = facts.get("configurations") or []
        if len(configurations) == 1:
            config = configurations[0]
            parts.append(_markdown(f"{config['model']} / {config['reasoning_effort']}"))
        if parts:
            lines.append(copy("field", label=f"**{copy('effort.' + role)}**", value=" · ".join(parts)))
        if len(configurations) > 1:
            for config in configurations:
                participation = [copy("effort.participation", count=config["rounds"])]
                if "execution_seconds" in config:
                    participation.append(duration(config["execution_seconds"]))
                lines.append(copy("field", label=_markdown(f"{config['model']} / {config['reasoning_effort']}"),
                                  value=" · ".join(participation)))
            if facts.get("shared_rounds"):
                lines.append(copy("effort.shared_rounds"))
    if event.get("next_step"):
        lines.append(copy("field", label=f"**{copy('next_step')}**", value=_markdown(event["next_step"])))
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": "\n".join(lines)}]
    if effort or any(len(str(event.get(key) or "")) > 260 for key in ("summary", "next_step")):
        elements.append({"tag": "markdown", "content": copy("field", label=copy("full_record"), value=f"`{_text(event.get('query', ''), 160).replace('`', '')}`")})
    url = event.get("url")
    if isinstance(url, str) and urlparse(url).scheme == "https" and urlparse(url).hostname == "github.com":
        elements.append({"tag": "button", "text": {"tag": "plain_text", "content": copy("open_pr" if "/pull/" in url else "open_issue")},
                         "type": "default", "behaviors": [{"type": "open_url", "default_url": url}]})
    return {
        "schema": "2.0",
        "config": {"width_mode": "compact", "summary": {"content": _text(f"{repository} #{number} · {event['title']}", 160)}},
        "header": {"title": {"tag": "plain_text", "content": _text(event["title"], 100)},
                   "subtitle": {"tag": "plain_text", "content": f"{repository} · #{number}"},
                   "template": event.get("color", "blue")},
        "body": {"direction": "vertical", "padding": "12px", "elements": elements},
    }
