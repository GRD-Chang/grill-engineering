"""Shared short interface copy; callers choose personal or frozen Run language.

Long Agent instructions belong to Markdown Prompt resources, not this catalog.
Keys and interpolation values are machine facts and are never translated.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LANGUAGES = ("zh", "en")
DEFAULT_LANGUAGE = "zh"


def validate_language(value: object) -> str:
    if not isinstance(value, str) or value not in LANGUAGES:
        raise ValueError("language 必须是 zh 或 en (must be zh or en)")
    return value


@lru_cache(maxsize=len(LANGUAGES))
def _catalog(language: str) -> dict[str, str]:
    path = Path(__file__).with_name("resources") / validate_language(language) / "messages.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in document.items()
    ):
        raise ValueError(f"Invalid short-copy catalog: {path}")
    return {key: value for key, value in document.items()}


def text(key: str, *, language: str = DEFAULT_LANGUAGE, **values: object) -> str:
    """Select copy without translating user text or falling back across languages."""
    return _catalog(language)[key].format(**values)


def selected_language(state: dict[str, object] | None = None) -> str:
    """Use a Run's fixed language, or personal defaults when no Run is involved."""
    if state is not None:
        return validate_language(state["language"])
    from agent_run.user_defaults import UserDefaultsStore

    return UserDefaultsStore().language()
