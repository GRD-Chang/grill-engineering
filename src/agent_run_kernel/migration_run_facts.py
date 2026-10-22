"""Read canonical original Run scope and owner facts without running old code."""
from __future__ import annotations

from dataclasses import dataclass, replace
import sqlite3
from typing import Any, Mapping

from agent_run.task_identity import TaskKey

from .kernel import Kernel
from .migration import MigrationRefused, _created_at_epoch
from .migration_active import _evidence
from .migration_conversion import _one, _policy
from .migration_graph import read_graph_requirements
from .migration_quiescence import read_v6_host_exit
from .model import CreationInputs, Policy, Profile, Requirements


@dataclass(frozen=True)
class SourceKey:
    table: str
    key: tuple[str | int, ...]

    def document(self) -> dict[str, Any]:
        return {'table': self.table, 'key': list(self.key)}


@dataclass(frozen=True)
class RunHeader:
    run_id: str
    repository: str
    parent_number: int
    parent_revision: str
    graph_revision: str
    graph_version: str
    created_at: float
    language: str
    requirements: Requirements
    policy: Policy
    inputs: CreationInputs
    run_branch: str
    default_branch: str
    default_sha: str
    task: TaskKey


@dataclass(frozen=True)
class TicketOwner:
    subject: str
    ticket: int
    generation: int
    job: SourceKey | None
    aliases: tuple[SourceKey, ...]
    graph_record: SourceKey


@dataclass(frozen=True)
class BudgetFacts:
    owner: SourceKey
    record: SourceKey
    window: int
    developments_used: int
    reviews_used: int


