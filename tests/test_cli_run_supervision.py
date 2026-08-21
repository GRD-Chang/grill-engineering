from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_fixtures import run_agents
from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import (
    parent_publication,
    passing_acceptance,
    publication,
    ticket,
)

from cli_run_supervision_support import (
    _assert_credential_wait_is_not_public,
    _assert_waiting_external_recovery_action,
    _interrupt_run,
    _parent_only_agents,
    _repair_agents,
    _run_until_pending_window,
)

def test_run_supervises_pending_parent_only_checks_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["pending", "pass"]},
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "parent_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["parent_job"]["phase"] == "ready_for_approval"
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1

@pytest.mark.parametrize(
    ("fixture_role", "invocation_role", "phase"),
    [
        ("reviews", "fresh_acceptance", "reviewing"),
        ("publications", "publication", "publication"),
    ],
)
def test_run_retries_initial_credential_before_starting_each_parent_only_worker(
    git_repo: Path, fixture_role: str, invocation_role: str, phase: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["initial_credential_failures_by_role"] = {
        fixture_role: ["temporary issuer outage"]
    }
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "parent_approval_pending"
    state = load_only_run_state(git_repo)
    assert "credential_availability" not in state
    assert "supervision_window" not in state
    _assert_credential_wait_is_not_public(git_repo, fixture, str(state["run_id"]))
    attempts = [
        invocation
        for invocation in state["agent_invocation_history"]
        if invocation.get("role") == invocation_role and invocation.get("phase") == phase
    ]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "completed"

def test_waiting_status_and_history_expose_a_sanitized_supervision_snapshot(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["pending"]},
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")
    process, state = _run_until_pending_window(git_repo, fixture, agents)
    try:
        run_id = str(state["run_id"])
        status = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
        history = stdout_json(run_cli(git_repo, fixture, "history", run_id, "--json"))

        for output in (status, history):
            wait = output["supervision"]
            assert wait["kind"] == "required_checks"
            assert wait["subject"] == "Parent PR #1 的 GitHub 对账 的 GitHub Required Checks"
            assert wait["head_sha"] is not None
            assert wait["base_sha"] is not None
            assert wait["started_at"] is not None
            assert wait["deadline"] is not None
            assert wait["remaining_seconds"] >= 0
            assert wait["retry_count"] >= 1
            assert "latest_observation" in wait
            assert wait["next_action"] == "agent-run run 1"
            assert wait["timeout_resume_action"] == "agent-run run 1"

        for command in ("status", "history"):
            text = run_cli(git_repo, fixture, command, run_id).stdout
            assert "等待对象: Parent PR #1 的 GitHub 对账 的 GitHub Required Checks" in text
            assert "等待窗口:" in text
            assert "重试次数:" in text
            assert "最新观测: 无" in text
            assert "超时恢复: agent-run run 1" in text
    finally:
        _interrupt_run(process)

def test_parent_only_approval_survives_pending_checks_until_the_same_pr_merges(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["none", "pending", "pass"]},
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")

    awaiting_approval = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert stdout_json(awaiting_approval)["status"] == "parent_approval_pending"
    run_id = str(stdout_json(awaiting_approval)["run_id"])

    waiting = run_cli(git_repo, fixture, "approve", run_id)
    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_checks"
    granted = load_only_run_state(git_repo)["parent_job"]["approval_grant"]
    assert granted["pr_number"] == 1
    assert granted["repository"] == "example/project"

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    assert load_only_run_state(git_repo)["parent_job"]["approval_grant"]["granted_at"] == granted["granted_at"]
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"][0]["state"] == "MERGED"

def test_parent_only_approval_grant_is_revoked_when_parent_revision_changes(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["none", "pending"]},
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    assert stdout_json(run_cli(git_repo, fixture, "approve", run_id))["status"] == "waiting_checks"

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Changed after approval."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    halted = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert halted.returncode == 2
    assert stdout_json(halted)["status"] == "requeue_required"
    state = load_only_run_state(git_repo)
    assert "approval_grant" not in state["parent_job"]
    assert data["delivery"]["pull_requests"][0]["state"] == "OPEN"

def test_parent_only_approval_recovers_a_lost_merge_response_without_reapproval(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["crash_after_normal_merge_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(waiting)["status"] == "waiting_external"
    _assert_waiting_external_recovery_action(git_repo, fixture, run_id)
    granted_at = load_only_run_state(git_repo)["parent_job"]["approval_grant"]["granted_at"]
    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    assert load_only_run_state(git_repo)["parent_job"]["approval_grant"]["granted_at"] == granted_at

def test_run_routes_pending_parent_only_check_failure_through_repair(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={
            "required_checks": ["pending", "fail", "pass"],
            "required_check_evidence": {
                "pr_number": 1,
                "checks": [
                    {
                        "name": "cancelled",
                        "workflow": "ci",
                        "bucket": "cancel",
                        "state": "CANCELLED",
                        "description": "The Parent-only check was cancelled.",
                        "link": "https://example.invalid/checks/cancelled",
                    }
                ],
            },
        },
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": "parent-developer-1",
            "thread_id": "parent-developer-1",
            "summary": "Repaired the failed Parent-only check.",
            "write_files": {"parent-feature.txt": "repaired\n"},
        }
    )
    data["publications"].append(parent_publication())
    data["reviews"].append(
        passing_acceptance("parent-reviewer-2", "The Parent-only repair passed.")
    )
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "parent_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["parent_job"]["modification_attempts"] == 2
    assert state["parent_job"]["phase"] == "ready_for_approval"

def test_run_routes_pending_final_run_check_failure_through_repair(
    git_repo: Path,
) -> None:
    code_failure = {
        "name": "quality",
        "workflow": "CI",
        "bucket": "fail",
        "state": "FAILURE",
        "description": "The configured test step failed.",
        "link": "https://example.invalid/checks/quality",
        "job": {
            "head_sha": "$CURRENT_HEAD",
            "name": "quality",
            "workflow_name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "steps": [
                {
                    "name": "Run tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "number": 6,
                }
            ],
        },
    }
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "pending", "fail", "pass", "pass"],
            "required_check_evidence": {"pr_number": 1, "checks": [code_failure]},
        },
    )
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "run-repair-developer-1",
            "summary": "Repaired the failed final Run check.",
            "write_files": {"run-repair.txt": "repaired\n"},
        }
    )
    data["publications"].append(publication())
    data["run_reviews"].append(
        passing_acceptance("run-reviewer-2", "The repaired Run passed.")
    )
    data["run_publications"].append(data["run_publications"][0])
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["run_acceptance"]["modification_attempts"] == 1
    assert state["run_publication"]["phase"] == "ready_for_approval"
    history = state["agent_invocation_history"]
    assert len(
        [
            item
            for item in history
            if item.get("work_subject") == f"run-repair:{state['run_id']}"
            and item.get("role") == "fresh_acceptance"
        ]
    ) == 1
    assert len(
        [
            item
            for item in history
            if item.get("work_subject") == f"run-acceptance:{state['run_id']}"
            and item.get("role") == "reviewer"
        ]
    ) == 1
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    final_prs = [
        pull for pull in delivery["pull_requests"] if pull.get("scope") == "final_run"
    ]
    assert len(final_prs) == 1
    assert final_prs[0]["state"] == "OPEN"
    assert "Parent Issue: #1" in final_prs[0]["body"]
    final_statuses = [
        status
        for status in delivery["agent_run_status"]
        if status.get("pr_number") == final_prs[0]["number"]
    ]
    assert final_statuses[-1]["required_checks"] == "pass"

