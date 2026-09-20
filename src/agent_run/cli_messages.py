"""CLI resource selection without making Run operations depend on personal defaults."""
from __future__ import annotations

from agent_run.messages import (
    DEFAULT_LANGUAGE, selected_language, text,
    error_message as error_message, error_detail as error_detail,
)
from agent_run.user_defaults import UserDefaultsError


def personal_language() -> str:
    try:
        return selected_language()
    except (UserDefaultsError, OSError):
        # Invalid personal settings must still permit help and existing Run access.
        return DEFAULT_LANGUAGE


def cli_message(key: str, *, language: str | None = None, **values: object) -> str:
    return text(key, language=language or personal_language(), **values)
