"""Producer wakes stay independent of the notification owner's real IO."""
from contextlib import closing, contextmanager
import fcntl
import sqlite3
from threading import Event, Thread, current_thread

import pytest

from agent_run.notifications.sender import NativeNotifications
from agent_run.storage import notification_outbox as outbox
from support.notification import _kernel, _operations, observe_and_wait
from support.run_scoped_kernel import ScopedKernel


@pytest.mark.parametrize('obstruction', ['projection', 'journal_lock'])
def test_blocked_notification_owner_cannot_hold_producer_or_core_preparation(tmp_path, monkeypatch, obstruction):
    kernel = _kernel(tmp_path)
    entered, release, constructed, advanced = (Event() for _ in range(4))
    observers, errors, preparations, owners = [], [], [], []
    original_page, original_transaction = NativeNotifications._page, outbox.transaction
    lock = None

    def page(self, after):
        result = original_page(self, after)
        if not entered.is_set():
            owners.append(current_thread().name)
            entered.set()
            assert release.wait(3), 'projection was not released'
        return result

    @contextmanager
    def transaction(*args, **kwargs):
        owners.append(current_thread().name)
        entered.set()
        with original_transaction(*args, **kwargs) as document:
            yield document

    if obstruction == 'projection':
        monkeypatch.setattr(NativeNotifications, '_page', page)
    else:
        path = outbox.notification_path(tmp_path, 'run-native').with_suffix('.lock')
        path.parent.mkdir()
        lock = path.open('a+')
        fcntl.flock(lock, fcntl.LOCK_EX)
        monkeypatch.setattr(outbox, 'transaction', transaction)

    def construct():
        try:
            observers.append(NativeNotifications(kernel.storage.path, tmp_path, storage=kernel.storage,
                transport=lambda *_args, **_kwargs: {'outcome': 'success', 'reason': None}))
            constructed.set()
        except BaseException as error:
            errors.append(error)

    def produce():
        try:
            observer = observers[0]
            observer.observe_committed()
            # Existing controlled Core transaction seam exercises actual work
            # selection/preparation. It is not native FIFO or Host-launch proof.
            with closing(sqlite3.connect(kernel.storage.path, isolation_level=None)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                core = ScopedKernel(db, 'run-native')
                core.storage.path = kernel.storage.path
                core.storage.run_id = 'run-native'
                core.clock = kernel.clock
                execution = core.claim_executor('producer-fixture')
                operations = _operations(core, execution)
                operations.prepare_agents = False
                step = operations.once()
                assert step.preparation is not None
                preparations.append(step.preparation)
            observer.observe_committed()
            assert kernel.control('stop', 'producer-stop') == 'applied'
            observer.observe_committed()
            advanced.set()
        except BaseException as error:
            errors.append(error)

    constructor, producer = Thread(target=construct), Thread(target=produce)
    try:
        constructor.start()
        assert entered.wait(3)
        assert constructed.wait(0.25), 'constructor projected on its producer thread'
        producer.start()
        assert advanced.wait(0.5), 'notification IO blocked wake, next Core preparation, or Stop'
        assert len(preparations) == 1 and not errors
        assert all(owner == 'kernel-notifications' for owner in owners)
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
        release.set()
        observer = observers[0]
        observe_and_wait(observer)
        observer.close(timeout_seconds=3)
        assert observer.closed and observer.failure_reason is None
        document = outbox.read_notifications(tmp_path, 'run-native')
        assert any(item['kind'] == 'boundary' and item['outcome'] == 'success' for item in document['records'])
        with kernel.storage.transaction(write=False) as tx:
            highwater = tx.one('SELECT max(position) FROM history')[0]
        assert document['native_cursor'] == highwater
        assert document['native_boundary_cursor'] == highwater
    finally:
        release.set()
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        constructor.join(3)
        if producer.ident is not None:
            producer.join(3)
        for observer in observers:
            observer.close(drain=False)
            if observer.thread is not None:
                observer.thread.join(3)
                assert not observer.thread.is_alive()
            physical = observer._call.thread if observer._call is not None else None
            if physical is not None:
                physical.join(3)
                assert not physical.is_alive()
        assert not constructor.is_alive() and not producer.is_alive() and not errors


def test_unexpected_physical_thread_exit_keeps_unknown_and_reclaims_both_threads(tmp_path):
    kernel = _kernel(tmp_path)
    calls = []

    def transport(*_args, **_kwargs):
        calls.append('original request')
        raise SystemExit('physical call ended without an ACK')

    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        observer.close(timeout_seconds=3)
        assert observer.closed and observer.thread is not None and not observer.thread.is_alive()
        assert observer._call is None or observer._call.thread is None or not observer._call.thread.is_alive()
        record, = outbox.read_notifications(tmp_path, 'run-native')['records']
        assert record['outcome'] == 'unknown' and record['attempts'] == 1
        assert 'not_sent' not in record['attempt_history'][0]
        assert calls == ['original request']
    finally:
        observer.close(drain=False)


@pytest.mark.parametrize('phase', ['claim', 'fault_receipt'])
def test_sender_journal_contention_keeps_wake_and_finite_close_outside_condition(tmp_path, monkeypatch, phase):
    kernel = _kernel(tmp_path)
    armed, ready, handoff, contending, produced, close_returned = (Event() for _ in range(6))
    after_scan = Event()
    errors = []
    original_validity = NativeNotifications._exit_validity
    original_wake = NativeNotifications._wake_pending
    original_boundaries = NativeNotifications._boundaries
    original_pause = NativeNotifications._pause
    original_transaction = outbox.transaction

    def transport(*_args, **_kwargs):
        armed.set()
        return {'outcome': 'success', 'reason': None}

    def wake(self, document):
        original_wake(self, document)
        if armed.is_set():
            after_scan.set()

    def validity(self):
        if phase == 'claim' and after_scan.is_set() and not ready.is_set():
            ready.set()
            assert handoff.wait(3)
        return original_validity(self)

    def boundaries(self, document):
        if phase == 'fault_receipt' and armed.is_set():
            raise OSError('original projection fault')
        return original_boundaries(self, document)

    def pause(self, error):
        if phase == 'fault_receipt' and not ready.is_set():
            ready.set()
            assert handoff.wait(3)
        original_pause(self, error)

    @contextmanager
    def transaction(*args, **kwargs):
        if ready.is_set() and handoff.is_set():
            contending.set()
        with original_transaction(*args, **kwargs) as document:
            yield document

    monkeypatch.setattr(NativeNotifications, '_wake_pending', wake)
    monkeypatch.setattr(NativeNotifications, '_exit_validity', validity)
    monkeypatch.setattr(NativeNotifications, '_boundaries', boundaries)
    monkeypatch.setattr(NativeNotifications, '_pause', pause)
    monkeypatch.setattr(outbox, 'transaction', transaction)
    # Exercise the existing Feishu-only cleanup branch with a controlled
    # unavailable Status source, without changing any cleanup deadline.
    def unavailable_status(*_args, **_kwargs):
        raise ValueError('Status source unavailable in this Feishu ownership case')
    monkeypatch.setattr('agent_run.notifications.sender.status_subjects', unavailable_status)
    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)

    def produce():
        try:
            observer.observe_committed()
            assert kernel.control('stop', 'stop-during-notification-lock') == 'applied'
            produced.set()
        except BaseException as error:
            errors.append(error)

    def close_sender():
        try:
            observer.close(timeout_seconds=0)
            close_returned.set()
        except BaseException as error:
            errors.append(error)

    producer, closer = Thread(target=produce), Thread(target=close_sender)
    lock_path = outbox.notification_path(tmp_path, 'run-native').with_suffix('.lock')
    lock_path.parent.mkdir(exist_ok=True)
    lock = lock_path.open('a+')
    try:
        assert not observer.status_enabled
        assert ready.wait(3)
        fcntl.flock(lock, fcntl.LOCK_EX)
        handoff.set()
        assert contending.wait(3)
        producer.start()
        assert produced.wait(0.25), 'journal contention held the producer condition'
        closer.start()
        assert close_returned.wait(2.5), 'finite close reentered blocked journal IO'
        assert observer._mode == "cancel_after_drain" and not observer.closed
        assert observer.thread is not None and observer.thread.is_alive()
        assert not errors
    finally:
        handoff.set()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        if producer.ident is not None:
            producer.join(3)
        if closer.ident is not None:
            closer.join(3)
        if observer.thread is not None:
            observer.thread.join(3)
        observer.close(drain=False)
        assert observer.closed and observer.thread is not None and not observer.thread.is_alive()
        physical = observer._call.thread if observer._call is not None else None
        if physical is not None:
            physical.join(3)
            assert not physical.is_alive()
        assert not producer.is_alive() and not closer.is_alive() and not errors


