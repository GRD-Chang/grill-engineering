"""完成已落盘、清理尚未登记时，重开可对账非活动 Ticket。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.controller import Controller
from agent_run.delivery_cleanup import DeliveryCleanupEngine
from agent_run.git import GitError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.object_state import object_store
from agent_run.state import SimulatedProcessCrash, StateStore
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("prior_cleanup_completed", [False, True])
def test_reopened_cleanup_registers_completed_ticket_outside_current_projection(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, prior_cleanup_completed: bool,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    if prior_cleanup_completed:
        state["delivery_cleanup"] = {"status": "completed", "items": {}}
        states.save_run(run_id, state)

    def crash_before_registration(_engine, current, job):
        durable = StateStore(states.root).load_current_run(run_id)
        assert durable is not None
        assert durable["ticket_jobs"]["3"]["phase"] == "completed"
        assert job["ticket_branch"] not in durable.get("delivery_cleanup", {}).get("items", {})
        raise SimulatedProcessCrash("completed before cleanup registration")

    with monkeypatch.context() as patch:
        patch.setattr(DeliveryCleanupEngine, "complete_ticket", crash_before_registration)
        with pytest.raises(SimulatedProcessCrash, match="before cleanup registration"):
            ticket_engine(
                git, states, fixture, TicketAgents(git_repo, (2, 3), lambda _: None),
            ).deliver_ticket(run_id, 3)

    Controller(FixtureGitHubReader(fixture), git, states).resume(run_id)
    reopened = StateStore(states.root)
    before = reopened.load_current_run(run_id)
    assert before is not None
    branch = before["ticket_jobs"]["3"]["ticket_branch"]
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    assert checkout.exists()
    assert git.resolve(branch) == before["ticket_jobs"]["3"]["publication_sha"]
    projection = object_store(reopened).load_current_run(run_id)
    assert projection is not None and "3" not in projection["ticket_jobs"]

    def reject_whole_save(*_args, **_kwargs):
        raise AssertionError("cleanup rewrote the complete Run")

    monkeypatch.setattr(reopened, "_save_run_unlocked", reject_whole_save)
    publisher = FixtureGitHubPublisher(fixture, git)
    DeliveryCleanupEngine(git=git, states=reopened, github=publisher).resume(run_id)

    after = StateStore(states.root).load_current_run(run_id)
    assert after is not None
    assert after["delivery_cleanup"]["items"][branch]["status"] == "completed"
    assert after["delivery_cleanup"]["status"] == "completed"
    assert not checkout.exists()
    with pytest.raises(GitError):
        git.resolve(branch)
    assert branch not in publisher.data["delivery"]["published_branches"]
    facts = deepcopy(after)
    if "delivery_cleanup" in before:
        facts["delivery_cleanup"] = before["delivery_cleanup"]
    else:
        facts.pop("delivery_cleanup")
    assert facts == before

    DeliveryCleanupEngine(git=git, states=reopened, github=publisher).resume(run_id)
    assert StateStore(states.root).load_current_run(run_id) == after
