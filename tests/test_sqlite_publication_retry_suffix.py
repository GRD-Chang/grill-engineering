"""已知追加后缀中的 Publication 重试只更新准确成员。"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run.agent_return import store_agent_return
from agent_run.agents import PublicationResult
from agent_run.publication_operation_retry import record_publication_operation_failure
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt
from agent_run.sqlite_change_jobs import projection_archives
from agent_run.sqlite_encoding import copy_json_tree
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.work_projection import JobHistoryAppend, JobProjection, publication_attempt_for_current_ordinal, restore_work_scopes
from support.cli_payloads import publication
from support.work_records import attempt as stored_attempt


_RUN = "run-retry"
_JOB = "ticket:3:generation:1"


def _save(repository: SQLiteStateRepository, state: dict[str, Any]) -> None:
    repository.save_objects(_RUN, state, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"3"}},
                            archive_scope=projection_archives(state))


def _pending(root: Path) -> tuple[SQLiteStateRepository, dict[str, Any]]:
    repository = SQLiteStateRepository(root)
    job: dict[str, Any] = {"ticket_number": 3, "publication_attempts": 1, "phase": "accepted"}
    boundary = {"base_sha": "original-base", "candidate_sha": "original-candidate"}
    attempt = allocate_semantic_attempt(job, role="publication", work_subject="ticket:3", generation=1,
                                       currentness_boundary=boundary, ordinal=1)
    invocation = {"work_subject": "ticket:3", "started_at": "original-start", "status": "completed",
                  "generation": 1, "role": "publication", "phase": "publication", "mode": "fresh",
                  "currentness_boundary": boundary, "semantic_attempt": deepcopy(attempt)}
    repository.save(_RUN, {"ticket_jobs": {"3": job}, "ticket_agent_invocations": {"3": invocation},
                           "agent_invocation_history": [deepcopy(invocation)]})
    current = repository.load_objects(_RUN)[0]
    pending = current["ticket_jobs"]["3"]["pending_semantic_attempt"]
    pending["returned_result"] = store_agent_return(PublicationResult("original-publisher", publication()), pending)
    _save(repository, current)
    return repository, current


def _close(state: dict[str, Any]) -> JobProjection:
    job = state["ticket_jobs"]["3"]
    assert isinstance(job, JobProjection)
    close_semantic_attempt(job, job["pending_semantic_attempt"], outcome="publication_artifact",
                           result={"publication": publication()})
    job["phase"] = "publishing"
    assert isinstance(job["semantic_attempt_history"], JobHistoryAppend)
    return job


@pytest.mark.parametrize("copy_kind", ["same", "deepcopy", "json-tree", "restored"])
def test_saved_suffix_accepts_continuous_retry_and_keeps_original_return_and_invocation(
    tmp_path: Path, copy_kind: str,
) -> None:
    repository, current = _pending(tmp_path)
    job = _close(current)
    _save(repository, current)
    with closing(sqlite3.connect(repository.path)) as c:
        startup = c.execute("SELECT attempt_version_id,raw_result_evidence_id FROM work_invocations").fetchone()
        source = c.execute("SELECT evidence_id FROM work_attempt_returns").fetchone()
        original_member = c.execute("SELECT e.source_evidence_id,e.source_member FROM work_job_attempt_history h "
                                    "JOIN work_attempt_versions v USING(run_id,attempt_id,version_id) "
                                    "JOIN work_evidence e ON e.run_id=v.run_id AND e.evidence_id=v.publication_evidence_id").fetchone()
    for count in (1, 2):
        job = current["ticket_jobs"]["3"]
        record_publication_operation_failure(job, TimeoutError("original transport timeout"))
        assert job.publication_update is not None
        before, updated = job.publication_update
        assert updated["publication_operation_retry"] == {"attempts": count, "limit": 5}
        assert before.get("publication_operation_retry") == (None if count == 1 else {"attempts": 1, "limit": 5})
        if copy_kind == "deepcopy":
            current = deepcopy(current)
        elif copy_kind == "json-tree":
            current = copy_json_tree(current)
        elif copy_kind == "restored":
            copied = {"ticket_jobs": {"3": dict(current["ticket_jobs"]["3"])}}
            restore_work_scopes(copied, current)
            current = copied
        _save(repository, current)
    reopened = SQLiteStateRepository(tmp_path).load(_RUN)
    assert reopened is not None
    owner = reopened["ticket_jobs"]["3"]
    assert owner["publication_operation_retry"] == {"attempts": 2, "limit": 5}
    assert owner["last_publication_error"] == "original transport timeout"
    assert owner["semantic_attempt_history"][-1]["publication"] == publication()
    assert owner["semantic_attempt_history"][-1]["publication_operation_retry"] == {"attempts": 2, "limit": 5}
    assert reopened["ticket_agent_invocations"]["3"]["semantic_attempt"]["status"] == "pending"
    with closing(sqlite3.connect(repository.path)) as c:
        assert c.execute("SELECT attempt_version_id,raw_result_evidence_id FROM work_invocations").fetchone() == startup
        assert c.execute("SELECT evidence_id FROM work_attempt_returns").fetchone() == source
        member = c.execute("SELECT e.source_evidence_id,e.source_member FROM work_job_attempt_history h "
                           "JOIN work_attempt_versions v USING(run_id,attempt_id,version_id) "
                           "JOIN work_evidence e ON e.run_id=v.run_id AND e.evidence_id=v.publication_evidence_id").fetchone()
        assert original_member[0] == source[0]
        assert member == original_member


@pytest.mark.parametrize("retry_before_insert", [False, True])
def test_newly_closed_suffix_does_not_use_existing_attempt_retry_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_before_insert: bool,
) -> None:
    from agent_run import sqlite_work_execution_attempts as attempts

    repository, current = _pending(tmp_path)
    job = _close(current)
    assert publication_attempt_for_current_ordinal(job) is not None
    if retry_before_insert:
        record_publication_operation_failure(job, OSError("transport before first insert"))

    def reject_patch(*_args, **_kwargs):
        pytest.fail("A new suffix or unchanged binding used an existing Attempt retry patch")

    with monkeypatch.context() as patch:
        patch.setattr(attempts, "save_publication_retry", reject_patch)
        _save(repository, current)
        _save(repository, current)
    full = repository.load(_RUN)
    assert full is not None
    assert len(full["ticket_jobs"]["3"]["semantic_attempt_history"]) == 1


@pytest.mark.parametrize("damage", ["ordinary-attempt", "other-member", "foreign-owner", "foreign-run", "position"])
def test_retry_suffix_keeps_exact_member_owner_and_position_guards(tmp_path: Path, damage: str) -> None:
    repository, current = _pending(tmp_path)
    job = _close(current)
    _save(repository, current)
    original = deepcopy(repository.load(_RUN))
    job = current["ticket_jobs"]["3"]
    if damage == "ordinary-attempt":
        # A non-Publication completed Attempt in an append cannot be edited.
        other = stored_attempt("ticket:3", role="development")
        repository.save("other-run", {"ticket_jobs": {"3": {"ticket_number": 3,
                                                            "semantic_attempt_history": [other]}}})
        current = repository.load_objects("other-run")[0]
        job = current["ticket_jobs"]["3"]
        job["semantic_attempt_history"] = JobHistoryAppend([other], base_bytes=2, base_count=0)
        other["outcome"] = "changed-result"
        with pytest.raises(ValueError, match="Cannot overwrite an unread Semantic Attempt"):
            repository.save_objects("other-run", current, fields={"ticket_jobs"}, archive_scope=projection_archives(current))
        return
    record_publication_operation_failure(job, OSError("original failure"))
    if damage == "other-member":
        assert job.publication_update is not None
        job.publication_update[1]["publication"]["pr_title"] = "changed narrative"
    elif damage in {"foreign-owner", "foreign-run"}:
        assert job.publication_update is not None
        target_run = "foreign-run" if damage == "foreign-run" else _RUN
        number = 3 if damage == "foreign-run" else 4
        if damage == "foreign-run":
            repository.save(target_run, {"ticket_jobs": {"3": {"ticket_number": 3}}})
        foreign = repository.load_objects(target_run)[0]
        foreign_job = JobProjection({"ticket_number": number, "phase": "publishing", "publication_attempts": 1,
                                    "publication_operation_retry": {"attempts": 1, "limit": 5}},
                                    job_id=f"ticket:{number}:generation:1", revision=0,
                                    archives={"semantic_attempt_history": (2, 0)})
        foreign_job.publication_update = deepcopy(job.publication_update)
        foreign["ticket_jobs"][str(number)] = foreign_job
        with pytest.raises(ValueError, match="Completed Publication Attempt changed"):
            repository.save_objects(target_run, foreign, fields={"ticket_jobs"}, archive_scope=projection_archives(foreign),
                                    keyed_changes={"ticket_jobs": {str(number)}})
        assert repository.load(_RUN) == original
        return
    else:
        job["semantic_attempt_history"].base_count += 1
    with pytest.raises(ValueError, match="Only the exact completed Publication retry|different history position"):
        _save(repository, current)
    assert repository.load(_RUN) == original


def test_reopened_current_owner_can_retry_without_loading_its_archive(tmp_path: Path) -> None:
    repository, current = _pending(tmp_path)
    _close(current)
    _save(repository, current)
    record_publication_operation_failure(current["ticket_jobs"]["3"], OSError("first failure"))
    _save(repository, current)
    reopened = SQLiteStateRepository(tmp_path)
    current = reopened.load_objects(_RUN)[0]
    owner = current["ticket_jobs"]["3"]
    assert "semantic_attempt_history" not in owner
    assert owner.publication_update is not None

    def reject_archive(*_args):
        pytest.fail("An exact Publication retry loaded the complete archive")

    owner.history_reader = reject_archive
    record_publication_operation_failure(owner, OSError("failure after reopening"))
    _save(reopened, current)
    assert reopened.load(_RUN)["ticket_jobs"]["3"]["semantic_attempt_history"][-1]["publication_operation_retry"] == {
        "attempts": 2, "limit": 5,
    }


def test_retry_suffix_rejects_a_stale_exact_version(tmp_path: Path) -> None:
    repository, stale = _pending(tmp_path)
    _close(stale)
    _save(repository, stale)
    record_publication_operation_failure(stale["ticket_jobs"]["3"], OSError("stale retry"))
    concurrent = SQLiteStateRepository(tmp_path).load_objects(_RUN)[0]
    record_publication_operation_failure(concurrent["ticket_jobs"]["3"], OSError("newer retry"))
    _save(repository, concurrent)
    original = repository.load(_RUN)
    with pytest.raises(ValueError, match="Completed Publication Attempt changed before retry update"):
        _save(repository, stale)
    assert repository.load(_RUN) == original


@pytest.mark.parametrize("change", ["cleared", "counter", "role", "status"])
def test_current_attempt_scope_follows_retry_need_without_hiding_invalid_links(tmp_path: Path, change: str) -> None:
    from agent_run.state_contract import require_current_work_state
    from agent_run.state_errors import IncompatibleRunStateError

    repository, current = _pending(tmp_path)
    _close(current)
    _save(repository, current)
    record_publication_operation_failure(current["ticket_jobs"]["3"], OSError("original failure"))
    _save(repository, current)
    with repository.atomic():
        current = repository.load_work(_RUN, 3)
        assert current is not None
        owner = current["ticket_jobs"]["3"]
        assert repository.work_attempts(current)
        if change == "cleared":
            owner.pop("publication_operation_retry")
            assert repository.work_attempts(current) == {}
            return
        if change == "counter":
            owner["publication_attempts"] += 1
        else:
            for attempt in repository._local.work_attempts.values():
                attempt[change] = "development" if change == "role" else "pending"
        related = repository.work_attempts(current)
        assert related
        with pytest.raises(IncompatibleRunStateError, match="invalid scoped Publication Attempt association"):
            require_current_work_state(current, repository.work_archives(current), related)