def test_fatal_close_does_not_retry_unwritable_physical_result_forever(tmp_path, monkeypatch):
    kernel = _kernel(tmp_path)
    returned, failed = Event(), Event()
    original = outbox.transaction
    pauses = []
    original_pause = NativeNotifications._pause

    @contextmanager
    def unavailable(*args, **kwargs):
        if returned.is_set():
            raise OSError('original journal unavailable after ACK')
        with original(*args, **kwargs) as document:
            yield document

    def transport(*_args, **_kwargs):
        returned.set()
        return {'outcome': 'success', 'reason': None}

    def pause(self, error):
        pauses.append(str(error))
        original_pause(self, error)
        failed.set()

    monkeypatch.setattr(outbox, 'transaction', unavailable)
    monkeypatch.setattr(NativeNotifications, '_pause', pause)
    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert failed.wait(3)
        observer.close(drain=False)
        assert observer.closed and observer.thread is not None and not observer.thread.is_alive()
        assert len(pauses) == 2  # Original failure and one final result-settlement opportunity.
        pending, = outbox.read_notifications(tmp_path, 'run-native')['pending']
        assert pending['outcome'] == 'sending' and pending['attempts'] == 1
        assert 'finished_at' not in pending['attempt_history'][0]
    finally:
        monkeypatch.setattr(outbox, 'transaction', original)
        observer.close(drain=False)
        if observer.thread is not None:
            observer.thread.join(3)
            assert not observer.thread.is_alive()
        if observer._call is not None and observer._call.thread is not None:
            observer._call.thread.join(3)
            assert not observer._call.thread.is_alive()


