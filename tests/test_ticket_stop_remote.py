"""Ticket Stop preserves unknown remote writes until their outcome is observed."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.github import MergeOutcomeUnknownError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.state import SimulatedProcessCrash
from agent_run.ticket_pause import request_ticket_pause
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("lost_action", ["publish", "ensure_pr"])
def test_stop_reconciles_lost_publish_response_before_releasing_seat(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, lost_action: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = str(state["run_id"])
    agents = TicketAgents(git_repo, (2,), lambda _number: None)
    engine = ticket_engine(git, states, fixture, agents)
    assert engine.develop_frontier(run_id, state)
    publish = FixtureGitHubPublisher.publish_branch
    published: list[str] = []

    def lose_response(
        publisher: FixtureGitHubPublisher, branch: str, head_sha: str,
        *, expected_remote_sha: str,
    ) -> Any:
        published.append(head_sha)
        result = publish(publisher, branch, head_sha, expected_remote_sha=expected_remote_sha)
        if lost_action != "publish":
            return result
        stopped = request_ticket_pause(states, run_id, 2)
        assert stopped["ticket_jobs"]["2"]["ticket_write_intent"]["action"] == "publish_ticket_ref"
        assert stopped["ticket_integration_holder"] == 2
        raise SimulatedProcessCrash("Publisher exited after remote publication")

    ensure_pr = FixtureGitHubPublisher.ensure_ticket_pr
    pr_calls: list[int] = []

    def lose_pr_response(publisher: FixtureGitHubPublisher, **kwargs: Any) -> int:
        number = ensure_pr(publisher, **kwargs)
        pr_calls.append(number)
        if lost_action == "ensure_pr" and len(pr_calls) == 1:
            stopped = request_ticket_pause(states, run_id, 2)
            assert stopped["ticket_jobs"]["2"]["ticket_write_intent"]["action"] == "ensure_ticket_pr"
            assert stopped["ticket_integration_holder"] == 2
            raise SimulatedProcessCrash("Publisher exited after PR creation")
        return number

    monkeypatch.setattr(FixtureGitHubPublisher, "publish_branch", lose_response)
    monkeypatch.setattr(FixtureGitHubPublisher, "ensure_ticket_pr", lose_pr_response)
    with pytest.raises(SimulatedProcessCrash):
        engine.deliver_ticket(run_id, 2)

    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    job = interrupted["ticket_jobs"]["2"]
    assert interrupted["ticket_integration_holder"] == 2
    assert "2" in interrupted["ticket_pause_requests"]
    head = job["publication_sha"]
    remote = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert remote["published_branches"][job["ticket_branch"]] == head
    assert len(remote.get("pull_requests", [])) == (1 if lost_action == "ensure_pr" else 0)

    # A fresh Engine observes the exact published ref before consuming Stop.
    ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, 2)

    paused = states.load_current_run(run_id)
    assert paused is not None
    job = paused["ticket_jobs"]["2"]
    assert job["blocked_reason"] == "ticket_paused"
    assert job["pause_resume_phase"] == "publishing"
    assert job["published_sha"] == head
    assert "ticket_write_intent" not in job
    assert paused["ticket_integration_holder"] is None
    assert "2" not in paused["ticket_pause_requests"]
    assert published == [head]
    if lost_action == "ensure_pr":
        assert pr_calls == [1]
        assert job["pr_number"] == 1
    remote = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(remote.get("pull_requests", [])) == (1 if lost_action == "ensure_pr" else 0)
    assert remote.get("closed_issues", []) == []


def test_stop_unknown_merge_waits_without_resending_then_finishes_observed_merge(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = str(state["run_id"])
    agents = TicketAgents(git_repo, (2,), lambda _number: None)
    engine = ticket_engine(git, states, fixture, agents)
    assert engine.develop_frontier(run_id, state)
    merge = FixtureGitHubPublisher.squash_merge
    live_pull = FixtureGitHubPublisher.live_pull_request
    merge_calls: list[dict[str, Any]] = []
    reads: list[int] = []

    def unknown_merge(publisher: FixtureGitHubPublisher, **kwargs: Any) -> str:
        merge_calls.append(kwargs)
        stopped = request_ticket_pause(states, run_id, 2)
        assert stopped["ticket_integration_holder"] == 2
        assert stopped["ticket_jobs"]["2"]["merge_intent"]["attempts"] == 1
        raise MergeOutcomeUnknownError("Merge sent; remote outcome not visible yet")

    def observe(publisher: FixtureGitHubPublisher, pr_number: int) -> Any:
        reads.append(pr_number)
        return live_pull(publisher, pr_number)

    monkeypatch.setattr(FixtureGitHubPublisher, "squash_merge", unknown_merge)
    monkeypatch.setattr(FixtureGitHubPublisher, "live_pull_request", observe)
    engine.deliver_ticket(run_id, 2)
    waiting = states.load_current_run(run_id)
    assert waiting is not None
    before = deepcopy(waiting["ticket_jobs"]["2"])
    read_count = len(reads)

    for _ in range(2):
        ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, 2)
        waiting = states.load_current_run(run_id)
        assert waiting is not None
        job = waiting["ticket_jobs"]["2"]
        assert waiting["ticket_integration_holder"] == 2
        assert "2" in waiting["ticket_pause_requests"]
        assert job["merge_intent"] == before["merge_intent"]
        assert job["candidate_sha"] == before["candidate_sha"]
        assert job["review_budget"] == before["review_budget"]
    assert len(reads) > read_count
    assert len(merge_calls) == 1
    remote = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert remote["pull_requests"][0]["state"] == "OPEN"
    assert remote.get("closed_issues", []) == []

    # Model the already sent remote request becoming visible on GitHub.
    integrated = merge(FixtureGitHubPublisher(fixture, git), **merge_calls[0])
    ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, 2)

    completed = states.load_current_run(run_id)
    assert completed is not None
    job = completed["ticket_jobs"]["2"]
    assert job["phase"] == "completed"
    assert job["integrated_sha"] == integrated
    assert git.resolve(state["run_branch"]) == integrated
    assert completed["ticket_integration_holder"] is None
    assert "2" not in completed["ticket_pause_requests"]
    assert len(merge_calls) == 1
    remote = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert remote["closed_issues"] == [2]
    assert [entry["action"] for entry in remote["mutations"]].count("close_issue") == 1


def test_stop_waiting_checks_prevents_merge_and_preserves_candidate_budget(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = str(state["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = TicketAgents(git_repo, (2,), lambda _number: None)
    engine = ticket_engine(git, states, fixture, agents)
    assert engine.develop_frontier(run_id, state)
    engine.deliver_ticket(run_id, 2)
    waiting = states.load_current_run(run_id)
    assert waiting is not None
    before = deepcopy(waiting["ticket_jobs"]["2"])
    assert before["phase"] == "waiting_checks"
    assert waiting["ticket_integration_holder"] == 2

    def unexpected_merge(publisher: FixtureGitHubPublisher, **kwargs: Any) -> str:
        pytest.fail("Stopped Ticket must not send a merge")

    monkeypatch.setattr(FixtureGitHubPublisher, "squash_merge", unexpected_merge)
    request_ticket_pause(states, run_id, 2)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["required_checks"] = ["pass"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    restarted = ticket_engine(git, states, fixture, agents)
    restarted.supervise(run_id, frozenset())
    restarted.deliver_ticket(run_id, 2)

    paused = states.load_current_run(run_id)
    assert paused is not None
    job = paused["ticket_jobs"]["2"]
    assert job["blocked_reason"] == "ticket_paused"
    assert job["pause_resume_phase"] == "waiting_checks"
    assert paused["ticket_integration_holder"] is None
    for key in ("candidate_sha", "publication_sha", "pr_number", "review_budget"):
        assert job[key] == before[key]
    remote = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(remote["pull_requests"]) == 1
    assert remote["pull_requests"][0]["state"] == "OPEN"
    assert remote.get("closed_issues", []) == []


def test_stop_during_premerge_status_write_pauses_unsent_merge_and_resumes_checks(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = str(state["run_id"])
    agents = TicketAgents(git_repo, (2,), lambda _number: None)
    engine = ticket_engine(git, states, fixture, agents)
    assert engine.develop_frontier(run_id, state)
    record_status = FixtureGitHubPublisher.record_agent_run_status
    stopped_jobs: list[dict[str, Any]] = []

    def stop_after_status(
        publisher: FixtureGitHubPublisher, pr_number: int, status: dict[str, Any],
    ) -> None:
        record_status(publisher, pr_number, status)
        current = states.load_current_run(run_id)
        assert current is not None
        job = current["ticket_jobs"]["2"]
        if job["phase"] == "merging":
            assert job["merge_intent"]["attempts"] == 0
            assert current["ticket_integration_holder"] == 2
            stopped_jobs.append(deepcopy(job))
            request_ticket_pause(states, run_id, 2)

    def unexpected_merge(publisher: FixtureGitHubPublisher, **kwargs: Any) -> str:
        pytest.fail("Stop received before dispatch must prevent the merge request")

    monkeypatch.setattr(FixtureGitHubPublisher, "record_agent_run_status", stop_after_status)
    monkeypatch.setattr(FixtureGitHubPublisher, "squash_merge", unexpected_merge)
    engine.deliver_ticket(run_id, 2)

    paused = states.load_current_run(run_id)
    assert paused is not None
    assert len(stopped_jobs) == 1
    job = paused["ticket_jobs"]["2"]
    assert job["blocked_reason"] == "ticket_paused"
    assert job["pause_resume_phase"] == "waiting_checks"
    assert job["merge_intent"] == stopped_jobs[0]["merge_intent"]
    assert paused["ticket_integration_holder"] is None
    assert "2" not in paused["ticket_pause_requests"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data["delivery"]["pull_requests"][0]["state"] == "OPEN"
    assert data["delivery"].get("closed_issues", []) == []

    data["delivery"]["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True, ticket_number=2,
    )
    assert resumed["ticket_jobs"]["2"]["integration_resume_phase"] == "waiting_checks"
    ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, 2)
    waiting = states.load_current_run(run_id)
    assert waiting is not None
    job = waiting["ticket_jobs"]["2"]
    assert job["phase"] == "waiting_checks"
    assert waiting["ticket_integration_holder"] == 2
    for key in ("candidate_sha", "publication_sha", "pr_number", "review_budget", "merge_intent"):
        assert job[key] == stopped_jobs[0][key]


@pytest.mark.parametrize("next_write", ["push", "create_pr"])
def test_stop_during_publication_read_prevents_new_write_and_can_resume(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, next_write: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2,))
    run_id = str(state["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = TicketAgents(git_repo, (2,), lambda _number: None)
    engine = ticket_engine(git, states, fixture, agents)
    assert engine.develop_frontier(run_id, state)
    events: list[str] = []
    before_stop: list[dict[str, Any]] = []
    verify = FixtureGitHubPublisher.verify_ticket_pr_before_publish
    revision = FixtureGitHubPublisher.current_effective_revision
    publish = FixtureGitHubPublisher.publish_branch
    ensure_pr = FixtureGitHubPublisher.ensure_ticket_pr

    def stop() -> None:
        current = states.load_current_run(run_id)
        assert current is not None
        job = current["ticket_jobs"]["2"]
        assert job.get("ticket_write_intent") is None
        assert current["ticket_integration_holder"] == 2
        before_stop.append(deepcopy(job))
        request_ticket_pause(states, run_id, 2)
        events.append("stop")

    def verify_then_stop(publisher: FixtureGitHubPublisher, **kwargs: Any) -> None:
        verify(publisher, **kwargs)
        if next_write == "push" and not before_stop:
            stop()

    def revision_then_stop(publisher: FixtureGitHubPublisher, **kwargs: Any) -> str:
        result = revision(publisher, **kwargs)
        current = states.load_current_run(run_id)
        assert current is not None
        job = current["ticket_jobs"]["2"]
        if (
            next_write == "create_pr" and not before_stop
            and job.get("phase") == "publishing"
            and job.get("published_sha") == job.get("publication_sha")
            and job.get("published_sha") is not None
        ):
            stop()
        return result

    def record_push(publisher: FixtureGitHubPublisher, *args: Any, **kwargs: Any) -> Any:
        events.append("push")
        return publish(publisher, *args, **kwargs)

    def record_pr(publisher: FixtureGitHubPublisher, **kwargs: Any) -> int:
        events.append("create_pr")
        return ensure_pr(publisher, **kwargs)

    monkeypatch.setattr(FixtureGitHubPublisher, "verify_ticket_pr_before_publish", verify_then_stop)
    monkeypatch.setattr(FixtureGitHubPublisher, "current_effective_revision", revision_then_stop)
    monkeypatch.setattr(FixtureGitHubPublisher, "publish_branch", record_push)
    monkeypatch.setattr(FixtureGitHubPublisher, "ensure_ticket_pr", record_pr)
    engine.deliver_ticket(run_id, 2)

    assert events == (["stop"] if next_write == "push" else ["push", "stop"])
    paused = states.load_current_run(run_id)
    assert paused is not None
    job = paused["ticket_jobs"]["2"]
    assert job["blocked_reason"] == "ticket_paused"
    assert job["pause_resume_phase"] == "publishing"
    assert paused["ticket_integration_holder"] is None
    assert "2" not in paused["ticket_pause_requests"]
    assert job.get("ticket_write_intent") is None
    assert len(before_stop) == 1
    for key in ("candidate_sha", "publication_sha", "review_budget", "ci_recovery", "ticket_branch_generation"):
        assert job[key] == before_stop[0][key]
    remote = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert remote.get("pull_requests", []) == []
    assert remote.get("closed_issues", []) == []
    assert remote["published_branches"][job["ticket_branch"]] == (
        job["base_sha"] if next_write == "push" else job["publication_sha"]
    )

    Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True, ticket_number=2,
    )
    engine.deliver_ticket(run_id, 2)
    resumed = states.load_current_run(run_id)
    assert resumed is not None
    assert resumed["ticket_jobs"]["2"]["phase"] == "waiting_checks"
    assert resumed["ticket_jobs"]["2"]["pr_number"] == 1
    assert events.count("push") == events.count("create_pr") == 1
    assert resumed["ticket_jobs"]["2"]["review_budget"] == before_stop[0]["review_budget"]
