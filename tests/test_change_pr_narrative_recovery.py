"""Change PR 更新中断后只接受旧值或目标值，保留外部第三种文案。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_publish import GhGitHubPublisher
from agent_run.state import StateStore


def _mock_pr_readback(publisher: GhGitHubPublisher, monkeypatch: pytest.MonkeyPatch) -> None:
    def pull(number: int) -> dict[str, Any]:
        matches = [item for item in publisher._change_prs("unused", state="all") if item["number"] == number]
        if len(matches) != 1:
            raise GitHubReadError("github_read_failed", "Original PR is missing")
        return matches[0]

    def live(number: int) -> dict[str, Any]:
        current = pull(number)
        head, base = current["head"], current["base"]
        return {
            "state": "OPEN", "head_branch": head["ref"], "head_sha": head["sha"],
            "head_repository": head["repo"]["full_name"], "base_branch": base["ref"],
            "base_sha": base["sha"], "base_repository": base["repo"]["full_name"],
        }

    def narrative(number: int) -> dict[str, str]:
        current = pull(number)
        if not all(isinstance(current.get(key), str) for key in ("title", "body")):
            raise GitHubReadError("github_invalid_response", "PR narrative must contain title and body")
        return {"title": current["title"], "body": current["body"]}

    monkeypatch.setattr(publisher, "live_pull_request", live)
    monkeypatch.setattr(publisher, "read_run_pr_narrative", narrative)


@pytest.mark.parametrize("fault", ["response", "result_save"])
@pytest.mark.parametrize("observed", ["target", "prior", "external"])
def test_change_pr_unknown_write_reconciles_saved_narrative(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, fault: str, observed: str,
) -> None:
    git = GitRepository(git_repo)
    head = git.resolve("HEAD")
    authority = {
        "branch": "agent-run/test/ticket-2", "base_branch": "main",
        "expected_head_sha": head, "expected_base_sha": head,
        "title": "Publish title", "body": "Publish body",
    }
    remote: dict[str, Any] = {
        "number": 12, "title": "Previous title", "body": "Previous body",
        "head": {"ref": authority["branch"], "sha": head, "repo": {"full_name": "example/project"}},
        "base": {"ref": "main", "sha": head, "repo": {"full_name": "example/project"}},
    }
    states = StateStore(git_repo / ".change-pr-state")
    state: dict[str, Any] = {"ticket_write_intent": {
        "action": "ensure_ticket_pr", "branch": authority["branch"],
        "head_sha": head, "base_sha": head, "base_branch": "main",
    }}
    states.save_run("test", state)
    writes: list[tuple[str, ...]] = []
    fail = True

    def publisher() -> GhGitHubPublisher:
        result = GhGitHubPublisher("example/project", git)
        _mock_pr_readback(result, monkeypatch)
        monkeypatch.setattr(result, "_change_prs", lambda *_args, **_kwargs: [deepcopy(remote)])

        def write(*args: str) -> None:
            nonlocal fail
            saved = states.load_run("test")
            assert saved is not None
            intent = saved["ticket_write_intent"]
            assert intent["pr_number"] == 12
            assert intent["prior_narrative"] == {"title": "Previous title", "body": "Previous body"}
            assert intent["target_narrative"] == {"title": authority["title"], "body": authority["body"]}
            writes.append(args)
            remote.update(title=authority["title"], body=authority["body"])
            if fail and fault == "response":
                fail = False
                raise OSError("PR edit applied; response lost")

        monkeypatch.setattr(result, "_require", write)
        return result

    def ensure(current: dict[str, Any]) -> int:
        return publisher().ensure_change_pr(
            **authority, write_intent=current["ticket_write_intent"],
            save_intent=lambda: states.save_run("test", current),
        )

    if fault == "response":
        with pytest.raises(OSError, match="response lost"):
            ensure(state)
    else:
        assert ensure(state) == 12
        # The write outcome is not installed in the durable Job: a restart
        # reads the real StateStore checkpoint made before the Gh write.
        state.pop("ticket_write_intent")
        original = states._save_run_unlocked
        monkeypatch.setattr(states, "_save_run_unlocked", lambda *_args: (_ for _ in ()).throw(OSError("result save lost")))
        with pytest.raises(OSError, match="result save lost"):
            states.save_run("test", state)
        monkeypatch.setattr(states, "_save_run_unlocked", original)
        fail = False
    current = StateStore(states.root).load_run("test")
    assert current is not None
    intent = deepcopy(current["ticket_write_intent"])
    if observed == "prior":
        remote.update(intent["prior_narrative"])
    elif observed == "external":
        remote.update(title="Maintainer title", body="Maintainer body")
    if observed == "external":
        with pytest.raises(GitHubReadError, match="neither .*saved prior nor target"):
            ensure(current)
        assert remote["body"] == "Maintainer body"
        assert current["ticket_write_intent"] == intent
        assert len(writes) == 1
    else:
        assert ensure(current) == 12
        assert len(writes) == (1 if observed == "target" else 2)
        assert remote["body"] == authority["body"]
    assert StateStore(states.root).load_run("test")["ticket_write_intent"] == intent


def test_change_pr_read_failure_does_not_admit_write(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    _mock_pr_readback(publisher, monkeypatch)
    writes: list[tuple[str, ...]] = []
    monkeypatch.setattr(publisher, "_change_prs", lambda *_args, **_kwargs: (_ for _ in ()).throw(GitHubReadError("github_read_failed", "read unavailable")))
    monkeypatch.setattr(publisher, "_require", lambda *args: writes.append(args))
    intent: dict[str, Any] = {"action": "ensure_ticket_pr"}
    with pytest.raises(GitHubReadError, match="read unavailable"):
        publisher.ensure_change_pr(
            branch="ticket", base_branch="main", title="title", body="body",
            expected_head_sha="a" * 40, expected_base_sha="b" * 40,
            write_intent=intent, save_intent=lambda: None,
        )
    assert intent == {"action": "ensure_ticket_pr"}
    assert writes == []


@pytest.mark.parametrize("parent_only", [False, True], ids=["ticket", "parent"])
@pytest.mark.parametrize("fault", ["response", "result_save"])
def test_public_resume_preserves_change_pr_intent_until_narrative_is_known(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, parent_only: bool, fault: str,
) -> None:
    from agent_run.controller import Controller
    from agent_run.delivery import TicketDeliveryEngine
    from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
    from agent_run.parent_delivery import ParentDeliveryEngine
    from support.delivery_agents import PassAgents, issue
    from support.seeding import write_fixture

    fixture = write_fixture(git_repo / "github.json", issues={} if parent_only else {"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = str(state["run_id"])
    subject = "parent_job" if parent_only else "active_ticket_job"
    checkout = states.root / "worktrees" / run_id / ("parent" if parent_only else "ticket-3")
    agents = PassAgents(checkout)
    engine_type = ParentDeliveryEngine if parent_only else TicketDeliveryEngine
    publisher = FixtureGitHubPublisher(fixture, git)
    original_ensure = getattr(publisher, "ensure_change_pr" if parent_only else "ensure_ticket_pr")
    applied = False
    target: dict[str, str] = {}

    def interrupt(*args: Any, **kwargs: Any) -> int:
        nonlocal applied
        # Build a pre-existing PR at exactly the published ref, so this tests
        # an update, rather than unknown creation covered elsewhere.
        seed = {key: value for key, value in kwargs.items() if key not in {"write_intent", "save_intent"}}
        seed.update(title="Previous title", body="Previous body")
        original_ensure(*args, **seed)
        target.update(title=kwargs["title"], body=kwargs["body"])
        number = original_ensure(*args, **kwargs)
        applied = True
        if fault == "response":
            raise RuntimeError("Change PR update response lost")
        return number

    monkeypatch.setattr(publisher, "ensure_change_pr" if parent_only else "ensure_ticket_pr", interrupt)
    original_save = states._save_run_unlocked
    injected = False

    def save(identifier: str, current: dict[str, Any]) -> None:
        nonlocal injected
        job = current[subject]
        if applied and fault == "result_save" and not injected and "ticket_write_intent" not in job:
            injected = True
            raise OSError("Change PR result save lost")
        original_save(identifier, current)

    monkeypatch.setattr(states, "_save_run_unlocked", save)
    if parent_only:
        with pytest.raises((RuntimeError, OSError), match="(response|save) lost"):
            engine_type(git=git, states=states, github=publisher, agents=agents).deliver(run_id)
    else:
        engine_type(git=git, states=states, github=publisher, agents=agents).deliver(run_id)
    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    before = deepcopy(interrupted[subject])
    intent = before["ticket_write_intent"]
    assert intent["pr_number"] == 1
    assert intent["target_narrative"] == target
    assert injected == (fault == "result_save")
    assert before["phase"] == ("publishing" if parent_only else "blocked")
    assert len(agents.publication_requests) == 1
    # A fresh Publisher sees a third value after the interrupted update.
    remote = FixtureGitHubPublisher(fixture, git)
    remote.data["delivery"]["pull_requests"][0].update(title="Maintainer title", body="Maintainer body")
    remote._save()
    if not parent_only:
        Controller(FixtureGitHubReader(fixture), git, states).resume(run_id, explicit_resume=True)
    paused = engine_type(git=git, states=states, github=FixtureGitHubPublisher(fixture, git), agents=agents).deliver(run_id)
    assert paused["status"] == "blocked"
    assert paused[subject]["execution_resume_phase"] == "publishing"
    assert paused[subject]["ticket_write_intent"] == intent
    if not parent_only:
        assert paused["ticket_integration_holder"] == 3
    observed = FixtureGitHubPublisher(fixture, git)
    assert observed.data["delivery"]["pull_requests"][0]["body"] == "Maintainer body"
    observed.data["delivery"]["pull_requests"][0].update(target)
    observed._save()
    Controller(FixtureGitHubReader(fixture), git, states).resume(run_id, explicit_resume=True)
    completed = engine_type(git=git, states=states, github=FixtureGitHubPublisher(fixture, git), agents=agents).deliver(run_id)
    assert completed["status"] == ("parent_approval_pending" if parent_only else "ticket_completed")
    assert "ticket_write_intent" not in completed[subject]
    assert len(agents.publication_requests) == 1
    for key in ("candidate_sha", "publication_sha", "acceptance_record", "review_budget"):
        assert completed[subject][key] == before[key]


@pytest.mark.parametrize("observation", ["missing", "replacement", "missing_body"])
def test_saved_change_pr_identity_and_narrative_cannot_be_guessed(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, observation: str,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    _mock_pr_readback(publisher, monkeypatch)
    head = "a" * 40
    base = "b" * 40
    pull: dict[str, Any] = {
        "number": 12, "title": "Target", "body": "Target body",
        "head": {"ref": "ticket", "sha": head, "repo": {"full_name": "example/project"}},
        "base": {"ref": "main", "sha": base, "repo": {"full_name": "example/project"}},
    }
    intent: dict[str, Any] = {
        "pr_number": 12, "prior_narrative": {"title": "Old", "body": "Old body"},
        "target_narrative": {"title": "Target", "body": "Target body"},
    }
    before = deepcopy(intent)
    if observation == "replacement":
        pull["number"] = 13
    elif observation == "missing_body":
        pull.pop("body")
    monkeypatch.setattr(publisher, "_change_prs", lambda *_args, **_kwargs: [] if observation == "missing" else [pull])
    writes: list[tuple[str, ...]] = []
    monkeypatch.setattr(publisher, "_require", lambda *args: writes.append(args))
    with pytest.raises(GitHubReadError):
        publisher.ensure_change_pr(
            branch="ticket", base_branch="main", title="Target", body="Target body",
            expected_head_sha=head, expected_base_sha=base,
            write_intent=intent, save_intent=lambda: None,
        )
    assert writes == []
    assert intent == before


@pytest.mark.parametrize("existing", [False, True], ids=["create", "edit"])
def test_change_pr_readback_external_edit_preserves_pending_intent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, existing: bool,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    _mock_pr_readback(publisher, monkeypatch)
    pull = {
        "number": 12, "title": "Old", "body": "Old body",
        "head": {"ref": "ticket", "sha": "a" * 40, "repo": {"full_name": "example/project"}},
        "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "example/project"}},
    }
    writes: list[tuple[str, ...]] = []
    monkeypatch.setattr(publisher, "_change_prs", lambda *_args, **_kwargs: [pull] if existing or writes else [])

    def write(*args: str) -> None:
        writes.append(args)
        pull.update(title="Maintainer title", body="Maintainer body")

    monkeypatch.setattr(publisher, "_require", write)
    intent: dict[str, Any] = {"action": "ensure_ticket_pr"}
    with pytest.raises(GitHubReadError) as error:
        publisher.ensure_change_pr(
            branch="ticket", base_branch="main", title="Target", body="Target body",
            expected_head_sha="a" * 40, expected_base_sha="b" * 40,
            write_intent=intent, save_intent=lambda: None,
        )
    assert error.value.code == "github_write_outcome_unknown"
    assert intent["target_narrative"] == {"title": "Target", "body": "Target body"}
    assert len(writes) == 1
    assert pull["body"] == "Maintainer body"


def test_change_pr_readback_prior_value_allows_continuation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = GhGitHubPublisher("example/project", GitRepository(git_repo))
    _mock_pr_readback(publisher, monkeypatch)
    pull = {
        "number": 12, "title": "Old", "body": "Old body",
        "head": {"ref": "ticket", "sha": "a" * 40, "repo": {"full_name": "example/project"}},
        "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": "example/project"}},
    }
    writes: list[tuple[str, ...]] = []
    monkeypatch.setattr(publisher, "_change_prs", lambda *_args, **_kwargs: [pull])

    def write(*args: str) -> None:
        writes.append(args)
        if len(writes) == 2:
            pull.update(title="Target", body="Target body")

    monkeypatch.setattr(publisher, "_require", write)
    intent: dict[str, Any] = {"action": "ensure_ticket_pr"}
    authority = {
        "branch": "ticket", "base_branch": "main", "title": "Target", "body": "Target body",
        "expected_head_sha": "a" * 40, "expected_base_sha": "b" * 40,
        "write_intent": intent, "save_intent": lambda: None,
    }
    with pytest.raises(GitHubReadError) as error:
        publisher.ensure_change_pr(**authority)
    assert error.value.code == "github_write_failed"
    assert intent["prior_narrative"] == {"title": "Old", "body": "Old body"}
    assert publisher.ensure_change_pr(**authority) == 12
    assert len(writes) == 2
