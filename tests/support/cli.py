"""共享测试支撑：cli；每次调用保持独立状态。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
import agent_run.cli as cli
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.controller import Controller
from agent_run.codex import CodexProcessError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader, GitHubReadError
from agent_run.run_driver import DirectRunOperations, RunStep
from agent_run.semantic_attempt import canonical_fingerprint
from agent_run.state import FaultInjectingStateStore, StateStore
from agent_run.worker_sandbox import WorkerSandboxError
from support.workspace import managed_state, prepare_workspace


PROJECT_ROOT = Path(__file__).parents[2]


def issue(
    number: int,
    *,
    state: str = "OPEN",
    labels: list[str] | None = None,
    blocked_by: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "body": f"Implement ticket {number}.",
        "state": state,
        "labels": labels if labels is not None else ["ready-for-agent"],
        "blocked_by": blocked_by or [],
    }


def run_cli(
    repo: Path,
    fixture: Path | None,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
    machine_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the public CLI; structured legacy assertions opt into JSON by default.

    Human-presentation tests must pass ``machine_output=False`` so a default
    output regression cannot be hidden by this shared fixture.
    """

    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    if extra_env:
        environment.update(extra_env)
    command_arguments = list(arguments)
    if (
        machine_output
        and command_arguments
        and command_arguments[0]
        in {
            "run",
            "resume",
            "approve",
            "revise",
            "requeue",
            "stop",
            "abandon",
            "configure",
        }
        and "--json" not in command_arguments
    ):
        command_arguments.append("--json")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_run",
            *command_arguments,
            *(["--github-fixture", str(fixture)] if fixture is not None else []),
        ],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def run_policy_cli(
    repo: Path,
    *arguments: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "agent_run", "policy", *arguments],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def run_internal_stage(
    repo: Path, fixture: Path, stage: str, run_id: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Exercise a legacy stage's engine seam without reviving its CLI command."""

    agent_fixture = (
        Path(arguments[arguments.index("--agent-fixture") + 1])
        if "--agent-fixture" in arguments
        else fixture
    )
    crash_after_save = (
        int(arguments[arguments.index("--crash-after-save") + 1])
        if "--crash-after-save" in arguments
        else None
    )
    workspace = prepare_workspace(repo)
    git = workspace.open()
    states = (
        FaultInjectingStateStore(
            workspace.state_root, crash_after_save=crash_after_save
        )
        if crash_after_save is not None
        else StateStore(workspace.state_root)
    )
    reader = FixtureGitHubReader(fixture)
    controller = Controller(reader, git, states)
    operations = DirectRunOperations(
        controller=controller,
        states=states,
        git=git,
        github_reader=reader,
        publisher_factory=lambda: FixtureGitHubPublisher(fixture, git),
        agents=FixtureAgentBackend(agent_fixture),
    )
    step = (
        {
            "deliver": RunStep.DELIVER,
            "accept-run": RunStep.ACCEPT,
            "publish-run": RunStep.PUBLISH,
        }[stage]
        if stage != "approve"
        else None
    )
    current = states.load_current_run(run_id)
    invocation = current.get("active_agent_invocation") if isinstance(current, dict) else None
    if (
        isinstance(current, dict)
        and current.get("status") == "execution_failed"
        and isinstance(invocation, dict)
        and invocation.get("status") in {"failed", "completed"}
    ):
        controller.resume(run_id)
    try:
        state = (
            operations.approve(run_id).state
            if step is None
            else operations.dispatch(step, run_id).state
        )
    except (
        CodexProcessError,
        GitHubReadError,
        OSError,
        ValueError,
        WorkerSandboxError,
    ) as error:
        assert controller.record_execution_failure(run_id, str(error))
        state = states.load_current_run(run_id)
        assert state is not None
    active_ticket = state.get("active_ticket_job")
    output = {
        "result": "resumed",
        "run_id": state["run_id"],
        "status": state["status"],
        "run_branch": state.get("run_branch", state.get("parent_branch")),
        "active_ticket": (
            active_ticket.get("ticket_number")
            if isinstance(active_ticket, dict)
            else None
        ),
        "diagnostics": state.get("diagnostics", []),
        "scope_change": state.get("unsupported_scope_change"),
        "next_action": cli.cli_presentation._next_action(state),
    }
    return subprocess.CompletedProcess(
        args=["internal-stage", stage, run_id],
        returncode=(
            0
            if cli._lifecycle_result_succeeded(stage, state.get("status"))
            else 2
        ),
        stdout=json.dumps(output, ensure_ascii=False),
        stderr="",
    )


def git_fetch_failure_wrapper(
    directory: Path, *, failures: int
) -> tuple[Path, dict[str, str]]:
    real_git = shutil.which("git")
    assert real_git is not None
    counter = directory / "git-fetch-failures"
    counter.write_text(str(failures), encoding="utf-8")
    wrapper = directory / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"fetch\" ] && [ \"$(cat \"$AGENT_RUN_FETCH_COUNTER\")\" -gt 0 ]; then\n"
        "  remaining=$(cat \"$AGENT_RUN_FETCH_COUNTER\")\n"
        "  echo $((remaining - 1)) > \"$AGENT_RUN_FETCH_COUNTER\"\n"
        "  echo 'dial tcp: i/o timeout' >&2\n"
        "  exit 1\n"
        "fi\n"
        "exec \"$AGENT_RUN_REAL_GIT\" \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return counter, {
        "AGENT_RUN_FETCH_COUNTER": str(counter),
        "AGENT_RUN_REAL_GIT": real_git,
        "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}",
    }


def stdout_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    loaded: object = json.loads(result.stdout)
    assert isinstance(loaded, dict)
    return loaded


def load_only_run_state(
    repo: Path, *, extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    run_files = list((managed_state(repo, extra_env=extra_env) / "runs").glob("*.json"))
    assert len(run_files) == 1
    loaded: object = json.loads(run_files[0].read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def failed_invocation(
    *,
    work_subject: str,
    role: str,
    phase: str,
    generation: int = 1,
    status: str = "failed",
) -> dict[str, Any]:
    semantic_role = (
        "development"
        if role == "development"
        else "reviewer" if role in {"fresh_acceptance", "reviewer"} else "publication"
    )
    boundary_fingerprint = canonical_fingerprint({})
    identity = {
        "role": semantic_role,
        "work_subject": work_subject,
        "generation": generation,
        "currentness_boundary_fingerprint": boundary_fingerprint,
        "ordinal": 1,
        "budget_window": 1 if semantic_role in {"development", "reviewer"} else None,
    }
    semantic_attempt = {
        "attempt_id": canonical_fingerprint(identity),
        **identity,
        "status": "pending",
    }
    return {
        "work_subject": work_subject,
        "generation": generation,
        "role": role,
        "phase": phase,
        "mode": "fresh",
        "input_fingerprint": "fixture",
        "currentness_boundary": {},
        "semantic_attempt": semantic_attempt,
        "status": status,
        "requested_thread_id": None,
        "reported_thread_id": None,
        "attempt_count": 1,
        "started_at": "2026-08-13T00:00:00+00:00",
        "ended_at": "2026-08-13T00:00:01+00:00",
        "error": "fixture failure",
        "return_code": 1,
        "signal": None,
    }
