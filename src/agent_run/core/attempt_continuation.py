"""Attempt-owned native input and binding; execution facts come from Invocations.

Opening facts are immutable history, not current work or dispatch authority.
The current result and Invocation still belong to the existing Attempt row.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import sqlite3
from typing import Any, Mapping

from agent_run.errors import KernelError
from agent_run.model import wire_json
from agent_run.storage.sqlite import Transaction
from agent_run.storage.attempts import AttemptRecords


@dataclass(frozen=True)
class PriorExecution:
    invocation_id: str | None
    status: str
    ordinal: int
    dispatch_state: str
    raw_id: str | None
    process_identity: str | None
    exit_code: int | None
    failure_kind: str | None
    recovery_ready_at: float | None
    start_evidence: str | None
    deadline_seconds: float
    deadline_at: float
    lease_id: str | None = None


@dataclass(frozen=True)
class AttemptContinuationBasis:
    thread_id: str
    context_id: str


@dataclass(frozen=True)
class ContinuationFacts:
    thread_id: str
    context: Mapping[str, Any]
    prior: PriorExecution | None
    ordinary_used: int
    capacity_count: int
    output_ordinal: int
    semantic_used: int
    semantic_deadline: float | None


def _thread(records: AttemptRecords, attempt: sqlite3.Row, thread_id: str, kind: str) -> sqlite3.Row:
    thread = records.thread(thread_id)
    if (thread['subject'] != attempt['subject'] or not (thread['role'] == attempt['role']
            or attempt['role'] == 'publication' and thread['role'] == 'development' and kind != 'final_run')):
        raise KernelError('continuation_binding')
    revision = records.profile_revision(thread['profile_revision'])
    profile = json.loads(records.transaction.get(revision['material_id'], revision['subject']))
    if thread['role'] not in profile['profiles']:
        raise KernelError('continuation_binding')
    return thread


def _context(records: AttemptRecords, attempt: sqlite3.Row, kind: str, context_id: str) -> dict[str, Any]:
    context = json.loads(records.transaction.get(context_id, attempt['subject']))
    required = {'repository', 'parent_issue', 'issue', 'scope', 'generation', 'role', 'purpose',
                'revision', 'parent_revision', 'graph_revision', 'completion_revision', 'base_sha',
                'candidate_sha', 'candidate_tree', 'expected_tree', 'checkout', 'readonly'}
    if not isinstance(context, dict) or not required <= context.keys():
        raise KernelError('continuation_basis_missing')
    if (any(context[key] != attempt[key] for key in ('generation', 'role', 'purpose', 'revision',
            'parent_revision', 'graph_revision', 'completion_revision', 'base_sha', 'candidate_sha', 'candidate_tree', 'expected_tree'))
            or context['scope'] != kind):
        raise KernelError('continuation_input_changed')
    return context


_NATIVE_OPENING = wire_json({"provenance_id": None, "ordinary_used": 0, "capacity_count": 0, "prior": None})


def validate_native_opening(body: bytes) -> None:
    """Accept exactly the native writer's bytes, without decoding imported facts."""
    if body != _NATIVE_OPENING:
        raise KernelError('continuation_opening')


def bind_attempt_continuation(tx: Transaction, attempt_id: str, basis: AttemptContinuationBasis) -> None:
    """Bind native input once, without choosing work, charging or allocating."""
    records = AttemptRecords(tx)
    attempt = records.attempt(attempt_id)
    kind = records.subject_kind(attempt['subject'])['kind']
    _thread(records, attempt, basis.thread_id, kind)
    _context(records, attempt, kind, basis.context_id)
    if (attempt['status'] != 'allocated' or attempt['result_id'] is not None
            or attempt['outcome'] is not None or attempt['accounted'] or attempt['resume_action'] is not None):
        raise KernelError('continuation_already_started')
    material = tx.put(attempt['subject'], 'attempt_continuation_opening', _NATIVE_OPENING)
    if attempt['thread_id'] is not None or attempt['opening_id'] is not None:
        if (attempt['thread_id'], attempt['opening_id'], attempt['context_id']) != (basis.thread_id, material, basis.context_id):
            raise KernelError('continuation_already_bound')
        return
    if attempt['current_invocation'] is not None or records.has_invocation(attempt_id):
        raise KernelError('continuation_already_started')
    records.bind_opening(attempt_id, basis.thread_id, material, basis.context_id)


