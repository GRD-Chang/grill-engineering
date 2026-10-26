"""Run-owned payloads on the inactive schema; no protocol or Core activation."""
from contextlib import closing
from dataclasses import FrozenInstanceError
import sqlite3

import pytest

from agent_run.errors import KernelError
from agent_run.storage import run_scope
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import SCHEMA, Transaction
from support.repository_scope import add_subject, connection, seed


@pytest.fixture
def database():
    with closing(connection()) as db:
        db.execute("BEGIN")
        seed(db, "A")
        seed(db, "B")
        seed(db, "C", subject="only-C")
        db.commit()
        yield db


def test_same_content_and_subject_have_run_owned_identity_without_mutating_reads(database):
    first, second = RunTransaction(Transaction(database), "A"), RunTransaction(Transaction(database), "B")
    raw = b'{"original":true, "bytes":"unchanged"}\n'
    a, b = first.put("parent:1", "raw", raw), second.put("parent:1", "raw", raw)
    assert a != b
    before = database.total_changes
    assert first.get(a, "parent:1") == second.get(b, "parent:1") == raw
    for tx, foreign in ((first, b), (second, a)):
        with pytest.raises(KernelError, match="material_corrupt"):
            tx.get(foreign, "parent:1")
    assert database.total_changes == before
    assert database.execute("SELECT count(*) FROM material WHERE purpose='raw'").fetchone()[0] == 2


def test_selection_is_exact_and_immutable_and_does_not_read_a_sibling_header(database):
    scope = RunTransaction(Transaction(database), "A")
    assert scope.scope.run_id == "A" and scope.scope.repository == "example/project"
    with pytest.raises(FrozenInstanceError):
        scope.scope.run_id = "B"
    with pytest.raises(AttributeError):
        scope.run_id = "B"
    before = database.total_changes
    for run in ("missing", "a"):
        with pytest.raises(KernelError, match="run_scope_missing"):
            RunTransaction(Transaction(database), run)
    assert database.total_changes == before


@pytest.mark.parametrize("run", [None, True, 1, ""])
def test_invalid_scope_is_not_coerced_or_guessed(database, run):
    with pytest.raises(KernelError, match="run_scope_invalid"):
        RunTransaction(Transaction(database), run)


@pytest.mark.parametrize("field,value", [("repository", "foreign/project"), ("parent_issue", 99),
                                        ("repository_root", "/different/owned")])
def test_retargeted_header_cannot_reuse_a_bound_material_scope(database, field, value):
    tx = RunTransaction(Transaction(database), "A")
    identity = tx.put("parent:1", "raw", b"original")
    database.execute("BEGIN")
    try:
        database.execute(f"UPDATE runs SET {field}=? WHERE id='A'", (value,))
        for operation in (lambda: tx.get(identity, "parent:1"), lambda: tx.put("parent:1", "raw", b"late")):
            with pytest.raises(KernelError, match="run_scope_changed"):
                operation()
    finally:
        database.rollback()
    assert tx.get(identity, "parent:1") == b"original"


def test_foreign_subject_insert_is_rejected_and_failed_transaction_remains_poisoned(database):
    tx = RunTransaction(Transaction(database), "A")
    database.execute("BEGIN")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            tx.put("only-C", "raw", b"foreign")
        assert tx.failed is True
    finally:
        database.rollback()
    assert database.execute("SELECT count(*) FROM material WHERE purpose='raw'").fetchone()[0] == 0


def test_failed_batch_does_not_persist_a_material_or_change_the_other_run(database):
    original = RunTransaction(Transaction(database), "B").put("parent:1", "raw", b"B original")
    database.execute("BEGIN")
    try:
        candidate = RunTransaction(Transaction(database), "A").put("parent:1", "raw", b"A pending")
        raise ValueError("controlled transaction rollback")
    except ValueError:
        database.rollback()
    with pytest.raises(KernelError, match="material_corrupt"):
        RunTransaction(Transaction(database), "A").get(candidate, "parent:1")
    assert RunTransaction(Transaction(database), "B").get(original, "parent:1") == b"B original"


