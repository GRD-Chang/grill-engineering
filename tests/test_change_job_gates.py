"""Real currentness refreshes retain independent recovery conditions."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from agent_run.controller import Controller, _resume_ticket_execution_failure
from agent_run.ci_recovery_budget import new_ticket_ci_recovery_budget
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.object_state import current_projection_facts, object_store
from agent_run.operator_gate import run_failure_diagnostic
from agent_run.review_budget import TICKET_POLICY, new_budget
from agent_run.revisions import effective_revision
from agent_run.sqlite_state import SQLiteStateRepository
from support.state import persist_run_fixture
from support.ticket_parallel import ticket_run


def _gates(path: Path, run_id: str, number: int = 2) -> list[tuple[str, str | None]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT gate_kind,reason FROM work_job_gates WHERE run_id=? AND job_id=? "
                                  "ORDER BY priority DESC,gate_id", (run_id, f"ticket:{number}:generation:1")).fetchall()


def _ticket_with_pr(repo: Path, reason: str, resume_field: str, *, global_failure: bool = False) -> tuple[Any, ...]:
    state, states, git, fixture = ticket_run(repo, (2, 3))
    run_id = state["run_id"]
    job = state["active_ticket_job"]
    publisher = FixtureGitHubPublisher(fixture, git)
    head = git.resolve(state["run_branch"])
    branch = f"agent-run/{run_id}/ticket-2"
    publisher.ensure_ticket_branch(ticket_number=2, branch=branch, base_branch=state["run_branch"],
        expected_base_sha=head, expected_remote_sha=head, recovery_remote_sha=head)
    pr = publisher.ensure_ticket_pr(branch=branch, base_branch=state["run_branch"], title="Ticket", body="Ticket",
        primary_ticket=2, expected_head_sha=head, expected_base_sha=head)
    stage = "developing" if reason == "modification_budget_exhausted" else "reviewing"
    used = TICKET_POLICY.development_limit if reason == "modification_budget_exhausted" else 0
    job.update(ticket_branch_generation=1, ticket_branch=branch, phase="blocked", blocked_reason=reason,
        review_budget=new_budget(development_attempts=used), review_budget_history=[], modification_attempts=used,
        ci_recovery=new_ticket_ci_recovery_budget(), policy_snapshot=deepcopy(state["policy_snapshot"]),
        effective_revision=effective_revision(ticket_revision=state["ticket_graph"]["tickets"]["2"]["content_revision"],
            parent_revision=state["parent"]["revision"], graph_revision=state["ticket_graph"]["revision"]),
        base_sha=head, candidate_sha=head, pr_number=pr, publication_sha=head)
    job[resume_field] = stage
    if reason == "agent_requires_human":
        job["human_blockers"] = ["Keep the original human response boundary"]
    if reason == "ticket_execution_failed":
        job["execution_failure"] = "original execution failure"
    state["ticket_jobs"]["2"] = job
    state.update(status="blocked", terminal_kind=None, diagnostics=[])
    if global_failure:
        state.update(status="execution_failed", terminal_kind="execution_failed", diagnostics=[
            run_failure_diagnostic(state, code="shared_failure", message="Retain Run control"),
        ])
    persist_run_fixture(states, run_id, state)
    states.load_current_run(run_id)
    store = object_store(states)
    return run_id, states, store, Controller(FixtureGitHubReader(fixture), git, states), fixture, stage


@pytest.mark.parametrize(("reason", "resume_field"), [
    ("ticket_execution_failed", "execution_resume_phase"),
    ("agent_requires_human", "human_blocker_phase"),
    ("ticket_paused", "pause_resume_phase"),
    ("modification_budget_exhausted", "execution_resume_phase"),
])
@pytest.mark.parametrize("global_failure", [False, True])
def test_external_pr_overlay_preserves_original_gate_and_save_frame(
    git_repo: Path, reason: str, resume_field: str, global_failure: bool,
) -> None:
    run_id, states, store, controller, fixture, stage = _ticket_with_pr(git_repo, reason, resume_field, global_failure=global_failure)
    current = store.load_current_run(run_id)
    before = deepcopy(current)
    observations: list[dict[str, Any]] = []
    states.run_saved_observer = observations.append
    fixture_data = json.loads(fixture.read_text())
    fixture_data["delivery"]["pull_requests"][0]["base_branch"] = "foreign"
    fixture.write_text(json.dumps(fixture_data))

    controller._mark_stale_change_job(current)
    store.save_run(run_id, current)

    assert {gate[1] for gate in _gates(states._repository.path, run_id)} == {reason, "change_pr_base_changed_externally"}
    with closing(sqlite3.connect(states._repository.path)) as connection:
        assert connection.execute("SELECT stage FROM work_jobs WHERE run_id=? AND job_id=?",
                                  (run_id, "ticket:2:generation:1")).fetchone() == (stage,)
    fixture_data["delivery"]["pull_requests"][0]["base_branch"] = before["run_branch"]
    fixture.write_text(json.dumps(fixture_data))
    current = store.load_current_run(run_id)
    owner = current["ticket_jobs"]["2"]

    controller._mark_stale_change_job(current)
    store.save_run(run_id, current)
    reopened = states.load_current_run(run_id)

    assert owner is current["ticket_jobs"]["2"]
    assert current["active_ticket_job"] == current["ticket_jobs"]["2"]
    assert current_projection_facts(current)["ticket_jobs"] == current_projection_facts(reopened)["ticket_jobs"]
    assert owner["phase"] == "blocked" and owner["blocked_reason"] == reason
    assert owner[resume_field] == stage
    assert len(_gates(states._repository.path, run_id)) == 1
    assert owner["review_budget"] == before["ticket_jobs"]["2"]["review_budget"]
    assert current_projection_facts(observations[-1])["ticket_jobs"]["2"] == current_projection_facts(reopened)["ticket_jobs"]["2"]
    if reason == "agent_requires_human":
        assert owner["human_blockers"] == before["ticket_jobs"]["2"]["human_blockers"]
    if global_failure:
        assert current["status"] == reopened["status"] == "execution_failed"
        assert current["diagnostics"] == reopened["diagnostics"] == before["diagnostics"]
    else:
        assert current["status"] == reopened["status"] == "blocked"
        assert current["diagnostics"][0]["code"] == reopened["diagnostics"][0]["code"] == reason


def test_explicit_execution_resume_reveals_retained_pause_in_same_frame(git_repo: Path) -> None:
    run_id, states, store, _controller, _fixture, stage = _ticket_with_pr(git_repo, "ticket_paused", "pause_resume_phase")
    current = store.load_current_run(run_id)
    current["ticket_jobs"]["2"].update(blocked_reason="ticket_execution_failed", execution_resume_phase=stage)
    store.save_run(run_id, current)
    assert len(_gates(states._repository.path, run_id)) == 2
    owner = current["ticket_jobs"]["2"]

    assert _resume_ticket_execution_failure(current, ticket_number=2)
    store.save_run(run_id, current)
    reopened = states.load_current_run(run_id)

    assert current["ticket_jobs"]["2"] is owner
    assert owner["phase"] == "blocked" and owner["blocked_reason"] == "ticket_paused"
    assert owner["pause_resume_phase"] == stage
    assert current["active_ticket_job"] is owner
    assert current_projection_facts(current)["ticket_jobs"]["2"] == current_projection_facts(reopened)["ticket_jobs"]["2"]
    assert current["status"] == reopened["status"] == "blocked"
    assert _gates(states._repository.path, run_id) == [("pause", "ticket_paused")]


def test_same_kind_condition_change_preserves_exact_old_condition(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    job = {"ticket_number": 2, "phase": "blocked", "blocked_reason": "ticket_execution_failed", "execution_resume_phase": "reviewing"}
    original = {"ticket_jobs": {"2": job}}
    repository.save("r", original)
    changed = deepcopy(original)
    changed["ticket_jobs"]["2"]["blocked_reason"] = "ticket_supervision_timeout"
    repository.save_objects("r", changed, fields={"ticket_jobs"})
    assert {reason for _, reason in _gates(repository.path, "r")} == {"ticket_execution_failed", "ticket_supervision_timeout"}
    current = repository.load("r")
    visible = current["ticket_jobs"]["2"].pop("blocked_reason")
    current["ticket_jobs"]["2"].pop("execution_resume_phase")
    current["ticket_jobs"]["2"]["phase"] = "reviewing"
    repository.save_objects("r", current, fields={"ticket_jobs"})
    remaining = repository.load("r")["ticket_jobs"]["2"]
    assert remaining["phase"] == "blocked" and remaining["blocked_reason"] != visible
    assert len(_gates(repository.path, "r")) == 1


def test_sibling_gate_projection_preserves_selection_and_continuing_job(tmp_path: Path) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    jobs = {"1": {"ticket_number": 1, "phase": "completed"},
            "2": {"ticket_number": 2, "phase": "blocked", "blocked_reason": "ticket_paused", "pause_resume_phase": "reviewing"},
            "3": {"ticket_number": 3, "phase": "developing", "review_context": {"body": "continuing"}}}
    original = {"status": "active", "ticket_jobs": jobs, "active_ticket_job": jobs["1"]}
    repository.save("r", original)
    overlay = deepcopy(original)
    overlay["ticket_jobs"]["2"].update(blocked_reason="ticket_execution_failed", execution_resume_phase="reviewing")
    repository.save_objects("r", overlay, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
    current = repository.load("r")
    target = current["ticket_jobs"]["2"]
    target.update(phase="reviewing")
    target.pop("blocked_reason")
    target.pop("execution_resume_phase")
    with repository.atomic():
        repository.save_objects("r", current, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
        projected, mirrors = repository.project_saved_job_gates("r", current, fields={"ticket_jobs"}, keyed_changes={"ticket_jobs": {"2"}})
        assert projected and mirrors == set()
    assert current["ticket_jobs"]["2"] is target and target["blocked_reason"] == "ticket_paused"
    assert current["status"] == "active" and current["active_ticket_job"] == jobs["1"]
    assert current["ticket_jobs"]["3"] == jobs["3"]


@pytest.mark.parametrize("stage", ["publishing", "merging", "merged"])
def test_publication_retry_retains_exact_continuation(tmp_path: Path, stage: str) -> None:
    repository = SQLiteStateRepository(tmp_path, max_bytes=65536)
    job = {"ticket_number": 2, "phase": "publication_pending", "execution_resume_phase": stage}
    repository.save("r", {"status": "publication_pending", "terminal_kind": "publication_pending",
                          "ticket_jobs": {"2": job}, "active_ticket_job": job})
    current = repository.load("r")
    owner = current["ticket_jobs"]["2"]
    assert owner["phase"] == "publication_pending" and owner["execution_resume_phase"] == stage
    owner["phase"] = owner.pop("execution_resume_phase")
    repository.save_objects("r", current, fields={"ticket_jobs"})
    restored = repository.load("r")["ticket_jobs"]["2"]
    assert restored["phase"] == stage and "execution_resume_phase" not in restored
    assert _gates(repository.path, "r") == []
