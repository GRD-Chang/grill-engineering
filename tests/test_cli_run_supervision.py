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
                if ready and "推进: waiting_checks" in process.stderr.readline():
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
