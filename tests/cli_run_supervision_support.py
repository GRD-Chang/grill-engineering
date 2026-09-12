from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_run.executor_host import _process_start_token
from cli_fixtures import run_agents
from support.inprocess_cli import invoke_cli_inprocess
from test_cli import PROJECT_ROOT, load_only_run_state, stdout_json
from test_cli_delivery import parent_publication, passing_acceptance, publication

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
        json_result = invoke_cli_inprocess(repo, fixture, command, run_id, "--json")
        text_result = invoke_cli_inprocess(repo, fixture, command, run_id)

        assert json_result.returncode == text_result.returncode == 0
        wait = stdout_json(json_result)["supervision"]
        assert isinstance(wait, dict)
        assert _WAIT_FIELDS <= wait.keys()
        assert wait["kind"] in {"github_convergence", "required_checks"}
        assert wait["started_at"] is not None
        assert wait["deadline"] is not None
        assert wait["timeout_resume_action"] == "agent-run run 1"
        for label in (
            "等待 GitHub 的操作结果",
            "已等待：",
            "本轮最多还可等待：",
            "无法确认 Runner 是否仍在自动等待",
            "agent-run doctor",
        ):
            assert label in text_result.stdout
        assert "截止=" not in text_result.stdout
        assert "超时恢复:" not in text_result.stdout
        if secret is not None:
            assert secret not in json_result.stdout
            assert secret not in text_result.stdout

def _assert_waiting_external_recovery_action(
    repo: Path, fixture: Path, run_id: str
) -> None:
    expected_action = "agent-run run 1"
    for command in ("status", "history"):
        json_result = invoke_cli_inprocess(repo, fixture, command, run_id, "--json")
        text_result = invoke_cli_inprocess(repo, fixture, command, run_id)

        assert json_result.returncode == text_result.returncode == 0
        assert stdout_json(json_result)["next_action"] == expected_action
        assert f"下一步: {expected_action}" in text_result.stdout

def _assert_credential_wait_is_not_public(
    repo: Path, fixture: Path, run_id: str
) -> None:
    for command in ("status", "history"):
        output = stdout_json(invoke_cli_inprocess(repo, fixture, command, run_id, "--json"))
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
        _interrupt_run(process, repo)
        raise
    _interrupt_run(process, repo)
    pytest.fail("agent-run run did not reach its pending-window barrier before timeout")


def _interrupt_run(process: subprocess.Popen[str], repo: Path) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=3)
    assert process.returncode is not None
    executors: list[tuple[int, str]] = []
    for path in (repo / ".agent-run" / "task-control").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        executor = record.get("executor")
        if (
            isinstance(executor, dict)
            and executor.get("status") in {"starting", "running"}
            and type(executor.get("pid")) is int
            and isinstance(executor.get("process_start_token"), str)
        ):
            executors.append(
                (executor["pid"], executor["process_start_token"])
            )
    for pid, start_token in executors:
        if not _fixture_process_matches(pid, start_token):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    for pid, start_token in executors:
        while time.monotonic() < deadline and _fixture_process_matches(
            pid, start_token
        ):
            time.sleep(0.01)
        if _fixture_process_matches(pid, start_token):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        kill_deadline = time.monotonic() + 3
        while time.monotonic() < kill_deadline and _fixture_process_matches(
            pid, start_token
        ):
            time.sleep(0.01)
        assert not _fixture_process_matches(pid, start_token)


def _fixture_process_matches(pid: int, start_token: str) -> bool:
    if _process_start_token(pid) != start_token:
        return False
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return False
    closing = raw.rfind(")")
    if closing < 0:
        return False
    fields = raw[closing + 2 :].split()
    return bool(fields) and fields[0] != "Z"

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
