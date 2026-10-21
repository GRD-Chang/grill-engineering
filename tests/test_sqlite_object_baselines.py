"""对象保存复用同一写快照，范围不足时仍读取准确的持久基线。"""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run import sqlite_change_jobs as jobs, sqlite_work_records
from agent_run.sqlite_state import SQLiteStateRepository
from agent_run.work_projection import JobProjection, publication_attempt_for_current_ordinal
from support.work_records import attempt, invocation


@pytest.fixture
def baseline_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = sqlite_work_records.baseline_for_save

    def reading(connection, run_id, *args, **kwargs):
        calls.append(run_id)
        return original(connection, run_id, *args, **kwargs)

    monkeypatch.setattr(sqlite_work_records, "baseline_for_save", reading)
    return calls


@pytest.mark.parametrize("present", [False, True], ids=["proven-absent", "present"])
def test_object_snapshot_reuses_queried_fields(
    tmp_path: Path, baseline_reads: list[str], present: bool,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"status": "active", **({"phase": "pending"} if present else {})}
    repository.save("run-1", original)
    with repository.atomic():
        repository.load_objects("run-1", thin_timeline=True)
        repository.save_objects("run-1", {"phase": "candidate"}, fields={"phase"})
    assert not baseline_reads
    assert repository.load("run-1") == {**original, "phase": "candidate"}


def test_object_snapshot_reuses_current_jobs_invocations_and_unread_history(
    tmp_path: Path, baseline_reads: list[str],
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    job = {"ticket_number": 2, "phase": "developing", "reviewer_thread_ids": ["retained-reviewer"],
           "human_blocker_history": [{"question": "retained original evidence"}]}
    call = invocation(context={"body": "exact Invocation evidence"})
    repository.save("run-1", {"ticket_jobs": {"2": job}, "active_ticket_job": deepcopy(job),
                              "active_agent_invocation": call, "ticket_agent_invocations": {"2": deepcopy(call)},
                              "agent_invocation_history": [deepcopy(call)]})
    expected = repository.load("run-1")
    with repository.atomic():
        current, _ = repository.load_objects("run-1", thin_timeline=True)
        current["ticket_jobs"]["2"]["phase"] = "reviewing"
        for record in (current["active_agent_invocation"], current["ticket_agent_invocations"]["2"],
                       current["agent_invocation_history"][0]):
            record["error"] = "late terminal detail"
        repository.save_objects(
            "run-1", current, fields={"ticket_jobs", "active_ticket_job", *sqlite_work_records.INVOCATION_FIELDS},
            keyed_changes={"ticket_jobs": {"2"}, "ticket_agent_invocations": {"2"}},
            archive_scope=jobs.projection_archives(current),
        )
    assert not baseline_reads
    expected["ticket_jobs"]["2"]["phase"] = expected["active_ticket_job"]["phase"] = "reviewing"
    for record in (expected["active_agent_invocation"], expected["ticket_agent_invocations"]["2"],
                   expected["agent_invocation_history"][0]):
        record["error"] = "late terminal detail"
    assert repository.load("run-1") == expected


@pytest.mark.parametrize("boundary", ["run", "fields", "sql", "transaction", "rollback"])
def test_object_baseline_never_crosses_run_read_or_transaction_boundaries(
    tmp_path: Path, baseline_reads: list[str], boundary: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    repository.save("run-1", {"phase": "pending"})
    repository.save("run-2", {"phase": "independent"})
    target = "run-2" if boundary == "run" else "run-1"
    with repository.work_session():
        if boundary in {"transaction", "rollback"}:
            try:
                with repository.atomic():
                    repository.load_objects("run-1", thin_timeline=True)
                    changes = repository._local.connection.total_changes
                    if boundary == "rollback":
                        raise RuntimeError("rollback this read")
            except RuntimeError as error:
                assert str(error) == "rollback this read"
            assert repository._local.work_view is None
        with repository.atomic():
            if boundary in {"transaction", "rollback"}:
                assert repository._local.connection.total_changes == changes
            else:
                repository.load_objects("run-1", thin_timeline=True)
                changes = repository._local.connection.total_changes
                if boundary == "fields":
                    repository.load_fields("run-1", {"phase"}, thin_timeline=True)
                elif boundary == "sql":
                    repository._local.connection.execute("UPDATE runs SET phase='fresh' WHERE run_id='run-1'")
                else:
                    assert repository._local.connection.total_changes == changes
            repository.save_objects(target, {"phase": "candidate"}, fields={"phase"})
    assert baseline_reads == [target]
    assert repository.load(target) == {"phase": "candidate"}
    assert repository.load("run-1" if target == "run-2" else "run-2") == {
        "phase": "pending" if target == "run-2" else "independent"
    }


@pytest.mark.parametrize("field", ["phase", "ticket_jobs"])
def test_object_read_exclusions_cannot_prove_missing_facts(
    tmp_path: Path, baseline_reads: list[str], field: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"phase": "pending", "ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing"}}}
    repository.save("run-1", deepcopy(original))
    proposed = {"phase": "candidate"} if field == "phase" else {
        "ticket_jobs": {"2": {"ticket_number": 2, "phase": "reviewing"}}
    }
    with repository.atomic():
        repository.load_objects("run-1", excluded_fields={field, "ticket_agent_invocations"}, thin_timeline=True)
        repository.save_objects("run-1", proposed, fields={field}, archive_scope={})
    assert baseline_reads == ["run-1"]
    assert repository.load("run-1") == {**original, **proposed}


