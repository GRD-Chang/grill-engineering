"""独立核验 CLI accepted Ticket 的 Git、审核、持久身份与未发布边界。"""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.agent_profiles import AgentProfileStore
from agent_run.agent_invocation import select_publication_thread
from agent_run.change_currentness import stale_change_job_reason
from agent_run.delivery_loop import TicketDeliveryAdapter
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.run_locator import RunLocatorIndex
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from agent_run.ticket_integration import integration_record_is_valid
from support.accepted_ticket import prepare_accepted_ticket
from support.cli import run_cli, stdout_json
from support.cli_payloads import ticket
from support.seeding import write_fixture
from support.workspace import prepare_workspace


@pytest.mark.parametrize("ticket_number", [2, 3])
def test_accepted_ticket_preserves_current_authority_without_publication(
    git_repo: Path, tmp_path: Path, ticket_number: int,
) -> None:
    issue = {**ticket(), "number": ticket_number, "body": "Keep this exact Ticket contract."}
    fixture = write_fixture(
        git_repo / "github.json", issues={str(ticket_number): issue},
        delivery={"crash_after_ensure_ticket_pr_once": True, "required_checks": ["pending"]},
    )
    workspace = prepare_workspace(git_repo)
    git = workspace.open()
    # 比较外部模型的完整初始事实；Publisher 的空列表等默认值不代表
    # 任何已执行动作。准备仅可增加两个准确 base refs。
    fixture_before = FixtureGitHubPublisher(fixture, git).data
    index_before = (git.root / ".git" / "index").read_bytes()

    state = prepare_accepted_ticket(git_repo, fixture, ticket_number=ticket_number)

    run_id = str(state["run_id"])
    states = StateStore(workspace.state_root)
    assert states.load_current_run(run_id) == state
    assert state["checkout_identity"] == git.checkout_identity()
    job = state["active_ticket_job"]
    assert job == state["ticket_jobs"][str(ticket_number)]
    assert job["phase"] == "accepted"
    assert state["ticket_graph"]["tickets"][str(ticket_number)]["body"] == issue["body"]
    assert job["effective_revision"] == job["acceptance_record"]["effective_revision"]
    assert job["policy_snapshot"] == state["policy_snapshot"]
    candidate, base = job["candidate_sha"], job["base_sha"]
    tree = git.resolve(f"{candidate}^{{tree}}")
    assert git.commit_parents(candidate) == [base]
    assert tree != git.resolve(f"{base}^{{tree}}")
    assert git.resolve(job["ticket_branch"]) == candidate
    assert git.resolve(state["run_branch"]) == base
    assert job["acceptance_record"]["reviewed_candidate_sha"] == candidate
    assert job["acceptance_record"]["reviewed_candidate_tree"] == tree
    assert integration_record_is_valid(git, job)
    publisher = FixtureGitHubPublisher(fixture, git)
    adapter = TicketDeliveryAdapter(git, publisher)
    assert adapter.acceptance_is_current(state, job, job["acceptance_record"])
    assert stale_change_job_reason(state, f"ticket:{ticket_number}", job, git) is None
    changed = deepcopy(state)
    changed["parent"]["revision"] = "changed-parent-contract"
    assert stale_change_job_reason(changed, f"ticket:{ticket_number}", job, git) == (
        "ticket_requirements_changed"
    )
    assert subprocess.run(
        ["git", "show", f"{candidate}:delivered.txt"], cwd=git.root,
        text=True, capture_output=True, check=True,
    ).stdout == "first attempt\n"
    assert (git.root / ".git" / "index").read_bytes() == index_before
    assert not (git.root / "delivered.txt").exists()
    assert not (workspace.state_root / "worktrees" / run_id).exists()
    assert not list(workspace.state_root.glob("ticket-scenario-*"))
    assert job["modification_attempts"] == job["validation_attempts"] == 1
    assert job["review_budget"]["development_attempts"] == 1
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert len(job["review_budget"]["review_artifacts"]) == 1
    assert state["active_agent_invocation"] is None
    assert state["agent_invocation_history"] == []
    assert not job.get("pending_semantic_attempt")
    assert not job.get("semantic_attempt_history")
    assert job.get("publication_attempts", 0) == 0
    for field in ("publication", "publication_sha", "published_sha", "pr_number",
                  "required_checks_evidence", "approval_grant"):
        assert field not in job
    expected_fixture = deepcopy(fixture_before)
    expected_fixture["delivery"]["published_branches"] = {
        state["run_branch"]: base, job["ticket_branch"]: base,
    }
    assert json.loads(fixture.read_text(encoding="utf-8")) == expected_fixture
    fixture_after_base_refs = fixture.read_bytes()
    delivery = json.loads(fixture_after_base_refs)["delivery"]
    assert delivery["crash_after_ensure_ticket_pr_once"] is True
    assert delivery["required_checks"] == ["pending"]
    assert not delivery.get("pull_requests")
    assert delivery["published_branches"] == {
        state["run_branch"]: base, job["ticket_branch"]: base,
    }
    assert candidate not in delivery["published_branches"].values()
    assert not delivery.get("mutations")

    profiles = AgentProfileStore(workspace.state_root)
    profile = profiles.load(run_id)
    assert profile is not None and profile["profile_revision"] == 1
    assert len(profile["bindings"]) == 2
    bindings = {binding["role"]: binding for binding in profile["bindings"]}
    assert bindings["development"]["thread_id"] == job["development_thread_id"]
    assert bindings["review"]["thread_id"] == job["reviewer_thread_ids"][0]
    for role, binding in bindings.items():
        assert binding["profile_revision"] == 1
        assert binding["model"] == profile["profiles"][role]["model"]
        assert binding["reasoning_effort"] == profile["profiles"][role]["reasoning_effort"]
    requested = select_publication_thread(job, max_context_attempts=4)
    assert requested == job["development_thread_id"]
    assert profiles.bind_thread(run_id, role="publication", thread_id=requested) == (
        bindings["development"]
    )
    assert profiles.load(run_id) == profile
    locator = RunLocatorIndex.default()
    assert locator.resolve_state_dir(run_id) == workspace.state_root
    entry, = locator.entries()
    assert entry["repository_root"] == str(git.root)
    assert entry["repository"] == state["repository"]
    assert entry["parent_number"] == 1
    control = TaskControlStore(workspace.state_root)
    task = TaskKey(git.root, str(state["repository"]), 1)
    ownership = control.load(task)
    assert ownership is not None and ownership["run_id"] == run_id
    assert ownership["action"] is ownership["executor"] is None
    assert ownership["action_history"] == []
    locator_before = locator.path.read_bytes()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    queried = run_cli(elsewhere, fixture, "status", run_id, "--json")
    assert queried.returncode == 0, queried.stdout + queried.stderr
    assert stdout_json(queried)["run_id"] == run_id
    assert states.load_current_run(run_id) == state
    assert control.load(task) == ownership
    assert AgentProfileStore(workspace.state_root).load(run_id) == profile
    assert locator.path.read_bytes() == locator_before
    assert fixture.read_bytes() == fixture_after_base_refs
