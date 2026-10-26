"""Inactive same-file projection seam; relational records are not Core authority."""
from contextlib import closing

import pytest

from agent_run.errors import KernelError
from agent_run.contracts.agent_profiles import resolve_profiles
from agent_run.model import wire_json
from agent_run.queries.read_facts import QueryError, read_boundaries, read_header, recorded_review, supervision
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.repository_scope import connection, insert, seed


SUBJECT = "parent:1"


@pytest.fixture
def database():
    with closing(connection()) as db:
        db.execute("BEGIN")
        for run in ("A", "B"):
            seed(db, run)
        db.commit()
        yield db


def _gate(db, run, code, detail):
    evidence = RunTransaction(db, run).put(SUBJECT, "gate", wire_json(detail))
    insert(db, "gates", {"run_id": run, "subject": SUBJECT, "code": code, "evidence": evidence})


def _history(db, run, identity):
    insert(db, "history", {"run_id": run, "subject": SUBJECT, "kind": "paused", "identity": identity,
        "generation": 1, "created_at": 100.})


def _wait(db, run, *, padding=""):
    scope = RunTransaction(db, run).put(SUBJECT, "supervision_scope",
        wire_json({"run": run, "subject": SUBJECT, "detail": padding}))
    deadline = 200. if run == "A" else 400.
    insert(db, "waits", {"run_id": run, "subject": SUBJECT, "kind": "required_checks", "scope_id": scope,
        "started_at": 100., "deadline": deadline})
    return scope


def test_header_and_operator_gate_projection_use_only_the_selected_run(database):
    database.execute("UPDATE runs SET stopped=1,language='zh' WHERE id='A'")
    database.execute("UPDATE subjects SET phase='complete' WHERE run_id='B'")
    for run in ("A", "B"):
        _gate(database, run, "human_blocker", {"human_blockers": ["original " + run]})
        _history(database, run, "same-history-identity")
    before = database.total_changes
    a, b = RunTransaction(database, "A"), RunTransaction(database, "B")
    first, second = read_header(a), read_header(b)
    assert (first["run_id"], first["language"], first["is_terminal"]) == ("A", "zh", False)
    assert (second["run_id"], second["language"], second["is_terminal"]) == ("B", "en", True)
    av, bv = read_boundaries(a, 100.), read_boundaries(b, 100.)
    assert av["status"] == "operator_stopped" and bv["status"] == "ready_for_human"
    assert av["subjects"][0]["gates"][0]["detail"]["human_blockers"] == ["original A"]
    assert bv["subjects"][0]["gates"][0]["detail"]["human_blockers"] == ["original B"]
    assert (av["highwater"], bv["highwater"]) == (1, 2)
    assert all(action["action"]["selector"] == "A" for action in av["actions"])
    assert all(action["action"]["selector"] == "B" for action in bv["actions"])
    assert database.total_changes == before


def test_sibling_abandon_action_cannot_fence_the_selected_run(database):
    for run, kind in (("A", "resume"), ("B", "abandon")):
        insert(database, "actions", {"run_id": run, "id": "same-action", "kind": kind,
            "subject": SUBJECT, "status": "complete"})
    before = database.total_changes
    a, b = (read_boundaries(RunTransaction(database, run), 100.) for run in ("A", "B"))
    assert a["abandonment"] is None and a["status"] == "parent_delivery_pending"
    assert b["abandonment"] == {"action": "same-action", "status": "complete"}
    assert b["status"] == "abandoned" and not b["actions"]
    assert database.total_changes == before


def test_supervision_keeps_original_scope_deadline_and_timeout_evidence(database):
    scopes = {run: _wait(database, run) for run in ("A", "B")}
    _gate(database, "B", "supervision_timeout", {"scope_id": scopes["B"], "kind": "required_checks"})
    before = database.total_changes
    a, = supervision(RunTransaction(database, "A"), 120.)
    b, = supervision(RunTransaction(database, "B"), 120.)
    assert (a["scope_id"], a["deadline"], a["remaining_seconds"], a["timed_out"]) == (scopes["A"], 200., 80, False)
    assert (b["scope_id"], b["deadline"], b["remaining_seconds"], b["timed_out"]) == (scopes["B"], 400., 0, True)
    assert a["scope"]["run"] == "A" and b["scope"]["run"] == "B"
    assert "-- A" in a["resume_action"] and "-- B" in b["resume_action"]
    assert database.total_changes == before


