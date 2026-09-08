from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

import agent_run.cli as cli_module
from agent_run.agent_invocation import record_operator_stop
from agent_run.delivery_policy import DeliveryPolicyStore
from agent_run.executor_host import ExecutorSpec, HostObservation
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.publication_operation_retry import record_publication_operation_failure
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from cli_run_supervision_support import _interrupt_run, _parent_only_agents
from cli_fixtures import run_agents
from conftest import seed_run, write_fixture
from test_cli import (
    PROJECT_ROOT,
    issue,
    load_only_run_state,
    run_cli,
    run_internal_stage,
    stdout_json,
)
from test_cli_delivery import parent_round_agents, passing_acceptance, ticket


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


def _establish_ordinary_run_boundary(
    git_repo: Path,
    fixture: Path,
    boundary: str,
) -> None:
    if boundary == "execution_failed":
        _fail_parent_run(git_repo, fixture)
    elif boundary == "ready_for_human":
        _block_parent_for_human(git_repo, fixture)
    elif boundary == "blocked_waiting_human":
        _block_parent_for_human(git_repo, fixture)
        state = load_only_run_state(git_repo)
        state.update({"status": "blocked", "terminal_kind": "waiting_human"})
        StateStore(git_repo / ".agent-run").save_run(str(state["run_id"]), state)
    elif boundary == "permanent_blocked":
        blocked = run_cli(git_repo, fixture, "run", "1")
        assert blocked.returncode == 2, blocked.stderr
    elif boundary == "publication_pending":
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(run_agents(git_repo / "initial-agents.json")),
        )
        assert started.returncode == 0, started.stderr
        state = load_only_run_state(git_repo)
        publication = state["run_publication"]
        assert isinstance(publication, dict)
        for _ in range(5):
            record_publication_operation_failure(
                publication, RuntimeError("publication readback failed")
            )
        publication["phase"] = "publication_pending"
        state.update(
            {"status": "publication_pending", "terminal_kind": "publication_pending"}
        )
        StateStore(git_repo / ".agent-run").save_run(str(state["run_id"]), state)
    elif boundary == "unsupported_scope_change":
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(_parent_only_agents(git_repo / "initial-agents.json")),
        )
        assert started.returncode == 0, started.stderr
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        fixture_data["parent"]["sub_issues"] = [3]
        fixture_data["issues"] = {"3": ticket()}
        fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
        blocked = run_internal_stage(
            git_repo,
            fixture,
            "deliver",
            str(stdout_json(started)["run_id"]),
        )
        assert blocked.returncode == 2, blocked.stderr
    elif boundary == "deterministic_contradiction":
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(run_agents(git_repo / "initial-agents.json")),
        )
        assert started.returncode == 0, started.stderr
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        final_pr = next(
            pull
            for pull in fixture_data["delivery"]["pull_requests"]
            if pull.get("scope") == "final_run"
        )
        final_pr["head_repository"] = "foreign/project"
        fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
        state = load_only_run_state(git_repo)
        state.update(
            {"status": "waiting_external", "terminal_kind": "waiting_external"}
        )
        state["run_publication"]["phase"] = "waiting_external"
        StateStore(git_repo / ".agent-run").save_run(str(state["run_id"]), state)
        blocked = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(run_agents(git_repo / "contradiction-agents.json")),
        )
        assert blocked.returncode == 2, blocked.stderr
    elif boundary == "abandonment_pending":
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(_parent_only_agents(git_repo / "initial-agents.json")),
        )
        assert started.returncode == 0, started.stderr
        state = load_only_run_state(git_repo)
        state.update(
            {"status": "abandonment_pending", "terminal_kind": "abandonment_pending"}
        )
        StateStore(git_repo / ".agent-run").save_run(str(state["run_id"]), state)
    elif boundary == "progress_exhausted":
        blocked = run_cli(git_repo, fixture, "run", "1")
        assert blocked.returncode == 2, blocked.stderr
        state = load_only_run_state(git_repo)
        state["terminal_kind"] = "waiting_human"
        StateStore(git_repo / ".agent-run").save_run(str(state["run_id"]), state)
    else:
        agents = (
            run_agents(git_repo / "initial-agents.json")
            if boundary.startswith("run_approval_pending")
            else _parent_only_agents(git_repo / "initial-agents.json")
        )
        started = run_cli(
            git_repo,
            fixture,
            "run",
            "1",
            "--agent-fixture",
            str(agents),
        )
        assert started.returncode == 0, started.stderr
        if boundary == "run_approval_pending_after_revise":
            revision_agents = run_agents(git_repo / "revision-agents.json")
            revision_data = json.loads(
                revision_agents.read_text(encoding="utf-8")
            )
            revision_data["developments"] = [
                {
                    "expected_thread_id": None,
                    "thread_id": "run-revision-developer",
                    "summary": "Applied the requested Run revision.",
                    "write_files": {"run-revision.txt": "revised\n"},
                }
            ]
            revision_data["reviews"] = []
            revision_data["run_reviews"] = [
                passing_acceptance(
                    "run-revision-reviewer", "The revision passed."
                )
            ]
            revision_agents.write_text(json.dumps(revision_data), encoding="utf-8")
            revised = run_cli(
                git_repo,
                fixture,
                "revise",
                "1",
                "--message",
                "preserve the explicit approval boundary",
                "--agent-fixture",
                str(revision_agents),
            )
            assert revised.returncode == 0, f"{revised.stdout}\n{revised.stderr}"
        if boundary == "operator_stopped":
            state = load_only_run_state(git_repo)
            record_operator_stop(
                state,
                save=lambda stopped: StateStore(git_repo / ".agent-run").save_run(
                    str(stopped["run_id"]), stopped
                ),
            )
    expected_status = (
        "blocked"
        if boundary in {"blocked_waiting_human", "permanent_blocked"}
        else boundary.removesuffix("_after_revise")
    )
    assert load_only_run_state(git_repo)["status"] == expected_status


