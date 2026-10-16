"""同身份查询基线补读历史不扩大薄 Job 提交范围。"""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.work_projection import JobProjection


@pytest.mark.parametrize("query", ["work", "objects", "fields"])
def test_only_write_transactions_retain_an_independent_commit_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    from agent_run import sqlite_state

    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"status": "active", "timeline": []})
    copies = []
    copy_tree = sqlite_state.copy_json_tree

    def copying(value):
        copies.append(True)
        return copy_tree(value)

    monkeypatch.setattr(sqlite_state, "copy_json_tree", copying)

    def load():
        if query == "work":
            return repository.load_work("run-1", None, thin_timeline=True)
        if query == "objects":
            return repository.load_objects("run-1", thin_timeline=True)[0]
        return repository.load_fields("run-1", {"status", "timeline"}, thin_timeline=True)

    assert load()["status"] == "active"
    assert not copies
    with pytest.raises(ValueError, match="current transaction"):
        repository.current_work_baseline("run-1")
    with repository.atomic():
        loaded = load()
        assert copies == [True]
        baseline = repository.current_work_baseline("run-1")
        loaded["status"] = "blocked"
        assert baseline["status"] == "active"
        repository._local.connection.execute("UPDATE runs SET status='operator_stopped' WHERE run_id='run-1'")
        with pytest.raises(ValueError, match="current transaction"):
            repository.current_work_baseline("run-1")


@pytest.mark.parametrize("threads", [[], ["prior-reviewer"]], ids=["empty", "retained"])
def test_baseline_only_thread_read_preserves_unread_job_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, threads: list[str],
) -> None:
    from agent_run import sqlite_change_job_queries as queries

    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"ticket_jobs": {"2": {
        "ticket_number": 2, "phase": "developing", "reviewer_thread_ids": threads,
        "human_blocker_history": [{"question": "original blocker evidence"}],
    }}})
    original = repository.load("run-1")
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        assert current is not None
        proposed = deepcopy(current)
        baseline = repository.current_work_baseline("run-1")
        baseline_job = baseline["ticket_jobs"]["2"]
        proposed_job = proposed["ticket_jobs"]["2"]
        assert isinstance(baseline_job, JobProjection)
        assert isinstance(proposed_job, JobProjection)
        # The previous Timeline marker may read this exact baseline after
        # the proposed view was copied; that read grants no removal intent.
        assert baseline_job.materialize("reviewer_thread_ids") == threads
        assert "reviewer_thread_ids" not in proposed_job
        proposed_job["phase"] = "reviewing"

        def reject_history_decode(*_args, **_kwargs):
            pytest.fail("A current Job save decoded its untouched archive")

        with monkeypatch.context() as unread:
            unread.setattr(queries, "_load_history", reject_history_decode)
            repository.save_work("run-1", proposed, baseline)
    assert original is not None
    original["ticket_jobs"]["2"]["phase"] = "reviewing"
    assert repository.load("run-1") == original


@pytest.mark.parametrize("damage", ["owner", "metadata"])
def test_additionally_read_baseline_requires_matching_owner_and_archive_scope(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"ticket_jobs": {"2": {
        "ticket_number": 2, "phase": "developing", "reviewer_thread_ids": ["retained"],
    }}})
    original = repository.load("run-1")
    with pytest.raises(ValueError, match="Cannot remove unread Job history"):
        with repository.atomic():
            current = repository.load_work("run-1", 2)
            assert current is not None
            proposed = deepcopy(current)
            baseline = repository.current_work_baseline("run-1")
            baseline_job = baseline["ticket_jobs"]["2"]
            assert isinstance(baseline_job, JobProjection)
            baseline_job.materialize("reviewer_thread_ids")
            if damage == "owner":
                baseline_job.job_id = "ticket:3:generation:1"
            else:
                size, count = baseline_job.archives["reviewer_thread_ids"]
                baseline_job.archives["reviewer_thread_ids"] = size + 1, count
            proposed["ticket_jobs"]["2"]["phase"] = "reviewing"
            repository.save_work("run-1", proposed, baseline)
    assert repository.load("run-1") == original


@pytest.mark.parametrize("damage", ["position", "length", "missing", "empty-count"])
def test_exact_thread_tail_rejects_damage_in_its_read_scope(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"ticket_jobs": {"2": {
        "ticket_number": 2, "reviewer_thread_ids": ["exact-reviewer"],
    }}})
    with repository.atomic():
        state = repository.load_work("run-1", 2)
        assert state is not None
        job = state["ticket_jobs"]["2"]
        assert isinstance(job, JobProjection) and job.latest_thread_reader is not None
        c = repository._local.connection
        identity = ("run-1", job.job_id, "reviewer_thread_ids")
        predicate = " WHERE run_id=? AND job_id=? AND history_kind=?"
        if damage == "position":
            c.execute("UPDATE work_job_archives SET position=position+1" + predicate, identity)
        elif damage == "length":
            c.execute("UPDATE work_job_archives SET logical_bytes=logical_bytes+1" + predicate, identity)
        elif damage == "missing":
            c.execute("DELETE FROM work_job_archives" + predicate, identity)
        else:
            c.execute("UPDATE work_job_archive_roots SET item_count=0,logical_bytes=2" + predicate, identity)
        with pytest.raises(ValueError, match="Invalid exact Thread history tail"):
            job.latest_thread_reader(job, "reviewer_thread_ids")


def test_exact_thread_reader_rejects_other_fields_and_stale_job_revision(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"ticket_jobs": {"2": {
        "ticket_number": 2, "phase": "developing", "reviewer_thread_ids": ["exact-reviewer"],
    }}})
    state = repository.load_objects("run-1")[0]
    job = state["ticket_jobs"]["2"]
    assert isinstance(job, JobProjection) and job.latest_thread_reader is not None
    with pytest.raises(ValueError, match="Unsupported exact Thread history"):
        job.latest_thread_reader(job, "human_blocker_history")
    with repository.atomic():
        current = repository.load_work("run-1", 2)
        assert current is not None
        baseline = repository.current_work_baseline("run-1")
        current["ticket_jobs"]["2"]["phase"] = "reviewing"
        repository.save_work("run-1", current, baseline)
    with pytest.raises(ValueError, match="Job changed before its exact Thread query"):
        job.latest_thread_reader(job, "reviewer_thread_ids")
