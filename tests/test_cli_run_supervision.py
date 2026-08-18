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
    data["reviews"].append(
        passing_acceptance("run-repair-reviewer-1", "The Run repair passed.")
    )
    data["run_reviews"].append(
        passing_acceptance("run-reviewer-2", "The repaired Run passed.")
    )
    data["run_reviews"].append(
        passing_acceptance("run-reviewer-3", "The refreshed Run passed.")
    )
    data["run_publications"].append(data["run_publications"][0])
    agents.write_text(json.dumps(data), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["run_acceptance"]["modification_attempts"] == 1
    assert state["run_publication"]["phase"] == "ready_for_approval"


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


def test_final_run_approval_grant_is_revoked_when_acceptance_facts_change(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "none", "pending"]},
    )
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    assert stdout_json(run_cli(git_repo, fixture, "approve", run_id))["status"] == "waiting_checks"

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Changed after final approval."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    halted = run_cli(git_repo, fixture, "publish-run", run_id)

    assert stdout_json(halted)["status"] == "run_acceptance_pending"
    state = load_only_run_state(git_repo)
    assert "approval_grant" not in state["run_publication"]
    assert data["delivery"]["pull_requests"][1]["state"] == "OPEN"


def test_final_run_approval_grant_is_revoked_when_pr_identity_changes(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "none", "pending"]},
    )
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    assert stdout_json(run_cli(git_repo, fixture, "approve", run_id))["status"] == "waiting_checks"

    state = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["published_branches"][state["run_branch"]] = "foreign-head"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    halted = run_cli(git_repo, fixture, "publish-run", run_id)

    assert stdout_json(halted)["status"] == "run_acceptance_pending"
    assert "approval_grant" not in load_only_run_state(git_repo)["run_publication"]
    assert data["delivery"]["pull_requests"][1]["state"] == "OPEN"


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


def test_final_run_revokes_approval_when_identity_drifts_while_waiting_external(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["none", "none", "pending"]},
    )
    agents = run_agents(git_repo / "agents.json")
    initial = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))
    run_id = str(stdout_json(initial)["run_id"])
    assert stdout_json(run_cli(git_repo, fixture, "approve", run_id))["status"] == "waiting_checks"

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["open_live_pull_request_failures"] = [
        {"code": "github_read_failed", "message": "readback unavailable"}
    ]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    assert stdout_json(run_cli(git_repo, fixture, "publish-run", run_id))["status"] == "waiting_external"

    state = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["published_branches"][state["run_branch"]] = "foreign-head"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    halted = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert halted.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["run_acceptance"]["phase"] == "reviewing"
    assert "approval_grant" not in state["run_publication"]
    final_pulls = [
        pull for pull in json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
        if pull.get("scope") == "final_run"
    ]
    assert all(pull["state"] == "OPEN" for pull in final_pulls)


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
