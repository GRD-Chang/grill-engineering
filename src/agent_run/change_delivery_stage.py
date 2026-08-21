from __future__ import annotations

"""Host port shared by the focused Change Delivery stage modules."""

from pathlib import Path
from typing import Any, Callable, Protocol

from agent_run.agents import AgentBackend
from agent_run.change_delivery_contracts import (
    ChangeDeliveryAdapter,
    ChangeDeliveryPublisher,
    ChangeJobContract,
)
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository


class ChangeDeliveryStage(Protocol):
    git: GitRepository
    github: GitHubPublisher
    agents: AgentBackend
    contract: ChangeJobContract
    adapter: ChangeDeliveryAdapter
    publisher: ChangeDeliveryPublisher

    def save(self, state: dict[str, Any]) -> dict[str, Any]: ...

    def _reject_stale(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
        message: str,
    ) -> None: ...

    def _invalidate_stale(
        self, state: dict[str, Any], job: dict[str, Any], checkout: Path
    ) -> None: ...

    def _block(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        code: str,
        message: str,
    ) -> bool: ...

    def _agent_is_current(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool: ...

    def _publication_is_current(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> bool: ...

    def _sync_attempts(
        self, state: dict[str, Any], job: dict[str, Any]
    ) -> None: ...

    def _invocation_events(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        request: dict[str, Any],
        *,
        role: str = "publication",
        phase: str,
    ) -> Callable[..., None]: ...

    def _wait_for_initial_credential(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        http_status: int | None,
    ) -> None: ...

    def _wait_for_human(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        phase: str,
        blockers: tuple[str, ...],
        code: str = "agent_requires_human",
    ) -> None: ...

    def _record_agent_run_status(
        self,
        pr_number: int,
        job: dict[str, Any],
        checks: str,
        *,
        next_action: str | None = None,
    ) -> None: ...

    def modification_budget_exhausted(self, job: dict[str, Any]) -> bool: ...

    def publication_budget_exhausted(self, attempts: int) -> bool: ...
