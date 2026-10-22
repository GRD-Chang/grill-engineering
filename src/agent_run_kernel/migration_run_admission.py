"""Authenticate immutable original-source facts at existing Task admission."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from agent_run.managed_workspace import ManagedWorkspace

from .model import KernelError
from .storage import Storage, Transaction
from .migration_quiescence import read_v6_host_exit


def _refuse() -> KernelError:
    return KernelError('legacy_requires_offline_migration', 'The original Run transfer facts do not match its retired source')


def authenticate_run_transfer(tx: Transaction, imported: sqlite3.Row, body: Mapping[str, Any],
        receipt: Mapping[str, str], managed: ManagedWorkspace, db: sqlite3.Connection) -> None:
    """Match exact original PKs and stored proofs, never reauthorize live resources."""
    run_id = receipt['run_id']
    factory = db.row_factory
    try:
        db.row_factory = sqlite3.Row
        read_v6_host_exit(db, run_id, required=True)
    finally:
        db.row_factory = factory
    keys = {'origin_nonce', 'source_sha256', 'profile_sha256', 'external_readproof_sha256',
            'resource_mode', 'run_record', 'run_ref_ownership', 'subjects'}
    # Historical untagged manifests retain their exact WholeRun semantics.
    # A Single label only selects the source SQL owner path, never authority.
    scope = body.get('source_scope', 'whole-run')
    if (scope not in {'single-ticket', 'whole-run'} or set(body) not in (keys, keys | {'source_scope'})
            or imported['purpose'] != 'v6_run_import' or imported['identity'] != receipt['nonce']
            or imported['generation'] != 1 or body['origin_nonce'] != receipt['nonce']
            or body['source_sha256'] != receipt['source_sha256'] or body['resource_mode'] != 'existing-verified'
            or body['run_record'] != {'table': 'run_root', 'key': [run_id]}):
        raise _refuse()
    for field in ('profile_sha256', 'external_readproof_sha256'):
        if not isinstance(body[field], str) or re.fullmatch('[0-9a-f]{64}', body[field]) is None:
            raise _refuse()
    roots = db.execute('SELECT r.value_run_branch,r.value_graph_snapshot_id,g.version_id,b.value_sha FROM run_root r '
                       'JOIN run_graph_snapshots g ON g.run_id=r.run_id AND g.snapshot_id=r.value_graph_snapshot_id '
                       'JOIN run_condition_base b ON b.run_id=r.run_id WHERE r.run_id=?', (run_id,)).fetchall()
    if len(roots) != 1:
        raise _refuse()
    root = roots[0]
    members = {row[0] for row in db.execute('SELECT ticket_number FROM run_graph_members WHERE run_id=? AND version_id=?', (run_id, root[2]))}
    entries = body['subjects']
    if (not isinstance(entries, list) or len(entries) != len(members)
            or any(not isinstance(entry, dict) or set(entry) != {'subject', 'generation', 'proof_id'} for entry in entries)
            or {entry['subject'] for entry in entries} != {f'ticket:{number}' for number in members}
            or tx.one("SELECT count(*) FROM history WHERE kind='v6_subject_import'")[0] != len(entries)):
        raise _refuse()
    slots = db.execute('SELECT container,slot,job_id FROM work_job_slots WHERE run_id=?', (run_id,)).fetchall()
    if any(row[0] != 'ticket_jobs' or row[1] not in {str(number) for number in members} for row in slots):
        raise _refuse()
    active = db.execute('SELECT active_job_id FROM work_job_layout WHERE run_id=?', (run_id,)).fetchone()
    if active is None or scope == 'single-ticket' and len(members) != 1:
        raise _refuse()
    current_jobs: set[str] = set()
    seen_refs = {'refs/heads/' + root[0]}
    seen_checkouts: set[str] = set()
    for entry in entries:
        subject = entry['subject']
        number = int(subject.removeprefix('ticket:'))
        native = tx.one('SELECT id,issue,generation FROM subjects WHERE id=?', (subject,))
        fact = tx.one("SELECT h.identity,h.material_id,h.generation,m.purpose FROM history h JOIN material m ON m.id=h.material_id WHERE h.kind='v6_subject_import' AND h.subject=?", (subject,))
        proof = json.loads(tx.get(entry['proof_id'], subject))
        if (entry['generation'] != 1 or native['issue'] != number or native['generation'] < 1
                or fact['identity'] != receipt['nonce'] or fact['generation'] != 1
                or fact['purpose'] != 'v6_subject_import' or fact['material_id'] != entry['proof_id']
                or not isinstance(proof, dict) or set(proof) != {'origin_nonce', 'source_sha256', 'subject', 'generation',
                    'kind', 'source_record', 'aliases', 'historical_base', 'budget_owner', 'current_attempt', 'resources'}
                or proof['origin_nonce'] != receipt['nonce'] or proof['source_sha256'] != receipt['source_sha256']
                or proof['subject'] != subject or proof['generation'] != 1):
            raise _refuse()
        selected = [row for row in slots if row[1] == str(number)]
        if len(selected) > 1:
            raise _refuse()
        # Single selection slots can still name the inert selection job while
        # work_job_layout names the real generation owner. Do not invent a slot.
        job_id = active[0] if scope == 'single-ticket' else selected[0][2] if selected else None
        aliases = [{'table': 'work_job_slots', 'key': [run_id, row[0], row[1]]}
            for row in selected if row[2] == job_id]
        jobs = [] if job_id is None else db.execute('SELECT stage,value_ticket_number,value_base_sha,value_ticket_branch,'
            'value_candidate_sha,budget_owner_id FROM work_jobs WHERE run_id=? AND job_id=?', (run_id, job_id)).fetchall()
        if job_id is not None and (len(jobs) != 1 or jobs[0][1] != number):
            raise _refuse()
        if job_id == f'ticket:{number}:selection':
            if jobs[0][0] is not None:
                raise _refuse()
            aliases.append({'table': 'work_jobs', 'key': [run_id, job_id]})
        if active[0] == job_id and job_id is not None:
            aliases.append({'table': 'work_job_layout', 'key': [run_id]})
        if proof['aliases'] != aliases or not isinstance(proof['resources'], list):
            raise _refuse()
        if proof['kind'] == 'unallocated':
            if (job_id not in (None, f'ticket:{number}:selection')
                    or proof['source_record'] != {'table': 'run_graph_members', 'key': [run_id, root[2], number]}
                    or any(proof[field] is not None for field in ('historical_base', 'budget_owner', 'current_attempt'))
                    or proof['resources']):
                raise _refuse()
            continue
        if job_id != f'ticket:{number}:generation:1' or proof['kind'] not in {'completed', 'returned'}:
            raise _refuse()
        current_jobs.add(job_id)
        job = jobs[0]
        record = {'table': 'work_jobs', 'key': [run_id, job_id]}
        attempts = db.execute('SELECT pending_attempt_id FROM work_job_attempt_roots WHERE run_id=? AND job_id=?', (run_id, job_id)).fetchall()
        if (len(attempts) != 1 or proof['source_record'] != record or proof['budget_owner'] != record
                or job[5] != job_id or proof['historical_base'] != job[2]
                or proof['current_attempt'] != attempts[0][0]
                or (job[0] == 'completed') != (proof['kind'] == 'completed')
                or proof['kind'] == 'returned' and job[0] not in {'developing', 'reviewing', 'accepted'}
                or db.execute('SELECT 1 FROM work_job_gates WHERE run_id=? AND job_id=? LIMIT 1', (run_id, job_id)).fetchone()):
            raise _refuse()
        if proof['kind'] == 'completed' and proof['resources']:
            raise _refuse()  # First whole-Run unit proves completed resources absent.
        if proof['kind'] == 'returned' and len(proof['resources']) != 1:
            raise _refuse()
        for resource in proof['resources']:
            if not isinstance(resource, dict) or set(resource) != {'layout', 'checkout_index_sha256', 'source_record'}:
                raise _refuse()
            layout = resource['layout']
            archive = layout.get('checkout_archive_sha256') if isinstance(layout, dict) else None
            checkout = managed.state_root / 'worktrees' / run_id / f'ticket-{number}'
            ref = 'refs/heads/' + job[3]
            expected = {'version': 1, 'nonce': receipt['nonce'], 'source_sha256': receipt['source_sha256'],
                'source_uuid': receipt['source_uuid'], 'repository_root': str(managed.repository_root),
                'checkout': str(checkout), 'owned_root': str(checkout.parent), 'common_git_dir': str(managed.repository_root / '.git'),
                'source_ref': ref, 'expected_oid': job[4] or job[2], 'before_head': 'ref: ' + ref + '\n',
                'checkout_archive_sha256': archive}
            if (layout != expected or resource['source_record'] != record or ref in seen_refs or str(checkout) in seen_checkouts
                    or not isinstance(archive, str) or re.fullmatch('[0-9a-f]{64}', archive) is None
                    or not isinstance(resource['checkout_index_sha256'], str)
                    or re.fullmatch('[0-9a-f]{64}', resource['checkout_index_sha256']) is None):
                raise _refuse()
            seen_refs.add(ref)
            seen_checkouts.add(str(checkout))
    if current_jobs != {row[0] for row in db.execute('SELECT job_id FROM work_jobs WHERE run_id=? AND stage IS NOT NULL', (run_id,))}:
        raise _refuse()
    from .migration_records import V6RecordReader
    from .migration_run_ownership import authenticate_run_ownership

    try:
        records = V6RecordReader(Storage(Path(receipt['target_db'])))
        authenticate_run_ownership(tx, body, receipt, managed.repository_root, records, source=db)
    except (KernelError, KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        raise _refuse() from error
