"""Parent and Ticket commits maintain one authority and both query indexes."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.sqlite_integrity import field_size_proof, require_field_sizes, source_objects_digest
from agent_run.sqlite_objects import _rows
from agent_run.sqlite_state import SQLiteStateRepository


@pytest.mark.parametrize("aggregate", [True, False])
def test_alternating_work_and_owned_commits_reopen_complete_evidence(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    repository._aggregate_rows = aggregate
    expected = {
        "phase": "ticket_delivery", "ticket_jobs": {
            "2": {"ticket_number": 2, "phase": "developing", "semantic_attempt_history": []},
            "3": {"ticket_number": 3, "phase": "developing", "human_blocker_history": [{"body": "证据" * 5000}]},
        },
        "ticket_agent_invocations": {}, "agent_invocation_history": [], "timeline": [],
    }
    repository.save("run-1", expected)
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        current = deepcopy(baseline)
        assert "human_blocker_history" not in current["ticket_jobs"]["3"]
        current["ticket_jobs"]["3"]["phase"] = "reviewing"
        current["ticket_jobs"]["2"]["semantic_attempt_history"] = [{"role": "developer", "status": "completed", "ordinal": 1}]
        current["agent_invocation_history"].append({"work_subject": "parent-only", "started_at": "2026-01-01T00:00:00Z"})
        current["timeline"].append({"event": "work"})
        repository.save_work("run-1", current, baseline=baseline)
    expected["ticket_jobs"]["3"]["phase"] = "reviewing"
    expected["ticket_jobs"]["2"]["semantic_attempt_history"] = current["ticket_jobs"]["2"]["semantic_attempt_history"]
    expected["agent_invocation_history"] = current["agent_invocation_history"]
    expected["timeline"] = current["timeline"]
    assert repository.load("run-1") == expected
    repository.save_objects("run-1", {"phase": "run_acceptance", "ticket_jobs": {"2": {"ticket_number": 2, "phase": "completed"}}},
                            fields={"phase", "ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    expected["phase"] = "run_acceptance"
    expected["ticket_jobs"]["2"] = {"ticket_number": 2, "phase": "completed"}
    repository.append_array("run-1", "timeline", [{"event": "parent"}])
    expected["timeline"].append({"event": "parent"})
    reopened = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    assert reopened.load("run-1") == expected
    assert reopened.load_objects("run-1")[0]["ticket_jobs"] == expected["ticket_jobs"]
    assert reopened.load_work("run-1", 3)["ticket_jobs"]["3"] == expected["ticket_jobs"]["3"]
    with reopened._reading() as connection:
        assert connection is not None
        size = require_field_sizes(connection, "run-1", max_bytes=reopened.max_bytes)
        assert size == reopened._check_size(expected)
        assert connection.execute("SELECT logical_bytes FROM run_sizes").fetchone() == (size,)
        assert connection.execute("SELECT content_digest FROM run_object_index WHERE object_path='[\"ticket_jobs\"]'").fetchone() == (
            source_objects_digest(_rows(connection, "run-1", '["ticket_jobs"]')),)


@pytest.mark.parametrize("aggregate", [True, False])
def test_work_updates_hidden_active_archive_owner_with_complete_field_proof(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4 * 1024 * 1024)
    repository._aggregate_rows = aggregate
    job = {"ticket_number": 3, "phase": "developing", "human_blocker_history": [{"body": "retained evidence" * 100}]}
    expected = {"active_ticket_job": deepcopy(job), "ticket_jobs": {"2": {}, "3": deepcopy(job)}}
    repository.save("run-1", expected)
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        assert "human_blocker_history" not in baseline["active_ticket_job"]
        current = deepcopy(baseline)
        current["active_ticket_job"]["phase"] = "reviewing"
        repository.save_work("run-1", current, baseline)
    expected["active_ticket_job"]["phase"] = "reviewing"
    assert repository.load("run-1") == expected
    with repository._reading() as connection:
        assert connection is not None
        assert require_field_sizes(connection, "run-1", max_bytes=repository.max_bytes) == repository._check_size(expected)
        digest, child_count = connection.execute(
            "SELECT content_digest,child_count FROM run_object_index WHERE object_path='[\"active_ticket_job\"]'",
        ).fetchone()
        assert child_count == len(expected["active_ticket_job"])
        assert digest == source_objects_digest(_rows(connection, "run-1", '["active_ticket_job"]'))


@pytest.mark.parametrize("aggregate", [True, False])
def test_work_history_generic_json_entries_have_no_invocation_locator(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository._aggregate_rows = aggregate
    repository.save("run-1", {"agent_invocation_history": []})
    with repository.atomic():
        baseline = repository.load_work("run-1", 2)
        assert baseline is not None
        current = deepcopy(baseline)
        current["agent_invocation_history"].extend([None, "historical note", 2])
        repository.save_work("run-1", current, baseline)
    assert repository.load("run-1") == current
    with repository._reading() as connection:
        assert connection is not None
        assert connection.execute("SELECT count(*) FROM run_object_index WHERE invocation_id IS NOT NULL").fetchone() == (0,)

@pytest.mark.parametrize("aggregate", [True, False])
def test_work_commit_borrows_only_its_isolated_current_transaction_baseline(tmp_path: Path, aggregate: bool) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository._aggregate_rows = aggregate
    original = {"ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing"}}, "timeline": []}
    repository.save("run-1", original)
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        baseline = repository.work_baseline("run-1", current)
        assert baseline is repository._local.work_view.baseline
        current["ticket_jobs"]["2"]["phase"] = "reviewing"
        assert baseline == original
        repository.save_work("run-1", current, baseline)
    assert repository.load("run-1") == current
    # Any intervening SQL write invalidates the transaction view; copying
    # the supplied snapshot cannot substitute for a new save baseline.
    with pytest.raises(ValueError, match="current transaction baseline"):
        with repository.atomic():
            loaded = repository.load_work("run-1", 2)
            before = repository.work_baseline("run-1", loaded)
            repository._local.connection.execute("UPDATE run_objects SET value_json=value_json WHERE object_path='[\"timeline\"]'")
            copied = repository.work_baseline("run-1", loaded)
            assert copied == loaded and copied is not loaded
            repository.save_work("run-1", copied, before)
    assert repository.load("run-1") == current
    independent = repository.work_baseline("run-1", current)
    independent["ticket_jobs"]["2"]["phase"] = "caller mutation"
    assert repository.load("run-1") == current


@pytest.mark.parametrize("aggregate", [True, False])
def test_noop_work_commit_does_not_create_revision_but_repairs_actual_metadata_drift(tmp_path: Path, aggregate: bool) -> None:
    from agent_run.sqlite_watcher import SQLiteWatcher

    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository._aggregate_rows = aggregate
    original = {"status": "active", "ticket_agent_invocations": {}, "agent_invocation_history": [], "timeline": []}
    repository.save("run-1", original)
    with SQLiteWatcher(repository.path) as watcher:
        before = watcher.version()
        with repository.atomic():
            current = repository.load_work("run-1", 2)
            baseline = repository.work_baseline("run-1", current)
            changes = repository._local.connection.total_changes
            repository.save_work("run-1", current, baseline)
            assert repository._local.connection.total_changes == changes
        assert watcher.version() == before
        with repository.atomic():
            repository._local.connection.execute("UPDATE runs SET status='stale metadata'")
        drift = watcher.version()
        assert drift != before
        with repository.atomic():
            current = repository.load_work("run-1", 2)
            repository.save_work("run-1", current, repository.work_baseline("run-1", current))
            assert repository._local.connection.execute("SELECT status FROM runs").fetchone() == ("active",)
        assert watcher.version() != drift
    assert repository.load("run-1") == original


@pytest.mark.parametrize("digest", ["A" * 64, "g" * 64, "ｆ" * 64, "f" * 63, "f" * 65, "f" * 63 + "\n"])
def test_field_digest_rejects_noncanonical_hex_even_with_matching_proof(tmp_path: Path, digest: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=4096)
    repository.save("run-1", {"phase": "active"})
    with repository.atomic():
        connection = repository._local.connection
        path = '["phase"]'
        size, count, kind, encoded = connection.execute(
            "SELECT i.logical_bytes,i.child_count,o.kind,o.value_json FROM run_object_index i "
            "JOIN run_objects o USING(run_id,object_path) WHERE i.run_id=? AND i.object_path=?", ("run-1", path),
        ).fetchone()
        proof = field_size_proof(path, size, digest, (kind, encoded), count)
        connection.execute("UPDATE run_object_index SET content_digest=?,size_proof=? WHERE run_id=? AND object_path=?",
                           (digest, proof, "run-1", path))
    with pytest.raises(ValueError, match="field size proof"):
        repository.load_objects("run-1")
