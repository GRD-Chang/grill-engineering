"""Submission order at the real SQLite boundary, independent of work scheduling."""
import inspect
from contextlib import closing, contextmanager
import sqlite3
import os
import sys
import threading
import time

import pytest

from agent_run.storage.sqlite import Storage
from agent_run.storage import save_order
from agent_run.errors import KernelError
from agent_run.storage.preflight import UnsupportedState
from support.core_operations import agent_preparation_fixture


@pytest.fixture(autouse=True)
def empty_save_registry():
    assert not save_order._gates
    yield
    assert not save_order._gates


@contextmanager
def paused_head(storage, subject):
    code = Storage._transaction.__wrapped__.__code__
    lines, start = inspect.getsourcelines(Storage._transaction.__wrapped__)
    line = start + next(i for i, text in enumerate(lines) if 'connection.execute("BEGIN IMMEDIATE")' in text)
    ready, release = threading.Event(), threading.Event()
    errors = []
    def run():
        def trace(frame, event, arg):
            if frame.f_code is code and event == 'line' and frame.f_lineno == line:
                ready.set()
                assert release.wait(2)
            return trace
        sys.settrace(trace)
        try:
            with storage.transaction() as tx:
                tx.put(subject, 'head', b'original head')
        except BaseException as error:
            errors.append(error)
        finally:
            sys.settrace(None)
    thread = threading.Thread(target=run)
    try:
        thread.start()
        assert ready.wait(2)
        yield release
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive() and not errors, errors


def wait_for_queue(path, count):
    identity = save_order.database_identity(path)
    deadline = time.monotonic() + 0.5
    while True:
        with save_order._condition:
            gate = save_order._gates.get(identity)
            if gate is not None and len(gate.queue) == count:
                return
        assert time.monotonic() < deadline, 'save did not reach its actual queue slot'
        threading.Event().wait(0.002)


@pytest.mark.parametrize('client', ['same', 'separate', 'directory-alias'])
def test_earlier_save_cannot_be_overtaken_after_preflight(tmp_path, client):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    subject = step.preparation.subject
    other = kernel.storage
    if client == 'separate':
        other = Storage(kernel.storage.path)
    elif client == 'directory-alias':
        alias = tmp_path / 'state-alias'
        alias.symlink_to(kernel.storage.path.parent, target_is_directory=True)
        other = Storage(alias / kernel.storage.path.name)
    code = Storage._transaction.__wrapped__.__code__
    lines, start = inspect.getsourcelines(Storage._transaction.__wrapped__)
    before_begin = start + next(i for i, line in enumerate(lines) if 'connection.execute("BEGIN IMMEDIATE")' in line)
    first_ready, release, second_entered, second_saved = (threading.Event() for _ in range(4))
    failures = []
    def save(storage, label):
        def trace(frame, event, arg):
            if frame.f_code is Storage.transaction.__wrapped__.__code__ and label == 'B' and event == 'call':
                second_entered.set()
            if frame.f_code is code and label == 'A' and event == 'line' and frame.f_lineno == before_begin:
                first_ready.set()
                assert release.wait(2), 'first save barrier was not released'
            return trace
        sys.settrace(trace)
        try:
            with storage.transaction() as tx:
                tx.put(subject, 'fifo-test', label.encode())
            if label == 'B':
                second_saved.set()
        except BaseException as error:
            failures.append(error)
        finally:
            sys.settrace(None)
    first = threading.Thread(target=save, args=(kernel.storage, 'A'))
    second = threading.Thread(target=save, args=(other, 'B'))
    try:
        first.start()
        assert first_ready.wait(2)
        second.start()
        assert second_entered.wait(2)
        assert not second_saved.wait(0.15), 'late save overtook an already-entered save'
    finally:
        release.set()
        for thread in (first, second):
            if thread.ident is not None:
                thread.join(3)
    assert not first.is_alive() and not second.is_alive() and not failures
    with Storage(kernel.storage.path).transaction(write=False) as tx:
        assert [row['body'] for row in tx.rows(
            "SELECT body FROM material WHERE subject=? AND purpose='fifo-test' ORDER BY rowid", (subject,))] == [b'A', b'B']


