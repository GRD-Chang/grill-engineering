"""Parent-owned repair PRs; budgets, Agents and effects remain in the Core.

The parent observation stays Run -> default. Its optional child observes the
repair branch -> Run, so neither CI nor approval can confuse those baselines.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from agent_run.contracts.copy import text
from agent_run.contracts.required_checks import same_required_checks
from agent_run.core.operation_contract import DeliveryRead
from agent_run.core.delivery_state import DeliveryState
from agent_run.core import delivery_checks
from agent_run.contracts.pr_observation import pr_target
from agent_run.contracts.repository import same_repository
from agent_run.errors import KernelError
from agent_run.model import Commit, EffectResult, PullRequest, Step, World, wire_json
from agent_run.storage.sqlite import Transaction
from agent_run.storage.run_scope import transaction_scope

@dataclass(frozen=True)
class EffectPlan:
    operation: str
    before: str
    after: str
    repair: sqlite3.Row
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WaitPlan:
    kind: str = 'github_convergence'
    seconds: float = 600


@dataclass(frozen=True)
class ChecksPlan:
    job: Mapping[str, Any]
    material: str | None
    stage: str


@dataclass(frozen=True)
class Consumed:
    step: Step
    promote: bool = False


def _pr_identity(run: sqlite3.Row, resource: sqlite3.Row, pr: PullRequest) -> bool:
    return bool(same_repository(pr.repository, run['repository'])
        and same_repository(pr.head_repository, run['repository'])
        and pr.source_ref == resource['source_ref'] and pr.target_ref == resource['target_ref']
        and (resource['pr_number'] is None or pr.number == resource['pr_number']))


def _pr_target(run: sqlite3.Row, resource: sqlite3.Row, world: World) -> Commit | None:
    return None if world.pr is None else pr_target(world.pr,
        repository=run['repository'], pr_number=resource['pr_number'],
        source_ref=resource['source_ref'], target_ref=resource['target_ref'],
        source=world.source, target=world.target, ancestry=world.ancestry)


@dataclass(frozen=True)
class EffectBinding:
    source_ref: str
    target_ref: str
    base: Commit
    candidate: Commit
    pr_number: int | None
    payload: Mapping[str, Any]


def effect_binding(row: sqlite3.Row, world: World, operation: str) -> EffectBinding:
    assert world.repair is not None and world.repair.target is not None
    payload = dict(repair_id=row['id'], kind='run_repair', default_sha=row['default_sha'],
                   integrated_sha=row['integrated_sha'], integrated_tree=row['integrated_tree'])
    if operation == 'push':
        payload['expected_remote_sha'] = None if world.repair.source is None else world.repair.source.sha
    return EffectBinding(row['source_ref'], row['target_ref'], world.repair.target,
                         Commit(row['integrated_sha'], row['integrated_tree']), row['pr_number'], payload)


def reject_authority(tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, effect: sqlite3.Row) -> None:
    tx.execute("UPDATE effects SET status='retired' WHERE run_id=? AND id=?", (run['id'], effect['id']))
    tx.execute("UPDATE subjects SET phase='repair_checks',version=version+1 WHERE run_id=? AND id=?",
               (run['id'], job['id']))


def base_current(effect: sqlite3.Row, world: World) -> bool:
    return bool(world.repair is not None and world.repair.target is not None
                and effect['base_sha'] == world.repair.target.sha)


def current(tx: Transaction, subject: str) -> sqlite3.Row | None:
    rows = tx.rows("SELECT * FROM run_repairs WHERE run_id=? AND subject=? AND state='open'",
                   (transaction_scope(tx).run_id, subject))
    return rows[0] if rows else None


def is_effect(job: sqlite3.Row, row: sqlite3.Row) -> bool:
    return (job['kind'] == 'final_run' and row['source_ref'] != job['source_ref']
            and row['target_ref'] == job['source_ref'] and row['operation'] in {'push', 'pr_write', 'merge'})


def demand(row: sqlite3.Row, *, read_checks: bool) -> DeliveryRead:
    anchors = tuple(sorted({row[key] for key in ('run_sha', 'integrated_sha', 'candidate_sha', 'default_sha')}))
    return DeliveryRead(row['source_ref'], row['target_ref'], row['pr_number'], anchors,
        None,
        ancestry_pairs=(('pr_base', 'target'), (row['run_sha'], 'target'),
                        (row['candidate_sha'], row['integrated_sha']),
                        (row['default_sha'], row['integrated_sha']),
                        (row['run_sha'], row['integrated_sha']),
                        (row['integrated_sha'], 'source'), (row['integrated_sha'], 'target')) +
                       (() if row['published_sha'] is None else ((row['published_sha'], 'source'),)), read_checks=read_checks)


def start(state: DeliveryState, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World, *, review_current: bool) -> Step:
    if not review_current or world.source is None or world.target is None:
        raise KernelError('repair_acceptance_missing')
    if any((sha, job['integrated_sha']) not in world.ancestry
           for sha in (world.source.sha, world.target.sha, job['candidate_sha'])):
        state.gate(tx, job['id'], 'repair_ancestry_unproven')
        return Step('paused', job['id'], 'repair_ancestry_unproven')
    row = current(tx, job['id'])
    values = dict(generation=job['generation'], run_sha=world.source.sha, default_sha=world.target.sha,
        candidate_sha=job['candidate_sha'], candidate_tree=job['candidate_tree'],
        integrated_sha=job['integrated_sha'], integrated_tree=job['integrated_tree'], expected_tree=job['expected_tree'],
        review_attempt=job['review_attempt'], revision=job['revision'], parent_revision=run['parent_revision'],
        graph_revision=run['graph_revision'], completion_revision=job['completion_revision'])
    if row is None:
        sequence = tx.one('SELECT coalesce(max(sequence),0)+1 FROM run_repairs WHERE run_id=? AND subject=?',
                          (run['id'], job['id']))[0]
        repair_id = uuid4().hex
        values = dict(run_id=run['id'], id=repair_id, subject=job['id'], sequence=sequence, state='open',
                      source_ref=job['source_ref'] + '-repair-' + str(sequence), target_ref=job['source_ref'], **values)
        tx.execute('INSERT INTO run_repairs(' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')',
                   tuple(values.values()))
    else:
        repair_id = row['id']
        tx.execute('UPDATE run_repairs SET ' + ','.join(key + '=?' for key in values) + ',checks_id=NULL WHERE run_id=? AND id=?',
                   (*values.values(), run['id'], repair_id))
    tx.execute("UPDATE subjects SET phase='repair_push',grant_id=NULL,version=version+1 WHERE run_id=? AND id=?",
               (run['id'], job['id']))
    state.event(tx, job['id'], 'repair_prepared', repair_id)
    return Step('progress', job['id'])


def _identity(run: sqlite3.Row, row: sqlite3.Row, world: World) -> bool:
    child = world.repair
    if child is None or child.target != world.source:
        return False
    if (None if child.source is None else child.source.sha) != row['published_sha']:
        return False
    pr = child.pr
    if pr is None:
        return row['pr_number'] is None
    return bool(_pr_identity(run, row, pr) and pr.state == 'OPEN'
                and pr.actor == world.publisher_actor and child.source == pr.head
                and _pr_target(run, row, child) is not None)


def _basis(row: sqlite3.Row, job: sqlite3.Row, run: sqlite3.Row, world: World) -> bool:
    return bool(world.source is not None and world.target is not None
        and row['generation'] == job['generation'] and row['run_sha'] == world.source.sha
        and row['default_sha'] == world.target.sha and row['revision'] == job['revision']
        and row['parent_revision'] == run['parent_revision'] and row['graph_revision'] == run['graph_revision']
        and row['completion_revision'] == job['completion_revision'] and row['review_attempt'] == job['review_attempt']
        and row['integrated_sha'] == job['integrated_sha'] and row['integrated_tree'] == job['integrated_tree']
        and row['expected_tree'] == job['expected_tree'])


def authority(tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
              effect: sqlite3.Row, world: World, *, review_current: bool) -> bool:
    row = current(tx, job['id'])
    payload = json.loads(tx.get(effect['payload_id'], job['id']))
    if (row is None or payload.get('repair_id') != row['id'] or not _basis(row, job, run, world)
            or not _identity(run, row, world) or not review_current
            or effect['base_sha'] != row['run_sha'] or effect['after_sha'] != row['integrated_sha']
            or effect['source_ref'] != row['source_ref'] or effect['target_ref'] != row['target_ref']
            or effect['pr_number'] != row['pr_number']):
        return False
    child = world.repair
    assert child is not None
    if any((row[key], row['integrated_sha']) not in child.ancestry
           for key in ('candidate_sha', 'default_sha', 'run_sha')):
        return False
    if effect['operation'] != 'merge':
        return effect['operation'] == 'push' or child.source is not None and child.source.sha == effect['after_sha']
    checks = child.checks
    if (not row['checks_id'] or checks is None or checks.result not in {'pass', 'none'}
            or checks.pr_number != row['pr_number'] or checks.head_sha != row['integrated_sha']):
        return False
    recorded = tx.one('SELECT material_id FROM checks WHERE run_id=? AND id=?', (run['id'], row['checks_id']))[0]
    return same_required_checks(json.loads(tx.get(recorded, job['id'])), checks)


def observe_source_append(state: DeliveryState, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                          world: World, effect: sqlite3.Row | None = None) -> Step | None:
    row, child = current(tx, job['id']), world.repair
    if row is None or child is None or child.source is None or child.source.sha == row['published_sha']:
        return None
    pr = child.pr
    valid_pr = (pr is None and row['pr_number'] is None or pr is not None and pr.state == 'OPEN'
        and _pr_identity(run, row, pr) and pr.actor == world.publisher_actor
        and _pr_target(run, row, child) is not None)
    if (not row['published_sha'] or not valid_pr or child.target != world.source
            or (row['published_sha'], child.source.sha) not in child.ancestry):
        state.gate(tx, job['id'], 'repair_pr_identity_changed')
        return Step('paused', job['id'], 'repair_pr_identity_changed')
    allowance = tx.one('SELECT * FROM resync_allowances WHERE run_id=? AND subject=? AND current=1',
                       (run['id'], job['id']))
    if allowance['used'] >= allowance['allowance']:
        state.gate(tx, job['id'], 'integration_resync_exhausted')
        return Step('paused', job['id'], 'integration_resync_exhausted')
    evidence = tx.put(job['id'], 'repair_source_append', wire_json({
        'repair_id': row['id'], 'before_sha': row['published_sha'], 'after_sha': child.source.sha}))
    tx.execute('UPDATE run_repairs SET published_sha=?,checks_id=NULL WHERE run_id=? AND id=?',
               (child.source.sha, run['id'], row['id']))
    tx.execute('UPDATE resync_allowances SET used=used+1 WHERE run_id=? AND subject=? AND current=1',
               (run['id'], job['id']))
    if effect is not None:
        tx.execute("UPDATE effects SET status='retired' WHERE run_id=? AND id=? AND status='prepared'",
                   (run['id'], effect['id']))
    state.update_boundary(tx, run, job)
    state.event(tx, job['id'], 'repair_source_append', row['id'], evidence)
    return Step('progress', job['id'], 'repair_source_changed')


def refresh_prepared(state: DeliveryState, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                     effect: sqlite3.Row, world: World) -> Step | None:
    """Retire only an unstarted intent when its legal combination advances."""
    appended = observe_source_append(state, tx, run, job, world, effect)
    if appended is not None:
        return appended
    row = current(tx, job['id'])
    if row is None or not _identity(run, row, world):
        state.gate(tx, job['id'], 'repair_pr_identity_changed')
        return Step('paused', job['id'], 'repair_pr_identity_changed')
    if not state.observe_integration_snapshot(tx, job, world):
        return Step('paused', job['id'], 'integration_snapshot')
    if _basis(row, job, run, world):
        return None
    tx.execute("UPDATE effects SET status='retired' WHERE run_id=? AND id=? AND status='prepared'",
               (run['id'], effect['id']))
    state.update_boundary(tx, run, job)
    return Step('progress', job['id'], 'repair_basis_changed')


def reconcile_unapplied(state: DeliveryState, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                        effect: sqlite3.Row, result: EffectResult, world: World) -> Step | WaitPlan | None:
    """A sent merge can be retired only after its exact non-application proof."""
    row, child = current(tx, job['id']), world.repair
    if row is None or child is None or child.source is None or child.target is None:
        return WaitPlan()
    changed = (not _basis(row, job, run, world) or row['published_sha'] != child.source.sha)
    if not changed:
        return None
    pr, proof = result.pr, result.raw.get('merge_not_applied')
    valid = (isinstance(proof, dict) and proof == {
        'before_sha': effect['before_sha'], 'after_sha': effect['after_sha'],
        'source_sha': child.source.sha, 'target_sha': child.target.sha, 'pr_number': effect['pr_number']}
        and pr is not None and child.pr is not None and pr.state == child.pr.state == 'OPEN'
        and pr.head == child.pr.head == child.source and pr.actor == effect['actor'] == world.publisher_actor
        and _pr_identity(run, effect, pr) and _pr_identity(run, row, child.pr)
        and child.target == world.source
        and (effect['before_sha'], child.target.sha) in child.ancestry
        and (effect['after_sha'], child.source.sha) in child.ancestry
        and (effect['after_sha'], child.target.sha) not in child.ancestry)
    if not valid:
        return WaitPlan()
    material = tx.put(job['id'], 'repair_merge_not_applied', wire_json(asdict(result)))
    tx.execute("UPDATE effects SET status='retired' WHERE run_id=? AND id=? AND status='sent'",
               (run['id'], effect['id']))
    state.event(tx, job['id'], 'repair_merge_not_applied', effect['id'], material)
    refreshed = refresh_prepared(state, tx, run, job, effect, world)
    if refreshed is not None:
        return refreshed
    state.update_boundary(tx, run, job)
    return Step('progress', job['id'], 'repair_basis_changed')


def advance(state: DeliveryState, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
            world: World) -> Step | EffectPlan | ChecksPlan:
    row, child = current(tx, job['id']), world.repair
    if row is None or child is None:
        raise KernelError('repair_observation_missing')
    appended = observe_source_append(state, tx, run, job, world)
    if appended is not None:
        return appended
    if not _identity(run, row, world):
        state.gate(tx, job['id'], 'repair_pr_identity_changed')
        return Step('paused', job['id'], 'repair_pr_identity_changed')
    if not state.observe_integration_snapshot(tx, job, world):
        return Step('paused', job['id'], 'integration_snapshot')
    if not _basis(row, job, run, world):
        state.update_boundary(tx, run, job)
        return Step('progress', job['id'], 'repair_basis_changed')
    stage = job['phase'].removeprefix('repair_')
    if stage == 'push':
        if row['published_sha'] == row['integrated_sha']:
            return _phase(tx, run['id'], job['id'], 'repair_pr')
        return EffectPlan('push', row['published_sha'] or row['run_sha'], row['integrated_sha'], row)
    if stage == 'pr':
        pr = child.pr
        title = text("repair.pr_title", language=run['language'], issue=job['issue'], sequence=row['sequence'])
        body = text("repair.pr_body", language=run['language'], issue=job['issue'],
                    candidate=row['integrated_sha'], review=row['review_attempt'])
        return EffectPlan('pr_write', row['integrated_sha'], row['integrated_sha'], row,
            payload={'title': title, 'body': body, 'prior': None if pr is None else {'title': pr.title, 'body': pr.body}})
    if stage not in {'checks', 'merge'}:
        raise KernelError('repair_phase_invalid')
    shadow = dict(job, pr_number=row['pr_number'], publication_sha=row['integrated_sha'])
    material = delivery_checks.record(tx, run, shadow, child.checks, repair_id=row['id'])
    return ChecksPlan(shadow, material, stage)


def after_checks(state: DeliveryState, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                 plan: ChecksPlan) -> Step | EffectPlan:
    row = current(tx, job['id'])
    assert row is not None and plan.material is not None
    if plan.stage == 'checks':
        state.event(tx, job['id'], 'repair_checks_observed', row['id'], plan.material)
        return _phase(tx, run['id'], job['id'], 'repair_merge')
    return EffectPlan('merge', row['run_sha'], row['integrated_sha'], row,
        payload={'method': 'merge', 'parents': [row['run_sha'], row['integrated_sha']],
                 'commit_message': f"merge: Parent #{job['issue']} repair {row['sequence']}"})


def _phase(tx: Transaction, run_id: str, subject: str, phase: str) -> Step:
    tx.execute('UPDATE subjects SET phase=?,version=version+1 WHERE run_id=? AND id=?', (phase, run_id, subject))
    return Step('progress', subject)


def receipt_subject(job: sqlite3.Row, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate receipts against the tree bound to the original repair intent."""
    return dict(job, publication_tree=payload['integrated_tree'])


