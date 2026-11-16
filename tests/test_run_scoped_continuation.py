"""Inactive relational consumer seam, not a Core/Worker lifecycle producer."""
from contextlib import closing

import pytest

from agent_run.core.attempt_continuation import (
    AttemptContinuationBasis, bind_attempt_continuation, load_continuation,
)
from agent_run.errors import KernelError
from agent_run.model import wire_json
from agent_run.queries.execution import recorded_profile
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.repository_scope import connection, insert, seed


SUBJECT = "parent:1"


def _attempt(db, run, *, identity="attempt", thread="thread"):
    """Persist only the exact record shapes consumed by continuation policy."""
    tx = RunTransaction(Transaction(db), run)
    profile = tx.put(SUBJECT, "profile", wire_json({"profiles": {"development": {"model": run}}}))
    db.execute("UPDATE profile_revisions SET material_id=? WHERE run_id=? AND id=1", (profile, run))
    insert(db, "threads", {"run_id": run, "id": thread, "subject": SUBJECT,
        "role": "development", "profile_revision": 1})
    facts = {"generation": 1, "role": "development", "purpose": "requirements", "revision": "r",
        "parent_revision": "r", "graph_revision": "g", "completion_revision": "no-tickets",
        "base_sha": "a" * 40, "candidate_sha": None, "candidate_tree": None, "expected_tree": None}
    context = tx.put(SUBJECT, "input", wire_json(facts | {"repository": "example/project", "parent_issue": 1,
        "issue": 1, "scope": "parent_only", "checkout": "/private/owned/" + run,
        "readonly": False, "human_responses": ["original " + run]}))
    insert(db, "attempts", facts | {"run_id": run, "id": identity, "subject": SUBJECT, "ordinal": 1,
        "window": 1, "context_id": context, "status": "allocated"})
    return AttemptContinuationBasis(thread, context)


@pytest.fixture
def database():
    with closing(connection()) as db:
        db.execute("BEGIN")
        for run in ("A", "B"):
            seed(db, run)
        db.commit()
        yield db


def _output(db, run, identity, basis, *, predecessor=None, recovery=None, ordinal=0):
    tx = RunTransaction(Transaction(db), run)
    raw = tx.put(SUBJECT, "raw", (run + " original raw\r\n").encode())
    insert(db, "invocations", {"run_id": run, "id": identity, "attempt": "attempt", "thread": "thread",
        "executor": "owner-" + run, "epoch": 1, "status": "failed", "allocated_at": 100.,
        "deadline_seconds": 7., "deadline_at": 107. if run == "A" else 207.,
        "predecessor": predecessor, "recovery_kind": recovery})
    insert(db, "invocation_outputs", {"run_id": run, "invocation": identity, "ordinal": ordinal,
        "context_id": basis.context_id, "lease": run + "-" + identity, "raw_id": raw,
        "dispatch_state": "exited", "exit_code": 23 if run == "A" else 24,
        "allocated_at": 100., "mode": "start", "failure_kind": "ordinary"})
    db.execute("UPDATE attempts SET current_invocation=? WHERE run_id=? AND id='attempt'", (identity, run))
    return raw


def test_binding_and_loading_same_ids_do_not_use_or_modify_the_sibling_run(database):
    bases = {run: _attempt(database, run) for run in ("A", "B")}
    sibling = tuple(database.execute("SELECT * FROM attempts WHERE run_id='B'").fetchone())
    a = RunTransaction(Transaction(database), "A")
    bind_attempt_continuation(a, "attempt", bases["A"])
    changes = database.total_changes
    bind_attempt_continuation(a, "attempt", bases["A"])
    assert database.total_changes == changes
    assert tuple(database.execute("SELECT * FROM attempts WHERE run_id='B'").fetchone()) == sibling
    assert database.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0
    assert database.execute("SELECT count(*) FROM leases").fetchone()[0] == 0
    bind_attempt_continuation(RunTransaction(Transaction(database), "B"), "attempt", bases["B"])
    for run in ("A", "B"):
        facts = load_continuation(RunTransaction(Transaction(database), run), "attempt")
        assert facts.context["human_responses"] == ["original " + run]
        assert facts.prior is None and facts.output_ordinal == 0
        assert (facts.ordinary_used, facts.capacity_count, facts.semantic_used) == (0, 0, 0)
        assert recorded_profile(RunTransaction(Transaction(database), run), 1)["profiles"]["development"]["model"] == run
    assert database.execute("PRAGMA foreign_key_check").fetchone() is None


