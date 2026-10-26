"""Initial allocation on persisted staged Runs, not public Task creation.

Fixtures provide relational creation prerequisites only. Real Kernel allocation,
preparation and Worker receipt APIs run over one owner/SQLite transaction.
"""
from contextlib import closing
import sqlite3

import pytest

from agent_run.contracts.agent_profiles import resolve_profiles
from agent_run.core import kernel as core
from agent_run.core.operation_contract import Work
from agent_run.errors import KernelError
from agent_run.model import Commit, Execution, Policy, Role, World, wire_json
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.repository_scope import add_subject, connection, insert, seed
from support.run_scoped_kernel import ScopedKernel


SUBJECT = "parent:1"
WORLD = World("r", "g", "r", Commit("a" * 40, "b" * 40))


@pytest.fixture
def scenario(monkeypatch):
    with closing(connection()) as db:
        db.execute("BEGIN")
        owner = Transaction(db)
        for run in ("A", "B"):
            seed(db, run)
            tx = RunTransaction(owner, run)
            policy = Policy(development_deadline_seconds=7.)
            db.execute("UPDATE runs SET policy=?,executor='same-owner',epoch=1 WHERE id=?",
                (wire_json(core.Kernel._policy_values(policy)), run))
            profiles = resolve_profiles(overrides={"development_model": "frozen-" + run})
            profile = tx.put(SUBJECT, "profile", wire_json(profiles))
            db.execute("UPDATE profile_revisions SET material_id=? WHERE run_id=?", (profile, run))
            inputs = tx.put(SUBJECT, "creation_inputs", wire_json({
                "prompt_resources": {"fixture": "frozen-" + run}, "worker_identity": {"fixture": run}}))
            insert(db, "run_inputs", {"run_id": run, "subject": SUBJECT, "material_id": inputs})
            insert(db, "budgets", {"run_id": run, "subject": SUBJECT, "purpose": "requirements",
                "window": 1, "development_limit": 3, "review_limit": 3, "review_required": 0})
        db.commit()
        # Deliberately collide logical IDs in distinct Runs, never physical leases.
        counter = iter(["same-attempt", "same-thread", "physical-A", "same-attempt", "same-thread", "physical-B", "same-invocation"])
        monkeypatch.setattr(core, "identity", lambda: next(counter))
        yield db, {run: ScopedKernel(db, run) for run in ("A", "B")}


def _work(run, *, allow=True):
    return Work(Execution(run, "same-owner", 1), "token", SUBJECT, False, allow, None, None)


def _allocate(kernel, run, **kwargs):
    with kernel.storage.transaction() as tx:
        header = tx.one("SELECT * FROM runs WHERE id=?", (run,))
        job = kernel._job(tx, SUBJECT)
        return kernel._allocate(tx, header, job, kwargs.pop("work", _work(run)), WORLD, **kwargs)


def _rows(db, run):
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: [tuple(row) for row in db.execute(
        f"SELECT * FROM {name} WHERE {'id' if name == 'runs' else 'run_id'}=? ORDER BY rowid", (run,))]
        for name in names if name != "meta"}


def test_initial_allocation_uses_only_its_run_and_freezes_inputs_before_any_charge(scenario):
    db, kernels = scenario
    before_b = _rows(db, "B")
    a = _allocate(kernels["A"], "A").preparation
    assert a is not None and a.execution.run_id == "A"
    assert a.context["_prompt_resources"] == {"fixture": "frozen-A"}
    assert a.context["github_identity"] == {"fixture": "A"}
    assert a.profile.model == "frozen-A" and a.preparation_deadline == 700.
    assert _rows(db, "B") == before_b
    assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 0
    before_a = _rows(db, "A")
    b = _allocate(kernels["B"], "B").preparation
    assert b is not None and b.attempt_id == a.attempt_id and b.thread_id == a.thread_id
    assert b.profile.model == "frozen-B" and _rows(db, "A") == before_a
    call = kernels["A"].admit_agent(a).call
    assert call is not None and call.deadline_at == 107.
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 0
    assert kernels["A"].dispatch_agent(call)
    kernels["A"].bind_thread(call, "physical-backend-A")
    kernels["A"].agent_started(call, {"pid": 21, "pgid": 21, "start_token": "synthetic-A",
        "host_pid": 10, "host_start_token": "synthetic-host"})
    kernels["A"].agent_source_ready(call)
    kernels["A"].agent_source_ready(call)
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='A'").fetchone()[0] == 1
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id='B'").fetchone()[0] == 0
    assert db.execute("PRAGMA foreign_key_check").fetchone() is None


