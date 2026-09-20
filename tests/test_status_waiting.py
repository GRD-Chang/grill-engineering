from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from support.workspace import managed_repo, managed_state, prepare_workspace

from agent_run import cli, external_supervision
from agent_run.executor_host import HostObservation
from agent_run.run_lifecycle import prepare_action_application_receipt
from agent_run.run_locator import RunLocatorIndex
from agent_run.semantic_attempt import allocate_semantic_attempt
from agent_run.state import StateStore
from agent_run.task_control import TaskControlStore, TaskKey
from conftest import write_fixture
from test_run_lifecycle import _file_snapshot, _isolated_environment
from support.inprocess_cli import invoke_cli_inprocess
from support.published_run import prepare_published_run


def _workspace_snapshot(repo: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    return _file_snapshot(repo), _file_snapshot(managed_repo(repo).parent)


class TerminalOutput(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def waiting_run(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, Any]]:
    environment = _isolated_environment(tmp_path / "waiting-status")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    # These cases inspect an existing publication. Ticket execution and process
    # startup remain covered by the public run/lifecycle integration tests.
    _, state = prepare_published_run(git_repo)
    run_id = state["run_id"]
    RunLocatorIndex.default().register(
        run_id=run_id,
        repository_root=managed_repo(git_repo),
        state_dir=managed_state(git_repo),
        repository=state["repository"],
        parent_number=state["parent"]["number"],
    )
    path = managed_state(git_repo) / "runs" / f"{run_id}.json"
    publication = state["run_publication"]
    publication["phase"] = "waiting_checks"
    publication["required_checks_evidence"] = {
        "pr_number": publication["pr_number"],
        "head_sha": publication["required_checks_evidence"]["head_sha"],
        "result": "pending",
        "checks": [{"name": "quality", "bucket": "pending"}],
    }
    state.update(status="waiting_checks", terminal_kind="waiting_external", diagnostics=[])
    monkeypatch.setattr(external_supervision, "monotonic", lambda: 1_000_000.0)
    external_supervision.ensure_supervision_window(state, now=lambda: 999_760.0)
    control = TaskControlStore(managed_state(git_repo))
    task = TaskKey(managed_repo(git_repo), state["repository"], state["parent"]["number"])
    claim = control.claim_action(task, kind="run", payload={"parent": 1})
    assert claim.action is not None and claim.action_id is not None
    control.bind_run(task, claim.action_id, run_id)
    prepare_action_application_receipt(state, claim.action)
    path.write_text(json.dumps(state))
    control.record_application(
        task, action_id=claim.action_id, run_id=run_id,
        payload_digest=claim.action["payload_digest"],
    )
    reservation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id)
    control.mark_process_started(
        task, action_id=claim.action_id, generation=reservation.generation,
        pid=123, process_start_token=None,
    )
    control.mark_handshake(
        task, action_id=claim.action_id, generation=reservation.generation,
        pid=123, process_start_token=None,
    )
    control.complete_action(task, action_id=claim.action_id, result_status=state["status"])
    monkeypatch.setattr(
        cli, "observe_systemd_executor",
        lambda spec, *args, **kwargs: HostObservation("running", spec.generation, 123, True),
    )
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    return git_repo, state


def status_text(run_id: str, *, plain: bool) -> str:
    output = TerminalOutput()
    with redirect_stdout(output):
        result = cli.main(["status", run_id, *(["--plain"] if plain else [])])
    assert result == 0, output.getvalue()
    return Text.from_ansi(output.getvalue()).plain


