"""有限通知前置保留真实 Reviewer、持久事实与通知 observer。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.git import GitRepository
from agent_run.notification_events import events
from agent_run.notifications import read_notifications
from agent_run.user_defaults import UserDefaultsStore
from support.cli_fixtures import run_agents
from support.notification_scenarios import prepare_blocked_run_acceptance
from support.state import snapshot_runs
from support.workspace import managed_repo, managed_state


def test_blocked_run_notification_scenario_uses_a_real_review_and_preserves_its_owner(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    UserDefaultsStore().configure(notifications={
        "enabled": True, "profile": "test", "open_id": "ou_test", "app_id": "cli_test",
    })
    delivered = []

    def send(_config, rendered, *_args, **_kwargs):
        delivered.append(rendered)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    agents = run_agents(git_repo / "blocked-review.json")
    payload = json.loads(agents.read_text())
    payload["run_reviews"][0]["checks"]["e2e"].update(
        status="blocked", evidence="External test account unavailable.", findings=[],
    )
    agents.write_text(json.dumps(payload))
    fixture, state = prepare_blocked_run_acceptance(git_repo, agents)

    job = state["run_acceptance"]
    pending = job["pending_semantic_attempt"]
    assert state["status"] == "ready_for_human"
    assert state["notifications"]["enabled"] is True
    assert state["notifications"]["mode"] == "detailed"
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert job["review_budget"]["development_attempts"] == 0
    assert pending["role"] == "reviewer"
    assert pending["ordinal"] == 1
    assert pending["work_subject"] == f"run-acceptance:{state['run_id']}"
    invocation, = state["agent_invocation_history"]
    assert invocation["reported_thread_id"] == "run-reviewer-1"
    assert invocation["semantic_attempt"]["attempt_id"] == pending["attempt_id"]
    assert invocation["work_subject"] == pending["work_subject"]
    assert invocation["status"] == "completed"
    assert all(segment["started_at"] and segment["ended_at"]
               for segment in invocation["execution_segments"])
    assert invocation["execution_segments"]
    git = GitRepository(managed_repo(git_repo))
    assert pending["review_context"]["full_range"]["candidate_sha"] == git.resolve(state["run_branch"])
    assert state["ticket_jobs"]["3"]["phase"] == "completed"
    assert len(json.loads(fixture.read_text())["delivery"]["pull_requests"]) == 1
    root = managed_state(git_repo)
    before = snapshot_runs(root)
    projected = events(state)
    blocked, = [event for event in projected
                if event["kind"] == "stage_end" and event.get("attempt_id") == pending["attempt_id"]]
    assert blocked["round"] == 1
    assert blocked["color"] == "yellow"
    assert blocked["title"] in [card["header"]["title"]["content"] for card in delivered]
    assert read_notifications(root, state["run_id"])["seen"]
    assert not read_notifications(root, state["run_id"])["pending"]
    assert snapshot_runs(root) == before
