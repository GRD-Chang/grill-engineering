"""Physical operation contracts; no SQL, scheduling or Core permission policy."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Literal, Mapping, Protocol

from agent_run.contracts.write_authority import WriteAuthority

from agent_run.model import (Checks, Commit, CreationInputs, Effect, EffectResult, Execution,
                    PullRequest, Requirements, ResourceInspection, ResourceWitness, World)

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
    # None retains the complete anchor relation. An explicit tuple requests
    # only these directed edges; source, target and pr_base resolve in this observation.
    ancestry_pairs: tuple[tuple[str, str], ...] | None = None
    # False omits only Required Checks, never PR/ref identity observation.
    read_checks: bool = True
    # A complete Parent may also observe one repair PR targeting its Run source.
    # The primary delivery remains Run -> default; nested repairs are invalid.
    repair: DeliveryRead | None = None

@dataclass(frozen=True)
class PreparedRequirements:
    """Source plus opaque facts owned by this physical observation only."""
    source: Requirements
    physical: object


@dataclass(frozen=True)
class DeliveryFacts:
    target: Commit | None
    source: Commit | None
    pr: PullRequest | None = None
    checks: Checks | None = None
    ancestry: tuple[tuple[str, str], ...] = ()
    publisher_actor: str | None = None
    publisher_identity: Mapping[str, Any] | None = None
    # Checked-but-not-positive is false. None is never proof of a false edge.
    ancestry_checked: tuple[tuple[str, str], ...] | None = None
    # False means not requested. With an observed PR, True + no open PR is
    # inapplicable; True + open PR + checks=None is unknown, never a pass.
    checks_requested: bool = True
    repair: DeliveryFacts | None = None


def requested_ancestry(request: DeliveryRead, source: Commit | None,
                       target: Commit | None = None, pr: PullRequest | None = None) -> tuple[tuple[str, str], ...] | None:
    """Resolve each directed demand from the same observation's exact objects.

    Missing refs leave only their incident edges unresolved, never negative.
    An explicit empty request is distinct from an entirely unresolved request.
    """
    if request.ancestry_pairs is None:
        return None
    if (not isinstance(request.ancestry_pairs, tuple)
            or any(not isinstance(pair, tuple) or len(pair) != 2
                   or any(not isinstance(value, str) for value in pair) for pair in request.ancestry_pairs)):
        raise ReadUnavailable("ancestry_request_invalid", "Ancestry requires directed pairs")
    observed = {"source": source, "target": target, "pr_base": None if pr is None else pr.base}
    def exact(value: str) -> bool:
        return len(value) in (40, 64) and all(char in "0123456789abcdef" for char in value)
    if any(value not in observed and not exact(value) for pair in request.ancestry_pairs for value in pair):
        raise ReadUnavailable("ancestry_request_invalid", "Ancestry requires exact commit identities")
    heads = {name: None if commit is None else commit.sha for name, commit in observed.items()}
    resolved = set()
    for pair in request.ancestry_pairs:
        before, after = (heads.get(value, value) for value in pair)
        if before is None or after is None:
            continue
        if not exact(before) or not exact(after):
            raise ReadUnavailable("ancestry_request_invalid", "Observed endpoint is not an exact commit identity")
        resolved.add((before, after))
    return tuple(sorted(resolved)) if resolved or not request.ancestry_pairs else None

@dataclass(frozen=True)
class OperationSupervision:
    """Current cancellation and wake facts; never a work or exit decision."""
    concurrency: int
    active: tuple[str, ...]
    cancel: tuple[str, ...]
    wake_at: float | None
    preparing: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationState:
    concurrency: int
    active: tuple[str, ...]  # Durable lease identities, never Future completion.
    cancel: tuple[str, ...]  # The same lease identity space as active.
    wake_at: float | None  # Absolute time in Kernel.clock's domain.
    disposition: Literal["ready", "waiting", "quiescent", "complete"]
    preparing: tuple[str, ...] = ()  # Preparation IDs become the real lease IDs atomically.

class ObservationPorts(Protocol):
    """Read Source and Delivery facts without holding Effect write capabilities."""

    def source(self, parent_number: int) -> Requirements: ...
    def source_observation(self, parent_number: int, query: DeliveryRead | None) -> PreparedRequirements: ...
    def complete_observation(self, query: DeliveryRead, prepared: PreparedRequirements) -> DeliveryFacts: ...
    def delivery(self, request: DeliveryRead) -> DeliveryFacts: ...


class OperationPorts(ObservationPorts, Protocol):
    """Compose observation and authorized physical Effect capabilities."""

    def inspect(self, resources: tuple[ResourceWitness, ...]) -> tuple[ResourceInspection, ...]: ...
    def execute(self, effect: Effect, authorize: WriteAuthority) -> EffectResult: ...
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
    # A check-free result belongs only to this exact local write admission.
    local_effect: Effect | None = None


@dataclass(frozen=True)
class RequirementsRead:
    fence: ReadFence
    parent_number: int
    # Presealed physical demand only; Source acceptance regenerates it before
    # any prefetched Delivery facts can be consumed.
    query: DeliveryRead | None = None


@dataclass(frozen=True)
class LocalCleanupRead:
    effect: Effect
    source: RequirementsRead


@dataclass(frozen=True)
class LocalCleanupObservation:
    effect: Effect
    fence: ReadFence


@dataclass(frozen=True)
class DeliveryObservation:
    fence: ReadFence
    query: DeliveryRead


@dataclass(frozen=True)
class _SentRead:
    effect_id: str
    dispatch_serial: int
    subject: str
    generation: int

@dataclass(frozen=True)
class _ApprovalRead:
    subject: str
    generation: int
    boundary: tuple[str | int | None, ...]

@dataclass
class _ObservationPass:
    execution: Execution
    action_id: str
    pending: list[_SentRead | _ApprovalRead]
    lock: RLock = field(default_factory=RLock, repr=False)

    def complete(self, member: _SentRead | _ApprovalRead) -> None:
        with self.lock:
            if member in self.pending:
                self.pending.remove(member)

    def defer(self, member: _SentRead | _ApprovalRead) -> None:
        with self.lock:
            if member in self.pending:
                self.pending.remove(member)
                self.pending.append(member)

@dataclass(frozen=True)
class Work:
    execution: Execution
    token: str
    subject: str | None
    cleanup: bool
    allow_agent: bool
    observation: _ObservationPass | None
    member: _SentRead | _ApprovalRead | None
    approval_observed: bool = False


@dataclass(frozen=True)
class PrepareIO:
    work: Work
    effect: Effect
    kind: Literal["publication", "retirement"]
    token: str


@dataclass(frozen=True)
class EffectIO:
    work: Work
    effect: Effect
    kind: Literal["execute", "readback"]
    token: str


@dataclass(frozen=True)
class ObserveIO:
    work: Work
    request: RequirementsRead
    after: PrepareIO | EffectIO | None = None
    result: Mapping[str, Any] | EffectResult | None = None


IORequest = ObserveIO | PrepareIO | EffectIO
IOResult = tuple[World, ReadFence] | Mapping[str, Any] | EffectResult
