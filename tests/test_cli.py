from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pytest

import agent_run.cli as cli
import agent_run.doctor as doctor
from agent_run.agent_invocation import invocation_event_recorder
from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.cli import build_parser, main
from agent_run.controller import Controller
from agent_run.codex import CodexProcessError
from agent_run.delivery_history import history_records
from agent_run.delivery_status import _status_style, invocation_execution_seconds
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader, GitHubReadError
from agent_run.operator_action_presentation import print_operator_action
from agent_run.presentation_helpers import human_next_action
from agent_run.requeue import RequeueError
from agent_run.run_driver import DirectRunOperations, RunStep
from agent_run.semantic_attempt import canonical_fingerprint
from agent_run.state import FaultInjectingStateStore, StateStore
from agent_run.worker_sandbox import WorkerSandboxError
from conftest import seed_run, write_fixture
from support.inprocess_cli import invoke_cli_inprocess


PROJECT_ROOT = Path(__file__).parents[1]
_DOCTOR_TIMEOUT_TEST_LIMIT_SECONDS = 5


def _canonical_run_budget() -> dict[str, object]:
    return {
        "window": 1,
        "development_attempts": 0,
        "reviewer_invocations": 0,
        "final_ci_fix_used": False,
        "review_artifacts": [],
        "checkpoint_reason": None,
    }


def test_lifecycle_help_describes_operator_boundaries() -> None:
    help_text = build_parser().format_help()

    assert "doctor" in help_text
    assert "创建或继续 Parent 的自动交付，停在需要操作者处理的边界" in help_text
    assert "恢复 Stop、失败/Human Blocker Invocation 或监督超时窗口" in help_text
    assert "立即停止活动 Executor 并保留可显式恢复的现场" in help_text
    assert "仅从 requeue_required 创建新的 Change Job Generation" in help_text
    assert "显示当前状态与下一条允许的操作" in help_text
    assert "查看各轮 Agent 工作、关键进展与结果" in help_text
    assert "promotion-handshake" not in help_text
    for internal_command in ("deliver", "accept-run", "publish-run"):
        assert internal_command not in help_text
        with pytest.raises(SystemExit):
            build_parser().parse_args([internal_command, "run-id"])


def test_status_and_history_expose_plain_output_switch() -> None:
    status = build_parser().parse_args(["status", "run-id", "--plain"])
    history = build_parser().parse_args(["history", "run-id", "--plain"])

    assert status.plain is True
    assert history.plain is True


def test_history_exposes_details_switch() -> None:
    history = build_parser().parse_args(["history", "run-id", "--details"])

    assert history.details is True


def test_history_details_does_not_change_json_audit_projection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "completed",
        "timeline": [],
        "timeline_continuation": [],
        "agent_invocation_history": [],
        "resume_audit": {"history": []},
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=True)
    without_details = json.loads(capsys.readouterr().out)
    cli.cli_presentation._print_history(state, as_json=True, details=True)
    with_details = json.loads(capsys.readouterr().out)

    assert with_details == without_details


def test_status_does_not_fall_back_to_findings_from_another_work_subject(
    git_repo: Path,
) -> None:
    finding = "问题：旧候选未修复；证据：old；必须修复：修复；复验：重跑"
    passing_artifact = {
        "checks": {
            lane: {"status": "pass", "evidence": "evidence", "findings": []}
            for lane in ("e2e", "standards", "spec")
        }
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "run_publication_pending",
        "active_ticket_job": {
            "ticket_number": 2,
            "phase": "candidate",
            "candidate_sha": "current",
            "acceptance_record": {
                "reviewed_candidate_sha": "current",
                "artifact": passing_artifact,
            },
        },
        "parent_job": {
            "phase": "blocked",
            "acceptance_artifact": {
                "checks": {
                    "e2e": {
                        "status": "fail",
                        "evidence": "old evidence",
                        "findings": [finding],
                    },
                    "standards": {
                        "status": "pass",
                        "evidence": "old evidence",
                        "findings": [],
                    },
                    "spec": {
                        "status": "pass",
                        "evidence": "old evidence",
                        "findings": [],
                    },
                }
            },
        },
        "diagnostics": [],
    }
    state["schema_version"] = 1
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run("run-1", state)

    result = invoke_cli_inprocess(git_repo, fixture, "status", "run-1", "--json")
    output = stdout_json(result)

    assert output["progress"]["findings"] == []
    assert output["progress"]["conclusion"] == "当前有效通过"


@pytest.mark.parametrize(
    ("publication_head", "expected_conclusion"),
    [
        ("run-head", "验收通过，等待发布"),
        ("old-head", "尚无有效验收结论"),
    ],
)
def test_status_binds_run_acceptance_verdict_to_publication_head(
    git_repo: Path,
    publication_head: str,
    expected_conclusion: str,
) -> None:
    passing_artifact = {
        "checks": {
            lane: {"status": "pass", "findings": []}
            for lane in ("e2e", "standards", "spec")
        }
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "publication_pending",
        "run_acceptance": {
            "phase": "accepted",
            "reviewed_head_sha": "run-head",
            "acceptance_record": {
                "reviewed_head_sha": "run-head",
                "artifact": passing_artifact,
            },
        },
        "run_publication": {
            "phase": "publication_pending",
            "record": {"run_head_sha": publication_head},
        },
        "diagnostics": [],
    }
    state["schema_version"] = 1
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run("run-1", state)

    result = invoke_cli_inprocess(git_repo, fixture, "status", "run-1", "--json")
    output = stdout_json(result)

    assert output["progress"]["current_object"] == "Run Publication"
    assert output["progress"]["findings"] == []
    assert output["progress"]["conclusion"] == expected_conclusion


@pytest.mark.parametrize(
    ("publication_phase", "publication_head", "expected_conclusion"),
    [
        ("publication_pending", "integrated-head", "验收通过，等待发布"),
        ("merged", "integrated-head", "当前有效通过"),
        ("awaiting_approval", "integrated-head", "验收通过，等待发布"),
        ("waiting_checks", "integrated-head", "验收通过，等待发布"),
        ("publication_pending", "drifted-head", "尚无有效验收结论"),
    ],
)
def test_status_binds_promoted_repair_acceptance_to_publication_boundary(
    git_repo: Path,
    publication_phase: str,
    publication_head: str,
    expected_conclusion: str,
) -> None:
    passing_artifact = {
        "checks": {
            lane: {"status": "pass", "findings": []}
            for lane in ("e2e", "standards", "spec")
        }
    }
    state: dict[str, object] = {
        "run_id": "run-promoted-repair-status",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "completed" if publication_phase == "merged" else "run_publication_pending",
        "run_acceptance": {
            "phase": "accepted",
            "candidate_sha": "repair-candidate",
            "reviewed_head_sha": "integrated-head",
            "acceptance_record": {
                "acceptance_scope": "run",
                "acceptance_state": "integrated",
                "reviewed_candidate_sha": "repair-candidate",
                "reviewed_head_sha": "integrated-head",
                "artifact": passing_artifact,
            },
        },
        "run_publication": {
            "phase": publication_phase,
            "record": {"run_head_sha": publication_head},
        },
        "diagnostics": [],
        "schema_version": 1,
    }
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run(
        "run-promoted-repair-status", state
    )

    output = stdout_json(
        invoke_cli_inprocess(
            git_repo,
            fixture,
            "status",
            "run-promoted-repair-status",
            "--json",
        )
    )

    assert output["progress"]["current_object"] == "Run Publication"
    assert output["progress"]["conclusion"] == expected_conclusion


@pytest.mark.parametrize(
    "changes",
    [
        {"reviewed_candidate_sha": "stale-candidate"},
        {"reviewed_head_sha": "stale-head"},
        {"acceptance_state": "candidate"},
        {"reviewed_head_sha": None},
    ],
)
def test_status_rejects_invalid_repair_promotion(changes: dict[str, object]) -> None:
    from agent_run.delivery_status import status_progress_view

    record = {
        "acceptance_state": "integrated",
        "reviewed_candidate_sha": "candidate",
        "reviewed_head_sha": "promoted",
        "artifact": {"checks": {"spec": {"status": "pass", "findings": []}}},
        **changes,
    }
    state = {
        "status": "run_approval_pending",
        "run_acceptance": {
            "phase": "accepted",
            "candidate_sha": "candidate",
            "reviewed_head_sha": "promoted",
            "acceptance_record": record,
        },
        "run_publication": {
            "phase": "awaiting_approval",
            "record": {"run_head_sha": "promoted"},
        },
    }
    assert status_progress_view(state, {})["conclusion"] == "尚无有效验收结论"


@pytest.mark.parametrize(
    ("subject_key", "phase"),
    [("parent_job", "completed"), ("run_acceptance", "accepted")],
)
def test_status_keeps_matching_acceptance_conclusion_after_completion(
    git_repo: Path, subject_key: str, phase: str
) -> None:
    passing_artifact = {
        "checks": {
            lane: {"status": "pass", "findings": []}
            for lane in ("e2e", "standards", "spec")
        }
    }
    state: dict[str, object] = {
        "run_id": "run-completed-conclusion",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Completed Parent"},
        "status": "completed",
        subject_key: {
            "phase": phase,
            "candidate_sha": "candidate-current",
            "acceptance_record": {
                "reviewed_candidate_sha": "candidate-current",
                "artifact": passing_artifact,
            },
        },
        "diagnostics": [],
        "schema_version": 1,
    }
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run(
        "run-completed-conclusion", state
    )

    json_result = invoke_cli_inprocess(
        git_repo, fixture, "status", "run-completed-conclusion", "--json"
    )
    plain_result = invoke_cli_inprocess(
        git_repo, fixture, "status", "run-completed-conclusion", "--plain"
    )

    output = stdout_json(json_result)
    assert output["progress"]["conclusion"] == "当前有效通过"
    assert "结论:       当前版本已通过验收" in plain_result.stdout


@pytest.mark.parametrize(
    "acceptance_record",
    [
        None,
        {
            "reviewed_candidate_sha": "candidate-old",
            "artifact": {
                "checks": {
                    lane: {"status": "pass", "findings": []}
                    for lane in ("e2e", "standards", "spec")
                }
            },
        },
    ],
)
def test_status_does_not_infer_completed_acceptance_without_matching_record(
    git_repo: Path, acceptance_record: dict[str, object] | None
) -> None:
    state: dict[str, object] = {
        "run_id": "run-completed-without-proof",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Completed Parent"},
        "status": "completed",
        "parent_job": {
            "phase": "completed",
            "candidate_sha": "candidate-current",
        },
        "diagnostics": [],
        "schema_version": 1,
    }
    parent_job = state["parent_job"]
    assert isinstance(parent_job, dict)
    if acceptance_record is not None:
        parent_job["acceptance_record"] = acceptance_record
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run(
        "run-completed-without-proof", state
    )

    output = stdout_json(
        invoke_cli_inprocess(
            git_repo, fixture, "status", "run-completed-without-proof", "--json"
        )
    )

    assert output["progress"]["conclusion"] == "尚无有效验收结论"


@pytest.mark.parametrize("phase", ["candidate", "reviewing"])
@pytest.mark.parametrize("object_kind", ["ticket", "parent"])
def test_status_keeps_acceptance_findings_until_repaired_candidate_is_accepted(
    capsys: pytest.CaptureFixture[str], phase: str, object_kind: str
) -> None:
    finding = "问题：修复未完成；证据：旧候选失败；必须修复：完成修复；复验：重新验收"
    failed_artifact = {
        "checks": {
            "e2e": {"status": "fail", "findings": [finding]},
            "standards": {"status": "pass", "findings": []},
            "spec": {"status": "pass", "findings": []},
        }
    }
    passing_artifact = {
        "checks": {
            lane: {"status": "pass", "findings": []}
            for lane in ("e2e", "standards", "spec")
        }
    }
    job: dict[str, object] = {
        "phase": phase,
        "repair_source": "acceptance",
        "candidate_sha": "candidate-b",
        "modification_attempts": 1,
        "validation_attempts": 1,
        "acceptance_record": {
            "reviewed_candidate_sha": "candidate-a",
            "artifact": failed_artifact,
        },
        "review_budget": _canonical_run_budget(),
    }
    if object_kind == "ticket":
        job["ticket_number"] = 2
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "active",
        "active_ticket_job": job if object_kind == "ticket" else None,
        "parent_job": job if object_kind == "parent" else None,
        "active_agent_invocation": (
            {
                "work_subject": (
                    "ticket:2" if object_kind == "ticket" else "parent-only:run-1"
                ),
                "role": "reviewer",
                "status": "running",
                "started_at": "2026-09-09T00:00:00+00:00",
            }
            if phase == "reviewing"
            else None
        ),
        "_executor_control": {"activity": "running"},
        "diagnostics": [],
        "schema_version": 1,
    }
    cli.cli_presentation._print_status(state, as_json=True)
    before = json.loads(capsys.readouterr().out)

    assert before["progress"]["findings"] == [finding]
    assert before["progress"]["conclusion"] == (
        "修复完成，待复验" if phase == "candidate" else "正在复验"
    )

    job["phase"] = "accepted"
    job["acceptance_record"] = {
        "reviewed_candidate_sha": "candidate-b",
        "artifact": passing_artifact,
    }
    cli.cli_presentation._print_status(state, as_json=True)
    after = json.loads(capsys.readouterr().out)

    assert after["progress"]["findings"] == []
    assert after["progress"]["conclusion"] == "当前有效通过"


def test_status_uses_saved_publication_boundary_before_publication_record(
    git_repo: Path,
) -> None:
    passing_artifact = {
        "checks": {
            lane: {"status": "pass", "findings": []}
            for lane in ("e2e", "standards", "spec")
        }
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "run_publication_pending",
        "run_acceptance": {
            "phase": "accepted",
            "reviewed_head_sha": "run-head",
            "acceptance_record": {
                "reviewed_head_sha": "run-head",
                "artifact": passing_artifact,
            },
        },
        "run_publication": {
            "phase": "publishing",
            "pending_semantic_attempt": {
                "role": "publication",
                "currentness_boundary": {"reviewed_head_sha": "run-head"},
            },
        },
        "diagnostics": [],
        "schema_version": 1,
    }
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run("run-1", state)

    output = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "status", "run-1", "--json")
    )

    assert output["progress"]["current_object"] == "Run Publication"
    assert output["progress"]["findings"] == []
    assert output["progress"]["conclusion"] == "验收通过，等待发布"


@pytest.mark.parametrize(
    ("prior_failure", "activity", "expected"),
    [
        (False, "running", "首次验收中"),
        (False, "not_running", "验收已中断，等待恢复"),
        (False, "unknown", "验收状态无法确认"),
        (False, "capacity_wait", "模型容量不足，等待自动续接首次验收"),
        (False, "recovery_wait", "验收异常，正在自动续接首次验收"),
        (True, "running", "正在复验"),
        (True, "not_running", "验收已中断，等待恢复"),
        (True, "unknown", "验收状态无法确认"),
        (True, "capacity_wait", "模型容量不足，等待自动续接复验"),
        (True, "recovery_wait", "验收异常，正在自动续接复验"),
    ],
)
def test_status_distinguishes_first_acceptance_revalidation_and_activity(
    capsys: pytest.CaptureFixture[str],
    prior_failure: bool,
    activity: str,
    expected: str,
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "run_acceptance_pending",
        "run_acceptance": {
            "phase": "reviewing",
            "validation_attempts": 1,
            "unresolved_acceptance_artifact": (
                {
                    "checks": {
                        "e2e": {
                            "status": "fail",
                            "findings": ["问题：旧失败；证据：e；必须修复：修复；复验：重跑"],
                        }
                    }
                }
                if prior_failure
                else None
            ),
        },
        "active_agent_invocation": {
            "work_subject": "run-acceptance:run-1",
            "role": "reviewer",
            "status": "running",
            "started_at": "2026-09-09T00:00:00+00:00",
        },
        "_executor_control": {"activity": activity},
        "diagnostics": [],
        "schema_version": 1,
    }
    if activity in {"capacity_wait", "recovery_wait"}:
        state["_executor_control"] = {"activity": "running"}
        state["active_agent_invocation"].update(
            status="resuming",
            recovery_waiting=True,
            recovery_kind=(
                "capacity" if activity == "capacity_wait" else "ordinary"
            ),
        )
    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["progress"]["conclusion"] == expected


@pytest.mark.parametrize(
    ("phase", "activity", "expected"),
    [
        ("developing", "running", "上次验收失败，正在修复"),
        ("developing", "interrupted", "上次验收失败，修复已中断，等待恢复"),
        ("developing", "unknown", "上次验收失败，修复状态无法确认"),
        ("developing", "capacity_wait", "上次验收失败，等待模型容量后自动续接修复"),
        ("developing", "recovery_wait", "上次验收失败，修复异常，正在自动续接"),
        ("repairing", "running", "上次验收失败，正在修复"),
        ("repairing", "interrupted", "上次验收失败，修复已中断，等待恢复"),
        ("repairing", "unknown", "上次验收失败，修复状态无法确认"),
        ("repairing", "capacity_wait", "上次验收失败，等待模型容量后自动续接修复"),
        ("repairing", "recovery_wait", "上次验收失败，修复异常，正在自动续接"),
    ],
)
def test_status_repair_conclusion_respects_execution_activity(
    capsys: pytest.CaptureFixture[str],
    phase: str,
    activity: str,
    expected: str,
) -> None:
    finding = "问题：修复未完成；证据：旧候选失败；必须修复：完成修复；复验：重新验收"
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "active",
        "active_ticket_job": {
            "ticket_number": 2,
            "phase": phase,
            "repair_source": "acceptance",
            "candidate_sha": "candidate-b",
            "acceptance_record": {
                "reviewed_candidate_sha": "candidate-a",
                "artifact": {
                    "checks": {
                        "e2e": {"status": "fail", "findings": [finding]},
                        "standards": {"status": "pass", "findings": []},
                        "spec": {"status": "pass", "findings": []},
                    }
                },
            },
        },
        "active_agent_invocation": {
            "work_subject": "ticket:2",
            "role": "development",
            "status": "running",
            "started_at": "2026-09-07T01:39:19+00:00",
        },
        "_executor_control": {"activity": "running"},
        "diagnostics": [],
        "schema_version": 1,
    }
    if activity == "interrupted":
        state["_executor_control"] = {"activity": "not_running"}
    elif activity == "unknown":
        state["_executor_control"] = {"activity": "unknown"}
    elif activity in {"capacity_wait", "recovery_wait"}:
        state["active_agent_invocation"].update(
            status="resuming",
            recovery_waiting=True,
            recovery_kind=(
                "capacity" if activity == "capacity_wait" else "ordinary"
            ),
        )

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["progress"]["execution_activity"] == activity
    assert output["progress"]["findings"] == [finding]
    assert output["progress"]["conclusion"] == expected


