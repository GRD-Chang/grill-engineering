from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from conftest import write_fixture


PROJECT_ROOT = Path(__file__).parents[1]


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


def run_cli(repo: Path, fixture: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_run",
            *arguments,
            "--github-fixture",
            str(fixture),
        ],
        cwd=repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def stdout_json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    loaded: object = json.loads(result.stdout)
    assert isinstance(loaded, dict)
    return loaded


def load_only_run_state(repo: Path) -> dict[str, Any]:
    run_files = list((repo / ".agent-run" / "runs").glob("*.json"))
    assert len(run_files) == 1
    loaded: object = json.loads(run_files[0].read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_start_creates_one_run_branch_and_resume_is_idempotent(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2), "3": issue(3)},
    )

    first = run_cli(git_repo, fixture, "start", "1")
    assert first.returncode == 0, first.stderr
    first_output = stdout_json(first)
    state = load_only_run_state(git_repo)

    second = run_cli(git_repo, fixture, "start", "1")
    assert second.returncode == 0, second.stderr
    second_output = stdout_json(second)

    assert first_output["result"] == "started"
    assert second_output["result"] == "resumed"
    assert first_output["run_id"] == second_output["run_id"] == state["run_id"]
    assert state["parent"]["number"] == 1
    assert state["base"]["branch"] == "main"
    assert state["ticket_graph"]["ordered_ticket_numbers"] == [2, 3]
    assert state["frontier"] == [2, 3]
    assert state["active_ticket_job"]["ticket_number"] == 2
    branches = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/agent-run/"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert branches == [state["run_branch"]]
    assert len(list((git_repo / ".agent-run" / "runs").glob("*.json"))) == 1


def test_frontier_uses_native_dependencies_labels_state_and_parent_order(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "9": issue(9, blocked_by=[{"number": 20, "state": "OPEN"}]),
            "8": issue(8, labels=["ready-for-agent", "needs-info"]),
            "7": issue(7, labels=[]),
            "6": issue(6, state="CLOSED"),
            "10": issue(10, blocked_by=[{"number": 20, "state": "CLOSED"}]),
            "5": issue(5),
            "2": issue(2),
        },
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 0, result.stderr
    state = load_only_run_state(git_repo)
    assert state["frontier"] == [10, 5, 2]
    assert state["active_ticket_job"] == {
        "ticket_number": 10,
        "selection_reason": "first eligible ticket by parent sub-issue order, then issue number",
    }
    tickets = state["ticket_graph"]["tickets"]
    assert tickets["9"]["eligibility"]["reason"] == "blocked_by_open_issues"
    assert tickets["8"]["eligibility"]["reason"] == "disqualifying_label:needs-info"
    assert tickets["7"]["eligibility"]["reason"] == "missing_ready_for_agent"
    assert tickets["6"]["eligibility"]["reason"] == "ticket_closed"
    assert tickets["10"]["eligibility"]["reason"] == "eligible"


def test_unreliable_sub_issue_order_falls_back_to_issue_number(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"9": issue(9), "2": issue(2), "5": issue(5)},
        parent={
            "number": 1,
            "title": "Parent spec",
            "body": "Deliver the ticket set.",
            "sub_issues": [9, 2, 5],
            "sub_issue_order_reliable": False,
        },
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 0, result.stderr
    state = load_only_run_state(git_repo)
    assert state["frontier"] == [2, 5, 9]
    assert state["active_ticket_job"]["ticket_number"] == 2


def test_resume_command_reuses_exact_run(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]

    resumed = run_cli(git_repo, fixture, "resume", run_id)

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["result"] == "resumed"
    assert stdout_json(resumed)["run_id"] == run_id


def test_resume_accepts_run_branch_that_advanced_from_original_base(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    tree = subprocess.run(
        ["git", "rev-parse", f"{state['run_branch']}^{{tree}}"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    advanced = subprocess.run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            state["run_branch"],
            "-m",
            "integrate accepted ticket",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{state['run_branch']}", advanced],
        cwd=git_repo,
        check=True,
    )

    resumed = run_cli(
        git_repo, fixture, "resume", stdout_json(started)["run_id"]
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["result"] == "resumed"
    assert (
        subprocess.run(
            ["git", "rev-parse", state["run_branch"]],
            cwd=git_repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        == advanced
    )


def test_live_default_head_is_fetched_before_run_branch_creation(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(git_repo), str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)],
        cwd=git_repo,
        check=True,
    )
    updater = tmp_path / "updater"
    subprocess.run(
        ["git", "clone", str(remote), str(updater)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Updater"], cwd=updater, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "updater@example.invalid"],
        cwd=updater,
        check=True,
    )
    (updater / "remote.txt").write_text("new live head\n", encoding="utf-8")
    subprocess.run(["git", "add", "remote.txt"], cwd=updater, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance remote"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    live_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=updater,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2)},
        default_head_sha=live_head,
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 0, result.stderr
    state = load_only_run_state(git_repo)
    assert state["base"]["sha"] == live_head
    branch_head = subprocess.run(
        ["git", "rev-parse", state["run_branch"]],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert branch_head == live_head


def test_no_executable_ticket_is_progress_exhaustion_not_completion(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2, blocked_by=[{"number": 99, "state": "OPEN"}])},
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 2
    output = stdout_json(result)
    state = load_only_run_state(git_repo)
    assert output["status"] == "progress_exhausted"
    assert state["status"] == "progress_exhausted"
    assert state["active_ticket_job"] is None
    assert state["diagnostics"][0]["code"] == "no_executable_ticket"


def test_cycle_is_persisted_as_blocked_with_diagnostic(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": issue(2, blocked_by=[{"number": 3, "state": "OPEN"}]),
            "3": issue(3, blocked_by=[{"number": 2, "state": "OPEN"}]),
        },
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "blocked"
    assert state["terminal_kind"] == "permanent_blocked"
    assert state["diagnostics"][0]["code"] == "dependency_cycle"
    assert state["diagnostics"][0]["ticket_numbers"] == [2, 3]


def test_missing_ticket_is_persisted_as_blocked(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2)},
        parent={
            "number": 1,
            "title": "Parent spec",
            "body": "Deliver the ticket set.",
            "sub_issues": [2, 404],
            "sub_issue_order_reliable": True,
        },
    )

    result = run_cli(git_repo, fixture, "start", "1")

    assert result.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "blocked"
    assert state["terminal_kind"] == "permanent_blocked"
    assert state["diagnostics"][0] == {
        "code": "missing_ticket",
        "message": "GitHub did not return sub-issue #404",
        "ticket_number": 404,
    }


def test_github_read_failure_is_persisted_and_retryable(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2)},
        error={"code": "github_read_failed", "message": "simulated outage"},
    )

    failed = run_cli(git_repo, fixture, "start", "1")

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    state = load_only_run_state(git_repo)
    assert state["status"] == "execution_failed"
    assert state["terminal_kind"] == "execution_failed"
    assert state["diagnostics"][0]["code"] == "github_read_failed"

    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    retried = run_cli(git_repo, fixture, "start", "1")

    assert retried.returncode == 0, retried.stderr
    assert stdout_json(retried)["result"] == "resumed"
    assert load_only_run_state(git_repo)["status"] == "active"


def test_existing_run_records_repository_read_failure_without_network_retry(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": issue(2)}
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["repository"] = 42
    fixture.write_text(json.dumps(data), encoding="utf-8")

    failed = run_cli(git_repo, fixture, "resume", run_id)

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    state = load_only_run_state(git_repo)
    assert state["status"] == "execution_failed"
    assert state["terminal_kind"] == "execution_failed"
