"""Read settled original Attempts by owner and explicit version references."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Mapping

from .migration import MigrationRefused, _created_at_epoch
from .migration_active import _evidence
from .migration_conversion import _one
from .migration_run_facts import BudgetFacts, RunHeader, TicketOwner
from .kernel import Kernel
from .model import KernelError, Role, artifact, wire_json


@dataclass(frozen=True)
class AttemptFacts:
    attempt_id: str
    role: Role
    old_ordinal: int
    pending: bool
    thread_id: str
    binding: Mapping[str, Any]
    invocation: Mapping[str, Any]
    bound_version: Mapping[str, Any]
    selected_version: Mapping[str, Any]
    global_current_version: Mapping[str, Any]
    raw_evidence_id: str
    raw: Mapping[str, Any]
    normalized: Mapping[str, Any]
    allocation_boundary: Mapping[str, Any]


def _normalized(role: Role, raw: Any, thread: str) -> dict[str, Any]:
    wire_publication = (role == Role.PUBLICATION and isinstance(raw, dict)
                        and set(raw) == {'kind', 'artifact'} and raw.get('kind') == 'publication_wire')
    if not isinstance(raw, dict) or not wire_publication and raw.get('thread_id') != thread:
        raise MigrationRefused('source_returned_result')
    expected = {Role.DEVELOPMENT: 'development', Role.REVIEW: 'review', Role.PUBLICATION: 'publication'}[role]
    if not wire_publication and raw.get('kind') != expected:
        raise MigrationRefused('unsupported_source_returned_kind')
    value: Any
    if role == Role.DEVELOPMENT:
        if not isinstance(raw.get('summary'), str):
            raise MigrationRefused('source_returned_result')
        value = {'result_kind': 'development', 'summary': raw['summary'], 'human_blockers': None}
    else:
        value = raw.get('artifact')
    try:
        result = artifact(role, wire_json(value))
    except (KernelError, ValueError, TypeError) as error:
        raise MigrationRefused('unsupported_source_artifact') from error
    if (result.get('result_kind') == 'human_blocker'
            or role == Role.REVIEW and any(check['status'] == 'blocked' for check in result['checks'].values())):
        raise MigrationRefused('unsupported_source_human_return')
    return result


def read_owner_attempts(db: sqlite3.Connection, header: RunHeader, owner: TicketOwner,
                        budget: BudgetFacts, profile: Mapping[str, Any],
                        *, historical_base: str, effective_revision: str,
                        completed: bool) -> tuple[AttemptFacts, ...]:
    """Select history.version for history and current.version only for pending.

    Startup versions remain Invocation evidence even if their status is pending.
    Chronology is checked within this owner, not across independent Tickets.
    """
    if owner.job is None:
        raise MigrationRefused('source_attempt_owner')
    run_id, job_id = owner.job.key
    if not isinstance(run_id, str) or not isinstance(job_id, str):
        raise MigrationRefused('source_attempt_owner')
    roots = _one(db, 'SELECT * FROM work_job_attempt_roots WHERE run_id=? AND job_id=?', (run_id, job_id))
    history = db.execute('SELECT * FROM work_job_attempt_history WHERE run_id=? AND job_id=? ORDER BY position',
                         (run_id, job_id)).fetchall()
    if ([row['position'] for row in history] != list(range(len(history)))
            or roots['history_count'] != len(history) or roots['pending_present'] not in (0, 1)
            or bool(roots['pending_present']) == completed
            or (roots['pending_attempt_id'] is None) != completed):
        raise MigrationRefused('source_attempt_roots')
    selected = [(row['attempt_id'], row['version_id'], False) for row in history]
    if not completed:
        current = _one(db, 'SELECT * FROM work_attempt_current WHERE run_id=? AND attempt_id=?',
                       (run_id, roots['pending_attempt_id']))
        selected.append((roots['pending_attempt_id'], current['version_id'], True))
    if (not selected or len({item[0] for item in selected}) != len(selected)
            or {item[0] for item in selected} != {row[0] for row in db.execute(
                'SELECT attempt_id FROM work_attempts WHERE run_id=? AND job_id=?', (run_id, job_id))}):
        raise MigrationRefused('source_attempt_set')
    raw_bindings = profile.get('bindings')
    profiles = profile.get('profiles')
    if not isinstance(raw_bindings, list) or not isinstance(profiles, dict):
        raise MigrationRefused('source_profile_bindings')
    bindings = {row.get('binding_id'): row for row in raw_bindings if isinstance(row, dict)}
    if len(bindings) != len(raw_bindings):
        raise MigrationRefused('source_profile_bindings')
    calls = []
    previous_end = 0.0
    ordinals: dict[Role, int] = {}
    developments = 0
    charged_reviews = 0
    development_threads: set[str] = set()
    for attempt_id, version_id, pending in selected:
        attempt = _one(db, 'SELECT * FROM work_attempts WHERE run_id=? AND attempt_id=?', (run_id, attempt_id))
        role = {'development': Role.DEVELOPMENT, 'reviewer': Role.REVIEW,
                'publication': Role.PUBLICATION}.get(attempt['role'])
        if (role is None or attempt['job_id'] != job_id or attempt['work_subject'] != owner.subject
                or attempt['generation'] != owner.generation
                or attempt['ordinal'] != ordinals.get(role, 0) + 1
                or attempt['budget_window'] != (None if role == Role.PUBLICATION else budget.window)):
            raise MigrationRefused('source_attempt_identity')
        ordinals[role] = attempt['ordinal']
        version = _one(db, 'SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?',
                       (run_id, attempt_id, version_id))
        current = _one(db, 'SELECT * FROM work_attempt_current WHERE run_id=? AND attempt_id=?', (run_id, attempt_id))
        global_current = _one(db, 'SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?',
                              (run_id, attempt_id, current['version_id']))
        returned = _one(db, 'SELECT * FROM work_attempt_returns WHERE run_id=? AND attempt_id=?', (run_id, attempt_id))
        invocation = _one(db, 'SELECT * FROM work_invocations WHERE run_id=? AND work_subject=? AND attempt_id=?',
                          (run_id, owner.subject, attempt_id))
        bound = _one(db, 'SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?',
                     (run_id, attempt_id, invocation['attempt_version_id']))
        _one(db, 'SELECT * FROM work_invocation_history WHERE run_id=? AND work_subject=? AND started_at=?',
             (run_id, owner.subject, invocation['started_at']))
        if (version['status'] != ('pending' if pending else 'completed')
                or version['returned_result_evidence_id'] != (returned['evidence_id'] if pending else None)
                or invocation['generation'] != owner.generation
                or invocation['raw_result_evidence_id'] != returned['evidence_id']
                or invocation['status'] != 'completed' or invocation['ended_at'] is None
                or invocation['error'] is not None or invocation['execution_interrupted'] != 0
                or invocation['recovery_waiting'] != 0 or invocation['return_code'] is not None
                or invocation['signal'] is not None or invocation['profile_revision'] != 1
                or invocation['execution_segments_null'] != 0 or invocation['resume_pending'] != 0
                or bound['status'] != 'pending' or bound['returned_result_evidence_id'] is not None
                or bound['version_id'] == version_id):
            raise MigrationRefused('unsupported_source_invocation')
        started = _created_at_epoch(invocation['started_at'])
        ended = _created_at_epoch(invocation['ended_at'])
        if not previous_end <= started <= ended:
            raise MigrationRefused('source_invocation_chronology')
        previous_end = ended
        thread = invocation['reported_thread_id']
        binding = bindings.get(invocation['binding_id'])
        binding_role = invocation['binding_role']
        if (not isinstance(thread, str) or not thread or not isinstance(binding, dict)
                or binding_role not in {'development', 'review', 'publication'}
                or binding.get('role') != binding_role or binding.get('thread_id') != thread
                or binding.get('profile_revision') != 1
                or invocation['profile_role'] != binding_role
                or invocation['invocation_role'] != role.value
                or binding.get('model') != invocation['model']
                or binding.get('reasoning_effort') != invocation['reasoning_effort']
                or invocation['model'] != profiles[binding_role]['model']
                or invocation['reasoning_effort'] != profiles[binding_role]['reasoning_effort']
                or (role != Role.PUBLICATION and binding_role != role.value)
                or (invocation['mode'] == 'resume' and invocation['requested_thread_id'] != thread)
                or invocation['mode'] not in {'fresh', 'resume'}):
            raise MigrationRefused('source_profile_bindings')
        if role == Role.DEVELOPMENT:
            developments += 1
            development_threads.add(thread)
        if role == Role.PUBLICATION and binding_role == 'development' and (
                thread not in development_threads or invocation['mode'] != 'resume'):
            raise MigrationRefused('source_publication_thread')
        invocation_owner = 'invocation:' + json.dumps([owner.subject, invocation['started_at']], separators=(',', ':'))
        boundary = _evidence(db, run_id, invocation['currentness_boundary_evidence_id'],
                             owner=invocation_owner, kind='currentness_boundary')
        execution_binding = _evidence(db, run_id, invocation['thread_execution_binding_evidence_id'],
                                      owner=invocation_owner, kind='thread_execution_binding')
        if (not isinstance(boundary, dict) or boundary.get('base_sha') != historical_base
                or boundary.get('effective_revision') != effective_revision
                or attempt['currentness_boundary_fingerprint'] != Kernel._fingerprint(boundary)
                or not isinstance(execution_binding, dict)
                or execution_binding != binding):
            raise MigrationRefused('source_allocation_boundary')
        if role == Role.REVIEW:
            context = _evidence(db, run_id, bound['review_context_evidence_id'],
                                owner='attempt:' + attempt_id, kind='review_context')
            identity = context.get('facts', {}).get('current_review_identity') if isinstance(context, dict) else None
            if (not isinstance(identity, dict) or identity.get('reviewed_base_sha') != historical_base
                    or identity.get('reviewed_candidate_sha') != boundary.get('candidate_sha')
                    or identity.get('reviewed_candidate_tree') != boundary.get('candidate_tree')
                    or version['review_context_evidence_id'] != bound['review_context_evidence_id']
                    or version['budget_consumed'] not in (None, 0, 1)):
                raise MigrationRefused('source_review_identity')
            charged_reviews += int(version['budget_consumed'] == 1)
        raw = _evidence(db, run_id, returned['evidence_id'], owner='attempt:' + attempt_id, kind='returned_result')
        normalized = _normalized(role, raw, thread)
        if not pending:
            expected_outcomes = {Role.DEVELOPMENT: {'candidate'}, Role.REVIEW: {'acceptance_artifact', 'findings'},
                                 Role.PUBLICATION: {'publication_artifact'}}[role]
            if version['outcome'] not in expected_outcomes:
                raise MigrationRefused('unsupported_source_attempt_outcome')
        calls.append(AttemptFacts(attempt_id, role, attempt['ordinal'], pending, thread, binding,
            dict(invocation), dict(bound), dict(version), dict(global_current), returned['evidence_id'],
            raw, normalized, boundary))
    if (developments != budget.developments_used or charged_reviews != budget.reviews_used
            or len(calls) != db.execute('SELECT count(*) FROM work_invocations WHERE run_id=? AND work_subject=?',
                                        (run_id, owner.subject)).fetchone()[0]):
        raise MigrationRefused('source_budget_accounting')
    return tuple(calls)