def test_status_exposes_current_cycle_rounds_alongside_window_budget(
    git_repo: Path,
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "active",
        "active_ticket_job": {
            "ticket_number": 2,
            "phase": "candidate",
            "modification_attempts": 2,
            "validation_attempts": 3,
            "review_budget": {
                **_canonical_run_budget(),
                "window": 3,
                "development_attempts": 5,
                "reviewer_invocations": 4,
                "final_ci_fix_used": True,
            },
        },
        "diagnostics": [],
        "schema_version": 1,
    }
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run("run-1", state)

    json_result = invoke_cli_inprocess(
        git_repo, fixture, "status", "run-1", "--json"
    )
    plain_result = invoke_cli_inprocess(
        git_repo, fixture, "status", "run-1", "--plain"
    )
    output = stdout_json(json_result)
    rounds = output["progress"]["round_progress"]

    assert rounds["development_attempts"] == 5
    assert rounds["reviewer_invocations"] == 4
    assert rounds["cycle_development_attempts"] == 2
    assert rounds["cycle_review_attempts"] == 3
    assert rounds["development_attempt"] == 2
    assert rounds["review_attempt"] == 3
    assert "本次授权已用：开发 5 /" in plain_result.stdout
    assert "验收 4 /" in plain_result.stdout
    assert "额外 CI 修复：已用 1 / 1 次" in plain_result.stdout
    assert "当前子任务累计：开发 2 次，验收 3 次" in plain_result.stdout
    assert "Attempt #" not in plain_result.stdout
    assert "True /" not in plain_result.stdout
    assert rounds["final_ci_fix_used"] is True


def test_status_fails_closed_when_current_candidate_identity_is_missing(
    git_repo: Path,
) -> None:
    stale_finding = "问题：旧候选；证据：old；必须修复：修复；复验：重跑"
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "active",
        "active_ticket_job": {
            "ticket_number": 2,
            "phase": "candidate",
            "review_budget": {
                "review_artifacts": [
                    {
                        "candidate_sha": "old-candidate",
                        "artifact": {
                            "checks": {
                                "e2e": {"status": "fail", "findings": [stale_finding]},
                            }
                        },
                    }
                ]
            },
        },
        "diagnostics": [],
        "schema_version": 1,
    }
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run("run-1", state)

    result = invoke_cli_inprocess(git_repo, fixture, "status", "run-1", "--json")
    output = stdout_json(result)

    assert output["progress"]["findings"] == []
    assert output["progress"]["conclusion"] == "尚无有效验收结论"


def test_plain_status_places_next_action_before_complete_finding_details(
    git_repo: Path,
) -> None:
    finding = "问题：缺少修复；证据：feature.txt 只有一行；必须修复：补齐实现；复验：运行 CLI"
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "active",
        "active_ticket_job": {
            "ticket_number": 2,
            "phase": "repairing",
            "repair_source": "acceptance",
            "unresolved_acceptance_artifact": {
                "checks": {
                    "e2e": {"findings": [finding]},
                    "standards": {"findings": []},
                    "spec": {"findings": []},
                }
            },
        },
        "diagnostics": [],
    }
    state["schema_version"] = 1
    fixture = write_fixture(git_repo / "github.json", issues={})
    StateStore(git_repo / ".agent-run").save_run("run-1", state)

    result = invoke_cli_inprocess(
        git_repo, fixture, "status", "run-1", "--plain"
    )
    output = result.stdout

    assert output.index("下一步") < output.index("当前问题")
    assert "问题：缺少修复" in output
    assert "证据：feature.txt 只有一行" in output
    assert "必须修复：补齐实现" in output
    assert "复验：运行 CLI" in output
    for value in ("缺少修复", "feature.txt 只有一行", "补齐实现", "复验：运行 CLI"):
        assert output.count(value) == 1


@pytest.mark.parametrize("width", [60, 80, 120])
def test_rich_status_wraps_long_findings_and_next_action(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    from io import StringIO
    from rich.text import Text

    class TerminalBuffer(StringIO):
        def isatty(self) -> bool:
            return True

    finding_tail = "证据完整尾标"
    finding = (
        "问题：需要保留完整问题正文；证据："
        f"/tmp/{'long-directory-' * 12}中文文件-{finding_tail}；"
        "必须修复：完整保留该证据并同步覆盖同族路径；"
        f"复验：在窄终端逐字符核对-{finding_tail}"
    )
    command_tail = "command-tail"
    command = (
        "agent-run run 1 --repo "
        f"{'long-repository-name-' * 12}{command_tail}"
    )
    output = TerminalBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setenv("LINES", "40")
    monkeypatch.setattr(
        os,
        "get_terminal_size",
        lambda _fd: os.terminal_size((width, 40)),
    )

    cli.cli_presentation.print_rich_status_progress(
        {},
        {
            "operator_action": {
                "type": "Human Blocker",
                "reasons": [],
                "next_action": command,
                "preserved": "当前状态与已有审计证据",
                "phase": "developing",
                "trigger_invocation": {
                    "role": "review",
                    "model": "status-model",
                    "reasoning_effort": "high",
                    "duration_seconds": 42,
                },
            }
        },
        {
            "repository": "example/project",
            "parent": {"number": 1, "title": "Status card"},
            "status": "blocked",
            "conclusion": "需要人工处理",
            "phase": "developing",
            "current_object": "Ticket #2",
            "ticket_progress": None,
            "round_progress": None,
            "run_repair": None,
            "elapsed_seconds": 1,
            "current_agent": None,
            "execution_activity": "not_running",
            "next_action": command,
            "findings": [finding],
        },
    )

    rendered = output.getvalue()
    visible = Text.from_ansi(rendered).plain
    assert finding_tail in visible
    assert command_tail in visible
    assert f"命令：{command}" in visible
    assert f"命令：{command}\n" in visible
    assert visible.index(f"命令：{command}") < visible.index("下一步")
    assert "触发阻塞的 Agent：验收 Agent" in visible
    assert "Review Agent" not in visible
    assert "模型" in visible and "status-model" in visible
    assert "推理强度" in visible and "high" in visible
    readable = " ".join(visible.replace("│", " ").split())
    assert "本轮时长" in readable and "42 秒" in readable
    assert "\x1b[90m" not in rendered


@pytest.mark.parametrize(
    ("command", "executor_bound", "expected"),
    [
        ("run", False, False),
        ("run", True, True),
        ("publish-run", False, True),
        ("resume", False, False),
    ],
)
def test_publication_pending_success_depends_on_execution_context(
    command: str, executor_bound: bool, expected: bool
) -> None:
    assert (
        cli._lifecycle_result_succeeded(
            command, "publication_pending", executor_bound=executor_bound
        )
        is expected
    )


def test_public_run_preserves_non_invocation_execution_failure_until_resume(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    agents = git_repo / "agents.json"
    agent_data = {
        "developments": [
            {
                "expected_thread_id": None,
                "thread_id": "developer-2",
                "human_blockers": ["Maintainer input is required."],
            }
        ],
        "publications": [],
        "reviews": [],
    }
    agents.write_text(json.dumps(agent_data), encoding="utf-8")
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    states = StateStore(git_repo / ".agent-run")
    assert Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    ).record_execution_failure(run_id, "controller failed before an Agent started")
    failed = load_only_run_state(git_repo)
    assert failed["active_agent_invocation"] is None

    status = run_cli(git_repo, fixture, "status", run_id)
    assert "类型: 执行失败" in status.stdout
    assert "下一步: agent-run resume 1 --repo example/project" in status.stdout
    for command in ("status", "history"):
        json_view = stdout_json(
            run_cli(git_repo, fixture, command, run_id, "--json")
        )
        assert json_view["operator_action"]["type"] == "Execution Failure"
        assert json_view["operator_action"]["object"] == "Ticket #2"
        assert json_view["operator_action"]["phase"] == "active"
        assert json_view["operator_action"]["next_action"] == (
            "agent-run resume 1 --repo example/project"
        )
        assert json_view["next_action"] == json_view["operator_action"][
            "next_action"
        ]
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["repository_read_failures"] = [
        {
            "code": "github_read_failed",
            "message": "repository binding has not converged",
        }
    ]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    ordinary_run = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert ordinary_run.returncode == 2
    assert load_only_run_state(git_repo) == failed
    assert json.loads(agents.read_text(encoding="utf-8")) == agent_data

    ordinary_run_after_binding = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )
    assert ordinary_run_after_binding.returncode == 2
    assert load_only_run_state(git_repo) == failed

    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    with pytest.raises(RequeueError, match="unresolved Execution Failure"):
        controller.requeue(run_id)
    assert load_only_run_state(git_repo) == failed

    for lifecycle_command in ("requeue", "approve", "revise"):
        rejected = run_cli(git_repo, fixture, lifecycle_command, run_id)
        assert rejected.returncode == 2
        assert load_only_run_state(git_repo) == failed

    for invalid_option in (
        ("--message", "not valid for an Execution Failure"),
        ("--new-thread",),
    ):
        invalid_resume = run_cli(
            git_repo,
            fixture,
            "resume",
            "1",
            "--repo",
            "example/project",
            *invalid_option,
        )
        assert invalid_resume.returncode == 2
        assert load_only_run_state(git_repo) == failed

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        "1",
        "--repo",
        "example/project",
        "--agent-fixture",
        str(agents),
    )

    assert resumed.returncode == 2, resumed.stderr
    resumed_state = load_only_run_state(git_repo)
    assert stdout_json(resumed)["status"] == "ready_for_human"
    assert resumed_state["status"] == "ready_for_human"
    assert resumed_state["resume_audit"]["history"][-1]["kind"] == (
        "execution_failure"
    )
    assert resumed_state["resume_audit"]["history"][-1]["failure_code"] == (
        "command_failed"
    )

    final_audit = load_only_run_state(git_repo)["resume_audit"]["history"][-1]
    assert final_audit["kind"] == "execution_failure"
    assert isinstance(final_audit["successor_invocation_started_at"], str)


def test_public_policy_cli_persists_user_defaults_and_shows_resolved_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "policy",
                "configure",
                "--ticket-review-rounds",
                "2",
                "--parent-only-paired-rounds",
                "7",
                "--run-repair-rounds",
                "6",
                "--review-deadline",
                "90m",
            ]
        )
        == 0
    )
    configured = json.loads(capsys.readouterr().out)
    assert configured["result"] == "configured"
    assert configured["policy"]["ticket_review_rounds"] == 2
    assert configured["policy"]["parent_only_paired_rounds"] == 7
    assert configured["policy"]["run_repair_rounds"] == 6
    assert configured["policy"]["invocation_deadlines"]["review"] == 5400

    assert main(["policy", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["result"] == "policy"
    assert shown["user_defaults"] == {
        "invocation_deadlines": {"review": "90m"},
        "parent_only_paired_rounds": 7,
        "run_repair_rounds": 6,
        "ticket_review_rounds": 2,
    }


def test_invalid_policy_is_rejected_before_run_state_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert main(["run", "1", "--review-deadline", "0s", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["result"] == "error"
    assert not (tmp_path / ".agent-run").exists()


def test_public_operator_docs_describe_the_v01_quickstart() -> None:
    reference = (PROJECT_ROOT / "docs" / "agent-run.md").read_text(encoding="utf-8")
    for required in (
        "./install.sh", "release tag", "agent-run doctor", "auth app configure",
        "--rollback", "--uninstall", "Linux", "./setup.sh", "目标交付仓库",
    ):
        assert required in reference

    # Public READMEs route agents to language-specific guides; installation
    # details belong there rather than being duplicated in the landing page.
    for readme, guide_name, install_name in (
        ("README.md", "agent-guide.en.md", "install.en.md"),
        ("README.zh-CN.md", "agent-guide.md", "install.md"),
    ):
        landing = (PROJECT_ROOT / readme).read_text(encoding="utf-8")
        guide = (PROJECT_ROOT / "docs" / guide_name).read_text(encoding="utf-8")
        install = (PROJECT_ROOT / "docs" / install_name).read_text(encoding="utf-8")
        assert f"](docs/{guide_name})" in landing
        assert f"]({install_name}#" in guide
        for required in (
            "./setup.sh", "./install.sh", "agent-run doctor --json",
            "--rollback", "--uninstall", "Linux", "gh auth login",
            "agent-run auth status", "codex login status",
        ):
            assert required in install
        assert "](agent-run.md#github-只读身份)" in install
        assert "](agent-run.md#权限边界)" in install


def test_doctor_reports_host_readiness_without_mutating_user_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, output in {
        "git": "git version 2.40.0\n",
        "codex": "--json --output-last-message --output-schema --dangerously-bypass-approvals-and-sandbox --model --config --cd --color GH_TOKEN=doctor-secret\n",
        "openssl": "OpenSSL 3.0\n",
        "bwrap": "bubblewrap 0.8\n",
    }.items():
        executable = fake_bin / name
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '%s' {output!r}\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = --version ] && exit 0\n"
        "[ \"$1\" = auth ] && exit 0\n"
        "[ \"$1\" = api ] && { echo '--paginate --slurp --method --header'; exit 0; }\n"
        "exit 1\n",
        encoding="utf-8",
    )
    gh.chmod(0o700)

    home = tmp_path / "home"
    home.mkdir()
    config = home / "config"
    data = home / "data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        doctor,
        "execution_readiness",
        lambda: {
            "status": "ok",
            "host": "systemd-user",
            "linger_required": False,
            "reason": None,
        },
    )

    assert main(["doctor", "--json"]) == 0

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["result"] == "doctor"
    assert output["status"] == "issues"
    assert output["checks"]["git"]["status"] == "ok"
    assert output["checks"]["codex"]["status"] == "ok"
    assert output["checks"]["openssl"]["status"] == "ok"
    assert output["checks"]["bubblewrap"]["status"] == "ok"
    assert output["checks"]["github"]["logged_in"] is True
    assert output["checks"]["active_runner"]["status"] == "missing"
    assert output["checks"]["path"]["agent_run"] is None
    assert output["checks"]["worker_read_provider"] == {
        "provider": "host",
        "status": "ok",
    }
    assert output["installation_readiness"]["status"] == "issues"
    assert output["installation_readiness"]["checks"] == [
        "python",
        "active_runner",
        "path",
    ]
    assert output["execution_readiness"]["status"] == "ok"
    assert output["execution_readiness"]["host"] == "systemd-user"
    assert output["execution_readiness"]["linger_required"] is False
    assert "doctor-secret" not in output_text
    assert not config.exists()
    assert not data.exists()


@pytest.mark.parametrize(
    ("openssl_script", "expected_status"),
    [
        (None, "missing"),
        (
            '#!/bin/sh\n[ "$1" = version ] && exit 7\nexit 0\n',
            "unavailable",
        ),
        (
            f"#!{sys.executable}\nimport time\ntime.sleep(30)\n",
            "timeout",
        ),
    ],
)
def test_doctor_classifies_bounded_openssl_probe_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    openssl_script: str | None,
    expected_status: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    if openssl_script is not None:
        openssl = fake_bin / "openssl"
        openssl.write_text(openssl_script, encoding="utf-8")
        openssl.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["checks"]["openssl"]["status"] == expected_status


def test_doctor_does_not_fallback_when_app_profile_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    profile = config / "github-app.json"
    profile.write_text("{not-json}\n", encoding="utf-8")
    profile.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "invalid",
    }
    assert "not-json" not in output_text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_id", "9" * 5000),
        ("installation_id", "9" * 5000),
        ("app_id", "not-a-number"),
        ("installation_id", "-1"),
    ],
)
def test_doctor_reports_invalid_app_identifiers_without_unbounded_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
) -> None:
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    config.chmod(0o700)
    profile = {
        "app_id": "123",
        "installation_id": "456",
        "private_key_path": str(tmp_path / "secret-key.pem"),
    }
    profile[field] = value
    profile_path = config / "github-app.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    profile_path.chmod(0o600)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    captured = capsys.readouterr()
    output_text = captured.out
    output = json.loads(output_text)
    assert {
        "python",
        "git",
        "codex",
        "github",
        "openssl",
        "bubblewrap",
        "active_runner",
        "path",
    } <= output["checks"].keys()
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "invalid",
    }
    assert "command_failed" not in output_text
    assert "blocked" not in output_text
    assert "Traceback" not in output_text
    assert "secret-key.pem" not in output_text
    assert "Traceback" not in captured.err


def test_doctor_detects_a_non_managed_agent_run_path_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    conflicting_entry = fake_bin / "agent-run"
    conflicting_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    conflicting_entry.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    user_bin = home / ".local" / "bin"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", os.pathsep.join((str(fake_bin), str(user_bin))))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["checks"]["path"] == {
        "agent_run": str(conflicting_entry),
        "status": "conflict",
        "user_bin_on_path": True,
    }


@pytest.mark.parametrize(
    ("path_state", "human_detail"),
    [
        ("ok", "agent-run 可用"),
        ("needs_refresh", "需要刷新登录 shell"),
        ("conflict", "检测到非受管同名入口"),
        ("invalid", "受管入口无效"),
        ("missing", "未找到 agent-run 入口"),
    ],
)
def test_doctor_classifies_managed_path_states_and_human_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path_state: str,
    human_detail: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    user_bin = home / ".local" / "bin"
    managed_target = home / "data" / "agent-run" / "active" / "current" / "bin" / "agent-run"
    managed_target.parent.mkdir(parents=True)
    managed_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    managed_target.chmod(0o700)
    stable_entry = user_bin / "agent-run"

    if path_state in {"ok", "needs_refresh", "conflict"}:
        user_bin.mkdir(parents=True)
        stable_entry.symlink_to(managed_target)
    elif path_state == "invalid":
        user_bin.mkdir(parents=True)
        stable_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stable_entry.chmod(0o700)

    path_entries: list[str] = []
    if path_state == "conflict":
        conflict_bin = tmp_path / "conflict-bin"
        conflict_bin.mkdir()
        conflicting_entry = conflict_bin / "agent-run"
        conflicting_entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        conflicting_entry.chmod(0o700)
        path_entries.append(str(conflict_bin))
    if path_state == "ok" or path_state == "invalid":
        path_entries.append(str(user_bin))

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("PATH", os.pathsep.join(path_entries))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["path"]["status"] == path_state

    assert main(["doctor"]) == 0
    human = capsys.readouterr().out
    assert human_detail in human
    if path_state in {"conflict", "invalid"}:
        assert "agent-run 可用" not in human


