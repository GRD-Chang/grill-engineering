from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from conftest import seed_idle_control, seed_run
from support.workspace import managed_repo, managed_state, prepare_workspace
from run_publication_test_support import InvocationRunPublicationAgents, _accepted_run


def prepare_accepted_run(
    repo: Path, *, ticket_number: int = 3, delivery: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Prepare an accepted Run for tests of its next CLI action.

    The completed Ticket is historical setup. Git commits, Run Acceptance,
    durable state and CLI ownership registration remain real.
    """
    workspace = prepare_workspace(repo)
    state, _, _, _ = _accepted_run(
        workspace.repository_root, ticket_number=ticket_number,
        state_root=workspace.state_root,
    )
    fixture = workspace.repository_root / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {}).update(delivery or {})
    data["delivery"]["closed_issues"] = [ticket_number]
    integration = state["ticket_jobs"][str(ticket_number)]["deterministic_integration_record"]
    pulls = data["delivery"].setdefault("pull_requests", [])
    # FixtureGitHubPublisher allocates len(pulls) + 1. Preserve the historical
    # Ticket PR so Final and Repair PRs cannot reuse its integration identity.
    assert integration["pr_number"] == integration["pr"]["number"] == len(pulls) + 1
    pulls.append({
        **integration["pr"],
        "scope": "ticket", "state": "MERGED", "primary_ticket": ticket_number,
        "branch": f"agent-run/{state['run_id']}/ticket-{ticket_number}",
        "base_branch": state["run_branch"],
        "head_repository": state["repository"], "base_repository": state["repository"],
        "head_sha": integration["publication_sha"], "base_sha": integration["base_sha"],
        "integrated_sha": integration["integrated_sha"],
        "title": f"Completed Ticket #{ticket_number}", "body": "Accepted historical delivery.",
    })
    fixture.write_text(json.dumps(data), encoding="utf-8")

    registered = seed_run(repo, fixture)
    assert registered.returncode == 0, registered.stderr
    seed_idle_control(
        TaskControlStore(managed_state(repo)),
        TaskKey(managed_repo(repo), str(state["repository"]), 1),
        str(state["run_id"]),
        state_dir=managed_state(repo),
    )
    return fixture, state


def prepare_published_run(
    repo: Path, *, ticket_number: int = 3, delivery: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Add a real Final PR at the explicit approval boundary."""
    fixture, state = prepare_accepted_run(
        repo, ticket_number=ticket_number, delivery=delivery,
    )
    git = GitRepository(managed_repo(repo))
    published = RunPublicationEngine(
        git=git,
        states=StateStore(managed_state(repo)),
        agents=InvocationRunPublicationAgents(),
        github=FixtureGitHubPublisher(fixture, git),
        default_branch="main",
        default_head_sha=git.resolve("main"),
    ).publish(str(state["run_id"]))
    assert published["status"] == "run_approval_pending"
    historical_pr = state["ticket_jobs"][str(ticket_number)]["deterministic_integration_record"]["pr_number"]
    assert published["run_publication"]["pr_number"] != historical_pr
    pulls = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
    assert len({pull["number"] for pull in pulls}) == len(pulls)
    return fixture, published
