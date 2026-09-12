"""真实 CLI 两轮合同；修复来源组合由共享 Engine 的轻量测试负责。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_fixtures import run_agents
from conftest import write_fixture
from support.inprocess_cli import invoke_cli_inprocess
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import (
    parent_round_agents,
    passing_acceptance,
    publication,
    repair_acceptance,
    ticket,
)


@pytest.mark.parametrize("scope", ["ticket", "parent_only", "run_repair"])
@pytest.mark.parametrize("policy", ["reuse", "new-per-attempt"])
def test_public_two_development_attempts_preserve_code_budget_and_thread_history(
    git_repo: Path, scope: str, policy: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={} if scope == "parent_only" else {"3": ticket()}
    )
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    first_thread = f"{scope}-development-1"
    second_thread = first_thread if policy == "reuse" else f"{scope}-development-2"
    developments = [
        {
            "expected_thread_id": None,
            "thread_id": first_thread,
            "summary": "Prepared the first candidate.",
            "write_files": {"inherited.txt": "first candidate\n"},
        },
        {
            "expected_thread_id": first_thread if policy == "reuse" else None,
            "thread_id": second_thread,
            "summary": "Repaired the existing candidate.",
            "expected_files": {"inherited.txt": "first candidate\n"},
            "write_files": {"repaired.txt": "second candidate\n"},
        },
    ]
    reviews = [
        repair_acceptance(f"{scope}-reviewer-1"),
        passing_acceptance(f"{scope}-reviewer-2", "The repaired candidate passed."),
    ]
    if scope == "parent_only":
        data = parent_round_agents(2, passing_last=True)
        data["developments"] = developments
        data["reviews"] = reviews
    elif scope == "ticket":
        data["developments"] = developments
        data["reviews"] = reviews
    else:
        data["developments"].extend(developments)
        data["run_reviews"] = [repair_acceptance("initial-run-reviewer"), *reviews]
        data["publications"].append(publication())
    data["publications"][-1].update(
        expected_thread_id=second_thread, thread_id=second_thread
    )
    agents.write_text(json.dumps(data), encoding="utf-8")
    # Omit the default flag deliberately: upgrading must preserve old behaviour.
    options = () if policy == "reuse" else ("--development-thread-policy", policy)
    result = run_cli(
        git_repo, fixture, "run", "1", *options, "--agent-fixture", str(agents)
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    state = load_only_run_state(git_repo)
    assert state["policy_snapshot"]["development_thread_policy"] == policy
    if scope == "ticket":
        job = state["ticket_jobs"]["3"]
    elif scope == "parent_only":
        job = state["parent_job"]
    else:
        job = state["run_acceptance"]
        assert state["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 2
    if scope != "run_repair":
        assert job["review_budget"]["development_attempts"] == 2
        assert job["review_budget"]["reviewer_invocations"] == 2
    history = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "history", state["run_id"], "--json")
    )
    invocations = history["agent_invocations"]
    development = [
        item for item in invocations
        if item["role"] == "development"
        and item.get("reported_thread_id") in {first_thread, second_thread}
    ]
    assert [item["reported_thread_id"] for item in development] == [first_thread, second_thread]
    attempts = [item["semantic_attempt"] for item in development]
    assert len({attempt["attempt_id"] for attempt in attempts}) == 2
    assert [attempt["ordinal"] for attempt in attempts] == [1, 2]
    assert len({attempt["generation"] for attempt in attempts}) == 1
    assert len({attempt["budget_window"] for attempt in attempts}) == 1
    recorded_threads = set(job["development_thread_history"]) | {
        job.get("development_thread_id")
    }
    assert {first_thread, second_thread} <= recorded_threads
    reviewers = [item for item in invocations if item["role"] in {"reviewer", "fresh_acceptance"}]
    assert reviewers
    assert all(item["reported_thread_id"] not in {first_thread, second_thread} for item in reviewers)


def test_rotated_development_uses_frozen_defaults_then_explicit_run_profile(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.agent_fixture import FixtureAgentBackend
    from test_cli_user_defaults import settings

    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "agents.json"
    data = parent_round_agents(3, passing_last=True)
    for index, step in enumerate(data["developments"], start=1):
        step.update(expected_thread_id=None, thread_id=f"developer-{index}")
    data["publications"][-1].update(expected_thread_id="developer-3", thread_id="developer-3")
    agents.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.chdir(git_repo)
    code, _ = settings("configure", "--development-thread-policy", "new-per-attempt",
                       "--development-model", "frozen-model", "--development-effort", "high")
    assert code == 0
    original = FixtureAgentBackend.develop
    calls = 0

    def develop_with_configuration_change(self: FixtureAgentBackend, request: dict):
        nonlocal calls
        result = original(self, request)
        calls += 1
        if calls == 1:
            code, _ = settings("configure", "--development-model", "personal-later",
                               "--development-effort", "low")
            assert code == 0
        elif calls == 2:
            run_id = load_only_run_state(git_repo)["run_id"]
            changed = invoke_cli_inprocess(
                git_repo, fixture, "configure", run_id, "--development-model", "run-revised",
                "--development-effort", "medium", "--json",
            )
            assert changed.returncode == 0, changed.stderr
        return result

    monkeypatch.setattr(FixtureAgentBackend, "develop", develop_with_configuration_change)
    result = invoke_cli_inprocess(git_repo, fixture, "run", "1", "--agent-fixture", str(agents), "--json")
    assert result.returncode == 0, (result.stdout, result.stderr)
    state = load_only_run_state(git_repo)
    developments = [item for item in state["agent_invocation_history"] if item["role"] == "development"]
    assert [item["reported_thread_id"] for item in developments] == ["developer-1", "developer-2", "developer-3"]
    assert [item["model"] for item in developments] == ["frozen-model", "frozen-model", "run-revised"]
    assert [item["reasoning_effort"] for item in developments] == ["high", "high", "medium"]
    assert [item["profile_revision"] for item in developments] == [1, 1, 2]
    assert state["parent_job"]["review_budget"]["development_attempts"] == 3
