from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

import agent_run.cli as cli
from agent_run.cli import build_parser, main
from agent_run.runner_promotion import PromotionVerification
from conftest import write_fixture


PROJECT_ROOT = Path(__file__).parents[1]


def test_lifecycle_help_describes_operator_boundaries() -> None:
    help_text = build_parser().format_help()

    assert "推进正常 Job Loop，停在需要操作者处理的边界" in help_text
    assert "仅恢复当前失败或 Human Blocker 的 Agent Invocation" in help_text
    assert "仅从 requeue_required 创建新的 Change Job Generation" in help_text
    assert "显示当前状态与下一条允许的操作" in help_text
    assert "显示有界 Invocation 与状态时间线" in help_text
    assert (
        "从不可变 Runner 执行一次真实 Structured Outputs promotion handshake"
        in " ".join(help_text.split())
    )


def test_promotion_preflight_failure_returns_a_cli_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(PROJECT_ROOT)

    assert main(["promotion-handshake", "not-a-sha", "--audit-file", str(tmp_path / "audit.json")]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "error"
    assert output["status"] == "blocked"


def test_immutable_runner_blocks_start_without_a_promotion_audit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    immutable_runner = PromotionVerification(
        runner_commit_sha="a" * 40,
        runner_python="/runner/bin/python",
        runner_module="/runner/lib/python/site-packages/agent_run/__init__.py",
        runner_package_sha256="sha256:runner-package",
    )
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(cli, "current_immutable_runner", lambda: immutable_runner)
    monkeypatch.setattr(
        cli,
        "require_promotion_audit",
        lambda verification, audit_file, codex_version: (_ for _ in ()).throw(
            ValueError("immutable Runner has no promotion audit")
        ),
    )

    assert main(["start", "1", "--github-fixture", str(fixture)]) == 2
    assert not (git_repo / ".agent-run").exists()


def test_source_runner_rejects_production_lifecycle_commands(
    git_repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(git_repo)

    assert main(["start", "1", "--repo", "example/project"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "error"
    assert output["status"] == "blocked"


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
    fixture: Path,
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


def load_only_run_state(repo: Path) -> dict[str, Any]:
    run_files = list((repo / ".agent-run" / "runs").glob("*.json"))
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
    return {
        "work_subject": work_subject,
        "generation": generation,
        "role": role,
        "phase": phase,
        "mode": "fresh",
        "input_fingerprint": "fixture",
        "currentness_boundary": {},
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
    assert "schema_version" not in state
    assert state["active_agent_invocation"] is None
    assert state["agent_invocation_history"] == []
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


def test_resume_rejects_a_run_without_an_agent_boundary(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]

    resumed = run_cli(git_repo, fixture, "resume", run_id)

    assert resumed.returncode == 2
    assert stdout_json(resumed)["result"] == "resumed"
    assert stdout_json(resumed)["run_id"] == run_id
    assert stdout_json(resumed)["status"] == "active"


def test_run_reports_an_incompatible_legacy_state_without_recording_a_failure(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    state["schema_version"] = 1
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "run", "1")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history"])
def test_status_and_history_reject_an_incompatible_legacy_state(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    state = load_only_run_state(git_repo)
    state["schema_version"] = 1
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history"])
@pytest.mark.parametrize("timeline", [None, {}, "not an event list"])
def test_status_and_history_reject_an_invalid_timeline_without_mutation(
    git_repo: Path, command: str, timeline: object
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    if timeline is None:
        state.pop("timeline")
    else:
        state["timeline"] = timeline
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_history_rejects_a_timeline_with_a_non_event_without_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["timeline"] = ["not an event"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "history", run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
def test_cli_rejects_a_malformed_canonical_nested_state(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["parent"].pop("number")
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)
    arguments = (command, run_id, "--json") if command != "resume" else (command, run_id)

    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert stdout_json(result)["diagnostics"][0]["code"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_agent_invocation", {"status": ["failed"]}),
        ("agent_invocation_history", ["not an invocation record"]),
    ],
)
def test_status_rejects_malformed_invocation_records(
    git_repo: Path, field: str, value: object
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "execution_failed"
    state[field] = value
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "status", run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_resume_rejects_unknown_invocation_role_without_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "execution_failed"
    state["active_agent_invocation"] = failed_invocation(
        work_subject="ticket:2", role="unknown", phase="developing"
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_resume_rejects_active_invocation_for_missing_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "execution_failed"
    state["active_agent_invocation"] = failed_invocation(
        work_subject="ticket:999", role="development", phase="developing"
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("owner", [None, {}, {"phase": "unknown"}])
def test_resume_rejects_final_publication_without_a_valid_owner(
    git_repo: Path,
    owner: dict[str, str] | None,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "execution_failed",
            "run_acceptance": {"acceptance_generation": 1},
            **({"run_publication": owner} if owner is not None else {}),
            "active_agent_invocation": failed_invocation(
                work_subject=f"run-publication:{run_id}",
                role="final_publication",
                phase="run_publication",
            ),
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
@pytest.mark.parametrize(
    "owner",
    [
        {"validation_attempts": 1},
        {"acceptance_generation": 1},
        {"acceptance_generation": 1, "validation_attempts": 1},
    ],
)
def test_resume_rejects_an_incomplete_run_acceptance_owner(
    git_repo: Path,
    owner: dict[str, int | str],
    command: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "execution_failed",
            "run_acceptance": owner,
            "active_agent_invocation": failed_invocation(
                work_subject=f"run-acceptance:{run_id}",
                role="reviewer",
                phase="run_acceptance",
            ),
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    arguments = (command, run_id, "--json") if command != "resume" else (command, run_id)
    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize(
    ("role", "phase", "owner"),
    [
        ("reviewer", "run_acceptance", {"phase": "accepted", "acceptance_generation": 1, "validation_attempts": 1}),
        ("final_publication", "run_publication", {"phase": "waiting_checks"}),
    ],
)
def test_resume_rejects_an_owner_that_has_already_advanced(
    git_repo: Path, role: str, phase: str, owner: dict[str, int | str]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "execution_failed",
            "run_acceptance": {
                "phase": "accepted",
                "acceptance_generation": 1,
                "validation_attempts": 1,
            },
            "active_agent_invocation": failed_invocation(
                work_subject=(
                    f"run-acceptance:{run_id}"
                    if role == "reviewer"
                    else f"run-publication:{run_id}"
                ),
                role=role,
                phase=phase,
            ),
        }
    )
    if role == "final_publication":
        state["run_publication"] = owner
    else:
        state["run_acceptance"] = owner
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
@pytest.mark.parametrize(
    ("role", "invocation_phase", "owner_phase"),
    [
        ("development", "developing", "accepted"),
        ("fresh_acceptance", "reviewing", "accepted"),
        ("publication", "publication", "publishing"),
    ],
)
def test_change_resume_rejects_an_owner_that_has_already_advanced(
    git_repo: Path,
    command: str,
    role: str,
    invocation_phase: str,
    owner_phase: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["ticket_jobs"]["2"].update(
        {"ticket_branch_generation": 1, "phase": owner_phase}
    )
    state.update(
        {
            "status": "execution_failed",
            "active_agent_invocation": failed_invocation(
                work_subject="ticket:2", role=role, phase=invocation_phase
            ),
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    arguments = (command, run_id, "--json") if command != "resume" else (command, run_id)
    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history"])
def test_completed_invocation_remains_a_readable_audit_snapshot(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "run_publication_pending",
            "run_acceptance": {
                "phase": "accepted",
                "acceptance_generation": 1,
                "validation_attempts": 1,
            },
            "active_agent_invocation": {
                **failed_invocation(
                    work_subject=f"run-acceptance:{run_id}",
                    role="reviewer",
                    phase="run_acceptance",
                    status="completed",
                ),
                "reported_thread_id": "reviewer-thread",
                "error": None,
                "return_code": 0,
            },
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 0
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_incompatible_state_does_not_replay_its_diagnostics(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "blocked"
    state["diagnostics"] = [{"code": "attacker", "message": "not canonical"}]
    state["parent"].pop("number")
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "status", run_id, "--json")

    assert result.returncode == 2
    output = stdout_json(result)
    assert output["status"] == "incompatible_run_state"
    assert output["diagnostics"] == [
        {
            "code": "incompatible_run_state",
            "message": "本地 Run state 不符合当前唯一 Invocation/Generation 契约；"
            "不会迁移、兼容读取或执行任何 mutation，请重新创建或清理该 Run",
        }
    ]
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_cli_rejects_malformed_human_blocker_without_mutation(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "ready_for_human",
            "parent_job": {
                "phase": "blocked",
                "blocked_reason": "agent_requires_human",
                "human_blockers": [1],
            },
        }
    )
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = run_cli(git_repo, fixture, "resume", run_id)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
def test_cli_rejects_resolved_run_with_missing_observed_revisions(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["parent"]["revision"] = None
    state["ticket_graph"]["revision"] = None
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)
    arguments = (command, run_id, "--json") if command != "resume" else (command, run_id)

    result = run_cli(git_repo, fixture, *arguments)

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_status_prints_the_recovery_command_for_manual_boundaries(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    current = load_only_run_state(git_repo)
    cases = [
        (
            "execution_failed",
            {
                "active_agent_invocation": failed_invocation(
                    work_subject="ticket:2", role="development", phase="developing"
                )
            },
            f"agent-run resume {run_id}",
        ),
        (
            "ready_for_human",
            {
                "parent_job": {
                    "phase": "blocked",
                    "blocked_reason": "agent_requires_human",
                    "human_blockers": ["Need maintainer input."],
                }
            },
            f"agent-run resume {run_id}",
        ),
        ("requeue_required", {}, f"agent-run requeue {run_id}"),
    ]

    for status, additions, expected_action in cases:
        state = deepcopy(current)
        state["status"] = status
        state.update(additions)
        if state.get("active_agent_invocation") is not None:
            state["ticket_jobs"]["2"].update(
                {"ticket_branch_generation": 1, "phase": "developing"}
            )
        state_path.write_text(json.dumps(state), encoding="utf-8")

        result = run_cli(git_repo, fixture, "status", run_id, "--json")

        assert result.returncode == 0
        assert stdout_json(result)["next_action"] == expected_action


def test_resume_does_not_refresh_a_run_without_an_agent_boundary(
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

    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "active"
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

    failed = run_cli(git_repo, fixture, "deliver", run_id)

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    state = load_only_run_state(git_repo)
    assert state["status"] == "execution_failed"
    assert state["terminal_kind"] == "execution_failed"