def test_queued_cancel_removes_only_its_ticket_and_next_save_progresses(tmp_path):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    subject = step.preparation.subject
    cancel = threading.Event()
    errors, ran = [], []
    def waiting():
        try:
            with Storage(kernel.storage.path).transaction(cancelled=cancel):
                ran.append(True)
        except BaseException as error:
            errors.append(error)
    waiter = threading.Thread(target=waiting)
    try:
        with paused_head(kernel.storage, subject):
            waiter.start()
            wait_for_queue(kernel.storage.path, 2)
            cancel.set()
            waiter.join(0.5)
            assert not waiter.is_alive() and not ran
            assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
            wait_for_queue(kernel.storage.path, 1)
    finally:
        cancel.set()
        if waiter.ident is not None:
            waiter.join(2)
    with kernel.storage.transaction() as tx:
        tx.put(subject, 'after-cancel', b'saved')


def test_full_queue_keeps_stop_and_read_only_access_available(tmp_path, monkeypatch):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    subject = step.preparation.subject
    monkeypatch.setattr(save_order, 'MAX_PENDING_WRITES', 2)
    errors = []
    def waiting():
        try:
            with kernel.storage.transaction() as tx:
                tx.put(subject, 'second', b'second result')
        except BaseException as error:
            errors.append(error)
    waiter = threading.Thread(target=waiting)
    try:
        with paused_head(kernel.storage, subject):
            waiter.start()
            wait_for_queue(kernel.storage.path, 2)
            with pytest.raises(KernelError, match='storage_busy'):
                with Storage(kernel.storage.path).transaction():
                    pytest.fail('full queue executed a transaction')
            with kernel.storage.transaction(write=False) as tx:
                assert tx.one('SELECT 1')[0] == 1
            assert kernel.control('stop', 'stop-through-full-save-queue') == 'applied'
            assert kernel.status(subject)['stopped']
    finally:
        if waiter.ident is not None:
            waiter.join(2)
    assert not waiter.is_alive() and not errors
    assert kernel.status(subject)['budgets']['requirements']['developments_used'] == 0


@pytest.mark.parametrize('ordered', [True, False])
def test_nested_write_fails_without_poisoning_outer_transaction(tmp_path, ordered):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    subject = step.preparation.subject
    other = Storage(kernel.storage.path)
    with kernel.storage.transaction() as tx:
        tx.put(subject, 'outer-before', b'before')
        with pytest.raises(KernelError, match='storage_busy'):
            with other.transaction(ordered=ordered):
                pytest.fail('nested write entered SQLite')
        tx.put(subject, 'outer-after', b'after')
    with kernel.storage.transaction(write=False) as tx:
        assert len(tx.rows("SELECT id FROM material WHERE subject=? AND purpose IN ('outer-before','outer-after')", (subject,))) == 2


@pytest.mark.parametrize('error', [ValueError, KeyboardInterrupt])
def test_transaction_exception_releases_turn_and_rolls_back_all_writes(tmp_path, error):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    subject = step.preparation.subject
    exact = error('controlled body failure')
    with pytest.raises(error) as caught:
        with kernel.storage.transaction() as tx:
            tx.put(subject, 'rolled-back', b'not committed')
            raise exact
    assert caught.value is exact
    with kernel.storage.transaction() as tx:
        assert not tx.rows("SELECT id FROM material WHERE purpose='rolled-back'")
        tx.put(subject, 'after-exception', b'committed')


