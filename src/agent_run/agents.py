from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class DevelopmentResult:
    thread_id: str
    summary: str
    replaced_thread_id: str | None = None


@dataclass(frozen=True)
class PublicationResult:
    thread_id: str
    artifact: dict[str, Any]
    replaced_thread_id: str | None = None


@dataclass(frozen=True)
class HumanBlockerResult:
    """The one semantic escape hatch shared by top-level Codex roles."""

    thread_id: str
    human_blockers: tuple[str, ...]
    replaced_thread_id: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    thread_id: str
    artifact: dict[str, Any]


class AgentBackend(Protocol):
    def develop(
        self, request: dict[str, Any]
    ) -> DevelopmentResult | HumanBlockerResult: ...

    def publication(
        self, request: dict[str, Any]
    ) -> PublicationResult | HumanBlockerResult | dict[str, Any]: ...

    def review(self, request: dict[str, Any]) -> ReviewResult: ...

    def run_publication(self, request: dict[str, Any]) -> dict[str, Any]: ...
