from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from agent_run import cli, external_supervision
from agent_run.executor_host import HostObservation
from cli_fixtures import run_agents
from conftest import write_fixture
from test_cli import run_cli, stdout_json
from test_cli_delivery import ticket
from test_run_lifecycle import _file_snapshot, _isolated_environment


class TerminalOutput(StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def waiting_run(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, Any]]:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = run_agents(git_repo / "agents.json")
    environment = _isolated_environment(tmp_path / "waiting-status")
    started = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents),
        extra_env=environment,
    )
    assert started.returncode == 0, started.stderr
    run_id = stdout_json(started)["run_id"]
    path = git_repo / ".agent-run" / "runs" / f"{run_id}.json"
    state = json.loads(path.read_text())
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
    path.write_text(json.dumps(state))
    control_path = next((git_repo / ".agent-run/task-control").glob("*.json"))
    control = json.loads(control_path.read_text())
    control["executor"].update(status="running", pid=123)
    control_path.write_text(json.dumps(control))
    monkeypatch.setattr(
        cli, "observe_systemd_executor",
        lambda spec, *args, **kwargs: HostObservation("running", spec.generation, 123, True),
    )
    monkeypatch.chdir(git_repo)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("COLUMNS", "120")
    return git_repo, state


def status_text(run_id: str, *, plain: bool) -> str:
    output = TerminalOutput()
    with redirect_stdout(output):
        assert cli.main(["status", run_id, *(["--plain"] if plain else [])]) == 0
    return Text.from_ansi(output.getvalue()).plain


def test_waiting_status_shows_current_checks_without_restart_command(
    waiting_run: tuple[Path, dict[str, Any]],
) -> None:
    repo, state = waiting_run
    before = _file_snapshot(repo)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert f"等待 PR #{state['run_publication']['pr_number']} 的自动检查" in output
        assert "Runner 正在后台自动检查" in output
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
    assert _file_snapshot(repo) == before


@pytest.mark.parametrize("host", ["not_running", "unknown"])
def test_waiting_status_does_not_promise_an_exited_or_unknown_runner_will_continue(
    waiting_run: tuple[Path, dict[str, Any]], host: str,
) -> None:
    repo, state = waiting_run
    control_path = next((repo / ".agent-run/task-control").glob("*.json"))
    if host == "unknown":
        control_path.unlink()
    else:
        control = json.loads(control_path.read_text())
        control["executor"].update(status="exited", pid=None)
        control_path.write_text(json.dumps(control))
    before = _file_snapshot(repo)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "无需操作" not in output
        assert "Runner 正在后台自动检查" not in output
        if host == "not_running":
            assert "自动等待已停止" in output
            assert "agent-run run 1 --repo example/project" in output
        else:
            assert "无法确认 Runner 是否仍在自动等待" in output
            assert "agent-run doctor" in output
            assert "agent-run run" not in output
            assert "agent-run resume" not in output
        assert "最近 Agent" not in output
    assert _file_snapshot(repo) == before


def test_timed_out_wait_shows_human_time_and_current_recovery_action(
    waiting_run: tuple[Path, dict[str, Any]],
) -> None:
    repo, state = waiting_run
    supervisor = external_supervision.ExternalSupervisor(
        now=lambda: 1_010_000.0, sleeper=lambda _: pytest.fail("expired wait must not sleep"),
    )
    assert supervisor.before_retry(state) is False
    (repo / ".agent-run/runs" / f"{state['run_id']}.json").write_text(json.dumps(state))
    before = _file_snapshot(repo)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "已超时" in output
        assert "agent-run resume 1 --repo example/project" in output
        assert "截止=" not in output
        assert "最近 Agent" not in output
    assert _file_snapshot(repo) == before


def test_unconfigured_checks_are_not_shown_as_a_successful_ci_run(
    waiting_run: tuple[Path, dict[str, Any]],
) -> None:
    repo, state = waiting_run
    state["run_publication"]["required_checks_evidence"].update(result="none", checks=[])
    (repo / ".agent-run/runs" / f"{state['run_id']}.json").write_text(json.dumps(state))
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "未配置合并前自动检查" in output
        assert "自动检查结果：已通过" not in output
        assert "quality" not in output


def test_waiting_status_does_not_invent_time_after_clock_discontinuity(
    waiting_run: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, state = waiting_run
    monkeypatch.setattr(external_supervision, "monotonic", lambda: 998_000.0)
    for plain in (True, False):
        output = status_text(state["run_id"], plain=plain)
        assert "已等待：未知" in output
        assert "本轮最多还可等待：未知" in output
