from __future__ import annotations

import json
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from agent_run import cli
from agent_run.run_locator import RunLocatorIndex
from agent_run.state import StateStore
from support.workspace import prepare_workspace
from test_run_lifecycle import _file_snapshot


class TerminalOutput(StringIO):
    def isatty(self) -> bool:
        return True


def _save_run(repo: Path, state: dict[str, Any]) -> Path:
    workspace = prepare_workspace(repo)
    StateStore(workspace.state_root).save_run(state["run_id"], state)
    RunLocatorIndex.default().register(
        run_id=state["run_id"],
        repository_root=workspace.repository_root,
        state_dir=workspace.state_root,
        repository=state["repository"],
        parent_number=state["parent"]["number"],
    )
    return workspace.state_root


@pytest.mark.parametrize("mode, completed, status, acceptance, publication, expected", [
    ("parent_only", 0, "active", None, None, "无子任务，直接推进需求"),
    (None, 0, "active", None, None, "无法确认进度"),
    (None, 1, "active", None, None, "无法确认进度"),
    ("ticket_run", 0, "active", None, None, "无法确认进度"),
    ("ticket_run", 1, "active", None, None, "已完成 1 / 2 个子任务"),
    ("ticket_run", 2, "active", "pending", None, "已完成 2 / 2 个子任务，还要验收并发布"),
    ("ticket_run", 2, "run_acceptance_pending", "reviewing", None, "已完成 2 / 2 个子任务，正在验收"),
    ("ticket_run", 2, "run_approval_pending", "accepted", "ready_for_approval", "已完成 2 / 2 个子任务，等待你确认后发布"),
    ("ticket_run", 2, "run_publication_pending", "accepted", "publishing", "已完成 2 / 2 个子任务，正在发布"),
    ("ticket_run", 2, "completed", "accepted", "merged", "已完成 2 / 2 个子任务"),
    ("ticket_run", 2, "blocked", "reviewing", None, "已完成 2 / 2 个子任务"),
    ("ticket_run", 2, "execution_failed", "accepted", "publishing", "已完成 2 / 2 个子任务"),
    ("ticket_run", 2, "run_publication_pending", "accepted", "waiting_checks", "已完成 2 / 2 个子任务"),
    ("ticket_run", 2, "ready_for_human", "ready_for_human", None, "已完成 2 / 2 个子任务"),
])
@pytest.mark.parametrize("plain", [True, False])
def test_delivery_progress_preserves_audit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, plain: bool,
    mode: str | None, completed: int, status: str, acceptance: str | None,
    publication: str | None, expected: str,
) -> None:
    state = {
        "run_id": "run-progress", "schema_version": 1, "language": "zh",
        "repository": "example/project", "parent": {"number": 1},
        "delivery_type": mode, "status": status, "diagnostics": [],
    }
    if completed:
        state["ticket_graph"] = {"tickets": {"2": {}, "3": {}}}
        state["ticket_jobs"] = {
            "2": {"phase": "completed"},
            "3": {"phase": "completed" if completed == 2 else "developing"},
        }
    if acceptance:
        state["run_acceptance"] = {"phase": acceptance}
    if publication:
        state["run_publication"] = {"phase": publication}
    root = _save_run(git_repo, state)
    before = _file_snapshot(root)
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("COLUMNS", "160")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TerminalOutput()
    with redirect_stdout(output):
        assert cli.main(["status", state["run_id"], *(["--plain"] if plain else [])]) == 0
    human = Text.from_ansi(output.getvalue()).plain
    assert expected in human
    if expected == "已完成 2 / 2 个子任务":
        assert "已完成 2 / 2 个子任务，" not in human
    audit_output = StringIO()
    with redirect_stdout(audit_output):
        assert cli.main(["status", state["run_id"], "--json"]) == 0
    progress = json.loads(audit_output.getvalue())["progress"]
    assert set(progress) == {
        "repository", "parent", "status", "phase", "current_object",
        "ticket_progress", "round_progress", "run_repair", "elapsed_seconds",
        "current_agent", "execution_activity", "findings", "conclusion", "next_action",
    }
    assert progress["ticket_progress"] == {
        "completed": completed, "total": 2 if completed else 0,
    }
    assert _file_snapshot(root) == before


