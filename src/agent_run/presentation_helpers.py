from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any


def human_next_action(value: object, *, run_id: object = None) -> object:
    """Hide managed Run identifiers from ordinary operator instructions."""

    if not isinstance(value, str):
        return value
    if isinstance(run_id, str) and run_id:
        return re.sub(
            rf"(?<!\S){re.escape(run_id)}(?!\S)",
            "<run-id>",
            value,
        )
    return value


def elapsed_seconds_since(value: object, *, end: datetime | None = None) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        started = datetime.fromisoformat(value)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    finished = end or datetime.now(UTC)
    return max(0, int((finished - started).total_seconds()))


def delivery_object_label(
    state: dict[str, Any],
    locator: str,
    *,
    ticket_number: object = None,
    include_parent_for_run: bool = False,
) -> str:
    """Render one stable label for a delivery work subject or state location."""

    if locator.startswith("ticket:"):
        number = ticket_number or locator.split(":", 1)[1]
        return f"Ticket #{number}"
    parent = state.get("parent")
    parent_number = parent.get("number", "?") if isinstance(parent, dict) else "?"
    if locator == "parent" or locator.startswith("parent-only:"):
        return f"Parent Issue #{parent_number}"
    if locator in {"run_acceptance", "run_repair"} or locator.startswith(
        ("run-acceptance:", "run-repair:")
    ):
        return "Run Acceptance"
    if locator == "run_publication" or locator.startswith("run-publication:"):
        return "Run Publication"
    if include_parent_for_run:
        return f"Delivery Run（Parent Issue #{parent_number}）"
    return "Delivery Run"
