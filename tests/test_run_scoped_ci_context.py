"""CI context reads on real Run-owned material and cumulative SQL accounting.

These are projection tests, not public CI/Agent/Publication lifecycle proof.
The Publication tests supply one development-result query response explicitly;
they do not insert a completed Attempt or grant any physical write authority.
"""
from contextlib import contextmanager
import json
import sqlite3

import pytest

from agent_run.core import kernel as core
from agent_run.errors import KernelError
from agent_run.model import Commit, Role, World, wire_json
from agent_run.storage import sqlite as storage
from support.offline import review_result
from support.repository_scope import insert
from support.run_scoped_kernel import effect_runs


SUBJECT = "parent:1"
WORLD = World("r", "g", "r", Commit("a" * 40, "b" * 40))
CI = b'{"head_sha":"original-ci-head","logs":"original logs","jobs":[{"steps":["failed"]}]}'


@pytest.fixture
def scenario(monkeypatch):
    with effect_runs(monkeypatch) as value:
        yield value


def prepare(db, kernel, raw=CI, *, purpose="ci", repair_source="required_checks"):
    with kernel.storage.transaction() as tx:
        run = tx.run_id
        failure = tx.put(SUBJECT, "ci_failure", raw)
        acceptance = tx.put(SUBJECT, "acceptance", wire_json(review_result("fail")))
        development = tx.put(SUBJECT, "projection_test_input", wire_json({
            "result_kind": "development", "summary": "original development", "human_blockers": None}))
        tx.execute("UPDATE subjects SET kind='ticket',purpose=?,repair_source=?,ci_failure=?,last_failure=?,"
                   "gate_mode='ci_waiver' WHERE run_id=? AND id=?",
                   (purpose, repair_source, failure, acceptance, run, SUBJECT))
        insert(db, "budgets", {"run_id": run, "subject": SUBJECT, "purpose": "ci", "window": 1,
            "development_limit": 3, "review_limit": 3, "review_required": 1})
    return failure, development


@contextmanager
def context_read(kernel, monkeypatch, failure, development=None):
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT * FROM runs WHERE id=?", (tx.run_id,))
        job = tx.one("SELECT * FROM subjects WHERE run_id=? AND id=?", (tx.run_id, SUBJECT))
        original_get, original_one = tx.get, tx.one
        reads = []

        def get(identity, subject):
            before = tx.rows_read, tx.bytes_read
            value = original_get(identity, subject)
            if identity == failure:
                reads.append((tx.rows_read - before[0], tx.bytes_read - before[1]))
            return value

        def one(sql, parameters=()):
            if development is not None and sql.startswith("SELECT result_id FROM attempts WHERE"):
                assert parameters == (SUBJECT, job["generation"], tx.run_id)
                return {"result_id": development}
            return original_one(sql, parameters)

        monkeypatch.setattr(tx, "get", get)
        monkeypatch.setattr(tx, "one", one)
        yield tx, run, job, reads


@pytest.mark.parametrize("raw", [
    pytest.param(b"null", id="null"), pytest.param(b"{}", id="empty-object"),
    pytest.param(CI, id="nested-evidence"),
    pytest.param(b" " * (storage.MATERIAL_LIMIT - 4) + b"null", id="maximum-material-null"),
])
def test_ci_development_reads_and_decodes_original_material_once(scenario, monkeypatch, raw):
    db, kernels = scenario
    failure, _ = prepare(db, kernels["A"], raw)
    loads, decoded = core.json.loads, []

    def decode(value, *args, **kwargs):
        if value == raw:
            decoded.append(value)
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(core.json, "loads", decode)
    with context_read(kernels["A"], monkeypatch, failure) as (tx, run, job, reads):
        context = kernels["A"]._context(tx, run, job, WORLD, Role.DEVELOPMENT)
        assert context["ci_evidence"] == loads(raw)
        assert context["work_source"] == "ci" and "acceptance_artifact" not in context
        assert len(reads) == len(decoded) == 1
        assert reads[0][0] > 0 and reads[0][1] >= len(raw)
        assert tx.rows_read > reads[0][0] and tx.bytes_read > reads[0][1]


@pytest.mark.parametrize("raw", [
    pytest.param(b"null", id="null"), pytest.param(b"{}", id="empty-object"),
    pytest.param(CI, id="nested-evidence"),
    pytest.param(b"[" * 600 + b"0" + b"]" * 600, id="deep-array"),
    pytest.param(b" " * (storage.MATERIAL_LIMIT - 4) + b"null", id="maximum-material-null"),
])
def test_ci_publication_reuses_material_bytes_but_not_mutable_json(scenario, monkeypatch, raw):
    db, kernels = scenario
    failure, development = prepare(db, kernels["A"], raw)
    with context_read(kernels["A"], monkeypatch, failure, development) as (tx, run, job, reads):
        context = kernels["A"]._context(tx, run, job, WORLD, Role.PUBLICATION)
        first = context["ci_evidence"]
        second = context["ci_repair_publication_context"]["failed_required_checks"]
        assert first == second == json.loads(raw)
        assert len(reads) == 1
        if isinstance(first, (dict, list)):
            assert first is not second
        if raw == CI:
            first["jobs"][0]["steps"].append("only first")
            assert second["jobs"][0]["steps"] == ["failed"]
            second["jobs"].append({"only": "second"})
            assert len(first["jobs"]) == 1