@pytest.mark.parametrize("plain", [True, False])
def test_completed_status_requires_cleanup_when_closeout_crashed_before_scheduling(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, plain: bool,
) -> None:
    state = {
        "run_id": "run-cleanup-not-scheduled", "schema_version": 1, "language": "zh",
        "repository": "example/project", "parent": {"number": 1},
        "status": "completed", "run_branch": "agent-run/final-delivery",
        "run_publication": {
            "phase": "merged", "approval_grant": {}, "parent_closed": True,
        }, "diagnostics": [],
    }
    root = _save_run(git_repo, state)
    before = _file_snapshot(root)
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("COLUMNS", "160")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TerminalOutput()
    with redirect_stdout(output):
        assert cli.main(["status", state["run_id"], *(["--plain"] if plain else [])]) == 0
    human = Text.from_ansi(output.getvalue()).plain
    assert "已合并，待清理" in human
    assert "agent-run resume 1 --repo example/project" in human
    assert "无需操作" not in human
    assert "工作区清理：已完成" not in human
    audit_output = StringIO()
    with redirect_stdout(audit_output):
        assert cli.main(["status", state["run_id"], "--json"]) == 0
    audit = json.loads(audit_output.getvalue())
    assert audit["status"] == "completed"
    assert audit["delivery_cleanup"] is None
    assert _file_snapshot(root) == before


@pytest.mark.parametrize("plain", [True, False])
def test_completed_status_keeps_work_facts_without_internal_labels(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, plain: bool,
) -> None:
    state = {
        "run_id": "run-language", "schema_version": 1, "language": "zh", "repository": "example/project",
        "parent": {"number": 1, "title": "整体需求"}, "status": "completed",
        "run_acceptance": {
            "phase": "accepted", "candidate_sha": "accepted-head",
            "modification_attempts": 0, "validation_attempts": 1,
            "review_budget": {"window": 1, "development_attempts": 0,
                              "reviewer_invocations": 1, "final_ci_fix_used": False},
            "acceptance_record": {
                "reviewed_candidate_sha": "accepted-head",
                "artifact": {"checks": {lane: {"status": "pass", "findings": []}
                                        for lane in ("e2e", "standards", "spec")}},
            },
        },
        "run_publication": {"phase": "merged", "head_sha": "accepted-head"},
        "agent_invocation_history": [{
            "role": "final_publication", "work_subject": "run-publication:run-language",
            "status": "completed", "model": "gpt-5.6-luna", "reasoning_effort": "xhigh",
            "started_at": "2026-09-12T15:20:14.737344+00:00",
            "ended_at": "2026-09-12T15:22:14.737344+00:00",
        }],
        "delivery_cleanup": {"status": "completed", "items": {}}, "diagnostics": [],
    }
    root = _save_run(git_repo, state)
    before = _file_snapshot(root)
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("COLUMNS", "160")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    try:
        with monkeypatch.context() as timezone:
            timezone.setenv("TZ", "Asia/Shanghai")
            time.tzset()
            output = TerminalOutput()
            with redirect_stdout(output):
                assert cli.main(["status", state["run_id"], *(["--plain"] if plain else [])]) == 0
    finally:
        time.tzset()
    human = Text.from_ansi(output.getvalue()).plain
    for expected in (
        "整体交付", "任务已完成", "当前版本已通过验收",
        "工作区清理", "已完成", "无需操作",
    ):
        assert expected in human
    for internal in (
        "Run Publication", "Publication Agent", "Attempt #", "预算窗口", "False /",
        "Findings", "Candidate", "状态: completed", "已保留 0", "恢复操作见 --json",
        "2026-09-12T15:20", "run-language", "命令：无",
        "发布 Agent", "gpt-5.6-luna", "本次授权已用",
    ):
        assert internal not in human
    audit_output = StringIO()
    with redirect_stdout(audit_output):
        assert cli.main(["status", state["run_id"], "--json"]) == 0
    audit = json.loads(audit_output.getvalue())
    assert audit["progress"]["current_object"] == "Run Publication"
    assert audit["progress"]["conclusion"] == "当前有效通过"
    assert audit["progress"]["current_agent"]["started_at"] == "2026-09-12T15:20:14.737344+00:00"
    assert audit["review_budget"]["final_ci_fix_used"] is False
    assert audit["review_budget"]["final_ci_fix_limit"] == 0
    assert _file_snapshot(root) == before


@pytest.mark.parametrize(("status", "command"), [
    ("unsupported_scope_change", "abandon"), ("deterministic_contradiction", "run"),
    ("publication_pending", "abandon"),
])
def test_status_qualifies_manual_abandon_guidance_without_changing_json(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, status: str, command: str,
) -> None:
    state = {
        "run_id": "run-action-language", "schema_version": 1, "language": "zh", "repository": "example/project",
        "parent": {"number": 1}, "status": status, "diagnostics": [],
    }
    root = _save_run(git_repo, state)
    before = _file_snapshot(root)
    monkeypatch.chdir(git_repo)
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(["status", state["run_id"], "--plain"]) == 0
    assert f"agent-run {command} 1 --repo example/project" in output.getvalue()
    assert "确定性外部矛盾" not in output.getvalue()
    audit_output = StringIO()
    with redirect_stdout(audit_output):
        assert cli.main(["status", state["run_id"], "--json"]) == 0
    assert "agent-run abandon 1 --repo" not in json.loads(audit_output.getvalue())["next_action"]
    assert _file_snapshot(root) == before


