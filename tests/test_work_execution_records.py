from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_return import restore_agent_return
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt
from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.sqlite_work_evidence import TABLES as EVIDENCE_TABLES, get_evidence
from agent_run.sqlite_work_execution import (
    INDEXES, TABLES, attempt_history_metadata, find_result_reference, load_attempts,
    load_invocations, save_attempts, save_invocations,
)

RUN = "run-records"
JOB = "ticket:472:generation:1"


@pytest.fixture
def database(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    path = tmp_path / "execution.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE runs(run_id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE work_jobs(run_id TEXT,job_id TEXT,owner_kind TEXT,budget_owner_id TEXT,PRIMARY KEY(run_id,job_id))")
    for ddl in (*EVIDENCE_TABLES.values(), *TABLES.values(), *INDEXES.values()):
        connection.execute(ddl)
    connection.execute("INSERT INTO runs VALUES (?)", (RUN,))
    connection.execute("INSERT INTO work_jobs(run_id,job_id,owner_kind) VALUES (?,?,'ticket')", (RUN, JOB))
    connection.commit()
    try:
        yield connection, path
    finally:
        connection.close()


def _subject() -> dict[str, Any]:
    subject: dict[str, Any] = {}
    allocate_semantic_attempt(subject, role="development", work_subject="ticket:472",
                              generation=1, currentness_boundary={"candidate": "first"}, ordinal=1,
                              budget_window=1)
    return subject


def _invocation(attempt: dict[str, Any], started: str) -> dict[str, Any]:
    return {
        "work_subject": attempt["work_subject"], "started_at": started, "status": "completed",
        "generation": attempt["generation"], "role": "development", "phase": "developing",
        "mode": "fresh", "input_fingerprint": "request", "semantic_attempt": deepcopy(attempt),
        "currentness_boundary": {"candidate": "first"},
        "requested_thread_id": None, "reported_thread_id": "thread-one",
        "execution_segments": [{"started_at": started, "ended_at": "2026-10-15T00:00:02+00:00"}],
    }


def _state(subject: dict[str, Any], invocation: dict[str, Any]) -> dict[str, Any]:
    return {"ticket_jobs": {"472": subject}, "active_agent_invocation": invocation,
            "ticket_agent_invocations": {"472": deepcopy(invocation)},
            "agent_invocation_history": [deepcopy(invocation)]}


def _save(connection: sqlite3.Connection, state: dict[str, Any], baseline: dict[str, Any]) -> None:
    save_attempts(connection, RUN, JOB, state["ticket_jobs"]["472"], baseline.get("ticket_jobs", {}).get("472", {}))
    save_invocations(connection, RUN, state, baseline)