@pytest.mark.parametrize('closing', [False, True])
def test_ack_after_projection_fault_is_persisted_without_a_recovery_wake(tmp_path, monkeypatch, closing):
    kernel = _kernel(tmp_path)
    started, finished = Event(), Event()
    calls = []
    original_finish = NativeNotifications._finish_result

    def transport(_config, _card, _identity, cancel, **_kwargs):
        calls.append('original call')
        started.set()
        assert cancel.wait(3)
        return {'outcome': 'success', 'reason': 'original ACK after cancellation'}

    def unavailable(*_args, **_kwargs):
        raise OSError('original projection unavailable')

    def finish(self, event_id, result, returned_at):
        original_finish(self, event_id, result, returned_at)
        finished.set()

    monkeypatch.setattr(NativeNotifications, '_finish_result', finish)
    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    closer = Thread(target=lambda: observer.close(timeout_seconds=3))
    try:
        assert started.wait(3)
        original_cancel = observer._call.cancel
        monkeypatch.setattr('agent_run.notifications.sender.read_boundaries', unavailable)
        if closing:
            closer.start()
        else:
            observer.observe_committed()
        assert finished.wait(3), 'returned ACK was gated on a later recovery request'
        if closing:
            closer.join(3)
            assert not closer.is_alive() and observer.closed
        document = outbox.read_notifications(tmp_path, 'run-native')
        record, = document['records']
        assert document['pending'] == []
        assert record['outcome'] == 'success' and record['attempts'] == 1
        assert record['attempt_history'][0]['finished_at'] >= record['attempt_history'][0]['started_at']
        assert document['unavailable_reason'] == observer.failure_reason == 'original projection unavailable'
        assert original_cancel.is_set() and calls == ['original call']
    finally:
        observer.close(drain=False)
        if closer.ident is not None:
            closer.join(3)
            assert not closer.is_alive()
        assert observer.closed
        assert observer._call is None or observer._call.thread is None or not observer._call.thread.is_alive()


