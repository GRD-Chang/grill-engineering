from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.external_supervision import ExternalSupervisor, restore_supervision_wait
from agent_run.git import GitError, GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.github_publish import GhGitHubPublisher
from agent_run.state import StateStore
from conftest import write_fixture
from test_delivery import PassAgents, ScriptedPublisher, issue


@pytest.mark.parametrize("failure_point", ["revision", "verify", "publish"])
def test_publication_read_outage_preserves_work_through_bounded_pause(
    git_repo: Path, failure_point: str,
) -> None:
    states = StateStore(git_repo / ".agent-run")
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    run_id = state["run_id"]

    class OfflineAfterPublication(ScriptedPublisher):
        offline = True

        def current_effective_revision(self, **kwargs: Any) -> str:
            saved = states.load_run(run_id)
            job = saved.get("active_ticket_job") if saved else None
            if (
                failure_point == "revision" and self.offline
                and isinstance(job, dict) and job["phase"] == "publishing"
            ):
                raise GitHubReadError("github_timeout", "GitHub command timed out")
            return super().current_effective_revision(**kwargs)

        def verify_ticket_pr_before_publish(self, **kwargs: Any) -> None:
            if failure_point == "verify" and self.offline:
                raise GitHubReadError("github_timeout", "GitHub command timed out")
            return super().verify_ticket_pr_before_publish(**kwargs)

        def publish_branch(self, *args: Any, **kwargs: Any) -> None:
            if failure_point == "publish" and self.offline:
                raise GitHubReadError("github_timeout", "GitHub command timed out")
            return super().publish_branch(*args, **kwargs)

    github = OfflineAfterPublication(git_repo)
    agents = PassAgents(states.root / "worktrees" / run_id / "ticket-3")
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=github, agents=agents
    )
    waiting = engine.deliver(run_id)
    assert waiting["status"] == "waiting_external"
    job = deepcopy(waiting["active_ticket_job"])
    assert job["phase"] == "publishing"
    assert job["publication_sha"]
    assert waiting["diagnostics"][0]["code"] == "github_timeout"
    assert states.load_run(run_id)["diagnostics"] == waiting["diagnostics"]

    started_at = waiting["supervision_window"]["started_at"]
    now = [started_at]
    supervisor = ExternalSupervisor(
        now=lambda: now[0], sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    while supervisor.before_retry(waiting):
        states.save_run(run_id, waiting)
        waiting = engine.deliver(run_id)
        assert waiting["active_ticket_job"] == job
    assert now[0] - started_at == 600
    assert waiting["status"] == "supervision_timeout"
    assert waiting["diagnostics"][0]["last_error"]["code"] == "github_timeout"
    assert agents.review_count == 1
    assert len(agents.publication_requests) == 1
    assert github.created_prs == 0

    github.offline = False
    restore_supervision_wait(waiting)
    states.save_run(run_id, waiting)
    completed = engine.deliver(run_id)
    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["publication_sha"] == job["publication_sha"]
    assert completed["active_ticket_job"]["review_budget"] == job["review_budget"]
    assert len(agents.publication_requests) == 1
    assert agents.review_count == 1
    assert github.created_prs == 1


def test_proven_github_contradiction_does_not_become_a_network_wait(
    git_repo: Path,
) -> None:
    class InvalidRepository(ScriptedPublisher):
        def current_effective_revision(self, **kwargs: Any) -> str:
            raise GitHubReadError("missing_parent", "Parent is missing")

    states = StateStore(git_repo / ".agent-run")
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    run_id = state["run_id"]
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo),
        states=states,
        github=InvalidRepository(git_repo),
        agents=PassAgents(states.root / "worktrees" / run_id / "ticket-3"),
    )
    with pytest.raises(GitHubReadError, match="Parent is missing"):
        engine.deliver(run_id)
    saved = states.load_run(run_id)
    assert saved is not None
    assert saved["status"] != "waiting_external"
    assert "supervision_window" not in saved


@pytest.mark.parametrize("requirements_changed", [False, True])
def test_publication_result_survives_read_timeout_before_local_commit(
    git_repo: Path, requirements_changed: bool,
) -> None:
    class OfflinePublisher(ScriptedPublisher):
        offline = False

        def current_effective_revision(self, **kwargs: Any) -> str:
            if self.offline:
                raise GitHubReadError("github_timeout", "GitHub command timed out")
            return super().current_effective_revision(**kwargs)

    github = OfflinePublisher(git_repo)

    class CompletedPublication(PassAgents):
        def publication(self, request: dict[str, Any]) -> dict[str, Any]:
            result = super().publication(request)
            github.offline = True
            return result

    states = StateStore(git_repo / ".agent-run")
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    state, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).start(1)
    run_id = state["run_id"]
    agents = CompletedPublication(states.root / "worktrees" / run_id / "ticket-3")
    engine = TicketDeliveryEngine(
        git=GitRepository(git_repo), states=states, github=github, agents=agents
    )
    waiting = engine.deliver(run_id)
    assert waiting["status"] == "waiting_external"
    assert waiting["active_ticket_job"]["phase"] == "accepted"
    assert len(agents.publication_requests) == 1
    github.offline = False
    if requirements_changed:
        github.revision_override = "changed-requirements"
    completed = engine.deliver(run_id)
    if requirements_changed:
        assert completed["status"] == "blocked"
        assert "pending_publication_result" not in completed["active_ticket_job"]
        assert github.created_prs == 0
        assert len(agents.publication_requests) == 1
        return
    assert completed["status"] == "ticket_completed"
    assert len(agents.publication_requests) == 1
    assert completed["active_ticket_job"]["publication_attempts"] == 1


@pytest.mark.parametrize("readback", ["old", "new", "foreign", "absent", "unavailable"])
def test_push_response_loss_uses_remote_facts_before_retrying(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, readback: str,
) -> None:
    reads = ["old", readback]
    writes: list[list[str]] = []

    def read(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        value = reads.pop(0)
        return subprocess.CompletedProcess(
            arguments, 1 if value == "unavailable" else 0,
            "" if value in {"unavailable", "absent"} else f"{value}\trefs/heads/ticket\n",
            "GitHub command timed out" if value == "unavailable" else "",
        )

    def write(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        writes.append(arguments)
        return subprocess.CompletedProcess(arguments, 1, "", "connection reset")

    monkeypatch.setattr("agent_run.github_publish.run_read_command", read)
    monkeypatch.setattr("agent_run.github_publish.run_write_command", write)
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    if readback == "new":
        publisher.publish_branch("ticket", "new", expected_remote_sha="old")
    elif readback in {"foreign", "absent"}:
        with pytest.raises(GitError, match="drifted"):
            publisher.publish_branch("ticket", "new", expected_remote_sha="old")
    else:
        with pytest.raises(GitHubReadError) as caught:
            publisher.publish_branch("ticket", "new", expected_remote_sha="old")
        assert caught.value.code == (
            "github_write_failed" if readback == "old" else "github_read_failed"
        )
    assert len(writes) == 1
    assert "--force-with-lease=refs/heads/ticket:old" in writes[0]
