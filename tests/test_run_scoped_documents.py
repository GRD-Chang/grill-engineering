"""Inactive native query projection on one SQLite snapshot, no public activation."""
from contextlib import closing

import pytest

from agent_run.contracts.agent_profiles import resolve_profiles
from agent_run.model import wire_json
from agent_run.queries import run as query
from agent_run.queries.execution import activity, execution_facts, worker_activity_start
from agent_run.queries.resume_facts import resume_facts
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.repository_scope import connection, insert, seed
from support.environment import durable_snapshot


SUBJECT = "parent:1"


@pytest.fixture
def database():
    with closing(connection()) as db:
        db.execute("BEGIN")
        for run in ("A", "B"):
            seed(db, run)
            tx = RunTransaction(Transaction(db), run)
            profile = tx.put(SUBJECT, "profile", wire_json(resolve_profiles(overrides={"development_model": "model-" + run})))
            db.execute("UPDATE profile_revisions SET material_id=? WHERE run_id=?", (profile, run))
            insert(db, "threads", {"run_id": run, "id": "same-thread", "subject": SUBJECT,
                "role": "development", "profile_revision": 1, "backend_id": "physical-" + run})
            material = tx.put(SUBJECT, "raw", wire_json({"original": run}))
            insert(db, "history", {"run_id": run, "subject": SUBJECT, "kind": "note", "identity": "same-note",
                "material_id": material, "generation": 1, "created_at": 100.})
        db.commit()
        yield db


def test_settings_thread_profile_and_frozen_defaults_never_include_a_sibling(database):
    before = database.total_changes
    for run in ("A", "B"):
        header = dict(database.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone())
        document = query._frozen_settings(RunTransaction(Transaction(database), run), header)
        binding, = document["bindings"]
        assert binding["thread_identity"] == "same-thread" and binding["thread_id"] == "physical-" + run
        assert binding["model"] == "model-" + run
        assert document["run_id"] == run and document["source"] == "run_snapshot"
    assert database.total_changes == before


def test_history_page_and_has_more_use_only_the_selected_runs_records(database):
    before = database.total_changes
    owners = [{"subject": SUBJECT, "kind": "parent_only"}]
    for run, position in (("A", 1), ("B", 2)):
        tx = RunTransaction(Transaction(database), run)
        page = query._native_history(tx, owners, after=0, limit=1)
        event, = page["events"]
        assert event["evidence"] == {"original": run} and event["position"] == position
        assert page["next_after"] == position and page["has_more"] is False
        assert page["attempts"] == page["actions"] == page["effects"] == []
        assert query._native_history(tx, owners, after=position, limit=1)["events"] == []
    assert database.total_changes == before


def _document_facts(db):
    for run, used in (("A", 1), ("B", 4)):
        tx = RunTransaction(Transaction(db), run)
        insert(db, "requirement_versions", {"run_id": run, "issue": 1, "revision": "r",
            "title": "original title " + run, "body": "original body " + run})
        insert(db, "budgets", {"run_id": run, "subject": SUBJECT, "purpose": "requirements", "window": 1,
            "developments_used": used, "development_limit": 5, "review_required": 0})
        insert(db, "resync_allowances", {"run_id": run, "subject": SUBJECT, "generation": 1,
            "used": used, "allowance": 5})
        inputs = tx.put(SUBJECT, "run_inputs", wire_json({"notifications": {"enabled": False, "original": run}}))
        insert(db, "run_inputs", {"run_id": run, "subject": SUBJECT, "material_id": inputs})
        insert(db, "work_queue", {"run_id": run, "subject": SUBJECT, "ready": 1})


@pytest.mark.parametrize("kind", ["status", "history", "settings"])
def test_complete_snapshot_projects_exact_run_without_mutation_or_new_opening(database, tmp_path, kind):
    _document_facts(database)
    before = database.total_changes
    files = durable_snapshot(tmp_path)
    paths = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    database.execute("BEGIN")
    try:
        for run, used in (("A", 1), ("B", 4)):
            document = query.read_snapshot(RunTransaction(Transaction(database), run), tmp_path / "kernel.sqlite3", kind,
                expected_run=run, expected_repository="example/project", limit=1)
            assert document.kind == kind and document.data["run_id"] == run
            if kind == "settings":
                binding, = document.data["bindings"]
                assert binding["model"] == "model-" + run
            else:
                assert document.data["parent"]["body"] == "original body " + run
                job, = document.data["subjects"]
                budget, = job["budgets"]
                assert budget["developments_used"] == used and job["integration_resync"]["used"] == used
                assert document.data["notifications"]["configuration"]["original"] == run
                assert document.data["scheduling"]["work_fifo"] == [SUBJECT]
                if kind == "history":
                    assert document.data["history"]["has_more"] is False
                    assert document.data["history"]["events"][0]["evidence"] == {"original": run}
            assert database.in_transaction
    finally:
        database.rollback()
    assert database.total_changes == before and durable_snapshot(tmp_path) == files
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == paths