def test_global_timeout_history_and_null_subject_wait_cannot_cross_runs(database):
    scopes = {}
    for run in ("A", "B"):
        tx = RunTransaction(database, run)
        scope = tx.put(SUBJECT, "supervision_scope", wire_json({"run": run, "subject": None}))
        scopes[run] = scope
        insert(database, "waits", {"run_id": run, "subject": None, "kind": "github_convergence",
            "scope_id": scope, "started_at": 100., "deadline": 700.})
    tx = RunTransaction(database, "B")
    timeout = tx.put(SUBJECT, "supervision_timeout", wire_json({"scope_id": scopes["B"], "kind": "github_convergence"}))
    insert(database, "history", {"run_id": "B", "subject": SUBJECT, "kind": "paused",
        "identity": "supervision_timeout", "material_id": timeout, "generation": 1, "created_at": 110.})
    database.execute("UPDATE runs SET blocked='supervision_timeout'")
    before = database.total_changes
    first, = supervision(RunTransaction(database, "A"), 120.)
    second, = supervision(RunTransaction(database, "B"), 120.)
    assert first["subject"] is None and first["timeout"] is None and not first["timed_out"]
    assert second["subject"] is None and second["timed_out"]
    assert second["timeout"] == {"scope_id": scopes["B"], "kind": "github_convergence"}
    assert first["deadline"] == second["deadline"] == 700.
    assert database.total_changes == before


def test_sibling_cleanup_and_confirmed_close_are_not_the_selected_runs_receipt(database):
    for run in ("A", "B"):
        tx = RunTransaction(database, run)
        payload = tx.put(SUBJECT, "payload", wire_json({"run": run}))
        receipt = tx.put(SUBJECT, "receipt", wire_json({"original": run}))
        insert(database, "effects", {"run_id": run, "id": "same-effect", "subject": SUBJECT,
            "operation": "close", "status": "confirmed", "parent_revision": "r", "graph_revision": "g",
            "revision": "r", "generation": 1, "source_ref": "source", "target_ref": "target",
            "before_sha": "a" * 40, "base_sha": "a" * 40, "base_tree": "b" * 40,
            "pr_number": 10 if run == "A" else 20, "payload_id": payload, "receipt_id": receipt})
        insert(database, "resource_cleanup", {"run_id": run, "subject": SUBJECT, "generation": 1,
            "source_ref": "branch-" + run, "source_sha": "a" * 40, "checkout": "/owned/" + run,
            "workspace_sha": "a" * 40, "owned_root": "/owned", "common_git_dir": "/owned/.git",
            "outcome": "original " + run})
    before = database.total_changes
    for run, number in (("A", 10), ("B", 20)):
        view = read_boundaries(RunTransaction(database, run), 100.)
        close, = view["closures"]
        assert close["pr_number"] == number and close["effect_id"] == "same-effect"
        assert RunTransaction(database, run).get(close["receipt_id"], SUBJECT) == wire_json({"original": run})
        cleanup, = view["delivery_cleanup"]["items"]
        assert cleanup["branch"] == "branch-" + run and cleanup["last_error"] == "original " + run
        assert "-- " + run in cleanup["recovery_action"]
    assert database.total_changes == before


def test_unselected_wait_body_does_not_exhaust_the_selected_query_budget(database, monkeypatch):
    _wait(database, "A")
    _wait(database, "B", padding="unselected" * 2000)
    import agent_run.storage.sqlite as storage
    monkeypatch.setattr(storage, "QUERY_LIMIT", 8192)
    a = RunTransaction(database, "A")
    assert len(supervision(a, 120.)) == 1 and a.bytes_read < 8192
    with pytest.raises(KernelError, match="query_limit"):
        supervision(RunTransaction(database, "B"), 120.)


def test_unscoped_or_retargeted_read_boundary_fails_before_projection(database):
    with pytest.raises(KernelError, match="run_scope_required"):
        read_boundaries(Transaction(database), 100.)
    tx = RunTransaction(database, "A")
    database.execute("UPDATE runs SET parent_issue=99 WHERE id='A'")
    with pytest.raises(KernelError, match="run_scope_changed"):
        read_boundaries(tx, 100.)