@pytest.mark.parametrize("plain", [True, False])
def test_running_agent_status_does_not_offer_a_duplicate_run_command(
    waiting_run: tuple[Path, dict[str, Any]], plain: bool,
) -> None:
    repo, state = waiting_run
    publication = state["run_publication"]
    publication["phase"] = "publishing"
    state["status"] = "run_publication_pending"
    state.pop("supervision_window", None)
    state.pop("supervision_wait", None)
    subject = f"run-publication:{state['run_id']}"
    generation = state["run_acceptance"]["acceptance_generation"]
    attempt = allocate_semantic_attempt(
        publication, role="publication", work_subject=subject, generation=generation,
        currentness_boundary={}, ordinal=2,
    )
    state["active_agent_invocation"] = {
        "work_subject": subject, "role": "final_publication", "phase": "run_publication",
        "generation": generation, "mode": "fresh", "input_fingerprint": "status-running",
        "currentness_boundary": {}, "semantic_attempt": attempt, "attempt_count": 1,
        "status": "running", "started_at": state["created_at"],
        "model": "gpt-5.6-luna", "reasoning_effort": "xhigh",
    }
    path = managed_state(repo) / "runs" / f"{state['run_id']}.json"
    path.write_text(json.dumps(state))
    before = _workspace_snapshot(repo)
    human = status_text(state["run_id"], plain=plain)
    assert "你暂时无需操作" in human
    assert "gpt-5.6-luna" in human and "xhigh" in human
    assert "agent-run run" not in human
    audit_output = StringIO()
    with redirect_stdout(audit_output):
        assert cli.main(["status", state["run_id"], "--json"]) == 0
    audit = json.loads(audit_output.getvalue())
    assert audit["progress"]["execution_activity"] == "running"
    assert audit["operator_action"] is None
    assert audit["next_action"] == "agent-run run 1"
    assert _workspace_snapshot(repo) == before


def test_waiting_status_shows_current_checks_without_restart_command(
    waiting_run: tuple[Path, dict[str, Any]],
) -> None:
    repo, state = waiting_run
    before = _workspace_snapshot(repo)
    before_user_state = _file_snapshot(Path.home().parent)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert f"等待 PR #{state['run_publication']['pr_number']} 的合并前检查" in output
        assert "正在后台检查合并前检查结果" in output
        assert "无需操作" in output
        assert "已等待：4 分钟" in output
        assert "本轮最多还可等待：41 分钟" in output
        assert "quality" in output
        for misleading in (
            "最近 Agent", "发布 Agent", "Publication Agent", "截止=", "剩余=",
            "agent-run run", "agent-run resume", "超时恢复", "必需检查", "按上述命令继续",
        ):
            assert misleading not in output
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(["status", state["run_id"], "--json"]) == 0
    audit = json.loads(output.getvalue())
    assert audit["supervision"]["deadline"] == 1_002_460.0
    assert audit["supervision"]["remaining_seconds"] == 2460
    assert audit["progress"]["current_agent"]["is_active"] is False
    assert audit["executor_control"]["activity"] == "running"
    assert _workspace_snapshot(repo) == before
    assert _file_snapshot(Path.home().parent) == before_user_state


@pytest.mark.parametrize("host", ["not_running", "unknown"])
def test_waiting_status_does_not_promise_an_exited_or_unknown_runner_will_continue(
    waiting_run: tuple[Path, dict[str, Any]], host: str,
) -> None:
    repo, state = waiting_run
    control_path = next((managed_state(repo) / "task-control").glob("*.json"))
    if host == "unknown":
        control_path.unlink()
    else:
        control = json.loads(control_path.read_text())
        control["executor"].update(status="exited", pid=None)
        control_path.write_text(json.dumps(control))
    before = _workspace_snapshot(repo)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "无需操作" not in output
        assert "正在后台检查合并前检查结果" not in output
        if host == "not_running":
            assert "自动等待已停止" in output
            assert "agent-run run 1 --repo example/project" in output
        else:
            assert "无法确认后台等待是否仍在继续" in output
            assert "agent-run status --repo example/project --parent 1 --json" in output
            assert any(line.strip() in {
                "命令：agent-run status --repo example/project --parent 1 --json",
                "诊断命令: agent-run status --repo example/project --parent 1 --json",
            } for line in output.splitlines())
            assert "先核验原执行的归属和退出状态" in output
            assert "agent-run doctor" not in output
            assert "agent-run run" not in output
            assert "agent-run resume" not in output
        assert "最近 Agent" not in output
    assert _workspace_snapshot(repo) == before


