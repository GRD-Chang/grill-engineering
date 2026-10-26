"""Kernel Worker APIs on the inactive schema, not public admission/Host proof.

The test transaction source owns one real SQLite transaction and original
Transaction accounting. It does not substitute a production Store, implement
file admission/FIFO/capacity or claim the new public protocol can open yet.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
import sqlite3

import pytest

from agent_run.contracts.agent_profiles import resolve_profiles
from agent_run.core import kernel as core
from agent_run.core.attempt_continuation import AttemptContinuationBasis, bind_attempt_continuation
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import AgentPreparation, Execution, Profile, Purpose, Role, wire_json
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.repository_scope import connection, insert, seed


SUBJECT = "parent:1"


class ScopedKernel(Kernel):
    """Only transaction injection; every business operation is the real Kernel."""

    def __init__(self, db, run):
        self.owners = []

        @contextmanager
        def transaction(*, write=True, ordered=True, cancelled=None):
            assert cancelled is None and not db.in_transaction
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            owner = Transaction(db)
            self.owners.append(owner)
            try:
                yield RunTransaction(owner, run)
                if owner.failed:
                    raise KernelError("transaction_aborted")
                db.commit()
            except BaseException:
                db.rollback()
                raise

        self.storage = SimpleNamespace(transaction=transaction, path=Path("/fixture/kernel.sqlite3"))
        self.clock = lambda: 100.
        self._observation = None
        self._observation_lock = RLock()


def _prepare(db, owner, run):
    tx = RunTransaction(owner, run)
    profiles = resolve_profiles(overrides={"development_model": "original-" + run})
    material = tx.put(SUBJECT, "profile", wire_json(profiles))
    db.execute("UPDATE profile_revisions SET material_id=? WHERE run_id=?", (material, run))
    db.execute("UPDATE runs SET executor='same-owner',epoch=1 WHERE id=?", (run,))
    insert(db, "threads", {"run_id": run, "id": "same-thread", "subject": SUBJECT,
        "role": "development", "profile_revision": 1})
    facts = {"generation": 1, "role": "development", "purpose": "requirements", "revision": "r",
        "parent_revision": "r", "graph_revision": "g", "completion_revision": "no-tickets",
        "base_sha": "a" * 40, "candidate_sha": None, "candidate_tree": None, "expected_tree": None}
    context = facts | {"repository": "example/project", "parent_issue": 1, "issue": 1,
        "scope": "parent_only", "checkout": "/fixture/" + run, "readonly": False}
    context_id = tx.put(SUBJECT, "role_context", wire_json(context))
    insert(db, "attempts", facts | {"run_id": run, "id": "same-attempt", "subject": SUBJECT,
        "ordinal": 1, "window": 1, "context_id": context_id, "status": "allocated"})
    bind_attempt_continuation(tx, "same-attempt", AttemptContinuationBasis("same-thread", context_id))
    db.execute("UPDATE subjects SET current_attempt='same-attempt' WHERE run_id=?", (run,))
    insert(db, "budgets", {"run_id": run, "subject": SUBJECT, "purpose": "requirements", "window": 1,
        "development_limit": 3, "review_required": 0})
    profile = profiles["profiles"]["development"]
    preparation = AgentPreparation("physical-seat-" + run, "same-attempt", SUBJECT, Role.DEVELOPMENT,
        Purpose.REQUIREMENTS, "same-thread", Execution(run, "same-owner", 1), context,
        Profile(profile["model"], profile["reasoning_effort"]), 7., 120.)
    data = {key: value for key, value in asdict(preparation).items()
            if key not in {"context", "profile", "preparation_deadline"}}
    data.update(context_id=context_id, previous_invocation=None, resume_action=None, new_thread=False,
        owned_period=False, retained_deadline=None, recovery_kind=None)
    scope = tx.put(SUBJECT, "agent_preparation", wire_json(data))
    insert(db, "waits", {"run_id": run, "subject": SUBJECT, "kind": "agent_preparation", "scope_id": scope,
        "started_at": 100., "deadline": 120.})
    read_scope = tx.put(SUBJECT, "supervision_scope", wire_json({"run": run, "subject": SUBJECT}))
    insert(db, "waits", {"run_id": run, "subject": SUBJECT, "kind": "github_convergence", "scope_id": read_scope,
        "started_at": 100., "deadline": 700.})
    return preparation


@pytest.fixture
def scenario(monkeypatch):
    with closing(connection()) as db:
        db.execute("BEGIN")
        seed(db, "A")
        seed(db, "B")
        owner = Transaction(db)
        preparations = {run: _prepare(db, owner, run) for run in ("A", "B")}
        db.commit()
        # Opaque IDs may collide between Runs. Physical lease/backend identities
        # remain distinct, as required by the original physical uniqueness keys.
        monkeypatch.setattr(core, "identity", lambda: "same-invocation")
        yield db, {run: ScopedKernel(db, run) for run in ("A", "B")}, preparations


def _rows(db, run):
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: [tuple(row) for row in db.execute(
        f"SELECT * FROM {name} WHERE {'id' if name == 'runs' else 'run_id'}=? ORDER BY rowid", (run,))]
        for name in names if name != "meta"}


def _start(kernel, preparation):
    call = kernel.admit_agent(preparation).call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.bind_thread(call, "physical-backend-" + call.execution.run_id)
    process = {"pid": 21, "pgid": 21, "start_token": "synthetic-" + call.execution.run_id,
        "host_pid": 10, "host_start_token": "synthetic-host"}
    kernel.agent_started(call, process)
    return call, process


def test_two_runs_prepare_go_charge_receive_and_exit_without_touching_the_sibling(scenario):
    db, kernels, preparations = scenario
    before_b = _rows(db, "B")
    first, process = _start(kernels["A"], preparations["A"])
    assert _rows(db, "B") == before_b
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 0
    kernels["A"].agent_source_ready(first)
    kernels["A"].agent_source_ready(first)
    assert db.execute("SELECT count(*) FROM waits WHERE run_id='A' AND kind='github_convergence'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM waits WHERE run_id='B' AND kind='github_convergence'").fetchone()[0] == 1
    raw = b'{"result_kind":"development","summary":"original A","human_blockers":null}'
    kernels["A"].receive(first, raw)
    kernels["A"].receive(first, raw)
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 1
    assert not kernels["A"].worker_settled(first)
    kernels["A"].agent_exited(first, process, 0)
    assert kernels["A"].worker_settled(first)
    assert _rows(db, "B") == before_b
    before_a = _rows(db, "A")
    second, process_b = _start(kernels["B"], preparations["B"])
    assert first.invocation_id == second.invocation_id == "same-invocation"
    assert first.deadline_at == second.deadline_at == 107.
    kernels["B"].receive(second, raw.replace(b"A", b"B"))
    kernels["B"].agent_exited(second, process_b, 0)
    assert kernels["B"].worker_settled(second) and _rows(db, "A") == before_a
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='B'").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchone() is None


def test_foreign_run_same_owner_epoch_call_cannot_write_and_rollback_is_exact(scenario):
    db, kernels, preparations = scenario
    call, process = _start(kernels["A"], preparations["A"])
    before = {run: _rows(db, run) for run in ("A", "B")}
    foreign = replace(call, execution=Execution("B", call.execution.owner, call.execution.epoch))
    with pytest.raises(KernelError, match="invocation_binding"):
        kernels["A"].receive(foreign, b"foreign original raw")
    assert {run: _rows(db, run) for run in ("A", "B")} == before
    with pytest.raises(KernelError):
        kernels["A"].guard_agent(replace(call, execution=replace(call.execution, epoch=2)))
    with pytest.raises(KernelError, match="invocation_binding"):
        kernels["A"].receive(replace(call, subject="unknown-original-subject"), b"foreign raw")
    with pytest.raises(KernelError, match="invocation_binding"):
        kernels["A"].receive(replace(call, context=preparations["B"].context), b"foreign context raw")
    assert {run: _rows(db, run) for run in ("A", "B")} == before


def test_failed_final_event_rolls_back_preparation_transfer_and_keeps_shared_poison(scenario):
    db, kernels, preparations = scenario
    before = {run: _rows(db, run) for run in ("A", "B")}
    db.set_authorizer(lambda action, table, *_: sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_INSERT and table == "history" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            kernels["A"].admit_agent(preparations["A"])
    finally:
        db.set_authorizer(None)
    assert kernels["A"].owners[-1].failed
    assert {run: _rows(db, run) for run in ("A", "B")} == before
    call = kernels["A"].admit_agent(preparations["A"]).call
    assert call is not None and call.deadline_at == 107.
    assert _rows(db, "B") == before["B"]


def test_owned_preparation_cleanup_is_zero_fee_and_cannot_settle_a_sibling(scenario):
    db, kernels, preparations = scenario
    before_b = _rows(db, "B")
    with pytest.raises(KernelError, match="preparation_binding"):
        kernels["A"].guard_preparation(preparations["B"])
    with pytest.raises(KernelError, match="preparation_binding"):
        kernels["A"].worker_settled(preparations["B"])
    kernels["A"].preparation_failed(preparations["A"], "credential_unavailable", {"closed": True})
    assert kernels["A"].worker_settled(preparations["A"])
    assert not kernels["B"].worker_settled(preparations["B"])
    assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 0
    before_a = _rows(db, "A")
    kernels["A"].preparation_failed(preparations["A"], "cancelled", {"closed": True})
    assert _rows(db, "A") == before_a and _rows(db, "B") == before_b


def test_unknown_dispatch_holds_its_lease_and_raw_does_not_invent_an_exit(scenario):
    db, kernels, preparations = scenario
    before_b = _rows(db, "B")
    kernel = kernels["A"]
    call = kernel.admit_agent(preparations["A"]).call
    assert call is not None and kernel.dispatch_agent(call)
    kernel.agent_start_unknown(call, {"reason": "synthetic lost start acknowledgement"})
    assert not kernel.worker_settled(call)
    before_a = _rows(db, "A")
    with pytest.raises(KernelError, match="worker_exit_unproven"):
        kernel.agent_unavailable(call, "unavailable", {"closed": True})
    assert _rows(db, "A") == before_a
    kernel.receive(call, b"original raw proves work but not exit")
    assert not kernel.worker_settled(call)
    assert db.execute("SELECT count(*) FROM leases WHERE run_id='A'").fetchone()[0] == 1
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 1
    assert _rows(db, "B") == before_b


def test_exact_activity_is_idempotent_and_conflicting_evidence_rolls_back(scenario):
    db, kernels, preparations = scenario
    before_b = _rows(db, "B")
    call, process = _start(kernels["A"], preparations["A"])
    kernels["A"].agent_source_ready(call)
    kernels["A"].clock = lambda: 101.
    evidence = {"type": "turn.started", "source": "synthetic-owned-Worker"}
    kernels["A"].observe_worker_activity(call, "same-observation", active=True, observed_at=101., evidence=evidence)
    before_a = _rows(db, "A")
    kernels["A"].observe_worker_activity(call, "same-observation", active=True, observed_at=101., evidence=evidence)
    assert _rows(db, "A") == before_a
    with pytest.raises(KernelError, match="conflicting_activity_observation"):
        kernels["A"].observe_worker_activity(call, "same-observation", active=True, observed_at=101.,
            evidence={"type": "different original evidence"})
    assert _rows(db, "A") == before_a and _rows(db, "B") == before_b
    assert db.execute("SELECT count(*) FROM history WHERE run_id='A' AND kind='worker_active'").fetchone()[0] == 1
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 1


def test_original_gate_revokes_go_but_late_raw_and_exit_settle_only_their_lease(scenario):
    db, kernels, preparations = scenario
    before_b = _rows(db, "B")
    kernel = kernels["A"]
    call, process = _start(kernel, preparations["A"])
    # Exercise the existing domain gate, not an unconverted public Stop router.
    with kernel.storage.transaction() as tx:
        kernel._gate(tx, SUBJECT, "operator_stopped")
    with pytest.raises(KernelError, match="agent_authority_changed"):
        kernel.agent_source_ready(call)
    kernel.receive(call, b"original exact late raw")
    assert not kernel.worker_settled(call)
    kernel.agent_exited(call, process, 0)
    assert kernel.worker_settled(call)
    assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == "operator_stopped"
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 1
    assert _rows(db, "B") == before_b


def test_original_deadlines_do_not_refresh_and_expired_go_remains_zero_fee(scenario):
    db, kernels, preparations = scenario
    before_b = _rows(db, "B")
    kernel = kernels["A"]
    kernel.clock = lambda: 120.
    before_a = _rows(db, "A")
    with pytest.raises(KernelError, match="preparation_deadline_exceeded"):
        kernel.admit_agent(preparations["A"])
    assert _rows(db, "A") == before_a
    kernel.clock = lambda: 119.
    call, process = _start(kernel, preparations["A"])
    assert call.deadline_at == 126. and call.deadline_seconds == 7.
    kernel.clock = lambda: 126.
    with pytest.raises(KernelError, match="invocation_deadline_exceeded"):
        kernel.agent_source_ready(call)
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 0
    assert db.execute("SELECT deadline_at FROM invocations WHERE run_id='A'").fetchone()[0] == 126.
    assert _rows(db, "B") == before_b


def test_preparation_exit_uses_only_the_exact_run_owner_epoch_and_agent_seats(scenario):
    db, kernels, preparations = scenario
    before = {run: _rows(db, run) for run in ("A", "B")}
    with kernels["A"].storage.transaction() as tx:
        assert kernels["A"]._agent_seats(tx) == kernels["A"]._agent_seats(tx, SUBJECT) == 1
        kernels["A"]._host_preparations_exited(tx, preparations["B"].execution, {"synthetic": True})
        kernels["A"]._host_preparations_exited(tx, replace(preparations["A"].execution, epoch=2), {"synthetic": True})
    assert {run: _rows(db, run) for run in ("A", "B")} == before
    with kernels["A"].storage.transaction() as tx:
        kernels["A"]._host_preparations_exited(tx, preparations["A"].execution, {"synthetic": True})
        assert kernels["A"]._agent_seats(tx) == 0
    assert kernels["A"].worker_settled(preparations["A"])
    assert _rows(db, "B") == before["B"]


def test_existing_ownership_and_review_charge_gates_refuse_foreign_rows(scenario):
    db, kernels, preparations = scenario
    before = {run: _rows(db, run) for run in ("A", "B")}
    with kernels["A"].storage.transaction() as tx:
        foreign_run = db.execute("SELECT * FROM runs WHERE id='B'").fetchone()
        foreign_attempt = db.execute("SELECT * FROM attempts WHERE run_id='B'").fetchone()
        with pytest.raises(KernelError, match="executor_fenced"):
            kernels["A"]._executor_owned(tx, foreign_run, preparations["B"].execution)
        with pytest.raises(KernelError, match="attempt_binding"):
            kernels["A"]._charge_review(tx, foreign_attempt)
    with pytest.raises(KernelError, match="preparation_binding"):
        kernels["A"].guard_preparation(preparations["B"])
    assert {run: _rows(db, run) for run in ("A", "B")} == before


def test_foreign_abandon_record_fences_its_run_without_blocking_sibling_authority(scenario):
    db, kernels, preparations = scenario
    insert(db, "actions", {"run_id": "B", "id": "foreign-abandon", "kind": "abandon", "status": "pending"})
    before_b = _rows(db, "B")
    with pytest.raises(KernelError, match="executor_fenced"):
        kernels["B"].guard_preparation(preparations["B"])
    call, process = _start(kernels["A"], preparations["A"])
    kernels["A"].agent_source_ready(call)
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 1
    assert _rows(db, "B") == before_b
