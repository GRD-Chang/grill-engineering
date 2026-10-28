"""Short handshake state tests; no Core, Host, process or native clock."""
from threading import Event

import pytest

from support import observer_errors
from support.observer_errors import HeldCompletion, ObserverErrors


def test_completion_is_proved_while_original_worker_is_held_then_returned():
    held, polled = HeldCompletion(), Event()
    polled.set()
    with held.hold():
        assert held.wait(polled, 8)
        assert held.complete_if_held()
        assert held.wait(held.completed, 8)
        assert not held.returned.is_set()
        held.mark_returned()
        assert held.snapshot() == dict(held=True, timed_out=False, completed=True, returned=True)
        assert not held.complete_if_held()
    assert held.snapshot() == dict(held=False, timed_out=False, completed=True, returned=True)


@pytest.mark.parametrize('stage', ['poll', 'completion'])
@pytest.mark.parametrize('ready', [False, True])
def test_deadline_rejects_late_observer_before_waiting_worker_marks_timeout(monkeypatch, stage, ready):
    held, polled = HeldCompletion(), Event()
    now, acknowledgements = [100.0], []
    monkeypatch.setattr(observer_errors, 'monotonic', lambda: now[0])
    with held.hold():
        if stage == 'completion':
            polled.set()
            assert held.wait(polled, 8)
        event = polled if stage == 'poll' else held.completed
        def timed_wait(timeout):
            assert timeout == 8
            now[0] = 108.0
            # An observer finishing its real checks now loses the atomic race,
            # even though the Worker has not yet marked its timeout flag.
            acknowledgements.append(held.complete_if_held())
            return ready
        monkeypatch.setattr(event, 'wait', timed_wait)
        assert not held.wait(event, 8)
        assert held.snapshot() == dict(held=True, timed_out=True, completed=False, returned=False)
        assert not held.complete_if_held()
    assert acknowledgements == [False] and not held.complete_if_held()


@pytest.mark.parametrize('failure', [None, ValueError('Original Worker failure')])
def test_observer_cannot_claim_completion_after_worker_leaves_hold(failure):
    held = HeldCompletion()
    try:
        with held.hold():
            if failure is not None:
                raise failure
    except ValueError as error:
        assert error is failure
    assert held.snapshot() == dict(held=False, timed_out=False, completed=False, returned=False)
    assert not held.complete_if_held()


def test_wait_never_keeps_state_lock_across_blocking_boundary(monkeypatch):
    held, polled = HeldCompletion(), Event()
    def proof_during_wait(timeout):
        assert timeout == 8 and held._lock.acquire(blocking=False)
        held._lock.release()
        assert held.complete_if_held()
        return True
    monkeypatch.setattr(polled, 'wait', proof_during_wait)
    with held.hold():
        assert held.wait(polled, 8)
        assert held.wait(held.completed, 8)
        held.mark_returned()


def test_return_without_real_completion_is_rejected():
    held = HeldCompletion()
    with held.hold():
        with pytest.raises(AssertionError):
            held.mark_returned()
        assert not held.returned.is_set()


def test_sender_unknown_conversion_cannot_hide_original_transport_assertion():
    errors, failure = ObserverErrors(), AssertionError('Original transport ordering failure')
    try:
        with errors.capture():
            raise failure
    except Exception:
        result = {'outcome': 'unknown'}  # The real sender deliberately catches transport exceptions.
    assert result == {'outcome': 'unknown'}
    with pytest.raises(AssertionError) as caught:
        errors.raise_if_any()
    assert caught.value is failure


@pytest.mark.parametrize('unavailable', [False, True])
def test_sender_attempt_note_preserves_error_and_exact_first_gate_facts(tmp_path, monkeypatch, unavailable):
    import json
    from agent_run.storage import notification_outbox as outbox
    from support.notification import sender_attempt_note
    history = [{'number': 1, 'started_at': 100, 'finished_at': 101, 'outcome': 'unknown',
                'reason': 'Private transport exception detail'}]
    document = {'pending': [{'event': {'id': 'gate', 'summary': 'Private message'},
                            'attempts': 0, 'outcome': 'pending', 'deadline_at': 115, 'attempt_history': []}],
                'records': [{'id': 'first', 'attempts': 1, 'outcome': 'unknown', 'deadline_at': 110,
                             'attempt_history': history}, {'id': 'foreign', 'attempts': 99}]}
    calls = []
    def read(root, run):
        calls.append((root, run))
        if unavailable:
            raise OSError('Private unreadable journal detail')
        return document
    monkeypatch.setattr(outbox, 'read_notifications', read)
    failure = AssertionError('Original ordering assertion')
    sender_attempt_note(failure, tmp_path, 'run', first_id='first', gate_ids=['gate'])
    assert calls == [(tmp_path, 'run')] and len(failure.__notes__) == 1
    assert 'Private' not in failure.__notes__[0] and len(failure.__notes__[0].encode()) <= 8192
    if unavailable:
        assert failure.__notes__[0] == 'Sender attempt evidence unavailable: OSError'
    else:
        facts = json.loads(failure.__notes__[0])
        assert [(row['id'], row['section'], row['attempts'], row['outcome']) for row in facts['sender_attempts']] == [
            ('gate', 'pending', 0, 'pending'), ('first', 'records', 1, 'unknown')]
        assert facts['sender_attempts'][1]['attempt_history'] == [{key: history[0][key] for key in
            ('number', 'started_at', 'finished_at', 'outcome')}]
