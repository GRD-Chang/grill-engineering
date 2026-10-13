"""真实恢复边界：目标漂移不能丢弃尚未对账的 Change PR 更新。"""
from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.state import StateStore
from support.delivery_agents import PassAgents, issue
from support.seeding import write_fixture


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True,
    ).stdout.strip()


@pytest.mark.parametrize("parent_only", [False, True], ids=["ticket", "parent"])
@pytest.mark.parametrize("fault", ["response", "result_save"])
def test_target_append_preserves_unknown_pr_until_public_resume_can_settle(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, parent_only: bool, fault: str,
) -> None:
    # Both delivery adapters and both durable interruption windows matter:
    # pure Publisher tests cannot observe integration invalidating an intent.
    fixture = write_fixture(git_repo / "github.json", issues={} if parent_only else {"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    initial, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(initial["run_id"])
    subject = "parent_job" if parent_only else "active_ticket_job"
    checkout = states.root / "worktrees" / run_id / ("parent" if parent_only else "ticket-3")
    agents = PassAgents(checkout)
    engine_type = ParentDeliveryEngine if parent_only else TicketDeliveryEngine
    publisher = FixtureGitHubPublisher(fixture, git)
    operation = "ensure_change_pr" if parent_only else "ensure_ticket_pr"
    original_ensure = getattr(publisher, operation)
    applied = False

    def interrupt(*args: Any, **kwargs: Any) -> int:
        nonlocal applied
        seed = {key: value for key, value in kwargs.items() if key not in {"write_intent", "save_intent"}}
        seed.update(title="Previous title", body="Previous body")
        original_ensure(*args, **seed)
        number = original_ensure(*args, **kwargs)
        applied = True
        if fault == "response":
            raise RuntimeError("Change PR update response lost")
        return number

    monkeypatch.setattr(publisher, operation, interrupt)
    save_method = "_save_run_unlocked" if parent_only else "_save_work_and_notify_unlocked"
    original_save = getattr(states, save_method)
    injected = False

    def save(identifier: str, current: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        nonlocal injected
        if applied and fault == "result_save" and not injected and "ticket_write_intent" not in current[subject]:
            injected = True
            raise OSError("Change PR result save lost")
        original_save(identifier, current, *args, **kwargs)

    monkeypatch.setattr(states, save_method, save)
    engine = engine_type(git=git, states=states, github=publisher, agents=agents)
    if parent_only:
        with pytest.raises((RuntimeError, OSError), match="(response|save) lost"):
            engine.deliver(run_id)
    else:
        engine.deliver(run_id)
    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    before = deepcopy(interrupted[subject])
    intent = before["ticket_write_intent"]
    assert intent["pr_number"] == 1
    assert injected == (fault == "result_save")
    assert len(agents.publication_requests) == 1
    reviews_before = agents.review_count

    (git_repo / "maintainer.txt").write_text("target append\n", encoding="utf-8")
    _git(git_repo, "add", "maintainer.txt")
    _git(git_repo, "commit", "-m", "append target")
    target = _git(git_repo, "rev-parse", "HEAD")
    assert git.is_ancestor(intent["base_sha"], target)
    _git(git_repo, "update-ref", f"refs/heads/{intent['base_branch']}", target)
    remote = FixtureGitHubPublisher(fixture, git)
    remote.data["delivery"]["published_branches"][intent["base_branch"]] = target
    pull = remote.data["delivery"]["pull_requests"][0]
    pull.update(title="Maintainer title", body="Maintainer body", base_sha=target)
    remote._save()

    def resume(*, explicit: bool = True) -> dict[str, Any]:
        fresh_states = StateStore(states.root)
        if explicit:
            Controller(FixtureGitHubReader(fixture), git, fresh_states).resume(run_id, explicit_resume=True)
        return engine_type(
            git=git, states=fresh_states, github=FixtureGitHubPublisher(fixture, git), agents=agents,
        ).deliver(run_id)

    # Explicit resume is a usable entry, but grants no right to discard the
    # previous unknown write or overwrite an external third narrative.
    for attempt in range(2):
        # Parent process loss leaves runnable publishing work; its restarted
        # engine reaches the recovery pause before explicit Controller resume.
        paused = resume(explicit=not parent_only or attempt > 0)
        assert paused["status"] == "blocked"
        assert paused[subject]["ticket_write_intent"] == intent
        assert paused[subject]["execution_resume_phase"] == "publishing"
        assert len(agents.publication_requests) == 1
        assert agents.review_count == reviews_before
        if not parent_only:
            assert paused["ticket_integration_holder"] == 3
        observed = FixtureGitHubPublisher(fixture, git)
        assert len(observed.data["delivery"]["pull_requests"]) == 1
        assert observed.data["delivery"]["pull_requests"][0]["body"] == "Maintainer body"
        for key in ("publication_sha", "candidate_sha", "acceptance_record"):
            assert paused[subject][key] == before[key]

    # Both proved outcomes permit refreshing the now-stale integration;
    # this read-only settlement must not replay an old write on a new base.
    settlement = "target_narrative" if fault == "response" else "prior_narrative"
    observed.data["delivery"]["pull_requests"][0].update(intent[settlement])
    observed.data["delivery"]["required_checks"] = ["pass"]
    observed._save()
    completed = resume()
    job = completed[subject]
    assert completed["status"] == ("parent_approval_pending" if parent_only else "ticket_completed")
    assert "ticket_write_intent" not in job
    assert len(agents.publication_requests) == 2
    assert len(agents.development_requests) == 1
    assert job["publication_sha"] != before["publication_sha"]
    assert git.is_ancestor(target, job["publication_sha"])
    assert job["parent_integration" if parent_only else "ticket_integration"]["target_sha"] == target
    assert job["required_checks_evidence"]["head_sha"] == job["publication_sha"]
    assert job["required_checks_evidence"]["result"] == "pass"
    if parent_only:
        assert agents.review_count == reviews_before + 1
        assert job["acceptance_record"]["reviewed_candidate_sha"] == job["candidate_sha"]
        assert job["acceptance_record"]["reviewed_base_sha"] == target
        assert "approval_grant" not in job
        assert job["phase"] == "ready_for_approval"
    else:
        assert job["candidate_sha"] == before["candidate_sha"]
        assert job["acceptance_record"] == before["acceptance_record"]
    final_remote = FixtureGitHubPublisher(fixture, git)
    assert len(final_remote.data["delivery"]["pull_requests"]) == 1
    assert final_remote.data["delivery"]["pull_requests"][0]["number"] == intent["pr_number"]