def _tree_snapshot(root: Path) -> dict[Path, bytes | None]:
    return {
        path.relative_to(root): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


def _assert_production_run_does_not_prepare_executor(
    git_repo: Path,
    fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_arguments: tuple[str, ...],
    *,
    expected_return_code: int,
) -> None:
    state_root = git_repo / ".agent-run"
    state_tree_before = _tree_snapshot(state_root)
    fixture_before = fixture.read_bytes()
    environment_carrier = git_repo / "runtime" / "environment-carrier.json"
    hosts: list[ObservedSystemdHost] = []

    class UsageLease:
        def fileno(self) -> int:
            return 1

    class ObservedSystemdHost:
        def __init__(self, **_options: object) -> None:
            self.prepare_count = 0
            hosts.append(self)

        def check_readiness(self) -> None:
            pass

        def prepare_environment(self, _command: object) -> None:
            self.prepare_count += 1
            environment_carrier.parent.mkdir(parents=True, exist_ok=True)
            environment_carrier.write_text("unexpected\n", encoding="utf-8")

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(git_repo / "runtime"))
    monkeypatch.setattr(cli_module, "_running_active_runner", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "runner_usage_lease",
        lambda _path: nullcontext(UsageLease()),
    )
    monkeypatch.setattr(cli_module, "SystemdUserExecutorHost", ObservedSystemdHost)
    monkeypatch.setattr(
        cli_module,
        "GhGitHubReader",
        lambda _repo, *, working_directory: FixtureGitHubReader(fixture),
    )

    return_code = cli_module.main(
        ["run", "1", "--repo", "example/project", *run_arguments, "--json"]
    )

    assert return_code == expected_return_code
    assert len(hosts) == 1
    assert hosts[0].prepare_count == 0
    assert not environment_carrier.exists()
    assert _tree_snapshot(state_root) == state_tree_before
    assert fixture.read_bytes() == fixture_before


