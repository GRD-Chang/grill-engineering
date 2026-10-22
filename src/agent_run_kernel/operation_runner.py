"""Core operation interface, bound to one in-process physical adapter.

run_once performs one committed transition, one Effect, or one poll and returns;
an AgentCall is returned immediately for Host supervision. It never runs an
Agent or waits for a workflow/CI to finish. observe is an explicit operational
read: it accepts fresh Requirements/audit facts but allocates no work, advances
no phase and grants no write. User status/history queries do not call observe.

This first commit fixes the adapter contract; the concrete Core runner follows.
No Receipt, persisted cursor, or alternative current Run crosses this seam.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol

from .model import (AgentCall, Checks, Commit, CreationInputs, Effect, EffectResult,
                    PullRequest, Requirements, ResourceInspection, ResourceWitness, Step, World)


@dataclass(frozen=True)
class OperationBinding:
    repository_root: Path
    repository: str
    parent_number: int
    inputs: CreationInputs
    hidden_paths: tuple[Path, ...] = ()
    hidden_environment: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryRead:
    source_ref: str
    target_ref: str
    pr_number: int | None
    anchors: tuple[str, ...]
    check_head: str | None = None
    read_pr: bool = True


@dataclass(frozen=True)
class DeliveryFacts:
    target: Commit | None
    source: Commit | None
    pr: PullRequest | None = None
    checks: Checks | None = None
    ancestry: tuple[tuple[str, str], ...] = ()
    publisher_actor: str | None = None
    publisher_identity: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class OperationState:
    concurrency: int
    active: tuple[str, ...]  # Durable lease identities, never Future completion.
    cancel: tuple[str, ...]
    wake_at: float | None
    disposition: Literal["ready", "waiting", "quiescent", "complete"]


class OperationPorts(Protocol):
    """Physical reads/writes only; no Core SQL, refresh or World construction."""

    def source(self, parent_number: int) -> Requirements: ...
    def delivery(self, request: DeliveryRead) -> DeliveryFacts: ...
    def inspect(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]: ...
    def execute(self, effect: Effect, authorize: Callable[[], None]) -> EffectResult: ...
    def reconcile(self, effect: Effect) -> EffectResult: ...
    def prepare_publication(self, effect: Effect) -> Mapping[str, Any]: ...
    def prepare_retirement(self, effect: Effect) -> Mapping[str, Any]: ...


class OperationRunner(Protocol):
    """Factory: kernel.operation_runner(execution, ports).

    Host runs at most one run_once Future in its independent I/O pool. A
    bootstrap authorization reads inline in the Agent thread, never queues back
    into a pool it is waiting on. Native raw/exit/thread/activity hooks remain
    their existing exact-call record interfaces.
    """
    binding: OperationBinding

    def run_once(self, *, agent_capacity: int = 1) -> Step: ...
    def observe(self, subject: str | None) -> World: ...
    def authorize_agent(self, call: AgentCall) -> None: ...
    def worker_settled(self, call: AgentCall) -> bool: ...
    def state(self) -> OperationState: ...
