"""Test-only entry coverage and outer reader accounting, not native acceptance."""
from contextlib import contextmanager
import os
import threading
from types import SimpleNamespace

import pytest

from agent_run.core.kernel import Kernel
from agent_run.core.operation_runner import OperationRunner
from agent_run.notifications.sender import NativeNotifications
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Storage, Transaction
from support import host_cost_trace as costs


@pytest.fixture
def trace(tmp_path):
    root = tmp_path / 'host-costs'
    root.mkdir(mode=0o700)
    observed = costs.HostCosts(root)
    try:
        yield observed
    finally:
        os.close(observed.directory)


@contextmanager
def context(tx, events):
    events.append('enter')
    try:
        yield tx
    finally:
        events.append('exit')


def view(owner):
    result = object.__new__(RunTransaction)
    result._transaction = owner
    return result


def test_nested_shared_and_independent_owners_count_once_with_frozen_category(trace, monkeypatch):
    now, cpu, events = [10], [20], []
    monkeypatch.setattr(costs, 'time', SimpleNamespace(monotonic_ns=lambda: now[0]))
    monkeypatch.setattr(costs, 'thread_time_ns', lambda: cpu[0])
    shared, independent = Transaction(None), Transaction(None)
    with trace.entry('operation/accept'):
        with trace.reader(context(view(shared), events)):
            shared.rows_read, shared.bytes_read = 3, 40
            with trace.entry('guard/preparation'):
                with trace.reader(context(view(view(shared)), events)):
                    shared.rows_read, shared.bytes_read = 7, 90
                with trace.reader(context(independent, events)):
                    independent.rows_read, independent.bytes_read = 2, 10
            now[0], cpu[0] = 110, 80
    row, = trace.reader_totals.values()
    assert trace.reader_totals.keys() == {'operation/accept'}
    assert row == {'calls': 1, 'failed': 0, 'wall_ns': 100, 'thread_ns': 60, 'max_depth': 2,
                   'owners': 2, 'rows': 9, 'bytes': 100, 'missing': {}, 'trace_entries': 0, 'profile_entries': 0}
    assert events == ['enter', 'enter', 'exit', 'enter', 'exit', 'exit']
    assert trace.local.reader is None and trace.local.category == 'other'


def test_parallel_threads_have_separate_outer_readers_and_no_inherited_tag(trace):
    entered, release = threading.Barrier(3, timeout=2), threading.Event()
    errors = []
    def worker(tag):
        try:
            scope = trace.entry(tag) if tag else context(None, [])
            with scope:
                with trace.reader(context(Transaction(None), [])):
                    entered.wait()
                    assert release.wait(2)
        except BaseException as error:
            errors.append(error)
    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ('state/supervise', None)]
    with trace.entry('guard/preparation'):
        try:
            for thread in threads:
                thread.start()
            entered.wait()
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=3)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert trace.reader_totals.keys() == {'state/supervise', 'other'}
    assert all(row['calls'] == row['owners'] == row['max_depth'] == 1 for row in trace.reader_totals.values())


@pytest.mark.parametrize('stage', ['enter', 'body', 'exit'])
@pytest.mark.parametrize('original_type', [RuntimeError, TimeoutError, InterruptedError, KeyboardInterrupt])
@pytest.mark.parametrize('measurement_type', [TimeoutError, InterruptedError])
def test_context_failure_wins_over_exit_timer_failure_and_restores_tls(
    trace, monkeypatch, stage, original_type, measurement_type,
):
    original, measurement, events, ticks = original_type('original'), measurement_type('timer'), [], []
    def timer():
        ticks.append(1)
        if len(ticks) == 2:
            raise measurement
        return 1
    monkeypatch.setattr(costs, 'thread_time_ns', timer)
    @contextmanager
    def failing():
        events.append('enter')
        if stage == 'enter':
            raise original
        try:
            yield Transaction(None)
        finally:
            events.append('exit')
            if stage == 'exit':
                raise original
    with pytest.raises(original_type) as caught:
        with trace.entry('guard/preparation'):
            with trace.reader(failing()):
                if stage == 'body':
                    raise original
    assert caught.value is original
    assert events == (['enter'] if stage == 'enter' else ['enter', 'exit'])
    assert trace.local.reader is None and trace.local.category == 'other'
    assert not trace.reader_totals  # Timer unavailable, never invent a sample.