def test_queue_wait_and_sqlite_busy_share_one_second(tmp_path, monkeypatch):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    clients = [Storage(kernel.storage.path), Storage(kernel.storage.path)]
    entered = [threading.Event(), threading.Event()]
    results, ran = {}, []
    original = sqlite3.connect
    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        name = threading.current_thread().name
        if name in {'budget-A', 'budget-B'}:
            index = int(name == 'budget-B')
            db.set_trace_callback(lambda sql: entered[index].set() if sql == 'BEGIN IMMEDIATE' else None)
        return db
    monkeypatch.setattr(sqlite3, 'connect', connect)
    def save(index):
        start = time.monotonic()
        try:
            with clients[index].transaction():
                ran.append(index)
        except BaseException as error:
            results[index] = (time.monotonic() - start, error)
    threads = [threading.Thread(target=save, args=(i,), name='budget-' + 'AB'[i]) for i in range(2)]
    external = original(kernel.storage.path, isolation_level=None)
    try:
        external.execute('BEGIN IMMEDIATE')
        threads[0].start()
        assert entered[0].wait(0.5)
        threads[1].start()
        wait_for_queue(kernel.storage.path, 2)
        for thread in threads:
            thread.join(1.5)
        assert all(not thread.is_alive() for thread in threads) and not ran
        assert set(results) == {0, 1}
        for elapsed, error in results.values():
            assert isinstance(error, KernelError) and error.code == 'storage_busy'
            assert 0.8 <= elapsed < 1.3, elapsed
    finally:
        external.rollback()
        external.close()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)


@pytest.mark.parametrize('stage', ['preflight', 'connect'])
def test_failed_setup_releases_slot_before_next_attempt(tmp_path, monkeypatch, stage):
    import agent_run.storage.sqlite as storage_module
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    exact = OSError('controlled isolated setup failure')
    with monkeypatch.context() as patch:
        if stage == 'preflight':
            def fail(*args, **kwargs):
                raise exact
            patch.setattr(storage_module, 'require_supported_state', fail)
        else:
            original = sqlite3.connect
            def connect(name, *args, **kwargs):
                if '?mode=rw' in str(name):
                    raise exact
                return original(name, *args, **kwargs)
            patch.setattr(sqlite3, 'connect', connect)
        with pytest.raises(OSError) as caught:
            with kernel.storage.transaction():
                pytest.fail('failed setup executed body')
        assert caught.value is exact
    with kernel.storage.transaction() as tx:
        tx.put(step.preparation.subject, 'after-setup-failure', b'once')


def test_caught_sql_error_still_poisons_and_releases_transaction(tmp_path):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    with pytest.raises(KernelError, match='transaction_aborted'):
        with kernel.storage.transaction() as tx:
            tx.put(step.preparation.subject, 'poisoned-write', b'rollback')
            with pytest.raises(sqlite3.Error):
                tx.execute('SELECT missing_column FROM missing_table')
    with kernel.storage.transaction() as tx:
        assert not tx.rows("SELECT id FROM material WHERE purpose='poisoned-write'")


@pytest.mark.parametrize('alias', ['symlink', 'hardlink'])
def test_unsafe_database_alias_refuses_without_admission_leak(tmp_path, alias):
    kernel, _, _, _, _ = agent_preparation_fixture(tmp_path)
    path = tmp_path / 'unsafe-alias.sqlite3'
    if alias == 'symlink':
        path.symlink_to(kernel.storage.path)
    else:
        os.link(kernel.storage.path, path)
    try:
        with pytest.raises(UnsupportedState, match='uncertain_state'):
            with save_order.save_turn(path, ordered=True, deadline=time.monotonic() + 1, cancelled=None):
                pytest.fail('unsafe alias obtained save admission')
    finally:
        path.unlink()
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT 1')[0] == 1


def test_an_independent_database_is_not_held_by_another_save_queue(tmp_path):
    (tmp_path / 'first').mkdir()
    (tmp_path / 'second').mkdir()
    first, _, _, first_step, _ = agent_preparation_fixture(tmp_path / 'first')
    second, _, _, second_step, _ = agent_preparation_fixture(tmp_path / 'second')
    with paused_head(first.storage, first_step.preparation.subject):
        with second.storage.transaction() as tx:
            tx.put(second_step.preparation.subject, 'independent', b'committed')


