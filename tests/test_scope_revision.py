from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.external_supervision import ensure_supervision_window
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.graph import state_from_graph
from agent_run.revisions import effective_revision, ticket_graph_revision
from agent_run.run_currentness import refresh_run_currentness
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from support.cli_payloads import passing_acceptance, publication
from support.seeding import write_fixture


def _ticket(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "body": "Implement it.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


@pytest.mark.parametrize("blocked_reason", [
    "agent_requires_human", "reviewer_requires_human", "modification_budget_exhausted",
    "ci_development_budget_exhausted",
    "ticket_execution_failed", "ticket_paused",
])
def test_graph_refresh_preserves_known_ticket_pauses_for_currentness_check(blocked_reason: str) -> None:
    from dataclasses import replace

    from agent_run.models import DeliveryGraph, Issue, ParentIssue

    ticket = Issue(3, "Ticket", "Original requirements", "OPEN", frozenset({"ready-for-agent"}), ())
    graph = DeliveryGraph(ParentIssue(1, "Parent", "Parent requirements", (3,), True), {3: ticket})
    state = state_from_graph({"ticket_integration_holder": None, "ticket_integration_queue": []}, graph)
    job = {"ticket_number": 3, "phase": "blocked", "blocked_reason": blocked_reason,
           "effective_revision": effective_revision(
               ticket_revision=state["ticket_graph"]["tickets"]["3"]["content_revision"],
               parent_revision=state["parent"]["revision"], graph_revision=state["ticket_graph"]["revision"],
           )}
    state.update(ticket_jobs={"3": job}, active_ticket_job=job)
    changed = replace(graph, issues={3: replace(ticket, body="Changed requirements")})

    projected = state_from_graph(state, changed)

    # Graph refresh must reach Controller currentness validation, which owns
    # Requeue. A recognized old pause must not abort that refresh as unknown.
    assert projected["active_ticket_job"]["source_revision_changed"] is True
    assert projected["active_ticket_job"]["blocked_reason"] == blocked_reason
    assert projected["diagnostics"][0]["code"] == blocked_reason


@pytest.mark.parametrize(
    ("paused_ticket", "changed_ticket"),
    [(True, 2), (False, 2), (False, 3)],
    ids=["human-blocked-A", "waiting-CI-A", "queued-B"],
)
def test_noncurrent_ticket_revision_stops_run_before_independent_delivery(
    git_repo: Path, paused_ticket: bool, changed_ticket: int,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
        delivery={"required_checks": ["pending"]},
    )
    agent_fixture = git_repo / "agents.json"
    blocked_review = {
        "thread_id": "reviewer-2",
        "checks": {
            "e2e": {
                "status": "blocked",
                "evidence": "发生：需求待确认；尝试：核对 Issue；人必须：确认需求。",
                "findings": [],
            },
            "standards": passing_acceptance("unused", "pass")["checks"]["standards"],
            "spec": passing_acceptance("unused", "pass")["checks"]["spec"],
        },
    }
    agent_fixture.write_text(json.dumps({
        "developments": [
            {"thread_id": f"developer-{number}", "summary": f"Ticket {number}",
             "write_files": {f"ticket-{number}.txt": "done\n"}}
            for number in (2, 3)
        ],
        "reviews": [
            blocked_review if paused_ticket else passing_acceptance("reviewer-2", "passed"),
            passing_acceptance("reviewer-3", "passed"),
        ],
        "publications": [publication(), publication()],
    }), encoding="utf-8")
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(
        FixtureGitHubReader(fixture), git, states,
        delivery_policy=DeliveryPolicy(ticket_development_workers=1),
    )
    started, _ = controller.start(1)
    engine = DeliveryRunEngine(
        controller=controller,
        tickets=TicketDeliveryEngine(
            git=git, states=states, github=FixtureGitHubPublisher(fixture, git),
            agents=FixtureAgentBackend(agent_fixture),
        ),
    )
    waiting = engine.deliver(started["run_id"])
    assert waiting["ticket_jobs"]["2"]["phase"] == (
        "blocked" if paused_ticket else "waiting_checks"
    )
    assert waiting["ticket_jobs"]["3"]["phase"] == (
        "waiting_checks" if paused_ticket else "ready_for_integration"
    )
    data = json.loads(fixture.read_text(encoding="utf-8"))
    before_delivery = data["delivery"]
    data["issues"][str(changed_ticket)]["body"] += "\nChanged authoritative requirement."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = controller.resume(started["run_id"])
    assert refreshed["status"] == "requeue_required"
    assert refreshed["requeue_required"]["work_subject"] == f"ticket:{changed_ticket}"
    assert refreshed["active_ticket_job"]["ticket_number"] == changed_ticket
    require_current_run_state(refreshed)
    repeated, _ = controller.resume(started["run_id"])
    assert repeated["status"] == "requeue_required"
    assert repeated["active_ticket_job"]["ticket_number"] == changed_ticket
    require_current_run_state(repeated)
    stopped = engine.deliver(started["run_id"])
    assert stopped["status"] == "requeue_required"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == before_delivery
    if changed_ticket == 3:
        requeued, retired = controller.requeue(started["run_id"])
        assert retired["work_subject"] == "ticket:3"
        assert requeued["ticket_jobs"]["2"]["phase"] == "waiting_checks"
        assert requeued["ticket_integration_holder"] == 2


def test_refresh_drops_supervision_window_when_frontier_ticket_changes(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"151": _ticket(151), "152": _ticket(152)},
    )
    reader = FixtureGitHubReader(fixture)
    controller = Controller(
        reader,
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    old_job = {
        "ticket_number": 151,
        "phase": "waiting_checks",
        "pr_number": 166,
        "publication_sha": "old-head",
    }
    state["active_ticket_job"] = old_job
    state["ticket_jobs"]["151"] = old_job
    state["status"] = "waiting_checks"
    assert ensure_supervision_window(state, now=lambda: 10.0) is not None

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["151"]["state"] = "CLOSED"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed = controller._refresh(state, 1)  # noqa: SLF001 - refresh boundary

    assert refreshed["active_ticket_job"]["ticket_number"] == 152
    assert "supervision_window" not in refreshed


def test_run_currentness_drops_supervision_window_when_frontier_ticket_changes(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"151": _ticket(151), "152": _ticket(152)},
    )
    reader = FixtureGitHubReader(fixture)
    controller = Controller(
        reader,
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    old_job = {
        "ticket_number": 151,
        "phase": "waiting_checks",
        "pr_number": 166,
        "publication_sha": "old-head",
    }
    state["active_ticket_job"] = old_job
    state["ticket_jobs"]["151"] = old_job
    state["status"] = "waiting_checks"
    assert ensure_supervision_window(state, now=lambda: 10.0) is not None

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["151"]["state"] = "CLOSED"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refresh_run_currentness(state, reader=reader, git=GitRepository(git_repo))

    assert state["active_ticket_job"]["ticket_number"] == 152
    assert "supervision_window" not in state


def test_ticket_graph_revision_ignores_parent_execution_order(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
    )
    reader = FixtureGitHubReader(fixture)
    original = reader.delivery_graph(1)
    original_state = state_from_graph(
        {"ticket_integration_holder": None, "ticket_integration_queue": []}, original
    )

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"] = [3, 2]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    reordered = reader.delivery_graph(1)
    reordered_state = state_from_graph(original_state, reordered)

    assert ticket_graph_revision(reordered) == ticket_graph_revision(original)
    assert (
        reordered_state["ticket_graph"]["revision"]
        == original_state["ticket_graph"]["revision"]
    )
    assert reordered_state["ticket_graph"]["ordered_ticket_numbers"] == [3, 2]


def test_parent_revision_drift_is_recorded_without_scope_agent(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    started, _ = controller.start(1)
    accepted = started["accepted_parent_spec_revision"]

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] += "\nThis sentence changes the Parent revision."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    resumed, _ = controller.resume(str(started["run_id"]))

    assert resumed["status"] == "active"
    assert resumed["accepted_parent_spec_revision"] == accepted
    assert resumed["observed_parent_spec_revision"] == resumed["parent"]["revision"]
    assert resumed["observed_parent_spec_revision"] != accepted
    assert "pending_structure_change" not in resumed
    assert "latest_scope_impact_assessment" not in resumed


def test_parent_revision_is_recorded_when_ticket_graph_also_drifts(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    started, _ = controller.start(1)
    accepted_parent = started["accepted_parent_spec_revision"]

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] += "\nChange the Parent and ticket graph together."
    data["parent"]["sub_issues"].append(3)
    data["issues"]["3"] = _ticket(3)
    fixture.write_text(json.dumps(data), encoding="utf-8")
    expected_observed_parent = state_from_graph(
        started, FixtureGitHubReader(fixture).delivery_graph(1)
    )["parent"]["revision"]

    resumed, _ = controller.resume(str(started["run_id"]))

    assert resumed["status"] == "unsupported_scope_change"
    assert resumed["accepted_parent_spec_revision"] == accepted_parent
    assert resumed["observed_parent_spec_revision"] == expected_observed_parent
    assert expected_observed_parent != accepted_parent