def _assert_production_run_rejects_before_readiness(
    git_repo: Path,
    fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_arguments: tuple[str, ...],
    *,
    expected_status: str,
    state_root: Path | None = None,
    state_home: Path | None = None,
) -> None:
    state_root = state_root or git_repo / ".agent-run"
    state_tree_before = _tree_snapshot(state_root)
    fixture_before = fixture.read_bytes()
    environment_carrier = git_repo / "runtime" / "environment-carrier.json"
    capsys.readouterr()

    for readiness_failure in ("active-runner-missing", "systemd-unavailable"):
        calls = {"lease": 0, "active_runner": 0, "host": 0, "readiness": 0}

        class UsageLease:
            def fileno(self) -> int:
                return 1

        class UnavailableSystemdHost:
            def __init__(self, **_options: object) -> None:
                calls["host"] += 1

            def check_readiness(self) -> None:
                calls["readiness"] += 1
                raise cli_module.SystemdExecutionReadinessError(
                    "user systemd unavailable"
                )

        def observed_runner_lease(_path: Path) -> object:
            calls["lease"] += 1
            return nullcontext(UsageLease())

        def active_runner_available() -> bool:
            calls["active_runner"] += 1
            return readiness_failure == "systemd-unavailable"

        with monkeypatch.context() as readiness:
            readiness.chdir(git_repo)
            readiness.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
            readiness.setenv("XDG_RUNTIME_DIR", str(git_repo / "runtime"))
            if state_home is not None:
                readiness.setenv("XDG_STATE_HOME", str(state_home))
            readiness.setattr(cli_module, "runner_usage_lease", observed_runner_lease)
            readiness.setattr(
                cli_module, "_running_active_runner", active_runner_available
            )
            readiness.setattr(
                cli_module, "SystemdUserExecutorHost", UnavailableSystemdHost
            )
            readiness.setattr(
                cli_module,
                "GhGitHubReader",
                lambda _repo, *, working_directory: FixtureGitHubReader(fixture),
            )

            return_code = cli_module.main(
                ["run", "1", "--repo", "example/project", *run_arguments, "--json"]
            )

        output = json.loads(capsys.readouterr().out)
        assert return_code == 2
        assert output["result"] == "rejected"
        assert output["status"] == expected_status
        assert calls == {"lease": 0, "active_runner": 0, "host": 0, "readiness": 0}
        assert not environment_carrier.exists()
        assert _tree_snapshot(state_root) == state_tree_before
        assert fixture.read_bytes() == fixture_before


