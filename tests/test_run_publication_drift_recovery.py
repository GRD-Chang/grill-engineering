"""默认分支追加不能绕过未知 Final Run PR 文案写入的只读对账。"""
from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import ReviewResult
from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from support.run_publication import RunPublicationAgents
from support.run_scenarios import PassingRunReviewer, _accepted_run


class CountingReviewer(PassingRunReviewer):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.requests.append(request)
        return ReviewResult("fresh-run-reviewer", super().review(request).artifact)


class InterruptedUpdatePublisher(FixtureGitHubPublisher):
    def __init__(self, *args: Any, fault: str) -> None:
        super().__init__(*args)
        self.fault = fault
        self.update_calls = 0

    def refresh_run_pr_narrative(self, **authority: Any) -> None:
        self.update_calls += 1
        super().refresh_run_pr_narrative(**authority)
        if self.fault == "response" and self.update_calls == 1:
            raise OSError("Final Run update response lost")


@pytest.mark.parametrize("fault", ["response", "result_save"])
@pytest.mark.parametrize("with_currentness_reader", [False, True], ids=["direct-engine", "live-currentness"])
def test_default_append_preserves_unknown_final_update_until_read_only_settlement(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, fault: str, with_currentness_reader: bool,
) -> None:
    # Pure Publisher tests cannot see Acceptance/currentness clearing a pending
    # write before integration; retain real Git and StateStore at this boundary.
    state, states, git, initial_publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    fixture = git_repo / "github.json"
    agents = RunPublicationAgents()
    reviewer = CountingReviewer()
    initial = RunPublicationEngine(
        git=git, states=states, agents=agents, github=initial_publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(run_id)
    assert initial["status"] == "run_approval_pending"
    pr_number = initial["run_publication"]["pr_number"]
    original_head = initial["run_publication"]["record"]["run_head_sha"]
    # An existing PR with old text forces the actual update operation while
    # retaining the already prepared publication artifact and exact PR.
    initial["run_publication"].pop("record")
    initial["run_publication"]["phase"] = "waiting_external"
    initial.update(status="waiting_external", terminal_kind="waiting_external")
    prior = {"title": "Previous title", "body": "Previous body"}
    initial_publisher.data["delivery"]["pull_requests"][0].update(prior)
    initial_publisher._save()
    states.save_run(run_id, initial)
    publisher = InterruptedUpdatePublisher(fixture, git, fault=fault)
    original_save = states.save_run
    injected = False

    def save(identifier: str, current: dict[str, Any]) -> None:
        nonlocal injected
        durable = states.load_current_run(identifier)
        if (
            fault == "result_save" and not injected and publisher.update_calls == 1
            and isinstance(durable["run_publication"].get("write_intent"), dict)
            and "write_intent" not in current["run_publication"]
        ):
            injected = True
            raise OSError("Final Run update result save lost")
        original_save(identifier, current)

    monkeypatch.setattr(states, "save_run", save)
    waiting = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(run_id)
    assert injected == (fault == "result_save")
    assert publisher.update_calls == 1
    assert waiting["status"] == "waiting_external"
    before = deepcopy(states.load_current_run(run_id)["run_publication"])
    intent = before["write_intent"]
    target = publisher.read_run_pr_narrative(pr_number)
    assert intent["action"] == "refresh_final_pr_narrative"
    assert intent["prior_narrative"] == prior
    assert target != prior

    (git_repo / "maintainer.txt").write_text("default append\n", encoding="utf-8")
    subprocess.run(["git", "add", "maintainer.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "append default"], cwd=git_repo,
        text=True, capture_output=True, check=True,
    )
    default_head = git.resolve("main")
    assert git.is_ancestor(intent["authority"]["base_sha"], default_head)
    external = FixtureGitHubPublisher(fixture, git)
    external.data["default_head_sha"] = default_head
    external.data["delivery"]["published_branches"]["main"] = default_head
    third = {"title": "Maintainer title", "body": "Maintainer body"}
    external.data["delivery"]["pull_requests"][0].update(third, base_sha=default_head)
    external._save()

    def accept() -> dict[str, Any]:
        return RunAcceptanceEngine(
            git=git, states=StateStore(states.root), agents=reviewer,
            github=FixtureGitHubPublisher(fixture, git), default_head_sha=default_head,
            currentness_reader=FixtureGitHubReader(fixture) if with_currentness_reader else None,
        ).accept(run_id)

    def publish() -> dict[str, Any]:
        return RunPublicationEngine(
            git=git, states=StateStore(states.root), agents=agents,
            github=FixtureGitHubPublisher(fixture, git), default_branch="main",
            default_head_sha=default_head,
            currentness_reader=FixtureGitHubReader(fixture) if with_currentness_reader else None,
        ).publish(run_id)

    controller = Controller(FixtureGitHubReader(fixture), git, StateStore(states.root))
    for attempt in range(2):
        if attempt:
            controller.resume(run_id, explicit_resume=True)
        for operation in (accept, publish):
            paused = operation()
            publication = paused["run_publication"]
            assert paused["status"] == "ready_for_human"
            assert publication["phase"] == "ready_for_human"
            assert publication["pause_resume_phase"] == "waiting_external"
            assert publication["blocked_reason"] == "github_write_outcome_unknown"
            assert publication["write_intent"] == intent
            assert publication["write_intent"]["pr_number"] == pr_number
            assert publication["write_intent"]["target_narrative"] == target
            assert publication["artifact"] == before["artifact"]
            assert publication["pr_number"] == pr_number
            durable = StateStore(states.root).load_current_run(run_id)
            assert durable["run_publication"]["write_intent"] == intent
            assert durable["run_publication"]["artifact"] == before["artifact"]
            assert git.resolve(str(paused["run_branch"])) == original_head
            assert len(agents.requests) == 1
            assert not reviewer.requests
            assert "approval_grant" not in publication
            observed = FixtureGitHubPublisher(fixture, git)
            assert len(observed.data["delivery"]["pull_requests"]) == 1
            assert observed.read_run_pr_narrative(pr_number) == third

    # Target text proves completion; old text proves non-application. Either
    # allows read-only settlement followed by fresh integration and evidence.
    observed.data["delivery"]["pull_requests"][0].update(
        target if fault == "response" else prior,
    )
    observed.data["delivery"].update(required_checks=["pending", "pass"], check_position=0)
    observed._save()
    controller.resume(run_id, explicit_resume=True)
    accepted = accept()
    assert accepted["status"] == "run_publication_pending"
    assert accepted["run_acceptance"]["phase"] == "accepted"
    assert len(reviewer.requests) == 1
    assert len(agents.requests) == 1
    new_head = git.resolve(str(accepted["run_branch"]))
    assert new_head != original_head
    assert git.is_ancestor(default_head, new_head)
    record = accepted["run_acceptance"]["acceptance_record"]
    assert record["reviewed_head_sha"] == new_head
    assert record["reviewed_default_base_sha"] == default_head

    checking = publish()
    assert checking["status"] == "waiting_checks"
    assert "write_intent" not in checking["run_publication"]
    assert checking["run_publication"]["pr_number"] == pr_number
    assert checking["run_publication"]["required_checks_evidence"]["head_sha"] == new_head
    assert checking["run_publication"]["required_checks_evidence"]["result"] == "pending"
    assert "approval_grant" not in checking["run_publication"]
    assert len(agents.requests) == 2
    approved_gate = publish()
    assert approved_gate["status"] == "run_approval_pending"
    publication = approved_gate["run_publication"]
    assert publication["record"]["run_head_sha"] == new_head
    assert publication["record"]["default_head_sha"] == default_head
    assert publication["required_checks_evidence"]["head_sha"] == new_head
    assert publication["required_checks_evidence"]["pr_number"] == pr_number
    assert publication["required_checks_evidence"]["result"] == "pass"
    assert "approval_grant" not in publication
    assert len(agents.requests) == 2
    assert len(reviewer.requests) == 1
    final_remote = FixtureGitHubPublisher(fixture, git)
    assert len(final_remote.data["delivery"]["pull_requests"]) == 1
    assert final_remote.live_pull_request(pr_number)["state"] == "OPEN"
    assert final_remote.live_pull_request(pr_number)["head_sha"] == new_head
