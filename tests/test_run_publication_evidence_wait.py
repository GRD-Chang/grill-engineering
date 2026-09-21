from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_publication_shared import RunPublicationShared
from agent_run.state import StateStore
from run_publication_test_support import RunPublicationAgents


class EvidencePublisher(FixtureGitHubPublisher):
    error: Exception | None = None

    def required_check_evidence(
        self, pr_number: int, *, expected_head_sha: str | None = None,
    ) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return {"pr_number": pr_number, "head_sha": expected_head_sha, "result": "fail"}


def _evidence_context(
    root: Path, *, approved: bool,
) -> tuple[RunPublicationShared, EvidencePublisher, dict[str, Any]]:
    # These tests enter the evidence-read seam; no Git operation or Agent work
    # precedes it. The retained CLI cases cover approval and repair execution.
    git = GitRepository(root)
    fixture = root / "github.json"
    fixture.write_text(json.dumps({"repository": "example/project", "delivery": {}}))
    publisher = EvidencePublisher(fixture, git)
    state: dict[str, Any] = {
        "run_id": "run-evidence",
        "repository": "example/project",
        "parent": {"number": 1},
        "run_branch": "agent-run/run-evidence",
        "base": {"sha": "a" * 40},
        "status": "run_approval_pending" if approved else "run_publication_pending",
        "run_acceptance": {"phase": "accepted", "repair_generation": 0},
        "run_publication": {
            "phase": "ready_for_approval" if approved else "waiting_checks",
            "pr_number": 2,
            "head_sha": "b" * 40,
        },
        "agent_invocation_history": [],
    }
    if approved:
        state["run_publication"]["approval_grant"] = {
            "repository": "example/project", "pr_number": 2,
            "head_sha": "b" * 40, "base_sha": "a" * 40,
            "granted_at": "2026-09-21T00:00:00+00:00",
        }
    states = StateStore(root / "state")
    states.save_run(state["run_id"], state)
    return RunPublicationShared(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha="a" * 40,
    ), publisher, state


@pytest.mark.parametrize("approved", [False, True], ids=["before-approval", "after-approval"])
@pytest.mark.parametrize("error_type", [OSError, TimeoutError], ids=["os", "timeout"])
def test_final_evidence_wait_preserves_durable_approval_and_repair_boundary(
    tmp_path: Path, approved: bool, error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CI exposed cancellation rounding when subtracting these float timestamps.
    now = 605.861132208
    monkeypatch.setattr("agent_run.external_supervision.monotonic", lambda: now)
    engine, publisher, state = _evidence_context(tmp_path, approved=approved)
    original = deepcopy(state)
    publisher.error = error_type("evidence transport unavailable")
    publication = state["run_publication"]

    assert engine._required_check_evidence(state, publication, 2, "b" * 40) is None
    persisted = engine.states.load_run(state["run_id"])
    assert persisted is not None
    assert persisted["status"] == persisted["terminal_kind"] == "waiting_external"
    assert persisted["run_publication"]["phase"] == "waiting_external"
    assert persisted["diagnostics"][0]["code"] == "github_check_evidence_observation_pending"
    window = deepcopy(persisted["supervision_window"])
    assert window["kind"] == "github_convergence"
    assert window["started_at"] == now
    assert window["deadline"] == now + 10 * 60
    assert persisted["run_acceptance"] == original["run_acceptance"]
    assert "repair_job" not in persisted["run_acceptance"]
    assert persisted["agent_invocation_history"] == original["agent_invocation_history"]
    assert persisted["run_publication"] == {
        **original["run_publication"], "phase": "waiting_external",
    }

    # A fresh read of saved state retries the same boundary without resetting
    # the window, consuming a repair generation, or losing an approval grant.
    now += 30.25
    assert engine._required_check_evidence(
        persisted, persisted["run_publication"], 2, "b" * 40,
    ) is None
    assert engine.states.load_run(state["run_id"]) == persisted
    assert persisted["supervision_window"] == window

    before_recovery = deepcopy(persisted)
    publisher.error = None
    evidence = engine._required_check_evidence(
        persisted, persisted["run_publication"], 2, "b" * 40,
    )
    assert evidence == {"pr_number": 2, "head_sha": "b" * 40, "result": "fail"}
    assert persisted == before_recovery
    assert engine.states.load_run(state["run_id"]) == before_recovery


def test_final_evidence_contradiction_does_not_become_a_convergence_wait(tmp_path: Path) -> None:
    engine, publisher, state = _evidence_context(tmp_path, approved=True)
    before = deepcopy(state)
    publisher.error = GitHubReadError("invalid_fixture", "contradictory evidence")

    with pytest.raises(GitHubReadError, match="contradictory evidence"):
        engine._required_check_evidence(state, state["run_publication"], 2, "b" * 40)

    assert state == before
    assert engine.states.load_run(state["run_id"]) == before