@pytest.mark.parametrize("control_case", ["missing", "corrupt"])
@pytest.mark.parametrize("host_status", ["exited", "unknown", "conflict"])
@pytest.mark.parametrize(
    "boundary",
    ["execution_failed", "ready_for_human", "operator_stopped"],
)
def test_resume_reconciles_a_terminal_receipt_before_admitting_its_successor(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control_case: str,
    host_status: str,
    boundary: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    _establish_ordinary_run_boundary(git_repo, fixture, boundary)
    state_before = load_only_run_state(git_repo)
    receipt_before = state_before["action_application_receipt"]
    assert isinstance(receipt_before, dict)
    control_store = TaskControlStore(git_repo / ".agent-run")
    task = TaskKey(git_repo, "example/project", 1)
    control_before = control_store.load(task)
    assert control_before is not None
    old_action = control_before["action"]
    assert isinstance(old_action, dict)
    old_generation = old_action["executor_generation"]
    control_path = control_store.path_for(task)
    if control_case == "missing":
        control_path.unlink()
    else:
        control_path.write_text("not json", encoding="utf-8")

    arguments = (
        ("--message", "operator granted the requested input")
        if boundary == "ready_for_human"
        else ()
    )

    class UsageLease:
        def fileno(self) -> int:
            return 1

    class ReconciliationHost(cli_module.FakeExecutorHost):
        def __init__(self) -> None:
            super().__init__()
            self.observe_count = 0

        def check_readiness(self) -> None:
            pass

        def prepare_environment(self, _arguments: object) -> None:
            pass

        def observe(
            self, spec: ExecutorSpec, _control: TaskControlStore
        ) -> HostObservation:
            self.observe_count += 1
            return HostObservation(
                host_status,  # type: ignore[arg-type]
                spec.generation,
                None,
                False,
                "original Executor ownership is not proven"
                if host_status != "exited"
                else None,
                runner_binding="0" * 16 if host_status == "exited" else None,
            )

    host = ReconciliationHost()
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(git_repo / "runtime"))
    monkeypatch.setattr(
        cli_module, "runner_usage_lease", lambda _path: nullcontext(UsageLease())
    )
    monkeypatch.setattr(cli_module, "_running_active_runner", lambda: True)
    monkeypatch.setattr(
        cli_module, "SystemdUserExecutorHost", lambda **_options: host
    )
    monkeypatch.setattr(
        cli_module,
        "GhGitHubReader",
        lambda _repo, *, working_directory: FixtureGitHubReader(fixture),
    )
    monkeypatch.setattr(
        cli_module,
        "GhGitHubPublisher",
        lambda _repo, git: FixtureGitHubPublisher(fixture, git),
    )
    command = [
        "resume",
        str(state_before["run_id"]),
        "--repo",
        "example/project",
        *arguments,
        "--agent-fixture",
        str(_resumed_parent_agents(git_repo)),
        "--json",
    ]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_bytes = state_path.read_bytes()
    fixture_bytes = fixture.read_bytes()
    return_code = cli_module.main(command)
    output = json.loads(capsys.readouterr().out)

    if host_status != "exited":
        assert return_code == 2, output
        assert output["diagnostics"][0]["code"] == "executor_start_unknown"
        assert host.start_count == 0
        assert state_path.read_bytes() == state_bytes
        assert fixture.read_bytes() == fixture_bytes
        repaired_bytes = control_path.read_bytes()
        repaired = control_store.load(task)
        assert repaired is not None
        assert repaired["next_generation"] == old_generation + 1
        assert repaired["action"]["action_id"] == receipt_before["action_id"]

        repeated_return_code = cli_module.main(command)
        repeated_output = json.loads(capsys.readouterr().out)
        assert repeated_return_code == 2, repeated_output
        assert repeated_output["diagnostics"][0]["code"] == "executor_start_unknown"
        assert control_path.read_bytes() == repaired_bytes
        assert state_path.read_bytes() == state_bytes
        assert fixture.read_bytes() == fixture_bytes
        assert host.observe_count == 2
        assert host.start_count == 0
        return

    assert return_code == 0, output
    assert output["action"]["submission"] == "started"
    assert host.observe_count == 1
    assert host.start_count == 1
    repaired = control_store.load(task)
    assert repaired is not None
    successor = repaired["action"]
    assert successor["kind"] == "resume"
    assert successor["executor_generation"] == old_generation + 1
    predecessors = [
        entry["action"]
        for entry in repaired["action_history"]
        if entry["action"]["action_id"] == receipt_before["action_id"]
    ]
    assert len(predecessors) == 1
    predecessor = predecessors[0]
    assert predecessor["status"] == "completed"
    assert predecessor["receipt_only_reconciliation"] is True
    assert "payload" not in predecessor
    assert all(
        predecessor[key] == receipt_before[key]
        for key in ("action_id", "kind", "payload_digest", "run_id")
    )
    durable = load_only_run_state(git_repo)
    assert durable["action_application_receipt"]["action_id"] == successor[
        "action_id"
    ]
    assert durable["resume_audit"]["total"] == 1