@pytest.mark.parametrize("number", ["2", "3"], ids=["loaded-key", "unread-sibling"])
@pytest.mark.parametrize("field", ["ticket_jobs", "retired_ticket_generations"])
def test_partial_collections_only_reuse_keys_actually_read(
    tmp_path: Path, baseline_reads: list[str], number: str, field: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing"},
                                "3": {"ticket_number": 3, "phase": "accepted"}},
                "retired_ticket_generations": {"2": 1, "3": 2}}
    repository.save("run-1", deepcopy(original))
    value = {"ticket_number": int(number), "phase": "reviewing"} if field == "ticket_jobs" else 3
    with repository.atomic():
        repository.load_objects("run-1", ticket_numbers={2}, thin_timeline=True)
        repository.save_objects("run-1", {field: {number: value}}, fields={field},
                                keyed_changes={field: {number}}, archive_scope={})
    assert baseline_reads == ([] if number == "2" else ["run-1"])
    original[field][number] = value
    assert repository.load("run-1") == original


@pytest.mark.parametrize("history", ["reviewer_thread_ids", "publication"])
def test_later_history_and_publication_reads_keep_the_original_baseline_query(
    tmp_path: Path, baseline_reads: list[str], history: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    job = ({"ticket_number": 2, "phase": "developing", "reviewer_thread_ids": ["original-reviewer"]}
           if history == "reviewer_thread_ids" else {
               "ticket_number": 2, "phase": "publishing", "publication_attempts": 1,
               "semantic_attempt_history": [attempt(role="publication")],
           })
    repository.save("run-1", {"ticket_jobs": {"2": deepcopy(job)}})
    with repository.atomic():
        current, _ = repository.load_objects(
            "run-1", thin_timeline=True,
            excluded_fields={"ticket_jobs"} if history == "publication" else set(),
        )
        # An exact path read must neither widen nor replace the original
        # transaction snapshot's object/Timeline coverage.
        owner = repository.load_paths("run-1", {("ticket_jobs", "2")})["ticket_jobs"]["2"]
        assert isinstance(owner, JobProjection)
        if history == "reviewer_thread_ids":
            assert owner.materialize(history) == ["original-reviewer"]
            owner["phase"] = job["phase"] = "reviewing"
        else:
            assert publication_attempt_for_current_ordinal(owner) is not None
            owner["phase"] = job["phase"] = "accepted"
        current.setdefault("ticket_jobs", {})["2"] = owner
        repository.save_objects("run-1", current, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}},
                                archive_scope=jobs.projection_archives(current))
    assert baseline_reads == ["run-1"]
    assert repository.load("run-1") == {"ticket_jobs": {"2": job}}


@pytest.mark.parametrize("scope", ["current", "complete", "unread-generation"])
def test_repair_budget_reuse_requires_the_same_exact_scope(
    tmp_path: Path, baseline_reads: list[str], scope: str,
) -> None:
    repository = SQLiteStateRepository(tmp_path)
    original = {"run_acceptance": {"phase": "repairing", "repair_job": {"repair_generation": 2},
                                  "repair_integration_budgets": {
                                      "1": {"policy_snapshot": {"revision": "old"}},
                                      "2": {"policy_snapshot": {"revision": "current"}},
                                  }}}
    repository.save("run-1", deepcopy(original))
    rejected = (pytest.raises(ValueError, match="Cannot overwrite unread Repair integration budget")
                if scope == "unread-generation" else nullcontext())
    with rejected, repository.atomic():
        current, _ = repository.load_objects("run-1", thin_timeline=True)
        owner = current["run_acceptance"]
        if scope == "complete":
            owner["repair_integration_budgets"] = deepcopy(original["run_acceptance"]["repair_integration_budgets"])
        generation = "1" if scope == "unread-generation" else "2"
        if scope == "unread-generation":
            owner["repair_integration_budgets"][generation] = {"policy_snapshot": {"revision": "replacement"}}
        else:
            owner["repair_integration_budgets"][generation]["policy_snapshot"]["revision"] = "updated"
        repository.save_objects("run-1", current, fields={"run_acceptance"}, archive_scope={})
    assert baseline_reads == ([] if scope == "current" else ["run-1"])
    if scope != "unread-generation":
        original["run_acceptance"]["repair_integration_budgets"]["2"]["policy_snapshot"]["revision"] = "updated"
    assert repository.load("run-1") == original
