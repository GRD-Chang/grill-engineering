"""Recorded retirement readback must prove complete, ordered foreign intervention.

Inputs are explicit consumer records. Real external events and generation changes
remain covered by the retirement transport and closed-base requeue journeys.
"""
from copy import deepcopy

import pytest

from agent_run.model import EffectResult
from support.retirement_record import SUBJECT, confirmed_retirement


@pytest.mark.parametrize('read_gap', [None, 'original_event', 'actor', 'time', 'duplicate_id'])
def test_confirmed_retirement_recheck_requires_complete_ordered_foreign_history(read_gap):
    with confirmed_retirement() as (db, kernel, closed, receipt):
        events = [deepcopy(receipt['raw']['event']),
            {'id': 2, 'event': 'reopened', 'actor': 'another-maintainer', 'created_at': '2026-01-01T00:00:02+00:00'},
            {'id': 3, 'event': 'closed', 'actor': 'another-maintainer', 'created_at': '2026-01-01T00:00:03+00:00'}]
        if read_gap == 'original_event':
            events = events[1:]
        elif read_gap == 'actor':
            events[-1]['actor'] = None
        elif read_gap == 'time':
            events[-1]['created_at'] = '2000-01-01T00:00:00Z'
        elif read_gap == 'duplicate_id':
            events[-1]['id'] = events[0]['id']
        result = EffectResult('unknown', pr=closed, raw={
            'state_events': events, 'close_intent': receipt['raw']['close_intent']})
        with kernel.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            job = kernel._job(tx, SUBJECT)
            action = tx.one("SELECT * FROM actions WHERE run_id='A' AND id='second'")
            row = tx.one("SELECT * FROM effects WHERE run_id='A'")
            original = dict(row)
            payload = tx.get(row['payload_id'], SUBJECT)
            receipt_bytes = tx.get(row['receipt_id'], SUBJECT)
            step = kernel._recheck_retirement(tx, run, job, action, row, result)
        assert (step.kind, step.reason) == (('progress', 'requeue_retirement_changed') if read_gap is None
                                           else ('paused', 'write_result_unknown'))
        with kernel.storage.transaction(write=False) as tx:
            retained = tx.one("SELECT * FROM effects WHERE run_id='A'")
            assert dict(retained) == original
            assert tx.get(retained['payload_id'], SUBJECT) == payload
            assert tx.get(retained['receipt_id'], SUBJECT) == receipt_bytes
            assert tx.one("SELECT generation FROM subjects WHERE run_id='A'")[0] == 1
            assert tx.one("SELECT status FROM actions WHERE run_id='A' AND id='second'")[0] == (
                'superseded' if read_gap is None else 'pending')
            assert tx.one("SELECT code FROM gates WHERE run_id='A'")[0] == step.reason
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None