@pytest.mark.parametrize('fault', [OSError, TimeoutError, InterruptedError])
def test_successful_exit_then_metric_failure_never_reenters_or_queries(trace, monkeypatch, fault):
    events, error = [], fault('metric unavailable')
    tx = Transaction(None)
    def unavailable(owner):
        assert events == ['enter', 'body', 'exit']
        raise error
    monkeypatch.setattr(trace, '_reader_owner', unavailable)
    with pytest.raises(fault) as caught:
        with trace.reader(context(tx, events)):
            events.append('body')
    assert caught.value is error and trace.local.reader is None
    row = trace.reader_totals['other']
    assert row['failed'] == 1 and row['owners'] == 0 and row['missing'] == {'observation_failed': 1}
    assert tx.rows_read == tx.bytes_read == 0


def test_work_metrics_follow_normal_original_exit_without_budget_or_scope_calls(trace, monkeypatch):
    events, owner = [], Transaction(None)
    scoped = view(owner)
    def forbidden(*args, **kwargs):
        pytest.fail('Diagnostic must not execute SQL or validate a scope')
    for name in ('rows', 'one', 'all', 'execute'):
        monkeypatch.setattr(Transaction, name, forbidden)
    monkeypatch.setattr(RunTransaction, 'require_scope', forbidden)
    @contextmanager
    def original():
        events.append('enter')
        yield scoped
        owner.rows_read, owner.bytes_read = 4, 80
        events.append('exit')
    with trace.reader(original()) as result:
        assert result is scoped
    row = trace.reader_totals['other']
    assert (row['rows'], row['bytes'], row['owners']) == (4, 80, 1)
    assert events == ['enter', 'exit']


def test_missing_work_is_explicit_and_failed_outer_does_not_adopt_nested_work(trace):
    owner = Transaction(None)
    with trace.reader(context(object(), [])):
        pass
    assert trace.reader_totals['other']['missing'] == {'owner_unavailable': 1}
    original = RuntimeError('outer failed')
    with pytest.raises(RuntimeError) as caught:
        with trace.reader(context(owner, [])):
            with trace.reader(context(owner, [])):
                owner.rows_read, owner.bytes_read = 4, 100
            raise original
    assert caught.value is original
    row = trace.reader_totals['other']
    assert (row['owners'], row['rows'], row['bytes']) == (0, 0, 0)
    assert row['missing']['context_failed'] == 1


@pytest.mark.parametrize('fault', [TimeoutError, InterruptedError])
def test_normal_exit_timer_failure_releases_owner_and_next_reader_is_independent(trace, monkeypatch, fault):
    events, ticks, error = [], [], fault('exit timer')
    def timer():
        ticks.append(1)
        if len(ticks) == 2:
            raise error
        return len(ticks)
    monkeypatch.setattr(costs, 'thread_time_ns', timer)
    with pytest.raises(fault) as caught:
        with trace.reader(context(Transaction(None), events)):
            retained = trace.local.reader
    assert caught.value is error and events == ['enter', 'exit']
    assert trace.local.reader is None
    assert retained['owners'] == {}
    with trace.reader(context(Transaction(None), events)):
        pass
    assert trace.reader_totals['other']['calls'] == 1
    assert trace.reader_totals['other']['max_depth'] == 1


def test_counter_unavailable_is_missing_not_an_invented_zero(trace):
    owner = Transaction(None)
    del owner.bytes_read
    with trace.reader(context(owner, [])):
        pass
    row = trace.reader_totals['other']
    assert row['owners'] == 0 and row['missing'] == {'counters_unavailable': 1}


def test_trace_and_profile_flags_are_sampled_only_at_outer_entry(trace, monkeypatch):
    calls = []
    monkeypatch.setattr(costs.sys, 'gettrace', lambda: calls.append('trace') or object())
    monkeypatch.setattr(costs.sys, 'getprofile', lambda: calls.append('profile') or None)
    with trace.reader(context(Transaction(None), [])):
        with trace.reader(context(Transaction(None), [])):
            pass
    assert calls == ['trace', 'profile']
    row = trace.reader_totals['other']
    assert (row['trace_entries'], row['profile_entries']) == (1, 0)


