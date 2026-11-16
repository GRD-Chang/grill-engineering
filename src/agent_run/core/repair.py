"""Parent-owned repair PRs; budgets, Agents and effects remain in the Core.

The parent observation stays Run -> default. Its optional child observes the
repair branch -> Run, so neither CI nor approval can confuse those baselines.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_run.contracts.copy import text
from agent_run.contracts.required_checks import same_required_checks
from agent_run.core.operation_contract import DeliveryRead, IORequest, Work
from agent_run.errors import KernelError
from agent_run.model import EffectResult, Step, World, wire_json
from agent_run.storage.sqlite import Transaction
from agent_run.storage.run_scope import transaction_scope

if TYPE_CHECKING:
    from agent_run.core.kernel import Kernel


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


def start(kernel: Kernel, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World) -> Step:
    if not kernel._review_current(tx, job, world) or world.source is None or world.target is None:
        raise KernelError('repair_acceptance_missing')
    if any((sha, job['integrated_sha']) not in world.ancestry
           for sha in (world.source.sha, world.target.sha, job['candidate_sha'])):
        kernel._gate(tx, job['id'], 'repair_ancestry_unproven')
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
    kernel._event(tx, job['id'], 'repair_prepared', repair_id)
    return Step('progress', job['id'])


def _identity(kernel: Kernel, run: sqlite3.Row, row: sqlite3.Row, world: World) -> bool:
    child = world.repair
    if child is None or child.target != world.source:
        return False
    if (None if child.source is None else child.source.sha) != row['published_sha']:
        return False
    pr = child.pr
    if pr is None:
        return row['pr_number'] is None
    return bool(kernel._pr_identity(run, row, pr) and pr.state == 'OPEN'
                and pr.actor == world.publisher_actor and child.source == pr.head
                and kernel._pr_target(run, row, child) is not None)


def _basis(row: sqlite3.Row, job: sqlite3.Row, run: sqlite3.Row, world: World) -> bool:
    return bool(world.source is not None and world.target is not None
        and row['generation'] == job['generation'] and row['run_sha'] == world.source.sha
        and row['default_sha'] == world.target.sha and row['revision'] == job['revision']
        and row['parent_revision'] == run['parent_revision'] and row['graph_revision'] == run['graph_revision']
        and row['completion_revision'] == job['completion_revision'] and row['review_attempt'] == job['review_attempt']
        and row['integrated_sha'] == job['integrated_sha'] and row['integrated_tree'] == job['integrated_tree']
        and row['expected_tree'] == job['expected_tree'])


def authority(kernel: Kernel, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
              effect: sqlite3.Row, world: World) -> bool:
    row = current(tx, job['id'])
    payload = json.loads(tx.get(effect['payload_id'], job['id']))
    if (row is None or payload.get('repair_id') != row['id'] or not _basis(row, job, run, world)
            or not _identity(kernel, run, row, world) or not kernel._review_current(tx, job, world)
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


def observe_source_append(kernel: Kernel, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                          world: World, effect: sqlite3.Row | None = None) -> Step | None:
    row, child = current(tx, job['id']), world.repair
    if row is None or child is None or child.source is None or child.source.sha == row['published_sha']:
        return None
    pr = child.pr
    valid_pr = (pr is None and row['pr_number'] is None or pr is not None and pr.state == 'OPEN'
        and kernel._pr_identity(run, row, pr) and pr.actor == world.publisher_actor
        and kernel._pr_target(run, row, child) is not None)
    if (not row['published_sha'] or not valid_pr or child.target != world.source
            or (row['published_sha'], child.source.sha) not in child.ancestry):
        kernel._gate(tx, job['id'], 'repair_pr_identity_changed')
        return Step('paused', job['id'], 'repair_pr_identity_changed')
    allowance = tx.one('SELECT * FROM resync_allowances WHERE run_id=? AND subject=? AND current=1',
                       (run['id'], job['id']))
    if allowance['used'] >= allowance['allowance']:
        kernel._gate(tx, job['id'], 'integration_resync_exhausted')
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
    kernel._update_boundary(tx, run, job)
    kernel._event(tx, job['id'], 'repair_source_append', row['id'], evidence)
    return Step('progress', job['id'], 'repair_source_changed')


def refresh_prepared(kernel: Kernel, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                     effect: sqlite3.Row, world: World) -> Step | None:
    """Retire only an unstarted intent when its legal combination advances."""
    appended = observe_source_append(kernel, tx, run, job, world, effect)
    if appended is not None:
        return appended
    row = current(tx, job['id'])
    if row is None or not _identity(kernel, run, row, world):
        kernel._gate(tx, job['id'], 'repair_pr_identity_changed')
        return Step('paused', job['id'], 'repair_pr_identity_changed')
    if not kernel._observe_integration_snapshot(tx, job, world):
        return Step('paused', job['id'], 'integration_snapshot')
    if _basis(row, job, run, world):
        return None
    tx.execute("UPDATE effects SET status='retired' WHERE run_id=? AND id=? AND status='prepared'",
               (run['id'], effect['id']))
    kernel._update_boundary(tx, run, job)
    return Step('progress', job['id'], 'repair_basis_changed')


def reconcile_unapplied(kernel: Kernel, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                        effect: sqlite3.Row, result: EffectResult, world: World) -> Step | None:
    """A sent merge can be retired only after its exact non-application proof."""
    row, child = current(tx, job['id']), world.repair
    if row is None or child is None or child.source is None or child.target is None:
        return kernel._wait(tx, run, job, 'github_convergence', 600)
    changed = (not _basis(row, job, run, world) or row['published_sha'] != child.source.sha)
    if not changed:
        return None
    pr, proof = result.pr, result.raw.get('merge_not_applied')
    valid = (isinstance(proof, dict) and proof == {
        'before_sha': effect['before_sha'], 'after_sha': effect['after_sha'],
        'source_sha': child.source.sha, 'target_sha': child.target.sha, 'pr_number': effect['pr_number']}
        and pr is not None and child.pr is not None and pr.state == child.pr.state == 'OPEN'
        and pr.head == child.pr.head == child.source and pr.actor == effect['actor'] == world.publisher_actor
        and kernel._pr_identity(run, effect, pr) and kernel._pr_identity(run, row, child.pr)
        and child.target == world.source
        and (effect['before_sha'], child.target.sha) in child.ancestry
        and (effect['after_sha'], child.source.sha) in child.ancestry
        and (effect['after_sha'], child.target.sha) not in child.ancestry)
    if not valid:
        return kernel._wait(tx, run, job, 'github_convergence', 600)
    material = tx.put(job['id'], 'repair_merge_not_applied', wire_json(asdict(result)))
    tx.execute("UPDATE effects SET status='retired' WHERE run_id=? AND id=? AND status='sent'",
               (run['id'], effect['id']))
    kernel._event(tx, job['id'], 'repair_merge_not_applied', effect['id'], material)
    refreshed = refresh_prepared(kernel, tx, run, job, effect, world)
    if refreshed is not None:
        return refreshed
    kernel._update_boundary(tx, run, job)
    return Step('progress', job['id'], 'repair_basis_changed')


def advance(kernel: Kernel, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
            work: Work, world: World) -> Step | IORequest:
    row, child = current(tx, job['id']), world.repair
    if row is None or child is None:
        raise KernelError('repair_observation_missing')
    appended = observe_source_append(kernel, tx, run, job, world)
    if appended is not None:
        return appended
    if not _identity(kernel, run, row, world):
        kernel._gate(tx, job['id'], 'repair_pr_identity_changed')
        return Step('paused', job['id'], 'repair_pr_identity_changed')
    if not kernel._observe_integration_snapshot(tx, job, world):
        return Step('paused', job['id'], 'integration_snapshot')
    if not _basis(row, job, run, world):
        kernel._update_boundary(tx, run, job)
        return Step('progress', job['id'], 'repair_basis_changed')
    stage = job['phase'].removeprefix('repair_')
    if stage == 'push':
        if row['published_sha'] == row['integrated_sha']:
            return _phase(tx, run['id'], job['id'], 'repair_pr')
        return kernel._prepare_effect(tx, run, job, work, world, 'push',
            before=row['published_sha'] or row['run_sha'], after=row['integrated_sha'], repair=row)
    if stage == 'pr':
        pr = child.pr
        title = text("repair.pr_title", language=run['language'], issue=job['issue'], sequence=row['sequence'])
        body = text("repair.pr_body", language=run['language'], issue=job['issue'],
                    candidate=row['integrated_sha'], review=row['review_attempt'])
        return kernel._prepare_effect(tx, run, job, work, world, 'pr_write',
            before=row['integrated_sha'], after=row['integrated_sha'], repair=row,
            payload={'title': title, 'body': body, 'prior': None if pr is None else {'title': pr.title, 'body': pr.body}})
    if stage not in {'checks', 'merge'}:
        raise KernelError('repair_phase_invalid')
    shadow = dict(job, pr_number=row['pr_number'], publication_sha=row['integrated_sha'])
    checks = child.checks
    material = None
    if checks is not None and checks.pr_number == row['pr_number'] and checks.head_sha == row['integrated_sha']:
        material = tx.put(job['id'], 'required_checks', wire_json(asdict(checks)))
        tx.execute('INSERT OR IGNORE INTO checks(id,subject,pr_number,head_sha,result,material_id,run_id) VALUES (?,?,?,?,?,?,?)',
                   (material, job['id'], checks.pr_number, checks.head_sha, checks.result, material, run['id']))
        tx.execute('UPDATE run_repairs SET checks_id=? WHERE run_id=? AND id=?', (material, run['id'], row['id']))
    waiting = kernel._checks_wait(tx, run, shadow, checks)
    if waiting is not None:
        return waiting
    assert checks is not None and material is not None
    tx.execute("DELETE FROM waits WHERE run_id=? AND subject=? AND kind='required_checks'", (run['id'], job['id']))
    if checks.result == 'fail':
        tx.execute("UPDATE subjects SET phase='development',purpose='ci',repair_source='required_checks',ci_failure=?,last_failure=?,grant_id=NULL,integrated_sha=NULL,integrated_tree=NULL,version=version+1 WHERE run_id=? AND id=?",
                   (material, material, run['id'], job['id']))
        kernel._release_integration(tx, job['id'])
        kernel._event(tx, job['id'], 'ci_failed', material, material)
        return Step('progress', job['id'])
    if stage == 'checks':
        kernel._event(tx, job['id'], 'repair_checks_observed', row['id'], material)
        return _phase(tx, run['id'], job['id'], 'repair_merge')
    return kernel._prepare_effect(tx, run, job, work, world, 'merge', before=row['run_sha'], after=row['integrated_sha'],
        repair=current(tx, job['id']), payload={'method': 'merge', 'parents': [row['run_sha'], row['integrated_sha']],
                                             'commit_message': f"merge: Parent #{job['issue']} repair {row['sequence']}"})


def _phase(tx: Transaction, run_id: str, subject: str, phase: str) -> Step:
    tx.execute('UPDATE subjects SET phase=?,version=version+1 WHERE run_id=? AND id=?', (phase, run_id, subject))
    return Step('progress', subject)


def consume(kernel: Kernel, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
            effect: sqlite3.Row, world: World, *, stale: bool = False) -> Step:
    payload = json.loads(tx.get(effect['payload_id'], job['id']))
    row = tx.one('SELECT * FROM run_repairs WHERE run_id=? AND id=?', (run['id'], payload['repair_id']))
    result = json.loads(tx.get(effect['receipt_id'], job['id']))
    # Use this intent's original tree, never the latest mutable candidate.
    shadow = dict(job, publication_tree=payload['integrated_tree'])
    kernel._validate_target(tx, run, shadow, effect, result)
    if result['disposition'] != 'target':
        raise KernelError('repair_write_not_confirmed')
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
            and row['completion_revision'] == job['completion_revision'] and kernel._review_current(tx, job, world)
            and commit['tree'] == row['integrated_tree'] == row['expected_tree']
            and world.repair is not None
            and all((row[key], row['integrated_sha']) in world.repair.ancestry
                    for key in ('candidate_sha', 'default_sha', 'run_sha')))
        tx.execute('UPDATE subjects SET source_sha=?,integrated_sha=?,integrated_tree=?,grant_id=NULL,checks_id=NULL WHERE run_id=? AND id=?',
                   (commit['sha'], commit['sha'] if applicable else job['integrated_sha'],
                    commit['tree'] if applicable else job['integrated_tree'], run['id'], job['id']))
        kernel._record_source_write(tx, job['id'], job['source_ref'], commit['sha'])
        phase = 'publish' if applicable else 'integration'
        kernel._event(tx, job['id'], 'repair_merged', row['id'], effect['receipt_id'])
        if applicable:
            kernel._acceptance_promoted(tx, kernel._job(tx, job['id']), world)
    tx.execute("UPDATE effects SET status='confirmed' WHERE run_id=? AND id=?", (run['id'], effect['id']))
    if not stale:
        _phase(tx, run['id'], job['id'], phase)
    kernel._event(tx, job['id'], 'effect_consumed', effect['id'], effect['receipt_id'])
    return Step('progress', job['id'])