def test_doctor_reports_a_corrupt_app_key_as_invalid_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required to exercise corrupt key validation")
    key = tmp_path / "app.pem"
    key.write_text("not-a-private-key\n", encoding="utf-8")
    key.chmod(0o600)
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    (config / "github-app.json").write_text(
        json.dumps(
            {
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": str(key),
            }
        ),
        encoding="utf-8",
    )
    (config / "github-app.json").chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(Path(openssl).parent))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "issues"
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "invalid",
    }


def test_doctor_rejects_a_zero_exit_openssl_probe_without_a_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    openssl = fake_bin / "openssl"
    openssl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    openssl.chmod(0o700)
    key = tmp_path / "app.pem"
    key.write_text("not-used-by-fake-openssl\n", encoding="utf-8")
    key.chmod(0o600)
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    config.chmod(0o700)
    profile = config / "github-app.json"
    profile.write_text(
        json.dumps(
            {
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": str(key),
            }
        ),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "invalid",
    }
    assert "not-used-by-fake-openssl" not in output_text


def test_doctor_reports_a_broken_managed_entry_as_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    stable_entry = home / ".local" / "bin" / "agent-run"
    target = home / "data" / "agent-run" / "active" / "current" / "bin" / "agent-run"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    stable_entry.parent.mkdir(parents=True)
    stable_entry.symlink_to(target)
    target.unlink()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("PATH", str(stable_entry.parent))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["path"]["status"] == "invalid"