def test_snapshot_expected_identity_and_page_bounds_fail_without_sibling_fallback(database, tmp_path):
    tx = RunTransaction(Transaction(database), "A")
    with pytest.raises(query.QueryError, match="active database snapshot"):
        query.read_snapshot(tx, tmp_path / "kernel.sqlite3", "status")
    database.execute("BEGIN")
    try:
        _reject_snapshot_identity(tx, tmp_path)
    finally:
        database.rollback()


def _reject_snapshot_identity(tx, tmp_path):
    with pytest.raises(query.QueryError, match="Run identity"):
        query.read_snapshot(tx, tmp_path / "kernel.sqlite3", "status", expected_run="B")
    with pytest.raises(query.QueryError, match="Run identity"):
        query.read_snapshot(tx, tmp_path / "kernel.sqlite3", "settings", expected_repository="foreign/project")
    for kwargs in ({"after": -1}, {"limit": 0}, {"limit": 129}):
        with pytest.raises(query.QueryError, match="History requires"):
            query.read_snapshot(tx, tmp_path / "kernel.sqlite3", "history", **kwargs)


def _resume(db, run):
    tx = RunTransaction(Transaction(db), run)
    for kind, identity, stamp, material in (("paused", "human_blocker", 105., None),):
        insert(db, "history", {"run_id": run, "subject": SUBJECT, "kind": kind, "identity": identity,
            "generation": 1, "created_at": stamp, "material_id": material})
    payload = tx.put(SUBJECT, "control", wire_json({"original": run}))
    receipt = tx.put(SUBJECT, "receipt", wire_json({"applied": True, "original": run}))
    insert(db, "actions", {"run_id": run, "id": "same-resume", "kind": "resume", "subject": SUBJECT,
        "payload_id": payload, "receipt_id": receipt, "status": "complete"})
    insert(db, "history", {"run_id": run, "subject": SUBJECT, "kind": "control_applied", "identity": "same-resume",
        "material_id": receipt, "generation": 1, "created_at": 110.})
    evidence = tx.put(SUBJECT, "failure", wire_json({"original": run}))
    insert(db, "history", {"run_id": run, "subject": SUBJECT, "kind": "paused", "identity": "worker_unavailable",
        "material_id": evidence, "generation": 1, "created_at": 115.})
    return db.execute("SELECT * FROM history WHERE run_id=? ORDER BY position DESC LIMIT 1", (run,)).fetchone()


def test_same_action_ids_keep_original_receipt_and_progress_interval(database):
    rows = {run: _resume(database, run) for run in ("A", "B")}
    before = database.total_changes
    for run in ("A", "B"):
        latest, = query._control_history(RunTransaction(Transaction(database), run))
        assert latest["payload"] == {"original": run}
        assert latest["receipt"] == {"applied": True, "original": run}
        assert latest["resume"]["applied"] is True
        facts = latest["resume"]["progress"]
        assert facts["source_position"] == rows[run]["position"]
        assert facts["source_material_id"] == rows[run]["material_id"]
        assert facts["resume_outcome"] == "failed" and facts["reason_code"] == "worker_unavailable"
    with pytest.raises(ValueError, match="different Run"):
        resume_facts(RunTransaction(Transaction(database), "A"), dict(rows["B"]))
    assert database.total_changes == before


