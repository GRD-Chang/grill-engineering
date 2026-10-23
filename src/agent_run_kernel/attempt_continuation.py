"""Attempt-owned input and binding, plus certified opening execution facts.

Opening facts are immutable history, not current work or dispatch authority.
The current result and Invocation still belong to the existing Attempt row.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import sqlite3
from typing import Any, Mapping

from .model import KernelError, wire_json
from .storage import Transaction


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
class ContinuationOpening:
    provenance_id: str | None = None
    ordinary_used: int = 0
    capacity_count: int = 0
    prior: PriorExecution | None = None


@dataclass(frozen=True)
class AttemptContinuationBasis:
    thread_id: str
    context_id: str
    opening: ContinuationOpening


@dataclass(frozen=True)
class ContinuationFacts:
    thread_id: str
    context: Mapping[str, Any]
    prior: PriorExecution | None
    ordinary_used: int
    capacity_count: int
    output_ordinal: int


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


def _opening(tx: Transaction, attempt: sqlite3.Row, value: ContinuationOpening) -> None:
    if (type(value.ordinary_used) is not int or value.ordinary_used not in (0, 1)
            or type(value.capacity_count) is not int or value.capacity_count < 0):
        raise KernelError('continuation_opening')
    if value.provenance_id is None:
        if value != ContinuationOpening():
            raise KernelError('continuation_provenance_missing')
        return
    provenance = json.loads(tx.get(value.provenance_id, attempt['subject']))
    if (not isinstance(provenance, dict) or provenance.get('attempt') != attempt['id']
            or any(provenance.get(key) != attempt[key] for key in ('subject', 'generation', 'role', 'purpose', 'window'))):
        raise KernelError('continuation_provenance_missing')
    prior = value.prior
    if prior is None:
        raise KernelError('continuation_exit_missing')
    if (prior.invocation_id is not None or prior.lease_id is not None or prior.status not in {'returned', 'interrupted'}
            or type(prior.ordinal) is not int or not 0 <= prior.ordinal <= 2
            or prior.dispatch_state not in {'exited', 'unavailable', 'unknown', 'allocated'}
            or prior.failure_kind not in {None, 'ordinary', 'capacity', 'fatal'}
            or prior.exit_code is not None and type(prior.exit_code) is not int
            or any(type(number) not in (int, float) or not math.isfinite(number)
                   for number in (prior.deadline_seconds, prior.deadline_at))
            or prior.deadline_seconds <= 0
            or prior.recovery_ready_at is not None and (type(prior.recovery_ready_at) not in (int, float)
                or not math.isfinite(prior.recovery_ready_at))
            or prior.failure_kind == 'capacity' and prior.recovery_ready_at is None):
        raise KernelError('continuation_opening')
    for material in (prior.raw_id, prior.process_identity, prior.start_evidence):
        if material is not None:
            tx.get(material, attempt['subject'])


def bind_attempt_continuation(tx: Transaction, attempt_id: str, basis: AttemptContinuationBasis) -> None:
    """Bind certified facts once, without choosing work, charging or allocating."""
    attempt = tx.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
    _context(tx, attempt, basis.thread_id, basis.context_id)
    _opening(tx, attempt, basis.opening)
    if basis.opening.provenance_id is None and (attempt['status'] != 'allocated' or attempt['result_id'] is not None
            or attempt['outcome'] is not None or attempt['accounted'] or attempt['resume_action'] is not None):
        raise KernelError('continuation_provenance_missing')
    if attempt['result_id'] is not None and (basis.opening.prior is None or basis.opening.prior.raw_id != attempt['result_id']):
        raise KernelError('continuation_return_changed')
    material = tx.put(attempt['subject'], 'attempt_continuation_opening', wire_json(asdict(basis.opening)))
    if attempt['thread_id'] is not None or attempt['opening_id'] is not None:
        if (attempt['thread_id'], attempt['opening_id'], attempt['context_id']) != (basis.thread_id, material, basis.context_id):
            raise KernelError('continuation_already_bound')
        return
    if attempt['current_invocation'] is not None or tx.rows("SELECT id FROM invocations WHERE attempt=? LIMIT 1", (attempt_id,)):
        raise KernelError('continuation_already_started')
    tx.execute("UPDATE attempts SET thread_id=?,opening_id=?,context_id=? WHERE id=?",
               (basis.thread_id, material, basis.context_id, attempt_id))


def load_continuation(tx: Transaction, attempt_id: str) -> ContinuationFacts:
    attempt = tx.one("SELECT * FROM attempts WHERE id=?", (attempt_id,))
    if attempt['thread_id'] is None or attempt['opening_id'] is None:
        raise KernelError('continuation_basis_missing')
    context = _context(tx, attempt, attempt['thread_id'], attempt['context_id'])
    data = json.loads(tx.get(attempt['opening_id'], attempt['subject']))
    try:
        data['prior'] = None if data['prior'] is None else PriorExecution(**data['prior'])
        opening = ContinuationOpening(**data)
    except (KeyError, TypeError) as error:
        raise KernelError('continuation_opening') from error
    _opening(tx, attempt, opening)
    prior = opening.prior
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
    return ContinuationFacts(attempt['thread_id'], context, prior, opening.ordinary_used + counts[0],
        opening.capacity_count + counts[1], max(ordinal, 0 if opening.prior is None else opening.prior.ordinal))
