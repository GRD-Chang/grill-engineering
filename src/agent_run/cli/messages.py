"""CLI resource selection without making Run operations depend on personal defaults."""
from __future__ import annotations

from agent_run.contracts.copy import DEFAULT_LANGUAGE
from agent_run.errors import error_message as error_message, error_detail as error_detail
from agent_run.config.defaults import UserDefaultsError, UserDefaultsStore


def personal_language() -> str:
    try:
        return UserDefaultsStore().language()
    except (UserDefaultsError, OSError):
        # Invalid personal settings must still permit help and existing Run access.
        return DEFAULT_LANGUAGE