def test_timed_out_wait_shows_human_time_and_current_recovery_action(
    waiting_run: tuple[Path, dict[str, Any]],
) -> None:
    repo, state = waiting_run
    supervisor = external_supervision.ExternalSupervisor(
        now=lambda: 1_010_000.0, sleeper=lambda _: pytest.fail("expired wait must not sleep"),
    )
    assert supervisor.before_retry(state) is False
    (managed_state(repo) / "runs" / f"{state['run_id']}.json").write_text(json.dumps(state))
    before = _workspace_snapshot(repo)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "已超时" in output
        assert "agent-run resume 1 --repo example/project" in output
        assert "截止=" not in output
        assert "最近 Agent" not in output
    assert _workspace_snapshot(repo) == before


def test_unconfigured_checks_are_not_shown_as_a_successful_ci_run(
    waiting_run: tuple[Path, dict[str, Any]],
) -> None:
    repo, state = waiting_run
    state["run_publication"]["required_checks_evidence"].update(result="none", checks=[])
    (managed_state(repo) / "runs" / f"{state['run_id']}.json").write_text(json.dumps(state))
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "未配置合并前检查" in output
        assert "合并前检查结果：已通过" not in output
        assert "quality" not in output


def test_credential_wait_explains_failure_without_raw_failure_class(
    waiting_run: tuple[Path, dict[str, Any]],
) -> None:
    repo, state = waiting_run
    state["credential_availability"] = {
        "failure_class": "credential_unavailable", "retry_count": 2, "http_status": 503,
    }
    external_supervision.ensure_supervision_window(state, now=lambda: 999_760.0)
    supervisor = external_supervision.ExternalSupervisor(
        now=lambda: 1_010_000.0, sleeper=lambda _: pytest.fail("expired wait must not sleep"),
    )
    assert supervisor.before_retry(state) is False
    path = managed_state(repo) / "runs" / f"{state['run_id']}.json"
    path.write_text(json.dumps(state))
    before = _workspace_snapshot(repo)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "暂时无法取得 GitHub 工作凭据" in output
        assert "重试次数：2" in output
        assert "credential_unavailable" not in output
        assert "凭据 HTTP 状态" not in output
        assert "agent-run resume 1 --repo example/project" in output
    assert _workspace_snapshot(repo) == before


def test_waiting_status_does_not_invent_time_after_clock_discontinuity(
    waiting_run: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, state = waiting_run
    monkeypatch.setattr(external_supervision, "monotonic", lambda: 998_000.0)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "已等待：未知" in output
        assert "本轮最多还可等待：未知" in output


def test_completed_cleanup_pending_never_says_no_action_needed(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    prepare_workspace(git_repo)
    StateStore(managed_state(git_repo)).save_run("run-1", {
        "schema_version": 1, "language": "zh", "run_id": "run-1", "status": "completed",
        "repository": "example/project", "parent": {"number": 1},
        "diagnostics": [], "delivery_cleanup": {
            "status": "cleanup_pending", "last_error": "source head changed",
            "items": {"agent-run/ticket-3": {
                "kind": "ticket", "branch": "agent-run/ticket-3",
                "checkout": "/preserved/worktree", "status": "cleanup_pending",
                "last_error": "source head changed",
            }},
        },
    })
    before = _workspace_snapshot(git_repo)
    for command in ("status", "history"):
        result = invoke_cli_inprocess(git_repo, fixture, command, "run-1", "--plain")
        assert result.returncode == 0, result.stderr
        assert "无需操作" not in result.stdout
        assert "已合并，待清理" in result.stdout
        assert "agent-run resume" in result.stdout
        if command == "status":
            assert "提交已变化" in result.stdout
            assert "/preserved/worktree" in result.stdout
    monkeypatch.chdir(git_repo)
    output = TerminalOutput()
    with redirect_stdout(output):
        assert cli.main(["status", "run-1", "--github-fixture", str(fixture)]) == 0
    assert "提交已变化" in Text.from_ansi(output.getvalue()).plain
    assert "/preserved/worktree" in Text.from_ansi(output.getvalue()).plain
    assert _workspace_snapshot(git_repo) == before
