from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.text import Text

from support.workspace import managed_state

from agent_run.cli import main
from conftest import seed_run, write_fixture


class Terminal(StringIO):
    def isatty(self) -> bool:
        return True


def publication_events() -> list[dict[str, Any]]:
    common = {
        "kind": "run_publication", "semantic_attempt_id": "publication-1",
        "status": "waiting_external", "phase": "waiting_external",
        "external_wait_reason": {
            "code": "github_write_pending", "message": "正在确认 Final Run PR 写入结果",
        },
    }
    return [
        {**common, "at": "2026-09-12T15:24:34+00:00", "github_write_action": "ensure_final_run_ref"},
        {**common, "at": "2026-09-12T15:25:28+00:00", "github_write_action": "create_final_pr"},
        {
            "at": "2026-09-12T15:26:52+00:00", "kind": "run_publication",
            "semantic_attempt_id": "publication-1", "status": "run_approval_pending",
            "phase": "ready_for_approval", "pr_number": 4,
            "required_checks_result": "pass", "required_checks_head_sha": "a" * 40,
            "required_checks_run_identity": "check-run-1", "required_checks_signature": "pass-1",
            "history_work_subject": "run-publication:run-example",
            "history_generation": 1, "history_budget_window": 1,
            "required_checks_evidence": {"checks": [{
                "name": "test", "bucket": "pass", "state": "SUCCESS", "workflow": "tests",
                "link": "https://github.com/o/r/actions/runs/1/job/2",
            }]},
        },
    ]


def save_history(repo: Path, events: list[dict[str, Any]]) -> tuple[str, Path]:
    fixture = write_fixture(repo / "github.json", issues={})
    started = seed_run(repo, fixture, idle_control=True)
    assert started.returncode == 0, started.stderr
    run_id = json.loads(started.stdout)["run_id"]
    path = managed_state(repo) / "runs" / f"{run_id}.json"
    state = json.loads(path.read_text())
    state["timeline"] = events
    path.write_text(json.dumps(state))
    return run_id, path


def render(run_id: str, *arguments: str) -> str:
    output = Terminal()
    with redirect_stdout(output):
        assert main(["history", run_id, *arguments]) == 0
    return Text.from_ansi(output.getvalue()).plain


def test_history_groups_final_pr_preparation_without_hiding_checks_or_raw_events(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = publication_events()
    run_id, path = save_history(git_repo, events)
    before = path.read_bytes()
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("COLUMNS", "120")
    for options in (("--plain",), ("--plain", "--details"), (), ("--details",)):
        output = render(run_id, *options)
        assert output.count("最终 PR #4 已创建") == 1
        assert "合并前检查通过" in output
        assert "等待人工批准" in output
        assert "test" in output
        for internal in ("关键节点", "监督边界", "Run Publication", "github_write_pending", "a" * 40):
            assert internal not in output
        if "--details" in options:
            assert "准备远端分支" in output
            assert "创建 PR" in output
            assert "https://github.com/o/r/actions/runs/1/job/2" in output
    audit = json.loads(render(run_id, "--json"))
    assert audit["timeline"] == events
    assert path.read_bytes() == before


@pytest.mark.parametrize("boundary", ["failure", "different_attempt", "unfinished"])
def test_history_does_not_invent_pr_creation_across_unproven_or_failed_steps(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    events = publication_events()
    if boundary == "unfinished":
        events.pop()
    elif boundary == "different_attempt":
        events[1]["semantic_attempt_id"] = "other-publication"
    else:
        events.insert(1, {
            **events[0], "at": "2026-09-12T15:25:00+00:00",
            "status": "execution_failed", "result": "github_write_failed",
            "external_wait_reason": {"code": "github_write_failed", "message": "远端分支写入失败"},
        })
    run_id, _ = save_history(git_repo, events)
    monkeypatch.chdir(git_repo)
    output = render(run_id, "--plain", "--details")
    assert "最终 PR #4 已创建" not in output
    assert "准备远端分支" in output
    assert "创建最终 PR" in output
    if boundary == "failure":
        assert "远端分支写入失败" in output


def test_abandoned_history_does_not_claim_successful_delivery(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [{"at": "2026-09-12T15:24:34+00:00", "kind": "run_publication",
               "status": "abandoned", "phase": "abandoned"}]
    run_id, _ = save_history(git_repo, events)
    monkeypatch.chdir(git_repo)
    output = render(run_id, "--plain")
    assert "交付已放弃" in output
    assert "整体交付完成" not in output
