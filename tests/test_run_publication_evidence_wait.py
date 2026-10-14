from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_publication_shared import RunPublicationShared
from support.final_publication import final_publication_context
from support.run_publication import RunPublicationAgents


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
    # 真正的 Run/PR 与批准身份先持久化；测试只进入当前证据读取边界。
    states, git, state = final_publication_context(root, approved=approved)
    publisher = EvidencePublisher(root / "github.json", git)
    engine = RunPublicationShared(
        git=git, states=states, agents=RunPublicationAgents(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    loaded = engine.states.load_run(state["run_id"])
    assert loaded is not None
    return engine, publisher, loaded


@pytest.mark.parametrize("approved", [False, True], ids=["before-approval", "after-approval"])
@pytest.mark.parametrize("error_type", [OSError, TimeoutError], ids=["os", "timeout"])
def test_final_evidence_wait_preserves_durable_approval_and_repair_boundary(
    git_repo: Path, approved: bool, error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CI exposed cancellation rounding when subtracting these float timestamps.
    now = 605.861132208
    monkeypatch.setattr("agent_run.external_supervision.monotonic", lambda: now)
    engine, publisher, state = _evidence_context(git_repo, approved=approved)
    original = deepcopy(state)
    publisher.error = error_type("evidence transport unavailable")
    publication = state["run_publication"]
    number = int(publication["pr_number"])
    head = str(publication["record"]["run_head_sha"])

    assert engine._required_check_evidence(state, publication, number, head) is None
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
        persisted, persisted["run_publication"], number, head,
    ) is None
    assert engine.states.load_run(state["run_id"]) == persisted
    assert persisted["supervision_window"] == window

    before_recovery = deepcopy(persisted)
    publisher.error = None
    evidence = engine._required_check_evidence(
        persisted, persisted["run_publication"], number, head,
    )
    assert evidence == {"pr_number": number, "head_sha": head, "result": "fail"}
    assert persisted == before_recovery
    assert engine.states.load_run(state["run_id"]) == before_recovery


def test_final_evidence_contradiction_does_not_become_a_convergence_wait(git_repo: Path) -> None:
    engine, publisher, state = _evidence_context(git_repo, approved=True)
    before = deepcopy(state)
    number = int(state["run_publication"]["pr_number"])
    head = str(state["run_publication"]["record"]["run_head_sha"])
    publisher.error = GitHubReadError("invalid_fixture", "contradictory evidence")

    with pytest.raises(GitHubReadError, match="contradictory evidence"):
        engine._required_check_evidence(state, state["run_publication"], number, head)

    assert state == before
    assert engine.states.load_run(state["run_id"]) == before