def test_nonagent_queue_registration_and_capacity_are_per_run(scenario):
    db, kernels = scenario
    assert _allocate(kernels["B"], "B", work=_work("B", allow=False)).kind == "needs_agent"
    before_b = _rows(db, "B")
    result = _allocate(kernels["A"], "A")
    assert result.kind == "preparing" and _rows(db, "B") == before_b
    assert db.execute("SELECT next_ready FROM runs WHERE id='A'").fetchone()[0] == 1
    assert db.execute("SELECT next_ready FROM runs WHERE id='B'").fetchone()[0] == 1
    assert _allocate(kernels["A"], "A").reason == "previous_invocation_exit"
    assert _allocate(kernels["B"], "B").kind == "preparing"


def test_own_queue_order_still_blocks_a_later_subject_without_creating_an_attempt(scenario):
    db, kernels = scenario
    add_subject(db, "A", "other")
    insert(db, "work_queue", {"run_id": "A", "subject": "other", "ready": 0})
    before_b = _rows(db, "B")
    result = _allocate(kernels["A"], "A")
    assert (result.kind, result.reason) == ("waiting", "agent_capacity")
    assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM work_queue WHERE run_id='A'").fetchone()[0] == 2
    assert _rows(db, "B") == before_b


@pytest.mark.parametrize("foreign", ["header", "job", "execution", "pending"])
def test_foreign_allocation_arguments_are_rejected_before_any_write(scenario, foreign):
    db, kernels = scenario
    pending = None
    if foreign == "pending":
        _allocate(kernels["B"], "B")
        pending = db.execute("SELECT * FROM attempts WHERE run_id='B'").fetchone()
    before = {run: _rows(db, run) for run in ("A", "B")}
    with pytest.raises(KernelError, match="allocation_binding|executor_fenced"):
        with kernels["A"].storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs WHERE id=?", ("B" if foreign == "header" else "A",))
            job = tx.one("SELECT * FROM subjects WHERE run_id=?", ("B" if foreign == "job" else "A",))
            kernels["A"]._allocate(tx, run, job, _work("B" if foreign == "execution" else "A"), WORLD, pending)
    assert {run: _rows(db, run) for run in ("A", "B")} == before


def test_initial_allocation_error_poisons_original_owner_and_rolls_back_the_whole_chain(scenario):
    db, kernels = scenario
    before = {run: _rows(db, run) for run in ("A", "B")}
    def refuse_wait(operation, table, *_):
        return sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_INSERT and table == "waits" else sqlite3.SQLITE_OK
    db.set_authorizer(refuse_wait)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            _allocate(kernels["A"], "A")
    finally:
        db.set_authorizer(None)
    assert kernels["A"].owners[-1].failed
    assert {run: _rows(db, run) for run in ("A", "B")} == before
    assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0


def test_exhausted_budget_gates_only_the_selected_run_without_allocating_or_charging(scenario):
    db, kernels = scenario
    db.execute("UPDATE budgets SET developments_used=development_limit WHERE run_id='A'")
    before_b = _rows(db, "B")
    result = _allocate(kernels["A"], "A")
    assert (result.kind, result.reason) == ("paused", "development_budget")
    assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == "modification_budget_exhausted"
    assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    assert _rows(db, "B") == before_b