def test_lineage_outputs_counts_and_original_raw_stay_with_the_selected_run(database):
    bases = {run: _attempt(database, run) for run in ("A", "B")}
    for run in ("A", "B"):
        bind_attempt_continuation(RunTransaction(Transaction(database), run), "attempt", bases[run])
        _output(database, run, "first", bases[run])
    raw_a = _output(database, "A", "second", bases["A"], predecessor="first", recovery="ordinary", ordinal=1)
    raw_b = _output(database, "B", "second", bases["B"], predecessor="first", recovery="capacity", ordinal=2)
    before = database.total_changes
    for run, ordinal, counts, raw, code, deadline in (
        ("A", 1, (1, 0), raw_a, 23, 107.), ("B", 2, (0, 1), raw_b, 24, 207.),
    ):
        tx = RunTransaction(Transaction(database), run)
        facts = load_continuation(tx, "attempt")
        assert facts.prior is not None and facts.prior.invocation_id == "second"
        assert (facts.prior.ordinal, facts.output_ordinal) == (ordinal, ordinal)
        assert (facts.ordinary_used, facts.capacity_count) == counts
        assert (facts.prior.exit_code, facts.prior.deadline_at, facts.prior.deadline_seconds) == (code, deadline, 7.)
        assert tx.get(facts.prior.raw_id, SUBJECT) == (run + " original raw\r\n").encode()
        assert facts.prior.raw_id == raw and facts.semantic_deadline is None
    assert database.total_changes == before


def test_sibling_invocation_does_not_prevent_an_unstarted_attempt_binding(database):
    bases = {run: _attempt(database, run) for run in ("A", "B")}
    bind_attempt_continuation(RunTransaction(Transaction(database), "B"), "attempt", bases["B"])
    _output(database, "B", "first", bases["B"])
    bind_attempt_continuation(RunTransaction(Transaction(database), "A"), "attempt", bases["A"])
    facts = load_continuation(RunTransaction(Transaction(database), "A"), "attempt")
    assert facts.prior is None and facts.output_ordinal == 0
    assert (facts.ordinary_used, facts.capacity_count) == (0, 0)
    assert database.execute("SELECT count(*) FROM invocations WHERE run_id='A'").fetchone()[0] == 0


def test_missing_output_cannot_use_same_named_sibling_output(database):
    for run in ("A", "B"):
        basis = _attempt(database, run)
        bind_attempt_continuation(RunTransaction(Transaction(database), run), "attempt", basis)
        _output(database, run, "first", basis)
    database.execute("DELETE FROM invocation_outputs WHERE run_id='A'")
    before = database.total_changes
    with pytest.raises(KernelError, match="missing_or_ambiguous_fact"):
        load_continuation(RunTransaction(Transaction(database), "A"), "attempt")
    assert database.total_changes == before
    assert load_continuation(RunTransaction(Transaction(database), "B"), "attempt").prior is not None


