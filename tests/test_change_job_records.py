"""Protect Job ownership, retained conditions and exact old budget evidence."""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run import sqlite_change_jobs as jobs
from agent_run import sqlite_work_evidence, sqlite_work_execution
from agent_run.delivery_budget import delivery_budget_owner
from agent_run.delivery_policy import DeliveryPolicy, run_repair_budget_policy
from agent_run.review_budget import RUN_POLICY, TICKET_POLICY, new_budget, renew_run_budget, reset_budget


def _open(path: Path, *, create: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    if create:
        connection.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO runs VALUES ('r')")
        for module in (sqlite_work_evidence, jobs, sqlite_work_execution):
            for ddl in module.TABLES.values():
                connection.execute(ddl)
            for ddl in module.INDEXES.values():
                connection.execute(ddl)
        connection.commit()
    return connection


def _job(number: int, phase: str = "reviewing") -> dict[str, Any]:
    return {"ticket_number": number, "ticket_branch_generation": 1, "phase": phase,
            "review_budget": new_budget(), "review_budget_history": [], "modification_attempts": 1,
            "validation_attempts": 1, "development_thread_history": [], "reviewer_thread_ids": []}


def test_selection_and_brother_changes_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first, second = _job(1, "completed"), _job(2)
    baseline = {"ticket_jobs": {"1": first, "2": second}, "active_ticket_job": first}
    connection = _open(path, create=True)
    try:
        jobs.save_jobs(connection, "r", baseline, {})
        connection.commit()
        failed = deepcopy(baseline)
        failed["ticket_jobs"]["2"].update(phase="blocked", blocked_reason="execution_failure", execution_resume_phase="reviewing")
        jobs.save_jobs(connection, "r", failed, baseline, keyed_changes={"ticket_jobs": {"2"}})
        third = deepcopy(baseline)
        third["ticket_jobs"]["3"] = _job(3, "developing")
        jobs.save_jobs(connection, "r", third, baseline, keyed_changes={"ticket_jobs": {"3"}})
        connection.commit()
    finally:
        connection.close()
    connection = _open(path)
    try:
        restored = jobs.load_jobs(connection, "r")
        assert restored["active_ticket_job"] is restored["ticket_jobs"]["1"]
        assert restored["active_ticket_job"]["phase"] == "completed"
        assert "blocked_reason" not in restored["active_ticket_job"]
        assert restored["ticket_jobs"]["2"]["blocked_reason"] == "execution_failure"
        assert restored["ticket_jobs"]["3"]["phase"] == "developing"
    finally:
        connection.close()


def test_removing_visible_gate_retains_independent_budget_condition(tmp_path: Path) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        initial = {"ticket_jobs": {"2": _job(2)}}
        jobs.save_jobs(connection, "r", initial, {})
        budget = deepcopy(initial)
        budget["ticket_jobs"]["2"].update(phase="blocked", blocked_reason="review_budget_exhausted")
        jobs.save_jobs(connection, "r", budget, initial)
        failed = deepcopy(budget)
        failed["ticket_jobs"]["2"].update(blocked_reason="execution_failure", execution_resume_phase="reviewing")
        jobs.save_jobs(connection, "r", failed, budget)
        visible = jobs.load_jobs(connection, "r")
        resumed = deepcopy(visible)
        resumed["ticket_jobs"]["2"].pop("blocked_reason")
        resumed["ticket_jobs"]["2"].pop("execution_resume_phase")
        resumed["ticket_jobs"]["2"]["phase"] = "reviewing"
        jobs.save_jobs(connection, "r", resumed, visible)
        connection.commit()
        restored = jobs.load_jobs(connection, "r")
        assert restored["ticket_jobs"]["2"]["phase"] == "blocked"
        assert restored["ticket_jobs"]["2"]["blocked_reason"] == "review_budget_exhausted"
        assert "execution_resume_phase" not in restored["ticket_jobs"]["2"]
    finally:
        connection.close()


def test_budget_window_keeps_original_evidence_without_mutable_job_copy(tmp_path: Path) -> None:
    job = _job(1, "blocked")
    job.update(candidate_sha="old", acceptance_artifact={"evidence": "old raw report"},
               candidate_commit_intent={"kind": "candidate_commit", "expected_head": "old", "attempt": 1, "token": "nonce"})
    job["review_budget"].update(development_attempts=4, reviewer_invocations=3)
    reset_budget(job, TICKET_POLICY)
    snapshot = job["review_budget_history"][0]
    assert "candidate_commit_intent" not in snapshot
    assert "development_thread_history" not in snapshot
    job.update(candidate_sha="new", acceptance_artifact={"evidence": "new raw report"})
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", {"ticket_jobs": {"1": job}}, {})
        connection.commit()
        restored = jobs.load_jobs(connection, "r")["ticket_jobs"]["1"]
        assert restored["acceptance_artifact"] == {"evidence": "new raw report"}
        assert restored["review_budget_history"][0]["acceptance_artifact"] == {"evidence": "old raw report"}
        assert restored["review_budget_history"][0]["candidate_sha"] == "old"
    finally:
        connection.close()


def test_run_repair_is_same_alias_and_budget_renewal_preserves_other_usage(tmp_path: Path) -> None:
    policy = DeliveryPolicy().snapshot()
    acceptance: dict[str, Any] = {"phase": "repairing", "policy_snapshot": policy, "acceptance_generation": 1,
        "review_budget": new_budget(development_attempts=2, reviewer_invocations=RUN_POLICY.review_limit),
        "review_budget_history": [], "modification_attempts": 2, "validation_attempts": 4,
        "repair_job": {"phase": "blocked", "repair_generation": 2, "modification_attempts": 2, "validation_attempts": 4,
                       "blocked_reason": "review_budget_exhausted", "development_thread_id": "repair-thread"},
        "repair_cycle": {"generation": 2, "status": "budget_exhausted", "code_modification_attempts": 2, "validation_attempts": 4}}
    renew_run_budget(acceptance, run_repair_budget_policy(DeliveryPolicy()), reason="review_budget_exhausted")
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", {"run_acceptance": acceptance}, {})
        connection.commit()
        state = jobs.load_jobs(connection, "r")
        restored = state["run_acceptance"]
        assert delivery_budget_owner(state, restored["repair_job"]) is restored
        assert restored["review_budget"]["development_attempts"] == 2
        assert restored["review_budget"]["reviewer_invocations"] == 0
        assert restored["review_budget_history"][0]["review_budget"]["reviewer_invocations"] == RUN_POLICY.review_limit
        assert "review_budget" not in restored["repair_job"]
        assert restored["repair_cycle"]["development_thread_id"] == "repair-thread"
    finally:
        connection.close()


def test_sparse_read_preserves_unloaded_archives_and_exposes_size_metadata(tmp_path: Path) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        first, second = _job(1), _job(2)
        second["human_blocker_history"] = [{"evidence": "previous human boundary"}]
        initial = {"ticket_jobs": {"1": first, "2": second}, "active_ticket_job": first}
        jobs.save_jobs(connection, "r", initial, {})
        sparse = jobs.load_jobs(connection, "r", ticket_numbers={1}, include_history=False)
        assert "human_blocker_history" not in sparse["ticket_jobs"]["2"]
        archives = jobs.read_job_archives(connection, "r", sparse)
        assert archives['["ticket_jobs","2","human_blocker_history"]'][1] == 1
        changed = deepcopy(sparse)
        changed["ticket_jobs"]["2"]["phase"] = "accepted"
        jobs.save_jobs(connection, "r", changed, sparse, keyed_changes={"ticket_jobs": {"2"}})
        assert jobs.load_jobs(connection, "r")["ticket_jobs"]["2"]["human_blocker_history"] == second["human_blocker_history"]
    finally:
        connection.close()


def test_recovery_intent_preserves_missing_null_and_empty_authority(tmp_path: Path) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        initial = {"parent_job": {"phase": "publishing", "ticket_write_intent": {"action": "ensure_ticket_pr", "prior_narrative": None, "authority": {}}}}
        jobs.save_jobs(connection, "r", initial, {})
        assert jobs.load_jobs(connection, "r") == initial
        assert jobs.load_jobs(connection, "r", fields=set()) == {}
    finally:
        connection.close()


def test_stale_same_job_write_cannot_replace_new_candidate(tmp_path: Path) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        initial = {"parent_job": {"phase": "candidate", "candidate_sha": "a"}}
        jobs.save_jobs(connection, "r", initial, {})
        jobs.save_jobs(connection, "r", {"parent_job": {"phase": "candidate", "candidate_sha": "b"}}, initial)
        with pytest.raises(ValueError, match="concurrently"):
            jobs.save_jobs(connection, "r", {"parent_job": {"phase": "candidate", "candidate_sha": "c"}}, initial)
        assert jobs.load_jobs(connection, "r")["parent_job"]["candidate_sha"] == "b"
    finally:
        connection.close()


def test_retired_jobs_append_without_replacing_prior_owner(tmp_path: Path) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        first = {"work_subject": "ticket:1", "generation": 1, "phase": "candidate", "candidate_sha": "a"}
        second = {"work_subject": "ticket:2", "generation": 2, "phase": "accepted", "candidate_sha": "b"}
        jobs.save_jobs(connection, "r", {"retired_job_generations": [first]}, {})
        jobs.append_archive(connection, "r", "retired_job_generations", [second])
        connection.commit()
        assert jobs.load_jobs(connection, "r")["retired_job_generations"] == [first, second]
    finally:
        connection.close()


@pytest.mark.parametrize(("container", "generation"), [
    ("ticket", "ticket_branch_generation"), ("parent", "parent_generation"), ("repair", "repair_generation"),
])
def test_replacement_generation_initializes_unchanged_values_and_history_roots(
    tmp_path: Path, container: str, generation: str,
) -> None:
    job = {"phase": "reviewing", generation: 1, "review_context": {"body": "retained requirements"},
           "review_budget": new_budget(), "review_budget_history": [], "development_thread_history": [],
           "reviewer_thread_ids": [], "modification_attempts": 0, "validation_attempts": 0}
    if container == "ticket":
        job["ticket_number"] = 2
        original = {"ticket_jobs": {"2": job}, "active_ticket_job": job}
    elif container == "parent":
        original = {"parent_job": job}
    else:
        original = {"run_acceptance": {"phase": "repairing", "repair_job": job}}
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", original, {})
        replacement = deepcopy(original)
        owner = (replacement["ticket_jobs"]["2"] if container == "ticket" else replacement["parent_job"]
                 if container == "parent" else replacement["run_acceptance"]["repair_job"])
        owner[generation] = 2
        jobs.save_jobs(connection, "r", replacement, original)
        connection.commit()
    finally:
        connection.close()
    connection = _open(tmp_path / "state.sqlite3")
    try:
        assert jobs.load_jobs(connection, "r") == replacement
        kind = {"ticket": "ticket", "parent": "parent", "repair": "run_repair"}[container]
        for ordinal in (1, 2):
            identity = jobs.job_identity(kind, {generation: ordinal}, ticket_number=2 if container == "ticket" else None)
            assert connection.execute("SELECT item_count,logical_bytes FROM work_job_archive_roots "
                                      "WHERE job_id=? AND history_kind='review_budget_history'", (identity,)).fetchone() == (0, 2)
    finally:
        connection.close()


def test_requeue_selection_does_not_reuse_retired_generation_or_attempts(tmp_path: Path) -> None:
    from agent_run.requeue import requeue_change_job
    from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt

    job = _job(2)
    attempt = allocate_semantic_attempt(job, role="development", work_subject="ticket:2", generation=1,
        currentness_boundary={"candidate_sha": "old"}, ordinal=1, budget_window=1)
    close_semantic_attempt(job, attempt, outcome="development_complete")
    original = {"run_id": "r", "status": "requeue_required", "ticket_jobs": {"2": job}, "active_ticket_job": job}
    changed = deepcopy(original)
    retired = requeue_change_job(changed)
    selection = {"ticket_number": 2, "selection_reason": "Fresh work selected after Requeue"}
    changed["ticket_jobs"]["2"] = selection
    changed["active_ticket_job"] = selection
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", original, {})
        jobs.save_jobs(connection, "r", changed, original)
        connection.commit()
    finally:
        connection.close()
    connection = _open(tmp_path / "state.sqlite3")
    try:
        restored = jobs.load_jobs(connection, "r")
        assert restored["ticket_jobs"]["2"] == restored["active_ticket_job"] == selection
        assert restored["retired_job_generations"] == [retired]
        assert jobs.selected_ticket_number(connection, "r") == 2
        assert connection.execute("SELECT job_id FROM work_job_slots WHERE container='ticket_jobs' AND slot='2'").fetchone() != (
            jobs.job_identity("ticket", job),)
    finally:
        connection.close()


def test_run_can_be_deleted_and_recreated_with_owned_budget_archives(tmp_path: Path) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        job = _job(1)
        reset_budget(job, TICKET_POLICY)
        initial = {"ticket_jobs": {"1": job}, "active_ticket_job": job,
                   "run_acceptance": {"phase": "pending", "repair_integration_budgets": {"1": {"policy_snapshot": {"revision": "old"}}}}}
        jobs.save_jobs(connection, "r", initial, {})
        connection.execute("DELETE FROM runs WHERE run_id='r'")
        connection.execute("INSERT INTO runs VALUES ('r')")
        jobs.save_jobs(connection, "r", {"parent_job": {"phase": "developing"}}, {})
        assert jobs.load_jobs(connection, "r") == {"parent_job": {"phase": "developing"}}
    finally:
        connection.close()


def test_independent_budget_usage_and_repair_resync_generations_roundtrip(tmp_path: Path) -> None:
    from agent_run.ci_development_budget import new_ci_development_budget, renew_ci_development_budget
    from agent_run.integration_development_budget import renew_integration_development_budget
    from agent_run.integration_resync_budget import renew_resync_budget

    policy = DeliveryPolicy().snapshot()
    job = _job(1)
    job["policy_snapshot"] = policy
    ci = new_ci_development_budget()
    ci.update(developments_used=1, failure_head="ci-failed", checkpoint_reason="ci_development_budget_exhausted", semantic_work_ids=["ci:old"])
    job["ci_recovery"] = ci
    renew_ci_development_budget(job, limit=1)
    limit = DeliveryPolicy().ticket_integration_development_limit
    job["integration_development_budget"] = {"window": 1, "used": limit, "limit": limit, "attempt_ids": [f"a{index}" for index in range(limit)], "renewals": []}
    renew_integration_development_budget(job, policy_snapshot=policy)
    resync_limit = DeliveryPolicy().integration_resync_limit
    resync_owner = {"policy_snapshot": policy, "integration_resync": {"generation": 1, "window": 1,
        "used": resync_limit, "limit": resync_limit, "open": False, "source_sha": "source", "target_sha": "target", "history": []}}
    renew_resync_budget(resync_owner, policy_snapshot=policy)
    initial = {"ticket_jobs": {"1": job}, "run_acceptance": {"phase": "repairing", "repair_integration_budgets": {"2": resync_owner}}}
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", initial, {})
        restored = jobs.load_jobs(connection, "r")
        assert restored == initial
        assert restored["ticket_jobs"]["1"]["integration_development_budget"]["used"] == limit
        assert restored["ticket_jobs"]["1"]["integration_development_budget"]["limit"] == 2 * limit
        assert restored["run_acceptance"]["repair_integration_budgets"]["2"]["integration_resync"]["generation"] == 2
    finally:
        connection.close()


def test_scoped_save_baseline_preserves_histories_omitted_by_work_view(tmp_path: Path) -> None:
    from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt

    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        job = _job(2)
        attempt = allocate_semantic_attempt(job, role="development", work_subject="ticket:2", generation=1,
            currentness_boundary={"candidate_sha": "old"}, ordinal=1, budget_window=1)
        close_semantic_attempt(job, attempt, outcome="development_complete")
        original = {"ticket_jobs": {"2": job}}
        jobs.save_jobs(connection, "r", original, {})
        sparse = jobs.load_jobs(connection, "r", ticket_numbers=set(), include_history=False)
        assert "review_budget_history" not in sparse["ticket_jobs"]["2"]
        scope = jobs.read_job_archives(connection, "r", sparse)
        baseline = jobs.load_jobs(connection, "r", archive_scope=scope)
        sparse["ticket_jobs"]["2"]["phase"] = "accepted"
        jobs.save_jobs(connection, "r", sparse, baseline)
        restored = jobs.load_jobs(connection, "r")["ticket_jobs"]["2"]
        assert restored["review_budget_history"] == []
        assert restored["semantic_attempt_history"] == job["semantic_attempt_history"]
    finally:
        connection.close()


def test_unloaded_archive_cannot_be_replaced_with_empty_list(tmp_path: Path) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        original = {"ticket_jobs": {"2": _job(2)}}
        jobs.save_jobs(connection, "r", original, {})
        sparse = jobs.load_jobs(connection, "r", ticket_numbers=set(), include_history=False)
        changed = deepcopy(sparse)
        changed["ticket_jobs"]["2"]["review_budget_history"] = []
        with pytest.raises(ValueError, match="cannot replace unloaded Job history"):
            jobs.save_jobs(connection, "r", changed, sparse)
        assert jobs.load_jobs(connection, "r")["ticket_jobs"]["2"]["review_budget_history"] == []
    finally:
        connection.close()


@pytest.mark.parametrize("event_id", [17, "fixture-event-17"])
def test_close_receipt_retains_original_event_identity_type(tmp_path: Path, event_id: str | int) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        original = {"parent_job": {"phase": "completed", "ticket_close_ownership": {"event_id": event_id, "actor": "publisher"}}}
        jobs.save_jobs(connection, "r", original, {})
        restored = jobs.load_jobs(connection, "r")
        assert restored == original
        assert type(restored["parent_job"]["ticket_close_ownership"]["event_id"]) is type(event_id)
    finally:
        connection.close()


def test_repair_review_budget_refs_original_return_after_consumption(tmp_path: Path) -> None:
    from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt

    artifact = {"checks": {lane: {"status": "pass", "evidence": "original report", "findings": []}
                           for lane in ("e2e", "standards", "spec")}}
    identity = {"reviewed_candidate_sha": "candidate"}
    record = {"reviewer_thread_id": "repair-review", "candidate_sha": "candidate", "reviewed_base_sha": "base", "review_identity": identity, "artifact": artifact}
    repair: dict[str, Any] = {"phase": "reviewing", "repair_generation": 1}
    attempt = allocate_semantic_attempt(repair, role="reviewer", work_subject="run-repair:r", generation=1,
        currentness_boundary=identity, ordinal=1, budget_window=1)
    attempt["review_context"] = {"facts": {"acceptance_scope": "run", "current_review_identity": identity}}
    attempt["returned_result"] = {"kind": "review", "thread_id": "repair-review", "artifact": artifact}
    acceptance = {"phase": "repairing", "review_budget": new_budget(review_artifacts=[record]), "review_budget_history": [], "repair_job": repair}
    original = {"run_acceptance": acceptance}
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", original, {})
        next_state = deepcopy(original)
        next_repair = next_state["run_acceptance"]["repair_job"]
        close_semantic_attempt(next_repair, next_repair["pending_semantic_attempt"], outcome="acceptance_artifact")
        next_repair["phase"] = "accepted"
        jobs.save_jobs(connection, "r", next_state, original)
        assert jobs.load_jobs(connection, "r")["run_acceptance"]["review_budget"]["review_artifacts"] == [record]
        reference = connection.execute("SELECT artifact_id FROM work_job_review_artifacts WHERE job_id=?", (jobs.job_identity("run_acceptance", acceptance),)).fetchone()[0]
        source = connection.execute("SELECT source_evidence_id FROM work_evidence WHERE evidence_id=?", (reference,)).fetchone()[0]
        assert source is not None
        assert sqlite_work_evidence.get_evidence(connection, "r", source, owner_id="attempt:" + attempt["attempt_id"], kind="returned_result")["thread_id"] == "repair-review"
    finally:
        connection.close()


def test_completed_ticket_budget_scope_is_bound_to_actual_sql_projection(git_repo: Path) -> None:
    from agent_run.integration_record_contract import require_completed_ticket_integration_records
    from agent_run.object_state import object_store
    from agent_run.review_budget import ensure_budget
    from agent_run.state_errors import IncompatibleRunStateError
    from support.run_scenarios import _completed_run

    state, states, _ = _completed_run(git_repo)
    run_id = state["run_id"]
    scoped = object_store(states).load_scheduler_current_run(run_id)
    assert scoped is not None
    assert "review_budget_history" not in scoped["ticket_jobs"]["2"]
    require_completed_ticket_integration_records(scoped)
    with pytest.raises(ValueError, match="review_budget_history"):
        ensure_budget(scoped["ticket_jobs"]["2"], TICKET_POLICY)
    # A plain dictionary copy carries no SQL archive proof.
    with pytest.raises(IncompatibleRunStateError, match="review_budget"):
        require_completed_ticket_integration_records(dict(scoped))
    full = states.load_current_run(run_id)
    assert full is not None
    require_completed_ticket_integration_records(full)
    full["ticket_jobs"]["2"].pop("review_budget_history")
    with pytest.raises(IncompatibleRunStateError, match="review_budget"):
        require_completed_ticket_integration_records(full)
    # Renaming an owner cannot transfer another Ticket's exact archive proof.
    renamed = deepcopy(scoped)
    renamed["ticket_jobs"]["3"] = renamed["ticket_jobs"].pop("2")
    with pytest.raises(IncompatibleRunStateError, match="review_budget"):
        require_completed_ticket_integration_records(renamed)


@pytest.mark.parametrize("field", ["human_blocker_history", "reviewer_thread_ids"])
@pytest.mark.parametrize("reader", ["full", "metadata"])
@pytest.mark.parametrize("damage", ["short", "long", "blob"])
def test_archive_size_damage_is_rejected_without_expanding_sibling_reports(
    tmp_path: Path, field: str, reader: str, damage: str,
) -> None:
    from agent_run.sqlite_encoding import encode_json

    values = ["review \"历史\"\n🙂", "another"] if field == "reviewer_thread_ids" else [{"message": "问题🙂" * 50}]
    original = {"ticket_jobs": {"2": {"ticket_number": 2, field: values}}}
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", original, {})
        sparse = jobs.load_jobs(connection, "r", ticket_numbers=set(), include_history=False)
        queries: list[str] = []
        connection.set_trace_callback(queries.append)
        metadata = jobs.read_job_archives(connection, "r", sparse)
        connection.set_trace_callback(None)
        assert metadata[encode_json(("ticket_jobs", "2", field))] == (len(encode_json(values).encode("utf-8")), len(values))
        assert not any("FROM work_evidence_materials" in query for query in queries)
        assert jobs.load_jobs(connection, "r") == original
        expression = {"short": "logical_bytes-1", "long": "logical_bytes+1", "blob": "CAST(logical_bytes AS BLOB)"}[damage]
        table = "work_job_archives" if reader == "full" else "work_job_archive_roots"
        connection.execute(f"UPDATE {table} SET logical_bytes={expression} WHERE history_kind=?", (field,))
        with pytest.raises(ValueError, match="Job history size"):
            jobs.load_jobs(connection, "r") if reader == "full" else jobs.read_job_archives(connection, "r", sparse)
    finally:
        connection.close()


def test_explicitly_removed_loaded_history_is_not_reintroduced(tmp_path: Path) -> None:
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        initial = {"run_publication": {"phase": "waiting_external", "pause_history": [{"reason": "previous observation"}]}}
        jobs.save_jobs(connection, "r", initial, {})
        full = jobs.load_jobs(connection, "r")
        changed = deepcopy(full)
        changed["run_publication"].pop("pause_history")
        jobs.save_jobs(connection, "r", changed, full)
        assert jobs.load_jobs(connection, "r") == changed
        assert connection.execute("SELECT COUNT(*) FROM work_evidence WHERE kind='pause_history'").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize("container", ["parent_job", "run_acceptance", "active_ticket_job"])
@pytest.mark.parametrize("old,new", [(1, True), (1, 1.0), (0.0, -0.0)])
def test_owner_updates_preserve_exact_nested_json_after_reopen(
    tmp_path: Path, container: str, old: Any, new: Any,
) -> None:
    from agent_run.sqlite_encoding import encode_json

    job = {"phase": "reviewing", "review_context": {"nested": [{"value": old}]},
           "review_budget": new_budget(review_artifacts=[{"artifact": {"value": old}}])}
    if container == "active_ticket_job":
        job["ticket_number"] = 2
    original = {container: job}
    updated = deepcopy(original)
    updated[container]["review_context"]["nested"][0]["value"] = new
    updated[container]["review_budget"]["review_artifacts"][0]["artifact"]["value"] = new
    path = tmp_path / "state.sqlite3"
    connection = _open(path, create=True)
    try:
        jobs.save_jobs(connection, "r", original, {})
        jobs.save_jobs(connection, "r", updated, original)
        connection.commit()
    finally:
        connection.close()
    connection = _open(path)
    try:
        assert encode_json(jobs.load_jobs(connection, "r")) == encode_json(updated)
    finally:
        connection.close()


def test_budget_window_rejects_python_equal_json_type_replacement(tmp_path: Path) -> None:
    original = {"ticket_jobs": {"2": {"ticket_number": 2, "review_budget_history": [
        {"phase": "reviewing", "review_context": {"value": 1}},
    ]}}}
    changed = deepcopy(original)
    changed["ticket_jobs"]["2"]["review_budget_history"][0]["review_context"]["value"] = True
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", original, {})
        connection.commit()
        with pytest.raises(ValueError, match="Historical budget evidence cannot be changed"):
            jobs.save_jobs(connection, "r", changed, original)
        connection.rollback()
        value = jobs.load_jobs(connection, "r")["ticket_jobs"]["2"]["review_budget_history"][0]["review_context"]["value"]
        assert type(value) is int and value == 1
    finally:
        connection.close()


@pytest.mark.parametrize("field", ["review_context", "review_budget"])
def test_concurrent_evidence_comparison_distinguishes_json_scalar_types(tmp_path: Path, field: str) -> None:
    def state(value: Any) -> dict[str, Any]:
        content = {"value": value} if field == "review_context" else new_budget(review_artifacts=[{"artifact": {"value": value}}])
        return {"ticket_jobs": {"2": {"ticket_number": 2, field: content}}}

    original, current, stale = state(1), state(True), state(1.0)
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", original, {})
        jobs.save_jobs(connection, "r", current, original)
        connection.commit()
        with pytest.raises(ValueError, match=f"changed concurrently: {field}"):
            jobs.save_jobs(connection, "r", stale, original)
        connection.rollback()
        assert jobs.load_jobs(connection, "r")["ticket_jobs"]["2"][field] == current["ticket_jobs"]["2"][field]
    finally:
        connection.close()


@pytest.mark.parametrize("value", [True, 1.0])
def test_exact_comparison_preserves_typed_core_counter_validation(tmp_path: Path, value: Any) -> None:
    original = {"ticket_jobs": {"2": {"ticket_number": 2, "modification_attempts": 1}}}
    changed = deepcopy(original)
    changed["ticket_jobs"]["2"]["modification_attempts"] = value
    connection = _open(tmp_path / "state.sqlite3", create=True)
    try:
        jobs.save_jobs(connection, "r", original, {})
        connection.commit()
        with pytest.raises(ValueError, match="modification_attempts must be integer"):
            jobs.save_jobs(connection, "r", changed, original)
    finally:
        connection.close()