@pytest.mark.parametrize('important', [False, True])
def test_quiet_wakes_preserve_slow_status_and_actual_important_gate_preempts_it(tmp_path, monkeypatch, important):
    """Controlled Status IO proves scheduling; native intent tests remain separate."""
    from time import monotonic
    from types import SimpleNamespace
    from agent_run.notifications.status_comment import StatusComments
    from support.notification import observe_and_wait

    kernel = _kernel(tmp_path)
    status_started, status_release, status_cancelled, status_finished = (Event() for _ in range(4))
    hold_projection, page_entered, page_release, important_sent = (Event() for _ in range(4))
    ordering, failures = [], []
    original_page = NativeNotifications._page

    def page(self, after):
        result = original_page(self, after)
        if hold_projection.is_set() and not page_entered.is_set():
            page_entered.set()
            assert page_release.wait(3)
        return result

    def status(self, _subject, *, api, cancelled):
        try:
            status_started.set()
            deadline = monotonic() + 3
            while not status_release.is_set():
                if cancelled():
                    status_cancelled.set()
                    return 'unknown'
                assert monotonic() < deadline, 'slow controlled Status was not released'
                status_release.wait(0.005)
            if not status_finished.is_set():
                ordering.append('status')
                status_finished.set()
            return 'confirmed'
        except BaseException as error:
            failures.append(error)
            raise

    def transport(*_args, **_kwargs):
        active, = [item for item in outbox.read_notifications(tmp_path, 'run-native')['pending']
                   if item['outcome'] == 'sending']
        if active['event']['kind'] == 'boundary':
            ordering.append('important')
            important_sent.set()
        return {'outcome': 'success', 'reason': None}

    monkeypatch.setattr(NativeNotifications, '_page', page)
    monkeypatch.setattr(StatusComments, 'deliver', status)
    monkeypatch.setattr('agent_run.notifications.sender.status_subjects', lambda *_args, **_kwargs: ['native-ticket'])
    monkeypatch.setattr('agent_run.notifications.sender.project_status',
        lambda *_args, **_kwargs: SimpleNamespace(target=SimpleNamespace(key='original-status'), body='unchanged copy'))
    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert status_started.wait(3)
        hold_projection.set()
        observer.observe_committed()
        assert page_entered.wait(3)
        for _ in range(3):
            observer.observe_committed()
        assert not status_cancelled.wait(0.15), 'unchanged Host wakes preempted slow Status'
        assert not observer._call.cancel.is_set()
        page_release.set()
        observe_and_wait(observer)
        if important:
            assert kernel.control('stop', 'real-important-gate') == 'applied'
            observe_and_wait(observer)
            assert status_cancelled.wait(3)
            assert important_sent.wait(3)
            assert ordering == ['important']
        status_release.set()
        assert status_finished.wait(3)
        observer.close(timeout_seconds=3)
        assert observer.closed and not failures
        assert ordering == (['important', 'status'] if important else ['status'])
    finally:
        page_release.set()
        status_release.set()
        observer.close(drain=False)
        assert observer.closed
        if observer.thread is not None:
            observer.thread.join(3)
            assert not observer.thread.is_alive()
        if observer._call is not None and observer._call.thread is not None:
            observer._call.thread.join(3)
            assert not observer._call.thread.is_alive()
        assert not failures