def test_doctor_accepts_a_real_private_key_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required to exercise valid key validation")
    key = tmp_path / "app.pem"
    subprocess.run(
        [openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    config = tmp_path / "config" / "agent-run"
    config.mkdir(parents=True)
    config.chmod(0o700)
    profile = config / "github-app.json"
    profile.write_text(
        json.dumps(
            {
                "app_id": "123",
                "installation_id": "456",
                "private_key_path": str(key),
            }
        ),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(Path(openssl).parent))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["checks"]["worker_read_provider"] == {
        "provider": "app",
        "status": "ok",
    }


def test_doctor_reaps_a_timed_out_dependency_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child_pid = tmp_path / "child.pid"
    probe = tmp_path / "git"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import subprocess\n"
        "import time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(30)'])\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    started = time.monotonic()
    assert main(["doctor", "--json"]) == 0
    elapsed = time.monotonic() - started
    output = json.loads(capsys.readouterr().out)

    assert elapsed < _DOCTOR_TIMEOUT_TEST_LIMIT_SECONDS
    assert output["checks"]["git"]["status"] == "timeout"
    child = int(child_pid.read_text(encoding="utf-8"))
    for _ in range(20):
        proc_stat = Path(f"/proc/{child}/stat")
        if not proc_stat.exists():
            break
        process_state = proc_stat.read_text(encoding="utf-8").split()[2]
        if process_state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed-out doctor probe left its child process running")


def test_doctor_reaps_descendants_after_a_probe_exits_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    child_pid = tmp_path / "child.pid"
    probe = tmp_path / "git"
    probe.write_text(
        f"#!{sys.executable}\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(30)'], stdout=subprocess.DEVNULL)\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        "print('git version 2.40.0')\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert main(["doctor", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["checks"]["git"]["status"] == "ok"
    child = int(child_pid.read_text(encoding="utf-8"))
    for _ in range(20):
        proc_stat = Path(f"/proc/{child}/stat")
        if not proc_stat.exists():
            break
        process_state = proc_stat.read_text(encoding="utf-8").split()[2]
        if process_state == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("normally completed doctor probe left its child process running")


def test_status_exposes_current_candidate_and_pr_in_top_level_and_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "active",
        "active_ticket_job": {
            "phase": "candidate",
            "ticket_number": 3,
            "candidate_sha": "CANDIDATE-1",
            "pr_number": 17,
            "review_budget": _canonical_run_budget(),
        },
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["candidate_sha"] == "CANDIDATE-1"
    assert output["pr_number"] == 17
    assert output["review_budget"]["candidate_sha"] == "CANDIDATE-1"
    assert output["review_budget"]["pr_number"] == 17


def test_status_distinguishes_semantic_invocation_output_budget_and_publication_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempt = {
        "attempt_id": "attempt-publication-2",
        "role": "publication",
        "work_subject": "ticket:3",
        "generation": 2,
        "currentness_boundary_fingerprint": "sha256:boundary",
        "ordinal": 2,
        "budget_window": None,
        "status": "pending",
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "publication",
        "status": "failed",
        "attempt_count": 3,
        "started_at": "2026-08-24T00:00:00+00:00",
        "semantic_attempt": deepcopy(attempt),
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "execution_failed",
        "active_ticket_job": {
            "ticket_number": 3,
            "phase": "publication_pending",
            "pending_semantic_attempt": deepcopy(attempt),
            "publication_operation_retry": {"attempts": 2, "limit": 4},
        },
        "active_agent_invocation": invocation,
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["semantic_agent_attempt"] == attempt
    assert output["agent_invocation"] == invocation
    assert output["output_attempt"] == {
        "invocation_started_at": "2026-08-24T00:00:00+00:00",
        "attempt_count": 3,
    }
    assert output["budget_window"] is None
    assert output["publication_operation_retry"] == {
        "semantic_attempt_id": "attempt-publication-2",
        "work_subject": "ticket:3",
        "attempts": 2,
        "limit": 4,
    }

    cli.cli_presentation._print_status(state, as_json=False)
    human = capsys.readouterr().out
    assert "状态:       执行失败，可恢复" in human
    assert "子任务 #3" in human
    assert "Semantic Agent Attempt" not in human
    assert "attempt-publication-2" not in human
    assert "sha256:boundary" not in human


def test_history_deduplicates_attempt_mirrors_and_projects_each_counter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    completed = {
        "attempt_id": "attempt-development-1",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "currentness_boundary_fingerprint": "sha256:first",
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "budget_consumed": True,
        "outcome": "candidate",
        "development_summary": "historical development result",
    }
    pending = {
        "attempt_id": "attempt-reviewer-1",
        "role": "reviewer",
        "work_subject": "ticket:3",
        "generation": 1,
        "currentness_boundary_fingerprint": "sha256:second",
        "ordinal": 1,
        "budget_window": 1,
        "status": "pending",
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "reviewer",
        "status": "failed",
        "attempt_count": 2,
        "started_at": "2026-08-24T00:01:00+00:00",
        "semantic_attempt": deepcopy(pending),
    }
    mirrored_job = {
        "ticket_number": 3,
        "phase": "reviewing",
        "semantic_attempt_history": [deepcopy(completed)],
        "pending_semantic_attempt": deepcopy(pending),
        "publication_operation_retry": {"attempts": 1, "limit": 3},
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "execution_failed",
        "timeline": [
            {
                "at": "2026-08-24T00:01:00+00:00",
                "kind": "ticket_phase",
                "status": "execution_failed",
                "semantic_attempt_id": "attempt-reviewer-1",
                "semantic_attempt_role": "reviewer",
                "semantic_attempt_ordinal": 1,
                "budget_window": 1,
                "agent_invocation_started_at": "2026-08-24T00:01:00+00:00",
                "agent_invocation_status": "failed",
                "output_attempt": 2,
                "publication_operation_retry_attempts": 1,
                "publication_operation_retry_limit": 3,
            }
        ],
        "active_ticket_job": deepcopy(mirrored_job),
        "ticket_jobs": {"3": deepcopy(mirrored_job)},
        "agent_invocation_history": [invocation],
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    public_completed = dict(completed)
    public_completed.pop("development_summary")
    assert output["semantic_agent_attempts"] == [public_completed, pending]
    assert output["agent_invocations"] == [invocation]
    assert output["output_attempts"] == [
        {
            "invocation_started_at": "2026-08-24T00:01:00+00:00",
            "work_subject": "ticket:3",
            "attempt_count": 2,
        }
    ]
    assert output["budget_windows"] == [
        {"work_subject": "ticket:3", "role": "development", "window": 1},
        {"work_subject": "ticket:3", "role": "reviewer", "window": 1},
    ]
    assert output["publication_operation_retries"] == [
        {
            "semantic_attempt_id": None,
            "work_subject": "ticket:3",
            "attempts": 1,
            "limit": 3,
        }
    ]

    cli.cli_presentation._print_history(state, as_json=False)
    human = capsys.readouterr().out
    assert "验收 Agent 第 1 轮" in human
    assert "Semantic Agent Attempt" not in human
    assert "attempt-reviewer-1" not in human
    assert "sha256:second" not in human

    cli.cli_presentation._print_history(state, as_json=False, plain=True, details=True)
    detailed_human = capsys.readouterr().out
    assert "historical development result" in detailed_human


def test_history_human_output_groups_invocations_and_keeps_resume_as_a_turning_point(
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = "问题：候选缺少边界处理；证据：candidate-a；必须修复：补齐边界；复验：重新运行 CLI。"
    attempt = {
        "attempt_id": "attempt-review-1",
        "role": "reviewer",
        "work_subject": "ticket:3",
        "generation": 1,
        "currentness_boundary_fingerprint": "sha256:review-boundary",
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "outcome": "acceptance_artifact",
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "reviewer",
        "invocation_role": "fresh_acceptance",
        "status": "completed",
        "started_at": "2026-08-24T00:01:00+00:00",
        "ended_at": "2026-08-24T00:02:00+00:00",
        "model": "review-model-v1",
        "reasoning_effort": "high",
        "reported_thread_id": "reviewer-2",
        "currentness_boundary": {"run_head_sha": "candidate-a"},
        "semantic_attempt": deepcopy(attempt),
    }
    resumed_invocation = deepcopy(invocation)
    resumed_invocation.update(
        {
            "started_at": "2026-08-24T00:03:00+00:00",
            "ended_at": "2026-08-24T00:04:00+00:00",
            "model": "review-model-v2",
        }
    )
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "History timeline"},
        "created_at": "2026-08-24T00:00:00+00:00",
        "status": "completed",
        "timeline": [
            {
                "at": "2026-08-24T00:01:30+00:00",
                "kind": "ticket_phase",
                "ticket": 3,
                "status": "active",
                "phase": "reviewing",
                "semantic_attempt_id": attempt["attempt_id"],
            },
            {
                "at": "2026-08-24T00:04:30+00:00",
                "kind": "ticket_phase",
                "ticket": 3,
                "status": "completed",
                "phase": "completed",
                "pr_number": 17,
                "commit_sha": "candidate-a",
                "required_checks_result": "pass",
                "required_checks_observed_at": "2026-08-24T00:04:00+00:00",
                "next_action": "manual acceptance",
                "semantic_attempt_id": attempt["attempt_id"],
            },
        ],
        "timeline_continuation": [],
        "ticket_jobs": {
            "3": {
                "ticket_number": 3,
                "review_budget": {
                    "window": 1,
                    "review_artifacts": [
                        {
                            "reviewer_thread_id": "reviewer-2",
                            "candidate_sha": "candidate-a",
                            "artifact": {
                                "checks": {
                                    "e2e": {
                                        "status": "fail",
                                        "evidence": "candidate-a",
                                        "findings": [finding],
                                    },
                                    "standards": {
                                        "status": "pass",
                                        "evidence": "standards",
                                        "findings": [],
                                    },
                                    "spec": {
                                        "status": "pass",
                                        "evidence": "spec",
                                        "findings": [],
                                    },
                                }
                            },
                        }
                    ],
                },
            }
        },
        "agent_invocation_history": [invocation, resumed_invocation],
        "semantic_agent_attempts": [attempt],
        "resume_audit": {
            "history": [
                {
                    "resume_id": "resume-1",
                    "requested_at": "2026-08-24T00:02:30+00:00",
                    "kind": "execution_failure",
                    "work_subject": "ticket:3",
                    "generation": 1,
                    "semantic_attempt_id": attempt["attempt_id"],
                    "human_response_supplied": False,
                }
            ]
        },
        "delivery_cleanup": {
            "status": "cleanup_pending",
            "last_error": "preserved checkout",
            "items": {
                "ticket-3": {
                    "branch": "agent-run/ticket-3",
                    "checkout": "/tmp/ticket-3",
                    "status": "cleanup_pending",
                    "recovery_kind": "stale_dirty_checkout",
                    "last_error": "tracked modifications",
                }
            },
        },
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=False, plain=True)
    output = capsys.readouterr().out

    assert output.count("第 1 轮") == 1
    assert "问题：候选缺少边界处理" in output
    assert "证据：candidate-a" not in output
    assert "恢复" in output
    assert "review-model-v1" in output
    assert "review-model-v2" in output
    assert "动作：验收代码；结果：验收未通过" in output

    cli.cli_presentation._print_history(state, as_json=False, plain=True, details=True)
    details = capsys.readouterr().out
    assert details.count("第 1 轮") == 1
    assert "问题：候选缺少边界处理" in details
    assert "证据：candidate-a" in details
    assert "必须修复：补齐边界" in details
    assert "复验：重新运行 CLI。" in details
    assert "PR 编号：17" in details
    assert "合并前检查结果：已通过" in details
    assert "preserved checkout" in details
    assert "保留原因：工作区仍有未提交修改" in details

    projection = history_records(
        state,
        {
            "semantic_agent_attempts": state["semantic_agent_attempts"],
            "agent_invocations": state["agent_invocation_history"],
            "timeline": state["timeline"],
            "timeline_continuation": state["timeline_continuation"],
            "agent_resumes": state["resume_audit"]["history"],
        },
    )
    assert projection[0]["ended_at"] == "2026-08-24T00:04:00+00:00"


def test_history_does_not_borrow_unique_artifact_for_identified_attempt() -> None:
    attempt = {
        "attempt_id": "attempt-without-boundary",
        "role": "reviewer",
        "work_subject": "ticket:3",
        "generation": 1,
        "currentness_boundary_fingerprint": "sha256:boundary",
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "outcome": "acceptance_artifact",
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "ticket_jobs": {
            "3": {
                "ticket_number": 3,
                "semantic_attempt_history": [attempt],
                "review_budget": {
                    "window": 1,
                    "review_artifacts": [
                        {
                            "reviewer_thread_id": "other-reviewer",
                            "candidate_sha": "other-candidate",
                            "artifact": {"checks": {"e2e": {"findings": ["wrong"]}}},
                        }
                    ],
                },
            }
        },
        "agent_invocation_history": [
            {
                "work_subject": "ticket:3",
                "role": "reviewer",
                "reported_thread_id": "missing-reviewer",
                "currentness_boundary": {"run_head_sha": "missing-candidate"},
                "semantic_attempt": deepcopy(attempt),
            }
        ],
    }
    records = history_records(
        state,
        {
            "semantic_agent_attempts": [attempt],
            "agent_invocations": state["agent_invocation_history"],
            "timeline": [],
            "timeline_continuation": [],
            "agent_resumes": [],
        },
    )

    assert records[0]["acceptance_artifact"] is None
    assert records[0]["findings"] == []


def test_history_keeps_authoritative_attempt_and_requires_both_review_identities() -> None:
    completed = {
        "attempt_id": "attempt-completed",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "outcome": "candidate",
        "development_summary": "completed result",
    }
    reviewer = {
        "attempt_id": "attempt-review",
        "role": "reviewer",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "outcome": "acceptance_artifact",
    }
    artifact = {
        "checks": {
            "e2e": {"findings": ["wrong-round"]},
            "standards": {"findings": []},
            "spec": {"findings": []},
        }
    }
    job = {
        "ticket_number": 3,
        "semantic_attempt_history": [completed, reviewer],
        "review_budget": {
            "window": 1,
            "development_attempts": 4,
            "reviewer_invocations": 3,
            "review_artifacts": [
                {
                    "reviewer_thread_id": "reviewer-1",
                    "candidate_sha": "candidate-1",
                    "artifact": artifact,
                }
            ],
        },
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "reviewer",
        "status": "completed",
        "started_at": "2026-08-24T00:01:00+00:00",
        "ended_at": "2026-08-24T00:02:00+00:00",
        "semantic_attempt": deepcopy(reviewer),
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "ticket_jobs": {"3": job},
        "active_ticket_job": deepcopy(job),
        "agent_invocation_history": [invocation],
    }
    audit = {
        "semantic_agent_attempts": [completed, reviewer],
        "agent_invocations": [invocation],
        "timeline": [],
        "timeline_continuation": [],
        "agent_resumes": [],
    }

    records = history_records(state, audit)
    completed_record = next(
        record for record in records if record["attempt_id"] == "attempt-completed"
    )
    reviewer_record = next(
        record for record in records if record["attempt_id"] == "attempt-review"
    )
    assert completed_record["attempt"]["status"] == "completed"
    assert completed_record["development_summary"] == "completed result"
    assert completed_record["budget_facts"]["development_attempts"] is None
    assert completed_record["budget_facts"]["reviewer_invocations"] is None
    assert reviewer_record["acceptance_artifact"] is None
    assert reviewer_record["budget_facts"]["development_attempts"] is None
    assert reviewer_record["budget_facts"]["reviewer_invocations"] is None

    invocation["reported_thread_id"] = "reviewer-1"
    invocation["currentness_boundary"] = {"run_head_sha": "candidate-1"}
    exact = history_records(state, audit)
    exact_record = next(
        record for record in exact if record["attempt_id"] == "attempt-review"
    )
    assert exact_record["acceptance_artifact"] == artifact

    legacy_attempt = {
        key: value
        for key, value in reviewer.items()
        if key != "attempt_id"
    }
    legacy_invocation = {
        **invocation,
        "semantic_attempt": deepcopy(legacy_attempt),
    }
    legacy_invocation.pop("reported_thread_id")
    legacy_invocation.pop("currentness_boundary")
    legacy_job = {**job, "semantic_attempt_history": [legacy_attempt]}
    legacy_state = {
        **state,
        "ticket_jobs": {"3": legacy_job},
        "active_ticket_job": deepcopy(legacy_job),
        "agent_invocation_history": [legacy_invocation],
    }
    legacy_audit = {
        **audit,
        "semantic_agent_attempts": [legacy_attempt],
        "agent_invocations": [legacy_invocation],
    }
    legacy_record = history_records(legacy_state, legacy_audit)[0]
    assert legacy_record["acceptance_artifact"] is None


def test_history_standalone_events_use_persisted_time_and_skip_snapshots() -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "ticket_jobs": {},
        "status": "active",
    }
    audit = {
        "semantic_agent_attempts": [],
        "agent_invocations": [],
        "timeline": [
            {
                "at": "2026-08-24T00:02:00+00:00",
                "kind": "ticket_phase",
                "ticket": 3,
                "status": "parent_delivery_pending",
                "thread_id": "internal-thread",
            },
            {
                "at": "2026-08-24T00:01:00+00:00",
                "kind": "ticket_phase",
                "ticket": 3,
                "status": "operator_stopped",
                "thread_id": "internal-thread",
            },
        ],
        "timeline_continuation": [],
        "agent_resumes": [
            {
                "requested_at": "2026-08-24T00:03:00+00:00",
                "work_subject": "ticket:3",
                "source_status": "supervision_timeout",
                "human_response_supplied": False,
            }
        ],
    }

    records = history_records(state, audit)
    assert [record["started_at"] for record in records] == [
        "2026-08-24T00:01:00+00:00",
        "2026-08-24T00:03:00+00:00",
    ]
    assert all(record["event_record"] is True for record in records)


def test_history_merges_internal_publication_snapshots_and_same_integration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    timeline = [
        {
            "at": "2026-08-24T00:01:00+00:00",
            "kind": "run_publication",
            "phase": "publication_pending",
            "status": "run_publication_pending",
            "attempt": 1,
            "thread_id": "publication-thread",
            "semantic_attempt_id": "publication-attempt",
            "pr_number": 31,
            "commit_sha": "publication-sha",
        },
        {
            "at": "2026-08-24T00:02:00+00:00",
            "kind": "run_publication",
            "phase": "publication_pending",
            "status": "run_publication_pending",
            "attempt": 1,
            "thread_id": "publication-thread",
            "semantic_attempt_id": "publication-attempt",
            "pr_number": 31,
            "commit_sha": "publication-sha",
        },
        {
            "at": "2026-08-24T00:03:00+00:00",
            "kind": "run_publication",
            "phase": "publication_pending",
            "status": "run_publication_pending",
            "attempt": 1,
            "thread_id": "publication-thread",
            "semantic_attempt_id": "publication-attempt",
            "pr_number": 31,
            "commit_sha": "publication-sha",
        },
        {
            "at": "2026-08-24T00:04:00+00:00",
            "kind": "integration",
            "phase": "merged",
            "status": "merged",
            "ticket": 3,
            "pr_number": 17,
            "commit_sha": "integrated-sha",
        },
        {
            "at": "2026-08-24T00:05:00+00:00",
            "kind": "integration",
            "phase": "completed",
            "status": "completed",
            "ticket": 3,
            "pr_number": 17,
            "commit_sha": "integrated-sha",
        },
        {
            "at": "2026-08-24T00:06:00+00:00",
            "kind": "integration",
            "phase": "merged",
            "status": "merged",
            "ticket": 3,
            "pr_number": 18,
            "commit_sha": "other-integrated-sha",
        },
        {
            "at": "2026-08-24T00:07:00+00:00",
            "kind": "run_status",
            "status": "waiting_external",
        },
        {
            "at": "2026-08-24T00:08:00+00:00",
            "kind": "run_status",
            "status": "waiting_external",
        },
    ]
    state: dict[str, object] = {
        "run_id": "run-history-snapshot-merge",
        "repository": "example/project",
        "parent": {"number": 1, "title": "History snapshots"},
        "status": "completed",
        "timeline": timeline,
        "timeline_continuation": [],
        "agent_invocation_history": [],
        "semantic_agent_attempts": [
            {
                "attempt_id": "publication-attempt",
                "role": "publication",
                "work_subject": "run-publication:run-history-snapshot-merge",
                "generation": 1,
                "ordinal": 1,
                "status": "completed",
            }
        ],
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=False, plain=True)
    output = capsys.readouterr().out

    assert "发布：等待运行发布" not in output
    assert output.count("代码已合并") == 2
    assert output.count("确认外部操作结果") == 2

    cli.cli_presentation._print_history(state, as_json=False, plain=True, details=True)
    detailed_output = capsys.readouterr().out
    assert "发布：等待运行发布" not in detailed_output
    assert detailed_output.count("代码已合并") == 2
    assert detailed_output.count("确认外部操作结果") == 2

    cli.cli_presentation._print_history(state, as_json=True)
    machine_output = json.loads(capsys.readouterr().out)
    assert len(machine_output["timeline"]) == len(timeline)
    assert len(machine_output["events"]) == len(timeline)


def test_history_merges_stale_human_blocker_snapshots_but_keeps_real_pauses() -> None:
    attempt = {
        "attempt_id": "attempt-human-pause",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "status": "completed",
    }
    timeline = [
        {
            "at": "2026-08-24T00:01:00+00:00",
            "kind": "ticket_phase",
            "ticket": 3,
            "phase": "blocked",
            "status": "ready_for_human",
            "semantic_attempt_id": attempt["attempt_id"],
            "human_blockers": ["Need maintainer input."],
        },
        {
            "at": "2026-08-24T00:02:00+00:00",
            "kind": "ticket_phase",
            "ticket": 3,
            "phase": "blocked",
            "status": "ready_for_human",
            "semantic_attempt_id": attempt["attempt_id"],
            "explicit_resume_sequence": 1,
            "explicit_resume_kind": "human_blocker",
            "human_blockers": ["Need maintainer input."],
        },
        {
            "at": "2026-08-24T00:03:00+00:00",
            "kind": "ticket_phase",
            "ticket": 3,
            "phase": "developing",
            "status": "active",
            "attempt": 0,
            "semantic_attempt_id": attempt["attempt_id"],
            "worker": "resumed development worker",
            "human_blockers": ["Need maintainer input."],
        },
        {
            "at": "2026-08-24T00:05:00+00:00",
            "kind": "ticket_phase",
            "ticket": 3,
            "phase": "blocked",
            "status": "ready_for_human",
            "semantic_attempt_id": attempt["attempt_id"],
            "explicit_resume_sequence": 1,
            "explicit_resume_kind": "execution_failure",
            "human_blockers": ["Need maintainer input."],
        },
        {
            "at": "2026-08-24T00:06:00+00:00",
            "kind": "ticket_phase",
            "ticket": 3,
            "phase": "developing",
            "status": "active",
            "attempt": 0,
            "semantic_attempt_id": attempt["attempt_id"],
            "worker": "resumed development worker",
            "explicit_resume_sequence": 1,
            "human_blockers": ["Need maintainer input."],
        },
        {
            "at": "2026-08-24T00:08:00+00:00",
            "kind": "ticket_phase",
            "ticket": 3,
            "phase": "blocked",
            "status": "ready_for_human",
            "semantic_attempt_id": attempt["attempt_id"],
            "explicit_resume_sequence": 1,
            "explicit_resume_kind": "execution_failure",
            "human_blockers": ["Need maintainer input."],
        },
    ]
    audit = {
        "semantic_agent_attempts": [attempt],
        "agent_invocations": [],
        "timeline": timeline,
        "timeline_continuation": [],
        "agent_resumes": [
            {
                "resume_id": "resume-1",
                "requested_at": "2026-08-24T00:01:30+00:00",
                "work_subject": "ticket:3",
                "generation": 1,
                "semantic_attempt_id": attempt["attempt_id"],
                "source_status": "ready_for_human",
                "human_response_supplied": True,
            },
            {
                "resume_id": "resume-2",
                "requested_at": "2026-08-24T00:07:00+00:00",
                "work_subject": "ticket:3",
                "generation": 1,
                "semantic_attempt_id": attempt["attempt_id"],
                "source_status": "ready_for_human",
                "human_response_supplied": False,
            },
        ],
    }
    state: dict[str, object] = {
        "run_id": "run-human-pause",
        "ticket_jobs": {"3": {"ticket_number": 3}},
        "human_response_audit": {"resume-1": "Access granted."},
    }

    records = history_records(state, audit)
    points = records[0]["turning_points"]

    assert [point["kind"] for point in points] == [
        "human_blocker",
        "resume",
        "human_blocker",
        "resume",
        "human_blocker",
    ]
    assert [point.get("resume_id") for point in points if point["kind"] == "resume"] == [
        "resume-1",
        "resume-2",
    ]
    assert audit["timeline"] == timeline


def test_history_preserves_distinct_resume_events_with_same_content() -> None:
    attempt = {
        "attempt_id": "attempt-resume-history",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "pending",
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "ticket_jobs": {
            "3": {
                "ticket_number": 3,
                "semantic_attempt_history": [attempt],
                "review_budget": {
                    "window": 1,
                    "development_attempts": 1,
                    "reviewer_invocations": 0,
                    "review_artifacts": [],
                },
            }
        },
    }
    state["human_response_audit"] = {
        "resume-1": "same response",
        "resume-2": "same response",
    }
    audit = {
        "semantic_agent_attempts": [attempt],
        "agent_invocations": [],
        "timeline": [],
        "timeline_continuation": [],
        "agent_resumes": [
            {
                "resume_id": "resume-1",
                "requested_at": "2026-08-24T01:00:00+00:00",
                "work_subject": "ticket:3",
                "generation": 1,
                "semantic_attempt_id": attempt["attempt_id"],
                "source_status": "execution_failed",
                "human_response_supplied": True,
            },
            {
                "resume_id": "resume-2",
                "requested_at": "2026-08-24T02:00:00+00:00",
                "work_subject": "ticket:3",
                "generation": 1,
                "semantic_attempt_id": attempt["attempt_id"],
                "source_status": "execution_failed",
                "human_response_supplied": True,
            },
        ],
    }

    records = history_records(state, audit)

    assert len(records) == 1
    assert [point["resume_id"] for point in records[0]["turning_points"]] == [
        "resume-1",
        "resume-2",
    ]


@pytest.mark.parametrize(
    ("control_activity", "expected_activity"),
    [("running", "running"), ("unknown", "unknown"), ("not_running", "interrupted")],
)
def test_history_open_resumption_does_not_use_prior_end_as_record_end(
    control_activity: str, expected_activity: str
) -> None:
    attempt = {
        "attempt_id": "attempt-open-resumption",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "pending",
    }
    first = {
        "work_subject": "ticket:3",
        "role": "development",
        "status": "completed",
        "started_at": "2026-08-24T01:00:00+00:00",
        "ended_at": "2026-08-24T01:01:00+00:00",
        "semantic_attempt": deepcopy(attempt),
    }
    resumed = {
        "work_subject": "ticket:3",
        "role": "development",
        "status": "running",
        "started_at": "2026-08-24T02:00:00+00:00",
        "ended_at": None,
        "semantic_attempt": deepcopy(attempt),
    }
    job = {
        "ticket_number": 3,
        "semantic_attempt_history": [attempt],
        "review_budget": {
            "window": 1,
            "development_attempts": 1,
            "reviewer_invocations": 0,
            "review_artifacts": [],
        },
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "ticket_jobs": {"3": job},
        "active_ticket_job": deepcopy(job),
        "active_agent_invocation": resumed,
        "_executor_control": {"activity": control_activity},
    }
    audit = {
        "semantic_agent_attempts": [attempt],
        "agent_invocations": [first, resumed],
        "timeline": [],
        "timeline_continuation": [],
        "agent_resumes": [],
    }

    record = history_records(state, audit)[0]

    assert record["started_at"] == "2026-08-24T01:00:00+00:00"
    assert record["ended_at"] is None
    assert record["activity"] == expected_activity
    if expected_activity == "running":
        assert record["span_seconds"] is not None
        assert record["execution_seconds"] is not None
    else:
        assert record["span_seconds"] is None
        assert record["execution_seconds"] is None


@pytest.mark.parametrize(
    ("latest_status", "control_activity", "expected_activity"),
    [
        ("running", "running", "running"),
        ("running", "unknown", "unknown"),
        ("running", "not_running", "interrupted"),
        ("completed", "not_running", "not_running"),
    ],
)
def test_history_does_not_accumulate_prior_unclosed_invocation(
    latest_status: str,
    control_activity: str,
    expected_activity: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempt = {
        "attempt_id": "attempt-prior-unclosed",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "pending",
    }
    historical = {
        "work_subject": "ticket:3",
        "role": "development",
        "status": "failed",
        "started_at": "2000-01-01T00:00:00+00:00",
        "ended_at": None,
        "interruption_observed_at": "2000-01-01T00:01:00+00:00",
        "semantic_attempt": deepcopy(attempt),
    }
    latest = {
        "work_subject": "ticket:3",
        "role": "development",
        "status": latest_status,
        "started_at": "2000-01-01T01:00:00+00:00",
        "ended_at": (
            None
            if latest_status == "running"
            else "2000-01-01T01:01:00+00:00"
        ),
        "semantic_attempt": deepcopy(attempt),
    }
    job = {
        "ticket_number": 3,
        "semantic_attempt_history": [attempt],
        "review_budget": {
            "window": 1,
            "development_attempts": 1,
            "reviewer_invocations": 0,
            "review_artifacts": [],
        },
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "ticket_jobs": {"3": job},
        "active_ticket_job": deepcopy(job),
        "active_agent_invocation": latest,
        "_executor_control": {"activity": control_activity},
    }
    audit = {
        "semantic_agent_attempts": [attempt],
        "agent_invocations": [historical, latest],
        "timeline": [],
        "timeline_continuation": [],
        "agent_resumes": [],
    }

    record = history_records(state, audit)[0]

    assert record["activity"] == expected_activity
    assert record["execution_seconds"] is None
    if expected_activity == "running":
        assert record["span_seconds"] is not None
    else:
        assert record["span_seconds"] is None

    cli.cli_presentation._print_history(state, as_json=False, plain=True)
    default_output = capsys.readouterr().out
    cli.cli_presentation._print_history(state, as_json=False, plain=True, details=True)
    details_output = capsys.readouterr().out
    assert "累计执行 未知" in default_output
    assert "累计执行 未知" in details_output
    assert "→ 未知时间" in details_output


def test_history_running_attempt_reports_current_elapsed_time() -> None:
    attempt = {
        "attempt_id": "attempt-running",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "pending",
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "development",
        "status": "running",
        "started_at": "2000-01-01T00:00:00+00:00",
        "ended_at": None,
        "semantic_attempt": deepcopy(attempt),
    }
    job = {
        "ticket_number": 3,
        "pending_semantic_attempt": attempt,
        "review_budget": {
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 0,
        },
    }
    state: dict[str, object] = {
        "run_id": "run-1",
        "ticket_jobs": {"3": job},
        "active_agent_invocation": invocation,
        "_executor_control": {"activity": "running"},
    }
    audit = {
        "semantic_agent_attempts": [attempt],
        "agent_invocations": [invocation],
        "timeline": [],
        "timeline_continuation": [],
        "agent_resumes": [],
    }

    record = history_records(state, audit)[0]
    assert record["activity"] == "running"
    assert record["span_seconds"] is not None
    assert record["execution_seconds"] is not None
    assert record["span_seconds"] <= int(
        (datetime.now(UTC) - datetime(2000, 1, 1, tzinfo=UTC)).total_seconds()
    )
    assert record["status_text"] == "进行中"


def test_history_details_reuses_development_acceptance_and_publication_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def attempt(attempt_id: str, role: str, ordinal: int) -> dict[str, object]:
        value: dict[str, object] = {
            "attempt_id": attempt_id,
            "role": role,
            "work_subject": "ticket:3",
            "generation": 1,
            "currentness_boundary_fingerprint": f"sha256:{attempt_id}",
            "ordinal": ordinal,
            "budget_window": 1 if role != "publication" else None,
            "status": "completed",
            "outcome": "candidate" if role == "development" else f"{role}_artifact",
        }
        if role != "publication":
            value["budget_snapshot"] = {
                "window": 1,
                "development_attempts": 1,
                "reviewer_invocations": 1,
            }
        return value

    development = attempt("attempt-development-1", "development", 1)
    review = attempt("attempt-reviewer-1", "reviewer", 1)
    publication = attempt("attempt-publication-1", "publication", 1)
    review_finding = "问题：需要补充验证；证据：e2e log；必须修复：增加断言；复验：重跑测试。"
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Details"},
        "created_at": "2026-08-24T00:00:00+00:00",
        "status": "completed",
        "timeline": [],
        "timeline_continuation": [],
        "ticket_jobs": {
            "3": {
                "ticket_number": 3,
                "development_summary": "Implemented the boundary handling.",
                "publication": {
                    "commit_message": "feat: publish ticket",
                    "pr_title": "feat: publish ticket",
                    "pr_body_markdown": "The candidate is ready for review.",
                },
                "review_budget": {
                    "window": 1,
                    "development_attempts": 1,
                    "reviewer_invocations": 1,
                    "review_artifacts": [
                        {
                            "reviewer_thread_id": "reviewer-thread",
                            "candidate_sha": "candidate-a",
                            "artifact": {
                                "checks": {
                                    "e2e": {
                                        "status": "fail",
                                        "evidence": "e2e log",
                                        "findings": [review_finding],
                                    },
                                    "standards": {
                                        "status": "pass",
                                        "evidence": "standards evidence",
                                        "findings": [],
                                    },
                                    "spec": {
                                        "status": "pass",
                                        "evidence": "spec evidence",
                                        "findings": [],
                                    },
                                }
                            },
                        }
                    ],
                },
            }
        },
        "agent_invocation_history": [
            {
                "work_subject": "ticket:3",
                "role": role,
                "status": "completed",
                "started_at": f"2026-08-24T00:0{index}:00+00:00",
                "ended_at": f"2026-08-24T00:0{index + 1}:00+00:00",
                "reported_thread_id": (
                    "reviewer-thread" if role == "reviewer" else None
                ),
                "currentness_boundary": (
                    {"run_head_sha": "candidate-a"} if role == "reviewer" else {}
                ),
                "semantic_attempt": deepcopy(item),
            }
            for index, (role, item) in enumerate(
                (("development", development), ("reviewer", review), ("publication", publication)),
                start=1,
            )
        ],
        "semantic_agent_attempts": [development, review, publication],
        "resume_audit": {"history": []},
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=False, plain=True, details=True)
    output = capsys.readouterr().out

    assert "开发说明" in output
    assert "Implemented the boundary handling." in output
    assert "验收证据" in output
    assert "e2e log" in output
    assert "必须修复：增加断言" in output
    assert "feat: publish ticket" in output
    assert "PR 标题" in output
    assert "第 1 次授权额度；本轮开始时已用：开发 1 / 4 次；验收 1 / 3 次" in output
    assert "执行额度：不适用（编写发布说明）" in output

    cli.cli_presentation._print_history(state, as_json=True)
    assert "budget_snapshot" not in capsys.readouterr().out


def test_history_default_keeps_the_complete_finding_question(
    capsys: pytest.CaptureFixture[str],
) -> None:
    question = "中文问题" * 90 + "QUESTION-END"
    finding = (
        f"问题：{question}；证据：evidence；必须修复：repair；复验：verify。"
    )
    attempt = {
        "attempt_id": "attempt-long-finding",
        "role": "reviewer",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "outcome": "acceptance_artifact",
    }
    artifact = {
        "checks": {
            "e2e": {"findings": [finding]},
            "standards": {"findings": []},
            "spec": {"findings": []},
        }
    }
    job = {
        "ticket_number": 3,
        "semantic_attempt_history": [attempt],
        "review_budget": {
            "window": 1,
            "development_attempts": 0,
            "reviewer_invocations": 1,
            "review_artifacts": [
                {
                    "reviewer_thread_id": "reviewer-long",
                    "candidate_sha": "candidate-long",
                    "artifact": artifact,
                }
            ],
        },
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "reviewer",
        "status": "completed",
        "started_at": "2026-08-24T00:01:00+00:00",
        "ended_at": "2026-08-24T00:02:00+00:00",
        "reported_thread_id": "reviewer-long",
        "currentness_boundary": {"run_head_sha": "candidate-long"},
        "semantic_attempt": deepcopy(attempt),
    }
    state: dict[str, object] = {
        "run_id": "run-long",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Long finding"},
        "created_at": "2026-08-24T00:00:00+00:00",
        "status": "completed",
        "ticket_jobs": {"3": job},
        "active_ticket_job": deepcopy(job),
        "agent_invocation_history": [invocation],
    }
    cli.cli_presentation._print_history(state, as_json=False, plain=True)
    output = capsys.readouterr().out

    assert question in output
    assert "QUESTION-END" in output
    assert "已截断" not in output


def test_history_matches_review_artifact_by_attempt_candidate_and_reviewer() -> None:
    def attempt(attempt_id: str, boundary: str) -> dict[str, object]:
        return {
            "attempt_id": attempt_id,
            "role": "reviewer",
            "work_subject": "ticket:3",
            "generation": 1,
            "currentness_boundary_fingerprint": f"fingerprint-{boundary}",
            "ordinal": 1,
            "budget_window": 1,
            "status": "completed",
            "outcome": "acceptance_artifact",
        }

    def invocation(
        semantic_attempt: dict[str, object], candidate: str, reviewer: str
    ) -> dict[str, object]:
        return {
            "work_subject": "ticket:3",
            "role": "reviewer",
            "reported_thread_id": reviewer,
            "currentness_boundary": {"run_head_sha": candidate},
            "status": "completed",
            "started_at": "2026-08-24T00:01:00+00:00",
            "ended_at": "2026-08-24T00:02:00+00:00",
            "semantic_attempt": deepcopy(semantic_attempt),
        }

    first = attempt("attempt-a", "candidate-a")
    second = attempt("attempt-b", "candidate-b")

    def artifact(name: str) -> dict[str, object]:
        finding = f"问题：{name}；证据：{name}；必须修复：{name}；复验：{name}。"
        return {
            "checks": {
                lane: {
                    "status": "fail" if lane == "e2e" else "pass",
                    "findings": [finding] if lane == "e2e" else [],
                }
                for lane in ("e2e", "standards", "spec")
            }
        }

    state: dict[str, object] = {
        "run_id": "run-1",
        "ticket_jobs": {
            "3": {
                "ticket_number": 3,
                "semantic_attempt_history": [first, second],
                "review_budget": {
                    "window": 1,
                    "review_artifacts": [
                        {
                            "reviewer_thread_id": "reviewer-a",
                            "candidate_sha": "candidate-a",
                            "artifact": artifact("candidate-a"),
                        },
                        {
                            "reviewer_thread_id": "reviewer-b",
                            "candidate_sha": "candidate-b",
                            "artifact": artifact("candidate-b"),
                        },
                    ],
                },
            }
        },
    }
    state["active_ticket_job"] = deepcopy(state["ticket_jobs"]["3"])
    audit = {
        "semantic_agent_attempts": [first, second],
        "agent_invocations": [
            invocation(first, "candidate-a", "reviewer-a"),
            invocation(second, "candidate-b", "reviewer-b"),
        ],
        "timeline": [],
        "timeline_continuation": [],
        "agent_resumes": [],
    }

    records = history_records(state, audit)

    assert [record["attempt_id"] for record in records] == ["attempt-a", "attempt-b"]
    assert records[0]["findings"] == [
        "问题：candidate-a；证据：candidate-a；必须修复：candidate-a；复验：candidate-a。"
    ]
    assert records[1]["findings"] == [
        "问题：candidate-b；证据：candidate-b；必须修复：candidate-b；复验：candidate-b。"
    ]


def test_timeline_projects_semantic_invocation_output_and_retry_counters(
    tmp_path: Path,
) -> None:
    attempt = {
        "attempt_id": "attempt-publication-2",
        "role": "publication",
        "work_subject": "ticket:3",
        "generation": 2,
        "currentness_boundary_fingerprint": "sha256:boundary",
        "ordinal": 2,
        "budget_window": None,
        "status": "pending",
    }
    state: dict[str, Any] = {
        "run_id": "run-1",
        "status": "execution_failed",
        "active_ticket_job": {
            "ticket_number": 3,
            "phase": "publication_pending",
            "pending_semantic_attempt": attempt,
            "publication_operation_retry": {"attempts": 2, "limit": 4},
        },
        "active_agent_invocation": {
            "work_subject": "ticket:3",
            "role": "publication",
            "status": "failed",
            "attempt_count": 1,
            "started_at": "2026-08-24T00:00:00+00:00",
            "semantic_attempt": deepcopy(attempt),
        },
        "timeline": [],
    }
    store = StateStore(tmp_path)

    store.save_run("run-1", state)
    state["active_agent_invocation"]["attempt_count"] = 2
    store.save_run("run-1", state)

    timeline = store.load_run("run-1")["timeline"]
    assert [event["output_attempt"] for event in timeline] == [1, 2]
    assert timeline[-1] | {"at": "ignored"} == {
        "at": "ignored",
        "kind": "ticket_phase",
        "status": "execution_failed",
        "ticket": 3,
        "worker": "发布工作代理",
        "phase": "publication_pending",
        "semantic_attempt_id": "attempt-publication-2",
        "semantic_attempt_role": "publication",
        "semantic_attempt_ordinal": 2,
        "budget_window": None,
        "agent_invocation_started_at": "2026-08-24T00:00:00+00:00",
        "agent_invocation_status": "failed",
        "output_attempt": 2,
        "publication_operation_retry_attempts": 2,
        "publication_operation_retry_limit": 4,
    }


def test_status_exposes_preserved_dirty_checkout_and_recovery_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "status": "completed",
        "diagnostics": [],
        "delivery_cleanup": {
            "status": "cleanup_pending",
            "last_error": "preserved dirty checkout",
            "items": {
                "agent-run/ticket-3": {
                    "kind": "ticket",
                    "branch": "agent-run/ticket-3",
                    "checkout": "/repo/.agent-run/worktrees/run-1/ticket-3",
                    "attempts": 3,
                    "status": "cleanup_pending",
                    "last_error": "tracked modifications; agent-run resume run-1",
                }
            },
        },
    }

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    assert output["delivery_cleanup"] == {
        "status": "cleanup_pending",
        "last_error": "preserved dirty checkout",
        "items": [
            {
                "kind": "ticket",
                "branch": "agent-run/ticket-3",
                "checkout": "/repo/.agent-run/worktrees/run-1/ticket-3",
                "status": "cleanup_pending",
                "last_error": "tracked modifications; agent-run resume run-1",
                "recovery_action": "agent-run resume run-1",
            }
        ],
    }
    assert output["next_action"] == "agent-run resume run-1"

    cli.cli_presentation._print_status(state, as_json=False)
    human = capsys.readouterr().out
    assert "已保留 1 个开发工作区" in human
    assert "/repo/.agent-run/worktrees/run-1/ticket-3" in human
    assert "tracked modifications" not in human
    assert "run-1" not in human.replace("/repo/.agent-run/worktrees/run-1/ticket-3", "")


def test_operator_action_keeps_repository_names_starting_with_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_operator_action(
        {
            "type": "Human Blocker",
            "object": "Ticket #2",
            "phase": "candidate",
            "reasons": ["需要操作者确认"],
            "trigger_invocation": None,
            "preserved": "当前状态与已有审计证据",
            "next_action": (
                "agent-run resume 1 --repo run-1-1234567890abcdef/project"
            ),
        }
    )

    output = capsys.readouterr().out
    assert "agent-run resume 1 --repo run-1-1234567890abcdef/project" in output
    assert "<run-id>/project" not in output
    assert human_next_action(
        "agent-run resume run-1-1234567890abcdef-2",
        run_id="run-1-1234567890abcdef-2",
    ) == "agent-run resume <run-id>"
    print_operator_action(
        {
            "type": "Deterministic Contradiction",
            "object": "Ticket #2",
            "phase": "blocked",
            "reasons": ["Candidate mismatch"],
            "trigger_invocation": None,
            "preserved": "当前状态与已有审计证据",
            "next_action": "agent-run abandon run-1-1234567890abcdef-2",
        },
        run_id="run-1-1234567890abcdef-2",
    )
    contradiction = capsys.readouterr().out
    assert "agent-run abandon <run-id>" in contradiction
    assert "run-1-1234567890abcdef-2" not in contradiction


def test_history_matches_responses_and_findings_to_their_subject_and_window(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def artifact(finding: str) -> dict[str, object]:
        return {
            "checks": {
                "e2e": {"findings": [finding]},
                "standards": {"findings": []},
                "spec": {"findings": []},
            }
        }

    state: dict[str, object] = {
        "run_id": "run-1-1234567890abcdef",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Parent spec"},
        "created_at": "2026-08-30T00:00:00+00:00",
        "status": "active",
        "timeline": [],
        "ticket_jobs": {
            "2": {
                "human_response_history": [{"response": "response-for-ticket-2"}],
                "review_budget_history": [
                    {
                        "review_budget": {
                            "window": 1,
                                "review_artifacts": [
                                    {
                                        "reviewer_thread_id": "reviewer-old",
                                        "candidate_sha": "old-candidate",
                                        "artifact": artifact("old-window"),
                                    }
                                ],
                        }
                    }
                ],
                "review_budget": {
                    "window": 2,
                        "review_artifacts": [
                            {
                                "reviewer_thread_id": "reviewer-ticket-2",
                                "candidate_sha": "current-candidate",
                                "artifact": artifact("current-window"),
                            }
                        ],
                },
            },
            "3": {
                "human_response_history": [{"response": "response-for-ticket-3"}],
                "review_budget_history": [],
                "review_budget": {"window": 1, "review_artifacts": []},
            },
        },
        "agent_invocation_history": [
            {
                "started_at": "2026-08-30T00:01:00+00:00",
                "ended_at": "2026-08-30T00:02:00+00:00",
                "status": "completed",
                "phase": "candidate",
                    "work_subject": "ticket:2",
                    "invocation_role": "reviewer",
                    "reported_thread_id": "reviewer-ticket-2",
                    "currentness_boundary": {"run_head_sha": "current-candidate"},
                    "semantic_attempt": {
                    "role": "reviewer",
                    "ordinal": 1,
                    "budget_window": 2,
                },
            }
        ],
        "resume_audit": {
            "history": [
                {
                    "requested_at": "2026-08-30T00:03:00+00:00",
                    "work_subject": "ticket:3",
                    "source_status": "ready_for_human",
                    "human_response_supplied": True,
                },
                {
                    "requested_at": "2026-08-30T00:04:00+00:00",
                    "work_subject": "ticket:2",
                    "source_status": "ready_for_human",
                    "human_response_supplied": True,
                },
            ]
        },
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=True)
    output = json.loads(capsys.readouterr().out)
    review = next(event for event in output["events"] if event["kind"] == "review")
    resumes = [event for event in output["events"] if event["kind"] == "resume"]

    assert review["details"] == ["current-window"]
    assert [(event["object"], event["details"]) for event in resumes] == [
        ("Ticket #3", ["response-for-ticket-3"]),
        ("Ticket #2", ["response-for-ticket-2"]),
    ]


def test_terminal_status_and_history_share_a_stable_elapsed_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1-1234567890abcdef",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Parent spec"},
        "created_at": "2026-08-30T00:00:00+00:00",
        "status": "completed",
        "timeline": [
            {
                "at": "2026-08-30T00:00:10+00:00",
                "kind": "run_status",
                "status": "completed",
            }
        ],
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=True)
    first_status = json.loads(capsys.readouterr().out)
    cli.cli_presentation._print_status(state, as_json=True)
    second_status = json.loads(capsys.readouterr().out)
    cli.cli_presentation._print_history(state, as_json=True)
    history = json.loads(capsys.readouterr().out)

    assert first_status["elapsed_seconds"] == 10
    assert second_status["elapsed_seconds"] == 10
    assert history["summary"]["elapsed_seconds"] == 10


def test_history_does_not_assign_a_new_generation_response_to_an_old_resume(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1-1234567890abcdef",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Parent spec"},
        "created_at": "2026-08-30T00:00:00+00:00",
        "status": "active",
        "timeline": [],
        "ticket_jobs": {
            "2": {
                "human_response_history": [
                    {"generation": 2, "response": "new-generation-response"}
                ]
            }
        },
        "resume_audit": {
            "history": [
                {
                    "requested_at": "2026-08-30T00:01:00+00:00",
                    "work_subject": "ticket:2",
                    "generation": 1,
                    "source_status": "ready_for_human",
                    "human_response_supplied": True,
                },
                {
                    "requested_at": "2026-08-30T00:02:00+00:00",
                    "work_subject": "ticket:2",
                    "generation": 2,
                    "source_status": "ready_for_human",
                    "human_response_supplied": True,
                },
            ]
        },
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=True)
    output = json.loads(capsys.readouterr().out)
    resumes = [event for event in output["events"] if event["kind"] == "resume"]

    assert [event["details"] for event in resumes] == [
        [],
        ["new-generation-response"],
    ]


def test_status_labels_the_latest_agent_with_its_own_ticket(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1-1234567890abcdef",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Parent spec"},
        "created_at": "2026-08-30T00:00:00+00:00",
        "status": "active",
        "active_ticket_job": {"ticket_number": 3, "phase": "developing"},
        "ticket_jobs": {
            "2": {"ticket_number": 2, "phase": "completed"},
            "3": {"ticket_number": 3, "phase": "developing"},
        },
        "agent_invocation_history": [
            {
                "started_at": "2026-08-30T00:01:00+00:00",
                "ended_at": "2026-08-30T00:02:00+00:00",
                "status": "completed",
                "work_subject": "ticket:2",
                "role": "publication",
                "model": "fixture-agent",
                "reasoning_effort": "high",
            }
        ],
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=False)
    output = capsys.readouterr().out

    assert "当前对象:   子任务 #3" in output
    assert "最近 Agent: 发布 Agent · 子任务 #2" in output
    assert "发布 Agent · 子任务 #3" not in output


def test_status_localizes_profiled_review_agent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "active",
        "active_ticket_job": {
            "ticket_number": 2,
            "phase": "reviewing",
        },
        "active_agent_invocation": {
            "work_subject": "ticket:2",
            "invocation_role": "review",
            "status": "failed",
            "started_at": "2026-08-30T00:01:00+00:00",
            "ended_at": "2026-08-30T00:02:00+00:00",
            "model": "review-model",
            "reasoning_effort": "high",
        },
        "agent_invocation_history": [
            {
                "work_subject": "ticket:2",
                "invocation_role": "review",
                "status": "failed",
                "started_at": "2026-08-30T00:01:00+00:00",
                "ended_at": "2026-08-30T00:02:00+00:00",
                "model": "review-model",
                "reasoning_effort": "high",
            }
        ],
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=False)
    output = capsys.readouterr().out

    assert "最近 Agent: 验收 Agent · 子任务 #2" in output
    assert "最近 Agent: review ·" not in output

    cli.cli_presentation._print_history(state, as_json=False)
    history_output = capsys.readouterr().out

    assert "Review Agent" not in history_output
    assert "· review ·" not in history_output


