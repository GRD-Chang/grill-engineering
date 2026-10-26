"""Notification envelope, stable identity and subject projection shared by event builders."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_run.contracts.copy import selected_language, text
from agent_run.queries.read_facts import action_command


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
        "query": action_command("history", run_id=str(state.get("run_id")), repository=repository, details=True), **facts,
    }


