from __future__ import annotations

from copy import deepcopy

import pytest

from agent_run.agent_invocation import canonical_fingerprint
from agent_run.delivery_status import current_acceptance_artifact
from agent_run.presentation_helpers import execution_duration
from agent_run.status_actions import controller_work, status_heading


def publication_state() -> dict:
    boundary = {"reviewed_head_sha": "head", "parent_revision": "p1", "ticket_graph_revision": "g1"}
    attempt = {"attempt_id": "a1", "role": "publication", "generation": 1,
               "currentness_boundary_fingerprint": canonical_fingerprint(boundary)}
    return {
        "run_id": "r1", "status": "run_publication_pending", "parent": {"revision": "p1"},
        "ticket_graph": {"revision": "g1"},
        "run_publication": {"phase": "publishing", "pending_semantic_attempt": attempt},
        "run_acceptance": {"phase": "accepted", "acceptance_generation": 1, "reviewed_head_sha": "head",
                           "acceptance_record": {"reviewed_head_sha": "head", "artifact": {"checks": {"spec": {"status": "pass"}}}}},
        "active_agent_invocation": {"work_subject": "run-publication:r1", "generation": 1,
                             "semantic_attempt": deepcopy(attempt), "currentness_boundary": boundary},
    }


def test_publication_verdict_uses_real_invocation_boundary() -> None:
    state = publication_state()
    assert current_acceptance_artifact(state) == state["run_acceptance"]["acceptance_record"]["artifact"]


@pytest.mark.parametrize("change", ["subject", "attempt", "generation", "fingerprint", "parent", "graph", "head"])
def test_publication_verdict_rejects_stale_boundary(change: str) -> None:
    state = publication_state()
    invocation = state["active_agent_invocation"]
    if change == "subject":
        invocation["work_subject"] = "run-publication:other"
    elif change == "attempt":
        invocation["semantic_attempt"]["attempt_id"] = "other"
    elif change == "generation":
        invocation["generation"] = 2
    elif change == "fingerprint":
        invocation["currentness_boundary"]["extra"] = True
    elif change == "parent":
        state["parent"]["revision"] = "p2"
    elif change == "graph":
        state["ticket_graph"]["revision"] = "g2"
    else:
        state["run_acceptance"]["reviewed_head_sha"] = "new-head"
    assert current_acceptance_artifact(state) is None


@pytest.mark.parametrize("activity, expected", [("running", "程序正在自动保存代码；无需操作。"), ("unknown", None), ("not_running", None)])
def test_controller_saving_requires_live_control(activity: str, expected: str | None) -> None:
    state = {"status": "active", "parent_job": {"phase": "committing_candidate"}}
    assert controller_work(state, {"executor_control": {"activity": activity}}) == expected


def test_active_run_acceptance_heading_is_not_waiting() -> None:
    state = {"status": "run_acceptance_pending", "run_acceptance": {"phase": "reviewing"}}
    assert status_heading(state, {"execution_activity": "running"}, "等待整体验收") == "正在验收整体需求"
    assert status_heading(state, {"execution_activity": "unknown"}, "无法确认") == "无法确认"


def test_execution_duration_preserves_seconds() -> None:
    assert execution_duration(475) == "7 分 55 秒"
    assert execution_duration(1748) == "29 分 8 秒"


@pytest.mark.parametrize("rich", [False, True])
def test_status_renders_controller_work_without_resume_command(capsys, monkeypatch, rich: bool) -> None:
    from agent_run.delivery_status import print_rich_status_progress, print_status_progress, status_progress_view
    from agent_run.presentation_helpers import human_status_term

    state = {"language": "zh", "run_id": "r1", "repository": "example/project", "parent": {"number": 1},
             "status": "active", "parent_job": {"phase": "committing_candidate"}}
    audit = {"status": "active", "phase": "committing_candidate",
             "executor_control": {"activity": "running"}, "next_action": "agent-run run 1"}
    view = status_progress_view(state, audit)
    monkeypatch.setenv("COLUMNS", "160")
    if rich:
        print_rich_status_progress(state, audit, view)
    else:
        print_status_progress(state, audit, view, display_term=human_status_term, print_operator_action=lambda action: None)
    output = capsys.readouterr().out
    assert "程序正在自动保存代码；无需操作。" in output
    assert "agent-run run 1" not in output
    assert "按上述命令继续" not in output


@pytest.mark.parametrize("details", [False, True])
def test_history_single_result_and_exact_round_time(capsys, details: bool) -> None:
    from agent_run.cli_presentation import _print_history

    attempt = {"attempt_id": "a", "role": "development", "work_subject": "parent:1", "ordinal": 1,
               "status": "completed", "outcome": "candidate"}
    invocation = {"semantic_attempt": attempt, "work_subject": "parent:1", "role": "development",
                  "status": "completed", "started_at": "2026-09-15T01:00:00+00:00",
                  "ended_at": "2026-09-15T01:07:55+00:00", "model": "test-model", "reasoning_effort": "high"}
    state = {"language": "zh", "run_id": "r", "status": "completed", "parent": {"number": 1},
             "agent_invocation_history": [invocation]}
    _print_history(state, as_json=False, plain=True, details=details)
    output = capsys.readouterr().out
    assert "本轮执行耗时：7 分 55 秒" in output
    assert "；结果：" not in output
    assert "配置 1" not in output
    assert "test-model" in output and "high" in output


@pytest.mark.parametrize("plain, details", [(True, False), (False, False), (True, True), (False, True)])
def test_history_omits_terminal_elapsed_without_end(capsys, monkeypatch, plain: bool, details: bool) -> None:
    from agent_run.cli_presentation import _print_history
    from agent_run.delivery_history import run_elapsed_seconds

    monkeypatch.setenv("COLUMNS", "160")
    state = {"language": "zh", "run_id": "r", "status": "completed", "parent": {"number": 1},
             "created_at": "2026-09-15T01:00:00+00:00"}
    assert run_elapsed_seconds(state) is None
    state["timeline"] = [{"at": "2026-09-15T01:01:00+00:00", "status": "active"}]
    assert run_elapsed_seconds(state) is None
    _print_history(state, as_json=False, plain=plain, details=details)
    assert "任务历时" not in capsys.readouterr().out


@pytest.mark.parametrize("rich", [False, True])
def test_status_omits_terminal_elapsed_without_end(capsys, rich: bool) -> None:
    from agent_run.delivery_status import print_rich_status_progress, print_status_progress, status_progress_view
    from agent_run.presentation_helpers import human_status_term

    state = {"language": "zh", "run_id": "r", "status": "completed", "parent": {"number": 1},
             "created_at": "2026-09-15T01:00:00+00:00"}
    audit = {"status": "completed"}
    view = status_progress_view(state, audit)
    if rich:
        print_rich_status_progress(state, audit, view)
    else:
        print_status_progress(state, audit, view, display_term=human_status_term, print_operator_action=lambda action: None)
    assert "任务历时" not in capsys.readouterr().out
