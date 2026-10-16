"""薄 Repair 保存与退休保留同一 Attempt 及其原始返回成员。"""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from agent_run.agent_return import store_agent_return
from agent_run.agents import DevelopmentResult
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt, retire_semantic_attempt_owner
from agent_run.sqlite_change_jobs import projection_archives
from agent_run.sqlite_state import SQLiteStateRepository


def test_unread_repair_archive_survives_current_commit_and_retirement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_run import sqlite_work_execution_attempts as attempts

    repository = SQLiteStateRepository(tmp_path)
    run_id = "run-repair-archive"
    repair = {"repair_generation": 1, "phase": "developing"}
    attempt = allocate_semantic_attempt(
        repair, role="development", work_subject=f"run-repair:{run_id}", generation=1,
        currentness_boundary={"candidate_sha": "original-candidate"}, ordinal=1,
    )
    summary = "Repaired the original accumulated behavior."
    attempt["returned_result"] = store_agent_return(DevelopmentResult("repair-developer", summary), attempt)
    repository.save(run_id, {"run_id": run_id, "run_acceptance": {"phase": "repairing", "repair_job": repair}})

    current = repository.load_objects(run_id)[0]
    repair = current["run_acceptance"]["repair_job"]
    close_semantic_attempt(repair, repair["pending_semantic_attempt"], outcome="candidate",
                           result={"development_summary": summary})
    repair["phase"] = "candidate"
    repository.save_objects(run_id, current, fields={"run_acceptance"}, archive_scope=projection_archives(current))
    original = deepcopy(repository.load(run_id)["run_acceptance"]["repair_job"]["semantic_attempt_history"])

    current = repository.load_objects(run_id)[0]
    repair = current["run_acceptance"]["repair_job"]
    assert "semantic_attempt_history" not in repair
    repair["phase"] = "reviewing"
    scope = projection_archives(current)
    with pytest.raises(ValueError, match="Cannot remove unread Job history"):
        repository.save_objects(run_id, current, fields={"run_acceptance"}, archive_scope={})

    def reject_history_decode(*_args, **_kwargs):
        pytest.fail("Current Repair save decoded an unread Attempt")

    with monkeypatch.context() as unread:
        unread.setattr(attempts, "load_version", reject_history_decode)
        repository.save_objects(run_id, current, fields={"run_acceptance"}, archive_scope=scope)

    retire_semantic_attempt_owner(current, repair, owner_kind="run_repair",
                                 work_subject=f"run-repair:{run_id}", generation=1)
    repository.append_array(run_id, "retired_semantic_attempt_owners", current["retired_semantic_attempt_owners"])
    reopened = SQLiteStateRepository(tmp_path).load(run_id)
    assert reopened["run_acceptance"]["repair_job"]["semantic_attempt_history"] == original
    assert reopened["retired_semantic_attempt_owners"][0]["semantic_attempt_history"] == original
    assert original[0]["development_summary"] == summary

    with closing(sqlite3.connect(repository.path)) as connection:
        versions = connection.execute(
            "SELECT DISTINCT h.version_id,e.source_member,source.kind FROM work_job_attempt_history h "
            "JOIN work_attempt_versions v USING(run_id,attempt_id,version_id) "
            "JOIN work_evidence e ON e.run_id=v.run_id AND e.evidence_id=v.development_summary_evidence_id "
            "JOIN work_evidence source ON source.run_id=e.run_id AND source.evidence_id=e.source_evidence_id "
            "WHERE h.run_id=? AND h.attempt_id=?", (run_id, original[0]["attempt_id"]),
        ).fetchall()
    assert len(versions) == 1
    assert versions[0][1:] == ("summary", "returned_result")