def test_failed_binding_transaction_rolls_back_only_its_own_material_and_binding(database):
    bases = {run: _attempt(database, run) for run in ("A", "B")}
    bind_attempt_continuation(RunTransaction(Transaction(database), "B"), "attempt", bases["B"])
    sibling = tuple(database.execute("SELECT * FROM attempts WHERE run_id='B'").fetchone())
    database.execute("BEGIN")
    try:
        bind_attempt_continuation(RunTransaction(Transaction(database), "A"), "attempt", bases["A"])
        raise ValueError("controlled caller rollback")
    except ValueError:
        database.rollback()
    assert tuple(database.execute("SELECT * FROM attempts WHERE run_id='B'").fetchone()) == sibling
    assert database.execute("SELECT thread_id,opening_id FROM attempts WHERE run_id='A'").fetchone()[:] == (None, None)
    assert database.execute("SELECT count(*) FROM material WHERE run_id='A' AND purpose='attempt_continuation_opening'").fetchone()[0] == 0


@pytest.mark.parametrize("foreign", ["attempt", "thread", "context", "profile"])
def test_missing_selected_record_never_falls_back_to_a_sibling(database, foreign):
    b = _attempt(database, "B", identity="foreign-attempt", thread="foreign-thread")
    a = _attempt(database, "A")
    if foreign == "profile":
        material = database.execute("SELECT material_id FROM profile_revisions WHERE run_id='B'").fetchone()[0]
        insert(database, "profile_revisions", {"run_id": "B", "id": 2, "subject": SUBJECT,
            "material_id": material, "created_at": 100.})
    before = database.total_changes
    with pytest.raises(KernelError):
        if foreign == "attempt":
            bind_attempt_continuation(RunTransaction(Transaction(database), "A"), "foreign-attempt", b)
        elif foreign == "thread":
            bind_attempt_continuation(RunTransaction(Transaction(database), "A"), "attempt",
                                      AttemptContinuationBasis("foreign-thread", a.context_id))
        elif foreign == "context":
            bind_attempt_continuation(RunTransaction(Transaction(database), "A"), "attempt",
                                      AttemptContinuationBasis("thread", b.context_id))
        else:
            recorded_profile(RunTransaction(Transaction(database), "A"), 2)
    assert database.total_changes == before
    assert database.execute("SELECT count(*) FROM material WHERE purpose='attempt_continuation_opening'").fetchone()[0] == 0


def test_unscoped_or_unknown_transaction_cannot_choose_repository_records(database):
    class UnknownTransaction(Transaction):
        pass
    _attempt(database, "A")
    for tx in (Transaction(database), UnknownTransaction(database)):
        with pytest.raises(KernelError, match="run_scope_required"):
            recorded_profile(tx, 1)


def test_retargeted_scope_cannot_reuse_an_existing_consumer(database):
    basis = _attempt(database, "A")
    tx = RunTransaction(Transaction(database), "A")
    database.execute("UPDATE runs SET parent_issue=99 WHERE id='A'")
    before = database.total_changes
    with pytest.raises(KernelError, match="run_scope_changed"):
        bind_attempt_continuation(tx, "attempt", basis)
    assert database.total_changes == before


