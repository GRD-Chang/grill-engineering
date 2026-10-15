"""Requeue takes precedence over a stale pause through the public lifecycle."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.notification_cards import card
from agent_run.notification_events import events
from agent_run.notifications import Notifications, read_notifications
from agent_run.state import StateStore
from support.cli import issue, load_only_run_state, run_cli, run_internal_stage, stdout_json
from support.cli_payloads import HUMAN_BLOCKER, human_blocker_step, passing_acceptance, publication
from support.inprocess_cli import invoke_cli_inprocess
from support.seeding import seed_run, write_fixture
from support.workspace import managed_state

from support.state import snapshot_run


@pytest.mark.parametrize("parent_only", [True, False], ids=["parent-only", "ticket"])
def test_changed_requirements_replace_the_old_human_action_with_requeue(git_repo: Path, parent_only: bool) -> None:
    # Two real flows exercise the distinct Parent and Ticket currentness paths.
    # Other pause kinds and independent siblings use lightweight fact tests.
    fixture = write_fixture(git_repo / "github.json", issues={} if parent_only else {"3": issue(3)})
    agents = git_repo / "agents.json"
    agents.write_text(json.dumps({"developments": [human_blocker_step("old-generation")]}), encoding="utf-8")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert initial.returncode == 2, initial.stdout + initial.stderr
    blocked = load_only_run_state(git_repo)
    run_id = blocked["run_id"]
    subject_id = f"parent-only:{run_id}" if parent_only else "ticket:3"
    generation_key = "parent_generation" if parent_only else "ticket_branch_generation"

    def job(state: dict[str, Any]) -> dict[str, Any]:
        return state["parent_job"] if parent_only else state["ticket_jobs"]["3"]

    def boundary(state: dict[str, Any]) -> dict[str, Any]:
        result, = [event for event in events(state) if event.get("current") and event.get("work_subject") == subject_id]
        return result

    old_job = deepcopy(job(blocked))
    assert old_job["human_blockers"] == [HUMAN_BLOCKER]
    assert old_job[generation_key] == 1
    old_boundary = boundary(blocked)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    changed = data["parent"] if parent_only else data["issues"]["3"]
    changed["body"] = "Changed requirements require a fresh Generation."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    stale_result = run_cli(git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents))

    assert stale_result.returncode == 2, stale_result.stdout + stale_result.stderr
    assert stdout_json(stale_result)["status"] == "requeue_required", stale_result.stdout + stale_result.stderr
    stale = load_only_run_state(git_repo)
    assert job(stale)["human_blockers"] == old_job["human_blockers"]
    assert job(stale)[generation_key] == 1
    assert stale["agent_invocation_history"] == blocked["agent_invocation_history"]
    reason = "parent_requirements_changed" if parent_only else "ticket_requirements_changed"
    assert stale["requeue_required"]["reason"] == reason
    run_ref = (managed_state(git_repo), str(run_id))
    before_queries = snapshot_run(run_ref)
    for command in ("status", "history"):
        result = invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        view = stdout_json(result)
        action, = view["operator_actions"]
        assert view["operator_action"] == action
        assert action["work_subject"] == subject_id
        assert action["generation"] == 1
        assert action["action_kind"] == "requeue_required"
        assert action["reason"] == reason
        assert action["scope"] == "global"
        assert action["reasons"] == [stale["diagnostics"][0]["message"]]
        assert action["evidence"]["diagnostics"] == stale["diagnostics"]
        assert action["action"] == {"kind": "command", "command": "requeue", "selector": run_id, "ticket_number": None}
        assert action["next_action"] == view["next_action"] == f"agent-run requeue {run_id}"
        human = invoke_cli_inprocess(git_repo, fixture, command, run_id).stdout
        assert "agent-run requeue 1 --repo example/project" in human
    assert snapshot_run(run_ref) == before_queries
    requeue_event = boundary(stale)
    assert requeue_event["id"] != old_boundary["id"]
    for key in ("work_subject", "generation", "scope", "reason", "evidence", "action"):
        assert requeue_event[key] == action[key]
    rendered = json.dumps(card(requeue_event), ensure_ascii=False)
    assert "agent-run requeue 1 --repo example/project" in rendered
    assert "agent-run resume" not in rendered
    assert HUMAN_BLOCKER not in rendered

    # Execute the advertised command: a fresh Agent starts in Generation 2,
    # rather than repeatedly trying to resume the stale Human Blocker.
    next_blocker = "New generation requires a separate decision."
    agents.write_text(json.dumps({"developments": [{
        **human_blocker_step("new-generation"), "human_blockers": [next_blocker],
    }]}), encoding="utf-8")
    requeued = run_cli(git_repo, fixture, "requeue", run_id, "--agent-fixture", str(agents))
    assert requeued.returncode == 2, requeued.stdout + requeued.stderr
    after = load_only_run_state(git_repo)
    assert after["status"] != "requeue_required"
    assert job(after)[generation_key] == 2
    assert job(after)["development_thread_id"] == "new-generation"
    assert job(after)["human_blockers"] == [next_blocker]
    retired, = after["retired_job_generations"]
    assert retired["work_subject"] == subject_id
    assert retired["generation"] == 1
    assert "old-generation" in retired["thread_ids"]
    next_event = boundary(after)
    assert next_event["id"] != requeue_event["id"]
    assert next_event["generation"] == 2
    assert next_event["action"]["command"] == "resume"
    assert next_event["summary"] == next_blocker


def test_requeue_preserves_an_independent_reviewer_blocker(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep a real multi-job save/read/CLI recovery: pure action projection does
    # not catch a Run that can be saved but fails the next state-contract read.
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2), "3": issue(3)},
                            delivery={"required_checks": ["pending"]})
    blocked_review = passing_acceptance("reviewer-2", "unused")
    blocked_review["checks"]["e2e"] = {
        "status": "blocked", "evidence": HUMAN_BLOCKER, "findings": [],
    }
    agents = git_repo / "agents.json"
    agents.write_text(json.dumps({
        "developments": [{"thread_id": f"developer-{number}", "summary": f"Ticket {number}",
                          "write_files": {f"ticket-{number}.txt": "done\n"}} for number in (2, 3)],
        "reviews": [blocked_review, passing_acceptance("reviewer-3", "passed")],
        "publications": [publication()],
    }), encoding="utf-8")
    run_id = stdout_json(seed_run(git_repo, fixture, "1", "--ticket-development-workers", "1",
                                 idle_control=True))["run_id"]
    run_internal_stage(git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents))
    states = StateStore(managed_state(git_repo))
    waiting = states.load_current_run(run_id)
    assert waiting is not None
    sibling = deepcopy(waiting["ticket_jobs"]["2"])
    assert sibling["blocked_reason"] == "reviewer_requires_human"
    assert sibling["human_blockers"] == [HUMAN_BLOCKER]
    assert sibling["pending_semantic_attempt"]["role"] == "reviewer"
    assert waiting["ticket_jobs"]["3"]["phase"] == "waiting_checks"
    invocations_before = deepcopy(waiting["agent_invocation_history"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["3"]["body"] += "\nChanged authoritative requirement."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents))

    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    assert stdout_json(resumed)["status"] == "requeue_required", resumed.stdout + resumed.stderr
    stale = states.load_current_run(run_id)
    assert stale is not None
    assert stale["ticket_jobs"]["2"] == sibling
    assert stale["agent_invocation_history"] == invocations_before
    run_ref = (managed_state(git_repo), str(run_id))
    before_queries = snapshot_run(run_ref)
    for command in ("status", "history"):
        result = invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json")
        assert result.returncode == 0, result.stdout + result.stderr
        view = stdout_json(result)
        requeue, blocker = view["operator_actions"]
        assert view["operator_action"] == requeue
        assert (requeue["work_subject"], requeue["generation"], requeue["scope"]) == ("ticket:3", 1, "global")
        assert requeue["reason"] == "ticket_requirements_changed"
        assert requeue["evidence"]["diagnostics"] == stale["diagnostics"]
        assert requeue["action"] == {"kind": "command", "command": "requeue", "selector": run_id, "ticket_number": None}
        assert (blocker["work_subject"], blocker["generation"], blocker["scope"]) == ("ticket:2", 1, "local")
        assert blocker["reason"] == "reviewer_requires_human"
        assert blocker["reasons"] == [HUMAN_BLOCKER]
        assert blocker["action"]["ticket_number"] == 2
        human = invoke_cli_inprocess(git_repo, fixture, command, run_id)
        assert human.returncode == 0, human.stdout + human.stderr
        assert "agent-run requeue 1 --repo example/project" in human.stdout
        assert HUMAN_BLOCKER in human.stdout
    assert snapshot_run(run_ref) == before_queries

    boundaries = {event["work_subject"]: event for event in events(stale) if event.get("current")}
    assert set(boundaries) == {"ticket:2", "ticket:3"}
    for action in (requeue, blocker):
        for key in ("generation", "scope", "reason", "evidence", "action"):
            assert boundaries[action["work_subject"]][key] == action[key]
    sent: list[dict[str, Any]] = []

    def send(_config: Any, rendered: dict[str, Any], _identity: str, _cancel: Any, **kwargs: Any) -> dict[str, Any]:
        sent.append(rendered)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    notification_state = {**stale, "notifications": {"enabled": True, "mode": "concise"}}
    for _ in range(2):
        sender = Notifications(managed_state(git_repo), notification_state)
        sender.close()
        assert sender.thread is not None and not sender.thread.is_alive()
    for event in boundaries.values():
        assert sent.count(card(event)) == 1
    assert read_notifications(managed_state(git_repo), run_id)["pending"] == []
    assert snapshot_run(run_ref) == before_queries

    replacement = git_repo / "replacement-agents.json"
    replacement.write_text(json.dumps({"developments": [human_blocker_step("developer-3-new")]}), encoding="utf-8")
    requeued = run_cli(git_repo, fixture, "requeue", run_id, "--agent-fixture", str(replacement))

    assert requeued.returncode == 2, requeued.stdout + requeued.stderr
    assert stdout_json(requeued)["status"] == "ready_for_human", requeued.stdout + requeued.stderr
    after = states.load_current_run(run_id)
    assert after is not None
    # Whole-job equality also protects the sibling's Candidate, Thread, pending
    # Attempt, review budget and CI recovery budget from replacement or charging.
    assert after["ticket_jobs"]["2"] == sibling
    assert after["ticket_jobs"]["3"]["ticket_branch_generation"] == 2
    assert after["ticket_jobs"]["3"]["development_thread_id"] == "developer-3-new"
    retired, = after["retired_job_generations"]
    assert (retired["work_subject"], retired["generation"]) == ("ticket:3", 1)
    assert after["agent_invocation_history"][:len(invocations_before)] == invocations_before
    assert all(invocation["work_subject"] == "ticket:3"
               for invocation in after["agent_invocation_history"][len(invocations_before):])
