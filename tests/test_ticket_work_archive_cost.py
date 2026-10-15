"""Real Ticket writes and lease checks avoid unrelated archived review bodies."""
from __future__ import annotations

import gc
import json
import sqlite3
import tracemalloc
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.delivery import _effective_revision
from agent_run.delivery_policy import ticket_budget_policy_for_job
from agent_run.review_budget import ensure_budget, reset_budget
from agent_run.semantic_attempt import canonical_fingerprint
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state, require_current_work_state
from agent_run.ticket_state import TicketState
from agent_run.ticket_queue import reconcile
from agent_run.ticket_work import TicketWorkAdmission
from agent_run.worker_control import worker_lease
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run
from support.state import persist_run_fixture


def _prepare_cost_run(git_repo: Path, monkeypatch: pytest.MonkeyPatch, *, retry_pending: bool) -> tuple[str, StateStore, dict]:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = str(state["run_id"])
    engine = ticket_engine(git, states, fixture, TicketAgents(git_repo, (3,), lambda _: None), development_only=not retry_pending)
    if retry_pending:
        def unavailable(**kwargs):
            raise OSError("Run Branch sync unavailable")

        monkeypatch.setattr(engine.github, "sync_run_branch", unavailable)
        for _ in range(5):
            engine.deliver_ticket(run_id, 3)
    else:
        engine.deliver_ticket(run_id, 3)
    original = states.load_current_run(run_id)
    assert original is not None
    # A real reviewed sibling supplies complete Attempt and Review Context
    # shapes. Allocate the target's current Job through the production helper.
    original["ticket_jobs"]["2"] = {"ticket_number": 2, **engine._new_job_fields(
        original, 2, _effective_revision(original, original["ticket_graph"]["tickets"]["2"]))}
    sibling = original["ticket_jobs"]["3"]
    if not retry_pending:
        sibling["phase"] = "developing"
    else:
        assert sibling["phase"] == "publication_pending"
        assert sibling["publication_operation_retry"] == {"attempts": 5, "limit": 5}
    original["active_ticket_job"] = deepcopy(sibling)
    if retry_pending:
        original["status"] = "publication_pending"
    original["active_agent_invocation"] = deepcopy(original["ticket_agent_invocations"]["3"])
    reconcile(original, sibling)
    require_current_run_state(original)
    return run_id, states, original


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("retry_pending", [True, False])
def test_sibling_archives_do_not_grow_ticket_commit_or_lease_check_cost(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, aggregate: bool, retry_pending: bool,
) -> None:
    run_id, states, original = _prepare_cost_run(git_repo, monkeypatch, retry_pending=retry_pending)
    sibling = original["ticket_jobs"]["3"]
    reviewer = next(attempt for attempt in sibling["semantic_attempt_history"] if attempt["role"] == "reviewer")
    costs = []
    real_loads, real_dumps = json.loads, json.dumps
    decoded: list[int] = []
    encoded: list[int] = []
    sql_steps = [0]
    measuring_sql = [False]
    real_connect = sqlite3.connect

    def progress():
        sql_steps[0] += 1
        return 0

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        if measuring_sql[0]:
            connection.set_progress_handler(progress, 1)
        return connection

    def loads(value, *args, **kwargs):
        decoded.append(len(value.encode("utf-8")) if isinstance(value, str) else len(value))
        return real_loads(value, *args, **kwargs)

    def dumps(value, *args, **kwargs):
        text = real_dumps(value, *args, **kwargs)
        encoded.append(len(text.encode("utf-8")))
        return text

    monkeypatch.setattr(json, "loads", loads)
    monkeypatch.setattr(json, "dumps", dumps)
    monkeypatch.setattr(sqlite3, "connect", connect)
    for count in (1, 10):
        current = deepcopy(original)
        job = current["ticket_jobs"]["3"]
        for ordinal in range(2, count + 1):
            attempt = deepcopy(reviewer)
            attempt["ordinal"] = ordinal
            attempt["attempt_id"] = canonical_fingerprint({key: attempt[key] for key in (
                "role", "work_subject", "generation", "currentness_boundary_fingerprint", "ordinal", "budget_window")})
            job["semantic_attempt_history"].append(attempt)
        job["reviewer_thread_ids"] += [f"retained-reviewer-{ordinal}" for ordinal in range(count)]
        job["human_blocker_history"] = [{"phase": "reviewing", "human_blockers": ["historical question" * 100]}
                                        for _ in range(count)]
        if not retry_pending:
            for _ in range(count):
                reset_budget(job, ticket_budget_policy_for_job(job))
        current["active_ticket_job"] = deepcopy(job)
        require_current_run_state(current)
        persist_run_fixture(states, run_id, current)
        # All measurements start with a fresh repository, excluding setup and
        # the deliberately complete query. Both SQL paths exercise real disk.
        work_states = StateStore(states.root)
        work_states._repository._aggregate_rows = aggregate
        admission = TicketWorkAdmission(work_states, run_id)
        decoded.clear()
        encoded.clear()
        gc.collect()
        collection_enabled = gc.isenabled()
        # Measure the conservative unreclaimed peak under the same collection
        # policy. Periodic GC timing otherwise depends on setup allocations.
        gc.disable()
        sql_steps[0] = 0
        measuring_sql[0] = True
        tracemalloc.start()
        try:
            token = admission.acquire(2, 1, "development")
            assert token is not None
            admission.check_control(token)
            lane = TicketState(work_states, run_id, 2)
            projected = lane.load_current_run(run_id)
            assert projected is not None
            assert "semantic_attempt_history" not in projected["ticket_jobs"]["3"]
            assert "review_budget_history" not in projected["ticket_jobs"]["3"]
            if retry_pending:
                assert set(work_states._repository.work_attempts()) == {'["ticket_jobs","3"]', '["active_ticket_job"]'}
                assert all(attempt["role"] == "publication" for attempt in work_states._repository.work_attempts().values())
            lease_context = worker_lease.set(token)
            try:
                projected["active_ticket_job"]["development_summary"] = "small target update"
                lane.save_run(run_id, projected)
            finally:
                worker_lease.reset(lease_context)
            admission.check_control(token)
            admission.release(token)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            measuring_sql[0] = False
            if collection_enabled:
                gc.enable()
        costs.append((sum(decoded), sum(encoded), peak, sql_steps[0]))
        saved = StateStore(states.root).load_current_run(run_id)
        assert saved is not None
        assert saved["ticket_jobs"]["3"] == current["ticket_jobs"]["3"]
        assert saved["active_ticket_job"] == current["active_ticket_job"]
        assert saved["agent_invocation_history"] == current["agent_invocation_history"]
        assert saved["ticket_jobs"]["2"]["development_summary"] == "small target update"
        assert saved["ticket_work_queue"] == []
        thin = work_states.load_work_run(run_id, 2)
        assert thin is not None
        archives = work_states._repository.work_archives()
        # The explicit archive scope never makes a partial tree acceptable at
        # the complete boundary, and failures cannot leak a budget exemption.
        with pytest.raises(ValueError, match="review_budget_history"):
            require_current_run_state(thin)
        invalid = deepcopy(thin)
        invalid["ticket_jobs"]["3"]["review_budget"]["development_attempts"] = 999
        invalid["active_ticket_job"]["review_budget"]["development_attempts"] = 999
        with pytest.raises(ValueError, match="review_budget"):
            require_current_work_state(invalid, archives, work_states._repository.work_attempts())
        with pytest.raises(ValueError, match="review_budget_history"):
            ensure_budget(thin["ticket_jobs"]["3"], ticket_budget_policy_for_job(thin["ticket_jobs"]["3"]))
        # Other pause probes or a complete public query must not replace the
        # scope of an older work snapshot that is still being advanced.
        snapshot = lane.load_current_run(run_id)
        lane.load_current_run(run_id, complete=True)
        lane.validate_work_projection(snapshot)
        lane.validate_work_projection(deepcopy(snapshot))
        with pytest.raises(ValueError, match="another Ticket"):
            TicketState(work_states, run_id, 3).validate_work_projection(snapshot)
        different_run = deepcopy(snapshot)
        different_run["run_id"] = "another-run"
        with pytest.raises(ValueError, match="another Ticket"):
            lane.validate_work_projection(different_run)
        invalid_scope = deepcopy(snapshot)
        invalid_scope["ticket_jobs"]["3"]["review_budget_history"] = []
        with pytest.raises(ValueError, match="archive was loaded"):
            lane.validate_work_projection(invalid_scope)
        # A plain JSON/container copy has no scope metadata and must satisfy
        # the complete contract instead of inheriting a later read's scope.
        plain = dict(snapshot)
        with pytest.raises(ValueError, match="canonical timeline"):
            lane.validate_work_projection(plain)
        plain.update((field, json.loads(scope.encoded)) for field, scope in snapshot.timelines.items())
        with pytest.raises(ValueError, match="review_budget_history"):
            lane.validate_work_projection(plain)
    # The small current window number grows by one digit; unrelated retained
    # bodies must never contribute to processing or transient memory cost.
    assert costs[1][0] <= costs[0][0] + 256, costs
    assert costs[1][1] <= costs[0][1] + 256, costs
    assert costs[1][2] <= costs[0][2] + 128 * 1024, costs
    assert costs[1][3] <= costs[0][3] + 256, costs
    print(f"native={aggregate},retry={retry_pending}: decoded/encoded/peak bytes/SQL VM steps for sibling archives 1→10: {costs}")


