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
from agent_run.run_driver import DirectRunOperations, RunDriver
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


@pytest.mark.parametrize("subject", ["change", "run"])
@pytest.mark.parametrize("readback", ["absent", "expected", "foreign"])
def test_initial_ref_push_failure_is_recoverable_only_without_foreign_ref(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, subject: str, readback: str,
) -> None:
    reads = ["absent", readback]
    writes: list[list[str]] = []
    branch = "agent-run/run-1" if subject == "run" else "ticket"

    def read(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        value = reads.pop(0)
        return subprocess.CompletedProcess(
            arguments, 0,
            "" if value == "absent" else f"{value}\trefs/heads/{branch}\n", "",
        )

    def write(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        writes.append(arguments)
        return subprocess.CompletedProcess(arguments, 1, "", "connection reset")

    monkeypatch.setattr("agent_run.github_publish.run_read_command", read)
    monkeypatch.setattr("agent_run.github_publish.run_write_command", write)
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))

    def ensure_ref() -> None:
        if subject == "run":
            publisher.ensure_final_run_ref(branch=branch, expected_head_sha="expected")
        else:
            publisher.ensure_change_branch(
                branch=branch, base_branch="main", expected_base_sha="expected",
                expected_remote_sha="expected", recovery_remote_sha="expected",
            )

    if readback == "expected":
        ensure_ref()
    elif readback == "foreign":
        with pytest.raises(GitError):
            ensure_ref()
    else:
        with pytest.raises(GitHubReadError) as caught:
            ensure_ref()
        assert caught.value.code == "github_write_failed"
    assert len(writes) == 1
    assert f"--force-with-lease=refs/heads/{branch}:" in writes[0]


@pytest.mark.parametrize("fetch_fails", [True, False])
def test_sync_fetch_failure_or_foreign_head_does_not_update_local_ref(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, fetch_fails: bool,
) -> None:
    writes: list[list[str]] = []

    def write(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        writes.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 1 if fetch_fails else 0, "", "connection reset" if fetch_fails else ""
        )

    monkeypatch.setattr("agent_run.github_publish.run_write_command", write)
    git = GitRepository(git_repo)
    head = git.resolve("HEAD")
    subprocess.run(
        ["git", "fetch", "--no-tags", ".", "HEAD"], cwd=git_repo,
        check=True, capture_output=True,
    )
    assert git.resolve("FETCH_HEAD") == head
    publisher = GhGitHubPublisher("example/project", git)
    if fetch_fails:
        with pytest.raises(GitHubReadError) as caught:
            publisher.sync_run_branch(run_branch="main", integrated_sha="expected")
        assert caught.value.code == "github_read_failed"
    else:
        with pytest.raises(GitError, match="does not match integrated commit"):
            publisher.sync_run_branch(run_branch="main", integrated_sha="expected")
    assert writes == [["git", "fetch", "--no-tags", "origin", "main"]]
    assert git.resolve("HEAD") == head


@pytest.mark.parametrize("error_code", ["github_write_failed", "github_invalid_response"])
def test_driver_supervises_initial_branch_outage_before_shared_delivery_loop(
    git_repo: Path, error_code: str,
) -> None:
    class OfflineBranch(ScriptedPublisher):
        offline = True

        def ensure_ticket_branch(self, **kwargs: Any) -> None:
            if self.offline:
                raise GitHubReadError(error_code, "initial branch observation failed")
            super().ensure_ticket_branch(**kwargs)

    states = StateStore(git_repo / ".agent-run")
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    reader = FixtureGitHubReader(fixture)
    controller = Controller(reader, git, states)
    state, _ = controller.start(1)
    run_id = state["run_id"]
    publisher = OfflineBranch(git_repo)
    agents = PassAgents(states.root / "worktrees" / run_id / "ticket-3")
    operations = DirectRunOperations(
        controller=controller, states=states, git=git, github_reader=reader,
        publisher_factory=lambda: publisher, agents=agents,
    )
    now = [0.0]
    driver = RunDriver(
        operations=operations, states=states,
        supervisor=ExternalSupervisor(
            now=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        ),
    )
    paused = driver.advance(state)
    if error_code == "github_invalid_response":
        assert paused["status"] == "deterministic_contradiction"
        assert now[0] == 0
        assert not agents.development_thread_ids
        return
    assert paused["status"] == "supervision_timeout"
    assert paused["diagnostics"][0]["last_error"]["code"] == "github_write_failed"
    assert now[0] == 600
    assert not agents.development_thread_ids
    assert not agents.publication_requests
    assert paused["active_ticket_job"]["ticket_number"] == 3
    assert paused["active_ticket_job"]["phase"] == "developing"
    publisher.offline = False
    controller.resume(run_id)
    completed = TicketDeliveryEngine(
        git=git, states=states, github=publisher, agents=agents
    ).deliver(run_id)
    assert completed["status"] == "ticket_completed"
    assert completed["active_ticket_job"]["modification_attempts"] == 1