def _bound_attempt(db, run, *, role="development"):
    tx = RunTransaction(db, run)
    profile = resolve_profiles()
    material = tx.put(SUBJECT, "profile", wire_json(profile))
    db.execute("UPDATE profile_revisions SET material_id=? WHERE run_id=?", (material, run))
    context = tx.put(SUBJECT, "role_context", wire_json({"original": run}))
    insert(db, "threads", {"run_id": run, "id": "thread", "subject": SUBJECT,
        "role": role, "profile_revision": 1})
    insert(db, "attempts", {"run_id": run, "id": "attempt", "subject": SUBJECT,
        "generation": 1, "ordinal": 1, "role": role, "purpose": "requirements", "window": 1,
        "parent_revision": "r", "graph_revision": "g", "revision": "r", "base_sha": "a" * 40,
        "candidate_sha": "c" * 40, "candidate_tree": "d" * 40,
        "completion_revision": "no-tickets", "context_id": context, "thread_id": "thread", "status": "allocated"})
    db.execute("UPDATE subjects SET current_attempt='attempt',candidate_sha=?,candidate_tree=? WHERE run_id=?",
               ("c" * 40, "d" * 40, run))
    db.execute("UPDATE runs SET observed_parent_revision='r',observed_graph_revision='g',executor=?,epoch=1 WHERE id=?",
               ("owner-" + run, run))
    return context


def test_review_applicability_uses_selected_candidate_and_rejects_foreign_input_rows(database):
    for run in ("A", "B"):
        _bound_attempt(database, run, role="review")
    database.execute("UPDATE subjects SET candidate_sha=? WHERE run_id='B'", ("e" * 40,))
    facts = {run: (dict(database.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()),
                   dict(database.execute("SELECT * FROM subjects WHERE run_id=?", (run,)).fetchone()))
             for run in ("A", "B")}
    before = database.total_changes
    assert recorded_review(RunTransaction(database, "A"), *facts["A"]) == "pending"
    assert recorded_review(RunTransaction(database, "B"), *facts["B"]) == "stale"
    with pytest.raises(QueryError, match="different Run"):
        recorded_review(RunTransaction(database, "A"), *facts["B"])
    assert database.total_changes == before


@pytest.mark.parametrize("damage", [None, "execution", "thread"])
def test_preparation_bindings_are_run_scoped_without_inventing_a_supervision_window(database, damage):
    contexts = {run: _bound_attempt(database, run) for run in ("A", "B")}
    insert(database, "threads", {"run_id": "B", "id": "foreign-thread", "subject": SUBJECT,
        "role": "development", "profile_revision": 1})
    for run in ("A", "B"):
        scope = {"reservation_id": "same-reservation", "attempt_id": "attempt", "subject": SUBJECT,
            "role": "development", "purpose": "requirements", "thread_id": "thread",
            "execution": {"run_id": run, "owner": "owner-" + run, "epoch": 1}, "deadline_seconds": 7.,
            "mode": "fresh", "reported_thread": None, "output_ordinal": 0, "profile_revision": 1,
            "profile_reference": None, "context_id": contexts[run], "previous_invocation": None,
            "resume_action": None, "new_thread": False, "owned_period": False, "retained_deadline": None,
            "recovery_kind": None}
        if run == "A" and damage == "execution":
            scope["execution"] = {"run_id": "B", "owner": "owner-B", "epoch": 1}
        if run == "A" and damage == "thread":
            scope["thread_id"] = "foreign-thread"
        material = RunTransaction(database, run).put(SUBJECT, "agent_preparation", wire_json(scope))
        insert(database, "waits", {"run_id": run, "subject": SUBJECT, "kind": "agent_preparation",
            "scope_id": material, "started_at": 100., "deadline": 110.})
    before = database.total_changes
    if damage is None:
        assert supervision(RunTransaction(database, "A"), 105.) == []
    else:
        with pytest.raises(QueryError, match="preparation scope"):
            supervision(RunTransaction(database, "A"), 105.)
    assert supervision(RunTransaction(database, "B"), 105.) == []
    assert database.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0
    assert database.execute("SELECT count(*) FROM leases").fetchone()[0] == 0
    assert database.total_changes == before