@pytest.mark.parametrize(
    "check",
    [
        {
            "name": "cancelled",
            "workflow": "ci",
            "bucket": "cancel",
            "state": "CANCELLED",
            "description": "cancelled",
            "link": "https://example.invalid/checks/cancelled",
        },
        {
            "name": "quality",
            "workflow": "CI",
            "bucket": "fail",
            "state": "FAILURE",
            "description": "The configured job failed.",
            "link": "https://example.invalid/checks/quality",
            "job": {
                "head_sha": "$CURRENT_HEAD",
                "name": "quality",
                "workflow_name": "CI",
                "status": "completed",
                "conclusion": "failure",
                "steps": [
                    {
                        "name": "Install system dependencies",
                        "status": "completed",
                        "conclusion": "failure",
                        "number": 3,
                    },
                    {
                        "name": "Run tests",
                        "status": "completed",
                        "conclusion": "skipped",
                        "number": 6,
                    },
                ],
            },
        },
        {
            "name": "unknown",
            "workflow": "ci",
            "bucket": "fail",
            "description": "missing conclusion",
            "link": "https://example.invalid/checks/unknown",
        },
    ],
    ids=("cancelled", "platform", "unknown"),
)
def test_public_run_supervises_non_repairable_final_check_failure(
    git_repo: Path, check: dict[str, Any]
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "fail"],
            "required_check_evidence": {"pr_number": 1, "checks": [check]},
        },
    )
    agents = run_agents(git_repo / "agents.json")

    waiting = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert waiting.returncode == 2, waiting.stderr
    assert stdout_json(waiting)["status"] == "supervision_timeout"
    state = load_only_run_state(git_repo)
    assert state["run_acceptance"]["modification_attempts"] == 0
    assert "repair_request" not in state["run_acceptance"]
    assert "repair_job" not in state["run_acceptance"]
    assert state["run_acceptance"].get("repair_generation", 0) == 0
    assert state["run_acceptance"].get("candidate_acceptance_history", []) == []
    assert not any(
        invocation.get("work_subject") == f"run-repair:{state['run_id']}"
        for invocation in state["agent_invocation_history"]
    )
    assert state["run_publication"]["phase"] == "waiting_external"
    status = stdout_json(
        run_cli(git_repo, fixture, "status", str(state["run_id"]), "--json")
    )
    assert status["supervision"]["kind"] == "github_convergence"
    assert status["supervision"]["timeout_resume_action"] == (
        f"agent-run resume {state['run_id']}"
    )

