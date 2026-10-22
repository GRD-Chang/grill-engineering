"""Proposed decoder output for one finite v6 import unit; no admission or I/O.

Decoders construct these facts directly from sealed source records/documents,
not by translating _InitialFacts, _Active or _Completed. The future installer
and source-derived expected projection consume this same contract. Neither
may derive expectations from a written candidate or treat a constructed fact
as authentication. Existing source/schema/resource checks remain mandatory.

All new document fields contain preserved bytes, not mutable decoded mappings.
SourceRecord keys are exact primary keys, including historical version keys;
chronology never substitutes for identity. Unknown physical outcomes are not
represented as absence. No constructor supplies a source fact by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_run.task_identity import TaskKey

from .attempt_continuation import AttemptContinuationBasis
from .migration import SourceSnapshot
from .model import Commit, Purpose, Requirements, Role, RunSpec


@dataclass(frozen=True)
class SourceRecord:
    table: str
    key: tuple[str | int, ...]


@dataclass(frozen=True)
class ImportSource:
    snapshot: SourceSnapshot
    mode: Literal['original', 'copied']
    scope: Literal['single-ticket', 'whole-run', 'selected-graph']
    task: TaskKey | None  # None only when this source contract has no Task record.
    checkout_uuid: str | None
    profile_document: bytes | None  # Explicit creation profiles instead for idle copies.
    external_readproof: bytes | None
    oracle_documents: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class ImportedBudget:
    owner: SourceRecord
    record: SourceRecord
    purpose: Purpose
    window: int
    developments_used: int
    reviews_used: int
    current: bool


@dataclass(frozen=True)
class HistoricalExecution:
    """An observed old Invocation/binding, never a native Invocation to insert."""
    record: SourceRecord
    binding_id: str
    thread_id: str
    binding: bytes
    invocation: bytes


@dataclass(frozen=True)
class ImportedAttempt:
    record: SourceRecord
    role: Role
    role_ordinal: int
    generation: int
    purpose: Purpose
    window: int | None  # Preserve the source Publication's absent budget window.
    pending: bool
    accounted: bool
    outcome: str | None
    raw_record: SourceRecord
    raw_result: bytes
    normalized_artifact: bytes
    bound_version: bytes | None
    selected_version: bytes
    current_version: bytes
    allocation_boundary: bytes | None
    execution: HistoricalExecution | None  # Synchronous sources have no physical witness.
    # Reserved only. No admitted v6 producer currently supplies this field.
    # Missing PID/PGID/start/exit records cannot certify a zero Opening or an
    # exited worker. Certification needs actual process exit evidence or a
    # persistently authenticated synchronous-backend contract, plus the full
    # frozen input/Thread/Profile/raw/Opening/pause identity. Until admitted,
    # basis stays None and decoders/installers keep refusing human/failed.
    continuation_basis: AttemptContinuationBasis | None = None


@dataclass(frozen=True)
class ImportedCompletion:
    records: tuple[SourceRecord, ...]
    publication: Commit
    integrated: Commit
    pr_number: int
    development_attempt: str
    review_attempt: str
    close_intent: bytes
    close_ownership: bytes | None
    close_readproof: bytes | None


@dataclass(frozen=True)
class ImportedResource:
    """A source-owned ref/workspace or an explicitly unused target identity.

    'absent' means proved absent, never unknown. 'new-unused' belongs only to
    the copied idle contract and grants no original-clone ownership. A retained
    merged workspace can require cleanup despite a settled Completion. The
    fixed cutover implementation decides physical operations; these are not
    caller-programmable action steps.
    """
    owner: SourceRecord
    kind: Literal['run-ref', 'ticket-workspace']
    disposition: Literal['existing', 'absent', 'new-unused']
    source_ref: str
    expected_oid: str | None
    checkout: Path | None
    workspace_oid: str | None
    owned_root: Path | None
    common_git_dir: Path
    before_head: bytes | None  # Observed HEAD bytes; nonce-bound layout is derived later.
    checkout_archive_sha256: str | None
    checkout_index_sha256: str | None
    head_evidence: tuple[SourceRecord, ...]
    cleanup_pending: bool
    checkout_status: tuple[str, ...] | None = None  # None is unobserved, never clean.


@dataclass(frozen=True)
class ImportedHistoryEvent:
    record: SourceRecord
    subject: str
    generation: int
    kind: str
    identity: str
    created_at: float
    body: bytes


@dataclass(frozen=True)
class ImportedSubject:
    subject: str
    issue: int
    generation: int
    record: SourceRecord
    aliases: tuple[SourceRecord, ...]
    kind: Literal['unallocated', 'returned', 'completed']
    historical_base: Commit | None
    budgets: tuple[ImportedBudget, ...]
    attempts: tuple[ImportedAttempt, ...]
    candidate: Commit | None
    integration: bytes | None
    accepted_review: str | None
    completion: ImportedCompletion | None
    resources: tuple[ImportedResource, ...]


@dataclass(frozen=True)
class ImportedRunFacts:
    """One installation input, with explicit source scope and physical mode.

    spec describes native creation layouts/frozen inputs; subjects describes
    only actual imported Ticket work. A SingleTicket may have one Subject.
    Kernel currently requires a Final target definition for a Ticket graph:
    that spec entry is not a source Final Subject, barrier, completion or
    activation. observed_requirements is sealed readback, never future defaults.
    Native relation IDs/audit rows are produced by the installer/projection;
    the original history and raw materials remain available through records.
    """
    source: ImportSource
    spec: RunSpec
    requirements: Requirements
    observed_requirements: Requirements
    created_at: float
    subjects: tuple[ImportedSubject, ...]
    run_resource: ImportedResource
    history: tuple[ImportedHistoryEvent, ...]