def test_fixed_entry_labels_count_real_calls_and_restore_after_failure(trace, monkeypatch):
    events, owner = [], Transaction(None)
    @contextmanager
    def transaction(*args, **kwargs):
        with context(owner, events) as tx:
            yield tx
    def read(*args, **kwargs):
        with Storage.transaction(object(), write=False):
            return sentinel
    sentinel = object()
    monkeypatch.setattr(Storage, 'transaction', transaction)
    for cls, name in ((Kernel, 'guard_agent'), (Kernel, 'worker_settled'), (NativeNotifications, '_send_status'), (OperationRunner, 'run_once'),
                      (OperationRunner, 'state'), (OperationRunner, 'supervise')):
        monkeypatch.setattr(cls, name, read)
    costs.observe_host(monkeypatch.setattr, trace)
    for cls, name in ((Kernel, 'guard_agent'), (Kernel, 'worker_settled'), (NativeNotifications, '_send_status'), (OperationRunner, 'run_once'),
                      (OperationRunner, 'state'), (OperationRunner, 'supervise')):
        assert getattr(cls, name)(object()) is sentinel
    assert {key: row['calls'] for key, row in trace.reader_totals.items()} == {
        'guard/preparation': 1, 'worker/lifecycle': 1, 'Status': 1,
        'operation/accept': 1, 'state/supervise': 2}
    assert trace.guard_calls == 1 and trace.local.category == 'other'
    assert events == ['enter', 'exit'] * 6


@pytest.mark.parametrize('owner,name', [(NativeNotifications, '_send_status'), (Kernel, 'worker_settled')])
def test_new_marks_add_no_timer_query_or_publish_when_original_does_not_read(trace, monkeypatch, owner, name):
    sentinel = object()
    monkeypatch.setattr(owner, name, lambda *args, **kwargs: sentinel)
    costs.observe_host(monkeypatch.setattr, trace)
    def forbidden(*args, **kwargs):
        pytest.fail('An entry label must not sample, query or publish')
    monkeypatch.setattr(costs, 'thread_time_ns', forbidden)
    monkeypatch.setattr(trace, 'flush', forbidden)
    monkeypatch.setattr(Transaction, 'execute', forbidden)
    assert getattr(owner, name)(object()) is sentinel
    assert not trace.totals and not trace.reader_totals and trace.local.category == 'other'


@pytest.mark.parametrize('target', ['status', 'settled'])
@pytest.mark.parametrize('failure_type', [RuntimeError, TimeoutError, InterruptedError, KeyboardInterrupt])
def test_new_entry_failure_keeps_original_when_reader_timer_also_fails(trace, monkeypatch, target, failure_type):
    original, watchdog = failure_type('original'), TimeoutError('reader timer')
    calls, failed = [], []
    timer = costs.thread_time_ns
    def measured_cpu():
        if failed:
            raise watchdog
        return timer()
    @contextmanager
    def transaction(*args, **kwargs):
        yield Transaction(None)
    def read(*args, **kwargs):
        calls.append((args, kwargs))
        with Storage.transaction(object(), write=False):
            failed.append(True)
            raise original
    owner, name = (NativeNotifications, '_send_status') if target == 'status' else (Kernel, 'worker_settled')
    monkeypatch.setattr(owner, name, read)
    monkeypatch.setattr(Storage, 'transaction', transaction)
    costs.observe_host(monkeypatch.setattr, trace)
    monkeypatch.setattr(costs, 'thread_time_ns', measured_cpu)
    sentinel = object()
    with pytest.raises(failure_type) as caught:
        getattr(owner, name)(sentinel, 'private', option=sentinel)
    assert caught.value is original and calls == [((sentinel, 'private'), {'option': sentinel})]
    assert trace.local.reader is None and trace.local.category == 'other'
    monkeypatch.setattr(costs, 'thread_time_ns', timer)
    with Storage.transaction(object(), write=False):
        pass
    assert trace.reader_totals.keys() == {'other'} and trace.guard_calls == 0


def test_executor_lifecycle_keeps_guard_separate_and_restores_outer_tag(trace, monkeypatch):
    import sys
    events = []
    @contextmanager
    def transaction(*args, **kwargs):
        with context(Transaction(None), events) as tx:
            yield tx
    def read(*args, **kwargs):
        with Storage.transaction(object(), write=False):
            pass
    class Driver:
        def agent(self):
            read()
            Kernel.guard_agent(object())
            Kernel.worker_settled(object())
    module = SimpleNamespace(__spec__=SimpleNamespace(name='agent_run.runtime.executor'), _Executor=Driver, wait=lambda: None)
    monkeypatch.delitem(sys.modules, 'agent_run.runtime.executor', raising=False)
    monkeypatch.setitem(sys.modules, '__main__', module)
    monkeypatch.setattr(Storage, 'transaction', transaction)
    monkeypatch.setattr(Kernel, 'guard_agent', read)
    monkeypatch.setattr(Kernel, 'worker_settled', read)
    costs.observe_host(monkeypatch.setattr, trace)
    Driver().agent()
    read()
    assert {key: row['calls'] for key, row in trace.reader_totals.items()} == {
        'worker/lifecycle': 2, 'guard/preparation': 1, 'other': 1}
    assert trace.guard_calls == 1 and trace.local.category == 'other'
    assert all(row['max_depth'] == 1 for row in trace.reader_totals.values())
    assert events == ['enter', 'exit'] * 4