@pytest.mark.parametrize("role", [Role.DEVELOPMENT, Role.PUBLICATION])
def test_no_initial_ci_projection_keeps_the_original_later_read(scenario, monkeypatch, role):
    db, kernels = scenario
    failure, development = prepare(db, kernels["A"], purpose="requirements")
    with context_read(kernels["A"], monkeypatch, failure,
                      development if role == Role.PUBLICATION else None) as (tx, run, job, reads):
        context = kernels["A"]._context(tx, run, job, WORLD, role)
        assert "work_source" not in context and len(reads) == 1
        if role == Role.DEVELOPMENT:
            assert context["ci_evidence"] == json.loads(CI)
        else:
            assert "ci_evidence" not in context
            assert context["ci_repair_publication_context"]["failed_required_checks"] == json.loads(CI)


def test_non_ci_acceptance_repair_does_not_read_or_gain_ci_evidence(scenario, monkeypatch):
    db, kernels = scenario
    failure, _ = prepare(db, kernels["A"], purpose="requirements", repair_source="acceptance")
    with context_read(kernels["A"], monkeypatch, failure) as (tx, run, job, reads):
        context = kernels["A"]._context(tx, run, job, WORLD, Role.DEVELOPMENT)
        assert context["acceptance_artifact"] == review_result("fail")
        assert "ci_evidence" not in context and "work_source" not in context and reads == []


def test_ci_acceptance_repair_keeps_original_ci_and_findings_separate(scenario, monkeypatch):
    db, kernels = scenario
    failure, _ = prepare(db, kernels["A"], repair_source="acceptance")
    with context_read(kernels["A"], monkeypatch, failure) as (tx, run, job, reads):
        context = kernels["A"]._context(tx, run, job, WORLD, Role.DEVELOPMENT)
        assert context["ci_evidence"] == json.loads(CI) and len(reads) == 1
        assert context["acceptance_artifact"] == review_result("fail")
        assert context["repair_source"] == "acceptance" and context["work_source"] == "ci"


def test_ci_review_keeps_the_original_evidence_and_head_range(scenario, monkeypatch):
    db, kernels = scenario
    failure, _ = prepare(db, kernels["A"])
    with context_read(kernels["A"], monkeypatch, failure) as (tx, run, job, reads):
        context = kernels["A"]._context(tx, run, job, WORLD, Role.REVIEW)
        assert context["ci_evidence"] == json.loads(CI) and len(reads) == 1
        assert context["review_context"]["facts"]["ci_evidence"] is context["ci_evidence"]
        assert context["review_context"]["review_range"]["base_sha"] == "original-ci-head"


@pytest.mark.parametrize("kind", ["ticket", "parent_only", "final_run"])
@pytest.mark.parametrize("new_failure", [False, True], ids=["same-head-findings", "new-failed-head"])
def test_ci_review_range_consumes_original_findings_for_each_delivery(scenario, kind, new_failure):
    db, kernels = scenario
    kernel = kernels["A"]
    failure, _ = prepare(db, kernel)
    db.execute("UPDATE subjects SET kind=? WHERE run_id='A'", (kind,))
    with kernel.storage.transaction() as tx:
        run = tx.one("SELECT * FROM runs WHERE id='A'")
        job = tx.one("SELECT * FROM subjects WHERE run_id='A'")
        previous = kernel._context(tx, run, job, WORLD, Role.REVIEW)
        original_context = tx.put(SUBJECT, "original_review_context", wire_json(previous))
        original_result = tx.put(SUBJECT, "original_review_result", wire_json(review_result("fail")))
        insert(db, "attempts", {"run_id": "A", "id": "previous-review", "subject": SUBJECT,
            "generation": 1, "ordinal": 1, "role": "review", "purpose": "ci", "window": 1,
            "parent_revision": run["parent_revision"], "graph_revision": run["graph_revision"],
            "revision": job["revision"], "base_sha": previous["base_sha"],
            "completion_revision": job["completion_revision"], "context_id": original_context,
            "result_id": original_result, "status": "consumed", "outcome": "fail"})
        current_failure = (tx.put(SUBJECT, "next_failed_head", wire_json({"head_sha": "new-ci-head"}))
                           if new_failure else failure)
        tx.execute("UPDATE subjects SET candidate_sha=?,candidate_tree=?,review_attempt=?,ci_failure=? WHERE run_id='A'",
            ("c" * 40, "d" * 40, "previous-review", current_failure))
        job = tx.one("SELECT * FROM subjects WHERE run_id='A'")
        current = kernel._context(tx, run, job, WORLD, Role.REVIEW)
        review = current["review_context"]
        assert review["review_range"] == {
            "base_sha": "new-ci-head" if new_failure else previous["reviewed_commit"],
            "candidate_sha": "c" * 40}
        assert review["facts"]["previous_acceptance_artifact"] == review_result("fail")
        assert review["facts"]["previous_review_identity"] == previous["review_context"]["facts"]["current_review_identity"]
        assert review["facts"]["current_review_identity"]["reviewed_candidate_sha"] == "c" * 40
        assert json.loads(tx.get(original_context, SUBJECT)) == previous