def consume(state: DeliveryState, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
            effect: sqlite3.Row, world: World, *, review_current: bool, stale: bool = False) -> Consumed:
    payload = json.loads(tx.get(effect['payload_id'], job['id']))
    row = tx.one('SELECT * FROM run_repairs WHERE run_id=? AND id=?', (run['id'], payload['repair_id']))
    result = json.loads(tx.get(effect['receipt_id'], job['id']))
    # Use this intent's original tree, never the latest mutable candidate.
    if result['disposition'] != 'target':
        raise KernelError('repair_write_not_confirmed')
    promote = False
    operation = effect['operation']
    if operation == 'push':
        tx.execute('UPDATE run_repairs SET published_sha=? WHERE run_id=? AND id=?',
                   (effect['after_sha'], run['id'], row['id']))
        phase = 'repair_pr'
    elif operation == 'pr_write':
        tx.execute('UPDATE run_repairs SET pr_number=? WHERE run_id=? AND id=?',
                   (result['pr']['number'], run['id'], row['id']))
        phase = 'repair_checks'
    else:
        commit = result['commit']
        tx.execute("UPDATE run_repairs SET state='merged',merge_sha=? WHERE run_id=? AND id=?",
                   (commit['sha'], run['id'], row['id']))
        # Parent workspace remains the reviewed integration commit. Only its
        # publication identity is promoted to GitHub's proven ordinary merge.
        # _basis compares the pre-merge Run head; compare that fact separately
        # now that the remote Run is the result of the original merge intent.
        applicable = (not stale and world.source is not None and world.source.sha == commit['sha']
            and world.target is not None and world.target.sha == row['default_sha']
            and row['review_attempt'] == job['review_attempt'] and row['integrated_sha'] == job['integrated_sha']
            and row['generation'] == job['generation'] and row['revision'] == job['revision']
            and row['parent_revision'] == run['parent_revision'] and row['graph_revision'] == run['graph_revision']
            and row['completion_revision'] == job['completion_revision'] and review_current
            and commit['tree'] == row['integrated_tree'] == row['expected_tree']
            and world.repair is not None
            and all((row[key], row['integrated_sha']) in world.repair.ancestry
                    for key in ('candidate_sha', 'default_sha', 'run_sha')))
        tx.execute('UPDATE subjects SET source_sha=?,integrated_sha=?,integrated_tree=?,grant_id=NULL,checks_id=NULL WHERE run_id=? AND id=?',
                   (commit['sha'], commit['sha'] if applicable else job['integrated_sha'],
                    commit['tree'] if applicable else job['integrated_tree'], run['id'], job['id']))
        state.record_source_write(tx, job['id'], job['source_ref'], commit['sha'])
        phase = 'publish' if applicable else 'integration'
        state.event(tx, job['id'], 'repair_merged', row['id'], effect['receipt_id'])
        promote = applicable
    tx.execute("UPDATE effects SET status='confirmed' WHERE run_id=? AND id=?", (run['id'], effect['id']))
    if not stale:
        _phase(tx, run['id'], job['id'], phase)
    state.event(tx, job['id'], 'effect_consumed', effect['id'], effect['receipt_id'])
    return Consumed(Step('progress', job['id']), promote)