def test_pre_cancelled_head_executes_no_body_and_leaves_no_ticket(tmp_path):
    kernel, _, _, _, _ = agent_preparation_fixture(tmp_path)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError, match='cancelled'):
        with kernel.storage.transaction(cancelled=cancelled):
            pytest.fail('cancelled head executed body')
    with kernel.storage.transaction() as tx:
        assert tx.one('SELECT 1')[0] == 1


def test_same_database_replacement_while_waiting_is_not_adopted(tmp_path, monkeypatch):
    import agent_run.storage.sqlite as storage_module
    kernel, _, _, _, _ = agent_preparation_fixture(tmp_path)
    replacement = Storage.create(tmp_path / 'replacement.sqlite3')
    original = storage_module.require_supported_state
    before = replacement.path.read_bytes()
    def replace_before_preflight(path, **kwargs):
        os.replace(replacement.path, path)
        return original(path, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(storage_module, 'require_supported_state', replace_before_preflight)
        with pytest.raises(UnsupportedState, match='uncertain_state'):
            with kernel.storage.transaction():
                pytest.fail('old save ticket adopted a replacement database')
    assert kernel.storage.path.read_bytes() == before


def test_control_revokes_a_waiting_admission_without_creating_an_invocation(tmp_path):
    kernel, runner, _, step, _ = agent_preparation_fixture(tmp_path)
    request = step.preparation
    code = Storage._transaction.__wrapped__.__code__
    lines, start = inspect.getsourcelines(Storage._transaction.__wrapped__)
    line = start + next(i for i, text in enumerate(lines) if 'connection.execute("BEGIN IMMEDIATE")' in text)
    ready, release = threading.Event(), threading.Event()
    failures = []
    def admit():
        def trace(frame, event, arg):
            if frame.f_code is code and event == 'line' and frame.f_lineno == line:
                ready.set()
                assert release.wait(2)
            return trace
        sys.settrace(trace)
        try:
            kernel.admit_agent(request)
        except BaseException as error:
            failures.append(error)
        finally:
            sys.settrace(None)
    thread = threading.Thread(target=admit)
    try:
        thread.start()
        assert ready.wait(2)
        assert kernel.control('stop', 'stop-before-queued-admission') == 'applied'
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], KernelError) and failures[0].code == 'executor_fenced'
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one('SELECT count(*) FROM invocations')[0] == 0
        assert tx.one('SELECT count(*) FROM leases')[0] == 0
    assert kernel.status(request.subject)['budgets']['requirements']['developments_used'] == 0
    kernel.preparation_failed(request, 'controlled_cancel_settlement', {'owned_resources_closed': True})
    assert kernel.worker_settled(request) and runner.state().preparing == ()


def test_shared_revocation_record_does_not_wait_on_business_save_queue(tmp_path):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    request = step.preparation
    with paused_head(kernel.storage, request.subject):
        kernel._retire_failed_executor(request.execution, RuntimeError('controlled record-only revocation'))
        assert kernel.status(request.subject)['gates'] == ['worker_unavailable']
    # This is only SQL admission revocation; no Host/Worker exit was invented.
    assert kernel.worker_settled(request) is False


def test_completed_requests_do_not_keep_registry_or_storage_instances(tmp_path):
    kernel, _, _, _, _ = agent_preparation_fixture(tmp_path)
    for _ in range(10):
        with Storage(kernel.storage.path).transaction() as tx:
            assert tx.one('SELECT 1')[0] == 1
        assert not save_order._gates