def test_history_uses_chinese_role_names_for_all_role_aliases(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = [
        ("development", "development"),
        ("reviewer", "reviewer"),
        ("fresh_acceptance", "fresh_acceptance"),
        ("publication", "publication"),
        ("final_publication", "final_publication"),
    ]
    semantic_attempts = []
    invocations = []
    for index, (role, invocation_role) in enumerate(attempts, start=1):
        attempt = {
            "attempt_id": f"history-role-{index}",
            "role": role,
            "work_subject": "ticket:3",
            "generation": 1,
            "ordinal": index,
            "budget_window": 1,
            "status": "completed",
        }
        semantic_attempts.append(attempt)
        invocations.append(
            {
                "work_subject": "ticket:3",
                "role": role,
                "invocation_role": invocation_role,
                "status": "completed",
                "started_at": f"2026-09-10T00:0{index}:00+00:00",
                "ended_at": f"2026-09-10T00:0{index}:30+00:00",
                "semantic_attempt": deepcopy(attempt),
            }
        )
    state: dict[str, object] = {
        "run_id": "run-history-role-labels",
        "repository": "example/project",
        "parent": {"number": 1, "title": "History role labels"},
        "status": "completed",
        "semantic_agent_attempts": semantic_attempts,
        "agent_invocation_history": invocations,
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=True)
    machine_output = json.loads(capsys.readouterr().out)
    machine_roles = {
        event["role"]
        for event in machine_output["events"]
        if event.get("kind") in {"development", "review", "publication"}
    }
    assert {"Development Agent", "Review Agent", "Publication Agent"} <= (
        machine_roles
    )

    cli.cli_presentation._print_history(
        state, as_json=False, plain=True, details=True
    )
    plain_output = capsys.readouterr().out
    for label in ("开发 Agent", "验收 Agent", "发布 Agent"):
        assert label in plain_output
    for label in ("Development Agent", "Review Agent", "Publication Agent"):
        assert label not in plain_output

    from io import StringIO
    from rich.text import Text

    class TerminalBuffer(StringIO):
        def isatty(self) -> bool:
            return True

    rich_output = TerminalBuffer()
    monkeypatch.setattr(sys, "stdout", rich_output)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    cli.cli_presentation._print_history(state, as_json=False)
    rich_visible = Text.from_ansi(rich_output.getvalue()).plain
    for label in ("开发 Agent", "验收 Agent", "发布 Agent"):
        assert label in rich_visible
    for label in ("Development Agent", "Review Agent", "Publication Agent"):
        assert label not in rich_visible


def test_status_localizes_pending_publication_phase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Status card"},
        "status": "publication_pending",
        "run_acceptance": {
            "phase": "accepted",
            "reviewed_head_sha": "run-head",
        },
        "run_publication": {"phase": "pending"},
        "diagnostics": [],
    }

    cli.cli_presentation._print_status(state, as_json=False)
    output = capsys.readouterr().out

    assert "阶段:       待发布" in output
    assert "阶段:       pending" not in output

    state["run_publication"] = {"phase": "pending"}
    state["run_acceptance"] = {"phase": "pending"}
    cli.cli_presentation._print_status(state, as_json=False)
    output = capsys.readouterr().out

    assert "阶段:       待验收" in output
    assert "阶段:       pending" not in output


