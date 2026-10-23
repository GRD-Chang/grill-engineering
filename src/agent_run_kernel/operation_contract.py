"""Immutable physical read contracts; no SQL, scheduling or write authority."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol

from .model import (Checks, Commit, CreationInputs, Effect, EffectResult, Execution,
                    PullRequest, Requirements, ResourceInspection, ResourceWitness)

class ReadUnavailable(RuntimeError):
    """Port read failure; optional fields are an actual write response only."""
    def __init__(self, code: str, message: str, *, write_returncode: int | None = None,
                 write_diagnostic: str = "") -> None:
        super().__init__(message)
        self.code, self.message = code, message
        self.write_returncode, self.write_diagnostic = write_returncode, write_diagnostic

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
    cancel: tuple[str, ...]  # The same lease identity space as active.
    wake_at: float | None  # Absolute time in Kernel.clock's domain.
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


@dataclass(frozen=True)
class ReadFence:
    """Identity of one read segment, never the enclosing operation's progress.

    scope uses canonical immutable bytes; no mutable SQL/context crosses I/O.
    """
    execution: Execution | None
    subject: str | None
    cleanup: bool
    scope: bytes
    source_sequence: int | None
    control_version: tuple[str | int | None, ...]


@dataclass(frozen=True)
class RequirementsRead:
    fence: ReadFence
    parent_number: int


@dataclass(frozen=True)
class DeliveryObservation:
    fence: ReadFence
    query: DeliveryRead