@pytest.mark.parametrize("include_invocations", [False, True])
def test_job_only_return_save_binds_current_invocation_in_same_transaction(
    tmp_path: Path, include_invocations: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    subject = _subject()
    state = _state(subject, _invocation(subject["pending_semantic_attempt"], "saved-call"))
    repository.save(RUN, state)
    changed = deepcopy(state)
    returned = {"kind": "development", "thread_id": "thread-one", "summary": "已保存返回", "replaced_thread_id": None}
    changed["ticket_jobs"]["472"]["pending_semantic_attempt"]["returned_result"] = returned
    proposed = changed if include_invocations else {"ticket_jobs": changed["ticket_jobs"]}
    with pytest.raises(RuntimeError, match="abort saved return"):
        with repository.atomic():
            repository.save_objects(RUN, proposed, fields={"ticket_jobs"})
            row = repository._local.connection.execute(
                "SELECT raw_result_evidence_id FROM work_invocations WHERE run_id=?", (RUN,),
            ).fetchone()
            assert row[0] is not None
            raise RuntimeError("abort saved return")
    assert repository.load(RUN) == state
    with closing(sqlite3.connect(repository.path)) as reader:
        assert reader.execute("SELECT COUNT(*) FROM work_attempt_returns").fetchone()[0] == 0
    repository.save_objects(RUN, proposed, fields={"ticket_jobs"})
    repository.save_objects(RUN, proposed, fields={"ticket_jobs"})
    with closing(sqlite3.connect(repository.path)) as reader:
        raw = reader.execute("SELECT raw_result_evidence_id FROM work_invocations WHERE run_id=?", (RUN,)).fetchone()[0]
        attempt_id = subject["pending_semantic_attempt"]["attempt_id"]
        assert get_evidence(reader, RUN, raw, owner_id="attempt:" + attempt_id, kind="returned_result") == returned
        assert reader.execute("SELECT COUNT(*) FROM work_attempt_returns").fetchone()[0] == 1


def test_job_only_return_cannot_rebind_a_previous_invocation(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=1024 * 1024)
    subject = _subject()
    state = _state(subject, _invocation(subject["pending_semantic_attempt"], "original-call"))
    returned = {"kind": "human_blocker", "thread_id": "thread-one", "human_blockers": ["原问题"], "replaced_thread_id": None}
    subject["pending_semantic_attempt"]["returned_result"] = returned
    repository.save(RUN, state)
    continued = deepcopy(state)
    continued["ticket_jobs"]["472"]["pending_semantic_attempt"].pop("returned_result")
    repository.save_objects(RUN, continued, fields={"ticket_jobs"})
    proposed = deepcopy(continued)
    proposed["ticket_jobs"]["472"]["pending_semantic_attempt"]["returned_result"] = {
        "kind": "development", "thread_id": "thread-one", "summary": "另一次调用的返回", "replaced_thread_id": None,
    }
    with pytest.raises(ValueError, match="Invocation already has a different saved result"):
        repository.save_objects(RUN, {"ticket_jobs": proposed["ticket_jobs"]}, fields={"ticket_jobs"})
    assert repository.load(RUN) == continued
    with closing(sqlite3.connect(repository.path)) as reader:
        raw = reader.execute("SELECT raw_result_evidence_id FROM work_invocations WHERE run_id=?", (RUN,)).fetchone()[0]
        attempt_id = subject["pending_semantic_attempt"]["attempt_id"]
        assert get_evidence(reader, RUN, raw, owner_id="attempt:" + attempt_id, kind="returned_result") == returned
        assert reader.execute("SELECT COUNT(*) FROM work_attempt_returns").fetchone()[0] == 1


@pytest.mark.parametrize("pending_binding", [None, "pending-allocation"])
def test_first_thread_attachment_can_resolve_pending_binding_once(
    database: tuple[sqlite3.Connection, Path], pending_binding: str | None,
) -> None:
    connection, _ = database
    subject = _subject()
    profile = {"binding_id": pending_binding, "role": "development", "model": "fixture-model",
               "reasoning_effort": "high", "profile_revision": 1, "thread_id": None}
    invocation = _invocation(subject["pending_semantic_attempt"], "first-attachment")
    invocation.update(status="running", reported_thread_id=None, binding_id=pending_binding,
                      thread_execution_binding=profile if pending_binding is not None else None)
    state = _state(subject, invocation)
    _save(connection, state, {})
    connection.commit()
    attached = deepcopy(state)
    for record in [attached["active_agent_invocation"], attached["ticket_agent_invocations"]["472"], *attached["agent_invocation_history"]]:
        record.update(binding_id="canonical-binding", reported_thread_id="thread-one",
                      thread_execution_binding={**profile, "binding_id": "canonical-binding", "thread_id": "thread-one"})
    with connection:
        save_invocations(connection, RUN, attached, state)
    assert load_invocations(connection, RUN, current_only=False, history=True)[0] == {
        field: attached[field] for field in ("active_agent_invocation", "ticket_agent_invocations", "agent_invocation_history")
    }
    changed = deepcopy(attached)
    for record in [changed["active_agent_invocation"], changed["ticket_agent_invocations"]["472"], *changed["agent_invocation_history"]]:
        record["binding_id"] = "replacement-binding"
        record["thread_execution_binding"]["binding_id"] = "replacement-binding"
    with pytest.raises(ValueError, match="immutable identity changed: binding_id"), connection:
        save_invocations(connection, RUN, changed, attached)
    assert load_invocations(connection, RUN, history=True)[0]["active_agent_invocation"] == attached["active_agent_invocation"]


@pytest.mark.parametrize("changed_field", ["role", "model", "reasoning_effort", "profile_revision", "thread_id"])
def test_first_thread_attachment_rejects_different_profile_or_thread(
    database: tuple[sqlite3.Connection, Path], changed_field: str,
) -> None:
    connection, _ = database
    subject = _subject()
    profile = {"binding_id": "pending-allocation", "role": "development", "model": "fixture-model",
               "reasoning_effort": "high", "profile_revision": 1, "thread_id": None}
    invocation = _invocation(subject["pending_semantic_attempt"], "invalid-attachment")
    invocation.update(status="running", reported_thread_id=None, binding_id="pending-allocation", thread_execution_binding=profile)
    state = _state(subject, invocation)
    _save(connection, state, {})
    connection.commit()
    changed = deepcopy(state)
    for record in [changed["active_agent_invocation"], changed["ticket_agent_invocations"]["472"], *changed["agent_invocation_history"]]:
        record.update(binding_id="canonical-binding", reported_thread_id="thread-one",
                      thread_execution_binding={**profile, "binding_id": "canonical-binding", "thread_id": "thread-one"})
        record["thread_execution_binding"][changed_field] = 2 if changed_field == "profile_revision" else "different"
    with pytest.raises(ValueError, match="immutable identity changed: binding_id"), connection:
        save_invocations(connection, RUN, changed, state)
    assert load_invocations(connection, RUN, history=True)[0]["active_agent_invocation"] == state["active_agent_invocation"]


def test_saved_return_survives_process_reopen_and_consumption(database: tuple[sqlite3.Connection, Path]) -> None:
    connection, path = database
    subject = _subject()
    invocation = _invocation(subject["pending_semantic_attempt"], "2026-10-15T00:00:01+00:00")
    state = _state(subject, invocation)
    _save(connection, state, {})
    baseline = deepcopy(state)
    returned = {"kind": "development", "thread_id": "thread-one", "summary": "已完成准确工作", "replaced_thread_id": None}
    subject["pending_semantic_attempt"]["returned_result"] = returned
    _save(connection, state, baseline)
    connection.commit()
    code = (
        "import json,sqlite3,sys; from agent_run.sqlite_work_execution import load_attempts; "
        "c=sqlite3.connect(sys.argv[1]); "
        "print(json.dumps(load_attempts(c,sys.argv[2],sys.argv[3]),ensure_ascii=False)); c.close()"
    )
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.run([sys.executable, "-c", code, str(path), RUN, JOB], env=environment,
                             check=True, capture_output=True, text=True, timeout=10)
    reopened = json.loads(process.stdout)
    restored = restore_agent_return(reopened["pending_semantic_attempt"]["returned_result"], reopened["pending_semantic_attempt"])
    assert restored.summary == "已完成准确工作"
    baseline = deepcopy(state)
    # A repeated observation keeps the same returned version and budget state.
    _save(connection, state, baseline)
    reference = find_result_reference(connection, RUN, JOB, "development_summary", restored.summary)
    assert reference is not None and reference[3] == ("summary",)
    close_semantic_attempt(subject, subject["pending_semantic_attempt"], outcome="development_result",
                           result={"development_summary": restored.summary})
    _save(connection, state, baseline)
    connection.commit()
    with sqlite3.connect(path) as reader:
        saved = load_attempts(reader, RUN, JOB)
        completed = saved["semantic_attempt_history"][0]
        assert "pending_semantic_attempt" not in saved and "returned_result" not in completed
        assert completed["development_summary"] == restored.summary
        assert "budget_consumed" not in completed
        raw_id = reader.execute("SELECT raw_result_evidence_id FROM work_invocations WHERE run_id=?", (RUN,)).fetchone()[0]
        assert raw_id == reference[0]
        assert get_evidence(reader, RUN, raw_id, owner_id=reference[1], kind="returned_result") == returned
        assert reader.execute("SELECT COUNT(*) FROM work_attempt_returns WHERE run_id=?", (RUN,)).fetchone()[0] == 1
        assert attempt_history_metadata(reader, RUN, JOB) == (len(encode_json([completed]).encode()), 1)


def test_human_continuation_keeps_each_invocation_input_and_raw_return(database: tuple[sqlite3.Connection, Path]) -> None:
    connection, _ = database
    subject = _subject()
    attempt = subject["pending_semantic_attempt"]
    attempt["review_context"] = {"facts": {"human_response": "第一版"}}
    first = _invocation(attempt, "2026-10-15T00:00:01+00:00")
    state = _state(subject, first)
    _save(connection, state, {})
    before = deepcopy(state)
    attempt["returned_result"] = {"kind": "human_blocker", "thread_id": "thread-one",
                                   "human_blockers": ["需要人工资料"], "replaced_thread_id": None}
    _save(connection, state, before)
    before = deepcopy(state)
    attempt.pop("returned_result")
    attempt["review_context"] = {"facts": {"human_response": "第二版"}}
    _save(connection, state, before)
    before = deepcopy(state)
    second = _invocation(attempt, "2026-10-15T00:00:02+00:00")
    state["active_agent_invocation"] = second
    state["ticket_agent_invocations"]["472"] = deepcopy(second)
    state["agent_invocation_history"].append(deepcopy(second))
    _save(connection, state, before)
    before = deepcopy(state)
    attempt["returned_result"] = {"kind": "development", "thread_id": "thread-one", "summary": "人工继续后完成", "replaced_thread_id": None}
    _save(connection, state, before)
    connection.commit()
    loaded, _ = load_invocations(connection, RUN, current_only=False, history=True)
    records = loaded["agent_invocation_history"]
    assert [record["semantic_attempt"]["review_context"] for record in records] == [
        {"facts": {"human_response": "第一版"}}, {"facts": {"human_response": "第二版"}},
    ]
    loaded["active_agent_invocation"]["semantic_attempt"]["review_context"]["facts"]["human_response"] = "本地修改"
    assert records[1]["semantic_attempt"]["review_context"]["facts"]["human_response"] == "第二版"
    assert loaded["ticket_agent_invocations"]["472"]["semantic_attempt"]["review_context"]["facts"]["human_response"] == "第二版"
    raw_ids = connection.execute("SELECT raw_result_evidence_id FROM work_invocations WHERE run_id=? ORDER BY started_at", (RUN,)).fetchall()
    raw = [get_evidence(connection, RUN, row[0], owner_id="attempt:" + attempt["attempt_id"], kind="returned_result") for row in raw_ids]
    assert [value["kind"] for value in raw] == ["human_blocker", "development"]
    assert raw[0]["human_blockers"] == ["需要人工资料"]
    assert raw[1]["summary"] == "人工继续后完成"


@pytest.mark.parametrize("state", [
    {}, {"active_agent_invocation": None}, {"ticket_agent_invocations": {}},
    {"agent_invocation_history": []},
    {"active_agent_invocation": {"work_subject": "ticket:472", "started_at": "first", "status": "running"}},
    {"agent_invocation_history": [{"work_subject": "ticket:472", "started_at": "first", "status": "failed"}]},
])
def test_partial_invocation_roots_reopen_exactly(database: tuple[sqlite3.Connection, Path], state: dict[str, Any]) -> None:
    connection, path = database
    save_invocations(connection, RUN, state, {})
    connection.commit()
    with sqlite3.connect(path) as reader:
        loaded, _ = load_invocations(reader, RUN, current_only=False, history=True)
    assert loaded == state


def test_resume_pending_is_current_only_and_recovery_wait_is_actual_status(database: tuple[sqlite3.Connection, Path]) -> None:
    connection, _ = database
    failed = {"work_subject": "ticket:472", "started_at": "first", "status": "failed", "error": "operator_stopped"}
    state = {"active_agent_invocation": failed, "ticket_agent_invocations": {"472": deepcopy(failed)}, "agent_invocation_history": [deepcopy(failed)]}
    save_invocations(connection, RUN, state, {})
    before = deepcopy(state)
    state["active_agent_invocation"]["status"] = "resuming"
    state["ticket_agent_invocations"]["472"]["status"] = "resuming"
    save_invocations(connection, RUN, state, before)
    loaded, _ = load_invocations(connection, RUN, current_only=False, history=True)
    assert loaded["active_agent_invocation"]["status"] == "resuming"
    assert loaded["ticket_agent_invocations"]["472"]["status"] == "resuming"
    assert loaded["agent_invocation_history"][0]["status"] == "failed"
    before = deepcopy(loaded)
    recovery = {"work_subject": "ticket:472", "started_at": "second", "status": "resuming", "recovery_waiting": True}
    loaded["active_agent_invocation"] = recovery
    loaded["ticket_agent_invocations"]["472"] = deepcopy(recovery)
    loaded["agent_invocation_history"].append(deepcopy(recovery))
    save_invocations(connection, RUN, loaded, before)
    actual, _ = load_invocations(connection, RUN, current_only=False, history=True)
    assert actual["active_agent_invocation"]["status"] == "resuming"
    assert [record["status"] for record in actual["agent_invocation_history"]] == ["failed", "resuming"]


def test_ticket_history_subset_keeps_global_positions_and_unloaded_sibling(database: tuple[sqlite3.Connection, Path]) -> None:
    connection, _ = database
    records = [{"work_subject": subject, "started_at": str(index), "status": "failed", "resume_id": "resume-three" if subject == "ticket:3" else None}
               for index, subject in enumerate(("ticket:472", "ticket:3", "ticket:472"))]
    state = {"ticket_agent_invocations": {"472": deepcopy(records[-1]), "3": deepcopy(records[1])},
             "agent_invocation_history": records}
    save_invocations(connection, RUN, state, {})
    partial, positions = load_invocations(connection, RUN, ticket_numbers={472}, current_only=False, history=True)
    assert [entry["started_at"] for entry in partial["agent_invocation_history"]] == ["0", "2"]
    assert positions == {encode_json(("ticket:472", "0")): 0, encode_json(("ticket:472", "2")): 2}
    before = deepcopy(partial)
    partial["ticket_agent_invocations"]["472"]["error"] = "本票更新"
    partial["agent_invocation_history"][-1]["error"] = "本票更新"
    save_invocations(connection, RUN, partial, before)
    complete, _ = load_invocations(connection, RUN, current_only=False, history=True)
    assert complete["ticket_agent_invocations"]["3"] == records[1]
    assert complete["agent_invocation_history"][1] == records[1]
    related, positions = load_invocations(connection, RUN, ticket_numbers={472}, resume_ids={"resume-three"}, history=True)
    assert [entry["started_at"] for entry in related["agent_invocation_history"]] == ["1", "2"]
    assert positions[encode_json(("ticket:3", "1"))] == 1


def test_attempt_run_identity_and_stale_or_duplicate_returns_fail_closed(database: tuple[sqlite3.Connection, Path]) -> None:
    connection, _ = database
    first = _subject()
    save_attempts(connection, RUN, JOB, first, {})
    stale = deepcopy(first)
    returned = {"kind": "development", "thread_id": "thread-one", "summary": "第一次", "replaced_thread_id": None}
    first["pending_semantic_attempt"]["returned_result"] = returned
    save_attempts(connection, RUN, JOB, first, stale)
    changed = deepcopy(first)
    changed["pending_semantic_attempt"]["returned_result"]["summary"] = "覆盖已存返回"
    with pytest.raises(ValueError, match="different saved result"):
        save_attempts(connection, RUN, JOB, changed, first)
    old_result = deepcopy(stale)
    old_result["pending_semantic_attempt"]["budget_consumed"] = True
    with pytest.raises(ValueError, match="changed before save"):
        save_attempts(connection, RUN, JOB, old_result, stale)
    connection.execute("INSERT INTO runs VALUES ('second-run')")
    connection.execute("INSERT INTO work_jobs(run_id,job_id,owner_kind) VALUES ('second-run',?,'ticket')", (JOB,))
    second = _subject()
    second["pending_semantic_attempt"]["returned_result"] = {**returned, "summary": "另一个Run"}
    save_attempts(connection, "second-run", JOB, second, {})
    assert load_attempts(connection, RUN, JOB)["pending_semantic_attempt"]["returned_result"]["summary"] == "第一次"
    assert load_attempts(connection, "second-run", JOB)["pending_semantic_attempt"]["returned_result"]["summary"] == "另一个Run"


def test_related_ticket_selection_keeps_global_active_identity(database: tuple[sqlite3.Connection, Path]) -> None:
    connection, _ = database
    first = {"work_subject": "ticket:1", "started_at": "first", "status": "completed"}
    second = {"work_subject": "ticket:2", "started_at": "second", "status": "failed"}
    state = {"active_agent_invocation": first, "ticket_agent_invocations": {"1": deepcopy(first), "2": deepcopy(second)},
             "agent_invocation_history": [deepcopy(first), deepcopy(second)]}
    save_invocations(connection, RUN, state, {})
    selected, positions = load_invocations(connection, RUN, ticket_numbers={2}, history=True)
    assert selected["active_agent_invocation"] == first
    assert set(selected["ticket_agent_invocations"]) == {"2"}
    assert selected["agent_invocation_history"] == [first, second]
    assert positions[encode_json(("ticket:1", "first"))] == 0
    before = deepcopy(selected)
    selected["active_agent_invocation"] = deepcopy(second)
    save_invocations(connection, RUN, selected, before)
    final, _ = load_invocations(connection, RUN, current_only=False, history=True)
    assert final["active_agent_invocation"] == second
    assert final["ticket_agent_invocations"]["1"] == first


def test_review_budget_owner_references_exact_historical_thread_and_identity(database: tuple[sqlite3.Connection, Path]) -> None:
    from support.run_artifacts import _passing_artifact

    connection, _ = database
    connection.execute("INSERT INTO work_jobs VALUES (?,?,?,?)", (RUN, "repair", "repair", JOB))
    subject: dict[str, Any] = {}
    artifact = _passing_artifact()
    records = []
    for ordinal in (1, 2):
        before = deepcopy(subject)
        attempt = allocate_semantic_attempt(subject, role="reviewer", work_subject="run-repair:" + RUN,
                                           generation=1, currentness_boundary={"candidate": ordinal}, ordinal=ordinal, budget_window=1)
        review_identity = {"candidate_sha": str(ordinal)}
        attempt["review_context"] = {"facts": {"acceptance_scope": "ticket", "current_review_identity": review_identity}}
        attempt["returned_result"] = {"kind": "review", "thread_id": f"review-{ordinal}", "artifact": artifact}
        save_attempts(connection, RUN, "repair", subject, before)
        records.append({"reviewer_thread_id": f"review-{ordinal}", "review_identity": review_identity, "artifact": artifact})
        before = deepcopy(subject)
        close_semantic_attempt(subject, attempt, outcome="acceptance_artifact")
        save_attempts(connection, RUN, "repair", subject, before)
    sources = [find_result_reference(connection, RUN, JOB, "review_artifact", record) for record in records]
    assert all(source is not None for source in sources)
    for ordinal, source in enumerate(sources, 1):
        assert source is not None
        original = get_evidence(connection, RUN, source[0], owner_id=source[1], kind=source[2])
        assert original["thread_id"] == f"review-{ordinal}"
    assert sources[0][0] != sources[1][0]
    assert find_result_reference(connection, RUN, JOB, "review_artifact", {**records[0], "review_identity": {"candidate_sha": "wrong"}}) is None


def test_normalized_publication_references_original_wire_fields(database: tuple[sqlite3.Connection, Path]) -> None:
    connection, _ = database
    subject: dict[str, Any] = {}
    attempt = allocate_semantic_attempt(subject, role="publication", work_subject="ticket:472", generation=1,
                                       currentness_boundary={}, ordinal=1)
    normalized = {"commit_message": "Finish publication", "pr_title": "Publish exact work", "pr_body_markdown": "原始发布正文"}
    attempt["returned_result"] = {"kind": "publication", "thread_id": "publication-thread", "replaced_thread_id": None,
                                   "artifact": {"result_kind": "publication", **normalized, "human_blockers": None}}
    save_attempts(connection, RUN, JOB, subject, {})
    source = find_result_reference(connection, RUN, JOB, "publication", normalized)
    assert source is not None and source[3] == ("publication_fields",)
    before = deepcopy(subject)
    close_semantic_attempt(subject, attempt, outcome="publication_artifact", result={"publication": normalized})
    save_attempts(connection, RUN, JOB, subject, before)
    completed = load_attempts(connection, RUN, JOB)["semantic_attempt_history"][0]
    assert completed["publication"] == normalized
    alias = connection.execute("SELECT e.source_evidence_id,e.material_id FROM work_attempt_versions v "
                               "JOIN work_evidence e ON e.run_id=v.run_id AND e.evidence_id=v.publication_evidence_id "
                               "WHERE v.run_id=? AND v.status='completed'", (RUN,)).fetchone()
    assert alias == (source[0], None)


@pytest.mark.parametrize("damage", ["snapshot", "boundary", "presence"])
def test_wrong_invocation_input_and_damaged_version_are_rejected(database: tuple[sqlite3.Connection, Path], damage: str) -> None:
    connection, _ = database
    subject = _subject()
    subject["pending_semantic_attempt"]["review_context"] = {"facts": "fixed"}
    state = _state(subject, _invocation(subject["pending_semantic_attempt"], "2026-10-15T00:00:01+00:00"))
    _save(connection, state, {})
    connection.commit()
    if damage == "presence":
        connection.execute("UPDATE work_attempt_versions SET presence='invalid' WHERE run_id=?", (RUN,))
        with pytest.raises(ValueError, match="presence"):
            load_invocations(connection, RUN, current_only=False, history=True)
    else:
        changed = deepcopy(state)
        for record in [changed["active_agent_invocation"], changed["ticket_agent_invocations"]["472"], *changed["agent_invocation_history"]]:
            if damage == "snapshot":
                record["semantic_attempt"]["review_context"] = {"facts": "different"}
            else:
                record["currentness_boundary"] = {"candidate": "wrong"}
        with pytest.raises(ValueError, match="immutable identity|boundary"):
            with connection:
                save_invocations(connection, RUN, changed, state)
        assert load_invocations(connection, RUN, current_only=False, history=True)[0]["active_agent_invocation"] == state["active_agent_invocation"]


def test_old_detail_compacts_after_current_reference_releases(database: tuple[sqlite3.Connection, Path]) -> None:
    connection, _ = database
    records = [{"work_subject": "ticket:472", "started_at": str(index), "status": "completed", "reported_thread_id": "thread-one",
                "execution_segments": [{"started_at": "2026-10-15T00:00:01+00:00", "ended_at": "2026-10-15T00:00:02+00:00"}],
                "recovery_wait_intervals": [{"kind": "capacity", "started_at": "2026-10-15T00:00:00+00:00", "ended_at": "2026-10-15T00:00:01+00:00"}]}
               for index in range(258)]
    state = {"active_agent_invocation": deepcopy(records[0]), "agent_invocation_history": records}
    save_invocations(connection, RUN, state, {})
    loaded, _ = load_invocations(connection, RUN, current_only=False, history=True)
    assert loaded["agent_invocation_history"][0]["execution_segments"]
    assert loaded["agent_invocation_history"][1]["execution_segments"] == []
    before = deepcopy(loaded)
    loaded["active_agent_invocation"] = None
    save_invocations(connection, RUN, loaded, before)
    final, _ = load_invocations(connection, RUN, current_only=False, history=True)
    oldest = final["agent_invocation_history"][0]
    assert oldest["execution_segments"] == [] and oldest["recovery_wait_intervals"] == []
    assert oldest["execution_segments_summary"]["count"] == 1
    assert oldest["execution_timing_summary"]["execution_timing_complete"] is False
    assert final["agent_invocation_history"][-1]["execution_segments"]
