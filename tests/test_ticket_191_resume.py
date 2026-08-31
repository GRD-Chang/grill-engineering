from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent_run.delivery_policy import DeliveryPolicyStore
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from cli_run_supervision_support import _interrupt_run, _parent_only_agents
from conftest import write_fixture
from test_cli import PROJECT_ROOT, load_only_run_state, run_cli, stdout_json
from test_cli_delivery import parent_round_agents, passing_acceptance


def _fail_parent_run(git_repo: Path, fixture: Path) -> None:
    failed_agents = git_repo / "failed-agents.json"
    failed_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-development",
                        "error_after_writes": "fixture process failed",
                    }
                ],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(failed_agents),
    )
    assert blocked.returncode == 2, blocked.stderr


def _resumed_parent_agents(git_repo: Path) -> Path:
    resumed_agents = _parent_only_agents(git_repo / "resumed-agents.json")
    agent_data = json.loads(resumed_agents.read_text(encoding="utf-8"))
    agent_data["developments"][0].update(
        {
            "expected_thread_id": "parent-development",
            "thread_id": "parent-development",
        }
    )
    resumed_agents.write_text(json.dumps(agent_data), encoding="utf-8")
    return resumed_agents


def _block_parent_for_human(git_repo: Path, fixture: Path) -> None:
    blocked_agents = git_repo / "human-blocked-agents.json"
    blocked_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-development",
                        "human_blockers": ["Maintainer approval is required."],
                    }
                ],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(blocked_agents),
    )
    assert blocked.returncode == 2, blocked.stderr
    assert load_only_run_state(git_repo)["status"] == "ready_for_human"


def _start_gated_resume(
    git_repo: Path,
    fixture: Path,
    agents: Path,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    source = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source
        if not environment.get("PYTHONPATH")
        else f"{source}{os.pathsep}{environment['PYTHONPATH']}"
    )
    environment.setdefault(
        "XDG_STATE_HOME", str(git_repo / ".agent-run-test-state")
    )
    environment.update(extra_env or {})
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_run",
            "resume",
            "1",
            *arguments,
            "--agent-fixture",
            str(agents),
            "--github-fixture",
            str(fixture),
        ],
        cwd=git_repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_agent_barrier(process: subprocess.Popen[str], started: Path) -> None:
    deadline = time.monotonic() + 5
    while not started.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"resume exited early: {stdout}\n{stderr}")
        if time.monotonic() >= deadline:
            raise AssertionError("resume did not reach the Agent fixture barrier")
        time.sleep(0.01)