@pytest.mark.parametrize("plain", [True, False])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_repair_blocker_shows_action_and_work_facts_without_control_identifiers(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, plain: bool, language: str,
) -> None:
    state = {
        "run_id": "run-repair-language", "schema_version": 1, "language": language, "repository": "example/project",
        "parent": {"number": 1}, "status": "ready_for_human", "diagnostics": [],
        "run_acceptance": {
            "phase": "repairing", "acceptance_generation": 31,
            "repair_cycle": {"generation": 72, "status": "active", "code_modification_attempts": 2},
            "repair_job": {
                "repair_generation": 99, "phase": "blocked", "candidate_sha": "private-candidate-sha",
                "blocked_reason": "agent_requires_human", "human_blocker_phase": "reviewing",
                "human_blockers": ["等待人工处理 / 原始 evidence"],
                "repair_checkout": "/preserved/development",
                "review_budget": {"window": 4, "development_attempts": 2,
                                  "reviewer_invocations": 1, "final_ci_fix_used": False},
            },
        },
        "agent_invocation_history": [{
            "role": "reviewer", "work_subject": "run-repair:run-repair-language",
            "status": "completed", "model": "review-model", "reasoning_effort": "high",
            "started_at": "2026-09-12T15:20:00+00:00", "ended_at": "2026-09-12T15:21:00+00:00",
        }],
    }
    root = _save_run(git_repo, state)
    with redirect_stdout(StringIO()):
        assert cli.main(["settings", "configure", "--language", "en" if language == "zh" else "zh", "--json"]) == 0
    before = _file_snapshot(root)
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "140")
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = TerminalOutput()
    with redirect_stdout(output):
        assert cli.main(["status", state["run_id"], *(["--plain"] if plain else [])]) == 0
    human = Text.from_ansi(output.getvalue()).plain
    readable = " ".join(human.replace("│", " ").split())
    expected_terms = (
        "整体修复", "代码修改 2 / 10", "当前版本验收", "需要人工处理", "验收中",
        "review-model", "high", "等待人工处理 / 原始 evidence",
        "当前代码版本已保存", "开发工作区已保留", "整项任务已暂停，其他子任务也不会继续",
        "agent-run resume 1 --repo example/project",
    ) if language == "zh" else (
        "Run repair", "Code modifications", "Human action required", "Review Agent",
        "review-model", "high", "等待人工处理 / 原始 evidence",
        "Current candidate saved", "Development workspace preserved",
        "agent-run resume 1 --repo example/project",
    )
    for expected in expected_terms:
        assert expected.casefold() in readable.casefold()
    for internal in (
        "Generation", "generation", "Human Blocker", "Candidate", "Managed Checkout",
        "Delivery Run", "reviewer", "run-repair-language", "private-candidate-sha",
    ):
        assert internal not in human
    assert _file_snapshot(root) == before


def test_status_json_is_identical_across_run_languages(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "run_id": "run-json-language", "schema_version": 1,
        "repository": "example/project", "parent": {"number": 1, "title": "等待人工处理 / raw title"},
        "status": "execution_failed", "diagnostics": [],
        "active_ticket_job": {"ticket_number": 2, "phase": "execution_failed"},
    }
    monkeypatch.chdir(git_repo)
    outputs = []
    for language in ("zh", "en"):
        state["language"] = language
        root = _save_run(git_repo, state)
        before = _file_snapshot(root)
        output = StringIO()
        with redirect_stdout(output):
            assert cli.main(["status", state["run_id"], "--json"]) == 0
        outputs.append(json.loads(output.getvalue()))
        assert _file_snapshot(root) == before
    assert outputs[0] == outputs[1]


def test_missing_run_language_is_reported_without_guessing_or_writing(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "run_id": "run-missing-language", "schema_version": 1,
        "repository": "example/project", "parent": {"number": 1},
        "status": "active", "diagnostics": [],
    }
    root = _save_run(git_repo, state)
    before = _file_snapshot(root)
    monkeypatch.chdir(git_repo)
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(["status", state["run_id"], "--plain"]) == 2
    assert "language" in output.getvalue()
    assert _file_snapshot(root) == before
