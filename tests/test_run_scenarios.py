"""场景前置本身保留真实身份、持久化与 currentness 的保障。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_currentness import refresh_run_currentness, ticket_completion_records
from support.repairing_run import prepare_run_repair
from support.run_scenarios import _accepted_run, _completed_run


@pytest.mark.parametrize("ticket_number", [2, 3])
def test_completed_ticket_scenario_has_real_integration_identity(
    git_repo: Path, ticket_number: int,
) -> None:
    state, states, git = _completed_run(git_repo, ticket_number=ticket_number)
    loaded = states.load_current_run(str(state["run_id"]))
    assert loaded == state
    job = loaded["ticket_jobs"][str(ticket_number)]
    record = job["deterministic_integration_record"]
    integrated = git.resolve(str(loaded["run_branch"]))
    assert job["integrated_sha"] == record["integrated_sha"] == integrated
    assert integrated != loaded["base"]["sha"]
    assert record["integrated_parents"] == git.commit_parents(integrated) == [record["base_sha"]]
    assert record["integrated_tree"] == git.resolve(f"{integrated}^{{tree}}")
    assert record["pr"]["merge_commit_sha"] == integrated
    assert record["acceptance_record"]["reviewed_candidate_sha"] == integrated
    publisher = FixtureGitHubPublisher(git_repo / "github.json", git)
    assert publisher.data["delivery"]["published_branches"][state["run_branch"]] == integrated
    before = ticket_completion_records(loaded)
    assert refresh_run_currentness(
        loaded, reader=FixtureGitHubReader(git_repo / "github.json"), git=git,
    ) == git.resolve("main")
    assert ticket_completion_records(loaded) == before


def test_accepted_run_scenario_is_persisted_and_repair_setup_is_isolated(git_repo: Path) -> None:
    state, states, git, _publisher = _accepted_run(git_repo)
    loaded = states.load_current_run(str(state["run_id"]))
    assert loaded == state
    assert loaded["run_acceptance"]["phase"] == "accepted"
    accepted = deepcopy(loaded)
    request = {"repair_source": "acceptance"}
    prepare_run_repair(loaded, states, repair_request=request)
    request["repair_source"] = "foreign-source"
    repair = states.load_current_run(str(state["run_id"]))
    assert repair["run_acceptance"]["phase"] == "repairing"
    assert repair["status"] == "run_acceptance_pending"
    assert repair["terminal_kind"] == "run_repair_pending"
    assert repair["diagnostics"] == []
    assert repair["run_acceptance"]["repair_request"] == {"repair_source": "acceptance"}
    assert state == accepted
    assert repair["run_acceptance"]["policy_snapshot"] == accepted["policy_snapshot"]
    repair["run_acceptance"]["policy_snapshot"]["test_only"] = True
    assert "test_only" not in repair["policy_snapshot"]
    assert git.resolve(str(state["run_branch"])) == state["ticket_jobs"]["2"]["integrated_sha"]
