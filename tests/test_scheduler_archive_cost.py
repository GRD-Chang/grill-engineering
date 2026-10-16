"""调度与对象保存不能因当前选择的旧历史增长而展开历史正文。"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from agent_run.delivery import _effective_revision
from agent_run.object_state import object_store
from agent_run.state import StateStore
from support.state import persist_run_fixture
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


def test_scheduler_and_object_save_keep_selected_archive_outside_read_scope(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial, states, git, fixture = ticket_run(git_repo, (2, 3, 4))
    run_id = initial["run_id"]
    engine = ticket_engine(git, states, fixture, TicketAgents(git_repo, (2,), lambda _: None))
    engine.deliver_ticket(run_id, 2)
    original = states.load_current_run(run_id)
    assert original["ticket_jobs"]["2"]["phase"] == "completed"
    for number in (3, 4):
        ticket = original["ticket_graph"]["tickets"][str(number)]
        original["ticket_jobs"][str(number)] = {
            "ticket_number": number,
            **engine._new_job_fields(original, number, _effective_revision(original, ticket)),
        }
    original["ticket_jobs"]["3"].update(
        phase="blocked", blocked_reason="ticket_execution_failed", execution_resume_phase="developing",
    )
    original["ticket_jobs"]["4"]["phase"] = "developing"
    original.update(status="active", terminal_kind=None, diagnostics=[], active_ticket_job=original["ticket_jobs"]["2"])
    body = "archived-selection-body " * 100
    real_loads, real_connect = json.loads, sqlite3.connect
    measured = False
    decoded = 0
    steps = 0
    costs = []

    def loads(value, *args, **kwargs):
        nonlocal decoded
        if measured and body in (value.decode() if isinstance(value, bytes) else value):
            decoded += 1
        return real_loads(value, *args, **kwargs)

    def progress():
        nonlocal steps
        if measured:
            steps += 1
        return 0

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_progress_handler(progress, 1)
        return connection

    monkeypatch.setattr(json, "loads", loads)
    monkeypatch.setattr(sqlite3, "connect", connect)
    for count in (0, 10, 500):
        prepared = deepcopy(original)
        prepared["ticket_jobs"]["2"]["human_blocker_history"] = [
            {"phase": "reviewing", "human_blockers": [body]} for _ in range(count)
        ]
        prepared["active_ticket_job"] = prepared["ticket_jobs"]["2"]
        persist_run_fixture(states, run_id, prepared)
        current = object_store(StateStore(states.root))
        decoded = steps = 0
        measured = True
        try:
            projected = current.load_scheduler_current_run(run_id)
            assert "human_blocker_history" not in projected["active_ticket_job"]
            projected["ticket_jobs"]["4"]["development_summary"] = "Continuing independent work"
            current.save_run(run_id, projected)
        finally:
            measured = False
        costs.append(steps)
        assert decoded == 0
        reopened = StateStore(states.root).load_current_run(run_id)
        assert reopened["active_ticket_job"] == reopened["ticket_jobs"]["2"] == prepared["ticket_jobs"]["2"]
        assert reopened["ticket_jobs"]["3"] == prepared["ticket_jobs"]["3"]
        assert reopened["ticket_jobs"]["4"]["development_summary"] == "Continuing independent work"
    # Empty history may take a different fixed query path. Bound increases
    # as history grows; a lower cost must not count as a growth regression.
    assert max(costs[1:]) <= costs[0] + 256, costs
    assert costs[2] <= costs[1] + 256, costs
