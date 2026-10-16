"""Run 根事实同源、准确固定输入、局部保存与全局门禁的回归。"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import sqlite3

import pytest

from agent_run.sqlite_run_inputs import input_version_for_revision, load_parent
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.state_errors import IncompatibleRunStateError
from support.work_records import invocation


def _facts() -> dict:
    return {
        "run_id": "run-1", "repository": "Example/Repo", "status": "active", "phase": "delivery",
        "created_at": "created", "updated_at": "updated", "language": "zh-CN",
        "parent": {"number": 1, "title": "需求", "body": "原始正文", "revision": "parent-1"},
        "ticket_graph": {"revision": "graph-1", "ordered_ticket_numbers": [2, 3], "order_source": "github_sub_issues",
                         "tickets": {"2": {"number": 2, "title": "A", "body": "A正文", "state": "OPEN", "blocked_by": []},
                                     "3": {"number": 3, "title": "B", "body": "B正文", "state": "OPEN", "blocked_by": [{"number": 2, "state": "OPEN"}]}}},
        "accepted_parent_spec_revision": "parent-1", "accepted_ticket_graph_revision": "graph-1",
        "frontier": [2], "ticket_integration_holder": None, "ticket_integration_queue": [],
        "ticket_work_queue": [{"ticket_number": 2, "generation": 1, "role": "development", "lease_id": None}],
        "ticket_pause_requests": {}, "retired_ticket_generations": {}, "pending_ticket_retirements": {},
        "diagnostics": [], "timeline": [{"at": "2026-10-15T00:00:00+00:00", "kind": "run_status", "status": "active"}],
        "ticket_jobs": {"2": {"ticket_number": 2, "ticket_branch_generation": 1, "phase": "candidate", "candidate_sha": "candidate-A"},
                        "3": {"ticket_number": 3, "ticket_branch_generation": 1, "phase": "completed", "candidate_sha": "candidate-B"}},
    }


def test_root_current_fields_paths_and_discovery_share_one_authority(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    state = _facts()
    repo.save("run-1", state)
    reopened = SQLiteStateRepository(tmp_path)
    assert reopened.load("run-1") == state
    assert reopened.load_work("run-1", 2) == state
    assert reopened.load_fields("run-1", {"repository", "parent", "frontier"}) == {key: state[key] for key in ("repository", "parent", "frontier")}
    assert reopened.load_paths("run-1", {("parent", "number"), ("ticket_graph", "tickets", "2", "body")}) == {
        "parent": {"number": 1}, "ticket_graph": {"tickets": {"2": {"body": "A正文"}}},
    }
    assert reopened.find_ids("example/repo", 1, unfinished_only=True) == ["run-1"]
    with closing(sqlite3.connect(repo.path)) as c:
        names = {row[0] for row in c.execute("SELECT name FROM sqlite_master")}
        assert not names & {"run_objects", "run_materials", "run_sizes", "run_object_index"}
        assert c.execute("SELECT repository,status,phase,parent_number FROM runs").fetchone() == ("Example/Repo", "active", "delivery", 1)
        c.execute("UPDATE runs SET status='completed'")
        c.commit()
    assert reopened.load_fields("run-1", {"status"}) == {"status": "completed"}
    assert reopened.find_ids("Example/Repo", 1, unfinished_only=True) == []


def test_parent_versions_preserve_requirements_and_reject_ambiguous_revision(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    state = _facts()
    repo.save("run-1", state)
    with closing(sqlite3.connect(repo.path)) as c:
        old = input_version_for_revision(c, "run-1", "parent", "parent-1")
    changed = deepcopy(state["parent"])
    changed.update(body="新正文", revision="parent-2")
    repo.save_objects("run-1", {"parent": changed}, fields={"parent"})
    with closing(sqlite3.connect(repo.path)) as c:
        assert load_parent(c, "run-1", old) == state["parent"]
        assert input_version_for_revision(c, "run-1", "parent", "parent-2") != old
    with pytest.raises(ValueError, match="revision"):
        repo.save_objects("run-1", {"parent": {**changed, "body": "同版本错误正文"}}, fields={"parent"})
    assert repo.load_fields("run-1", {"parent"}) == {"parent": changed}


def test_same_graph_structure_keeps_identity_across_current_observations(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    state = _facts()
    repo.save("run-1", state)
    with closing(sqlite3.connect(repo.path)) as c:
        version = input_version_for_revision(c, "run-1", "graph", "graph-1")
        original_snapshot = c.execute("SELECT value_graph_snapshot_id FROM run_root").fetchone()[0]
    observed = deepcopy(state["ticket_graph"])
    observed["ordered_ticket_numbers"] = [3, 2]
    observed["tickets"]["2"]["state"] = "CLOSED"
    observed["tickets"]["3"]["blocked_by"][0]["state"] = "CLOSED"
    repo.save_objects("run-1", {"ticket_graph": observed}, fields={"ticket_graph"})
    assert repo.load_fields("run-1", {"ticket_graph"}) == {"ticket_graph": observed}
    with closing(sqlite3.connect(repo.path)) as c:
        assert input_version_for_revision(c, "run-1", "graph", "graph-1") == version
        assert c.execute("SELECT value_graph_snapshot_id FROM run_root").fetchone()[0] != original_snapshot
        bodies = [json.loads(body) for body, in c.execute("SELECT body FROM work_evidence_materials")]
        reports = [body for body in bodies if isinstance(body, dict) and "tickets_present" in body]
        assert reports
        for report in reports:
            assert "revision" not in report and "ordered_ticket_numbers" not in report
            assert all("number" not in ticket and all("number" not in blocker for blocker in ticket.get("blocked_by", []))
                       for ticket in report["tickets"])
    changed = deepcopy(observed)
    changed["tickets"]["3"]["blocked_by"] = []
    with pytest.raises(ValueError, match="fixed structures"):
        repo.save_objects("run-1", {"ticket_graph": changed}, fields={"ticket_graph"})


def test_shared_run_failure_and_operator_stop_preserve_completed_sibling(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    original = _facts()
    repo.save("run-1", original)
    diagnostic = {"code": "shared_storage_failure", "message": "disk full", "operator_gate": {
        "work_subject": "run:run-1", "action_kind": "execution_failure", "phase": "execution_failed", "reason": "shared_storage_failure"}}
    failure = {"status": "execution_failed", "diagnostics": [diagnostic]}
    repo.save_objects("run-1", failure, fields=set(failure))
    stopped = {"status": "operator_stopped", "diagnostics": [{"code": "operator_stopped", "message": "stopped"}],
               "operator_stop": {"resume_status": "execution_failed", "resume_terminal_kind": None,
                                 "resume_diagnostics": [diagnostic], "stopped_at": "stop"}}
    repo.save_objects("run-1", stopped, fields=set(stopped))
    loaded = SQLiteStateRepository(tmp_path).load_work("run-1", 2)
    assert loaded["operator_stop"]["resume_diagnostics"] == [diagnostic]
    assert loaded["ticket_jobs"]["3"] == original["ticket_jobs"]["3"]
    with closing(sqlite3.connect(repo.path)) as c:
        row = c.execute("SELECT value_code,value_operator_gate_work_subject,value_operator_gate_reason FROM run_diagnostic_versions "
                        "WHERE value_code='shared_storage_failure'").fetchone()
        assert row == ("shared_storage_failure", "run:run-1", "shared_storage_failure")
        assert c.execute("SELECT COUNT(*) FROM run_diagnostic_versions WHERE value_code='shared_storage_failure'").fetchone() == (1,)


def test_keyed_partial_save_and_field_deletion_do_not_clear_siblings(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    state = _facts() | {"ticket_pause_requests": {"2": "pause-A", "3": "pause-B"},
                        "retired_ticket_generations": {"2": 1, "3": 2}}
    repo.save("run-1", state)
    repo.save_objects("run-1", {"ticket_pause_requests": {}, "retired_ticket_generations": {"2": 3}},
                      fields={"ticket_pause_requests", "retired_ticket_generations"},
                      keyed_changes={"ticket_pause_requests": {"2"}, "retired_ticket_generations": {"2"}})
    assert repo.load_fields("run-1", {"ticket_pause_requests", "retired_ticket_generations"}) == {
        "ticket_pause_requests": {"3": "pause-B"}, "retired_ticket_generations": {"2": 3, "3": 2}}
    assert repo.ticket_pause_requested("run-1", 3)
    assert not repo.ticket_pause_requested("run-1", 2)
    repo.save_objects("run-1", {}, fields={"language"})
    assert repo.load_fields("run-1", {"language"}) == {}
    assert repo.load("run-1")["ticket_jobs"]["3"] == state["ticket_jobs"]["3"]


def test_root_and_job_results_rollback_together(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    state = _facts()
    repo.save("run-1", state)
    with pytest.raises(OSError, match="crash"):
        with repo.atomic():
            changed = deepcopy(state)
            changed["status"] = "execution_failed"
            changed["ticket_jobs"]["2"]["candidate_sha"] = "new-candidate"
            repo.save_objects("run-1", changed, fields={"status", "ticket_jobs"})
            raise OSError("crash")
    assert SQLiteStateRepository(tmp_path).load("run-1") == state


def test_small_root_patch_does_not_load_fixed_inputs_or_unrelated_history(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    state = _facts()
    state["parent"]["body"] = "大正文" * 10000
    state["agent_invocation_history"] = [invocation(started=f"old-{number}", context={"body": "history" * 300})
                                          for number in range(100)]
    repo.save("run-1", state)
    with repo.atomic():
        c = repo._local.connection
        queries = []
        c.set_trace_callback(queries.append)
        try:
            repo.save_objects("run-1", {"status": "run_acceptance_pending"}, fields={"status"})
        finally:
            c.set_trace_callback(None)
    reads = [query.lower() for query in queries if query.lstrip().lower().startswith("select")]
    assert not any("from work_evidence_materials" in query or "from run_parent_versions" in query
                   for query in reads)
    assert repo.load_fields("run-1", {"status"}) == {"status": "run_acceptance_pending"}


def test_owned_root_material_cannot_borrow_another_run_reference(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    repo.save("run-1", {"prompt_resources": {"text": "one"}})
    repo.save("run-2", {"prompt_resources": {"text": "two"}})
    with closing(sqlite3.connect(repo.path)) as c:
        foreign = c.execute("SELECT value_prompt_resources FROM run_root WHERE run_id='run-2'").fetchone()[0]
        c.execute("UPDATE run_root SET value_prompt_resources=? WHERE run_id='run-1'", (foreign,))
        c.commit()
    with pytest.raises(ValueError, match="owned|owner"):
        repo.load_fields("run-1", {"prompt_resources"})


def test_previous_sqlite_protocol_is_rejected_without_rewriting(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    repo.save("run-1", _facts())
    with closing(sqlite3.connect(repo.path)) as c:
        c.execute("UPDATE schema_meta SET value='sqlite-work-records-v4'")
        c.commit()
    before = repo.path.read_bytes()
    with pytest.raises(IncompatibleRunStateError, match="protocol"):
        repo.load("run-1")
    assert repo.path.read_bytes() == before


def test_abandonment_and_cleanup_keep_typed_closeout_facts_and_exact_receipt(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    state = _facts()
    receipt = {"run_id": "run-1", "ticket_number": 3, "pr_number": 30, "integrated_sha": "integrated",
               "event_id": "event-3", "actor": "bot", "dispatch_attempted": True}
    state["ticket_jobs"]["3"]["ticket_close_ownership"] = receipt
    closeout = {
        "run_abandonment": {"phase": "pending", "kind": "ticket_run", "change_prs": [{"pr_number": 20, "status": "completed"}],
                            "final_pr": {"pr_number": 40, "status": "pending"}, "tickets": [{"ticket_number": 3, "pr_number": 30,
                            "integrated_sha": "integrated", "eligible": True, "recorded_ownership": receipt,
                            "expected_ownership": receipt, "status": "pending"}]},
        "delivery_cleanup": {"status": "cleanup_pending", "last_error": "dirty checkout", "items": {
            "agent-run/run-1/ticket-2": {"kind": "ticket", "branch": "agent-run/run-1/ticket-2", "expected_head_sha": "local",
                "expected_remote_head_sha": "remote", "checkout": "/isolated/checkout", "attempts": 2,
                "status": "cleanup_pending", "last_error": "dirty checkout", "recovery_kind": "stale_dirty_checkout"}}},
    }
    # Source receipt and referencing closeout join one atomic creation.
    repo.save("run-1", state | closeout)
    assert repo.load_fields("run-1", set(closeout)) == closeout
    with closing(sqlite3.connect(repo.path)) as c:
        assert c.execute("SELECT COUNT(*),source_record_kind,value_event_id FROM run_close_receipts").fetchone() == (1, "ticket_close_ownership", None)
        assert c.execute("SELECT value_expected_head_sha,value_expected_remote_head_sha,value_attempts FROM run_cleanup_items").fetchone() == ("local", "remote", 2)
    completed = deepcopy(closeout)
    completed["run_abandonment"]["phase"] = "completed"
    completed["run_abandonment"]["tickets"][0]["status"] = "completed"
    repo.save_objects("run-1", completed, fields=set(completed))
    assert SQLiteStateRepository(tmp_path).load_fields("run-1", set(completed)) == completed


@pytest.mark.parametrize("field", ["status", "repository", "language"])
def test_metadata_and_core_share_the_current_record_limit(tmp_path: Path, field: str) -> None:
    repo = SQLiteStateRepository(tmp_path)
    state = _facts()
    repo.save("run-1", state)
    with pytest.raises(ValueError, match="current record"):
        repo.save_objects("run-1", {field: "X" * (256 * 1024)}, fields={field})
    assert repo.load_fields("run-1", {field}) == {field: state[field]}
    with closing(sqlite3.connect(repo.path)) as c:
        table, name = ("runs", field) if field in {"status", "repository"} else ("run_root", "value_" + field)
        c.execute(f"UPDATE {table} SET {name}=?", ("X" * (256 * 1024),))
        c.commit()
    with pytest.raises(ValueError, match="current record"):
        repo.load_fields("run-1", {field})


def test_run_delete_cascades_owned_input_gate_and_closeout_relations(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    repo.save("run-1", _facts())
    repo.save_objects("run-1", {"diagnostics": [{"code": "failed", "message": "original"}],
                      "run_abandonment": {"kind": "parent_only", "phase": "pending", "parent_pr": {"pr_number": 42, "status": "pending"}}},
                      fields={"diagnostics", "run_abandonment"})
    with repo.atomic():
        repo._local.connection.execute("DELETE FROM runs WHERE run_id='run-1'")
        assert repo._local.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not repo.has("run-1")


def test_requeue_transition_references_an_exact_source_job_version(tmp_path: Path) -> None:
    from agent_run.requeue import _retired_record

    repo = SQLiteStateRepository(tmp_path)
    state = _facts()
    repo.save("run-1", state)
    original = repo.load_fields("run-1", {"ticket_jobs"})["ticket_jobs"]["2"]
    original["phase"] = "developing"
    transition = {"retired": _retired_record("ticket:2", original), "base_sha": "base", "close_nonce": "nonce"}
    repo.save_objects("run-1", {"requeue_transition": transition, "ticket_jobs": {"2": original}},
                      fields={"requeue_transition", "ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    assert repo.load_fields("run-1", {"requeue_transition"}) == {"requeue_transition": transition}
    refreshed = {**original, "source_revision_changed": True}
    repo.save_objects("run-1", {"ticket_jobs": {"2": refreshed}}, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    assert repo.load_fields("run-1", {"requeue_transition"}) == {"requeue_transition": transition}
    with pytest.raises(ValueError, match="requeue current record"):
        repo.save_objects("run-1", {"requeue_transition": {**transition, "base_sha": "X" * (256 * 1024)}},
                          fields={"requeue_transition"})
    assert repo.load_fields("run-1", {"requeue_transition"}) == {"requeue_transition": transition}
    changed = deepcopy(refreshed)
    changed["candidate_sha"] = "unexpected"
    with pytest.raises(ValueError, match="version changed"):
        repo.save_objects("run-1", {"ticket_jobs": {"2": changed}}, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    assert repo.load_fields("run-1", {"requeue_transition"}) == {"requeue_transition": transition}
    with closing(sqlite3.connect(repo.path)) as c, c:
        c.execute("UPDATE run_requeue_transition SET base_sha=?", ("X" * (256 * 1024),))
    with pytest.raises(ValueError, match="requeue current record"):
        repo.load_fields("run-1", {"requeue_transition"})
    with closing(sqlite3.connect(repo.path)) as c, c:
        c.execute("UPDATE run_requeue_transition SET base_sha=CAST('base' AS BLOB)")
    with pytest.raises(ValueError, match="requeue.base_sha must be text"):
        repo.load_fields("run-1", {"requeue_transition"})


def test_pause_limits_large_current_intent_before_returning_body(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path)
    repo.save("run-1", {"ticket_pause_requests": {"2": "original"}})
    with closing(sqlite3.connect(repo.path)) as c:
        c.execute("UPDATE run_ticket_pause_requests SET requested_at=?", ("X" * (300 * 1024),))
        c.commit()
    with pytest.raises(ValueError, match="scheduling record"):
        repo.ticket_pause_requested("run-1", 2)
