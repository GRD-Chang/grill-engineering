from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.delivery_step import DeliveryStepKind
from agent_run.run_publication import RunPublicationEngine
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run


def test_final_publication_steps_supervise_without_agent_and_preserve_approval(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["pending", "pending", "pass"]
    publisher._save()
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    run_id = str(state["run_id"])

    for _ in range(2):
        assert engine.advance(run_id).kind == DeliveryStepKind.NEEDS_AGENT
    waiting_for_slot = states.load_current_run(run_id)
    assert waiting_for_slot is not None
    assert "publication_attempts" not in waiting_for_slot["run_publication"]
    assert "pending_semantic_attempt" not in waiting_for_slot["run_publication"]
    assert not agents.requests

    assert engine.advance(run_id, allow_agent=True).kind == DeliveryStepKind.CONTINUE
    prepared = states.load_current_run(run_id)
    assert prepared is not None
    assert prepared["run_publication"]["artifact"]
    assert prepared["run_publication"]["semantic_attempt_history"][-1]["outcome"] == "publication_artifact"
    assert publisher.data["delivery"].get("pull_requests", []) == []
    for _ in range(2):
        assert engine.advance(run_id).kind == DeliveryStepKind.WAITING
    ready = engine.advance(run_id)
    assert ready.kind == DeliveryStepKind.PAUSED
    assert ready.reason == "awaiting_approval"
    persisted = states.load_current_run(run_id)
    assert persisted is not None
    publication = persisted["run_publication"]
    assert publication["publication_attempts"] == 1
    assert len(agents.requests) == 1
    assert len(publisher.data["delivery"]["pull_requests"]) == 1
    assert publisher.live_pull_request(publication["pr_number"])["state"] == "OPEN"

    completed = engine.approve(run_id)
    assert completed["status"] == "completed"
    assert engine.advance(run_id).kind == DeliveryStepKind.COMPLETED
    assert 1 in publisher.data["delivery"]["closed_issues"]
    assert len(agents.requests) == 1


def test_final_publication_artifact_and_attempt_survive_same_persistence_boundary(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    run_id = str(state["run_id"])
    after_saved = states._after_run_saved

    def interrupt_after_artifact() -> None:
        value = states.load_current_run(run_id)
        assert value is not None
        publication = value.get("run_publication", {})
        history = publication.get("semantic_attempt_history", [])
        if history and history[-1].get("outcome") == "publication_artifact":
            raise KeyboardInterrupt("controller disappeared after attempt completion")

    monkeypatch.setattr(states, "_after_run_saved", interrupt_after_artifact)
    with pytest.raises(KeyboardInterrupt):
        engine.advance(run_id, allow_agent=True)
    monkeypatch.setattr(states, "_after_run_saved", after_saved)
    persisted = states.load_current_run(run_id)
    assert persisted is not None
    assert persisted["run_publication"]["artifact"]
    assert "pending_semantic_attempt" not in persisted["run_publication"]
    resumed = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    assert resumed.advance(run_id).reason == "awaiting_approval"
    assert len(agents.requests) == 1
    assert len(publisher.data["delivery"]["pull_requests"]) == 1


def test_final_publication_head_drift_rejects_saved_artifact_without_agent(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    agents = RunPublicationAgents()
    engine = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    run_id = str(state["run_id"])
    assert engine.advance(run_id, allow_agent=True).kind == DeliveryStepKind.CONTINUE
    branch = str(state["run_branch"])
    drifted = subprocess.run(
        ["git", "commit-tree", git.resolve(f"{branch}^{{tree}}"), "-p", branch,
         "-m", "advance after final publication content was saved"],
        cwd=git_repo, text=True, capture_output=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "update-ref", f"refs/heads/{branch}", drifted],
                   cwd=git_repo, check=True)

    step = engine.advance(run_id)
    assert step.kind == DeliveryStepKind.NEEDS_AGENT
    assert step.reason == "run_acceptance"
    persisted = states.load_current_run(run_id)
    assert persisted is not None
    assert persisted["status"] == "run_acceptance_pending"
    assert persisted["run_publication"]["phase"] == "stale"
    assert "acceptance_record" not in persisted["run_acceptance"]
    assert len(agents.requests) == 1
    assert publisher.data["delivery"].get("pull_requests", []) == []