def test_same_feedback_identity_in_sibling_history_does_not_suppress_or_supply_current_feedback(scenario):
    db, kernels = scenario
    for run in ("A", "B"):
        with kernels[run].storage.transaction() as tx:
            payload = tx.put(SUBJECT, "control_intent", wire_json({"message": "original " + run, "new_thread": False}))
            receipt = tx.put(SUBJECT, "control_receipt", wire_json({"feedback_scope": {
                SUBJECT: {"generation": 1, "attempt": None, "role": "development"}}}))
            tx.execute("INSERT INTO actions(id,run_id,kind,payload_id,status,receipt_id) VALUES (?,?, 'resume',?,'applied',?)",
                ("same-action", run, payload, receipt))
            kernels[run]._event(tx, SUBJECT, "run_feedback_pending", "same-action", payload)
            if run == "B":
                kernels[run]._event(tx, SUBJECT, "run_feedback_bound", "same-action", receipt)
    before_b = _rows(db, "B")
    result = _allocate(kernels["A"], "A").preparation
    assert result is not None and result.context["human_responses"] == ["original A"]
    assert db.execute("SELECT count(*) FROM history WHERE run_id='A' AND kind='run_feedback_bound'").fetchone()[0] == 1
    assert _rows(db, "B") == before_b


def test_review_requirement_bodies_and_fixture_material_remain_in_the_selected_run(scenario):
    db, kernels = scenario
    for run in ("A", "B"):
        insert(db, "source_observations", {"run_id": run, "parent_revision": "r", "graph_revision": "g", "body": b"{}"})
        sequence = db.execute("SELECT sequence FROM source_observations WHERE run_id=?", (run,)).fetchone()[0]
        db.execute("UPDATE runs SET source_sequence=? WHERE id=?", (sequence, run))
        insert(db, "requirement_versions", {"run_id": run, "issue": 1, "revision": "r", "title": "Title " + run, "body": "Body " + run})
        with kernels[run].storage.transaction() as tx:
            tx.put(SUBJECT, "fixture_evidence", wire_json({"original": run}))
        db.execute("UPDATE subjects SET phase='review',validation_checkout=? WHERE run_id=?", ("/validation/" + run, run))
    before_b = _rows(db, "B")
    result = _allocate(kernels["A"], "A").preparation
    assert result is not None and result.role == Role.REVIEW
    assert result.context["requirements"][0]["title"] == "Title A"
    assert result.context["requirements"][0]["body"] == "Body A"
    assert result.context["frozen_fixture_evidence"] == {"original": "A"}
    assert result.context["checkout"] == "/validation/A" and result.context["readonly"] is True
    assert result.context["review_budget_context"]["current_review_attempt"] == 1
    assert _rows(db, "B") == before_b


def test_creation_input_lookup_cannot_be_redirected_to_a_sibling_header(scenario):
    db, kernels = scenario
    before = {run: _rows(db, run) for run in ("A", "B")}
    with kernels["A"].storage.transaction(write=False) as tx:
        foreign = tx.one("SELECT * FROM runs WHERE id='B'")
        with pytest.raises(KernelError, match="creation_input_binding"):
            kernels["A"]._inputs(tx, foreign)
    assert {run: _rows(db, run) for run in ("A", "B")} == before


def test_missing_original_profile_does_not_borrow_a_sibling_revision(scenario):
    db, kernels = scenario
    before = {run: _rows(db, run) for run in ("A", "B")}
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with kernels["A"].storage.transaction() as tx:
            # Root's deferred reference permits observation, but the new Thread
            # FK must refuse the missing A revision despite B's valid revision 1.
            tx.execute("DELETE FROM profile_revisions WHERE run_id='A'")
            with pytest.raises(KernelError, match="missing_or_ambiguous_fact"):
                kernels["A"]._profile_document(tx, 1)
            run, job = kernels["A"]._owned(tx, SUBJECT, _work("A").execution)
            kernels["A"]._allocate(tx, run, job, _work("A"), WORLD)
    assert {run: _rows(db, run) for run in ("A", "B")} == before


def test_prior_review_reference_cannot_fall_back_to_a_sibling_attempt(scenario):
    db, kernels = scenario
    _allocate(kernels["B"], "B")
    before = {run: _rows(db, run) for run in ("A", "B")}
    with pytest.raises(KernelError, match="missing_or_ambiguous_fact"):
        with kernels["A"].storage.transaction() as tx:
            tx.execute("UPDATE subjects SET review_attempt='same-attempt' WHERE run_id='A'")
            run, job = kernels["A"]._owned(tx, SUBJECT, _work("A").execution)
            # The context reader must reject this dangling local reference,
            # even though the identically named B Attempt exists.
            kernels["A"]._context(tx, run, job, WORLD, Role.DEVELOPMENT)
    assert {run: _rows(db, run) for run in ("A", "B")} == before
