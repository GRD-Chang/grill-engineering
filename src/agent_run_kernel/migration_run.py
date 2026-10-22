"""Fact conversion for a canonical original whole Run.

Only proved source facts enter the unpublished native database. Scheduling,
result consumption, budget charging and Final activation remain in Core.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Callable, Literal, Mapping, Sequence, TypeVar

from .migration import MigrationRefused, SourceSnapshot, validate_snapshot
from .migration_active import _RETURNED_WRITE_FIELDS, _evidence, _read_profile, _wire_row
from .migration_completed import _completed_evidence
from .migration_conversion import _git, _one
from .migration_run_attempts import AttemptFacts, _imported_attempt, read_owner_attempts
from .migration_run_facts import BudgetFacts, RunHeader, SourceKey, TicketOwner, read_run_header, read_ticket_budget, read_ticket_owners
from .migration_facts import (ImportSource, ImportedBudget, ImportedCompletion,
    ImportedResource, ImportedRunFacts, ImportedSubject, SourceRecord)
from .model import Commit, Delivery, DeliveryKind, Purpose, Role, RunSpec, wire_json
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
    snapshot: SourceSnapshot
    profile_document: bytes


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


_ReadTicket = TypeVar('_ReadTicket')


def _read_worked_source(db: sqlite3.Connection, header: RunHeader, owner: TicketOwner,
                        profile: Mapping[str, Any], emit: Callable[..., _ReadTicket]) -> _ReadTicket:
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
    return emit(owner, 'completed' if completed else 'returned', dict(job), budget, calls,
        job['value_base_sha'], 'refs/heads/' + job['value_ticket_branch'], job['value_candidate_sha'],
        candidate_tree, integration['target_sha'] if integration is not None else None,
        integration, accepted_review, close_intent, close_ownership, cleanup)


def _read_worked_ticket(db: sqlite3.Connection, header: RunHeader, owner: TicketOwner,
                        profile: Mapping[str, Any]) -> WorkedTicket:
    return _read_worked_source(db, header, owner, profile, WorkedTicket)


def _check_run_source_scope(db: sqlite3.Connection, header: RunHeader, owners: tuple[TicketOwner, ...],
                            calls: Sequence[AttemptFacts], profile: Mapping[str, Any]) -> None:
    jobs = {owner.job.key[1] for owner in owners if owner.job is not None}
    for row in db.execute('SELECT job_id FROM work_jobs WHERE run_id=? AND stage IS NOT NULL', (header.run_id,)):
        if row['job_id'] not in jobs:
            raise MigrationRefused('unsupported_source_retired_owner')
    if {call.attempt_id for call in calls} != {row[0] for row in db.execute(
            'SELECT attempt_id FROM work_attempts WHERE run_id=?', (header.run_id,))}:
        raise MigrationRefused('source_attempt_scope')
    if {(call.invocation['work_subject'], call.invocation['started_at']) for call in calls} != {
            (row[0], row[1]) for row in db.execute('SELECT work_subject,started_at FROM work_invocations WHERE run_id=?',
                                                 (header.run_id,))}:
        raise MigrationRefused('unsupported_source_execution_history')
    if {call.invocation['binding_id'] for call in calls} != {entry['binding_id'] for entry in profile['bindings']}:
        raise MigrationRefused('unsupported_source_profile_bindings')


def read_original_run(snapshot: SourceSnapshot, profile_snapshot: Path) -> OriginalRunFacts:
    profile, profile_sha = _read_profile(profile_snapshot)
    profile_document = profile_snapshot.read_bytes()
    if hashlib.sha256(profile_document).hexdigest() != profile_sha:
        raise MigrationRefused('source_profile_changed')
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
        calls = [call for ticket in tickets if isinstance(ticket, WorkedTicket) for call in ticket.calls]
        _check_run_source_scope(db, header, owners, calls, profile)
        return OriginalRunFacts(header, snapshot.sha256, profile_sha, profile, tickets, snapshot, profile_document)


def read_original_run_import(snapshot: SourceSnapshot, profile_snapshot: Path, *,
        external_snapshot: Path, checkouts_snapshot: Path) -> ImportedRunFacts:
    """Decode a whole original source before transfer, without a legacy DTO hop.

    SQL and external checks are shared with the existing path. These facts do
    not replace the install owner's mandatory complete resource authentication,
    nonce/fence, prepared, pointer or inverse checks. Checkout artifacts are
    read-only inputs from inspection, never action steps or new authority.
    """
    from agent_run.managed_workspace import ManagedWorkspace
    from .migration_completed import _read_document
    from .migration_run_external import verify_imported_run_external
    validate_snapshot(snapshot)
    profile, profile_sha = _read_profile(profile_snapshot)
    profile_document = profile_snapshot.read_bytes()
    external, external_sha = _read_document(external_snapshot)
    external_document = external_snapshot.read_bytes()
    sealed, sealed_sha = _read_document(checkouts_snapshot)
    if (hashlib.sha256(profile_document).hexdigest() != profile_sha
            or hashlib.sha256(external_document).hexdigest() != external_sha):
        raise MigrationRefused('source_documents_changed')
    with closing(sqlite3.connect(snapshot.path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        header = read_run_header(db, profile, profile_sha, snapshot.sha256)
        owners = read_ticket_owners(db, header)
        managed = ManagedWorkspace.for_repository(header.repository)
        root, common = managed.repository_root, managed.repository_root / '.git'
        worktrees = managed.state_root / 'worktrees' / header.run_id
        all_calls: list[AttemptFacts] = []

        def resource(owner: TicketOwner, *, existing: bool, source_ref: str, expected_oid: str | None) -> ImportedResource:
            key = owner.job or owner.graph_record
            checkout = worktrees / f'ticket-{owner.ticket}'
            archive_sha = index_sha = before_head = None
            workspace_oid = None
            if existing:
                data = sealed.get(owner.subject)
                if (not isinstance(data, dict) or set(data) != {'checkout_archive_sha256', 'checkout_index_sha256'}
                        or any(not isinstance(value, str) for value in data.values())):
                    raise MigrationRefused('source_checkout_archive')
                archive_sha, index_sha = data['checkout_archive_sha256'], data['checkout_index_sha256']
                folder = checkouts_snapshot.parent / f'ticket-{owner.ticket}'
                if (hashlib.sha256((folder / 'checkout.tar').read_bytes()).hexdigest() != archive_sha
                        or hashlib.sha256((folder / 'checkout-index').read_bytes()).hexdigest() != index_sha):
                    raise MigrationRefused('source_checkout_archive')
                workspace_oid = _git(checkout, 'rev-parse', 'HEAD')
                before_head = Path(_git(checkout, 'rev-parse', '--path-format=absolute', '--git-path', 'HEAD')).read_bytes()
                if workspace_oid != expected_oid:
                    raise MigrationRefused('source_existing_checkout')
            elif checkout.exists() or checkout.is_symlink() or _git(root, 'for-each-ref', '--format=%(refname)', source_ref):
                raise MigrationRefused('source_resource_absence')
            return ImportedResource(owner=SourceRecord(key.table, key.key), kind='ticket-workspace',
                disposition='existing' if existing else 'absent', source_ref=source_ref,
                expected_oid=expected_oid if existing else None, checkout=checkout if existing else None,
                workspace_oid=workspace_oid, owned_root=worktrees if existing else None, common_git_dir=common,
                before_head=before_head, checkout_archive_sha256=archive_sha, checkout_index_sha256=index_sha,
                head_evidence=(SourceRecord(key.table, key.key),), cleanup_pending=False)

        def worked(owner: TicketOwner, kind: Literal['completed', 'returned'], job: Mapping[str, Any],
                budget: BudgetFacts, calls: tuple[AttemptFacts, ...], base_sha: str, source_ref: str,
                candidate_sha: str | None, candidate_tree: str | None, integration_target: str | None,
                integration: Mapping[str, Any] | None, accepted_review: str | None,
                close_intent: Mapping[str, Any] | None, close_ownership: Mapping[str, Any] | None,
                cleanup: Mapping[str, Any] | None) -> ImportedSubject:
            assert owner.job is not None
            key = SourceRecord(owner.job.table, owner.job.key)
            all_calls.extend(calls)
            attempts = tuple(_imported_attempt(db, header.run_id, call.attempt_id, call.role, call.old_ordinal,
                call.pending, call.thread_id, call.binding, call.invocation, call.bound_version, call.selected_version,
                call.global_current_version, call.raw_evidence_id, call.raw, call.normalized, call.allocation_boundary)
                for call in calls)
            completion = None
            if kind == 'completed':
                assert close_intent is not None and close_ownership is not None and candidate_tree is not None and accepted_review is not None
                completion = ImportedCompletion(records=(key,
                    *(SourceRecord(table, key.key) for table in ('work_job_acceptance_record', 'work_job_merge_intent',
                        'work_job_ticket_close_intent', 'work_job_ticket_close_ownership')),
                    SourceRecord('run_cleanup_items', (header.run_id, job['value_ticket_branch']))),
                    publication=Commit(job['value_publication_sha'], candidate_tree),
                    integrated=Commit(job['value_integrated_sha'], job['value_integrated_tree']), pr_number=job['value_pr_number'],
                    development_attempt=[call.attempt_id for call in calls if call.role == Role.DEVELOPMENT][-1],
                    review_attempt=accepted_review, close_intent=wire_json(_wire_row(close_intent)),
                    close_ownership=wire_json(_wire_row(close_ownership)), close_readproof=None)
            candidate = None
            if candidate_sha is not None:
                assert candidate_tree is not None
                candidate = Commit(candidate_sha, candidate_tree)
            return ImportedSubject(subject=owner.subject, issue=owner.ticket, generation=owner.generation,
                record=key, aliases=tuple(SourceRecord(alias.table, alias.key) for alias in owner.aliases), kind=kind,
                historical_base=Commit(base_sha, _git(root, 'rev-parse', base_sha + '^{tree}')),
                budgets=(ImportedBudget(owner=SourceRecord(budget.owner.table, budget.owner.key),
                    record=SourceRecord(budget.record.table, budget.record.key), purpose=Purpose.REQUIREMENTS,
                    window=budget.window, developments_used=budget.developments_used, reviews_used=budget.reviews_used, current=True),),
                attempts=attempts, candidate=candidate, integration=None if integration is None else wire_json(dict(integration)),
                accepted_review=accepted_review, completion=completion, resources=(resource(owner, existing=kind == 'returned',
                    source_ref=source_ref, expected_oid=candidate_sha or base_sha),))

        subjects = []
        for owner in owners:
            if owner.job is not None:
                subjects.append(_read_worked_source(db, header, owner, profile, worked))
            else:
                key = owner.graph_record
                subjects.append(ImportedSubject(subject=owner.subject, issue=owner.ticket, generation=owner.generation,
                    record=SourceRecord(key.table, key.key), aliases=tuple(SourceRecord(alias.table, alias.key) for alias in owner.aliases),
                    kind='unallocated', historical_base=None, budgets=(), attempts=(), candidate=None, integration=None,
                    accepted_review=None, completion=None, resources=(resource(owner, existing=False,
                        source_ref=f'refs/heads/agent-run/{header.run_id}/ticket-{owner.ticket}', expected_oid=None),)))
        _check_run_source_scope(db, header, owners, all_calls, profile)
        if set(sealed) != {subject.subject for subject in subjects if subject.kind == 'returned'}:
            raise MigrationRefused('source_resource_union')
        run_head, closes = verify_imported_run_external(header, snapshot.sha256, tuple(subjects), external, root)
        subjects = [replace(subject, completion=replace(subject.completion,
            close_readproof=wire_json(dict(closes[subject.subject])))) if subject.completion is not None else subject
            for subject in subjects]
        base = Commit(header.default_sha, _git(root, 'rev-parse', header.default_sha + '^{tree}'))
        final = root.parent / 'worktrees' / header.run_id / 'run-publication'
        spec = RunSpec(header.run_id, header.repository, header.parent_number, header.parent_revision, header.graph_revision,
            (*tuple(Delivery(owner.subject, DeliveryKind.TICKET, owner.ticket, 'source-verified', base,
                f'refs/heads/agent-run/{header.run_id}/ticket-{owner.ticket}', 'refs/heads/' + header.run_branch,
                checkout=str(worktrees / f'ticket-{owner.ticket}'), owned_root=str(worktrees), common_git_dir=str(common)) for owner in owners),
             Delivery(f'final:{header.run_id}', DeliveryKind.FINAL_RUN, header.parent_number, header.parent_revision, base,
                'refs/heads/' + header.run_branch, 'refs/heads/' + header.default_branch,
                checkout=str(final), owned_root=str(final.parent), common_git_dir=str(common))),
            header.policy, header.language, str(root), header.inputs)
        head_evidence = tuple(subject.record for subject in subjects
            if subject.completion is not None and subject.completion.integrated.sha == run_head)
        if not head_evidence:
            if run_head != header.default_sha:
                raise MigrationRefused('source_run_head_unproven')
            head_evidence = (SourceRecord('run_condition_base', (header.run_id,)),)
        run_resource = ImportedResource(owner=SourceRecord('runs', (header.run_id,)), kind='run-ref', disposition='existing',
            source_ref='refs/heads/' + header.run_branch, expected_oid=run_head, checkout=None, workspace_oid=None, owned_root=None,
            common_git_dir=common, before_head=None, checkout_archive_sha256=None, checkout_index_sha256=None,
            head_evidence=head_evidence, cleanup_pending=False)
        source = ImportSource(snapshot=snapshot, mode='original', scope='whole-run', task=header.task,
            checkout_uuid=_one(db, 'SELECT value_checkout_identity FROM run_root WHERE run_id=?', (header.run_id,))[0],
            profile_document=profile_document, external_readproof=external_document,
            oracle_documents=tuple((row[0], row[1].encode('utf-8')) for row in db.execute(
                'SELECT e.evidence_id,m.body FROM work_evidence e JOIN work_evidence_materials m '
                'ON m.material_id=e.material_id WHERE e.run_id=? ORDER BY e.evidence_id', (header.run_id,))))
        # WholeRun's existing native history is import/Invocation evidence;
        # the complete original timeline remains in the uniform source records.
        result = ImportedRunFacts(source=source, spec=spec, requirements=header.requirements,
            observed_requirements=header.requirements, created_at=header.created_at, subjects=tuple(subjects),
            run_resource=run_resource, history=())
    validate_snapshot(snapshot)
    if (_read_profile(profile_snapshot)[1] != profile_sha or _read_document(external_snapshot)[1] != external_sha
            or _read_document(checkouts_snapshot)[1] != sealed_sha):
        raise MigrationRefused('source_documents_changed')
    return result
