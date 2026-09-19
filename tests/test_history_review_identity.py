from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from agent_run.delivery_history import (
    history_progress_view,
    history_records,
    print_history_progress,
)


OLD_FINDING = "旧候选仍泄漏内部状态，必须修复。"


@pytest.fixture
def repair_history() -> tuple[dict[str, Any], dict[str, Any]]:
    """Initial acceptance occupies a budget slot, but not a Repair ordinal."""
    subject = "run-repair:run-history"
    attempts = [
        {
            "attempt_id": f"repair-review-{ordinal}",
            "role": "reviewer",
            "work_subject": subject,
            "generation": 1,
            "ordinal": ordinal,
            "budget_window": 1,
            "status": "completed",
            "outcome": "acceptance_artifact",
        }
        for ordinal in (1, 2)
    ]
    artifacts = [
        {
            "candidate_sha": candidate,
            "reviewer_thread_id": reviewer,
            "artifact": {
                "checks": {
                    lane: {
                        "status": "fail" if findings and lane == "spec" else "pass",
                        "findings": findings if lane == "spec" else [],
                        "evidence": f"Checked {candidate}",
                    }
                    for lane in ("e2e", "standards", "spec")
                }
            },
        }
        for candidate, reviewer, findings in (
            ("initial-candidate", "initial-reviewer", ["初审问题"]),
            ("old-candidate", "old-reviewer", [OLD_FINDING]),
            ("new-candidate", "new-reviewer", []),
        )
    ]
    invocations = [
        {
            "work_subject": subject,
            "role": "reviewer",
            "status": "completed",
            "reported_thread_id": reviewer,
            "currentness_boundary": {"run_head_sha": candidate},
            "semantic_attempt": deepcopy(attempt),
            "started_at": f"2026-09-11T01:0{index}:00+00:00",
            "ended_at": f"2026-09-11T01:0{index}:30+00:00",
        }
        for index, (attempt, candidate, reviewer) in enumerate(
            zip(
                attempts,
                ("old-candidate", "new-candidate"),
                ("old-reviewer", "new-reviewer"),
            )
        )
    ]
    state = {
        "run_id": "run-history",
        "repository": "example/project",
        "parent": {"number": 1, "title": "History regression"},
        "run_acceptance": {
            "repair_job": {
                "semantic_attempt_history": attempts,
                "review_budget": {"window": 1, "review_artifacts": artifacts},
            }
        },
    }
    audit = {
        "semantic_agent_attempts": attempts,
        "agent_invocations": invocations,
        "timeline": [],
        "timeline_continuation": [],
        "agent_resumes": [],
    }
    return state, audit


def test_repair_history_does_not_use_shared_budget_position_as_round(
    repair_history: tuple[dict[str, Any], dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, audit = repair_history
    before = deepcopy((state, audit))

    records = history_records(state, audit)
    assert [record["status_code"] for record in records] == [
        "review_failed", "review_passed"
    ]
    assert [record["status_text"] for record in records] == [
        "验收未通过", "验收通过"
    ]
    assert records[0]["findings"] == [OLD_FINDING]
    assert records[1]["findings"] == []

    # This is the additive events projection exposed by history --json.
    progress = history_progress_view(state, audit)
    old, new = progress["events"]
    assert old["round"] == 1 and old["status"] == "fail"
    assert old["details"] == [OLD_FINDING]
    assert new["round"] == 2 and new["status"] == "completed"
    assert new["details"] == []

    print_history_progress(
        state,
        audit,
        progress,
        display_term=lambda value: value,
        print_operator_action=lambda _action: None,
        details=True,
    )
    output = capsys.readouterr().out
    assert "验收未通过" in output
    assert "验收通过" in output
    old_section, new_section = output.split("第 2 轮", maxsplit=1)
    assert OLD_FINDING in old_section
    assert OLD_FINDING not in new_section
    assert (state, audit) == before


@pytest.mark.parametrize(
    "identity_gap",
    [
        "missing_reviewer",
        "missing_candidate",
        "wrong_reviewer",
        "wrong_candidate",
        "wrong_window",
        "conflicting_artifacts",
    ],
)
def test_repair_history_does_not_borrow_unproven_review_results(
    repair_history: tuple[dict[str, Any], dict[str, Any]],
    identity_gap: str,
) -> None:
    state, audit = repair_history
    latest = audit["agent_invocations"][-1]
    budget = state["run_acceptance"]["repair_job"]["review_budget"]
    if identity_gap == "missing_reviewer":
        latest.pop("reported_thread_id")
    elif identity_gap == "missing_candidate":
        latest.pop("currentness_boundary")
    elif identity_gap == "wrong_reviewer":
        latest["reported_thread_id"] = "old-reviewer"
    elif identity_gap == "wrong_candidate":
        latest["currentness_boundary"] = {"run_head_sha": "old-candidate"}
    elif identity_gap == "wrong_window":
        latest["semantic_attempt"]["budget_window"] = 2
        audit["semantic_agent_attempts"][-1]["budget_window"] = 2
    else:
        conflicting = deepcopy(budget["review_artifacts"][-1])
        conflicting["artifact"] = deepcopy(
            budget["review_artifacts"][-2]["artifact"]
        )
        budget["review_artifacts"].append(conflicting)

    record = history_records(state, audit)[-1]
    assert record["acceptance_artifact"] is None
    assert record["findings"] == []
    assert record["status_text"] not in ("验收通过", "验收未通过")
    event = history_progress_view(state, audit)["events"][-1]
    assert event["details"] == []
    assert event["status"] == "completed"
