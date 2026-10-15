"""历史保留缓存返回的真实 Agent 关联，而实时状态继续确定性收口。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_return import AgentReturn, store_agent_return
from agent_run.agents import DevelopmentResult, PublicationResult, ReviewResult
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt
from agent_run.state import StateStore
from agent_run.work_facts import work_role
from support.run_artifacts import _passing_artifact
from support.run_publication import RunPublicationAgents


def _bound_invocation(attempt: dict[str, Any], **facts: Any) -> dict[str, Any]:
    role = {"development": "development", "reviewer": "fresh_acceptance", "publication": "publication"}[attempt["role"]]
    phase = {"development": "developing", "fresh_acceptance": "reviewing", "publication": "publication"}[role]
    if attempt["work_subject"].startswith("run-acceptance:") and role == "fresh_acceptance":
        role, phase = "reviewer", "run_acceptance"
    elif attempt["work_subject"].startswith("run-publication:"):
        role, phase = "final_publication", "run_publication"
    return {"work_subject": attempt["work_subject"], "started_at": "2026-10-09T00:00:00+00:00",
            "generation": attempt["generation"], "role": role, "phase": phase,
            "mode": "fresh", "input_fingerprint": "fixture", "currentness_boundary": {},
            "semantic_attempt": deepcopy(attempt), "status": "running", **facts}


def test_retired_attempt_snapshot_preserves_owner_and_invocation_start_version(tmp_path: Path) -> None:
    import sqlite3

    from agent_run.sqlite_state import SQLiteStateRepository

    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    owner: dict[str, Any] = {"parent_generation": 1}
    attempt = allocate_semantic_attempt(owner, role="development", work_subject="parent-only:run-1",
                                        generation=1, currentness_boundary={}, ordinal=1)
    invocation = _bound_invocation(attempt, status="completed")
    repository.save("run-1", {"parent_job": owner, "agent_invocation_history": [invocation]})
    with sqlite3.connect(repository.path) as connection:
        original = connection.execute("SELECT job_id,version_id FROM work_attempts JOIN work_attempt_current "
                                      "USING(run_id,attempt_id) WHERE run_id='run-1'").fetchone()
    closed = deepcopy(owner)
    close_semantic_attempt(closed, closed["pending_semantic_attempt"], outcome="currentness_invalidated")
    archive = {"owner_kind": "parent", "work_subject": "parent-only:run-1", "generation": 1,
               "semantic_attempt_history": closed["semantic_attempt_history"]}
    repository.save_objects("run-1", {"parent_job": {}, "retired_semantic_attempt_owners": [archive]},
                            fields={"parent_job", "retired_semantic_attempt_owners"})
    reopened = SQLiteStateRepository(tmp_path, max_bytes=65536).load("run-1")
    assert reopened["retired_semantic_attempt_owners"] == [archive]
    assert reopened["agent_invocation_history"] == [invocation]
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT job_id,version_id FROM work_attempts JOIN work_attempt_current "
                                  "USING(run_id,attempt_id) WHERE run_id='run-1'").fetchone() == original
        archived = connection.execute("SELECT h.version_id FROM work_job_attempt_history h JOIN work_jobs j "
                                      "USING(run_id,job_id) WHERE j.owner_kind='retired_attempt_owner'").fetchone()[0]
        assert archived != original[1]
    from agent_run.sqlite_work_execution import save_attempts
    with repository.atomic():
        connection = repository._local.connection
        archive_id = connection.execute("SELECT job_id FROM work_jobs WHERE owner_kind='retired_attempt_owner'").fetchone()[0]
        counts = tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                       for table in ("work_attempt_versions", "work_evidence", "work_evidence_materials"))
        save_attempts(connection, "run-1", archive_id, archive, archive)
        assert tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("work_attempt_versions", "work_evidence", "work_evidence_materials")) == counts
    assert SQLiteStateRepository(tmp_path, max_bytes=65536).load("run-1") == reopened


@pytest.mark.parametrize(("location", "role", "expected_worker"), [
    ("active_ticket_job", "development", "开发工作代理"),
    ("parent_job", "development", "开发工作代理"),
    ("repair_job", "development", "开发工作代理"),
    ("run_acceptance", "development", "开发工作代理"),
    ("active_ticket_job", "reviewer", "独立验收工作代理"),
    ("run_acceptance", "reviewer", "运行验收工作代理"),
    ("active_ticket_job", "publication", "发布工作代理"),
    ("run_publication", "publication", "运行发布工作代理"),
])
def test_cached_return_history_retains_role_attempt_and_returned_thread(
    tmp_path: Path, location: str, role: str, expected_worker: str,
) -> None:
    subject: dict[str, Any] = {
        "phase": {"development": "developing", "reviewer": "reviewing", "publication": "publishing"}[role],
        "development_thread_id": "stale-development",
        "reviewer_thread_ids": ["stale-reviewer"],
        "publication_thread_id": "stale-publication",
        "pending_attempt": 4,
    }
    state: dict[str, Any] = {"run_id": "run-1", "status": "running"}
    if location == "repair_job":
        state["status"] = "run_acceptance_pending"
        state["run_acceptance"] = {"phase": "repairing", "repair_job": subject}
    else:
        state[location] = subject
    if location == "active_ticket_job":
        subject["ticket_number"] = 3
    if location == "run_acceptance":
        state["status"] = "run_acceptance_pending"
    if location == "run_publication":
        state["status"] = "run_publication_pending"
    work_subject = {
        "active_ticket_job": "ticket:3", "parent_job": "parent-only:run-1",
        "repair_job": "run-repair:run-1", "run_acceptance": "run-acceptance:run-1",
        "run_publication": "run-publication:run-1",
    }[location]
    attempt = allocate_semantic_attempt(
        subject, role=role, work_subject=work_subject, generation=1,
        currentness_boundary={}, ordinal=4,
    )
    thread = f"returned-{role}"
    result: AgentReturn
    if role == "development":
        result = DevelopmentResult(thread, "完成开发")
    elif role == "reviewer":
        result = ReviewResult(thread, _passing_artifact())
    else:
        result = PublicationResult(thread, RunPublicationAgents().run_publication({}))
    attempt["returned_result"] = store_agent_return(result, attempt)
    # An unrelated Invocation must not override the durable return's identity.
    unrelated = allocate_semantic_attempt({}, role="development", work_subject="ticket:99",
                                          generation=1, currentness_boundary={}, ordinal=1)
    state["active_agent_invocation"] = _bound_invocation(unrelated, reported_thread_id="unrelated-thread")
    assert work_role(subject) is None
    store = StateStore(tmp_path)
    store.save_run("run-1", state)
    event = store.load_run("run-1")["timeline"][-1]
    assert event["worker"] == expected_worker
    assert event["attempt"] == 4
    assert event["thread_id"] == thread
    assert event["semantic_attempt_id"] == attempt["attempt_id"]


@pytest.mark.parametrize("matching", [True, False])
def test_history_uses_only_invocation_bound_to_pending_attempt(
    tmp_path: Path, matching: bool,
) -> None:
    subject: dict[str, Any] = {
        "ticket_number": 3, "phase": "reviewing", "reviewer_thread_ids": ["prior-reviewer"],
    }
    attempt = allocate_semantic_attempt(
        subject, role="reviewer", work_subject="ticket:3", generation=1,
        currentness_boundary={}, ordinal=2,
    )
    bound = attempt if matching else allocate_semantic_attempt(
        {}, role="reviewer", work_subject="ticket:3", generation=1, currentness_boundary={}, ordinal=3,
    )
    state: dict[str, Any] = {
        "run_id": "run-1", "status": "running", "active_ticket_job": subject,
        "active_agent_invocation": _bound_invocation(bound, reported_thread_id="current-reviewer"),
    }
    StateStore(tmp_path).save_run("run-1", state)
    event = state["timeline"][-1]
    assert event["worker"] == "独立验收工作代理"
    assert event["attempt"] == 2
    assert event.get("thread_id") == ("current-reviewer" if matching else None)


def test_publication_wire_return_history_uses_returned_thread(tmp_path: Path) -> None:
    subject: dict[str, Any] = {"phase": "publishing", "thread_id": "prior-publisher"}
    attempt = allocate_semantic_attempt(
        subject, role="publication", work_subject="run-publication:run-1", generation=1,
        currentness_boundary={}, ordinal=3,
    )
    artifact = RunPublicationAgents().run_publication({})
    artifact["_thread_id"] = "returned-publisher"
    attempt["returned_result"] = store_agent_return(artifact, attempt)
    state: dict[str, Any] = {
        "run_id": "run-1", "status": "run_publication_pending", "run_publication": subject,
    }
    StateStore(tmp_path).save_run("run-1", state)
    event = state["timeline"][-1]
    assert event["worker"] == "运行发布工作代理"
    assert event["attempt"] == 3
    assert event["thread_id"] == "returned-publisher"


@pytest.mark.parametrize("location", ["active_ticket_job", "run_acceptance"])
@pytest.mark.parametrize("continuation", ["fresh", "explicit_resume", "human_resume", "new_thread"])
def test_pending_reviewer_history_does_not_attribute_a_prior_thread_to_fresh_work(
    tmp_path: Path, location: str, continuation: str,
) -> None:
    subject: dict[str, Any] = {
        "phase": "reviewing", "reviewer_thread_ids": ["previous-reviewer"],
    }
    run_scope = location == "run_acceptance"
    prefix = "reviewer" if run_scope else "review"
    if continuation in {"explicit_resume", "new_thread"}:
        subject[f"{prefix}_resume_thread_id"] = "explicit-reviewer"
    if continuation == "human_resume":
        subject["prior_human_blockers" if run_scope else "review_human_blocker_resume"] = ["需要权限"] if run_scope else True
    if continuation == "new_thread":
        subject[f"{prefix}_new_thread"] = True
    if not run_scope:
        subject["ticket_number"] = 3
    allocate_semantic_attempt(
        subject, role="reviewer", work_subject="run-acceptance:run-1" if run_scope else "ticket:3",
        generation=1, currentness_boundary={}, ordinal=2,
    )
    state: dict[str, Any] = {
        "run_id": "run-1", "status": "run_acceptance_pending" if run_scope else "running",
        location: subject,
    }
    StateStore(tmp_path).save_run("run-1", state)
    event = state["timeline"][-1]
    assert event["attempt"] == 2
    if continuation in {"fresh", "new_thread"}:
        assert "thread_id" not in event
    else:
        assert event["thread_id"] == ("explicit-reviewer" if continuation == "explicit_resume" else "previous-reviewer")


@pytest.mark.parametrize("invocation_ticket", [3, 4])
def test_multi_ticket_history_counters_belong_to_marker_work(
    tmp_path: Path, invocation_ticket: int,
) -> None:
    jobs: dict[str, Any] = {}
    for number in (3, 4):
        job: dict[str, Any] = {"ticket_number": number, "phase": "reviewing"}
        allocate_semantic_attempt(
            job, role="reviewer", work_subject=f"ticket:{number}", generation=1,
            currentness_boundary={}, ordinal=number,
        )
        jobs[str(number)] = job
    invocation = _bound_invocation(jobs[str(invocation_ticket)]["pending_semantic_attempt"],
                                   reported_thread_id=f"reviewer-{invocation_ticket}", attempt_count=2)
    state: dict[str, Any] = {
        "run_id": "run-1", "status": "running", "ticket_jobs": jobs,
        "active_ticket_job": jobs["3"], "active_agent_invocation": invocation,
        "agent_invocation_history": [deepcopy(invocation)],
    }
    store = StateStore(tmp_path)
    store.save_run("run-1", state)
    saved = store.load_run("run-1")
    event = saved["timeline"][-1]
    assert event["ticket"] == 3
    assert event["attempt"] == event["semantic_attempt_ordinal"] == 3
    assert event["semantic_attempt_id"] == jobs["3"]["pending_semantic_attempt"]["attempt_id"]
    assert saved["agent_invocation_history"] == [invocation]
    if invocation_ticket == 3:
        assert event["thread_id"] == "reviewer-3"
        assert event["agent_invocation_status"] == "running"
        assert event["output_attempt"] == 2
    else:
        assert "thread_id" not in event
        assert "agent_invocation_status" not in event
        assert "output_attempt" not in event



def test_run_status_transition_retains_actual_invocation_continuation(tmp_path: Path) -> None:
    repair: dict[str, Any] = {"phase": "developing"}
    attempt = allocate_semantic_attempt(
        repair, role="development", work_subject="run-repair:run-1", generation=1,
        currentness_boundary={}, ordinal=2,
    )
    state: dict[str, Any] = {
        "run_id": "run-1", "status": "parent_delivery_pending",
        "run_acceptance": {"phase": "repairing", "repair_job": repair},
        "active_agent_invocation": _bound_invocation(attempt, status="resuming", reported_thread_id="repair-worker", attempt_count=1),
    }
    store = StateStore(tmp_path)
    store.save_run("run-1", state)
    event = store.load_run("run-1")["timeline"][-1]
    assert event["kind"] == "run_status"
    assert event["semantic_attempt_id"] == attempt["attempt_id"]
    assert event["semantic_attempt_role"] == "development"
    assert event["semantic_attempt_ordinal"] == 2
    assert event["agent_invocation_status"] == "resuming"