@pytest.mark.parametrize("aggregate", [True, False])
@pytest.mark.parametrize("damage", ["position", "missing", "refs"])
def test_related_attempt_damage_is_rejected_by_complete_and_work_queries(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, aggregate: bool, damage: str,
) -> None:
    run_id, states, original = _prepare_cost_run(git_repo, monkeypatch, retry_pending=True)
    persist_run_fixture(states, run_id, original)
    parent = '["ticket_jobs","3","semantic_attempt_history"]'
    with states._repository.atomic():
        connection = states._repository._local.connection
        path = connection.execute(
            "SELECT object_path FROM run_job_attempts WHERE run_id=? AND parent_path=? AND role='publication'",
            (run_id, parent),
        ).fetchone()[0]
        if damage == "position":
            connection.execute("UPDATE run_job_attempts SET position=position+1 WHERE run_id=? AND object_path=?", (run_id, path))
        elif damage == "missing":
            connection.execute("DELETE FROM run_job_attempts WHERE run_id=? AND object_path=?", (run_id, path))
        else:
            connection.execute("DELETE FROM run_object_material_refs WHERE run_id=? AND object_path=?", (run_id, path))
    reopened = StateStore(states.root)
    with pytest.raises(ValueError, match="Attempt index|material references"):
        reopened.load_current_run(run_id)
    reopened._repository._aggregate_rows = aggregate
    with pytest.raises(ValueError, match="Attempt|references|publication_operation_retry"):
        reopened.load_work_run(run_id, 2)