def test_guard_exception_keeps_same_object_and_cannot_leak_tag_to_next_reader(trace, monkeypatch):
    original, calls = InterruptedError('guard'), []
    def guard(*args, **kwargs):
        calls.append(1)
        with Storage.transaction(object(), write=False):
            raise original
    @contextmanager
    def transaction(*args, **kwargs):
        yield Transaction(None)
    monkeypatch.setattr(Kernel, 'guard_agent', guard)
    monkeypatch.setattr(Storage, 'transaction', transaction)
    costs.observe_host(monkeypatch.setattr, trace)
    with pytest.raises(InterruptedError) as caught:
        Kernel.guard_agent(object(), object())
    assert caught.value is original and calls == [1] and trace.guard_calls == 1
    with Storage.transaction(object(), write=False):
        pass
    assert trace.reader_totals['guard/preparation']['failed'] == 1
    assert trace.reader_totals['other']['calls'] == 1


@pytest.mark.parametrize('fails', [False, True])
def test_runner_execution_is_captured_only_after_successful_original_init(trace, monkeypatch, fails):
    execution = SimpleNamespace(run_id='A', owner='O', epoch=2)
    original, result, calls = RuntimeError('init'), object(), []
    def initialized(self, **kwargs):
        self.execution = execution
        calls.append(kwargs)
        if fails:
            raise original
        return result
    monkeypatch.setattr(OperationRunner, '__init__', initialized)
    costs.observe_host(monkeypatch.setattr, trace)
    runner = SimpleNamespace()
    if fails:
        with pytest.raises(RuntimeError) as caught:
            OperationRunner.__init__(runner, execution=execution)
        assert caught.value is original and trace.execution_identity is None
    else:
        assert OperationRunner.__init__(runner, execution=execution) is result
        assert trace.execution_identity == ('A', 'O', 2)
    assert calls == [{'execution': execution}]


@pytest.mark.parametrize('case', ['match', 'missing-request', 'missing-execution', 'mismatch', 'conflict', 'execution-conflict'])
def test_host_request_association_never_claims_physical_nonce_binding(trace, case):
    request = SimpleNamespace(run_id='A', owner='O', epoch=2, nonce='N')
    execution = SimpleNamespace(run_id='A', owner='O', epoch=2)
    if case != 'missing-request':
        trace.host_request('action', request)
        trace.host_request('action', request)
    if case != 'missing-execution':
        trace.execution(execution)
    if case == 'mismatch':
        # A different actual Execution, rather than a fabricated snapshot field.
        trace.execution_identity = None
        trace.execution(SimpleNamespace(run_id='B', owner='O', epoch=2))
    if case == 'conflict':
        trace.host_request('different-action', request)
        trace.host_request('action', request)  # Conflict remains sticky.
    if case == 'execution-conflict':
        trace.execution(SimpleNamespace(run_id='B', owner='O', epoch=2))
    value = trace._reader_snapshot()['association']
    assert value['request_matches_execution'] is (case == 'match')
    assert value['nonce_handshake_proven'] is False


@pytest.mark.parametrize('fails', [False, True])
def test_request_hook_captures_only_original_normal_return_without_flush(trace, monkeypatch, fails):
    result, original, calls = SimpleNamespace(run_id='A', owner='O', epoch=1, nonce='N'), RuntimeError('request'), []
    def request(self, action_id):
        calls.append((self, action_id))
        if fails:
            raise original
        return result
    monkeypatch.setattr(Kernel, 'host_request', request)
    monkeypatch.setattr(trace, 'flush', lambda: pytest.fail('Request hook must not flush'))
    costs.observe_host(monkeypatch.setattr, trace)
    kernel = object()
    if fails:
        with pytest.raises(RuntimeError) as caught:
            Kernel.host_request(kernel, action_id='action')
        assert caught.value is original and trace.request_identity is None
    else:
        assert Kernel.host_request(kernel, action_id='action') is result
        assert trace.request_identity == ('action', 'A', 'O', 1, 'N')
    assert calls == [(kernel, 'action')]
