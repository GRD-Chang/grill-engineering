from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli_fixtures import run_agents
from conftest import write_fixture
from test_cli import PROJECT_ROOT, load_only_run_state, run_cli, stdout_json
from test_cli_delivery import (
    parent_publication,
    passing_acceptance,
    publication,
    ticket,
)


_WAIT_FIELDS = {
    "kind",
    "subject",
    "head_sha",
    "base_sha",
    "started_at",
    "deadline",
    "remaining_seconds",
    "retry_count",
    "latest_observation",
    "next_action",
    "timeout_resume_action",
}


def _assert_public_wait_projection(
    repo: Path, fixture: Path, run_id: str, *, secret: str | None = None
) -> None:
    for command in ("status", "history"):
        json_result = run_cli(repo, fixture, command, run_id, "--json")
        text_result = run_cli(repo, fixture, command, run_id)

        assert json_result.returncode == text_result.returncode == 0
        wait = stdout_json(json_result)["supervision"]
        assert isinstance(wait, dict)
        assert _WAIT_FIELDS <= wait.keys()
        assert wait["kind"] in {"github_convergence", "required_checks"}
        assert wait["started_at"] is not None
        assert wait["deadline"] is not None
        assert wait["timeout_resume_action"] == "agent-run run 1"
        for label in (
            "等待种类:",
            "等待对象:",
            "等待 head/base:",
            "等待窗口:",
            "重试次数:",
            "最新观测:",
            "超时恢复: agent-run run 1",
        ):
            assert label in text_result.stdout
        if secret is not None:
            assert secret not in json_result.stdout
            assert secret not in text_result.stdout


def _assert_waiting_external_recovery_action(
    repo: Path, fixture: Path, run_id: str
) -> None:
    expected_action = "agent-run run 1"
    for command in ("status", "history"):
        json_result = run_cli(repo, fixture, command, run_id, "--json")
        text_result = run_cli(repo, fixture, command, run_id)

        assert json_result.returncode == text_result.returncode == 0
        assert stdout_json(json_result)["next_action"] == expected_action
        assert f"下一步: {expected_action}" in text_result.stdout


def _assert_credential_wait_is_not_public(
    repo: Path, fixture: Path, run_id: str
) -> None:
    for command in ("status", "history"):
        output = stdout_json(run_cli(repo, fixture, command, run_id, "--json"))
        assert output.get("supervision") is None


