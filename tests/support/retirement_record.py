"""Recorded retirement inputs for the real SQLite consumer, not a producer proof.

No PR is published or closed here. The transport and generation journey tests
own that evidence; these rows isolate authorization of an already-recorded close.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict, replace

from agent_run.model import Commit, EffectResult, PullRequest, wire_json
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.repository_scope import connection, insert, seed
from support.run_scoped_kernel import ScopedKernel

SUBJECT = 'parent:1'
BASE = Commit('a' * 40, 'b' * 40)
HEAD = Commit('c' * 40, 'd' * 40)


@contextmanager
def confirmed_retirement():
    with closing(connection()) as db:
        db.execute('BEGIN IMMEDIATE')
        seed(db, 'A')
        tx = RunTransaction(Transaction(db), 'A')
        prior = PullRequest(11, 'example/project', 'refs/heads/work', 'refs/heads/main',
            HEAD, BASE, 'Original PR', 'Original body', actor='controlled-actor',
            head_repository='example/project')
        intent = {'repository': prior.repository, 'pr_number': prior.number,
            'source_ref': prior.source_ref, 'target_ref': prior.target_ref,
            'head_sha': HEAD.sha, 'base_sha': BASE.sha, 'author': prior.actor,
            'baseline_event_id': 0, 'effect_id': 'retirement', 'control_action': 'first',
            'run_id': 'A', 'subject': SUBJECT, 'generation': 1,
            'control_kind': 'requeue', 'actor': prior.actor,
            'comment_body': 'Retiring the superseded generation.\n\n<!-- agent-run-retirement:retirement -->'}
        payload = {'prior_pr': asdict(prior), 'retirement_intent': intent,
                   'control_action': 'first', 'control_kind': 'requeue'}
        closed = replace(prior, state='CLOSED')
        receipt = asdict(EffectResult('target', pr=closed, raw={
            'close_intent': {**intent, 'comment_id': 7, 'comment_created_at': '2026-01-01T00:00:00+00:00'},
            'event': {'id': 1, 'event': 'closed', 'actor': prior.actor,
                      'created_at': '2026-01-01T00:00:01+00:00'}}))
        payload_id = tx.put(SUBJECT, 'effect_intent', wire_json(payload))
        receipt_id = tx.put(SUBJECT, 'effect_receipt', wire_json(receipt))
        for action, status in (('first', 'superseded'), ('second', 'pending')):
            insert(db, 'actions', {'run_id': 'A', 'id': action, 'kind': 'requeue',
                'subject': SUBJECT, 'status': status})
        db.execute("UPDATE subjects SET pr_number=11,publication_sha=?,publication_tree=?,phase='approval' WHERE run_id='A'",
                   (HEAD.sha, HEAD.tree))
        insert(db, 'effects', {'run_id': 'A', 'id': 'retirement', 'subject': SUBJECT,
            'operation': 'retire_pr', 'status': 'confirmed', 'parent_revision': 'r',
            'graph_revision': 'g', 'revision': 'r', 'generation': 1,
            'source_ref': prior.source_ref, 'target_ref': prior.target_ref,
            'before_sha': HEAD.sha, 'base_sha': BASE.sha, 'base_tree': BASE.tree,
            'pr_number': prior.number, 'actor': prior.actor, 'payload_id': payload_id,
            'receipt_id': receipt_id, 'attempts': 1, 'control_action': 'first'})
        db.commit()
        yield db, ScopedKernel(db, 'A'), closed, receipt
