"""精确 Run 定位保留本票、活动对象与 Resume 历史的同源选择。"""
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run.sqlite_encoding import encode_json
from agent_run.sqlite_state import SQLiteStateRepository


def _state() -> dict[str, Any]:
    return {
        "active_ticket_job": {"ticket_number": 3, "semantic_attempt_history": [{"body": "active archive"}]},
        "ticket_jobs": {
            "2": {"ticket_number": 2, "semantic_attempt_history": [{"body": "target archive"}], "human_blocker_history": []},
            "3": {"ticket_number": 3, "semantic_attempt_history": [{"body": "sibling archive"}]},
        },
        "ticket_agent_invocations": {
            "2": {"work_subject": "ticket:2", "started_at": "current"},
            "3": {"work_subject": "ticket:3", "started_at": "active"},
            "9": {"work_subject": "ticket:9", "started_at": "audited"},
        },
        "agent_invocation_history": [
            {"work_subject": "ticket:2", "started_at": "old", "body": "old"},
            {"work_subject": "ticket:2", "started_at": "current", "body": "target"},
            {"work_subject": "ticket:9", "started_at": "audited", "resume_id": "resume-9", "body": "audited"},
            {"work_subject": "ticket:3", "started_at": "active", "body": "unreferenced active history"},
        ],
        "resume_audit": {"history": [{"resume_id": "resume-9", "successor_invocation_started_at": "audited"}]},
    }


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("ticket", [None, 2])
def test_work_selectors_isolate_runs_and_retain_exact_current_and_resume_history(
    tmp_path: Path, aggregate: bool, ticket: int | None,
) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    repository.save("other-run", {
        "active_ticket_job": {"ticket_number": 99},
        "agent_invocation_history": [{"work_subject": "ticket:99", "started_at": "foreign"}],
    })
    repository._aggregate_rows = aggregate
    current = repository.load_work("run-1", ticket)
    assert current["active_ticket_job"]["ticket_number"] == 3
    selected = [original["agent_invocation_history"][position] for position in ((1, 2) if ticket == 2 else (2,))]
    assert current["agent_invocation_history"] == selected
    assert set(current["ticket_agent_invocations"]) == ({"2", "3", "9"} if ticket == 2 else {"3", "9"})
    assert "semantic_attempt_history" not in current["active_ticket_job"]
    assert "semantic_attempt_history" not in current["ticket_jobs"]["3"]
    if ticket == 2:
        assert current["ticket_jobs"]["2"]["semantic_attempt_history"] == original["ticket_jobs"]["2"]["semantic_attempt_history"]
    else:
        assert "semantic_attempt_history" not in current["ticket_jobs"]["2"]


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("damage", ["active_root", "history_root", "unknown_archive", "wrong_kind", "blob_path"])
def test_work_selectors_reject_invalid_archive_metadata(tmp_path: Path, aggregate: bool, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    repository.save("run-1", _state())
    repository._aggregate_rows = aggregate
    assert repository.load_work("run-1", 2) is not None
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage in {"active_root", "history_root", "unknown_archive"}:
            path = {"active_root": ("active_ticket_job",), "history_root": ("agent_invocation_history",),
                    "unknown_archive": ("ticket_jobs", "2", "unknown_archive")}[damage]
            connection.execute("INSERT INTO run_job_history VALUES (?,?,?,?)", ("run-1", encode_json(path), 2, 0))
        elif damage == "wrong_kind":
            connection.execute("UPDATE run_objects SET kind='scalar',value_json='0' WHERE object_path=?",
                               (encode_json(("ticket_jobs", "2", "human_blocker_history")),))
        else:
            connection.execute("UPDATE run_job_history SET object_path=CAST(object_path AS BLOB)")
        connection.commit()
    with pytest.raises(ValueError, match="Job history size|object parent|list item|BLOB"):
        repository.load_work("run-1", 2)


def test_work_selectors_retain_compatible_additional_index(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    original = _state()
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("CREATE INDEX extra_work_lookup ON run_objects(run_id,kind)")
    current = repository.load_work("run-1", 2)
    assert current["agent_invocation_history"] == original["agent_invocation_history"][1:3]