def test_status_distinguishes_stale_dirty_checkout_from_resumable_work(
    capsys: pytest.CaptureFixture[str],
) -> None:
    checkout = "/repo/.agent-run/worktrees/run-1/run-repair"
    state: dict[str, object] = {
        "run_id": "run-1",
        "repository": "example/project",
        "parent": {"number": 1},
        "status": "run_acceptance_pending",
        "diagnostics": [],
        "delivery_cleanup": {
            "status": "cleanup_pending",
            "last_error": "untracked files",
            "items": {
                "agent-run-repair/run-1/1": {
                    "kind": "run_repair",
                    "branch": "agent-run-repair/run-1/1",
                    "checkout": checkout,
                    "attempts": 0,
                    "status": "cleanup_pending",
                    "last_error": "untracked files",
                    "recovery_kind": "stale_dirty_checkout",
                }
            },
        },
    }

    cli.cli_presentation._print_status(state, as_json=True)
    output = json.loads(capsys.readouterr().out)

    recovery = output["delivery_cleanup"]["items"][0]["recovery_action"]
    assert f"copy/salvage {checkout}" in recovery
    assert "run 1 to retire it and continue fresh Run Acceptance" in recovery
    assert "abandon run-1 --discard-worktree" in recovery
    assert output["next_action"].startswith("先检查并把 stale")
    assert "agent-run run 1" in output["next_action"]

    human = str(cli.cli_presentation.human_next_action_for_state(state))
    assert "agent-run run 1 --repo example/project" in human
    assert (
        "agent-run abandon 1 --repo example/project --discard-worktree"
        in human
    )
    assert "run-1" not in human
    assert "<run-id>" not in human
    for internal in ("stale", "Managed Development Checkout", "fresh Run Acceptance", "clean"):
        assert internal not in human


