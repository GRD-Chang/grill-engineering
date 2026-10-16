"""精确 Run 定位保留本票、活动对象与 Resume 历史的同源选择。"""
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run.sqlite_state import SQLiteStateRepository
from support.work_records import attempt, invocation
from support.state import resume_audit_for_successors


def _state() -> dict[str, Any]:
    jobs = {str(number): {"ticket_number": number, "semantic_attempt_history": [
        attempt(f"ticket:{number}", context={"body": "target archive" if number == 2 else "sibling archive"}),
    ]} for number in (2, 3)}
    jobs["2"]["human_blocker_history"] = [{"phase": "reviewing", "human_blockers": ["Need input"]}]
    current = invocation("ticket:2", "current", context={"body": "target"})
    active = invocation("ticket:3", "active", context={"body": "sibling"})
    audited = invocation("ticket:9", "audited", context={"body": "audited"})
    audit = resume_audit_for_successors("run-1", [audited])
    return {
        "active_ticket_job": jobs["3"], "ticket_jobs": jobs,
        "ticket_agent_invocations": {"2": current, "3": active, "9": audited},
        "agent_invocation_history": [
            invocation("ticket:2", "old", context={"body": "old"}), current, audited, active,
        ],
        "resume_audit": audit,
    }


@pytest.mark.parametrize("ticket", [None, 2])
def test_work_selectors_isolate_runs_and_retain_exact_current_and_resume_history(
    tmp_path: Path, ticket: int | None,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = _state()
    repository.save("run-1", original)
    repository.save("other-run", {
        "active_ticket_job": {"ticket_number": 99},
        "agent_invocation_history": [invocation("ticket:99", "foreign")],
    })
    current = repository.load_work("run-1", ticket)
    assert current["active_ticket_job"]["ticket_number"] == 3
    selected = [original["agent_invocation_history"][position] for position in ((1, 2) if ticket == 2 else (1, 2, 3))]
    assert current["agent_invocation_history"] == selected
    assert set(current["ticket_agent_invocations"]) == {"2", "3", "9"}
    assert "semantic_attempt_history" not in current["active_ticket_job"]
    assert "semantic_attempt_history" not in current["ticket_jobs"]["3"]
    assert "semantic_attempt_history" not in current["ticket_jobs"]["2"]
    assert repository.load("run-1")["ticket_jobs"] == original["ticket_jobs"]


@pytest.mark.parametrize("damage", ["active_root", "history_root", "unknown_archive", "wrong_kind", "blob_path"])
def test_work_selectors_reject_invalid_archive_metadata(tmp_path: Path, damage: str) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", _state())
    assert repository.load_work("run-1", 2) is not None
    with closing(sqlite3.connect(repository.path)) as connection:
        if damage == "active_root":
            connection.execute("UPDATE work_job_layout SET active_job_id='missing'")
        elif damage == "history_root":
            connection.execute("UPDATE work_attempt_versions SET logical_bytes=logical_bytes+1")
        elif damage == "unknown_archive":
            connection.execute("UPDATE work_job_archives SET evidence_id='missing' WHERE history_kind='human_blocker_history'")
        elif damage == "wrong_kind":
            connection.execute("UPDATE work_jobs SET presence=CAST(presence AS TEXT)")
        else:
            connection.execute("UPDATE work_job_attempt_history SET attempt_id=CAST(attempt_id AS BLOB)")
        connection.commit()
    with pytest.raises(ValueError, match="Job|Attempt|evidence|presence"):
        if damage in {"unknown_archive", "blob_path"}:
            repository.load("run-1")
        else:
            repository.load_work("run-1", 2)


def test_work_selectors_retain_compatible_additional_index(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = _state()
    repository.save("run-1", original)
    with closing(sqlite3.connect(repository.path)) as connection:
        connection.execute("CREATE INDEX extra_work_lookup ON work_jobs(run_id,owner_kind)")
    current = repository.load_work("run-1", 2)
    assert current["agent_invocation_history"] == original["agent_invocation_history"][1:3]


@pytest.mark.parametrize("with_active", [False, True])
def test_current_invocations_are_loaded_once_with_exact_target_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_active: bool,
) -> None:
    from agent_run import sqlite_work_execution_invocations as invocations

    repository = SQLiteStateRepository(tmp_path)
    original = _state()
    if with_active:
        original["active_agent_invocation"] = original["ticket_agent_invocations"]["3"]
    repository.save("run-1", original)
    reads = []
    load = invocations.load_invocation

    def capture(connection, run_id, key):
        reads.append((run_id, key))
        return load(connection, run_id, key)

    monkeypatch.setattr(invocations, "load_invocation", capture)
    current = repository.load_work("run-1", 2)
    expected = original["agent_invocation_history"][1:4 if with_active else 3]
    assert current["agent_invocation_history"] == expected
    assert current["ticket_agent_invocations"] == original["ticket_agent_invocations"]
    assert sorted(reads) == [("run-1", (f"ticket:{number}", started))
                             for number, started in ((2, "current"), (3, "active"), (9, "audited"))]
    current["ticket_agent_invocations"]["2"]["currentness_boundary"]["fixture"]["body"] = "changed copy"
    assert current["agent_invocation_history"] == expected
