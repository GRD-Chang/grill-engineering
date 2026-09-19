"""Notification copy uses the same recovery decisions as the public Status view."""
from __future__ import annotations

from typing import Any

from agent_run.cli_presentation import human_next_action_for_state
from agent_run.messages import selected_language
from agent_run.presentation_helpers import human_pause_reason, human_status_term


def status_term(value: object, language: str) -> str:
    return str(human_status_term(value, language=language))


def pause_reason(value: object, language: str) -> str:
    return human_pause_reason(value, language=language)


def next_action(state: dict[str, Any]) -> str:
    return str(human_next_action_for_state(state, language=selected_language(state)))