@pytest.mark.parametrize(
    "command", ["start", "deliver", "accept-run", "publish-run"]
)
def test_removed_stage_commands_are_rejected_by_the_real_cli(command: str) -> None:
    environment = os.environ.copy()
    source_path = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else f"{source_path}{os.pathsep}{environment['PYTHONPATH']}"
    )
    result = subprocess.run(
        [sys.executable, "-m", "agent_run", command, "run-id"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_removed_promotion_command_is_rejected_by_the_public_cli(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(PROJECT_ROOT)

    with pytest.raises(SystemExit) as error:
        main(["promotion-handshake", "not-a-sha"])

    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_source_checkout_cannot_run_production_lifecycle_without_active_runner(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(git_repo)

    assert main(["run", "1", "--repo", "example/project", "--json"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["result"] == "error"
    assert output["status"] == "blocked"
    assert not (git_repo / ".agent-run").exists()


def test_production_run_without_a_real_executor_host_fails_before_writes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class UnavailableSystemdHost:
        def __init__(self, **_options: object) -> None:
            pass

        def check_readiness(self, *, command: object = None) -> None:
            raise cli.ExecutionReadinessError("user systemd unavailable")

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    monkeypatch.setattr(cli, "_running_active_runner", lambda: True)
    monkeypatch.setattr(cli, "SystemdUserExecutorHost", UnavailableSystemdHost)
    monkeypatch.setattr(
        cli,
        "FakeExecutorHost",
        lambda: pytest.fail("production run must not construct the fixture host"),
    )

    assert main(["run", "1", "--repo", "example/project", "--json"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "execution_readiness"
    assert not (git_repo / ".agent-run").exists()


def test_production_run_defers_environment_capture_to_action_admission(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class AvailableSystemdHost:
        def __init__(self, **_options: object) -> None:
            self.readiness_commands: list[object] = []
            self.prepared_commands: list[tuple[str, ...]] = []
            hosts.append(self)

        def check_readiness(self, *, command: object = None) -> None:
            self.readiness_commands.append(command)

        def prepare_environment(self, command: Sequence[str]) -> None:
            self.prepared_commands.append(tuple(command))
            raise cli.SystemdExecutionReadinessError("发起终端环境过大")

    hosts: list[AvailableSystemdHost] = []

    def enter_lifecycle(*_args: object, **options: object) -> None:
        prepare = options.get("prepare_executor_session")
        assert callable(prepare)
        prepare()
        pytest.fail("environment rejection must stop lifecycle admission")

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_DATA_HOME", str(git_repo / "runner-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(git_repo / "runner-state"))
    monkeypatch.setattr(cli, "_running_active_runner", lambda: True)
    monkeypatch.setattr(cli, "SystemdUserExecutorHost", AvailableSystemdHost)
    monkeypatch.setattr(cli, "_run_lifecycle", enter_lifecycle)

    assert main(["run", "1", "--repo", "example/project", "--json"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"][0]["code"] == "execution_readiness"
    assert len(hosts) == 1
    assert hosts[0].readiness_commands == [None]
    assert hosts[0].prepared_commands == [
        ("run", "1", "--repo", "example/project", "--json")
    ]
    assert not (git_repo / ".agent-run" / "task-control").exists()


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
    environment.setdefault("XDG_STATE_HOME", str(repo / ".agent-run-test-state"))
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
            "--github-fixture",
            str(fixture),
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
    git = GitRepository.discover(repo)
    states = (
        FaultInjectingStateStore(
            repo / ".agent-run", crash_after_save=crash_after_save
        )
        if crash_after_save is not None
        else StateStore(repo / ".agent-run")
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


def test_seed_run_creates_one_run_branch_and_is_idempotent(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": issue(2), "3": issue(3)},
    )

    first = seed_run(git_repo, fixture, "1")
    assert first.returncode == 0, first.stderr
    first_output = stdout_json(first)
    state = load_only_run_state(git_repo)

    second = seed_run(git_repo, fixture, "1")
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


def test_seed_run_preserves_the_managed_run_branch_side_effect(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})

    started = seed_run(git_repo, fixture, "1")

    assert started.returncode == 0, started.stderr
    state = load_only_run_state(git_repo)
    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert state["run_branch"] in branches


def test_run_repair_docs_describe_job_rotation_and_candidate_promotion() -> None:
    context = (PROJECT_ROOT / "CONTEXT.md").read_text(encoding="utf-8")
    operator_guide = (PROJECT_ROOT / "docs" / "agent-run.md").read_text(
        encoding="utf-8"
    )

    assert "一个 Cycle 可依次包含多个 Run Repair Job" in context
    assert "轮转出后继 Job" in context
    assert "Candidate Run Acceptance → 严格 promotion" in operator_guide
    assert "归档旧 Job/PR 并轮转新的 branch/PR" in operator_guide
    assert "一个活跃 Run Repair Job 实现一个 Repair Cycle" not in context
    assert "Run Repair → fresh Run Acceptance → 新 PR" not in operator_guide


def test_status_and_history_locate_a_new_run_from_an_unrelated_directory(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    started = seed_run(git_repo, fixture, "1", extra_env=locator_env)
    run_id = stdout_json(started)["run_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=elsewhere, check=True)

    status = run_cli(
        elsewhere, fixture, "status", run_id, "--json", extra_env=locator_env
    )
    history = run_cli(
        elsewhere, fixture, "history", run_id, "--json", extra_env=locator_env
    )

    assert status.returncode == history.returncode == 0
    assert stdout_json(status)["run_id"] == run_id
    assert stdout_json(history)["run_id"] == run_id
    assert not (elsewhere / ".agent-run").exists()
    locator = json.loads(
        (locator_home / "agent-run" / "run-locator.json").read_text(encoding="utf-8")
    )
    assert locator == {
        "entries": [
            {
                "run_id": run_id,
                "repository_root": str(git_repo.resolve()),
                "state_dir": str((git_repo / ".agent-run").resolve()),
                "updated_at": locator["entries"][0]["updated_at"],
                "repository": "example/project",
                "parent_number": 1,
            }
        ]
    }


def test_status_and_history_do_not_initialize_online_dependencies(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline command initialized an online dependency")

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("XDG_STATE_HOME", str(locator_home))
    monkeypatch.setattr(cli, "SystemdUserExecutorHost", unavailable)
    monkeypatch.setattr(cli, "CodexCliBackend", unavailable)
    monkeypatch.setattr(cli, "GhGitHubReader", unavailable)

    assert main(["status", run_id, "--json"]) == 0
    assert main(["history", run_id, "--json"]) == 0
    output = capsys.readouterr().out.splitlines()
    assert json.loads(output[-2])["run_id"] == run_id
    assert json.loads(output[-1])["run_id"] == run_id


def test_new_runs_from_separate_clones_have_distinct_locator_ids(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    first_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    clone_fixture = write_fixture(clone / "github.json", issues={})

    second = seed_run(clone, clone_fixture, "1", extra_env=locator_env)

    assert second.returncode == 0, second.stderr
    second_id = stdout_json(second)["run_id"]
    assert second_id != first_id
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run_cli(
        elsewhere, fixture, "status", first_id, "--json", extra_env=locator_env
    ).returncode == 0
    assert run_cli(
        elsewhere, clone_fixture, "history", second_id, "--json", extra_env=locator_env
    ).returncode == 0


def test_status_prefers_current_directory_state_and_explicit_state_dir(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    local_repo = tmp_path / "local-repo"
    local_repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=local_repo, check=True)
    local_state_dir = local_repo / ".agent-run" / "runs"
    local_state_dir.mkdir(parents=True)
    local_state = load_only_run_state(git_repo)
    local_state["diagnostics"] = [{"code": "local_priority", "message": "local"}]
    (local_state_dir / f"{run_id}.json").write_text(
        json.dumps(local_state), encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    local = run_cli(
        local_repo, fixture, "status", run_id, "--json", extra_env=locator_env
    )
    explicit = run_cli(
        elsewhere,
        fixture,
        "status",
        run_id,
        "--state-dir",
        str(git_repo / ".agent-run"),
        "--json",
        extra_env=locator_env,
    )

    assert stdout_json(local)["diagnostics"][0]["code"] == "local_priority"
    assert stdout_json(explicit)["diagnostics"] == []


@pytest.mark.parametrize("command", ["status", "history"])
def test_read_only_locator_errors_are_actionable_and_do_not_mutate_run_history(
    git_repo: Path, tmp_path: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    before = state_path.read_text(encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    locator_path.parent.mkdir(parents=True, exist_ok=True)
    locator_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "run_id": run_id,
                        "repository_root": str(git_repo.resolve()),
                        "state_dir": str(tmp_path / "missing-state"),
                        "updated_at": "2026-08-15T00:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = run_cli(
        elsewhere, fixture, command, run_id, "--json", extra_env=locator_env
    )

    assert result.returncode == 2
    output = stdout_json(result)
    assert output["status"] == "blocked"
    assert output["diagnostics"][0]["code"] == "run_locator_stale"
    assert "--state-dir" in output["diagnostics"][0]["message"]
    assert state_path.read_text(encoding="utf-8") == before


def test_missing_and_conflicting_locators_return_dedicated_read_only_errors(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    locator_path = locator_home / "agent-run" / "run-locator.json"
    locator_path.unlink()

    missing = run_cli(
        elsewhere, fixture, "status", run_id, "--json", extra_env=locator_env
    )

    assert stdout_json(missing)["diagnostics"][0]["code"] == "run_locator_missing"
    assert not locator_path.exists()
    source_state = next((git_repo / ".agent-run" / "runs").glob("*.json")).read_text(
        encoding="utf-8"
    )
    first_state_dir = tmp_path / "first-state"
    second_state_dir = tmp_path / "second-state"
    for state_dir in (first_state_dir, second_state_dir):
        (state_dir / "runs").mkdir(parents=True)
        (state_dir / "runs" / f"{run_id}.json").write_text(
            source_state, encoding="utf-8"
        )
    locator_path.parent.mkdir(parents=True, exist_ok=True)
    locator_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "run_id": run_id,
                        "repository_root": str(git_repo.resolve()),
                        "state_dir": str(first_state_dir),
                        "updated_at": "2026-08-15T00:00:00+00:00",
                    },
                    {
                        "run_id": run_id,
                        "repository_root": str(git_repo.resolve()),
                        "state_dir": str(second_state_dir),
                        "updated_at": "2026-08-15T00:00:01+00:00",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    conflict = run_cli(
        elsewhere, fixture, "history", run_id, "--json", extra_env=locator_env
    )

    assert conflict.returncode == 2
    assert stdout_json(conflict)["diagnostics"][0]["code"] == "run_locator_conflict"


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("run", ("1",)),
        ("resume", ("{run_id}",)),
        ("requeue", ("{run_id}",)),
        ("approve", ("{run_id}",)),
        ("revise", ("{run_id}", "--message", "feedback")),
        ("abandon", ("{run_id}",)),
    ],
)
def test_lifecycle_commands_do_not_use_cross_directory_locator(
    git_repo: Path,
    tmp_path: Path,
    command: str,
    arguments: tuple[str, ...],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    before = state_path.read_text(encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    command_arguments = tuple(argument.format(run_id=run_id) for argument in arguments)

    result = run_cli(
        elsewhere, fixture, command, *command_arguments, extra_env=locator_env
    )

    assert result.returncode == 2
    assert stdout_json(result)["diagnostics"][0]["code"] == "workspace_required"
    assert state_path.read_text(encoding="utf-8") == before


def test_status_and_history_select_the_unique_current_run_without_run_id(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    started = seed_run(git_repo, fixture, "1")
    run_id = stdout_json(started)["run_id"]

    status = run_cli(git_repo, fixture, "status", "--json")
    history = run_cli(git_repo, fixture, "history", "--json")

    assert status.returncode == history.returncode == 0
    assert stdout_json(status)["run_id"] == run_id
    assert stdout_json(history)["run_id"] == run_id


def test_parent_selector_works_in_a_repo_and_across_directories(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    local = run_cli(
        git_repo, fixture, "status", "--parent", "1", "--json", extra_env=locator_env
    )
    cross_directory = run_cli(
        elsewhere,
        fixture,
        "history",
        "--repo",
        "example/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert local.returncode == cross_directory.returncode == 0
    assert stdout_json(local)["run_id"] == run_id
    assert stdout_json(cross_directory)["run_id"] == run_id


def test_parent_selector_fails_closed_when_same_repository_has_multiple_clones(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    first_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:example/project.git"],
        cwd=clone,
        check=True,
    )
    clone_fixture = write_fixture(clone / "github.json", issues={})
    second_id = stdout_json(
        seed_run(clone, clone_fixture, "1", extra_env=locator_env)
    )["run_id"]

    result = run_cli(
        git_repo,
        fixture,
        "status",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_ambiguous"
    assert {candidate["run_id"] for candidate in diagnostic["candidates"]} == {
        first_id,
        second_id,
    }


def test_repository_parent_selector_does_not_collide_on_same_issue_number(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    first_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]

    other_repo = tmp_path / "other-repo"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(other_repo)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:other/project.git"],
        cwd=other_repo,
        check=True,
    )
    other_fixture = write_fixture(
        other_repo / "github.json", issues={}, repository="other/project"
    )
    second_id = stdout_json(
        seed_run(other_repo, other_fixture, "1", extra_env=locator_env)
    )["run_id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    first = run_cli(
        elsewhere,
        fixture,
        "status",
        "--repo",
        "example/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )
    second = run_cli(
        elsewhere,
        other_fixture,
        "history",
        "--repo",
        "other/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert first.returncode == second.returncode == 0
    assert stdout_json(first)["run_id"] == first_id
    assert stdout_json(second)["run_id"] == second_id


def test_repository_parent_selector_ignores_unrelated_stale_locator(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={}, repository="a/project"
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:a/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    first_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    first_state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))

    other_repo = tmp_path / "other-repo"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(other_repo)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:b/project.git"],
        cwd=other_repo,
        check=True,
    )
    other_fixture = write_fixture(
        other_repo / "github.json", issues={}, repository="b/project"
    )
    second_id = stdout_json(
        seed_run(other_repo, other_fixture, "1", extra_env=locator_env)
    )["run_id"]
    second_state_path = other_repo / ".agent-run" / "runs" / f"{second_id}.json"
    second_state_path.unlink()
    locator_path = tmp_path / "locator-home" / "agent-run" / "run-locator.json"
    before_first_state = first_state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    status = run_cli(
        elsewhere,
        fixture,
        "status",
        "--repo",
        "a/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )
    history = run_cli(
        elsewhere,
        fixture,
        "history",
        "--repo",
        "a/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert status.returncode == history.returncode == 0
    assert stdout_json(status)["run_id"] == first_id
    assert stdout_json(history)["run_id"] == first_id

    for command in ("status", "history"):
        unrelated = run_cli(
            elsewhere,
            other_fixture,
            command,
            "--repo",
            "b/project",
            "--parent",
            "1",
            "--json",
            extra_env=locator_env,
        )

        assert unrelated.returncode == 2
        diagnostic = stdout_json(unrelated)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_stale"
    assert first_state_path.read_text(encoding="utf-8") == before_first_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not second_state_path.exists()


def test_runs_discovers_bounded_human_run_candidates(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    state = load_only_run_state(git_repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    discovered = run_cli(
        elsewhere,
        fixture,
        "runs",
        "--repo",
        "example/project",
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    output = stdout_json(discovered)
    assert output["result"] == "runs"
    assert output["repository"] == "example/project"
    assert output["runs"] == [
        {
            "parent": 1,
            "repository": "example/project",
            "repository_root": str(git_repo.resolve()),
            "run_id": run_id,
            "started_at": state["created_at"],
            "state_dir": str((git_repo / ".agent-run").resolve()),
            "status": state["status"],
        }
    ]


def test_runs_with_default_state_dir_reports_the_checkout_root(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(git_repo / ".agent-run"),
        "--json",
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == str(git_repo.resolve())
    assert candidate["state_dir"] == str((git_repo / ".agent-run").resolve())


def test_runs_with_registered_custom_state_dir_reports_the_checkout_root(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    custom_state_dir = tmp_path / "custom-state"
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    run_id = stdout_json(
        seed_run(
            git_repo,
            fixture,
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == str(git_repo.resolve())
    assert candidate["state_dir"] == str(custom_state_dir.resolve())


def test_registered_state_dir_without_checkout_identity_is_unavailable(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    custom_state_dir = tmp_path / "custom-state"
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        seed_run(
            git_repo,
            fixture,
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("checkout_identity", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    for command in ("status", "history"):
        result = run_cli(
            git_repo,
            fixture,
            command,
            "--parent",
            "1",
            "--state-dir",
            str(custom_state_dir),
            "--json",
            extra_env=locator_env,
        )
        assert result.returncode == 2
        diagnostic = stdout_json(result)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_stale"
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator


def test_explicit_repository_run_without_origin_remains_selectable(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={}, repository="a/project"
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}

    started = seed_run(
        git_repo,
        fixture,
        "1",
        "--repo",
        "a/project",
        extra_env=locator_env,
    )

    assert started.returncode == 0, started.stderr
    run_id = stdout_json(started)["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    locator_path = tmp_path / "locator-home" / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    status = run_cli(
        git_repo, fixture, "status", "--json", extra_env=locator_env
    )
    history = run_cli(
        git_repo,
        fixture,
        "history",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert status.returncode == history.returncode == 0
    assert stdout_json(status)["run_id"] == run_id
    assert stdout_json(history)["run_id"] == run_id
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator


def test_runs_with_unknown_custom_state_dir_marks_worktree_unavailable(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    source = git_repo / ".agent-run" / "runs" / f"{run_id}.json"
    custom_state_dir = tmp_path / "custom-state"
    custom_runs = custom_state_dir / "runs"
    custom_runs.mkdir(parents=True)
    shutil.copy2(source, custom_runs / source.name)
    before = (custom_runs / source.name).read_text(encoding="utf-8")

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    assert candidate["state_dir"] == str(custom_state_dir.resolve())
    assert (custom_runs / source.name).read_text(encoding="utf-8") == before


def test_runs_with_nested_dot_agent_run_state_dir_marks_worktree_unavailable(
    git_repo: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    source = git_repo / ".agent-run" / "runs" / f"{run_id}.json"
    custom_state_dir = git_repo / "nested" / ".agent-run"
    custom_runs = custom_state_dir / "runs"
    custom_runs.mkdir(parents=True)
    shutil.copy2(source, custom_runs / source.name)
    before = (custom_runs / source.name).read_text(encoding="utf-8")

    discovered = run_cli(
        git_repo,
        fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    assert candidate["state_dir"] == str(custom_state_dir.resolve())
    assert (custom_runs / source.name).read_text(encoding="utf-8") == before


def test_locator_root_reuse_by_another_repository_fails_closed_and_preserves_inputs(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    custom_state_dir = tmp_path / "custom-state"
    run_id = stdout_json(
        seed_run(
            git_repo,
            fixture,
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    original = tmp_path / "original"
    git_repo.rename(original)
    subprocess.run(["git", "clone", "--quiet", str(original), str(git_repo)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:other/project.git"],
        cwd=git_repo,
        check=True,
    )
    replacement_fixture = write_fixture(
        git_repo / "github.json", issues={}, repository="other/project"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    discovered = run_cli(
        elsewhere,
        replacement_fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    for command in ("status", "history"):
        result = run_cli(
            git_repo,
            replacement_fixture,
            command,
            "--parent",
            "1",
            "--json",
            extra_env=locator_env,
        )
        assert result.returncode == 2
        diagnostic = stdout_json(result)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_stale"
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not (git_repo / ".agent-run").exists()


def test_same_repository_replacement_clone_fails_closed_for_all_selectors(
    git_repo: Path, tmp_path: Path
) -> None:
    from cli_fixtures import run_agents

    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    custom_state_dir = tmp_path / "custom-state"
    run_id = stdout_json(
        seed_run(
            git_repo,
            fixture,
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    original = tmp_path / "original"
    git_repo.rename(original)
    subprocess.run(["git", "clone", "--quiet", str(original), str(git_repo)], check=True)
    replacement_fixture = write_fixture(
        git_repo / "github.json", issues={}, repository="example/project"
    )
    agents = run_agents(git_repo / "agents.json")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    discovered = run_cli(
        elsewhere,
        replacement_fixture,
        "runs",
        "--state-dir",
        str(custom_state_dir),
        "--json",
        extra_env=locator_env,
    )

    assert discovered.returncode == 0, discovered.stderr
    candidate = stdout_json(discovered)["runs"][0]
    assert candidate["run_id"] == run_id
    assert candidate["repository_root"] == "unavailable"
    for command, arguments in (
        (
            "run",
            (
                "1",
                "--state-dir",
                str(custom_state_dir),
                "--agent-fixture",
                str(agents),
            ),
        ),
        ("status", ("--parent", "1", "--json")),
        ("history", ("--parent", "1", "--json")),
        (
            "resume",
            (
                "1",
                "--state-dir",
                str(custom_state_dir),
                "--agent-fixture",
                str(agents),
            ),
        ),
        (
            "resume",
            (
                run_id,
                "--state-dir",
                str(custom_state_dir),
                "--agent-fixture",
                str(agents),
            ),
        ),
    ):
        result = run_cli(
            git_repo,
            replacement_fixture,
            command,
            *arguments,
            extra_env=locator_env,
        )
        assert result.returncode == 2, result.stderr
        diagnostic = stdout_json(result)["diagnostics"][0]
        assert diagnostic["code"] == "run_locator_stale"
        assert state_path.read_text(encoding="utf-8") == before_state
        assert locator_path.read_text(encoding="utf-8") == before_locator
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not (git_repo / ".agent-run").exists()


def test_selector_does_not_cross_select_a_run_from_another_clone(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    custom_state_dir = tmp_path / "custom-state"
    run_id = stdout_json(
        seed_run(
            git_repo,
            fixture,
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:example/project.git"],
        cwd=clone,
        check=True,
    )
    clone_fixture = write_fixture(clone / "github.json", issues={})

    for command in ("status", "history"):
        result = run_cli(
            clone,
            clone_fixture,
            command,
            "--parent",
            "1",
            "--json",
            extra_env=locator_env,
        )
        assert result.returncode == 2, result.stderr
        diagnostic = stdout_json(result)["diagnostics"][0]
        assert diagnostic["code"] == "run_selector_requires_checkout"
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not (clone / ".agent-run").exists()


def test_resume_with_explicit_state_dir_does_not_cross_select_another_clone(
    git_repo: Path, tmp_path: Path
) -> None:
    from cli_fixtures import run_agents

    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    custom_state_dir = tmp_path / "custom-state"
    run_id = stdout_json(
        seed_run(
            git_repo,
            fixture,
            "1",
            "--state-dir",
            str(custom_state_dir),
            extra_env=locator_env,
        )
    )["run_id"]
    state_path = custom_state_dir / "runs" / f"{run_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    before_state = state_path.read_text(encoding="utf-8")
    before_locator = locator_path.read_text(encoding="utf-8")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(git_repo), str(clone)], check=True)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:example/project.git"],
        cwd=clone,
        check=True,
    )
    clone_fixture = write_fixture(clone / "github.json", issues={})
    agents = run_agents(clone / "agents.json")

    result = run_cli(
        clone,
        clone_fixture,
        "resume",
        "1",
        "--state-dir",
        str(custom_state_dir),
        "--agent-fixture",
        str(agents),
        extra_env=locator_env,
    )

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_requires_checkout"
    assert state_path.read_text(encoding="utf-8") == before_state
    assert locator_path.read_text(encoding="utf-8") == before_locator
    assert not (clone / ".agent-run").exists()


def test_parent_selector_fails_closed_with_all_ambiguous_candidates(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    locator_env = {"XDG_STATE_HOME": str(tmp_path / "locator-home")}
    first = seed_run(git_repo, fixture, "1", extra_env=locator_env)
    first_id = stdout_json(first)["run_id"]
    second = seed_run(
        git_repo, fixture, extra_env=locator_env, reuse_existing=False
    )
    second_id = stdout_json(second)["run_id"]
    state_paths = sorted((git_repo / ".agent-run" / "runs").glob("*.json"))
    before = [path.read_text(encoding="utf-8") for path in state_paths]

    result = run_cli(
        git_repo,
        fixture,
        "status",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_ambiguous"
    assert {candidate["run_id"] for candidate in diagnostic["candidates"]} == {
        first_id,
        second_id,
    }
    assert [path.read_text(encoding="utf-8") for path in state_paths] == before


def test_parent_selector_reports_stale_index_candidates_without_mutation(
    git_repo: Path, tmp_path: Path
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    locator_home = tmp_path / "locator-home"
    locator_env = {"XDG_STATE_HOME": str(locator_home)}
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", extra_env=locator_env)
    )["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    before = state_path.read_text(encoding="utf-8")
    locator_path = locator_home / "agent-run" / "run-locator.json"
    locator = json.loads(locator_path.read_text(encoding="utf-8"))
    locator["entries"][0]["state_dir"] = str(tmp_path / "missing-state")
    locator_path.write_text(json.dumps(locator), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = run_cli(
        elsewhere,
        fixture,
        "status",
        "--repo",
        "example/project",
        "--parent",
        "1",
        "--json",
        extra_env=locator_env,
    )

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_locator_stale"
    assert diagnostic["candidates"][0]["run_id"] == run_id
    assert diagnostic["candidates"][0]["state_dir"] == str(tmp_path / "missing-state")
    assert state_path.read_text(encoding="utf-8") == before


def test_resume_parent_selector_never_creates_a_run_when_no_recoverable_match(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})

    result = run_cli(git_repo, fixture, "resume", "1")

    assert result.returncode == 2
    diagnostic = stdout_json(result)["diagnostics"][0]
    assert diagnostic["code"] == "run_selector_not_found"
    assert not (git_repo / ".agent-run").exists()


def test_resume_parent_selector_reuses_one_existing_recoverable_run(
    git_repo: Path,
) -> None:
    from cli_fixtures import run_agents

    fixture = write_fixture(git_repo / "github.json", issues={})
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example/project.git"],
        cwd=git_repo,
        check=True,
    )
    agents = run_agents(git_repo / "agents.json")
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    run_file = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "supervision_timeout",
            "terminal_kind": "supervision_timeout",
            "supervision_wait": {"resume_status": "waiting_external"},
        }
    )
    run_file.write_text(json.dumps(state), encoding="utf-8")

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        "1",
        "--repo",
        "example/project",
        "--agent-fixture",
        str(agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["run_id"] == run_id
    resumed_state = load_only_run_state(git_repo)
    assert resumed_state["run_id"] == run_id
    assert resumed_state["resume_audit"]["total"] == 1
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

    result = seed_run(git_repo, fixture, "1")

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

    result = seed_run(git_repo, fixture, "1")

    assert result.returncode == 0, result.stderr
    state = load_only_run_state(git_repo)
    assert state["frontier"] == [2, 5, 9]
    assert state["active_ticket_job"]["ticket_number"] == 2


def test_resume_rejects_a_run_without_an_agent_boundary(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = seed_run(git_repo, fixture, "1")
    run_id = stdout_json(started)["run_id"]

    resumed = run_cli(git_repo, fixture, "resume", run_id)

    assert resumed.returncode == 2
    assert stdout_json(resumed)["result"] == "rejected"
    assert stdout_json(resumed)["run_id"] == run_id
    assert stdout_json(resumed)["status"] == "active"


def test_run_reports_an_incompatible_legacy_state_without_recording_a_failure(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = seed_run(git_repo, fixture, "1")
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
def test_status_and_history_show_incompatible_legacy_evidence(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = seed_run(git_repo, fixture, "1")
    run_id = stdout_json(started)["run_id"]
    state = load_only_run_state(git_repo)
    state["schema_version"] = 1
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 0
    output = stdout_json(result)
    assert output["run_id"] == run_id
    if command == "status":
        assert output["status"] == before["status"]
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history"])
@pytest.mark.parametrize("timeline", [None, {}, "not an event list"])
def test_status_and_history_reject_an_invalid_timeline_without_mutation(
    git_repo: Path, command: str, timeline: object
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    if timeline is None:
        state.pop("timeline")
    else:
        state["timeline"] = timeline
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_history_rejects_a_timeline_with_a_non_event_without_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["timeline"] = ["not an event"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("command", ["status", "history", "resume"])
def test_cli_rejects_a_malformed_canonical_nested_state(
    git_repo: Path, command: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "execution_failed"
    state[field] = value
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = invoke_cli_inprocess(git_repo, fixture, "status", run_id, "--json")

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "incompatible_run_state"
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_resume_rejects_unknown_invocation_role_without_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "execution_failed",
            "run_acceptance": {
                "phase": "accepted",
                "policy_snapshot": deepcopy(state["policy_snapshot"]),
                "acceptance_generation": 1,
                "validation_attempts": 1,
                "review_budget": _canonical_run_budget(),
                "review_budget_history": [],
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state.update(
        {
            "status": "run_publication_pending",
            "run_acceptance": {
                "phase": "accepted",
                "policy_snapshot": deepcopy(state["policy_snapshot"]),
                "acceptance_generation": 1,
                "validation_attempts": 1,
                "review_budget": _canonical_run_budget(),
                "review_budget_history": [],
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

    result = invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json")

    assert result.returncode == 0
    assert json.loads(state_path.read_text(encoding="utf-8")) == before


def test_incompatible_state_does_not_replay_its_diagnostics(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
    state = load_only_run_state(git_repo)
    state["status"] = "blocked"
    state["diagnostics"] = [{"code": "attacker", "message": "not canonical"}]
    state["parent"].pop("number")
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = deepcopy(state)

    result = invoke_cli_inprocess(git_repo, fixture, "status", run_id, "--json")

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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
    run_id = stdout_json(seed_run(git_repo, fixture, "1"))["run_id"]
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
            "agent-run resume 1 --repo example/project",
        ),
        (
            "ready_for_human",
            {
                "active_ticket_job": None,
                "parent_job": {
                    "phase": "blocked",
                    "blocked_reason": "agent_requires_human",
                    "human_blockers": ["Need maintainer input."],
                    "review_budget": _canonical_run_budget(),
                    "review_budget_history": [],
                },
            },
            "agent-run resume 1 --repo example/project",
        ),
        (
            "requeue_required",
            {
                "requeue_required": {
                    "work_subject": "ticket:2",
                    "generation": 1,
                    "reason": "ticket_requirements_changed",
                }
            },
            f"agent-run requeue {run_id}",
        ),
    ]

    for status, additions, expected_action in cases:
        state = deepcopy(current)
        state["status"] = status
        state.update(additions)
        if state.get("active_agent_invocation") is not None:
            invocation = state["active_agent_invocation"]
            assert isinstance(invocation, dict)
            state["ticket_jobs"]["2"].update(
                {
                    "ticket_branch_generation": 1,
                    "phase": "developing",
                    "review_budget": _canonical_run_budget(),
                    "review_budget_history": [],
                    "pending_semantic_attempt": deepcopy(
                        invocation["semantic_attempt"]
                    ),
                }
            )
            state["active_ticket_job"] = deepcopy(state["ticket_jobs"]["2"])
        if status == "requeue_required":
            state["ticket_jobs"]["2"].update(
                {
                    "ticket_branch_generation": 1,
                    "phase": "developing",
                    "review_budget": _canonical_run_budget(),
                    "review_budget_history": [],
                }
            )
            state["active_ticket_job"] = deepcopy(state["ticket_jobs"]["2"])
            state["terminal_kind"] = "requeue_required"
            state["diagnostics"] = [
                {
                    "code": "ticket_requirements_changed",
                    "message": "Ticket requirements changed; run requeue",
                }
            ]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        result = invoke_cli_inprocess(git_repo, fixture, "status", run_id, "--json")

        assert result.returncode == 0
        assert stdout_json(result)["next_action"] == expected_action


def test_resume_does_not_refresh_a_run_without_an_agent_boundary(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = seed_run(git_repo, fixture, "1")
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

    result = seed_run(git_repo, fixture, "1")

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

    result = seed_run(git_repo, fixture, "1")

    assert result.returncode == 2
    output = stdout_json(result)
    state = load_only_run_state(git_repo)
    assert output["status"] == "progress_exhausted"
    assert state["status"] == "progress_exhausted"
    assert state["active_ticket_job"] is None
    assert state["diagnostics"][0]["code"] == "no_executable_ticket"


@pytest.mark.parametrize(
    "triage_label", ["needs-triage", "needs-info", "ready-for-human"]
)
def test_triage_ticket_does_not_create_an_operator_gate(
    git_repo: Path,
    triage_label: str,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": issue(2, labels=["ready-for-agent", triage_label]),
        },
    )

    started = seed_run(git_repo, fixture, "1")

    assert started.returncode == 2
    run_id = stdout_json(started)["run_id"]
    state = load_only_run_state(git_repo)
    assert state["status"] == "progress_exhausted"
    assert state["terminal_kind"] == "temporarily_no_work"
    assert state["diagnostics"][0]["remaining_tickets"] == [
        {
            "ticket_number": 2,
            "reason": f"disqualifying_label:{triage_label}",
        }
    ]
    for command in ("status", "history"):
        view = stdout_json(
            invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json")
        )
        assert view["operator_action"] is None


def test_needs_triage_ticket_does_not_block_an_eligible_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": issue(2, labels=["ready-for-agent", "needs-triage"]),
            "3": issue(3),
        },
    )

    started = seed_run(git_repo, fixture, "1")

    assert started.returncode == 0, started.stderr
    output = stdout_json(started)
    assert output["status"] == "active"
    assert output["active_ticket"] == 3
    state = load_only_run_state(git_repo)
    assert state["frontier"] == [3]
    assert state["active_ticket_job"]["ticket_number"] == 3


def test_cycle_is_persisted_as_blocked_with_diagnostic(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": issue(2, blocked_by=[{"number": 3, "state": "OPEN"}]),
            "3": issue(3, blocked_by=[{"number": 2, "state": "OPEN"}]),
        },
    )

    result = seed_run(git_repo, fixture, "1")

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

    result = seed_run(git_repo, fixture, "1")

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

    waiting = seed_run(git_repo, fixture, "1")

    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_external"
    state = load_only_run_state(git_repo)
    assert state["status"] == "waiting_external"
    assert state["terminal_kind"] == "waiting_external"
    assert state["diagnostics"][0]["code"] == "github_read_failed"

    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    retried = seed_run(git_repo, fixture, "1")

    assert retried.returncode == 0, retried.stderr
    assert stdout_json(retried)["result"] == "resumed"
    assert load_only_run_state(git_repo)["status"] == "active"


@pytest.mark.parametrize(
    ("waits", "expected"),
    [
        (
            [
                {
                    "started_at": "2026-09-10T00:00:10+00:00",
                    "ended_at": "2026-09-10T00:00:40+00:00",
                }
            ],
            70,
        ),
        (
            [
                {
                    "started_at": "2026-09-10T00:00:10+00:00",
                    "ended_at": "2026-09-10T00:00:20+00:00",
                },
                {
                    "started_at": "2026-09-10T00:00:30+00:00",
                    "ended_at": "2026-09-10T00:00:40+00:00",
                },
            ],
            80,
        ),
        (
            [{"started_at": "2026-09-10T00:00:10+00:00"}],
            None,
        ),
    ],
)
def test_history_execution_seconds_excludes_only_trusted_recovery_waits(
    waits: list[dict[str, str]], expected: int | None
) -> None:
    attempt = {
        "attempt_id": "attempt-duration",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "outcome": "candidate",
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "development",
        "status": "completed",
        "started_at": "2026-09-10T00:00:00+00:00",
        "ended_at": "2026-09-10T00:01:40+00:00",
        "recovery_wait_intervals": waits,
        "semantic_attempt": deepcopy(attempt),
    }
    job = {
        "ticket_number": 3,
        "semantic_attempt_history": [attempt],
        "review_budget": {"window": 1, "review_artifacts": []},
    }
    state: dict[str, object] = {"run_id": "run-duration", "ticket_jobs": {"3": job}}

    record = history_records(
        state,
        {
            "semantic_agent_attempts": [attempt],
            "agent_invocations": [invocation],
            "timeline": [],
            "timeline_continuation": [],
            "agent_resumes": [],
        },
    )[0]

    assert record["execution_seconds"] == expected


def test_history_supporting_records_are_bound_to_attempt_version(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def attempt(attempt_id: str, ordinal: int) -> dict[str, object]:
        return {
            "attempt_id": attempt_id,
            "role": "reviewer",
            "work_subject": "ticket:3",
            "generation": 1,
            "ordinal": ordinal,
            "budget_window": 1,
            "status": "completed",
            "outcome": "acceptance_artifact",
        }

    first = attempt("attempt-a", 1)
    second = attempt("attempt-b", 2)
    job = {
        "ticket_number": 3,
        "semantic_attempt_history": [first, second],
        "review_budget": {
            "window": 1,
            "review_artifacts": [],
            "required_checks_evidence": None,
        },
        "required_checks_evidence": {
            "pr_number": 17,
            "head_sha": "publication-b",
            "result": "pass",
            "checks": [{"name": "fixture", "bucket": "pass"}],
        },
        "deterministic_integration_record": {
            "reviewer_thread_id": "hidden-reviewer-thread",
            "policy_snapshot": {"hidden-policy-marker": "do not display"},
            "review_budget": {"hidden-budget-marker": "do not display"},
            "acceptance_record": {"hidden-acceptance-marker": "do not display"},
            "candidate_sha": "candidate-b",
            "publication_sha": "publication-b",
            "integrated_sha": "integrated-b",
            "integrated_message": "Merge accepted candidate",
            "effective_revision": "shared-revision",
            "base_sha": "base-a",
            "window": 1,
            "required_checks_evidence": {
                "pr_number": 17,
                "head_sha": "publication-b",
                "result": "pass",
                "checks": [{"name": "fixture", "bucket": "pass"}],
            },
            "pr": {
                "number": 17,
                "state": "MERGED",
                "head_sha": "publication-b",
                "base_sha": "base-a",
                "merge_commit_sha": "integrated-b",
            },
        },
        "fallback_publication_receipt": {
            "reviewer_thread_id": "hidden-fallback-thread",
            "policy_snapshot": {"hidden-fallback-policy": True},
            "candidate_sha": "candidate-b",
            "publication_sha": "publication-b",
            "pr_number": 17,
            "effective_revision": "shared-revision",
            "base_sha": "base-a",
            "window": 1,
            "required_checks_evidence": {
                "pr_number": 17,
                "head_sha": "publication-b",
                "result": "pass",
                "checks": [{"name": "fixture", "bucket": "pass"}],
            },
            "failure_evidence_source": "git_integrity",
            "failure_evidence": {
                "kind": "git_integrity",
                "status": "fail",
                "reason": "managed checkout changed",
                "expected_head": "expected-head",
                "observed_head": "observed-head",
                "recovery_head": "expected-head",
                "recovery_action": "controller_reset_and_clean",
            },
        },
    }
    invocations = [
        {
            "work_subject": "ticket:3",
            "role": "reviewer",
            "status": "completed",
            "started_at": "2026-09-10T00:00:00+00:00",
            "ended_at": "2026-09-10T00:01:00+00:00",
            "currentness_boundary": {
                "base_sha": "base-a",
                "candidate_sha": "candidate-a",
                "effective_revision": "shared-revision",
            },
            "semantic_attempt": deepcopy(first),
        },
        {
            "work_subject": "ticket:3",
            "role": "reviewer",
            "status": "completed",
            "started_at": "2026-09-10T00:02:00+00:00",
            "ended_at": "2026-09-10T00:03:00+00:00",
            "currentness_boundary": {
                "base_sha": "base-a",
                "candidate_sha": "candidate-b",
                "effective_revision": "shared-revision",
            },
            "semantic_attempt": deepcopy(second),
        },
    ]
    state: dict[str, object] = {
        "run_id": "run-supporting-records",
        "ticket_jobs": {"3": job},
        "active_ticket_job": deepcopy(job),
        "semantic_agent_attempts": [first, second],
        "agent_invocation_history": invocations,
        "timeline": [],
        "timeline_continuation": [],
        "resume_audit": {"history": []},
    }
    audit = {
        "semantic_agent_attempts": [first, second],
        "agent_invocations": invocations,
        "timeline": [],
        "timeline_continuation": [],
        "agent_resumes": [],
    }

    records = history_records(state, audit)

    assert records[0]["supporting_records"] == []
    assert [item["kind"] for item in records[1]["supporting_records"]] == [
        "required_checks_evidence",
        "deterministic_integration_record",
        "fallback_publication_receipt",
    ]

    cli.cli_presentation._print_history(
        state, as_json=False, plain=True, details=True
    )
    details = capsys.readouterr().out
    assert "PR 编号：17" in details
    assert "已配置合并前检查" in details
    assert "合并前检查结果：已通过" in details
    assert "检查项：名称=fixture；结果=已通过" in details
    assert "PR 状态：已合并" in details
    assert "Merge accepted candidate" in details
    deterministic_details = details.rsplit("PR 合并记录", 1)[1].split(
        "兜底发布记录", 1
    )[0]
    fallback_details = details.rsplit("兜底发布记录", 1)[1]
    assert "已配置合并前检查" in deterministic_details
    assert "已配置合并前检查" in fallback_details
    assert "Git 完整性失败依据" in fallback_details
    assert "失败原因：managed checkout changed" in fallback_details
    assert "期望 HEAD：expected-head" in fallback_details
    assert "实际 HEAD：observed-head" in fallback_details
    assert "恢复后 HEAD：expected-head" in fallback_details
    assert "恢复方式：恢复已保存版本并清理工作区" in fallback_details
    for internal_value in (
        "hidden-reviewer-thread",
        "hidden-policy-marker",
        "hidden-budget-marker",
        "hidden-acceptance-marker",
        "hidden-fallback-thread",
        "hidden-fallback-policy",
        "reviewer_thread_id",
        "policy_snapshot",
        "review_budget",
        "acceptance_record",
        "publication-b",
        "integrated-b",
        "configured",
        "controller_reset_and_clean",
    ):
        assert internal_value not in details

    monkeypatch.setattr(
        cli.cli_presentation,
        "_use_rich_status",
        lambda plain: not plain,
    )
    cli.cli_presentation._print_history(state, as_json=False, details=True)
    rich_details = capsys.readouterr().out
    for internal_value in (
        "hidden-reviewer-thread",
        "hidden-policy-marker",
        "hidden-budget-marker",
        "hidden-acceptance-marker",
        "hidden-fallback-thread",
        "hidden-fallback-policy",
        "reviewer_thread_id",
        "policy_snapshot",
        "review_budget",
        "acceptance_record",
    ):
        assert internal_value not in rich_details

    cli.cli_presentation._print_history(state, as_json=True)
    without_details = json.loads(capsys.readouterr().out)
    cli.cli_presentation._print_history(state, as_json=True, details=True)
    with_details = json.loads(capsys.readouterr().out)
    assert with_details == without_details


def test_history_supporting_records_fail_closed_on_missing_or_conflicting_identity() -> None:
    attempt = {
        "attempt_id": "attempt-exact",
        "role": "reviewer",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
    }
    invocation = {
        "work_subject": "ticket:3",
        "role": "reviewer",
        "semantic_attempt": deepcopy(attempt),
        "currentness_boundary": {
            "base_sha": "base-a",
            "candidate_sha": "candidate-exact",
            "effective_revision": "shared-revision",
        },
    }

    def record_for(value: dict[str, object]) -> dict[str, object]:
        job = {
            "ticket_number": 3,
            "semantic_attempt_history": [attempt],
            "review_budget": {"window": 1, "review_artifacts": []},
            "deterministic_integration_record": value,
        }
        state: dict[str, object] = {
            "run_id": "run-supporting-records-fail-closed",
            "ticket_jobs": {"3": job},
        }
        return history_records(
            state,
            {
                "semantic_agent_attempts": [attempt],
                "agent_invocations": [invocation],
                "timeline": [],
                "timeline_continuation": [],
                "agent_resumes": [],
            },
        )[0]

    exact = record_for(
        {
            "candidate_sha": "candidate-exact",
            "base_sha": "base-a",
            "effective_revision": "shared-revision",
            "window": 1,
        }
    )
    assert [item["kind"] for item in exact["supporting_records"]] == [
        "deterministic_integration_record"
    ]

    for invalid in (
        {"candidate_sha": "candidate-exact", "window": 2},
        {"effective_revision": "shared-revision", "window": 1},
        {"candidate_sha": "candidate-exact", "base_sha": "base-b", "window": 1},
        {
            "candidate_sha": "candidate-exact",
            "attempt_id": "different-attempt",
            "window": 1,
        },
        {
            "candidate_sha": "candidate-other",
            "effective_revision": "shared-revision",
            "window": 1,
        },
    ):
        assert record_for(invalid)["supporting_records"] == []


def test_successor_invocation_starts_with_own_recovery_wait_intervals() -> None:
    semantic_attempt = {
        "attempt_id": "attempt-recovery-successor",
        "role": "development",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
    }
    state: dict[str, object] = {
        "run_id": "run-recovery-successor",
        "status": "execution_failed",
        "ticket_jobs": {"3": {"pending_semantic_attempt": semantic_attempt}},
        "agent_invocation_history": [],
        "active_agent_invocation": {
            "work_subject": "ticket:3",
            "generation": 1,
            "role": "development",
            "status": "failed",
            "execution_interrupted": True,
            "semantic_attempt": deepcopy(semantic_attempt),
            "ordinary_recovery_used": True,
            "capacity_recovery_count": 2,
            "recovery_wait_intervals": [
                {
                    "started_at": "2026-09-10T00:00:10+00:00",
                    "ended_at": "2026-09-10T00:00:20+00:00",
                }
            ],
        },
    }
    recorder = invocation_event_recorder(
        state,
        role="development",
        phase="developing",
        work_subject="ticket:3",
        generation=1,
        invocation_input={},
        currentness_boundary={"candidate_sha": "candidate-1"},
        semantic_attempt=semantic_attempt,
        save=lambda _state: None,
    )

    recorder("started")
    successor = state["active_agent_invocation"]
    assert isinstance(successor, dict)
    assert successor["recovery_wait_intervals"] == []
    assert successor["ordinary_recovery_used"] is True
    assert successor["capacity_recovery_count"] == 2

    successor.update(
        {
            "started_at": "2026-09-10T00:00:40+00:00",
            "ended_at": "2026-09-10T00:00:50+00:00",
            "status": "completed",
        }
    )
    assert invocation_execution_seconds(successor) == 10


def test_plain_status_and_history_strip_terminal_controls_but_keep_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    finding = "问题：清屏\x1b[2J；证据：[保留]；必须修复：光标\x1b[10C；复验：重试。"
    view = {
        "repository": "example/project",
        "parent": {"number": 1, "title": "安全展示"},
        "status": "blocked",
        "phase": "developing",
        "conclusion": "需要人工处理",
        "current_object": "Ticket #3",
        "ticket_progress": {"completed": 0, "total": 0},
        "round_progress": None,
        "run_repair": None,
        "elapsed_seconds": 1,
        "current_agent": None,
        "execution_activity": "not_running",
        "next_action": None,
        "findings": [finding],
    }
    cli.cli_presentation.print_status_progress(
        {},
        {},
        view,
        display_term=lambda value: value,
        print_operator_action=lambda _action: None,
    )
    status_output = capsys.readouterr().out
    assert "\x1b" not in status_output
    assert "[2J" in status_output
    assert "[保留]" in status_output

    attempt = {
        "attempt_id": "attempt-history-controls",
        "role": "reviewer",
        "work_subject": "ticket:3",
        "generation": 1,
        "ordinal": 1,
        "budget_window": 1,
        "status": "completed",
        "outcome": "acceptance_artifact",
    }
    job = {
        "ticket_number": 3,
        "semantic_attempt_history": [attempt],
        "review_budget": {
            "window": 1,
            "review_artifacts": [
                {
                    "reviewer_thread_id": "history-reviewer",
                    "candidate_sha": "history-candidate",
                    "artifact": {
                        "checks": {
                            "e2e": {"findings": [finding]},
                            "standards": {"findings": []},
                            "spec": {"findings": []},
                        }
                    },
                }
            ],
        },
    }
    history_state: dict[str, object] = {
        "run_id": "run-history-controls",
        "repository": "example/project",
        "parent": {"number": 1, "title": "安全展示"},
        "ticket_jobs": {"3": job},
        "active_ticket_job": deepcopy(job),
        "agent_invocation_history": [
            {
                "work_subject": "ticket:3",
                "role": "reviewer",
                "status": "completed",
                "started_at": "2026-09-10T00:00:00+00:00",
                "ended_at": "2026-09-10T00:01:00+00:00",
                "reported_thread_id": "history-reviewer",
                "currentness_boundary": {"candidate_sha": "history-candidate"},
                "semantic_attempt": deepcopy(attempt),
            }
        ],
    }
    cli.cli_presentation._print_history(history_state, as_json=False, plain=True)
    history_output = capsys.readouterr().out
    assert "\x1b" not in history_output
    assert "[2J" in history_output


def test_history_details_distinguish_output_continuations_and_missing_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts = [
        {
            "attempt_id": "attempt-output-first",
            "role": "development",
            "work_subject": "ticket:3",
            "generation": 1,
            "ordinal": 1,
            "budget_window": 1,
            "status": "completed",
            "outcome": "candidate",
        },
        {
            "attempt_id": "attempt-output-retry",
            "role": "development",
            "work_subject": "ticket:3",
            "generation": 1,
            "ordinal": 2,
            "budget_window": 1,
            "status": "completed",
            "outcome": "candidate",
        },
        {
            "attempt_id": "attempt-output-missing",
            "role": "development",
            "work_subject": "ticket:3",
            "generation": 1,
            "ordinal": 3,
            "budget_window": 1,
            "status": "completed",
            "outcome": "candidate",
        },
    ]
    invocations = []
    for index, attempt in enumerate(attempts, start=1):
        invocation = {
            "work_subject": "ticket:3",
            "role": "development",
            "status": "completed",
            "started_at": f"2026-09-10T00:0{index}:00+00:00",
            "ended_at": f"2026-09-10T00:0{index}:30+00:00",
            "semantic_attempt": deepcopy(attempt),
        }
        if index == 1:
            invocation["attempt_count"] = 1
        elif index == 2:
            invocation["attempt_count"] = 2
        invocations.append(invocation)
    job = {
        "ticket_number": 3,
        "semantic_attempt_history": attempts,
        "review_budget": {"window": 1, "review_artifacts": []},
    }
    state: dict[str, object] = {
        "run_id": "run-output-counts",
        "repository": "example/project",
        "parent": {"number": 1, "title": "输出计数"},
        "ticket_jobs": {"3": job},
        "active_ticket_job": deepcopy(job),
        "agent_invocation_history": invocations,
    }

    cli.cli_presentation._print_history(state, as_json=False, plain=True, details=True)
    output = capsys.readouterr().out

    assert "输出续接=0 次" not in output
    assert "输出续接=1 次" in output
    assert "输出续接次数：未记录" in output


def test_history_details_omit_empty_validation_errors_but_keep_real_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    invocations = []
    attempts = []
    for index, validation_error in enumerate(("", None, "schema mismatch"), start=1):
        attempt = {
            "attempt_id": f"validation-error-{index}",
            "role": "development",
            "work_subject": "ticket:3",
            "generation": 1,
            "ordinal": index,
            "budget_window": 1,
            "status": "completed",
            "outcome": "candidate",
        }
        attempts.append(attempt)
        invocation = {
            "work_subject": "ticket:3",
            "role": "development",
            "status": "completed",
            "started_at": f"2026-09-10T00:0{index}:00+00:00",
            "ended_at": f"2026-09-10T00:0{index}:30+00:00",
            "semantic_attempt": deepcopy(attempt),
            "validation_error": validation_error,
        }
        if index == 3:
            invocation["return_code"] = 0
        invocations.append(invocation)
    state: dict[str, object] = {
        "run_id": "run-validation-errors",
        "repository": "example/project",
        "parent": {"number": 1, "title": "Validation errors"},
        "status": "completed",
        "semantic_agent_attempts": attempts,
        "agent_invocation_history": invocations,
        "diagnostics": [],
    }

    cli.cli_presentation._print_history(state, as_json=False, plain=True, details=True)
    output = capsys.readouterr().out

    assert output.count("验证错误：") == 1
    assert "验证错误：schema mismatch" in output
    assert "验证错误：\n" not in output
    assert "返回码：0" in output


def test_rich_status_colors_follow_business_conclusion() -> None:
    assert _status_style("run_publication_pending") == "green"
    assert _status_style("run_approval_pending") == "yellow"
    assert _status_style("active") == "cyan"
