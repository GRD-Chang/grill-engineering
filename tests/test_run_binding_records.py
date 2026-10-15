"""Run bindings retain the reviewed completion set and exact merge authority."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run import sqlite_change_jobs as jobs
from agent_run import sqlite_ticket_completions as completions
from agent_run import sqlite_work_evidence, sqlite_work_execution


def _open(path: Path, *, create: bool = False) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=ON")
    if create:
        c.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
        c.execute("INSERT INTO runs VALUES ('run-1')")
        for module in (sqlite_work_evidence, jobs, sqlite_work_execution):
            for ddl in module.TABLES.values():
                c.execute(ddl)
        c.commit()
    return c


def _completion() -> list[dict[str, Any]]:
    return [{"ticket_number": 2, "integrated_sha": "ticket-merge", "effective_revision": "ticket-revision",
             "reviewed_base_sha": "ticket-base", "reviewed_candidate_tree": "ticket-tree"}]


def test_run_review_publication_and_merge_keep_one_fixed_completion_set(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    boundary = {"parent_revision": "parent-old", "ticket_graph_revision": "graph-old",
                "default_head_sha": "default-old", "run_head_sha": "run-head", "pr_head_sha": "run-head",
                "expected_merge_tree": "merge-tree", "ticket_completion_records": _completion()}
    review = {"acceptance_scope": "run", "reviewed_default_base_sha": "default-old", "reviewed_head_sha": "run-head",
              "expected_merge_tree": "merge-tree", "parent_revision": "parent-old", "ticket_graph_revision": "graph-old",
              "ticket_completion_records": _completion(), "reviewer_thread_id": "reviewer", "artifact": {"checks": "original"}}
    state = {"run_acceptance": {"phase": "accepted", "acceptance_record": review, "acceptance_artifact": review["artifact"]},
             "run_publication": {"phase": "ready_for_approval", "record": boundary,
                "approval_grant": {"head_sha": "run-head", "base_sha": "default-old", "acceptance_fingerprint": "reviewed"},
                "merge_intent": {"pr_number": 3, "head_sha": "run-head", "base_sha": "default-old",
                                 "attempts": 1, "acceptance_boundary": deepcopy(boundary)}}}
    with closing(_open(path, create=True)) as c, c:
        jobs.save_jobs(c, "run-1", state, {})
        assert jobs.load_jobs(c, "run-1") == state
        # Structural guard: the migrated core is not another report payload.
        assert c.execute("SELECT COUNT(*) FROM work_completion_sets").fetchone() == (1,)
        assert c.execute("SELECT kind FROM work_evidence WHERE kind IN ('record','acceptance_record','merge_intent.acceptance_boundary')").fetchall() == []
    with closing(_open(path)) as c, c:
        restored = jobs.load_jobs(c, "run-1")
        changed = deepcopy(restored)
        changed["run_publication"]["record"]["ticket_completion_records"][0]["integrated_sha"] = "new-ticket-merge"
        jobs.save_jobs(c, "run-1", changed, restored, fields={"run_publication"})
        actual = jobs.load_jobs(c, "run-1")
        assert actual["run_acceptance"] == state["run_acceptance"]
        assert actual["run_publication"]["merge_intent"] == state["run_publication"]["merge_intent"]
        assert actual["run_publication"]["approval_grant"] == state["run_publication"]["approval_grant"]
        assert actual["run_publication"]["record"]["ticket_completion_records"] != _completion()


def test_completion_set_preserves_null_absent_and_receipt_evidence_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    values = [{"ticket_number": 2, "reviewed_base_sha": None},
              {"ticket_number": 3, "ci_repair_publication_receipt": {"original": "raw CI authority"}}]
    with closing(_open(path, create=True)) as c, c:
        identity = completions.put_completions(c, "run-1", "acceptance", "tickets", values)
        assert completions.put_completions(c, "run-1", "publication", "tickets", values) == identity
    with closing(_open(path)) as c:
        assert completions.get_completions(c, "run-1", "acceptance", "tickets", identity) == values
        with pytest.raises(ValueError, match="owned Ticket completion"):
            completions.get_completions(c, "run-1", "other-owner", "tickets", identity)


def test_changed_completion_cannot_rewrite_review_evidence(tmp_path: Path) -> None:
    with closing(_open(tmp_path / "state.sqlite3", create=True)) as c, c:
        identity = completions.put_completions(c, "run-1", "acceptance", "tickets", _completion())
        c.execute("UPDATE work_ticket_completions SET integrated_sha='foreign'")
        with pytest.raises(ValueError, match="Immutable Ticket completion"):
            completions.get_completions(c, "run-1", "acceptance", "tickets", identity)


def test_completion_limit_fails_without_truncating_or_replacing_previous_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(_open(tmp_path / "state.sqlite3", create=True)) as c, c:
        identity = completions.put_completions(c, "run-1", "acceptance", "tickets", _completion())
        monkeypatch.setattr(completions, "MAX_COMPLETION_BYTES", 512)
        with pytest.raises(ValueError, match="exceeds"):
            completions.put_completions(c, "run-1", "acceptance", "tickets", [{"integrated_sha": "x" * 512}])
        assert completions.get_completions(c, "run-1", "acceptance", "tickets", identity) == _completion()


def test_run_binding_references_its_original_input_version_after_current_changes(tmp_path: Path) -> None:
    from agent_run.sqlite_state import SQLiteStateRepository

    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    parent = {"number": 1, "revision": "old-parent", "title": "Original", "body": "Original scope"}
    review = {"parent_revision": "old-parent", "ticket_graph_revision": "graph", "reviewed_head_sha": "reviewed"}
    repository.save("run-1", {"run_id": "run-1", "parent": parent,
        "ticket_graph": {"revision": "graph", "ordered_ticket_numbers": [], "tickets": {}},
        "run_acceptance": {"phase": "accepted", "acceptance_record": review}})
    changed = {"parent": {**parent, "revision": "new-parent", "body": "New scope"}}
    repository.save_objects("run-1", changed, fields={"parent"})
    reopened = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    restored = reopened.load("run-1")
    assert restored is not None
    assert restored["run_acceptance"]["acceptance_record"] == review
    assert restored["parent"] == changed["parent"]
    with closing(sqlite3.connect(reopened.path)) as c, c:
        c.execute("UPDATE work_job_acceptance_record SET parent_version_id='unrelated'")
    with pytest.raises(ValueError, match="immutable input version"):
        reopened.load("run-1")
    with closing(sqlite3.connect(reopened.path)) as c, c:
        c.execute("UPDATE work_job_acceptance_record SET parent_version_id=NULL,value_parent_revision='missing-parent'")
    with pytest.raises(ValueError, match="immutable parent input version"):
        reopened.load("run-1")


@pytest.mark.parametrize("owner,field", [("run_acceptance", "acceptance_record"),
                                         ("run_publication", "record"), ("run_publication", "merge_intent")])
@pytest.mark.parametrize("revision_key", ["parent_revision", "ticket_graph_revision"])
def test_run_binding_rejects_missing_input_without_replacing_previous_record(
    tmp_path: Path, owner: str, field: str, revision_key: str,
) -> None:
    from agent_run.sqlite_state import SQLiteStateRepository

    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    boundary = {"parent_revision": "parent", "ticket_graph_revision": "graph"}
    record = {"acceptance_boundary": boundary} if field == "merge_intent" else boundary
    original = {"run_id": "run-1", "parent": {"number": 1, "revision": "parent", "title": "Scope", "body": "Original"},
                "ticket_graph": {"revision": "graph", "ordered_ticket_numbers": [], "tickets": {}},
                owner: {"phase": "pending", field: record}}
    repository.save("run-1", original)
    changed = deepcopy(original[owner])
    target = changed[field]["acceptance_boundary"] if field == "merge_intent" else changed[field]
    target[revision_key] = "missing-input"
    with pytest.raises(ValueError, match="immutable.*input version"):
        repository.save_objects("run-1", {owner: changed}, fields={owner})
    assert SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024).load("run-1") == original