def test_status_handoff_does_not_consume_the_only_feishu_recovery_wake(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import agent_run.notifications.sender as notifications
    from agent_run.notifications.status_comment import StatusComments

    kernel = _kernel(tmp_path)
    delivered = Event()
    status_calls = []
    original = notifications.read_boundaries

    def unavailable(*_args, **_kwargs):
        raise OSError('original projection unavailable before Status')

    def status(*_args, **_kwargs):
        status_calls.append('confirmed')
        return 'confirmed'

    def transport(*_args, **_kwargs):
        delivered.set()
        return {'outcome': 'success', 'reason': None}

    monkeypatch.setattr(notifications, 'read_boundaries', unavailable)
    monkeypatch.setattr(notifications, 'status_subjects', lambda *_args, **_kwargs: ['native-ticket'])
    monkeypatch.setattr(notifications, 'project_status',
        lambda *_args, **_kwargs: SimpleNamespace(target=SimpleNamespace(key='original-status'), body='unchanged copy'))
    monkeypatch.setattr(StatusComments, 'deliver', status)
    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        monkeypatch.setattr(notifications, 'read_boundaries', original)
        observer.observe_committed()
        assert delivered.wait(3), 'Status consumed the only healthy Feishu recovery request'
        assert observer.failure_reason is None and status_calls
        observer.close(timeout_seconds=3)
        document = outbox.read_notifications(tmp_path, 'run-native')
        assert 'unavailable_reason' not in document
        record, = document['records']
        assert record['outcome'] == 'success' and record['attempts'] == 1
    finally:
        observer.close(drain=False)
        assert observer.closed


def test_continuous_quiet_wakes_cannot_starve_important_send_or_status(tmp_path, monkeypatch):
    """Every projection gets another wake until both physical calls progress."""
    from types import SimpleNamespace
    from support.observer_errors import ObserverErrors
    from agent_run.notifications.status_comment import StatusComments

    kernel = _kernel(tmp_path)
    assert kernel.control('stop', 'important-before-quiet-wakes') == 'applied'
    important_sent, status_finished, exercised = Event(), Event(), Event()
    storm = Event()
    storm.set()
    observed, ordering = [], []
    original_wake = NativeNotifications._wake_pending
    callback_errors = ObserverErrors()

    def wake(self, document, *args):
        with callback_errors.capture():
            if storm.is_set():
                # Deterministically model a Host tick during every projection.
                # It commits no new business fact and remains pending for catchup.
                self.observe_committed()
                boundaries = [item for item in document['pending'] + document['records']
                              if (item.get('event', item)).get('kind') == 'boundary']
                if boundaries:
                    observed.append(boundaries[0].copy())
                if len(observed) == 4:
                    assert important_sent.wait(1), 'quiet wakes starved the important physical send'
                if len(observed) == 8:
                    assert status_finished.wait(1), 'Status did not resume while quiet wakes continued'
                    exercised.set()
            original_wake(self, document, *args)

    def transport(*_args, **_kwargs):
        pending = outbox.read_notifications(tmp_path, 'run-native')['pending']
        if any(item['event']['kind'] == 'boundary' and item['outcome'] == 'sending' for item in pending):
            ordering.append('important')
            important_sent.set()
        return {'outcome': 'success', 'reason': None}

    def status(*_args, **_kwargs):
        ordering.append('status')
        status_finished.set()
        return 'confirmed'

    monkeypatch.setattr(NativeNotifications, '_wake_pending', wake)
    monkeypatch.setattr(StatusComments, 'deliver', status)
    monkeypatch.setattr('agent_run.notifications.sender.status_subjects', lambda *_args, **_kwargs: ['native-ticket'])
    monkeypatch.setattr('agent_run.notifications.sender.project_status',
        lambda *_args, **_kwargs: SimpleNamespace(target=SimpleNamespace(key='original-status'), body='unchanged copy'))
    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert exercised.wait(3), observer.failure_reason
        assert storm.is_set() and len(observed) >= 8
        assert ordering[:2] == ['important', 'status']
        storm.clear()
        observer.close(timeout_seconds=3)
        assert observer.closed and observer.failure_reason is None
        record, = [item for item in outbox.read_notifications(tmp_path, 'run-native')['records']
                   if item['kind'] == 'boundary']
        assert record['attempts'] == 1 and record['outcome'] == 'success'
        assert all(item['created_at'] == record['created_at'] and item['deadline_at'] == record['deadline_at']
                   for item in observed)
        with kernel.storage.transaction(write=False) as tx:
            highwater = tx.one("SELECT max(position) FROM history")[0]
        assert outbox.read_notifications(tmp_path, 'run-native')['native_cursor'] == highwater
    finally:
        storm.clear()
        observer.close(drain=False)
        assert observer.closed
        callback_errors.raise_if_any()
        assert observer.thread is not None and not observer.thread.is_alive()


def test_continuous_quiet_wakes_share_ordinary_feishu_and_status_opportunities(tmp_path, monkeypatch):
    """A new Host wake during every projection must leave both channels useful."""
    from types import SimpleNamespace
    from support.observer_errors import ObserverErrors
    from agent_run.notifications.status_comment import StatusComments

    kernel = _kernel(tmp_path)
    ordinary_sent, status_resumed, exercised = Event(), Event(), Event()
    storm = Event()
    storm.set()
    ticks, ordering = [], []
    original_wake = NativeNotifications._wake_pending
    callback_errors = ObserverErrors()

    def wake(self, document, *args):
        with callback_errors.capture():
            if storm.is_set():
                ticks.append(True)
                self.observe_committed()
                if len(ticks) == 4:
                    assert ordinary_sent.wait(1), 'Status and continuous quiet wakes starved ordinary Feishu'
                if len(ticks) == 8:
                    assert status_resumed.wait(1), 'Status did not regain a turn after ordinary Feishu'
                    exercised.set()
            original_wake(self, document, *args)

    def transport(*_args, **_kwargs):
        ordering.append('ordinary')
        ordinary_sent.set()
        return {'outcome': 'success', 'reason': None}

    def status(*_args, **_kwargs):
        ordering.append('status')
        if ordinary_sent.is_set():
            status_resumed.set()
        return 'unchanged'

    monkeypatch.setattr(NativeNotifications, '_wake_pending', wake)
    monkeypatch.setattr(StatusComments, 'deliver', status)
    monkeypatch.setattr('agent_run.notifications.sender.status_subjects', lambda *_a, **_k: ['native-ticket'])
    # Real changing display copy keeps Status eligible while the ordinary event waits.
    monkeypatch.setattr('agent_run.notifications.sender.project_status',
        lambda *_a, **_k: SimpleNamespace(target=SimpleNamespace(key='original-status'), body=f'copy {len(ticks)}'))
    observer = NativeNotifications(kernel.storage.path, tmp_path, transport=transport, storage=kernel.storage)
    try:
        assert exercised.wait(3), observer.failure_reason
        assert ordering[:3] == ['status', 'ordinary', 'status']
        storm.clear()
        observer.close(timeout_seconds=3)
        record, = outbox.read_notifications(tmp_path, 'run-native')['records']
        assert record['kind'] == 'run_start' and record['outcome'] == 'success' and record['attempts'] == 1
    finally:
        storm.clear()
        observer.close(drain=False)
        assert observer.closed
        callback_errors.raise_if_any()


def test_status_only_start_failure_recovers_its_durable_diagnostic(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent_run.notifications.status_comment import StatusComments

    kernel = _kernel(tmp_path, notifications=False)
    failed, delivered = Event(), Event()
    original_start = Thread.start

    def start(thread):
        if thread.name == 'kernel-notification-call' and not failed.is_set():
            failed.set()
            raise RuntimeError('original physical thread failed to start')
        original_start(thread)

    monkeypatch.setattr(Thread, 'start', start)
    monkeypatch.setattr('agent_run.notifications.sender.status_subjects', lambda *_a, **_k: ['native-ticket'])
    monkeypatch.setattr('agent_run.notifications.sender.project_status',
        lambda *_a, **_k: SimpleNamespace(target=SimpleNamespace(key='original-status'), body='current copy'))
    monkeypatch.setattr(StatusComments, 'deliver',
        lambda *_a, **_k: delivered.set() or 'unchanged')
    observer = NativeNotifications(kernel.storage.path, tmp_path, storage=kernel.storage)
    try:
        assert failed.wait(3)
        with observer.condition:
            assert observer.condition.wait_for(lambda: observer.failure_reason is not None, timeout=3)
        assert not delivered.wait(0.1), 'failed Status preparation retried without a new observation'
        assert outbox.read_notifications(tmp_path, 'run-native')['unavailable_reason'] == observer.failure_reason
        observer.observe_committed()
        assert delivered.wait(3)
        observer.close(timeout_seconds=3)
        assert observer.failure_reason is None
        assert 'unavailable_reason' not in outbox.read_notifications(tmp_path, 'run-native')
    finally:
        observer.close(drain=False)
        assert observer.closed
