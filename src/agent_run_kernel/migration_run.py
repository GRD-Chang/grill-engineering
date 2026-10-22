"""Fact conversion for a canonical original whole Run.

Only proved source facts enter the unpublished native database. Scheduling,
result consumption, budget charging and Final activation remain in Core.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping

from .migration import MigrationRefused, SourceSnapshot
from .migration_active import _RETURNED_WRITE_FIELDS, _evidence, _read_profile
from .migration_completed import _completed_evidence
from .migration_conversion import _one
from .migration_run_attempts import AttemptFacts, read_owner_attempts
from .migration_run_facts import BudgetFacts, RunHeader, SourceKey, TicketOwner, read_run_header, read_ticket_budget, read_ticket_owners
from .model import Role
from .kernel import Kernel


@dataclass(frozen=True)
class UnallocatedTicket:
    owner: TicketOwner


@dataclass(frozen=True)
class WorkedTicket:
    owner: TicketOwner
    kind: Literal['completed', 'returned']
    job: Mapping[str, Any]
    budget: BudgetFacts
    calls: tuple[AttemptFacts, ...]
    base_sha: str
    source_ref: str
    candidate_sha: str | None
    candidate_tree: str | None
    integration_target: str | None
    integration: Mapping[str, Any] | None
    accepted_review: str | None
    close_intent: Mapping[str, Any] | None
    close_ownership: Mapping[str, Any] | None
    cleanup: Mapping[str, Any] | None


@dataclass(frozen=True)
class OriginalRunFacts:
    header: RunHeader
    source_sha256: str
    profile_sha256: str
    profile: Mapping[str, Any]
    tickets: tuple[UnallocatedTicket | WorkedTicket, ...]


def _read_acceptance(db: sqlite3.Connection, header: RunHeader, job: sqlite3.Row,
                     calls: tuple[AttemptFacts, ...]) -> tuple[str, str, Mapping[str, Any]]:
    run_id, job_id, base = header.run_id, job['job_id'], job['value_base_sha']
    accepted = _one(db, 'SELECT * FROM work_job_acceptance_record WHERE run_id=? AND job_id=?', (run_id, job_id))
    review = _one(db, 'SELECT * FROM work_job_review_artifacts WHERE run_id=? AND job_id=? AND artifact_id=?',
                  (run_id, job_id, job['value_acceptance_artifact']))
    value = _completed_evidence(db, run_id, accepted['value_artifact'],
                                owner='job:' + job_id, kind='acceptance_record.artifact')
    support = [call for call in calls if call.role == Role.REVIEW and not call.pending
               and call.thread_id == accepted['value_reviewer_thread_id'] and call.raw.get('artifact') == value]
    if (len(support) != 1 or accepted['value_acceptance_scope'] != 'change_job'
            or accepted['value_effective_revision'] != job['value_effective_revision']
            or accepted['value_reviewed_base_sha'] != base
            or accepted['value_reviewed_candidate_sha'] != job['value_candidate_sha']
            or not isinstance(accepted['value_reviewed_candidate_tree'], str)
            or review['candidate_sha'] != job['value_candidate_sha'] or review['reviewed_base_sha'] != base
            or review['reviewer_thread_id'] != accepted['value_reviewer_thread_id']
            or any(check.get('status') != 'pass' for check in support[0].normalized['checks'].values())):
        raise MigrationRefused('source_acceptance')
    selected = support[0]
    if (selected.allocation_boundary.get('candidate_sha') != job['value_candidate_sha']
            or selected.allocation_boundary.get('candidate_tree') != accepted['value_reviewed_candidate_tree']
            or selected.allocation_boundary.get('effective_revision') != job['value_effective_revision']):
        raise MigrationRefused('source_acceptance_identity')
    integration = _evidence(db, run_id, job['value_ticket_integration'], owner='job:' + job_id, kind='ticket_integration')
    resync = _one(db, 'SELECT * FROM work_job_integration_resync WHERE run_id=? AND job_id=?', (run_id, job_id))
    if (not isinstance(integration, dict) or integration.get('base_sha') != base
            or integration.get('source_sha') != base
            or integration.get('candidate_sha') != job['value_candidate_sha']
            or integration.get('head_sha') != job['value_candidate_sha']
            or integration.get('candidate_tree') != accepted['value_reviewed_candidate_tree']
            or integration.get('head_tree') != accepted['value_reviewed_candidate_tree']
            or integration.get('target_sha') != base or integration.get('conflict_paths') != []
            or resync['value_generation'] != 1 or resync['value_window'] != 1 or resync['value_used'] != 0
            or resync['value_limit'] != header.policy.resync_limit or resync['value_open'] != 0
            or resync['value_source_sha'] != base or resync['value_target_sha'] != base):
        raise MigrationRefused('unsupported_source_integration')
    return selected.attempt_id, accepted['value_reviewed_candidate_tree'], integration


def _read_completed(db: sqlite3.Connection, header: RunHeader, job: sqlite3.Row,
                     calls: tuple[AttemptFacts, ...], candidate_tree: str) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    run_id, job_id, base = header.run_id, job['job_id'], job['value_base_sha']
    if (calls[-1].role != Role.PUBLICATION or job['value_ticket_closed_by_run'] != 1
            or type(job['value_pr_number']) is not int or job['value_pr_number'] < 1
            or not isinstance(job['value_publication_sha'], str)
            or not isinstance(job['value_integrated_sha'], str)
            or job['value_integrated_tree'] != candidate_tree):
        raise MigrationRefused('source_completion')
    integrated = _completed_evidence(db, run_id, job['value_deterministic_integration_record'],
                                     owner='job:' + job_id, kind='deterministic_integration_record')
    merge = _one(db, 'SELECT * FROM work_job_merge_intent WHERE run_id=? AND job_id=?', (run_id, job_id))
    expected = {'base_sha': base, 'candidate_sha': job['value_candidate_sha'], 'candidate_tree': candidate_tree,
        'publication_sha': job['value_publication_sha'], 'integrated_publication_sha': job['value_publication_sha'],
        'integrated_sha': job['value_integrated_sha'], 'integrated_tree': candidate_tree,
        'integrated_parents': [base], 'pr_number': job['value_pr_number']}
    if (not isinstance(integrated, dict) or any(integrated.get(key) != value for key, value in expected.items())
            or job['value_integrated_publication_sha'] != job['value_publication_sha']
            or job['value_publication_parent_sha'] != base
            or merge['value_head_sha'] != job['value_publication_sha'] or merge['value_base_sha'] != base
            or merge['value_base_branch'] != header.run_branch
            or merge['value_effective_revision'] != job['value_effective_revision']):
        raise MigrationRefused('source_integration_identity')
    pr = integrated.get('pr')
    if (not isinstance(pr, dict) or pr.get('head_sha') != job['value_publication_sha']
            or pr.get('merge_commit_sha') != job['value_integrated_sha']
            or pr.get('number') != job['value_pr_number'] or pr.get('base_sha') != base):
        raise MigrationRefused('source_integration_identity')
    for field, kind, expected_write in (
            ('value_linked_branch_display', 'linked_branch_display', {'status': 'linked'}),
            ('value_branch_publication_observation', 'branch_publication_observation',
             {'disposition': 'published', 'branch': job['value_ticket_branch'], 'head_sha': job['value_publication_sha']})):
        observed = _completed_evidence(db, run_id, job[field], owner='job:' + job_id, kind=kind)
        if not isinstance(observed, dict) or any(observed.get(key) != value for key, value in expected_write.items()):
            raise MigrationRefused('unsupported_source_effect_outcome')
    intent = _one(db, 'SELECT * FROM work_job_ticket_close_intent WHERE run_id=? AND job_id=?', (run_id, job_id))
    ownership = _one(db, 'SELECT * FROM work_job_ticket_close_ownership WHERE run_id=? AND job_id=?', (run_id, job_id))
    binding = f"pr-{job['value_pr_number']}:sha-{job['value_integrated_sha']}"
    if (intent['value_intent_binding'] != binding or ownership['value_intent_binding'] != binding
            or intent['value_dispatch_attempted'] != 1 or intent['value_actor'] != ownership['value_actor']
            or type(ownership['value_event_id']) is not int or ownership['value_event_id'] < 1):
        raise MigrationRefused('source_close_ownership')
    cleanup = _one(db, 'SELECT * FROM run_cleanup_items WHERE run_id=? AND branch=?',
                   (run_id, job['value_ticket_branch']))
    if (cleanup['value_status'] != 'completed' or cleanup['value_expected_head_sha'] != job['value_publication_sha']
            or cleanup['value_kind'] != 'ticket'):
        raise MigrationRefused('source_cleanup')
    return dict(intent), dict(ownership), dict(cleanup)


def _read_worked_ticket(db: sqlite3.Connection, header: RunHeader, owner: TicketOwner,
                        profile: Mapping[str, Any]) -> WorkedTicket:
    assert owner.job is not None
    job = _one(db, 'SELECT * FROM work_jobs WHERE run_id=? AND job_id=?', owner.job.key)
    if db.execute('SELECT 1 FROM work_job_gates WHERE run_id=? AND job_id=? LIMIT 1', owner.job.key).fetchone():
        raise MigrationRefused('unsupported_source_job_gate')
    issue = next(item for item in header.requirements.issues if item.number == owner.ticket)
    expected_revision = Kernel._fingerprint({'ticket': Kernel._fingerprint({'title': issue.title, 'body': issue.body}),
        'parent': header.parent_revision, 'graph': header.graph_revision})
    if job['value_effective_revision'] != expected_revision:
        raise MigrationRefused('source_effective_revision')
    completed = job['stage'] == 'completed'
    role = {'developing': Role.DEVELOPMENT, 'reviewing': Role.REVIEW, 'accepted': Role.PUBLICATION}.get(job['stage'])
    if (not completed and role is None or job['value_ticket_branch_initialized'] != 1
            or job['value_ticket_branch_generation'] != 1
            or job['value_ticket_branch'] != f'agent-run/{header.run_id}/ticket-{owner.ticket}'
            or not isinstance(job['value_base_sha'], str) or job['value_attempt_kind'] != 'ordinary'):
        raise MigrationRefused('unsupported_source_stage')
    for field in ('value_action_application_receipt', 'value_ci_repair_publication_receipt',
                  'value_fallback_publication_receipt', 'value_pending_publication_result',
                  'value_pending_review_result', 'value_publication_authority'):
        if job[field] is not None:
            raise MigrationRefused('unsupported_source_write_authority')
    if not completed:
        if any(job[field] is not None for field in _RETURNED_WRITE_FIELDS):
            raise MigrationRefused('unsupported_source_write_authority')
        for table in ('work_job_publication_commit_intent', 'work_job_ticket_write_intent',
                      'work_job_write_intent', 'work_job_run_branch_write_intent',
                      'work_job_integration_publication_intent', 'work_job_merge_intent',
                      'work_job_parent_close_intent', 'work_job_ticket_close_intent'):
            if db.execute(f'SELECT 1 FROM "{table}" WHERE run_id=? AND job_id=? LIMIT 1', owner.job.key).fetchone():
                raise MigrationRefused('unsupported_source_write_authority')
    budget = read_ticket_budget(db, header, owner)
    if budget.developments_used > header.policy.ticket_rounds + 1 or budget.reviews_used > header.policy.ticket_rounds:
        raise MigrationRefused('unsupported_source_budget')
    ci = _one(db, 'SELECT * FROM work_job_ci_recovery WHERE run_id=? AND job_id=?', owner.job.key)
    if (ci['value_window'] != 1 or any(ci[field] != 0 for field in (
            'value_developments_used', 'value_repair_pending', 'value_review_required', 'value_reviewer_invocations'))
            or ci['value_failure_head'] is not None or ci['value_previous_publication_authorization'] is not None
            or db.execute('SELECT 1 FROM work_job_budget_events WHERE run_id=? AND job_id=? LIMIT 1', owner.job.key).fetchone()):
        raise MigrationRefused('unsupported_source_ci_history')
    calls = read_owner_attempts(db, header, owner, budget, profile, historical_base=job['value_base_sha'],
                                effective_revision=expected_revision, completed=completed)
    if not completed and calls[-1].role != role:
        raise MigrationRefused('source_pending_role')
    development = [call for call in calls if call.role == Role.DEVELOPMENT]
    summary = _completed_evidence(db, header.run_id, job['value_development_summary'],
                                  owner='job:' + job['job_id'], kind='development_summary') if job['value_development_summary'] else None
    if (not development
            or job['value_development_thread_id'] != development[-1].thread_id
                and not (role == Role.DEVELOPMENT and job['value_development_thread_id'] is None)
            or summary is not None and summary != development[-1].raw.get('summary')):
        raise MigrationRefused('source_development_support')
    candidate_tree = None
    integration = None
    accepted_review = None
    if completed or role == Role.PUBLICATION:
        accepted_review, candidate_tree, integration = _read_acceptance(db, header, job, calls)
    elif role == Role.REVIEW:
        candidate_tree = calls[-1].allocation_boundary.get('candidate_tree')
        if (not isinstance(candidate_tree, str)
                or calls[-1].allocation_boundary.get('candidate_sha') != job['value_candidate_sha']):
            raise MigrationRefused('source_review_identity')
    elif role == Role.DEVELOPMENT:
        intent = _one(db, 'SELECT * FROM work_job_candidate_commit_intent WHERE run_id=? AND job_id=?', owner.job.key)
        if (intent['value_kind'] != 'candidate_commit' or not intent['value_token']
                or intent['value_attempt'] != calls[-1].old_ordinal
                or intent['value_expected_head'] != job['value_base_sha']):
            raise MigrationRefused('unsupported_source_candidate_intent')
    close_intent = close_ownership = cleanup = None
    if completed:
        assert candidate_tree is not None
        close_intent, close_ownership, cleanup = _read_completed(db, header, job, calls, candidate_tree)
    return WorkedTicket(owner, 'completed' if completed else 'returned', dict(job), budget, calls,
        job['value_base_sha'], 'refs/heads/' + job['value_ticket_branch'], job['value_candidate_sha'],
        candidate_tree, integration['target_sha'] if integration is not None else None,
        integration, accepted_review, close_intent, close_ownership, cleanup)


def read_original_run(snapshot: SourceSnapshot, profile_snapshot: Path) -> OriginalRunFacts:
    profile, profile_sha = _read_profile(profile_snapshot)
    with closing(sqlite3.connect(snapshot.path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        header = read_run_header(db, profile, profile_sha, snapshot.sha256)
        owners = read_ticket_owners(db, header)
        tickets = tuple(UnallocatedTicket(owner) if owner.job is None else _read_worked_ticket(db, header, owner, profile)
                        for owner in owners)
        # All current execution relations must belong to the current owner set.
        # Superseded data remains in records; accepting retired active obligations
        # requires its own retirement proof, outside this first whole-Run unit.
        jobs = {owner.job.key[1] for owner in owners if owner.job is not None}
        for row in db.execute('SELECT job_id FROM work_jobs WHERE run_id=? AND stage IS NOT NULL', (header.run_id,)):
            if row['job_id'] not in jobs:
                raise MigrationRefused('unsupported_source_retired_owner')
        actual_attempts = {call.attempt_id for ticket in tickets if isinstance(ticket, WorkedTicket) for call in ticket.calls}
        if actual_attempts != {row[0] for row in db.execute('SELECT attempt_id FROM work_attempts WHERE run_id=?', (header.run_id,))}:
            raise MigrationRefused('source_attempt_scope')
        calls = [call for ticket in tickets if isinstance(ticket, WorkedTicket) for call in ticket.calls]
        if {(call.invocation['work_subject'], call.invocation['started_at']) for call in calls} != {
                (row[0], row[1]) for row in db.execute('SELECT work_subject,started_at FROM work_invocations WHERE run_id=?',
                                                     (header.run_id,))}:
            raise MigrationRefused('unsupported_source_execution_history')
        used_bindings = {call.invocation['binding_id'] for ticket in tickets if isinstance(ticket, WorkedTicket) for call in ticket.calls}
        if used_bindings != {entry['binding_id'] for entry in profile['bindings']}:
            raise MigrationRefused('unsupported_source_profile_bindings')
        return OriginalRunFacts(header, snapshot.sha256, profile_sha, profile, tickets)
