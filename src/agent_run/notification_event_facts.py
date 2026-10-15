"""Notification envelope, stable identity and subject projection shared by event builders."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from agent_run.messages import selected_language, text


def event_copy(state: dict[str, Any], key: str, **values: object) -> str:
    return text("notification.event." + key, language=selected_language(state), **values)


def _identity(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def event_envelope(state: dict[str, Any], kind: str, identity: object, title: str,
          color: str = "blue", **facts: Any) -> dict[str, Any]:
    parent = state.get("parent") or {}
    repository = str(state.get("repository") or event_copy(state, "unknown_repository"))
    number = parent.get("number")
    return {
        "id": _identity(state.get("run_id"), kind, identity), "kind": kind,
        "title": title, "color": color, "repository": repository,
        "language": selected_language(state),
        "task_number": number, "task_title": parent.get("title") or "",
        "phase": event_copy(state, "delivery"), "round": None, "duration_seconds": None,
        "summary": "", "next_step": "", "current": False,
        "url": f"https://github.com/{repository}/issues/{number}" if number else None,
        "query": f"agent-run history {state.get('run_id')} --details", **facts,
    }


def event_subject(state: dict[str, Any], subject: object) -> dict[str, Any]:
    match = re.search(r"ticket:(\d+)", str(subject))
    if not match:
        return {}
    number = match.group(1)
    ticket = ((state.get("ticket_graph") or {}).get("tickets") or {}).get(number, {})
    return {"task_number": int(number), "task_title": ticket.get("title") or "",
            "url": f"https://github.com/{state.get('repository')}/issues/{number}"}


