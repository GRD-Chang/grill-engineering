"""Original Final Run authority survives incomplete or conflicting readback."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.github import GitHubReadError
from agent_run.run_pr_reconciliation import reconcile_run_pr_write


@pytest.mark.parametrize("observation", [
    "prior", "target", "prior_advanced", "target_advanced", "third", "closed", "foreign_head", "foreign_base",
    "missing_target", "missing_authority", "missing_publisher", "missing_pr", "read_failure", "save_failure",
])
def test_original_write_is_checked_without_remote_mutation(observation: str) -> None:
    intent: dict[str, Any] = {
        "action": "refresh_final_pr_narrative", "pr_number": 7,
        "authority": {"branch": "run/1", "head_sha": "original-head",
                      "base_branch": "main", "base_sha": "old-base"},
        "prior_narrative": {"title": "Prior", "body": "Prior body"},
        "target_narrative": {"title": "Target", "body": "Target body"},
    }
    if observation == "missing_target":
        intent.pop("target_narrative")
    if observation == "missing_authority":
        intent["authority"] = {}
    if observation == "missing_pr":
        intent.pop("pr_number")
    state: dict[str, Any] = {
        "repository": "example/repo", "status": "waiting_external",
        "run_publication": {"phase": "waiting_external", "artifact": {"pr_title": "Target"},
                            "write_intent": intent},
    }
    original = deepcopy(state["run_publication"])
    live = {
        "state": "CLOSED" if observation == "closed" else "OPEN",
        "head_branch": "run/1",
        "head_sha": "foreign" if observation == "foreign_head" else "original-head",
        "base_branch": "foreign" if observation == "foreign_base" else "main",
        "base_sha": "advanced-base" if observation.endswith("_advanced") or observation == "save_failure" else "old-base", "head_repository": "example/repo",
        "base_repository": "example/repo",
    }
    def read(_number: int) -> dict[str, str]:
        if observation == "read_failure":
            raise GitHubReadError("github_timeout", "Read unavailable")
        return intent.get(
            f"{observation.split('_')[0]}_narrative",
            intent["target_narrative"] if observation == "save_failure" else {"title": "External", "body": "External body"},
        )

    publisher = SimpleNamespace(
        live_pull_request=lambda _number: live,
        read_run_pr_narrative=read,
        find_change_pr_number=lambda **_kwargs: None,
    )  # Read methods only: this gate must not admit a new write.
    saves: list[Any] = []

    def save(current: dict[str, Any]) -> None:
        saves.append(deepcopy(current))
        if observation == "save_failure" and len(saves) == 1:
            raise OSError("Readback checkpoint lost")

    settled = reconcile_run_pr_write(
        state, None if observation == "missing_publisher" else publisher,
        save,
    )
    assert settled is (observation in {"prior", "target", "prior_advanced", "target_advanced"})
    publication = state["run_publication"]
    if observation.endswith("_advanced"):
        assert "write_intent" not in publication
        assert publication["phase"] == "stale"
    else:
        assert publication["write_intent"] == original["write_intent"]
    assert publication["artifact"] == original["artifact"]
    if settled:
        assert state["status"] == "waiting_external"
        assert publication["pr_number"] == 7
        assert len(saves) == 1
    else:
        assert state["status"] == "ready_for_human"
        assert publication["pause_resume_phase"] == "waiting_external"
        assert publication["blocked_reason"] == "github_write_outcome_unknown"
        assert len(saves) == (2 if observation == "save_failure" else 1)


@pytest.mark.parametrize("observation", ["absent", "target", "third", "closed", "ambiguous"])
def test_lost_create_uses_unique_original_pr_lookup_before_retiring(observation: str) -> None:
    target = {"title": "Target", "body": "Target body"}
    intent = {
        "action": "create_final_pr",
        "authority": {"branch": "run/1", "head_sha": "original-head",
                      "base_branch": "main", "base_sha": "old-base"},
        "target_narrative": target,
    }
    state: dict[str, Any] = {
        "repository": "example/repo", "status": "waiting_external",
        "run_publication": {"phase": "waiting_external", "write_intent": intent},
    }
    lookups: list[str] = []

    def lookup(*, branch: str) -> int | None:
        lookups.append(branch)
        if observation == "ambiguous":
            raise GitHubReadError("ambiguous_change_pr", "Multiple PRs found")
        return None if observation == "absent" else 7

    publisher = SimpleNamespace(
        find_change_pr_number=lookup,
        live_branch_heads=lambda *_args: ("original-head", "advanced-base"),
        live_pull_request=lambda _number: {
            "state": "CLOSED" if observation == "closed" else "OPEN",
            "head_branch": "run/1", "head_sha": "original-head",
            "base_branch": "main", "base_sha": "advanced-base",
            "head_repository": "example/repo", "base_repository": "example/repo",
        },
        read_run_pr_narrative=lambda _number: (
            {"title": "External", "body": "External body"} if observation == "third" else target
        ),
    )
    saved: list[Any] = []
    settled = reconcile_run_pr_write(state, publisher, lambda current: saved.append(deepcopy(current)))
    publication = state["run_publication"]
    assert lookups == ["run/1"]
    assert len(saved) == 1
    assert settled is (observation in {"absent", "target"})
    if settled:
        assert "write_intent" not in publication
        assert publication["phase"] == "stale"
        assert publication.get("pr_number") == (7 if observation == "target" else None)
    else:
        assert publication["write_intent"] == intent
        assert state["status"] == "ready_for_human"


@pytest.mark.parametrize("fault", ["response", "result_save"])
def test_final_create_restart_uses_public_resume_and_never_repeats_completed_write(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    from agent_run.controller import Controller
    from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
    from agent_run.run_publication import RunPublicationEngine
    from agent_run.state import StateStore
    from support.run_publication import RunPublicationAgents
    from support.run_scenarios import _accepted_run

    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    agents = RunPublicationAgents()
    original_create = publisher.ensure_run_pr
    created = False

    def create(**authority: Any) -> int:
        nonlocal created
        durable = StateStore(states.root).load_current_run(run_id)["run_publication"]
        assert durable["write_intent"]["action"] == "create_final_pr"
        assert durable["write_intent"]["target_narrative"] == {
            "title": authority["title"], "body": authority["body"],
        }
        number = original_create(**authority)
        created = True
        if fault == "response":
            raise OSError("Final create response lost")
        return number

    monkeypatch.setattr(publisher, "ensure_run_pr", create)
    original_save = states._save_run_unlocked
    failed = False

    def save(identifier: str, current: dict[str, Any]) -> None:
        nonlocal failed
        if fault == "result_save" and created and not failed and "write_intent" not in current["run_publication"]:
            failed = True
            raise OSError("Final create result checkpoint lost")
        original_save(identifier, current)

    monkeypatch.setattr(states, "_save_run_unlocked", save)
    waiting = RunPublicationEngine(
        git=git, states=states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(run_id)
    assert waiting["status"] == "waiting_external"
    assert failed is (fault == "result_save")
    before = StateStore(states.root).load_current_run(run_id)["run_publication"]
    assert before["write_intent"]["action"] == "create_final_pr"
    assert len(agents.requests) == 1

    fresh_states = StateStore(states.root)
    fresh_publisher = FixtureGitHubPublisher(git_repo / "github.json", git)
    # A conflicting result exhausts the existing bounded operation window;
    # public resume renews only that window and preserves the original write.
    fresh_publisher.data["delivery"]["pull_requests"][0].update(
        title="Maintainer", body="Unconfirmed text",
    )
    fresh_publisher._save()
    uncertain = RunPublicationEngine(
        git=git, states=fresh_states, agents=agents, github=fresh_publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    for _ in range(6):
        paused = uncertain.publish(run_id)
    assert paused["status"] == "publication_pending"
    assert paused["run_publication"]["write_intent"] == before["write_intent"]
    assert StateStore(states.root).load_current_run(run_id)["run_publication"]["write_intent"] == before["write_intent"]
    assert len(agents.requests) == 1
    fresh_publisher.data["delivery"]["pull_requests"][0].update(before["write_intent"]["target_narrative"])
    fresh_publisher._save()
    Controller(FixtureGitHubReader(git_repo / "github.json"), git, StateStore(states.root)).resume(
        run_id, explicit_resume=True,
    )
    fresh_publisher = FixtureGitHubPublisher(git_repo / "github.json", git)
    read_numbers: list[int] = []
    original_read = fresh_publisher.read_run_pr_narrative

    def read(number: int) -> dict[str, str]:
        read_numbers.append(number)
        return original_read(number)

    def forbidden_write(**_authority: Any) -> None:
        raise AssertionError("completed Final PR write must only be read back")

    monkeypatch.setattr(fresh_publisher, "read_run_pr_narrative", read)
    monkeypatch.setattr(fresh_publisher, "ensure_run_pr", forbidden_write)
    monkeypatch.setattr(fresh_publisher, "refresh_run_pr_narrative", forbidden_write)
    engine = RunPublicationEngine(
        git=git, states=fresh_states, agents=agents, github=fresh_publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    )
    recovered = engine.publish(run_id)
    assert recovered["status"] == "run_approval_pending"
    publication = recovered["run_publication"]
    assert read_numbers and set(read_numbers) == {publication["pr_number"]}
    assert publication["artifact"] == before["artifact"]
    assert "write_intent" not in publication
    assert "approval_grant" not in publication
    assert publication["record"]["run_head_sha"] == before["write_intent"]["authority"]["head_sha"]
    assert len(fresh_publisher.data["delivery"]["pull_requests"]) == 1
    assert len(agents.requests) == 1
    assert engine.publish(run_id)["status"] == "run_approval_pending"
    assert len(agents.requests) == 1
