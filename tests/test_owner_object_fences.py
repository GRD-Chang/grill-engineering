"""对象提交核对当前需求、完成证据并回滚关联事实。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.object_state import object_store
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.state import ConcurrentRunStateError, StateStore
from agent_run.state_errors import IncompatibleRunStateError
from support.run_agents import ScriptedRunAgents
from support.run_scenarios import _completed_run
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


def test_parent_commit_rejects_changed_demand_even_without_changing_owner(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    run_id = state["run_id"]
    parent = object_store(states, parent_scope=True)
    staged = parent.load_current_run(run_id)
    assert staged is not None
    changed = StateStore(states.root).load_current_run(run_id)
    assert changed is not None
    changed["parent"]["revision"] = "new-parent-revision"
    changed["accepted_parent_spec_revision"] = "new-parent-revision"
    states.save_run(run_id, changed)
    staged["updated_at"] = "2026-10-13T00:00:00+00:00"
    with pytest.raises(ConcurrentRunStateError, match="Parent demand changed"):
        parent.save_run(run_id, staged)
    assert StateStore(states.root).load_current_run(run_id) == changed


def test_outer_rollback_drops_provisional_baseline_and_notification(git_repo: Path) -> None:
    state, states, _git, _fixture = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    before = StateStore(states.root).load_current_run(run_id)
    adapter = object_store(states)
    observations: list[dict[str, object]] = []
    states.run_saved_observer = observations.append

    def interrupted(_current):
        staged = adapter.load_current_run(run_id)
        assert staged is not None
        staged["updated_at"] = "2026-10-13T00:00:00+00:00"
        adapter.save_run(run_id, staged)
        raise OSError("outer transaction interrupted")

    with pytest.raises(OSError, match="outer transaction interrupted"):
        states.update_run(run_id, interrupted)
    assert StateStore(states.root).load_current_run(run_id) == before
    assert observations == []
    staged = adapter.load_current_run(run_id)
    assert staged is not None
    staged["updated_at"] = "2026-10-13T01:00:00+00:00"
    adapter.save_run(run_id, staged)
    assert len(observations) == 1
    assert StateStore(states.root).load_current_run(run_id)["updated_at"] == staged["updated_at"]


def test_parent_acceptance_validates_completed_ticket_authority_at_evidence_boundary(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    run_id = state["run_id"]
    damaged = deepcopy(state)
    damaged["ticket_jobs"]["2"]["deterministic_integration_record"]["publication_sha"] = "different-publication"
    states.save_run(run_id, damaged)
    agents = ScriptedRunAgents()
    with pytest.raises(IncompatibleRunStateError, match="integrated_publication_sha"):
        RunAcceptanceEngine(git=git, states=states, agents=agents).accept(run_id)
    assert StateStore(states.root).load_run(run_id) == damaged


@pytest.mark.parametrize("concurrent_change", [False, True])
def test_controller_readback_reselects_retired_invocation_without_losing_concurrency_fence(
    git_repo: Path, concurrent_change: bool,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    engine = ticket_engine(
        git, states, fixture, TicketAgents(git_repo, (2,), lambda _: None),
        development_only=True,
    )
    engine.deliver_ticket(run_id, 2)
    adapter = object_store(states)
    state = adapter.load_scheduler_current_run(run_id)
    assert state is not None
    assert state["agent_invocation_history"]
    before = deepcopy(states.load_current_run(run_id)["agent_invocation_history"])
    with adapter.require_unchanged_run(run_id, state):
        state["active_agent_invocation"] = None
        state["ticket_agent_invocations"].pop("2")
        adapter.save_run(run_id, state)
        if concurrent_change:
            changed = states.load_current_run(run_id)
            changed["updated_at"] = "2026-10-13T02:00:00+00:00"
            states.save_run(run_id, changed)
            with pytest.raises(ConcurrentRunStateError, match="before Controller returned"):
                adapter.load_unchanged_run(run_id)
        else:
            latest = adapter.load_unchanged_run(run_id)
            assert latest is not None
            assert latest["agent_invocation_history"] == []
    assert states.load_current_run(run_id)["agent_invocation_history"] == before