def _semantic_history(records: AttemptRecords, attempt: sqlite3.Row, kind: str,
                      current_thread: sqlite3.Row) -> tuple[int, float | None]:
    # Transaction.all already enforces the common query/row/byte limits. A
    # predecessor is meaningful only within this one Attempt's linear history.
    rows = records.invocation_lineage(attempt['id'])
    by_id = {row['id']: row for row in rows}
    cursor, seen, chain = attempt['current_invocation'], set(), []
    while cursor is not None:
        if cursor in seen or cursor not in by_id:
            raise KernelError('continuation_lineage')
        seen.add(cursor)
        row = by_id[cursor]
        chain.append(row)
        cursor = row['predecessor']
    if len(seen) != len(rows) or len(by_id) != len(rows):
        raise KernelError('continuation_lineage')
    used, deadline, parent = 0, None, None
    threads = {current_thread['id']}
    for row in reversed(chain):
        if row['thread'] not in threads:
            _thread(records, attempt, row['thread'], kind)
            threads.add(row['thread'])
        if (not isinstance(row['deadline_at'], (int, float)) or not math.isfinite(row['deadline_at'])
                or row['recovery_kind'] not in {None, 'ordinary', 'capacity', 'explicit', 'semantic'}):
            raise KernelError('continuation_lineage')
        if row['recovery_kind'] == 'explicit':
            deadline = None
        elif row['recovery_kind'] == 'semantic':
            if parent is None or row['thread'] != parent['thread']:
                raise KernelError('continuation_lineage')
            used += 1
            deadline = parent['deadline_at']
        elif parent is None and row['recovery_kind'] in {'ordinary', 'capacity'}:
            raise KernelError('continuation_lineage')
        if used > 1 or deadline is not None and row['deadline_at'] != deadline:
            raise KernelError('continuation_lineage')
        parent = row
    return used, deadline


def load_continuation(tx: Transaction, attempt_id: str) -> ContinuationFacts:
    """Read and validate one snapshot, with no DML or cache between calls.

    The checked current Thread is also the lineage's current Thread. Reuse
    only these call-local facts; callers must reload after related writes.
    """
    records = AttemptRecords(tx)
    attempt = records.attempt(attempt_id)
    if attempt['thread_id'] is None or attempt['opening_id'] is None:
        raise KernelError('continuation_basis_missing')
    kind = records.subject_kind(attempt['subject'])['kind']
    thread = _thread(records, attempt, attempt['thread_id'], kind)
    context = _context(records, attempt, kind, attempt['context_id'])
    validate_native_opening(tx.get(attempt['opening_id'], attempt['subject']))
    prior = None
    if attempt['current_invocation'] is not None:
        row = records.latest_output(attempt['current_invocation'], attempt_id)
        if row['thread'] != attempt['thread_id']:
            raise KernelError('continuation_binding')
        prior = PriorExecution(row['id'], row['status'], row['ordinal'], row['dispatch_state'], row['raw_id'],
            row['process_identity'], row['exit_code'], row['failure_kind'], row['recovery_ready_at'], row['start_evidence'],
            row['deadline_seconds'], row['deadline_at'], row['lease'])
    counts = records.recovery_counts(attempt_id)
    ordinal = records.output_ordinal(attempt_id)
    semantic_used, semantic_deadline = _semantic_history(records, attempt, kind, thread)
    return ContinuationFacts(attempt['thread_id'], context, prior, counts[0], counts[1], ordinal,
                             semantic_used, semantic_deadline)
