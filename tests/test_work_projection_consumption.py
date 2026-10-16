"""业务消费区分当前预算、明确追加与真正需要读取的旧证据。"""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.human_responses import append_human_response, current_human_response_history
from agent_run.review_budget import TICKET_POLICY, ensure_budget, new_budget, previous_review_context, reset_budget
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt
from agent_run.work_projection import JobHistoryAppend, JobProjection


def projected(values, archives, histories=None):
    owner = JobProjection(values, job_id="ticket:2:generation:1", revision=7, archives=archives)
    reads = []

    def read(_owner, fields):
        values = {}
        for field in fields:
            reads.append(field)
            if histories is None or field not in histories:
                raise AssertionError("Unnecessary old history read: " + field)
            values[field] = deepcopy(histories[field])
        return values

    owner.history_reader = read
    return owner, reads


def test_current_budget_decisions_do_not_read_old_windows_and_plain_missing_history_fails() -> None:
    owner, reads = projected({"review_budget": new_budget(window=501)},
                             {"review_budget_history": (200_000, 500)})
    assert ensure_budget(owner, TICKET_POLICY)["window"] == 501
    assert reads == []
    with pytest.raises(ValueError, match="missing canonical review_budget_history"):
        ensure_budget(dict(owner), TICKET_POLICY)
    owner["review_budget_history"] = []
    with pytest.raises(ValueError, match="unloaded archive"):
        ensure_budget(owner, TICKET_POLICY)


def test_closing_new_attempt_appends_one_fact_without_loading_older_rounds() -> None:
    owner, reads = projected({}, {"semantic_attempt_history": (1_000_000, 500)})
    current = allocate_semantic_attempt(owner, role="development", work_subject="ticket:2", generation=1,
                                        currentness_boundary={"candidate": "exact"}, ordinal=1, budget_window=501)
    assert "semantic_attempt_history" not in owner
    close_semantic_attempt(owner, current, outcome="candidate")
    appended = owner["semantic_attempt_history"]
    assert isinstance(appended, JobHistoryAppend)
    assert appended.base_count == 500
    assert [record["attempt_id"] for record in appended] == [current["attempt_id"]]
    assert reads == []


def test_budget_renewal_appends_window_and_retains_exact_attempt_evidence() -> None:
    old_round = {"attempt_id": "old-exact-round", "status": "completed"}
    owner, reads = projected({"review_budget": new_budget(window=501), "phase": "developing",
                              "modification_attempts": 0, "validation_attempts": 0},
                             {"review_budget_history": (200_000, 500), "semantic_attempt_history": (200, 1)},
                             {"semantic_attempt_history": [old_round]})
    reset_budget(owner, TICKET_POLICY)
    assert ensure_budget(owner, TICKET_POLICY)["window"] == 502
    appended = owner["review_budget_history"]
    assert appended.base_count == 500
    assert len(appended) == 1
    assert appended[0]["review_budget"]["window"] == 501
    assert appended[0]["semantic_attempt_history"] == [old_round]
    assert reads == ["semantic_attempt_history"]


def test_human_response_append_preserves_unread_prefix_until_request_needs_it() -> None:
    first = {"generation": 1, "human_blockers": ["first question"], "response": "first answer"}
    owner, reads = projected({"human_response_generation": 1}, {"human_response_history": (200, 1)},
                             {"human_response_history": [first]})
    append_human_response(owner, ["second question"], "second answer", generation=1)
    assert reads == []
    assert current_human_response_history(owner, generation=2) is None
    assert current_human_response_history(owner, generation=1) == [
        first, {"generation": 1, "human_blockers": ["second question"], "response": "second answer"},
    ]
    assert reads == ["human_response_history"]


def test_next_run_review_reads_only_the_latest_accurate_prior_record() -> None:
    owner, reads = projected({"review_budget": new_budget(window=501), "acceptance_generation": 1},
                             {"review_budget_history": (1_000_000, 500)})
    record = {"artifact": {"summary": "previous exact verdict"},
              "review_identity": {"reviewed_head_sha": "reviewed-old-head"}}
    requested = []

    def previous(current):
        requested.append((current.job_id, current.revision))
        return deepcopy(record)

    owner.previous_review_reader = previous
    assert previous_review_context(owner) == {"artifact": record["artifact"], "identity": record["review_identity"]}
    assert requested == [("ticket:2:generation:1", 7)]
    assert reads == []

    pending = {"artifact": {"summary": "newer unsaved verdict"},
               "review_identity": {"reviewed_head_sha": "reviewed-newer-head"}}
    owner["review_budget_history"] = JobHistoryAppend(
        [{"review_budget": {"review_artifacts": [pending]}}], base_count=500, base_bytes=1_000_000,
    )
    assert previous_review_context(owner) == {
        "artifact": pending["artifact"], "identity": pending["review_identity"],
    }
    assert requested == [("ticket:2:generation:1", 7)]
    assert reads == []