def test_run_resumes_repair_promotion_after_merged_pr_readback_lags(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "pending", "fail", "pass", "pass"],
            "merged_live_pull_request_state_overrides": {"run_repair": ["OPEN"]},
        },
    )
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "run-repair-developer-1",
            "summary": "Repaired the failed final Run check.",
            "write_files": {"run-repair.txt": "repaired\n"},
        }
    )
    data["publications"].append(publication())
    data["run_reviews"].append(
        passing_acceptance("run-reviewer-2", "The repaired Run passed.")
    )
    data["run_publications"].append(data["run_publications"][0])
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    run = state["run_acceptance"]
    assert run["repair_cycle"]["status"] == "promoted"
    assert run["repair_cycle"]["generation"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert state["run_publication"]["phase"] == "ready_for_approval"
    assert len(
        [
            item
            for item in state["agent_invocation_history"]
            if item.get("work_subject") == f"run-repair:{state['run_id']}"
            and item.get("role") == "fresh_acceptance"
        ]
    ) == 1
    assert len(
        [
            item
            for item in state["agent_invocation_history"]
            if item.get("work_subject") == f"run-acceptance:{state['run_id']}"
            and item.get("role") == "reviewer"
        ]
    ) == 1
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert fixture_data["delivery"][
        "merged_live_pull_request_state_overrides"
    ]["run_repair"] == []

@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_repair_promotion_live_pr_readback(
    git_repo: Path, error_type: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "pending", "fail", "pass", "pass"],
            "merged_live_pull_request_failures": [
                {
                    "scope": "run_repair",
                    "type": error_type,
                    "code": "github_read_failed",
                    "message": "promotion readback unavailable",
                }
                for _ in range(3)
            ],
        },
        supervision_clock_multiplier=120,
    )
    agents = _repair_agents(git_repo / "agents.json", repair_generations=1)

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    job = run["repair_job"]
    checkout = Path(str(job["repair_checkout"]))
    candidate_sha = job["candidate_sha"]
    thread_id = job["development_thread_id"]
    assert paused_state["status"] == "supervision_timeout"
    assert run["repair_generation"] == 1
    assert run["repair_cycle"]["status"] == "active"
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert isinstance(job.get("integrated_sha"), str)
    assert checkout.exists()

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"]["merged_live_pull_request_failures"] = []
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    completed_run = completed["run_acceptance"]
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed_run["repair_generation"] == 1
    assert completed_run["repair_cycle"]["status"] == "promoted"
    assert completed_run["repair_cycle"]["code_modification_attempts"] == 1
    assert completed_run["candidate_sha"] == candidate_sha
    assert completed_run["development_thread_history"] == [thread_id]
    assert not checkout.exists()

def test_run_bounds_persistent_unknown_repair_checks_without_new_candidate(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "fail", "unknown"]},
        supervision_clock_multiplier=120,
    )
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "run-repair-developer-1",
            "summary": "Repaired the failed final Run check.",
            "write_files": {"run-repair.txt": "repaired\n"},
        }
    )
    data["publications"].append(publication())
    data["run_reviews"].append(
        passing_acceptance("run-reviewer-2", "The repaired Run passed.")
    )
    data["run_publications"].append(data["run_publications"][0])
    agents.write_text(json.dumps(data), encoding="utf-8")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    job = run["repair_job"]
    wait = paused_state["supervision_wait"]
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert paused_state["status"] == "supervision_timeout"
    assert wait["kind"] == "github_convergence"
    assert wait["retry_count"] >= 2
    assert wait["elapsed_seconds"] >= wait["budget_seconds"] == 10 * 60
    assert fixture_data["supervision_clock"] >= 10 * 60
    assert fixture_data["delivery"]["check_position"] <= 5
    assert job["phase"] == "waiting_checks"
    assert job["modification_attempts"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    candidate_reviews = [
        invocation
        for invocation in paused_state["agent_invocation_history"]
        if invocation.get("work_subject") == f"run-repair:{paused_state['run_id']}"
        and invocation.get("role") == "fresh_acceptance"
    ]
    assert len(candidate_reviews) == 1

    fixture_data["delivery"].update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovered = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    completed = load_only_run_state(git_repo)
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1
    completed_candidate_reviews = [
        invocation
        for invocation in completed["agent_invocation_history"]
        if invocation.get("work_subject") == f"run-repair:{completed['run_id']}"
        and invocation.get("role") == "fresh_acceptance"
    ]
    assert len(completed_candidate_reviews) == 1
