"""The approval update uses the same bounded CI repair as Run publication."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.ci_recovery_budget import mark_ci_repair, observe_repairable_ci_failure
from agent_run.state import StateStore
from support.cli import load_only_run_state, run_cli, stdout_json
from support.cli_payloads import passing_acceptance
from support.cli_supervision import _resume_repair_agents
from support.parent_approval_update import append_remote
from support.published_run import prepare_published_run
from support.run_approval_update import update_agents
from support.workspace import managed_state


@pytest.mark.parametrize("exhausted", [False, True], ids=["repair", "budget-checkpoint"])
def test_public_run_approval_update_routes_ci_and_preserves_other_budgets(
    git_repo: Path, exhausted: bool,
) -> None:
    fixture, before = prepare_published_run(git_repo)
    target = append_remote(git_repo, fixture, "main", "target.txt", "default update\n")
    if exhausted:
        # Prior CI windows are setup: actual checkpoint admission still goes
        # through public approve after real Git integration and fresh review.
        run = before["run_acceptance"]
        for index in range(3):
            observe_repairable_ci_failure(
                run, head_sha=f"previous-ci-head-{index}", limit=3, is_ticket=False,
            )
            mark_ci_repair(run, limit=3, is_ticket=False)
        StateStore(managed_state(git_repo)).save_run(before["run_id"], before)
        agents = update_agents(git_repo)
    else:
        agents = _resume_repair_agents(git_repo / "ci-update-agents.json", expected_thread_id=None)
        script = json.loads(agents.read_text())
        script["run_reviews"].insert(0, passing_acceptance("updated-run-reviewer", "Updated Run passed"))
        script["run_publications"].append(script["run_publications"][0])
        agents.write_text(json.dumps(script))
    data = json.loads(fixture.read_text())
    data["delivery"].update(required_checks=["fail", "pass", "pass"], check_position=0)
    fixture.write_text(json.dumps(data))

    updated = run_cli(git_repo, fixture, "approve", before["run_id"], "--agent-fixture", str(agents))

    state = load_only_run_state(git_repo)
    run = state["run_acceptance"]
    assert state["run_publication"]["pr_number"] == before["run_publication"]["pr_number"]
    assert state["run_publication"].get("approval_grant") is None
    assert state["ticket_jobs"] == before["ticket_jobs"]
    previous_budget = before["run_acceptance"]["review_budget"]
    assert run["review_budget"]["development_attempts"] == previous_budget["development_attempts"]
    assert run["review_budget"]["reviewer_invocations"] == previous_budget["reviewer_invocations"] + 1
    assert run["review_budget"]["window"] == previous_budget["window"]
    assert run["review_budget_history"] == before["run_acceptance"]["review_budget_history"]
    assert run["integration_resync"]["used"] == 1
    delivery = json.loads(fixture.read_text())["delivery"]
    final_pulls = [pull for pull in delivery["pull_requests"] if pull.get("scope") == "final_run"]
    repair_pulls = [pull for pull in delivery["pull_requests"] if pull.get("scope") == "run_repair"]
    assert len(final_pulls) == 1
    assert final_pulls[0]["state"] == "OPEN"
    assert 1 not in delivery["closed_issues"]
    if exhausted:
        assert updated.returncode == 2, updated.stdout + updated.stderr
        assert state["status"] == "ready_for_human"
        assert run["blocked_reason"] == "ci_development_budget_exhausted"
        previous = before["run_acceptance"]["ci_recovery"]
        assert run["ci_recovery"]["history"][:-1] == previous["history"]
        assert run["ci_recovery"]["history"][-1]["kind"] == "failure"
        assert run["ci_recovery"]["history"][-1]["developments_used"] == 3
        assert run["ci_recovery"]["window"] == 1
        assert run["ci_recovery"]["developments_used"] == 3
        assert not repair_pulls
        assert "repair_job" not in run
        assert "repair_request" not in run
    else:
        assert updated.returncode == 0, updated.stdout + updated.stderr
        assert stdout_json(updated)["action"]["status"] == "updating"
        assert state["status"] == "run_approval_pending"
        assert run["ci_recovery"]["window"] == 1
        assert run["ci_recovery"]["developments_used"] == 1
        assert run["ci_recovery"]["reviewer_invocations"] == 1
        assert run["repair_cycle"]["status"] == "promoted"
        assert run["acceptance_record"]["reviewed_default_base_sha"] == target
        assert run["acceptance_record"]["acceptance_state"] == "integrated"
        assert len(run["candidate_acceptance_history"]) == 1
        assert len(repair_pulls) == 1
        assert repair_pulls[0]["state"] == "MERGED"
        assert state["run_publication"]["required_checks_evidence"]["result"] == "pass"
