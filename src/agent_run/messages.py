"""Shared short interface copy; callers choose personal or frozen Run language.

Long Agent instructions belong to Markdown Prompt resources, not this catalog.
Keys and interpolation values are machine facts and are never translated.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
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
        return validate_language(state.get("language"))
    from agent_run.user_defaults import UserDefaultsStore

    return UserDefaultsStore().language()

# Human rendering is scoped to one query. Machine projections and background
# notification decisions keep their explicit/default language outside this scope.
_display_language: ContextVar[str] = ContextVar("display_language", default="zh")


def display_language() -> str:
    return _display_language.get()


@contextmanager
def presentation_language(language: str) -> Iterator[None]:
    token = _display_language.set(validate_language(language))
    try:
        yield
    finally:
        _display_language.reset(token)


def display_text(key: str, **values: object) -> str:
    return text(key, language=display_language(), **values)


class ErrorMessage(str):
    """Keep original diagnostics stable while retaining structured display copy."""

    key: str
    values: dict[str, object]

    def __new__(cls, key: str, *, audit: str | None = None, **values: object) -> ErrorMessage:
        instance = super().__new__(cls, audit if audit is not None else text(key, language=DEFAULT_LANGUAGE, **values))
        instance.key = key
        instance.values = values
        return instance

    def render(self, language: str) -> str:
        return text(self.key, language=language, **{
            key: value.render(language) if isinstance(value, ErrorMessage) else value
            for key, value in self.values.items()
        })


def error_message(key: str, *, audit: str | None = None, **values: object) -> ErrorMessage:
    return ErrorMessage(key, audit=audit, **values)


def error_detail(error: Exception, language: str) -> str:
    message = error.args[0] if error.args else ""
    return message.render(language) if isinstance(message, ErrorMessage) else str(error)
