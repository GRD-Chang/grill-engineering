"""Read-only completion sampling over normal Core control/history producers.

No Host, Worker or native save admission runs. The observer clock and child
status are explicit inputs; durable Task/Run/control facts use real SQLite.
"""
from contextlib import closing, contextmanager
from types import SimpleNamespace
import sqlite3

import pytest

from agent_run.errors import KernelError
from agent_run.model import Delivery, DeliveryKind, RunSpec
from support.core_operations import delivery_run
from support.repository_fixture import foreign_task_run
from support.sqlite_faults import logical_state
from support import systemd_manager


SUBJECT = 'ticket:2'
RESUME = 'original-resume'


class Child:
    """Only the observer's poll input; never a physical process claim."""
    returncode = None

    def poll(self):
        return self.returncode


@pytest.fixture
def resumed(tmp_path):
    kernel, source = delivery_run(tmp_path / 'kernel.sqlite3')
    assert kernel.control('stop', 'original-stop', subject=SUBJECT) == 'applied'
    assert kernel.control('resume', RESUME, subject=SUBJECT) == 'applied'
    return kernel, source


def gate(kernel, code):
    # Synthetic domain input through the sole production gate/history writer,
    # not a fabricated SQL pause or Completion row.
    with kernel.storage.transaction(ordered=False) as tx:
        kernel._gate(tx, SUBJECT, code)