@pytest.mark.parametrize("role", [Role.DEVELOPMENT, Role.PUBLICATION])
@pytest.mark.parametrize("damage", ["missing", "digest", "foreign"])
def test_first_ci_material_read_still_rejects_invalid_original_evidence(scenario, monkeypatch, damage, role):
    db, kernels = scenario
    failure, development = prepare(db, kernels["A"])
    if damage == "digest":
        db.execute("UPDATE material SET digest=? WHERE run_id='A' AND id=?", ("0" * 64, failure))
    foreign, _ = prepare(db, kernels["B"])
    with context_read(kernels["A"], monkeypatch, failure,
                      development if role == Role.PUBLICATION else None) as (tx, run, job, reads):
        if damage != "digest":
            job = {**job, "ci_failure": None if damage == "missing" else foreign}
        with pytest.raises(KernelError, match="material_corrupt"):
            kernels["A"]._context(tx, run, job, WORLD, role)
        assert reads == []


def test_ci_projection_does_not_reset_the_owners_cumulative_budget(scenario, monkeypatch):
    db, kernels = scenario
    failure, _ = prepare(db, kernels["A"])
    with context_read(kernels["A"], monkeypatch, failure) as (tx, run, job, reads):
        kernels["A"]._context(tx, run, job, WORLD, Role.DEVELOPMENT)
        owner = tx._transaction
        used = owner.rows_read, owner.bytes_read
        assert used == (tx.rows_read, tx.bytes_read) and len(reads) == 1
        assert storage.QUERY_LIMIT == 33554432
        monkeypatch.setattr(storage, "QUERY_LIMIT", used[1])
        with pytest.raises(KernelError, match="query_limit"):
            tx.one("SELECT 'one more adopted value'")
        assert (owner.rows_read, owner.bytes_read) == used


def test_first_material_sql_failure_poison_and_original_error_propagate(scenario, monkeypatch):
    db, kernels = scenario
    failure, _ = prepare(db, kernels["A"])
    observed = []
    with pytest.raises(sqlite3.DatabaseError) as caught:
        with context_read(kernels["A"], monkeypatch, failure) as (tx, run, job, reads):
            get = tx.get

            def reject(identity, subject):
                if identity != failure:
                    return get(identity, subject)
                db.set_authorizer(lambda action, table, column, *_:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "material"
                    and column == "body" else sqlite3.SQLITE_OK)
                try:
                    return get(identity, subject)
                except sqlite3.DatabaseError as error:
                    observed.append(error)
                    assert tx.failed and tx._transaction.failed and reads == []
                    raise
                finally:
                    db.set_authorizer(None)

            monkeypatch.setattr(tx, "get", reject)
            kernels["A"]._context(tx, run, job, WORLD, Role.DEVELOPMENT)
    assert observed == [caught.value]
    assert db.execute("PRAGMA foreign_key_check").fetchone() is None


@pytest.mark.parametrize("role", [Role.DEVELOPMENT, Role.PUBLICATION])
def test_later_sql_failure_still_stops_context_after_first_ci_read(scenario, monkeypatch, role):
    db, kernels = scenario
    failure, _ = prepare(db, kernels["A"])
    observed = []
    with pytest.raises(sqlite3.DatabaseError) as caught:
        with context_read(kernels["A"], monkeypatch, failure) as (tx, run, job, reads):
            db.set_authorizer(lambda action, table, column, *_:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "history"
                else sqlite3.SQLITE_OK)
            try:
                kernels["A"]._context(tx, run, job, WORLD, role)
            except sqlite3.DatabaseError as error:
                observed.append(error)
                assert len(reads) == 1 and tx.failed and tx._transaction.failed
                raise
            finally:
                db.set_authorizer(None)
    assert observed == [caught.value]
