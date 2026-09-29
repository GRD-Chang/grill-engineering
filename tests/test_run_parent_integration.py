"""Completed Run acceptance uses live source and target refs before review."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

from agent_run.review_budget import review_budget_owner

from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.controller import Controller
from agent_run.ci_recovery_budget import (
    mark_ci_repair, observe_repairable_ci_failure,
)
from agent_run.cli_presentation import _public_ci_recovery
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_parent_integration import prepare_run_parent_integration
from agent_run.run_publication import RunPublicationEngine
from support.run_artifacts import _passing_artifact
from support.run_agents import ScriptedRunAgents
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run, _completed_run


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=directory, check=True, text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def test_run_parent_acceptance_integrates_two_live_remote_refs_first(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    run_branch = str(state["run_branch"])
    remote = tmp_path / "collaboration.git"
    _git(tmp_path, "clone", "--bare", str(git_repo), str(remote))
    _git(git_repo, "remote", "add", "origin", str(remote))
    collaborator = tmp_path / "collaborator"
    _git(tmp_path, "clone", str(remote), str(collaborator))
    _git(collaborator, "config", "user.name", "Other Developer")
    _git(collaborator, "config", "user.email", "other@example.test")

    (collaborator / "default-added.txt").write_text("default\n", encoding="utf-8")
    _git(collaborator, "add", "default-added.txt")
    _git(collaborator, "commit", "-m", "advance default")
    target = _git(collaborator, "rev-parse", "HEAD")
    _git(collaborator, "push", "origin", "main")
    _git(collaborator, "checkout", run_branch)
    (collaborator / "run-added.txt").write_text("run\n", encoding="utf-8")
    _git(collaborator, "add", "run-added.txt")
    _git(collaborator, "commit", "-m", "collaborate on Run Branch")
    source = _git(collaborator, "rev-parse", "HEAD")
    _git(collaborator, "push", "origin", run_branch)

    fixture = git_repo / "github.json"
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["default_head_sha"] = target
    fixture_data["delivery"]["published_branches"][run_branch] = source
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    old_local_head = git.resolve(run_branch)

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    record = result["run_acceptance"]["run_integration"]
    assert record["source_sha"] == source
    assert record["target_sha"] == target
    assert git.is_ancestor(old_local_head, record["head_sha"])
    assert git.is_ancestor(source, record["head_sha"])
    assert git.is_ancestor(target, record["head_sha"])
    assert git.resolve(run_branch) == record["head_sha"]
    assert agents.review_requests[0]["run_integration_record"] == record
    assert result["run_acceptance"]["validation_attempts"] == 1
    assert result["run_acceptance"]["integration_resync"]["used"] == 0


def test_own_final_ref_readback_does_not_open_new_run_integration(
    git_repo: Path,
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["fail"]
    publisher._save()
    failed = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(),
        github=publisher, default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    run = failed["run_acceptance"]
    record = dict(run["run_integration"])
    assert failed["run_publication"]["published_head_sha"] == record["head_sha"]
    run["phase"] = "pending"
    states.save_run(str(state["run_id"]), failed)
    owner = RunAcceptanceEngine(
        git=git, states=states, agents=ScriptedRunAgents(), github=publisher,
    )

    assert prepare_run_parent_integration(owner, failed, run)
    assert run["run_integration"] == record
    assert run["integration_resync"]["used"] == 0
    assert run.get("blocked_reason") != "run_integration_moving"


def test_published_run_ref_rollback_pauses_before_reintegration(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["fail"]
    publisher._save()
    failed = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(),
        github=publisher, default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    run = failed["run_acceptance"]
    record = dict(run["run_integration"])
    published = failed["run_publication"]["published_head_sha"]
    assert published == record["head_sha"]
    old_source = git.resolve("main")
    assert old_source != published
    # The last integration observed this source before publishing its result.
    record["source_sha"] = old_source
    run["run_integration"] = record
    publisher.data["delivery"]["published_branches"][str(state["run_branch"])] = old_source
    publisher._save()
    run["phase"] = "pending"
    owner = RunAcceptanceEngine(
        git=git, states=states, agents=ScriptedRunAgents(), github=publisher,
    )

    assert not prepare_run_parent_integration(owner, failed, run)
    assert run["blocked_reason"] == "integration_history_rewritten"
    assert failed["status"] == "ready_for_human"
    assert run["run_integration"] == record
    assert publisher.live_branch_heads(str(state["run_branch"]), "main")[0] == old_source


def test_final_publication_rejects_rollback_before_repush(git_repo: Path) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["pending"]
    publisher._save()
    publication = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(),
        github=publisher, default_branch="main", default_head_sha=git.resolve("main"),
    )
    waiting = publication.publish(str(state["run_id"]))
    assert waiting["status"] == "waiting_checks"
    published = waiting["run_publication"]["published_head_sha"]
    old_source = git.resolve("main")
    assert published != old_source
    branch = str(state["run_branch"])
    publisher.data["delivery"]["published_branches"][branch] = old_source
    publisher.data["delivery"]["pull_requests"][0]["head_sha"] = old_source
    publisher._save()

    paused = publication.publish(str(state["run_id"]))
    assert paused["status"] == "ready_for_human"
    assert paused["run_acceptance"]["blocked_reason"] == "integration_history_rewritten"
    assert publisher.live_branch_heads(branch, "main")[0] == old_source
    assert paused["run_publication"]["published_head_sha"] == published
    reloaded = states.load_current_run(str(state["run_id"]))
    assert reloaded is not None
    assert publication.publish(str(state["run_id"])) == reloaded


def test_final_publication_append_requires_new_acceptance_and_reuses_pr(
    git_repo: Path, tmp_path: Path
) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    publisher.data["delivery"]["required_checks"] = ["pending"]
    publisher._save()
    publication = RunPublicationEngine(
        git=git, states=states, agents=RunPublicationAgents(),
        github=publisher, default_branch="main", default_head_sha=git.resolve("main"),
    )
    waiting = publication.publish(str(state["run_id"]))
    assert waiting["status"] == "waiting_checks"
    original_pr = waiting["run_publication"]["pr_number"]
    published = waiting["run_publication"]["published_head_sha"]
    branch = str(state["run_branch"])
    collaborator = tmp_path / "collaborator"
    _git(git_repo, "worktree", "add", "--detach", str(collaborator), published)
    try:
        (collaborator / "human.txt").write_text("human\n", encoding="utf-8")
        _git(collaborator, "add", "human.txt")
        _git(collaborator, "-c", "user.name=Collaborator", "-c", "user.email=c@example.invalid",
             "commit", "-m", "append final Run contribution")
        appended = _git(collaborator, "rev-parse", "HEAD")
    finally:
        _git(git_repo, "worktree", "remove", "--force", str(collaborator))
    publisher.data["delivery"]["published_branches"][branch] = appended
    publisher.data["delivery"]["pull_requests"][0]["head_sha"] = appended
    publisher._save()

    refreshed = publication.publish(str(state["run_id"]))
    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["run_publication"]["pr_number"] == original_pr
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    accepted = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
    ).accept(str(state["run_id"]))
    assert accepted["status"] == "run_publication_pending"
    record = accepted["run_acceptance"]["run_integration"]
    assert record["source_sha"] == appended
    assert git.is_ancestor(published, record["head_sha"])
    assert git.is_ancestor(appended, record["head_sha"])
    assert agents.review_requests[0]["run_integration_record"] == record
    assert accepted["run_publication"]["pr_number"] == original_pr
    publisher.data["delivery"]["required_checks"] = ["pass"]
    publisher.data["delivery"]["check_position"] = 0
    publisher._save()
    republished = publication.publish(str(state["run_id"]))
    assert republished["status"] == "run_approval_pending"
    assert republished["run_publication"]["pr_number"] == original_pr
    assert git.is_ancestor(appended, republished["run_publication"]["published_head_sha"])


def test_run_resync_resume_renews_only_exhausted_allowance(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    accepted = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    run = accepted["run_acceptance"]
    budget = run["integration_resync"]
    budget["used"] = budget["limit"]
    budget["open"] = False
    run["phase"] = "ready_for_human"
    run["blocked_reason"] = "integration_resync_exhausted"
    accepted["status"] = "ready_for_human"
    accepted["terminal_kind"] = "waiting_human"
    states.save_run(str(state["run_id"]), accepted)
    review_before = dict(review_budget_owner(run)["review_budget"])

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"]), explicit_resume=True,
    )

    assert resumed["run_acceptance"]["integration_resync"]["generation"] == 2
    assert resumed["run_acceptance"]["integration_resync"]["used"] == 0
    assert resumed["run_acceptance"]["review_budget"] == review_before
    assert resumed["run_acceptance"]["phase"] == "pending"
    assert resumed["status"] == "run_acceptance_pending"


def test_run_resync_resume_preserves_active_repair(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    accepted = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    run = accepted["run_acceptance"]
    budget = run["integration_resync"]
    budget["used"] = budget["limit"]
    budget["open"] = False
    run["repair_job"] = {
        "phase": "merging",
        "repair_mode": "squash",
        "policy_snapshot": deepcopy(run["policy_snapshot"]),
        "review_budget": run.pop("review_budget"),
        "review_budget_history": run.pop("review_budget_history"),
        "ci_recovery": deepcopy(run["ci_recovery"]),
    }
    repair_before = deepcopy(run["repair_job"])
    run.update({
        "phase": "ready_for_human",
        "blocked_reason": "integration_resync_exhausted",
    })
    accepted.update({"status": "ready_for_human", "terminal_kind": "waiting_human"})
    states.save_run(str(state["run_id"]), accepted)

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"]), explicit_resume=True,
    )

    assert resumed["run_acceptance"]["phase"] == "repairing"
    assert resumed["run_acceptance"]["repair_job"] == repair_before
    assert resumed["run_acceptance"]["integration_resync"]["generation"] == 2
    assert resumed["status"] == "run_acceptance_pending"
    assert states.load_current_run(str(state["run_id"]))["run_acceptance"]["phase"] == "repairing"


def test_run_ci_resume_renews_only_the_exhausted_inner_window(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]
    accepted = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))
    run = accepted["run_acceptance"]
    failed_head = git.resolve(str(state["run_branch"]))
    assert observe_repairable_ci_failure(
        run, head_sha=failed_head, outer_limit=3, inner_limit=3,
        is_ticket=False,
    ) == "repair"
    for _ in range(3):
        mark_ci_repair(run, outer_limit=3, inner_limit=3, is_ticket=False)
    assert observe_repairable_ci_failure(
        run, head_sha=failed_head, outer_limit=3, inner_limit=3,
        is_ticket=False,
    ) == "inner_exhausted"
    run.update({
        "phase": "ready_for_human", "blocked_reason": "ci_inner_budget_exhausted",
    })
    failed_observation = {
        "pr_number": 1, "head_sha": failed_head, "result": "fail",
        "checks": [{
            "bucket": "fail", "workflow": "fixture-ci",
            "name": "fixture-required-check", "link": "https://example.invalid/checks/fixture",
        }],
    }
    run["repair_job"] = {
        "phase": "blocked",
        "blocked_reason": "ci_inner_budget_exhausted",
        "escalation_code": "ci_inner_budget_exhausted",
        "repair_mode": "squash",
        "repair_source": "acceptance",
        "pr_number": 1,
        "publication_sha": failed_head,
        "required_checks_origin": failed_observation,
        "policy_snapshot": deepcopy(run["policy_snapshot"]),
        "review_budget": run.pop("review_budget"),
        "review_budget_history": run.pop("review_budget_history"),
        "ci_recovery": deepcopy(run["ci_recovery"]),
    }
    accepted["run_publication"] = {"phase": "stale"}
    accepted.update({"status": "ready_for_human", "terminal_kind": "waiting_human"})
    states.save_run(str(state["run_id"]), accepted)
    review_before = dict(review_budget_owner(run)["review_budget"])
    resync_before = dict(run["integration_resync"])

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"]), explicit_resume=True,
    )

    resumed_run = resumed["run_acceptance"]
    assert resumed_run["ci_recovery"]["inner_window"] == 2
    assert resumed_run["ci_recovery"]["outer_window"] == 1
    assert resumed_run["ci_recovery"]["rounds_used"] == 1
    assert resumed_run["repair_job"]["ci_recovery"] == resumed_run["ci_recovery"]
    assert resumed_run["repair_job"]["phase"] == "repairing"
    assert resumed_run["repair_job"]["next_attempt_kind"] == "ci_repair"
    assert "blocked_reason" not in resumed_run["repair_job"]
    assert review_budget_owner(resumed_run)["review_budget"] == review_before
    assert resumed_run["integration_resync"] == resync_before
    assert resumed_run["phase"] == "repairing"
    assert "repair_request" not in resumed_run
    assert resumed["run_publication"]["phase"] == "stale"
    assert _public_ci_recovery(resumed) == {
        "outer_window": 1, "rounds_used": 1, "rounds_limit": 3,
        "inner_window": 2, "repairs_used": 0, "repairs_limit": 3,
        "review_required": True, "checkpoint_reason": None,
        "failure_head": resumed_run["ci_recovery"]["failure_head"],
        "failed_checks": [{
            "workflow": "fixture-ci", "name": "fixture-required-check",
            "link": "https://example.invalid/checks/fixture",
        }],
    }
    persisted = states.load_current_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["run_acceptance"]["repair_job"]["ci_recovery"] == persisted["run_acceptance"]["ci_recovery"]