@pytest.mark.parametrize(
    "boundary",
    [
        "execution_failed",
        "ready_for_human",
        "blocked_waiting_human",
        "permanent_blocked",
        "publication_pending",
        "unsupported_scope_change",
        "deterministic_contradiction",
        "abandonment_pending",
        "progress_exhausted",
        "run_approval_pending",
        "run_approval_pending_after_revise",
        "parent_approval_pending",
        "operator_stopped",
    ],
)
def test_ordinary_run_preserves_explicit_authorization_boundaries(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    boundary: str,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues=(
            {
                "2": issue(2, blocked_by=[{"number": 3, "state": "OPEN"}]),
                "3": issue(3, blocked_by=[{"number": 2, "state": "OPEN"}]),
            }
            if boundary == "permanent_blocked"
            else {
                "2": issue(2, blocked_by=[{"number": 99, "state": "OPEN"}])
            }
            if boundary == "progress_exhausted"
            else {"3": ticket()}
            if boundary.startswith("run_approval_pending")
            or boundary in {"publication_pending", "deterministic_contradiction"}
            else {}
        ),
    )
    _establish_ordinary_run_boundary(git_repo, fixture, boundary)
    expected_status = (
        "blocked"
        if boundary in {"blocked_waiting_human", "permanent_blocked"}
        else boundary.removesuffix("_after_revise")
    )
    if boundary == "run_approval_pending_after_revise":
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        fixture_data["delivery_graph_read_failures"] = [
            {
                "code": "github_read_failed",
                "message": "revision replay must not refresh the original run",
            }
        ]
        fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    state_root = git_repo / ".agent-run"
    state_path = next((state_root / "runs").glob("*.json"))
    control_path = next((state_root / "task-control").glob("*.json"))
    current_state = load_only_run_state(git_repo)
    receipt_before = current_state["action_application_receipt"]
    assert isinstance(receipt_before, dict)
    state_before = state_path.read_bytes()
    control_before = control_path.read_bytes()
    control_document_before = json.loads(control_before)
    state_tree_before = _tree_snapshot(state_root)
    fixture_before = fixture.read_bytes()
    unused_agents = _parent_only_agents(git_repo / "unused-agents.json")
    unused_data = json.loads(unused_agents.read_text(encoding="utf-8"))
    unused_data["developments"][0]["write_files"] = {
        "unexpected-successor.txt": "unexpected\n"
    }
    unused_agents.write_text(json.dumps(unused_data), encoding="utf-8")
    marker = git_repo / "unexpected-successor.txt"
    assert not marker.exists()

    # 每组参数都必须只读，复用边界前置流程并逐组核对同一快照。
    for run_arguments in (
        (),
        ("--development-model", "future-model", "--development-effort", "high"),
        ("--ticket-review-rounds", "1"),
    ):
        try:
            with monkeypatch.context() as argument_patch:
                capsys.readouterr()
                rejected = run_cli(
                    git_repo,
                    fixture,
                    "run",
                    "1",
                    *run_arguments,
                    "--agent-fixture",
                    str(unused_agents),
                )

                expected_return_code = (
                    0
                    if expected_status
                    in {"run_approval_pending", "parent_approval_pending"}
                    else 2
                )
                assert rejected.returncode == expected_return_code, rejected.stderr
                output = stdout_json(rejected)
                assert output["status"] == expected_status, output
                if expected_status == "publication_pending":
                    assert output["result"] == "rejected"
                assert state_path.read_bytes() == state_before
                assert control_path.read_bytes() == control_before
                assert _tree_snapshot(state_root) == state_tree_before
                assert (
                    load_only_run_state(git_repo)["action_application_receipt"]
                    == receipt_before
                )
                control_document_after = json.loads(control_path.read_bytes())
                assert control_document_after["action"]["executor_generation"] == (
                    control_document_before["action"]["executor_generation"]
                )
                assert (
                    control_document_after["action_history"]
                    == control_document_before["action_history"]
                )
                assert fixture.read_bytes() == fixture_before
                assert not marker.exists()

                if expected_status not in {
                    "run_approval_pending", "parent_approval_pending"
                }:
                    _assert_production_run_rejects_before_readiness(
                        git_repo,
                        fixture,
                        argument_patch,
                        capsys,
                        run_arguments,
                        expected_status=expected_status,
                    )
                else:
                    _assert_production_run_does_not_prepare_executor(
                        git_repo,
                        fixture,
                        argument_patch,
                        run_arguments,
                        expected_return_code=expected_return_code,
                    )
                assert state_path.read_bytes() == state_before
                assert control_path.read_bytes() == control_before
                capsys.readouterr()
        except AssertionError as error:
            error.add_note(f"run_arguments={run_arguments!r}")
            raise