def read_run_header(db: sqlite3.Connection, profile: Mapping[str, Any], profile_sha: str,
                    source_sha: str) -> RunHeader:
    """Read one whole retirement unit; no per-Ticket or role-count assumptions."""
    run = _one(db, 'SELECT * FROM runs')
    if run['status'] != 'active' or run['phase'] is not None:
        raise MigrationRefused('unsupported_source_run_control')
    run_id = run['run_id']
    task = read_v6_host_exit(db, run_id, required=True)
    if task is None:
        raise MigrationRefused('source_task_identity')
    root = _one(db, 'SELECT * FROM run_root WHERE run_id=?', (run_id,))
    if (any(root[field] is not None for field in (
            'value_blocked_reason', 'value_execution_resume_phase', 'value_pause_resume_phase',
            'value_human_blocker_phase', 'value_terminal_kind', 'value_retired_parent_generation'))
            or any(root[field] not in (None, 0) for field in (
                'value_base_resolution_pending', 'value_currentness_resolution_pending',
                'value_github_refresh_pending', 'value_locator_registration_pending',
                'value_repository_binding_pending'))):
        raise MigrationRefused('unsupported_source_run_control')
    for table in ('run_ticket_pause_requests', 'run_requeue_transition', 'run_condition_requeue_required'):
        if db.execute(f'SELECT 1 FROM "{table}" WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
            raise MigrationRefused('unsupported_source_run_control')
    for table in ('run_work_queue', 'run_integration_queue'):
        if db.execute(f'SELECT 1 FROM "{table}" WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
            raise MigrationRefused('unsupported_source_queue')
    parent = _one(db, 'SELECT * FROM run_parent_versions WHERE run_id=? AND version_id=?',
                  (run_id, root['value_parent_version_id']))
    title = _evidence(db, run_id, parent['title_id'], owner='run:' + run_id, kind='parent.title')
    body = _evidence(db, run_id, parent['body_id'], owner='run:' + run_id, kind='parent.body')
    graph = _one(db, 'SELECT g.revision,g.version_id,s.evidence_id FROM run_graph_snapshots s '
                    'JOIN run_graph_versions g ON g.run_id=s.run_id AND g.version_id=s.version_id '
                    'WHERE s.run_id=? AND s.snapshot_id=?', (run_id, root['value_graph_snapshot_id']))
    observation = _evidence(db, run_id, graph['evidence_id'], owner='run:' + run_id, kind='graph.observation')
    decoded = read_graph_requirements(db, run_id=run_id,
        snapshot_id=root['value_graph_snapshot_id'], version_id=graph['version_id'],
        parent_number=run['parent_number'], title=title, body=body, observation=observation)
    requirements = decoded.requirements
    issues = requirements.issues
    order = list(requirements.parent.sub_issue_numbers)
    try:
        parent_revision = Kernel._fingerprint({'title': title, 'body': body})
        graph_revision = Kernel._fingerprint({'tickets': sorted(order),
            'dependencies': {str(issue.number): sorted(edge.number for edge in issue.blocked_by)
                             for issue in issues}})
        if (parent['revision'] != parent_revision or graph['revision'] != graph_revision
                or root['value_accepted_parent_spec_revision'] != parent_revision
                or root['value_accepted_ticket_graph_revision'] != graph_revision):
            raise MigrationRefused('source_revision')
    except (KeyError, TypeError, ValueError) as error:
        raise MigrationRefused('source_requirements') from error
    if root['value_language'] not in {'zh', 'en'}:
        raise MigrationRefused('source_language')
    frozen_ids = {kind: root[field] for field, kind in (
        ('value_policy_snapshot', 'policy_snapshot'), ('value_prompt_resources', 'prompt_resources'),
        ('value_github_identity', 'github_identity'), ('value_runner_github_identity', 'runner_github_identity'))}
    frozen = {kind: _evidence(db, run_id, evidence_id, owner='run:' + run_id, kind=kind)
              for kind, evidence_id in frozen_ids.items()}
    if profile.get('run_id') != run_id or profile.get('profile_revision') != 1:
        raise MigrationRefused('source_profile')
    profiles = {'preset': profile.get('preset'), 'profiles': profile.get('profiles')}
    roles = profiles['profiles']
    if not isinstance(roles, dict) or set(roles) != {'development', 'review', 'publication'}:
        raise MigrationRefused('source_profile')
    try:
        policy = replace(_policy(frozen['policy_snapshot']),
            development_profile=Profile(roles['development']['model'], roles['development']['reasoning_effort']),
            review_profile=Profile(roles['review']['model'], roles['review']['reasoning_effort']),
            publication_profile=Profile(roles['publication']['model'], roles['publication']['reasoning_effort']))
    except (KeyError, TypeError, ValueError) as error:
        raise MigrationRefused('source_profile') from error
    inputs = CreationInputs(profiles, frozen['prompt_resources'], frozen['github_identity'],
        frozen['runner_github_identity'], {'enabled': False}, {'source': 'v6-offline-conversion',
        'source_sha256': source_sha, 'frozen_evidence_ids': frozen_ids, 'profile_sha256': profile_sha,
        'profiles': 'old-profile-store', 'notifications': 'old-not-configured'})
    query = _one(db, 'SELECT presence FROM run_query_roots WHERE run_id=?', (run_id,))
    if query['presence'] != b'\x1a' or db.execute('SELECT 1 FROM run_notification_config LIMIT 1').fetchone():
        raise MigrationRefused('unsupported_source_notifications')
    base = _one(db, 'SELECT * FROM run_condition_base WHERE run_id=?', (run_id,))
    if root['value_run_branch'] != f'agent-run/{run_id}/run':
        raise MigrationRefused('source_run_branch')
    return RunHeader(run_id, run['repository'], run['parent_number'], parent_revision, graph_revision,
        graph['version_id'], _created_at_epoch(run['created_at']), root['value_language'], requirements,
        policy, inputs, root['value_run_branch'], base['value_branch'], base['value_sha'], task)


def read_ticket_owners(db: sqlite3.Connection, header: RunHeader) -> tuple[TicketOwner, ...]:
    """Resolve canonical current slots once; historical jobs never become owners."""
    run_id = header.run_id
    slots = db.execute('SELECT * FROM work_job_slots WHERE run_id=? ORDER BY container,position', (run_id,)).fetchall()
    current = [row for row in slots if row['container'] == 'ticket_jobs']
    if any(row['container'] != 'ticket_jobs' for row in slots):
        raise MigrationRefused('unsupported_source_current_owner')
    by_ticket: dict[int, sqlite3.Row] = {}
    aliases: dict[int, list[SourceKey]] = {}
    selected_only: set[int] = set()
    for slot in current:
        job = _one(db, 'SELECT * FROM work_jobs WHERE run_id=? AND job_id=?', (run_id, slot['job_id']))
        if db.execute('SELECT 1 FROM work_job_gates WHERE run_id=? AND job_id=? LIMIT 1',
                      (run_id, job['job_id'])).fetchone():
            raise MigrationRefused('unsupported_source_job_gate')
        ticket = job['value_ticket_number']
        if (type(ticket) is not int or ticket in by_ticket or slot['slot'] != str(ticket)
                or job['owner_kind'] != 'ticket' or job['owner_key'] != str(ticket)
                or job['job_id'] not in {f'ticket:{ticket}:generation:1', f'ticket:{ticket}:selection'}):
            raise MigrationRefused('source_owner_identity')
        if job['job_id'].endswith(':selection'):
            if (job['stage'] is not None or job['value_ticket_branch_initialized'] not in (None, 0)
                    or db.execute('SELECT 1 FROM work_attempts WHERE run_id=? AND job_id=? LIMIT 1',
                                  (run_id, job['job_id'])).fetchone()):
                raise MigrationRefused('source_unallocated_owner')
            selected_only.add(ticket)
        by_ticket[ticket] = job
        aliases[ticket] = [SourceKey('work_job_slots', (run_id, slot['container'], slot['slot']))]
        if ticket in selected_only:
            aliases[ticket].append(SourceKey('work_jobs', (run_id, job['job_id'])))
    active = _one(db, 'SELECT active_job_id FROM work_job_layout WHERE run_id=?', (run_id,))['active_job_id']
    if active is not None:
        owners = [number for number, row in by_ticket.items() if row['job_id'] == active]
        if len(owners) != 1:
            raise MigrationRefused('source_active_owner')
        aliases[owners[0]].append(SourceKey('work_job_layout', (run_id,)))
    scope = set(header.requirements.parent.sub_issue_numbers)
    if not set(by_ticket) <= scope:
        raise MigrationRefused('source_owner_scope')
    return tuple(TicketOwner(f'ticket:{number}', number, 1,
        None if number not in by_ticket or number in selected_only else SourceKey('work_jobs', (run_id, by_ticket[number]['job_id'])),
        tuple(aliases.get(number, ())), SourceKey('run_graph_members', (run_id, header.graph_version, number)))
        for number in header.requirements.parent.sub_issue_numbers)


def read_ticket_budget(db: sqlite3.Connection, header: RunHeader, owner: TicketOwner) -> BudgetFacts:
    if owner.job is None:
        raise MigrationRefused('source_budget_owner')
    job_id = owner.job.key[1]
    row = _one(db, 'SELECT budget_owner_id FROM work_jobs WHERE run_id=? AND job_id=?', (header.run_id, job_id))
    if row['budget_owner_id'] != job_id:
        raise MigrationRefused('unsupported_source_shared_budget')
    budget = _one(db, 'SELECT * FROM work_job_review_budget WHERE run_id=? AND job_id=?', (header.run_id, job_id))
    if (budget['value_window'] != 1 or budget['value_final_ci_fix_used'] != 0
            or any(type(budget[field]) is not int or budget[field] < 0 for field in (
                'value_development_attempts', 'value_reviewer_invocations'))):
        raise MigrationRefused('unsupported_source_budget')
    return BudgetFacts(owner.job, SourceKey('work_job_review_budget', (header.run_id, job_id)), 1,
                       budget['value_development_attempts'], budget['value_reviewer_invocations'])