def test_foreign_body_does_not_consume_the_selected_runs_material_read_budget(database, monkeypatch):
    a = RunTransaction(Transaction(database), "A").put("parent:1", "raw", b"small")
    b = RunTransaction(Transaction(database), "B").put("parent:1", "raw", b"large" * 1000)
    monkeypatch.setattr(run_scope, "MATERIAL_LIMIT", 128)
    tx = RunTransaction(Transaction(database), "A")
    assert tx.get(a, "parent:1") == b"small"
    with pytest.raises(KernelError, match="material_corrupt"):
        tx.get(b, "parent:1")
    assert tx.bytes_read < 1000


@pytest.mark.parametrize("damage", ["body", "size", "digest", "purpose", "subject"])
def test_corrupt_material_never_becomes_a_valid_payload(database, damage):
    tx = RunTransaction(Transaction(database), "A")
    identity = tx.put("parent:1", "raw", b"original")
    fields = {"body": b"replaced", "size": 2, "digest": "0" * 64, "purpose": "other", "subject": "only-C"}
    database.execute("BEGIN")
    try:
        if damage == "subject":
            # Use a valid second A-owned subject; corruption must fail even
            # though the relation itself is legal in the selected Run.
            add_subject(database, "A", "parent:1-other")
            fields["subject"] = "parent:1-other"
        database.execute(f"UPDATE material SET {damage}=? WHERE run_id='A' AND id=?", (fields[damage], identity))
        with pytest.raises(KernelError, match="material_corrupt"):
            tx.get(identity, "parent:1")
    finally:
        database.rollback()
    assert tx.get(identity, "parent:1") == b"original"


def test_current_unscoped_schema_is_refused_without_interpreting_or_writing_it():
    with closing(sqlite3.connect(":memory:")) as database:
        database.row_factory = sqlite3.Row
        database.executescript(SCHEMA)
        before = database.total_changes
        with pytest.raises(KernelError, match="run_scope_schema_required"):
            RunTransaction(Transaction(database), "old-or-unknown")
        assert database.total_changes == before


def test_material_identity_does_not_alias_delimiter_bytes_between_scope_fields(database):
    database.execute("BEGIN")
    seed(database, "X\0Y", subject="Z")
    seed(database, "X", subject="Y\0Z")
    database.commit()
    first, second = RunTransaction(Transaction(database), "X\0Y"), RunTransaction(Transaction(database), "X")
    a, b = first.put("Z", "raw", b"original"), second.put("Y\0Z", "raw", b"original")
    assert a != b
    with pytest.raises(KernelError, match="material_corrupt"):
        first.get(b, "Z")
    assert first.get(a, "Z") == second.get(b, "Y\0Z") == b"original"


@pytest.mark.parametrize("identity", [None, "Authorization: secret-fixture", "A" * 64, "f" * 65, 1, b"id"])
def test_malformed_reference_is_rejected_without_echoing_arbitrary_text(database, identity):
    with pytest.raises(KernelError) as error:
        RunTransaction(Transaction(database), "A").get(identity, "parent:1")
    assert error.value.code == "material_corrupt"
    assert "secret-fixture" not in str(error.value)


def test_reopened_readonly_file_keeps_both_run_scopes_and_original_bytes(tmp_path):
    path = tmp_path / "staged-scope.sqlite3"
    with closing(connection(path)) as database:
        assert database.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        database.execute("BEGIN")
        seed(database, "A")
        seed(database, "B")
        database.commit()
        identities = {run: RunTransaction(Transaction(database), run).put("parent:1", "raw", run.encode()) for run in ("A", "B")}
    before = path.read_bytes()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as reopened:
        reopened.row_factory = sqlite3.Row
        reopened.execute("PRAGMA query_only=ON")
        reopened.execute("PRAGMA trusted_schema=OFF")
        for run in ("A", "B"):
            tx = RunTransaction(Transaction(reopened), run)
            assert tx.get(identities[run], "parent:1") == run.encode()
            with pytest.raises(KernelError, match="material_corrupt"):
                tx.get(identities["B" if run == "A" else "A"], "parent:1")
    assert path.read_bytes() == before