def test_cancel_while_sqlite_waits_never_enters_business_body(tmp_path, monkeypatch):
    kernel, _, _, _, _ = agent_preparation_fixture(tmp_path)
    cancelled, beginning = threading.Event(), threading.Event()
    original = sqlite3.connect
    def connect(name, *args, **kwargs):
        db = original(name, *args, **kwargs)
        if '?mode=rw' in str(name):
            db.set_trace_callback(lambda sql: beginning.set() if sql == 'BEGIN IMMEDIATE' else None)
        return db
    monkeypatch.setattr(sqlite3, 'connect', connect)
    ran, failures = [], []
    def save():
        try:
            with kernel.storage.transaction(cancelled=cancelled):
                ran.append(True)
        except BaseException as error:
            failures.append(error)
    waiter = threading.Thread(target=save)
    external = original(kernel.storage.path, isolation_level=None)
    try:
        external.execute('BEGIN IMMEDIATE')
        waiter.start()
        assert beginning.wait(0.5)
        cancelled.set()
        external.rollback()
        waiter.join(1.5)
        assert not waiter.is_alive() and not ran
        assert len(failures) == 1 and isinstance(failures[0], InterruptedError)
    finally:
        external.close()
        if waiter.ident is not None:
            waiter.join(2)


@pytest.mark.parametrize('ordered', [True, False], ids=['business-save', 'control-write'])
def test_writer_lock_acquired_after_original_deadline_cannot_enter_body(tmp_path, ordered):
    kernel, _, _, step, _ = agent_preparation_fixture(tmp_path)
    code = Storage._transaction.__wrapped__.__code__
    lines, start = inspect.getsourcelines(Storage._transaction.__wrapped__)
    before_begin = start + next(i for i, line in enumerate(lines) if 'connection.execute("BEGIN IMMEDIATE")' in line)
    prepared, release, executing = (threading.Event() for _ in range(3))
    deadlines, acquired, failures, ran = [], [], [], []

    def save():
        def trace(frame, event, arg):
            if frame.f_code is code and event == 'line':
                if frame.f_lineno == before_begin:
                    # Preserve the real timeout already installed in SQLite,
                    # then model scheduling delay before its actual BEGIN.
                    deadlines.append(frame.f_locals['deadline'])
                    prepared.set()
                    assert release.wait(2), 'writer-boundary barrier was not released'
                    executing.set()
                elif frame.f_lineno == before_begin + 1 and not acquired:
                    acquired.append(time.monotonic())
            return trace
        sys.settrace(trace)
        try:
            with kernel.storage.transaction(ordered=ordered) as tx:
                ran.append(True)
                tx.put(step.preparation.subject, 'expired-save', b'must not commit')
        except BaseException as error:
            failures.append(error)
        finally:
            sys.settrace(None)

    waiter = threading.Thread(target=save)
    with closing(sqlite3.connect(kernel.storage.path, isolation_level=None)) as external:
        try:
            external.execute('BEGIN IMMEDIATE')
            waiter.start()
            assert prepared.wait(2)
            # The original real busy timeout is now longer than the remaining
            # total budget. Neither the clock nor SQLite's result is replaced.
            threading.Event().wait(0.4)
            release.set()
            assert executing.wait(2)
            threading.Event().wait(max(0, deadlines[0] + 0.05 - time.monotonic()))
            assert not acquired, 'writer lock was acquired while the other writer held it'
            external.rollback()
            waiter.join(2)
        finally:
            external.rollback()
            release.set()
            if waiter.ident is not None:
                waiter.join(3)
    assert not waiter.is_alive()
    assert len(acquired) == 1 and acquired[0] >= deadlines[0]
    assert not ran and len(failures) == 1
    assert isinstance(failures[0], KernelError) and failures[0].code == 'storage_busy'
    assert not save_order._gates
    with Storage(kernel.storage.path).transaction() as tx:
        assert tx.one("SELECT count(*) FROM material WHERE purpose='expired-save'")[0] == 0
        tx.put(step.preparation.subject, 'after-expired-save', b'next save remains usable')