def _invocation(db, run):
    tx = RunTransaction(Transaction(db), run)
    context = tx.put(SUBJECT, "role_context", wire_json({"original": run}))
    raw = tx.put(SUBJECT, "raw", wire_json({"original raw": run}))
    process = tx.put(SUBJECT, "process", wire_json({"original identity": run}))
    insert(db, "attempts", {"run_id": run, "id": "same-attempt", "subject": SUBJECT, "generation": 1,
        "ordinal": 1, "role": "development", "purpose": "requirements", "window": 1, "parent_revision": "r",
        "graph_revision": "g", "revision": "r", "base_sha": "a" * 40, "completion_revision": "no-tickets",
        "context_id": context, "status": "running", "thread_id": "same-thread"})
    insert(db, "invocations", {"run_id": run, "id": "same-invocation", "attempt": "same-attempt",
        "thread": "same-thread", "executor": "owner-" + run, "epoch": 1, "status": "returned",
        "allocated_at": 100., "deadline_seconds": 10., "deadline_at": 110.})
    end = 103. if run == "A" else 105.
    insert(db, "invocation_outputs", {"run_id": run, "invocation": "same-invocation", "ordinal": 0,
        "context_id": context, "lease": "physical-lease-" + run, "raw_id": raw, "dispatch_state": "exited",
        "process_identity": process, "exit_code": 0, "allocated_at": 100., "started_at": 101., "exited_at": end,
        "mode": "start"})
    insert(db, "history", {"run_id": run, "subject": SUBJECT, "kind": "agent_allocated", "identity": "same-invocation",
        "generation": 1, "created_at": 100.})
    for active, stamp in ((1, 101.), (0, end)):
        evidence = tx.put(SUBJECT, "activity", wire_json({"original": run, "active": bool(active)}))
        insert(db, "worker_activity", {"run_id": run, "invocation": "same-invocation", "ordinal": 0,
            "identity": "same-activity-" + str(active), "active": active, "observed_at": stamp, "evidence": evidence})
        if active:
            insert(db, "history", {"run_id": run, "subject": SUBJECT, "kind": "worker_active", "identity": "same-invocation:0",
                "material_id": evidence, "generation": 1, "created_at": stamp})


def test_same_invocation_output_and_activity_ids_preserve_original_run_and_profile(database):
    for run in ("A", "B"):
        _invocation(database, run)
    before = database.total_changes
    for run, seconds in (("A", 2.), ("B", 4.)):
        tx = RunTransaction(Transaction(database), run)
        segment = activity(tx, "same-invocation", 0, SUBJECT)
        assert segment["elapsed_seconds"] == seconds
        assert all(item["evidence"]["original"] == run for item in segment["events"])
        first = worker_activity_start(tx, SUBJECT)
        assert first["run_id"] == run and first["invocation_id"] == "same-invocation"
        row = {"run_id": run, "position": 9999, "subject": SUBJECT, "created_at": 200.}
        facts = execution_facts(tx, row, whole_run=True)
        assert facts["execution_seconds"] == seconds
        assert facts["effort"]["development"]["rounds"] == 1
        assert facts["effort"]["development"]["configurations"][0]["model"] == "model-" + run
        invocation, = query._invocations(tx, SUBJECT)
        assert invocation["model"] == "model-" + run and invocation["live_status"] == "unknown"
        output, = invocation["outputs"]
        assert output["raw_return"] == {"original raw": run} and output["elapsed_seconds"] == seconds
    for row in ({"run_id": "B"}, {}):
        with pytest.raises(ValueError, match="different Run"):
            execution_facts(RunTransaction(Transaction(database), "A"), row)
    assert database.total_changes == before


@pytest.mark.parametrize("sibling", ["activity-marker", "control-interval"])
def test_historical_binding_anchor_is_not_suppressed_by_a_sibling_run(database, sibling):
    for run in ("A", "B"):
        _invocation(database, run)
    database.execute("DELETE FROM history WHERE run_id='A' AND kind='worker_active'")
    process = database.execute("SELECT process_identity FROM invocation_outputs WHERE run_id='A'").fetchone()[0]
    insert(database, "history", {"run_id": "A", "subject": SUBJECT, "kind": "worker_started",
        "identity": "same-invocation", "generation": 1, "created_at": 100.5, "material_id": process,
        "status": wire_json({"stopped": False, "blocked": None, "gates": []}).decode()})
    if sibling == "control-interval":
        database.execute("DELETE FROM history WHERE run_id='B' AND kind='worker_active'")
        insert(database, "actions", {"run_id": "B", "id": "foreign-control", "kind": "stop", "status": "complete"})
        insert(database, "history", {"run_id": "B", "subject": SUBJECT, "kind": "control_applied",
            "identity": "foreign-control", "generation": 1, "created_at": 100.7})
    before = database.total_changes
    first = worker_activity_start(RunTransaction(Transaction(database), "A"), SUBJECT)
    assert first is not None and first["run_id"] == "A"
    assert first["started_at"] == 101. and first["invocation_id"] == "same-invocation"
    assert database.total_changes == before