def observe(kernel, monkeypatch, *, action=RESUME, deadline=101.0):
    now, reads, phases, errors = [100.0], [], [], []
    child = Child()
    original = kernel.storage.transaction

    @contextmanager
    def transaction(**kwargs):
        assert kwargs == {'write': False}
        reads.append(True)
        with original(**kwargs) as tx:
            yield tx

    monkeypatch.setattr(kernel.storage, 'transaction', transaction)
    # Only the test observer's clock namespace changes; Storage and Core keep
    # their original clock/admission providers.
    monkeypatch.setattr(systemd_manager, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    predicate = systemd_manager._resumed_ticket_completion(kernel, SUBJECT, action,
        deadline=deadline, child=child, errors=errors, output=[], on_phase=phases.append)
    return SimpleNamespace(predicate=predicate, now=now, reads=reads, phases=phases,
        errors=errors, child=child, original=original)


def test_old_stop_and_later_sibling_or_foreign_pauses_do_not_belong_to_the_resumed_ticket(resumed, tmp_path, monkeypatch):
    kernel, source = resumed
    assert kernel.control('stop', 'sibling-stop', subject='ticket:3') == 'applied'
    first, second = kernel.delivery(SUBJECT), kernel.delivery('ticket:3')
    final = Delivery('final:1', DeliveryKind.FINAL_RUN, 1, 'unused', first.base,
        first.target_ref, 'refs/heads/main')
    spec = RunSpec(kernel.storage.run_id, 'example/repository', 1, 'unused', 'unused',
        (first, second, final), repository_root=str(tmp_path), language='en')
    foreign = foreign_task_run(kernel, spec, source)
    assert foreign.control('stop', 'foreign-stop', subject=SUBJECT) == 'applied'
    assert foreign.control('resume', RESUME, subject=SUBJECT) == 'applied'
    gate(foreign, 'human_blocker')
    before = logical_state(kernel.storage.path)
    observation = observe(kernel, monkeypatch)
    assert observation.predicate() is False
    assert observation.phases == ['development'] and len(observation.reads) == 1
    assert logical_state(kernel.storage.path) == before


def test_a_human_gate_before_the_exact_answer_is_not_a_later_failure(resumed, monkeypatch):
    kernel, _ = resumed
    gate(kernel, 'human_blocker')
    assert kernel.control('resume', 'human-answer', subject=SUBJECT, message='Original answer') == 'applied'
    observation = observe(kernel, monkeypatch, action='human-answer')
    assert observation.predicate() is False
    assert observation.phases == ['development']


@pytest.mark.parametrize('code', ['operator_stopped', 'human_blocker', 'write_result_unknown'])
def test_any_current_target_gate_refuses_completion(resumed, monkeypatch, code):
    kernel, _ = resumed
    gate(kernel, code)
    before = logical_state(kernel.storage.path)
    observation = observe(kernel, monkeypatch)
    with pytest.raises(AssertionError) as caught:
        observation.predicate()
    assert code in str(caught.value)
    assert logical_state(kernel.storage.path) == before


@pytest.mark.parametrize('code', ['operator_stopped', 'human_blocker'])
def test_gate_created_and_cleared_before_first_sample_cannot_be_hidden_by_a_later_highwater(resumed, monkeypatch, code):
    kernel, _ = resumed
    gate(kernel, code)
    assert kernel.control('resume', 'later-answer', subject=SUBJECT, message='Later answer') == 'applied'
    assert kernel.status(SUBJECT)['gates'] == []
    with kernel.storage.transaction(write=False) as tx:
        position = tx.one("SELECT position FROM history WHERE run_id=? AND subject=? AND kind='control_applied' AND identity=?",
            (kernel.storage.run_id, SUBJECT, RESUME))[0]
        assert tx.one("SELECT count(*) FROM history WHERE run_id=? AND subject=? AND position>? AND kind='paused'",
            (kernel.storage.run_id, SUBJECT, position))[0] == 1
    observation = observe(kernel, monkeypatch)
    with pytest.raises(AssertionError) as caught:
        observation.predicate()
    assert code in str(caught.value)


def test_between_sample_pause_remains_visible_after_its_gate_is_cleared(resumed, monkeypatch):
    kernel, _ = resumed
    observation = observe(kernel, monkeypatch)
    assert observation.predicate() is False
    with monkeypatch.context() as writes:
        writes.setattr(kernel.storage, 'transaction', observation.original)
        assert kernel.control('stop', 'transient-stop', subject=SUBJECT) == 'applied'
        assert kernel.control('resume', 'transient-answer', subject=SUBJECT) == 'applied'
        assert kernel.status(SUBJECT)['gates'] == []
    observation.now[0] += 0.01
    assert observation.predicate() is False and len(observation.reads) == 1
    observation.now[0] = 100.051
    with pytest.raises(AssertionError, match='operator_stopped'):
        observation.predicate()
    assert len(observation.reads) == 2


@pytest.mark.parametrize('action', ['missing-resume', 'sibling-resume', 'original-stop'])
def test_missing_or_other_subject_resume_receipt_cannot_supply_the_observation_boundary(resumed, monkeypatch, action):
    kernel, _ = resumed
    assert kernel.control('stop', 'sibling-stop', subject='ticket:3') == 'applied'
    assert kernel.control('resume', 'sibling-resume', subject='ticket:3') == 'applied'
    observation = observe(kernel, monkeypatch, action=action)
    with pytest.raises(KernelError, match='missing_or_ambiguous_fact'):
        observation.predicate()


def test_repeated_pending_samples_are_read_only_and_limit_database_reads(resumed, monkeypatch):
    kernel, _ = resumed
    with closing(sqlite3.connect(kernel.storage.path)) as holder:
        holder.execute('BEGIN')
        holder.execute('SELECT count(*) FROM runs').fetchone()
        before, contents = logical_state(kernel.storage.path), kernel.storage.path.read_bytes()
        observation = observe(kernel, monkeypatch)
        for at in (100.0, 100.01, 100.02, 100.04):
            observation.now[0] = at
            assert observation.predicate() is False
        assert len(observation.reads) == 1
        observation.now[0] = 100.051
        assert observation.predicate() is False
        assert len(observation.reads) == 2 and observation.phases == ['development', 'development']
        assert logical_state(kernel.storage.path) == before
        assert kernel.storage.path.read_bytes() == contents


def test_manager_error_is_checked_inside_the_cached_interval(resumed, monkeypatch):
    kernel, _ = resumed
    observation = observe(kernel, monkeypatch)
    assert observation.predicate() is False
    failure = OSError('Original manager failure')
    observation.errors.append(failure)
    observation.now[0] += 0.001
    with pytest.raises(AssertionError) as caught:
        observation.predicate()
    assert caught.value.args[0][0] is failure and len(observation.reads) == 1


def test_child_exit_forces_a_fresh_read_even_inside_the_cached_interval(resumed, monkeypatch):
    kernel, _ = resumed
    observation = observe(kernel, monkeypatch)
    assert observation.predicate() is False
    observation.child.returncode = 0
    observation.now[0] += 0.001
    with pytest.raises(AssertionError) as caught:
        observation.predicate()
    assert caught.value.args[0][:2] == (0, 'development') and len(observation.reads) == 2


def test_exited_child_with_complete_read_result_satisfies_only_the_observer_contract(resumed, monkeypatch):
    """Synthetic terminal read result: this does not prove a Core Completion."""
    kernel, _ = resumed
    assert kernel.status(SUBJECT)['phase'] == 'development'
    before = logical_state(kernel.storage.path)
    observation = observe(kernel, monkeypatch)
    original = kernel.storage.transaction
    selected = []

    @contextmanager
    def terminal_read(**kwargs):
        with original(**kwargs) as tx:
            def one(statement, parameters=()):
                result = tx.one(statement, parameters)
                if statement == 'SELECT phase FROM subjects WHERE run_id=? AND id=?':
                    assert parameters == (kernel.storage.run_id, SUBJECT)
                    assert result[0] == 'development'
                    selected.append(parameters)
                    return ('complete',)  # Read-side input only; no stored fact changes.
                return result
            yield SimpleNamespace(one=one, rows=tx.rows)

    monkeypatch.setattr(kernel.storage, 'transaction', terminal_read)
    observation.child.returncode = 0
    assert observation.predicate() is True
    assert selected == [(kernel.storage.run_id, SUBJECT)]
    assert observation.phases == ['complete'] and len(observation.reads) == 1
    assert logical_state(kernel.storage.path) == before


def test_original_deadline_expiry_refuses_before_another_database_read(resumed, monkeypatch):
    kernel, _ = resumed
    observation = observe(kernel, monkeypatch, deadline=100.0)
    with pytest.raises(AssertionError, match='expected boundary'):
        observation.predicate()
    assert observation.reads == observation.phases == []


def test_a_read_crossing_the_original_deadline_cannot_publish_its_result(resumed, monkeypatch):
    kernel, _ = resumed
    observation = observe(kernel, monkeypatch, deadline=100.05)
    original = kernel.storage.transaction

    @contextmanager
    def expire_after_read(**kwargs):
        with original(**kwargs) as tx:
            yield tx
        observation.now[0] = 100.05

    monkeypatch.setattr(kernel.storage, 'transaction', expire_after_read)
    with pytest.raises(AssertionError, match='expected boundary'):
        observation.predicate()
    assert len(observation.reads) == 1 and observation.phases == []


def test_snapshot_failure_preserves_the_exact_exception_without_retry(resumed, monkeypatch):
    kernel, _ = resumed
    observation = observe(kernel, monkeypatch)
    failure, calls = OSError('Original preflight failure'), []

    @contextmanager
    def unavailable(**kwargs):
        calls.append(kwargs)
        raise failure
        yield  # pragma: no cover - context-manager shape only.

    monkeypatch.setattr(kernel.storage, 'transaction', unavailable)
    with pytest.raises(OSError) as caught:
        observation.predicate()
    assert caught.value is failure and calls == [{'write': False}]
    assert observation.phases == []