def test_current_comparison_keeps_resume_authority_without_comparing_unread_old_events() -> None:
    from agent_run.object_state import current_projection_facts

    old, latest = {"sequence": 1, "resume_id": "r1"}, {"sequence": 2, "resume_id": "r2"}
    retained = {"resume_audit": {"total": 2, "rolling_digest": "exact-current-chain", "history": [old, latest]},
                "human_response_audit": {"r1": "old reply", "r2": "current reply"}}
    current = {"resume_audit": {"total": 2, "rolling_digest": "exact-current-chain", "history": [latest]},
               "human_response_audit": {"r2": "current reply"}}
    assert current_projection_facts(retained) == current_projection_facts(current)
    changed_chain = deepcopy(current)
    changed_chain["resume_audit"]["rolling_digest"] = "another-authority"
    assert current_projection_facts(retained) != current_projection_facts(changed_chain)
    invocation = {"work_subject": "ticket:2", "started_at": "exact-start", "resume_id": "r1"}
    retained["active_agent_invocation"] = invocation
    current["active_agent_invocation"] = invocation
    assert current_projection_facts(retained) != current_projection_facts(current)


@pytest.mark.parametrize("count", [0, 1, 500])
def test_timeline_and_same_thread_resume_read_only_exact_thread_tail(tmp_path: Path, count: int) -> None:
    from agent_run.change_delivery_threads import latest_reviewer_thread
    from agent_run.sqlite_state import SQLiteStateRepository
    from agent_run.state import _thread_id_for_role
    from agent_run.state_limits import query_scope

    repository = SQLiteStateRepository(tmp_path)
    threads = [f"reviewer-{position:04d}" for position in range(count)]
    repository.save("run-threads", {"parent_job": {"phase": "reviewing", "reviewer_thread_ids": threads}})
    current = repository.load_objects("run-threads")[0]["parent_job"]
    assert "reviewer_thread_ids" not in current

    def no_archive(*_args, **_kwargs):
        pytest.fail("A Thread tail query expanded the original archive")

    current.history_reader = no_archive
    before = deepcopy(current)
    with query_scope(max_bytes=4096) as budget:
        assert _thread_id_for_role(current, "reviewer") == (threads[-1] if threads else None)
        assert latest_reviewer_thread(current) == (threads[-1] if threads else None)
        assert budget.rows < 20
    assert current == before
    assert current.loaded_histories == before.loaded_histories

    # A known, unsaved suffix takes precedence without a stale database read.
    size, total = current.archives["reviewer_thread_ids"]
    current["reviewer_thread_ids"] = JobHistoryAppend(["next-reviewer"], base_bytes=size, base_count=total)
    current.latest_thread_reader = no_archive
    assert latest_reviewer_thread(current) == "next-reviewer"


def test_requested_histories_share_one_snapshot_and_leave_other_archives_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3
    from agent_run.sqlite_state import SQLiteStateRepository

    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"parent_job": {
        "reviewer_thread_ids": ["reviewer-1"],
        "development_thread_history": ["developer-1"],
        "prior_reviewer_thread_ids": ["older-reviewer"],
    }})
    statements = []
    connect = sqlite3.connect

    def capture(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture)
    with repository.work_session():
        current = repository.load_objects("run-1")[0]["parent_job"]
        statements.clear()
        current.materialize_histories({"reviewer_thread_ids", "development_thread_history"})
        assert current["reviewer_thread_ids"] == ["reviewer-1"]
        assert current["development_thread_history"] == ["developer-1"]
        assert current.loaded_histories == {"reviewer_thread_ids", "development_thread_history"}
        assert "prior_reviewer_thread_ids" not in current
        assert statements.count("BEGIN") == 1
        assert statements.count("ROLLBACK") == 1
        statements.clear()
        current.materialize_histories({"reviewer_thread_ids", "development_thread_history"})
        assert statements == []


def test_history_batch_rejects_stale_identity_without_materializing(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing
    from agent_run.sqlite_state import SQLiteStateRepository

    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"parent_job": {
        "reviewer_thread_ids": ["reviewer-1"], "development_thread_history": ["developer-1"],
    }})
    current = repository.load_objects("run-1")[0]["parent_job"]
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("UPDATE work_jobs SET revision=revision+1 WHERE run_id=? AND job_id=?",
                           ("run-1", current.job_id))
        connection.commit()
    with pytest.raises(ValueError, match="Job changed before its exact historical query"):
        current.materialize_histories({"reviewer_thread_ids", "development_thread_history"})
    assert not current.loaded_histories


def test_history_batch_does_not_renew_the_query_byte_budget(tmp_path: Path) -> None:
    from agent_run.sqlite_state import SQLiteStateRepository
    from agent_run.state_limits import QueryLimitError, query_scope

    repository = SQLiteStateRepository(tmp_path)
    histories = {
        field: [f"{field}-{index}-" + "x" * 100 for index in range(20)]
        for field in ("reviewer_thread_ids", "development_thread_history")
    }
    repository.save("run-1", {"parent_job": histories})
    costs = []
    for field in histories:
        current = repository.load_objects("run-1")[0]["parent_job"]
        with query_scope() as budget:
            assert current.materialize(field) == histories[field]
            costs.append(budget.read_bytes)
    current = repository.load_objects("run-1")[0]["parent_job"]
    with query_scope(max_bytes=max(costs)), pytest.raises(QueryLimitError, match="byte read limit"):
        current.materialize_histories(set(histories))
    assert not current.loaded_histories