def _wait_for_run_status(git_repo: Path, expected: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while True:
        durable = load_only_run_state(git_repo)
        if durable.get("status") == expected:
            return durable
        if time.monotonic() >= deadline:
            raise AssertionError(f"detached Resume Executor did not reach {expected}")
        time.sleep(0.01)


def _assert_single_resume_action(git_repo: Path) -> None:
    record = TaskControlStore(git_repo / ".agent-run").load(
        TaskKey(git_repo, "example/project", 1)
    )
    assert record is not None
    actions = [record.get("action"), *record.get("action_history", [])]
    assert sum(
        isinstance(action, dict) and action.get("kind") == "resume"
        for action in actions
    ) == 1


def test_resume_supervises_required_checks_until_the_next_real_boundary(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"required_checks": ["pending", "pass"]},
    )
    _fail_parent_run(git_repo, fixture)
    resumed_agents = _resumed_parent_agents(git_repo)
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["repository_read_failures"] = [
        {
            "code": "github_read_failed",
            "message": "repository binding has not converged",
        }
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        "1",
        "--agent-fixture",
        str(resumed_agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    output = stdout_json(resumed)
    assert output["status"] == "parent_approval_pending"
    assert output["action"]["operation"] == "resume"
    assert output["action"]["submission"] == "started"
    state = load_only_run_state(git_repo)
    assert state["parent_job"]["phase"] == "ready_for_approval"
    assert state["resume_audit"]["total"] == 1
    assert all(
        diagnostic.get("code") != "controller_interrupted"
        for diagnostic in state["diagnostics"]
    )
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 1


def test_duplicate_resume_attaches_and_observer_exit_does_not_stop_executor(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    _fail_parent_run(git_repo, fixture)
    resumed_agents = _resumed_parent_agents(git_repo)
    started = git_repo / "resume-agent-started"
    release = git_repo / "resume-agent-release"
    agent_data = json.loads(resumed_agents.read_text(encoding="utf-8"))
    agent_data["invocation_gate"] = {
        "role": "developments",
        "attempt": 1,
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 10,
    }
    resumed_agents.write_text(json.dumps(agent_data), encoding="utf-8")
    first = _start_gated_resume(git_repo, fixture, resumed_agents)
    try:
        _wait_for_agent_barrier(first, started)
        deadline = load_only_run_state(git_repo)["policy_snapshot"][
            "invocation_deadlines"
        ]["development"]

        policy_override = run_cli(
            git_repo,
            fixture,
            "resume",
            "1",
            "--development-deadline",
            f"{deadline}s",
            "--agent-fixture",
            str(resumed_agents),
        )
        assert policy_override.returncode == 2
        assert (
            stdout_json(policy_override)["diagnostics"][0]["code"]
            == "action_busy"
        )

        duplicate = run_cli(
            git_repo,
            fixture,
            "resume",
            "1",
            "--agent-fixture",
            str(resumed_agents),
        )
        assert duplicate.returncode == 0, duplicate.stderr
        duplicate_output = stdout_json(duplicate)
        assert duplicate_output["action"]["submission"] == "attached"

        first.terminate()
        first.communicate(timeout=3)
        release.touch()
        durable = _wait_for_run_status(git_repo, "parent_approval_pending")
        assert durable["resume_audit"]["total"] == 1
        parent_job = durable["parent_job"]
        assert parent_job["review_budget"]["development_attempts"] == 1
        resumed_invocations = [
            invocation
            for invocation in durable["agent_invocation_history"]
            if invocation.get("resume_id") is not None
        ]
        assert len(resumed_invocations) == 1
        assert all(
            diagnostic.get("code") != "controller_interrupted"
            for diagnostic in durable["diagnostics"]
        )
        delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
        assert len(delivery["pull_requests"]) == 1
        assert delivery.get("normal_merges", []) == []
        assert delivery.get("mutations", []) == []
    finally:
        release.touch(exist_ok=True)
        _interrupt_run(first, git_repo)


def test_duplicate_human_blocker_resume_uses_normalized_message_identity(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    _block_parent_for_human(git_repo, fixture)
    resumed_agents = _resumed_parent_agents(git_repo)
    started = git_repo / "human-resume-agent-started"
    release = git_repo / "human-resume-agent-release"
    agent_data = json.loads(resumed_agents.read_text(encoding="utf-8"))
    agent_data["invocation_gate"] = {
        "role": "developments",
        "attempt": 1,
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 10,
    }
    resumed_agents.write_text(json.dumps(agent_data), encoding="utf-8")
    first = _start_gated_resume(
        git_repo,
        fixture,
        resumed_agents,
        "--message",
        "  granted  ",
    )
    try:
        _wait_for_agent_barrier(first, started)

        duplicate = run_cli(
            git_repo,
            fixture,
            "resume",
            "1",
            "--message",
            "granted",
            "--agent-fixture",
            str(resumed_agents),
        )

        assert duplicate.returncode == 0, duplicate.stderr
        assert stdout_json(duplicate)["action"]["submission"] == "attached"
        release.touch()
        first.communicate(timeout=5)
        durable = _wait_for_run_status(git_repo, "parent_approval_pending")
        assert durable["resume_audit"]["total"] == 1
        assert durable["parent_job"]["review_budget"]["development_attempts"] == 1
        resumed_invocations = [
            invocation
            for invocation in durable["agent_invocation_history"]
            if invocation.get("resume_id") is not None
        ]
        assert len(resumed_invocations) == 1
        _assert_single_resume_action(git_repo)
        delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
        assert len(delivery["pull_requests"]) == 1
        assert delivery.get("normal_merges", []) == []
        assert delivery.get("mutations", []) == []
    finally:
        release.touch(exist_ok=True)
        _interrupt_run(first, git_repo)


def test_duplicate_budget_window_resume_keeps_first_explicit_policy_identity(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    config_home = git_repo / "config"
    policy_store = DeliveryPolicyStore(
        config_home / "agent-run" / "delivery-policy.json"
    )
    policy_store.configure({"ticket_review_rounds": 3})
    policy_environment = {"XDG_CONFIG_HOME": str(config_home)}
    initial_agents = git_repo / "budget-checkpoint-agents.json"
    initial_agents.write_text(
        json.dumps(parent_round_agents(1, passing_last=False)), encoding="utf-8"
    )
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--parent-only-paired-rounds",
        "1",
        "--agent-fixture",
        str(initial_agents),
        extra_env=policy_environment,
    )
    assert blocked.returncode == 2, blocked.stderr
    assert load_only_run_state(git_repo)["parent_job"]["review_budget"]["window"] == 1

    resumed_agents = git_repo / "budget-checkpoint-resume-agents.json"
    resumed_data = parent_round_agents(1, passing_last=True)
    resumed_development = resumed_data["developments"][0]
    assert isinstance(resumed_development, dict)
    resumed_development.update(
        {
            "expected_thread_id": "parent-round-developer",
            "expected_files": {"parent-feature.txt": "candidate-1\n"},
            "write_files": {"parent-feature.txt": "candidate-2\n"},
        }
    )
    resumed_data["reviews"] = [
        passing_acceptance(
            "parent-resume-reviewer", "The resumed budget window passed."
        )
    ]
    started = git_repo / "budget-resume-agent-started"
    release = git_repo / "budget-resume-agent-release"
    resumed_data["invocation_gate"] = {
        "role": "developments",
        "attempt": 1,
        "started_file": str(started),
        "release_file": str(release),
        "timeout_seconds": 10,
    }
    resumed_agents.write_text(json.dumps(resumed_data), encoding="utf-8")
    first = _start_gated_resume(
        git_repo,
        fixture,
        resumed_agents,
        "--development-deadline",
        "1s",
        extra_env=policy_environment,
    )
    try:
        _wait_for_agent_barrier(first, started)
        policy_store.configure({"ticket_review_rounds": 4})

        duplicate = run_cli(
            git_repo,
            fixture,
            "resume",
            "1",
            "--development-deadline",
            "1s",
            "--agent-fixture",
            str(resumed_agents),
            extra_env=policy_environment,
        )

        assert duplicate.returncode == 0, duplicate.stderr
        assert stdout_json(duplicate)["action"]["submission"] == "attached"
        release.touch()
        first.communicate(timeout=5)
        durable = _wait_for_run_status(git_repo, "parent_approval_pending")
        policy = durable["policy_snapshot"]
        assert policy["ticket_review_rounds"] == 3
        assert policy["invocation_deadlines"]["development"] == 1
        parent_job = durable["parent_job"]
        assert parent_job["review_budget"]["window"] == 2
        assert parent_job["review_budget"]["development_attempts"] == 1
        assert parent_job["review_budget"]["reviewer_invocations"] == 1
        assert durable["resume_audit"]["total"] == 1
        resumed_invocations = [
            invocation
            for invocation in durable["agent_invocation_history"]
            if invocation.get("resume_id") is not None
        ]
        assert len(resumed_invocations) == 1
        _assert_single_resume_action(git_repo)
        delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
        assert len(delivery["pull_requests"]) == 1
        assert delivery.get("normal_merges", []) == []
        assert delivery.get("mutations", []) == []
    finally:
        release.touch(exist_ok=True)
        _interrupt_run(first, git_repo)


def test_ordinary_resume_rejects_policy_override_without_mutating_the_run(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    _fail_parent_run(git_repo, fixture)
    before = load_only_run_state(git_repo)

    rejected = run_cli(
        git_repo,
        fixture,
        "resume",
        "1",
        "--development-deadline",
        "1s",
        "--agent-fixture",
        str(_resumed_parent_agents(git_repo)),
    )

    assert rejected.returncode == 2
    assert stdout_json(rejected)["diagnostics"][0]["code"] == "command_failed"
    assert load_only_run_state(git_repo) == before


def test_ordinary_run_preserves_an_operator_stopped_boundary(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    started = run_cli(git_repo, fixture, "start", "1")
    assert started.returncode == 0, started.stderr
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "operator_stopped",
            "terminal_kind": "operator_stopped",
            "diagnostics": [],
        }
    )
    StateStore(git_repo / ".agent-run").save_run(str(state["run_id"]), state)
    fixture_before = fixture.read_bytes()

    stopped = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(_parent_only_agents(git_repo / "unused-agents.json")),
    )

    assert stopped.returncode == 2, stopped.stderr
    assert stdout_json(stopped)["status"] == "operator_stopped"
    persisted = load_only_run_state(git_repo)
    receipt = persisted.pop("action_application_receipt")
    assert receipt["kind"] == "run"
    assert persisted == state
    assert fixture.read_bytes() == fixture_before
