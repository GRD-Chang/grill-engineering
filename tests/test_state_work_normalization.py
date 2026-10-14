"""工作提交复用已持久化字段时仍脱敏修改，并保留独立续接基线。"""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.state import StateStore
from agent_run.ticket_state import TicketState
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("void_hook", [False, True])
def test_ticket_partial_save_keeps_normalization_and_reopens_independent_baselines(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, void_hook: bool,
) -> None:
    initial, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = initial["run_id"]
    ticket_engine(git, states, fixture, TicketAgents(git_repo, (2,), lambda _: None),
                  development_only=True).deliver_ticket(run_id, 2)
    if void_hook:
        original = states._save_work_and_notify_unlocked

        def save(*args, **kwargs) -> None:
            original(*args, **kwargs)

        monkeypatch.setattr(states, "_save_work_and_notify_unlocked", save)
    lane = TicketState(states, run_id, 2)
    current = lane.load_current_run(run_id)
    unchanged = deepcopy(current["active_ticket_job"]["review_budget"])
    job = current["active_ticket_job"]
    job["evidence"] = {"message": "token=private-value", "stdout": "private-output"}
    job["environment"] = {"PRIVATE_TOKEN": "private-value"}
    job["extension_number"] = 1
    lane.save_run(run_id, current)
    durable = StateStore(states.root).load_current_run(run_id)
    assert durable["ticket_jobs"]["2"]["evidence"] == {"message": "token=[REDACTED]"}
    assert "environment" not in durable["ticket_jobs"]["2"]
    assert durable["ticket_jobs"]["2"]["review_budget"] == unchanged
    # A later save against the same lane must use the normalized committed
    # baseline, even though the caller still owns its unsanitized input tree.
    job["evidence"] = {"error": "password=second-value\x00"}
    job["extension_number"] = True
    lane.save_run(run_id, current)
    reopened = StateStore(states.root).load_current_run(run_id)
    saved_job = reopened["ticket_jobs"]["2"]
    assert saved_job["evidence"] == {"error": "password=[REDACTED]"}
    assert saved_job["extension_number"] is True
    assert saved_job["review_budget"] == unchanged
    job["review_budget"]["caller_mutation"] = True
    assert StateStore(states.root).load_current_run(run_id)["ticket_jobs"]["2"]["review_budget"] == unchanged
