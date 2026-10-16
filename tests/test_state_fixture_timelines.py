"""原始测试前置保全工作视图未读历史，故意缺失的字段继续暴露。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.object_projection import ObjectProjection
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from support.state import persist_run_fixture, write_run_fixture
from support.ticket_parallel import ticket_run


@pytest.mark.parametrize("preparation", ["persist", "replace"])
def test_fixture_materializes_declared_timeline_before_replacing_run(
    git_repo: Path, preparation: str,
) -> None:
    current, states, _, _ = ticket_run(git_repo, (2,))
    run_id = current["run_id"]
    assert isinstance(current, ObjectProjection)
    assert "timeline" in current.timelines and "timeline" not in current
    before = states.load_current_run(run_id)
    assert before is not None and before["timeline"]
    current["updated_at"] = "2026-01-02T00:00:00+00:00"

    if preparation == "persist":
        persist_run_fixture(states, run_id, current)
    else:
        write_run_fixture((states.root, run_id), current)

    assert current["timeline"] == before["timeline"]
    assert current.timelines == {}
    require_current_run_state(current)
    reopened = StateStore(states.root).load_current_run(run_id)
    assert reopened["timeline"] == before["timeline"]
    assert reopened["updated_at"] == current["updated_at"]


@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_raw_fixture_keeps_explicit_timeline_damage(git_repo: Path, damage: str) -> None:
    current, states, _, _ = ticket_run(git_repo, (2,))
    run_id = current["run_id"]
    complete = states.load_current_run(run_id)
    before = deepcopy(complete)
    if damage == "missing":
        complete.pop("timeline")
        write_run_fixture((states.root, run_id), complete)
        with pytest.raises(ValueError, match="timeline"):
            StateStore(states.root).load_current_run(run_id)
    else:
        complete["timeline"] = "invalid"
        with pytest.raises(ValueError, match="Timeline|timeline"):
            write_run_fixture((states.root, run_id), complete)
        assert StateStore(states.root).load_current_run(run_id) == before