def _parent_only_agents(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Delivered the Parent-only request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance(
                        "parent-reviewer-1", "The Parent-only flow passed."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _run_until_pending_window(
    repo: Path,
    fixture: Path,
    agents: Path,
    *,
    wait_for_retry_message: bool = False,
) -> tuple[subprocess.Popen[str], dict[str, object]]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    environment.setdefault("XDG_STATE_HOME", str(repo / ".agent-run-test-state"))
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_run",
            "run",
            "1",
            "--agent-fixture",
            str(agents),
            "--github-fixture",
            str(fixture),
        ],
        cwd=repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    timeout = time.monotonic() + (8 if wait_for_retry_message else 3)
    try:
        while time.monotonic() < timeout:
            try:
                state = load_only_run_state(repo)
            except (AssertionError, FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.01)
                continue
            window = state.get("supervision_window")
            if state.get("status") == "waiting_checks" and isinstance(window, dict):
                if not wait_for_retry_message:
                    return process, state
                assert process.stderr is not None
                ready, _, _ = select.select([process.stderr], [], [], 0)
                if ready and "等待进度: kind=required_checks" in process.stderr.readline():
                    persisted = load_only_run_state(repo)
                    persisted_window = persisted.get("supervision_window")
                    if (
                        persisted.get("status") == "waiting_checks"
                        and isinstance(persisted_window, dict)
                    ):
                        return process, persisted
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    "agent-run run stopped before persisting its pending window: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
    except BaseException:
        _interrupt_run(process)
        raise
    _interrupt_run(process)
    pytest.fail("agent-run run did not reach its pending-window barrier before timeout")


def _interrupt_run(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=3)
    assert process.returncode is not None


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
        delivery={"required_checks": ["pending", "fail", "pass"]},
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


def _repair_agents(path: Path, *, repair_generations: int) -> Path:
    agents = run_agents(path)
    data = json.loads(agents.read_text(encoding="utf-8"))
    for generation in range(1, repair_generations + 1):
        data["developments"].append(
            {
                "expected_thread_id": (
                    None if generation == 1 else "run-repair-developer-1"
                ),
                "thread_id": "run-repair-developer-1",
                "summary": f"Repaired final Run check generation {generation}.",
                "write_files": {"run-repair.txt": f"repair-{generation}\n"},
            }
        )
        data["publications"].append(publication())
        data["run_reviews"].append(
            passing_acceptance(
                f"run-reviewer-{generation + 1}",
                f"Run repair generation {generation} passed.",
            )
        )
    data["run_publications"].append(data["run_publications"][0])
    agents.write_text(json.dumps(data), encoding="utf-8")
    return agents


def _resume_repair_agents(path: Path, *, expected_thread_id: str | None) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": expected_thread_id,
                        "thread_id": "run-repair-developer-1",
                        "summary": "Resumed the supervised Run repair.",
                        "write_files": {"run-repair.txt": "recovered\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [],
                "run_reviews": [
                    passing_acceptance(
                        "run-reviewer-resumed", "The resumed Run repair passed."
                    )
                ],
                "run_publications": [
                    {
                        "commit_message": "fix(run): publish supervised repair",
                        "pr_title": "fix(run): publish supervised repair",
                        "pr_body_markdown": (
                            "## What Problem This Solves\n\nThe repair was externally blocked.\n\n"
                            "## Why This Change Was Made\n\nThe same repair resumed after evidence converged.\n\n"
                            "## User Impact\n\nThe final Run remains reviewable.\n\n"
                            "## Evidence\n\nThe public CLI recovery passed."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_run_repair_resume_preserves_partial_worker_edits_and_thread(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending", "fail", "pass", "pass"]},
    )
    agents = run_agents(git_repo / "agents.json")
    data = json.loads(agents.read_text(encoding="utf-8"))
    data["developments"].append(
        {
            "expected_thread_id": None,
            "thread_id": "run-repair-developer-1",
            "summary": "unused after the injected failure",
            "write_files": {"partial-repair.txt": "preserve this edit\n"},
            "error_after_writes": "simulated Run Repair Worker failure",
        }
    )
    agents.write_text(json.dumps(data), encoding="utf-8")

    interrupted = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert interrupted.returncode == 2
    failed = load_only_run_state(git_repo)
    run = failed["run_acceptance"]
    job = run["repair_job"]
    checkout = Path(str(job["repair_checkout"]))
    generation = run["repair_generation"]
    assert failed["status"] == "execution_failed"
    assert run["repair_cycle"]["status"] == "active"
    assert job["phase"] == "developing"
    assert job["pending_attempt"] == 1
    assert checkout.exists()
    assert (checkout / "partial-repair.txt").read_text(encoding="utf-8") == (
        "preserve this edit\n"
    )

    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json",
        expected_thread_id="run-repair-developer-1",
    )
    recovery_data = json.loads(recovery_agents.read_text(encoding="utf-8"))
    recovery_data["developments"][0]["expected_files"] = {
        "partial-repair.txt": "preserve this edit\n"
    }
    recovery_agents.write_text(json.dumps(recovery_data), encoding="utf-8")
    recovered = run_cli(
        git_repo,
        fixture,
        "resume",
        str(failed["run_id"]),
        "--agent-fixture",
        str(recovery_agents),
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    completed_run = completed["run_acceptance"]
    assert stdout_json(recovered)["status"] == "run_publication_pending"
    assert completed_run["repair_generation"] == generation
    assert completed_run["repair_cycle"]["status"] == "promoted"
    assert completed_run["repair_cycle"]["code_modification_attempts"] == 1
    assert completed_run["development_thread_history"] == ["run-repair-developer-1"]
    assert not checkout.exists()


@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_repair_trigger_live_pr_readback(
    git_repo: Path, error_type: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    initial_agents = run_agents(git_repo / "initial-agents.json")
    awaiting = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(initial_agents)
    )
    run_id = str(stdout_json(awaiting)["run_id"])
    assert stdout_json(awaiting)["status"] == "run_approval_pending"

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {"required_checks": ["fail"], "check_position": 0}
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    queued = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(queued)["status"] == "run_acceptance_pending"

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock_multiplier"] = 120
    fixture_data["delivery"]["open_live_pull_request_failures"] = [
        {
            "scope": "final_run",
            "type": error_type,
            "code": "github_read_failed",
            "message": "repair trigger readback unavailable",
        }
        for _ in range(3)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json", expected_thread_id=None
    )

    paused = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    job = run["repair_job"]
    assert paused_state["status"] == "supervision_timeout"
    assert run["repair_generation"] == 1
    assert run["repair_cycle"]["status"] == "active"
    assert run["repair_cycle"]["code_modification_attempts"] == 0
    assert job["repair_trigger_pending"] is True
    assert job["development_thread_id"] is None
    assert "candidate_sha" not in job

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {
            "open_live_pull_request_failures": [],
            "required_checks": ["pass", "pass"],
            "check_position": 0,
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    completed_run = completed["run_acceptance"]
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed_run["repair_generation"] == 1
    assert completed_run["repair_cycle"]["status"] == "promoted"
    assert completed_run["repair_cycle"]["code_modification_attempts"] == 1


@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_repair_pr_failed_check_evidence_reads(
    git_repo: Path, error_type: str
) -> None:
    failures = [
        {"scope": "run_repair", "type": error_type, "message": "evidence unavailable"},
        {"scope": "run_repair", "type": error_type, "message": "evidence unavailable"},
        {"scope": "run_repair", "type": error_type, "message": "evidence unavailable"},
    ]
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "fail", "fail"],
            "required_check_evidence_failures": failures,
        },
        supervision_clock_multiplier=120,
    )
    agents = _repair_agents(git_repo / "agents.json", repair_generations=2)

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    run = paused_state["run_acceptance"]
    job = run["repair_job"]
    checkout = Path(str(job["repair_checkout"]))
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["retry_count"] >= 2
    assert job["phase"] == "publishing"
    assert job["modification_attempts"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 1
    assert job["development_thread_id"] == "run-repair-developer-1"
    assert checkout.exists()

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {"required_checks": ["fail", "pass", "pass"], "check_position": 0}
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json",
        expected_thread_id="run-repair-developer-1",
    )
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 2
    repair_developments = [
        invocation
        for invocation in completed["agent_invocation_history"]
        if invocation.get("work_subject") == f"run-repair:{completed['run_id']}"
        and invocation.get("role") == "development"
    ]
    assert [item["reported_thread_id"] for item in repair_developments] == [
        "run-repair-developer-1",
        "run-repair-developer-1",
    ]


@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_required_check_trigger_fingerprint_reads(
    git_repo: Path, error_type: str
) -> None:
    failures = [
        {
            "scope": "final_run",
            "type": error_type,
            "message": "fingerprint unavailable",
            "remaining_successes": 1,
        },
        {"scope": "final_run", "type": error_type, "message": "fingerprint unavailable"},
        {"scope": "final_run", "type": error_type, "message": "fingerprint unavailable"},
    ]
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "fail"],
            "required_check_evidence_failures": failures,
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
    assert paused_state["status"] == "supervision_timeout"
    assert job["phase"] == "developing"
    assert job["modification_attempts"] == 0
    assert run["repair_generation"] == 1
    assert run["repair_cycle"]["code_modification_attempts"] == 0
    assert job["development_thread_id"] is None
    assert checkout.exists()

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json", expected_thread_id=None
    )
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1


@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_initial_final_check_evidence_reads(
    git_repo: Path, error_type: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["none", "fail"],
            "required_check_evidence_failures": [
                {
                    "scope": "final_run",
                    "type": error_type,
                    "message": "final evidence unavailable",
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
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "github_convergence"
    assert run.get("repair_generation", 0) == 0
    assert "repair_job" not in run
    assert not any(
        invocation.get("work_subject") == f"run-repair:{paused_state['run_id']}"
        for invocation in paused_state["agent_invocation_history"]
    )

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {
            "required_check_evidence_failures": [],
            "required_checks": ["fail", "pass", "pass"],
            "check_position": 0,
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json", expected_thread_id=None
    )
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1


@pytest.mark.parametrize("error_type", ["github", "os", "timeout"])
def test_run_supervises_post_approval_final_check_evidence_reads(
    git_repo: Path, error_type: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = _repair_agents(git_repo / "agents.json", repair_generations=1)
    awaiting = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(awaiting)["run_id"])
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock_multiplier"] = 120
    fixture_data["delivery"].update(
        {
            "required_checks": ["fail"],
            "check_position": 0,
            "required_check_evidence_failures": [
                {
                    "scope": "final_run",
                    "type": error_type,
                    "message": "approved evidence unavailable",
                }
                for _ in range(4)
            ],
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "approve", run_id)
    assert waiting.returncode == 0
    assert stdout_json(waiting)["status"] == "waiting_external"
    waiting_state = load_only_run_state(git_repo)
    grant = waiting_state["run_publication"]["approval_grant"]
    assert waiting_state["run_acceptance"].get("repair_generation", 0) == 0
    assert not any(
        invocation.get("work_subject") == f"run-repair:{run_id}"
        for invocation in waiting_state["agent_invocation_history"]
    )

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "github_convergence"
    assert paused_state["run_publication"]["approval_grant"] == grant
    assert paused_state["run_acceptance"].get("repair_generation", 0) == 0

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"].update(
        {
            "required_check_evidence_failures": [],
            "required_checks": ["fail", "pass", "pass"],
            "check_position": 0,
        }
    )
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    recovery_agents = _resume_repair_agents(
        git_repo / "recovery-agents.json", expected_thread_id=None
    )
    recovered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(recovery_agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    completed = load_only_run_state(git_repo)
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert completed["run_acceptance"]["repair_generation"] == 1
    assert completed["run_acceptance"]["repair_cycle"]["code_modification_attempts"] == 1


@pytest.mark.parametrize(
    ("fixture_role", "invocation_role", "phase"),
    [
        ("run_reviews:run_repair", "fresh_acceptance", "reviewing"),
        ("publications:run", "publication", "publication"),
    ],
)
def test_run_retries_initial_credential_before_starting_each_run_repair_worker(
    git_repo: Path, fixture_role: str, invocation_role: str, phase: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending", "fail", "pass", "pass"]},
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
    data["run_reviews"].extend(
        [
            passing_acceptance("run-repair-reviewer-1", "The Run repair passed."),
            passing_acceptance("run-reviewer-2", "The repaired Run passed."),
            passing_acceptance("run-reviewer-3", "The refreshed Run passed."),
        ]
    )
    data["run_publications"].append(data["run_publications"][0])
    data["initial_credential_failures_by_role"] = {
        fixture_role: ["temporary issuer outage"]
    }
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert "credential_availability" not in state
    assert "supervision_window" not in state
    _assert_credential_wait_is_not_public(git_repo, fixture, str(state["run_id"]))
    attempts = [
        invocation
        for invocation in state["agent_invocation_history"]
        if invocation.get("role") == invocation_role
        and invocation.get("phase") == phase
        and invocation.get("work_subject") == f"run-repair:{state['run_id']}"
    ]
    assert len(attempts) == 1
    assert attempts[0]["status"] == "completed"


def test_run_supervises_pending_final_run_checks_in_one_call(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending", "pass"]},
    )
    agents = run_agents(git_repo / "agents.json")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["phase"] == "ready_for_approval"
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2


def test_final_run_approval_survives_pending_checks_until_the_same_pr_merges(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "none", "pending", "pass"]},
    )
    agents = run_agents(git_repo / "agents.json")

    awaiting_approval = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    assert stdout_json(awaiting_approval)["status"] == "run_approval_pending"
    run_id = str(stdout_json(awaiting_approval)["run_id"])

    waiting = run_cli(git_repo, fixture, "approve", run_id)
    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_checks"
    granted = load_only_run_state(git_repo)["run_publication"]["approval_grant"]
    assert granted["pr_number"] == 2
    assert granted["repository"] == "example/project"

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    assert load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"] == granted["granted_at"]
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"][1]["state"] == "MERGED"


def test_final_approval_supervises_a_transient_required_checks_read_failure(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")

    awaiting_approval = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    run_id = str(stdout_json(awaiting_approval)["run_id"])
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"]["run_required_checks_read_failures"] = [
        {"code": "github_timeout", "message": "required checks unavailable"}
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "approve", run_id)

    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_checks"
    persisted_wait = load_only_run_state(git_repo)
    assert persisted_wait["diagnostics"][0]["code"] == "github_timeout"
    assert persisted_wait["supervision_window"]["kind"] == "required_checks"
    granted_at = persisted_wait["run_publication"]["approval_grant"]["granted_at"]
    assert len(fixture_data["delivery"]["pull_requests"]) == 2

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    final_state = load_only_run_state(git_repo)
    assert final_state["run_publication"]["approval_grant"]["granted_at"] == granted_at
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2


def test_final_approval_required_checks_read_timeout_preserves_its_grant_for_resume(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    awaiting_approval = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )
    run_id = str(stdout_json(awaiting_approval)["run_id"])
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock_multiplier"] = 540
    fixture_data["delivery"]["run_required_checks_read_failures"] = [
        {"code": "github_timeout", "message": "required checks unavailable"}
        for _ in range(4)
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    assert stdout_json(run_cli(git_repo, fixture, "approve", run_id))["status"] == "waiting_checks"
    grant = load_only_run_state(git_repo)["run_publication"]["approval_grant"]
    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "required_checks"
    assert (
        paused_state["supervision_wait"]["deadline"]
        - paused_state["supervision_wait"]["started_at"]
        == 45 * 60
    )
    assert paused_state["run_publication"]["approval_grant"] == grant
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["delivery"]["run_required_checks_read_failures"] = []
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    resumed = run_cli(git_repo, fixture, "resume", run_id, "--agent-fixture", str(agents))

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "completed"
    assert load_only_run_state(git_repo)["run_publication"]["approval_grant"] == grant
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2






def test_final_run_approval_recovers_a_lost_merge_response_without_reapproval(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["crash_after_normal_merge_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(waiting)["status"] == "waiting_external"
    _assert_waiting_external_recovery_action(git_repo, fixture, run_id)
    granted_at = load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"]
    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    assert load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"] == granted_at


def test_final_run_supervises_a_recovery_read_failure_without_reapproval(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"].update(
        {
            "crash_after_normal_merge_once": True,
            "merged_live_pull_request_failures": [
                {"code": "github_read_failed", "message": "readback unavailable"}
            ],
        }
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(waiting)["status"] == "waiting_external"
    granted_at = load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"]
    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    assert load_only_run_state(git_repo)["run_publication"]["approval_grant"]["granted_at"] == granted_at




def test_final_run_recovers_a_lost_parent_closeout_response(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["crash_after_parent_close_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")

    interrupted = run_cli(git_repo, fixture, "approve", run_id)
    assert interrupted.returncode == 2
    assert load_only_run_state(git_repo)["status"] == "parent_closeout_pending"
    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "completed"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["closed_issues"].count(1) == 1


def test_run_recovers_parent_only_check_timeout_without_duplicate_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["pending"]},
        supervision_clock_multiplier=540,
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["supervision_wait"]["kind"] == "required_checks"
    window = paused_state["supervision_window"]
    assert window["deadline"] - window["started_at"] == 45 * 60
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 1

    fixture_data["delivery"].update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    recovered = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "parent_approval_pending"
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1


def test_start_repository_read_wait_is_immediately_observable_without_run(
    git_repo: Path,
) -> None:
    secret = "ghp_start_wait_secret"
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        repository_read_failures=[
            {
                "code": "github_timeout",
                "message": f"repository read failed with token {secret}",
            }
        ],
    )

    started = run_cli(git_repo, fixture, "start", "1")

    assert started.returncode == 0, started.stderr
    assert stdout_json(started)["status"] == "waiting_external"
    _assert_public_wait_projection(
        git_repo, fixture, str(stdout_json(started)["run_id"]), secret=secret
    )


def test_parent_approval_wait_is_immediately_observable_without_run(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["none", "pending"]},
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")
    awaiting = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert awaiting.returncode == 0, awaiting.stderr
    assert stdout_json(awaiting)["status"] == "parent_approval_pending"
    waiting = run_cli(git_repo, fixture, "approve", str(stdout_json(awaiting)["run_id"]))

    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_checks"
    _assert_public_wait_projection(git_repo, fixture, str(stdout_json(waiting)["run_id"]))


def test_final_approval_wait_is_immediately_observable_without_run(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "none", "pending"]},
    )
    agents = run_agents(git_repo / "agents.json")
    awaiting = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert awaiting.returncode == 0, awaiting.stderr
    assert stdout_json(awaiting)["status"] == "run_approval_pending"
    waiting = run_cli(git_repo, fixture, "approve", str(stdout_json(awaiting)["run_id"]))

    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_checks"
    _assert_public_wait_projection(git_repo, fixture, str(stdout_json(waiting)["run_id"]))


def test_parent_only_pending_window_survives_process_restart(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["pending"]},
    )
    agents = _parent_only_agents(git_repo / "parent-only-agents.json")

    first_process, first_state = _run_until_pending_window(
        git_repo, fixture, agents
    )
    _interrupt_run(first_process)
    first_window = first_state["supervision_window"]
    assert isinstance(first_window, dict)
    first_invocations = first_state["agent_invocation_history"]

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock"] = first_window["started_at"] + 60
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    second_process, _ = _run_until_pending_window(
        git_repo, fixture, agents, wait_for_retry_message=True
    )
    _interrupt_run(second_process)

    resumed_state = load_only_run_state(git_repo)
    assert resumed_state["supervision_window"] == first_window
    assert resumed_state["agent_invocation_history"] == first_invocations
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 1

    delivery.update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps({**fixture_data, "delivery": delivery}), encoding="utf-8")
    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "parent_approval_pending"
    final_state = load_only_run_state(git_repo)
    assert final_state["agent_invocation_history"] == first_invocations
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1


def test_run_recovers_final_run_check_timeout_without_duplicate_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending"]},
        supervision_clock_multiplier=540,
    )
    agents = run_agents(git_repo / "agents.json")

    paused = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert paused.returncode == 2
    paused_state = load_only_run_state(git_repo)
    assert paused_state["status"] == "supervision_timeout"
    assert paused_state["run_publication"]["phase"] == "waiting_checks"
    window = paused_state["supervision_window"]
    assert window["deadline"] - window["started_at"] == 45 * 60
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(fixture_data["delivery"]["pull_requests"]) == 2

    fixture_data["delivery"].update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    recovered = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "run_approval_pending"
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2


def test_final_run_pending_window_survives_process_restart(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "pending"]},
    )
    agents = run_agents(git_repo / "agents.json")

    first_process, first_state = _run_until_pending_window(
        git_repo, fixture, agents
    )
    _interrupt_run(first_process)
    first_window = first_state["supervision_window"]
    assert isinstance(first_window, dict)
    first_invocations = first_state["agent_invocation_history"]

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["supervision_clock"] = first_window["started_at"] + 60
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    second_process, _ = _run_until_pending_window(
        git_repo, fixture, agents, wait_for_retry_message=True
    )
    _interrupt_run(second_process)

    resumed_state = load_only_run_state(git_repo)
    assert resumed_state["supervision_window"] == first_window
    assert resumed_state["agent_invocation_history"] == first_invocations
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 2

    delivery.update({"required_checks": ["pass"], "check_position": 0})
    fixture.write_text(json.dumps({**fixture_data, "delivery": delivery}), encoding="utf-8")
    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    final_state = load_only_run_state(git_repo)
    assert final_state["agent_invocation_history"] == first_invocations
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 2
