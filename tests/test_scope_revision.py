from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_run.controller import Controller
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.graph import state_from_graph
from agent_run.revisions import ticket_graph_revision
from agent_run.state import StateStore
from conftest import write_fixture


def _ticket(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "body": "Implement it.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def test_ticket_graph_revision_ignores_parent_execution_order(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
    )
    reader = FixtureGitHubReader(fixture)
    original = reader.delivery_graph(1)
    original_state = state_from_graph({}, original)

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