@pytest.mark.parametrize('corruption,code', [
    ('thread_role', 'continuation_binding'),
    ('profile_hash', 'material_corrupt'),
    ('profile_size', 'material_corrupt'),
    ('foreign_profile', 'missing_or_ambiguous_fact'),
    ('context', 'continuation_input_changed'),
    ('cycle', 'continuation_lineage'),
    ('orphan', 'continuation_lineage'),
])
def test_load_rereads_after_related_dml_without_reusing_checked_facts(database, corruption, code):
    bases = {run: _attempt(database, run) for run in ('A', 'B')}
    for run, basis in bases.items():
        bind_attempt_continuation(RunTransaction(Transaction(database), run), 'attempt', basis)
        _output(database, run, 'first', basis)
    profile = database.execute("SELECT material_id FROM profile_revisions WHERE run_id='B' AND id=1").fetchone()[0]
    insert(database, 'profile_revisions', {'run_id': 'B', 'id': 2, 'subject': SUBJECT,
        'material_id': profile, 'created_at': 100.})
    database.execute('BEGIN')
    try:
        tx = RunTransaction(Transaction(database), 'A')
        first = load_continuation(tx, 'attempt')
        assert first.prior.invocation_id == 'first'
        if corruption == 'thread_role':
            database.execute("UPDATE threads SET role='publication' WHERE run_id='A'")
        elif corruption in {'profile_hash', 'profile_size'}:
            material = database.execute("SELECT material_id FROM profile_revisions WHERE run_id='A' AND id=1").fetchone()[0]
            body = tx.get(material, SUBJECT)
            damaged = b'x' * len(body) if corruption == 'profile_hash' else body[:-1]
            database.execute("UPDATE material SET body=? WHERE run_id='A' AND id=?", (damaged, material))
        elif corruption == 'foreign_profile':
            # A deliberately invalid input stays in this rolled-back unit
            # transaction. Foreign keys remain enabled; never commit it.
            database.execute('PRAGMA defer_foreign_keys=ON')
            database.execute("UPDATE threads SET profile_revision=2 WHERE run_id='A'")
        elif corruption == 'context':
            import json
            body = json.loads(tx.get(bases['A'].context_id, SUBJECT))
            material = tx.put(SUBJECT, 'changed-context', wire_json({**body, 'revision': 'changed'}))
            database.execute("UPDATE attempts SET context_id=? WHERE run_id='A'", (material,))
        elif corruption == 'cycle':
            database.execute("UPDATE invocations SET predecessor=id WHERE run_id='A'")
        else:
            _output(database, 'A', 'second', bases['A'])
        with pytest.raises(KernelError, match=code):
            load_continuation(tx, 'attempt')
    finally:
        database.rollback()
    assert load_continuation(RunTransaction(Transaction(database), 'A'), 'attempt').prior.invocation_id == 'first'
    assert load_continuation(RunTransaction(Transaction(database), 'B'), 'attempt').prior.invocation_id == 'first'


def test_lineage_still_validates_a_different_historical_thread(database):
    basis = _attempt(database, 'A')
    bind_attempt_continuation(RunTransaction(Transaction(database), 'A'), 'attempt', basis)
    _output(database, 'A', 'first', basis)
    insert(database, 'threads', {'run_id': 'A', 'id': 'new-thread', 'subject': SUBJECT,
        'role': 'development', 'profile_revision': 1})
    _output(database, 'A', 'second', basis, predecessor='first', recovery='explicit')
    database.execute("UPDATE invocations SET thread='new-thread' WHERE run_id='A' AND id='second'")
    database.execute("UPDATE attempts SET thread_id='new-thread' WHERE run_id='A'")
    assert load_continuation(RunTransaction(Transaction(database), 'A'), 'attempt').thread_id == 'new-thread'
    database.execute("UPDATE threads SET role='publication' WHERE run_id='A' AND id='thread'")
    with pytest.raises(KernelError, match='continuation_binding'):
        load_continuation(RunTransaction(Transaction(database), 'A'), 'attempt')


@pytest.mark.parametrize('failure', ['budget', 'sql'])
def test_subsequent_load_retains_original_owner_budget_and_poison(database, failure):
    import sqlite3
    from agent_run.storage.sqlite import QUERY_LIMIT

    basis = _attempt(database, 'A')
    bind_attempt_continuation(RunTransaction(Transaction(database), 'A'), 'attempt', basis)
    _output(database, 'A', 'first', basis)
    database.execute('BEGIN')
    owner = Transaction(database)
    tx = RunTransaction(owner, 'A')
    try:
        load_continuation(tx, 'attempt')
        if failure == 'budget':
            tx.bytes_read = QUERY_LIMIT
            assert owner.bytes_read == QUERY_LIMIT
            with pytest.raises(KernelError, match='query_limit'):
                load_continuation(tx, 'attempt')
        else:
            def denied(action, table, column, database_name, source):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'material' else sqlite3.SQLITE_OK
            database.set_authorizer(denied)
            with pytest.raises(sqlite3.DatabaseError):
                load_continuation(tx, 'attempt')
            assert owner.failed and tx.failed
    finally:
        database.set_authorizer(None)
        database.rollback()
