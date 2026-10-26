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


def _thread(tx: Transaction, attempt: sqlite3.Row, thread_id: str) -> sqlite3.Row:
    thread = tx.one("SELECT * FROM threads WHERE id=?", (thread_id,))
    job = tx.one("SELECT kind FROM subjects WHERE id=?", (attempt['subject'],))
    if (thread['subject'] != attempt['subject'] or not (thread['role'] == attempt['role']
            or attempt['role'] == 'publication' and thread['role'] == 'development' and job['kind'] != 'final_run')):
        raise KernelError('continuation_binding')
    revision = tx.one("SELECT subject,material_id FROM profile_revisions WHERE id=?", (thread['profile_revision'],))
    profile = json.loads(tx.get(revision['material_id'], revision['subject']))
    if thread['role'] not in profile['profiles']:
        raise KernelError('continuation_binding')
    return thread


def _context(tx: Transaction, attempt: sqlite3.Row, thread_id: str, context_id: str) -> dict[str, Any]:
    _thread(tx, attempt, thread_id)
    job = tx.one("SELECT kind FROM subjects WHERE id=?", (attempt['subject'],))
    context = json.loads(tx.get(context_id, attempt['subject']))
    required = {'repository', 'parent_issue', 'issue', 'scope', 'generation', 'role', 'purpose',
                'revision', 'parent_revision', 'graph_revision', 'completion_revision', 'base_sha',
                'candidate_sha', 'candidate_tree', 'expected_tree', 'checkout', 'readonly'}
    if not isinstance(context, dict) or not required <= context.keys():
        raise KernelError('continuation_basis_missing')
    if (any(context[key] != attempt[key] for key in ('generation', 'role', 'purpose', 'revision',
            'parent_revision', 'graph_revision', 'completion_revision', 'base_sha', 'candidate_sha', 'candidate_tree', 'expected_tree'))
            or context['scope'] != job['kind']):
        raise KernelError('continuation_input_changed')
    return context


_NATIVE_OPENING = wire_json({"provenance_id": None, "ordinary_used": 0, "capacity_count": 0, "prior": None})


def validate_native_opening(body: bytes) -> None:
    """Accept exactly the native writer's bytes, without decoding imported facts."""
    if body != _NATIVE_OPENING:
        raise KernelError('continuation_opening')


def bind_attempt_continuation(tx: Transaction, attempt_id: str, basis: AttemptContinuationBasis) -> None:
    """Bind native input once, without choosing work, charging or allocating."""
    attempt = tx.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
    _context(tx, attempt, basis.thread_id, basis.context_id)
    if (attempt['status'] != 'allocated' or attempt['result_id'] is not None
            or attempt['outcome'] is not None or attempt['accounted'] or attempt['resume_action'] is not None):
        raise KernelError('continuation_already_started')
    material = tx.put(attempt['subject'], 'attempt_continuation_opening', _NATIVE_OPENING)
    if attempt['thread_id'] is not None or attempt['opening_id'] is not None:
        if (attempt['thread_id'], attempt['opening_id'], attempt['context_id']) != (basis.thread_id, material, basis.context_id):
            raise KernelError('continuation_already_bound')
        return
    if attempt['current_invocation'] is not None or tx.rows("SELECT id FROM invocations WHERE attempt=? LIMIT 1", (attempt_id,)):
        raise KernelError('continuation_already_started')
    tx.execute("UPDATE attempts SET thread_id=?,opening_id=?,context_id=? WHERE id=?",
               (basis.thread_id, material, basis.context_id, attempt_id))


def _semantic_history(tx: Transaction, attempt: sqlite3.Row) -> tuple[int, float | None]:
    # Transaction.all already enforces the common query/row/byte limits. A
    # predecessor is meaningful only within this one Attempt's linear history.
    rows = tx.all("SELECT id,thread,predecessor,recovery_kind,deadline_at FROM invocations WHERE attempt=?", (attempt['id'],))
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
    threads = set()
    for row in reversed(chain):
        if row['thread'] not in threads:
            _thread(tx, attempt, row['thread'])
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
    attempt = tx.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
    if attempt['thread_id'] is None or attempt['opening_id'] is None:
        raise KernelError('continuation_basis_missing')
    context = _context(tx, attempt, attempt['thread_id'], attempt['context_id'])
    validate_native_opening(tx.get(attempt['opening_id'], attempt['subject']))
    prior = None
    if attempt['current_invocation'] is not None:
        row = tx.one("SELECT i.*,o.* FROM invocations i JOIN invocation_outputs o ON o.invocation=i.id "
                     "WHERE i.id=? AND i.attempt=? ORDER BY ordinal DESC LIMIT 1", (attempt['current_invocation'], attempt_id))
        if row['thread'] != attempt['thread_id']:
            raise KernelError('continuation_binding')
        prior = PriorExecution(row['id'], row['status'], row['ordinal'], row['dispatch_state'], row['raw_id'],
            row['process_identity'], row['exit_code'], row['failure_kind'], row['recovery_ready_at'], row['start_evidence'],
            row['deadline_seconds'], row['deadline_at'], row['lease'])
    counts = tx.one("SELECT coalesce(sum(recovery_kind='ordinary'),0),coalesce(sum(recovery_kind='capacity'),0) "
                    "FROM invocations WHERE attempt=?", (attempt_id,))
    ordinal = tx.one("SELECT coalesce(max(o.ordinal),0) FROM invocation_outputs o JOIN invocations i ON i.id=o.invocation "
                     "WHERE i.attempt=?", (attempt_id,))[0]
    semantic_used, semantic_deadline = _semantic_history(tx, attempt)
    return ContinuationFacts(attempt['thread_id'], context, prior, counts[0], counts[1], ordinal,
                             semantic_used, semantic_deadline)
