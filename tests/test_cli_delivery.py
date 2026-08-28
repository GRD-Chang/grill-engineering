from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.git import GitRepository
from agent_run.state import StateStore
from conftest import write_fixture
from test_cli import run_internal_stage, load_only_run_state, run_cli, stdout_json


HUMAN_BLOCKER = (
    "GitHub denied access; tried gh issue view; grant Issue read access."
)
STANDARDS_PASS_EVIDENCE = "审查范围或基线：仓库编码规范与候选 diff；结论：未发现违反项。"
SPEC_PASS_EVIDENCE = "已核对的验收标准：当前交付的全部验收标准；覆盖结论：候选完整覆盖。"


def ticket() -> dict[str, Any]:
    return {
        "number": 3,
        "title": "Complete one ticket autonomously",
        "body": "Deliver the active ticket through independent acceptance.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def publication() -> dict[str, str]:
    return {
        "commit_message": "feat(delivery): complete one ticket autonomously",
        "pr_title": "feat(delivery): complete one ticket autonomously",
        "pr_body_markdown": """
## What Problem This Solves

Active tickets previously stopped before publication.

## Why This Change Was Made

A bounded delivery loop now owns the workflow.

## User Impact

The ticket reaches the Run Branch without manual mutations.

## Evidence

The scripted CLI scenario passed.
""".strip(),
    }


def final_run_publication() -> dict[str, str]:
    return {
        "commit_message": "feat(run): publish the completed delivery",
        "pr_title": "feat(run): publish the completed delivery",
        "pr_body_markdown": """
## What Problem This Solves

The completed Ticket needs one review boundary.

## Why This Change Was Made

The Run branch keeps the standard delivery route.

## User Impact

Maintainers can approve the complete Parent delivery.

## Evidence

The independent expected-merge review passed.
""".strip(),
    }


def final_run_agents() -> dict[str, object]:
    return {
        "developments": [
            {
                "expected_thread_id": None,
                "thread_id": "ticket-developer",
                "summary": "Delivered the Ticket.",
                "write_files": {"feature.txt": "done\n"},
            }
        ],
        "publications": [publication()],
        "reviews": [passing_acceptance("ticket-reviewer", "Ticket passed.")],
        "run_reviews": [passing_acceptance("run-reviewer", "Run passed.")],
        "run_publications": [final_run_publication()],
    }


def passing_acceptance(thread_id: str, evidence: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "checks": {
            "e2e": {
                "status": "pass",
                "evidence": f"操作或命令：执行 CLI 公开验收流程；退出码：0；结果：{evidence}",
                "findings": [],
            },
            "standards": {
                "status": "pass",
                "evidence": STANDARDS_PASS_EVIDENCE,
                "findings": [],
            },
            "spec": {
                "status": "pass",
                "evidence": SPEC_PASS_EVIDENCE,
                "findings": [],
            },
        },
    }


def repair_acceptance(thread_id: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": "The first candidate lacks the repair.",
                "findings": [
                    "问题：修复缺失；证据：feature.txt 只有一行；必须修复：添加修复；复验：检查 feature.txt。"
                ],
            },
            "standards": {
                "status": "pass",
                "evidence": STANDARDS_PASS_EVIDENCE,
                "findings": [],
            },
            "spec": {
                "status": "pass",
                "evidence": SPEC_PASS_EVIDENCE,
                "findings": [],
            },
        },
    }


def out_of_scope_repair_acceptance(thread_id: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": "out-of-scope.txt exists in the first candidate.",
                "findings": [
                    "问题：Candidate 包含范围外文件 out-of-scope.txt；证据：文件存在；必须修复：删除 out-of-scope.txt；复验：确认文件不存在。"
                ],
            },
            "standards": {
                "status": "pass",
                "evidence": STANDARDS_PASS_EVIDENCE,
                "findings": [],
            },
            "spec": {
                "status": "pass",
                "evidence": SPEC_PASS_EVIDENCE,
                "findings": [],
            },
        },
    }


def human_blocker_step(thread_id: str) -> dict[str, object]:
    return {
        "expected_thread_id": None,
        "thread_id": thread_id,
        "human_blockers": [HUMAN_BLOCKER],
    }


def repairable_required_check_failure() -> dict[str, object]:
    return {
        "name": "quality",
        "workflow": "CI",
        "bucket": "fail",
        "state": "FAILURE",
        "description": "The configured test step failed.",
        "link": "https://example.invalid/checks/quality",
        "job": {
            "head_sha": "$CURRENT_HEAD",
            "name": "quality",
            "workflow_name": "CI",
            "status": "completed",
            "conclusion": "failure",
            "steps": [
                {
                    "name": "Run tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "number": 6,
                }
            ],
        },
    }


def test_public_run_completes_one_supervised_fallback_final_ci_fix(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["pending", "pending", "fail", "pass", "pass"],
            "required_check_evidence": {
                "pr_number": 1,
                "checks": [repairable_required_check_failure()],
            },
        },
    )
    agent_data = final_run_agents()
    agent_data["developments"] = [
        {
            "expected_thread_id": None,
            "thread_id": "ticket-final-ci",
            "summary": "Completed ordinary Development 1.",
            "write_files": {"feature.txt": "attempt 1\n"},
        },
        *[
            {
                "expected_thread_id": "ticket-final-ci",
                "thread_id": "ticket-final-ci",
                "summary": f"Completed ordinary Development {ordinal}.",
                "write_files": {"feature.txt": f"attempt {ordinal}\n"},
            }
            for ordinal in (2, 3, 4)
        ],
        {
            "expected_thread_id": "ticket-final-ci",
            "thread_id": "ticket-final-ci",
            "summary": "Completed the one-shot Final CI-fix.",
            "write_files": {"feature.txt": "final ci fix\n"},
        },
    ]
    agent_data["publications"] = [publication(), publication()]
    agent_data["reviews"] = [
        repair_acceptance("ticket-reviewer-1"),
        repair_acceptance("ticket-reviewer-2"),
        repair_acceptance("ticket-reviewer-3"),
    ]
    agents = git_repo / "fallback-final-ci-fix.json"
    agents.write_text(json.dumps(agent_data), encoding="utf-8")

    result = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert result.returncode == 0, result.stderr
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["3"]
    assert job["phase"] == "completed"
    assert job["development_thread_id"] == "ticket-final-ci"
    assert job["review_budget"]["development_attempts"] == 4
    assert job["review_budget"]["reviewer_invocations"] == 3
    assert job["review_budget"]["final_ci_fix_used"] is True
    assert job["modification_attempts"] == 5
    assert job["attempt_kind"] == "final_ci_fix"
    final_observation = job["required_checks_evidence"]
    assert final_observation["result"] == "pass"
    assert final_observation["head_sha"] == job["publication_sha"]
    assert final_observation["checks"] == [
        {
            "name": "quality",
            "workflow": "CI",
            "bucket": "pass",
            "state": "SUCCESS",
            "link": "https://example.invalid/checks/quality",
        }
    ]
    assert job["ci_evidence"]["result"] == "fail"
    assert job["ci_evidence"]["pr_number"] == job["pr_number"]
    assert job["ci_evidence"]["head_sha"] != job["publication_sha"]
    development_attempts = [
        attempt
        for attempt in job["semantic_attempt_history"]
        if attempt["role"] == "development"
    ]
    assert [attempt["ordinal"] for attempt in development_attempts] == [1, 2, 3, 4, 5]
    assert (
        sum(attempt["outcome"] == "candidate" for attempt in development_attempts) == 5
    )
    receipt = job["fallback_publication_receipt"]
    assert receipt["final_ci_fix_used"] is True
    assert receipt["final_ci_fix_failure_head"] != job["publication_sha"]
    assert receipt["required_check_failure_evidence"]["head_sha"] == receipt[
        "final_ci_fix_failure_head"
    ]
    failure_check = receipt["required_check_failure_evidence"]["checks"][0]
    assert failure_check["bucket"] == "fail"
    assert failure_check["state"] == "FAILURE"
    assert failure_check["job"]["head_sha"] == receipt["final_ci_fix_failure_head"]
    previous_authorization = receipt["previous_publication_authorization"]
    assert previous_authorization["authority"] == "fallback"
    assert previous_authorization["publication_sha"] == receipt[
        "final_ci_fix_failure_head"
    ]
    previous_observation = previous_authorization["fallback_receipt"][
        "required_checks_evidence"
    ]
    assert previous_observation["result"] == "fail"
    assert previous_observation["head_sha"] == receipt["final_ci_fix_failure_head"]
    assert previous_observation["checks"][0]["job"]["head_sha"] == receipt[
        "final_ci_fix_failure_head"
    ]
    assert receipt["repair_delta"] == [{"status": "M", "path": "feature.txt"}]
    assert receipt["required_checks_evidence"]["result"] == "pass"
    assert receipt["required_checks_evidence"]["head_sha"] == job["publication_sha"]
    assert receipt["required_checks_evidence"]["checks"] == final_observation["checks"]

    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    ticket_prs = [
        pull
        for pull in fixture_data["delivery"]["pull_requests"]
        if pull.get("primary_ticket") == 3
    ]
    assert len(ticket_prs) == 1
    assert ticket_prs[0]["state"] == "MERGED"
    assert ticket_prs[0]["head_sha"] == job["publication_sha"]
    assert fixture_data["delivery"]["check_position"] >= 5
    assert fixture_data["supervision_clock"] > 0


def test_ctrl_c_last_development_attempt_resumes_without_new_budget(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    interrupted_agents = git_repo / "interrupted-last-attempt.json"
    interrupted_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-last-attempt",
                        "summary": "Completed Development Attempt 1.",
                        "write_files": {"feature.txt": "attempt 1\n"},
                    },
                    {
                        "expected_thread_id": "ticket-last-attempt",
                        "thread_id": "ticket-last-attempt",
                        "expected_files": {"feature.txt": "attempt 1\n"},
                        "summary": "Completed Development Attempt 2.",
                        "write_files": {"feature.txt": "attempt 2\n"},
                    },
                    {
                        "expected_thread_id": "ticket-last-attempt",
                        "thread_id": "ticket-last-attempt",
                        "expected_files": {"feature.txt": "attempt 2\n"},
                        "summary": "Completed Development Attempt 3.",
                        "write_files": {"feature.txt": "attempt 3\n"},
                    },
                    {
                        "expected_thread_id": "ticket-last-attempt",
                        "thread_id": "ticket-last-attempt",
                        "expected_files": {"feature.txt": "attempt 3\n"},
                        "write_files": {
                            "feature.txt": "attempt 4 partial\n",
                            "partial.txt": "preserve me\n",
                        },
                        "keyboard_interrupt_after_writes": True,
                    }
                ],
                "publications": [],
                "reviews": [
                    repair_acceptance("ticket-reviewer-1"),
                    repair_acceptance("ticket-reviewer-2"),
                    repair_acceptance("ticket-reviewer-3"),
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    interrupted = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(interrupted_agents),
    )

    assert interrupted.returncode == 130
    assert stdout_json(interrupted)["status"] == "execution_failed"
    state = load_only_run_state(git_repo)
    job = state["active_ticket_job"]
    checkout = git_repo / ".agent-run" / "worktrees" / run_id / "ticket-3"
    assert (checkout / "feature.txt").read_text(encoding="utf-8") == (
        "attempt 4 partial\n"
    )
    assert (checkout / "partial.txt").read_text(encoding="utf-8") == "preserve me\n"
    active = state["active_agent_invocation"]
    attempt = active["semantic_attempt"]
    assert job["pending_semantic_attempt"]["attempt_id"] == attempt["attempt_id"]
    assert attempt["ordinal"] == 4
    assert job["modification_attempts"] == 3
    assert job["review_budget"]["development_attempts"] == 4
    assert [
        item["ordinal"]
        for item in job["semantic_attempt_history"]
        if item["role"] == "development"
    ] == [1, 2, 3]

    status = run_cli(git_repo, fixture, "status", run_id, "--json")
    status_output = stdout_json(status)
    assert status_output["semantic_agent_attempt"]["ordinal"] == 4
    assert status_output["next_action"] == f"agent-run resume {run_id}"

    recovery_data = final_run_agents()
    recovery_data["developments"] = [
        {
            "expected_thread_id": "ticket-last-attempt",
            "thread_id": "ticket-last-attempt",
            "summary": "Completed the interrupted final budget attempt.",
            "expected_files": {
                "feature.txt": "attempt 4 partial\n",
                "partial.txt": "preserve me\n",
            },
            "write_files": {"feature.txt": "done\n"},
        }
    ]
    recovery_agents = git_repo / "recover-last-attempt.json"
    recovery_agents.write_text(json.dumps(recovery_data), encoding="utf-8")

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    completed_job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert completed_job["review_budget"]["development_attempts"] == 4
    assert completed_job["modification_attempts"] == 4
    assert any(
        item["attempt_id"] == attempt["attempt_id"]
        for item in completed_job["semantic_attempt_history"]
    )
    assert [
        item["ordinal"]
        for item in completed_job["semantic_attempt_history"]
        if item["role"] == "development"
    ] == [1, 2, 3, 4]
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["base_branch"] == load_only_run_state(
        git_repo
    )["run_branch"]
    mutation_actions = [mutation["action"] for mutation in delivery["mutations"]]
    assert mutation_actions.count("completion_comment") == 1
    assert mutation_actions.count("close_issue") == 1
    assert mutation_actions.count("delete_managed_branch") == 1


def test_public_resume_reuses_pending_final_ci_fix_attempt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["fail", "pass", "pass"],
            "required_check_evidence": {
                "pr_number": 1,
                "checks": [repairable_required_check_failure()],
            },
        },
    )
    interrupted_agents = git_repo / "interrupted-final-ci-fix.json"
    interrupted_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-final-ci",
                        "summary": "Completed ordinary Development 1.",
                        "write_files": {"feature.txt": "attempt 1\n"},
                    },
                    *[
                        {
                            "expected_thread_id": "ticket-final-ci",
                            "thread_id": "ticket-final-ci",
                            "summary": f"Completed ordinary Development {ordinal}.",
                            "write_files": {"feature.txt": f"attempt {ordinal}\n"},
                        }
                        for ordinal in (2, 3, 4)
                    ],
                    {
                        "expected_thread_id": "ticket-final-ci",
                        "thread_id": "ticket-final-ci",
                        "expected_files": {"feature.txt": "attempt 4\n"},
                        "write_files": {
                            "feature.txt": "final ci fix\n",
                            "final-ci-partial.txt": "preserve me\n",
                        },
                        "keyboard_interrupt_after_writes": True,
                    },
                ],
                "publications": [publication()],
                "reviews": [
                    repair_acceptance("ticket-reviewer-1"),
                    repair_acceptance("ticket-reviewer-2"),
                    repair_acceptance("ticket-reviewer-3"),
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    interrupted = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(interrupted_agents),
    )

    assert interrupted.returncode == 130, interrupted.stdout
    state = load_only_run_state(git_repo)
    job = state["active_ticket_job"]
    pending = job["pending_semantic_attempt"]
    assert pending["role"] == "development"
    assert pending["ordinal"] == 5
    assert job["pending_attempt_kind"] == "final_ci_fix"
    assert job["review_budget"]["development_attempts"] == 4
    assert job["review_budget"]["final_ci_fix_used"] is True
    assert "required_checks_evidence" not in job
    assert "required_checks_evidence" not in job["fallback_publication_receipt"]
    assert job["ci_evidence"]["result"] == "fail"
    assert job["ci_evidence"]["head_sha"] == job["publication_sha"]
    assert job["ci_evidence"]["checks"][0]["job"]["head_sha"] == job[
        "publication_sha"
    ]
    assert job["ci_evidence"]["pr_number"] == job["pr_number"]
    assert job["ci_evidence"]["head_sha"] == job["publication_sha"]
    assert job["ci_evidence"]["result"] == "fail"
    checkout = git_repo / ".agent-run" / "worktrees" / run_id / "ticket-3"
    assert (checkout / "final-ci-partial.txt").read_text(encoding="utf-8") == (
        "preserve me\n"
    )

    recovery_data = final_run_agents()
    recovery_data["developments"] = [
        {
            "expected_thread_id": "ticket-final-ci",
            "thread_id": "ticket-final-ci",
            "expected_files": {
                "feature.txt": "final ci fix\n",
                "final-ci-partial.txt": "preserve me\n",
            },
            "summary": "Completed the interrupted Final CI-fix.",
        }
    ]
    recovery_agents = git_repo / "recover-final-ci-fix.json"
    recovery_agents.write_text(json.dumps(recovery_data), encoding="utf-8")

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert resumed.returncode == 0, resumed.stdout
    completed_job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert completed_job["review_budget"]["development_attempts"] == 4
    assert completed_job["review_budget"]["final_ci_fix_used"] is True
    assert completed_job["modification_attempts"] == 5
    assert completed_job["fallback_publication_receipt"][
        "required_checks_evidence"
    ]["head_sha"] == completed_job["publication_sha"]
    assert any(
        attempt["attempt_id"] == pending["attempt_id"]
        and attempt["outcome"] == "candidate"
        for attempt in completed_job["semantic_attempt_history"]
    )
    assert [
        attempt["ordinal"]
        for attempt in completed_job["semantic_attempt_history"]
        if attempt["role"] == "development"
    ] == [1, 2, 3, 4, 5]
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["pull_requests"][0]["base_branch"] == load_only_run_state(
        git_repo
    )["run_branch"]
    mutation_actions = [mutation["action"] for mutation in delivery["mutations"]]
    assert mutation_actions.count("completion_comment") == 1
    assert mutation_actions.count("close_issue") == 1
    assert mutation_actions.count("delete_managed_branch") == 1


def test_public_run_does_not_open_a_budget_window_at_true_checkpoint(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={
            "required_checks": ["fail", "fail", "pass", "pass"],
            "required_check_evidence": {
                "pr_number": 1,
                "checks": [repairable_required_check_failure()],
            },
        },
    )
    agents = git_repo / "exhaust-ticket-budget.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-budget",
                        "summary": "Completed ordinary Development 1.",
                        "write_files": {"feature.txt": "attempt 1\n"},
                    },
                    *[
                        {
                            "expected_thread_id": "ticket-budget",
                            "thread_id": "ticket-budget",
                            "summary": f"Completed ordinary Development {ordinal}.",
                            "write_files": {"feature.txt": f"attempt {ordinal}\n"},
                        }
                        for ordinal in (2, 3, 4)
                    ],
                    {
                        "expected_thread_id": "ticket-budget",
                        "thread_id": "ticket-budget",
                        "summary": "Completed the conditional Final CI-fix.",
                        "write_files": {"feature.txt": "final ci fix\n"},
                    },
                ],
                "publications": [publication(), publication()],
                "reviews": [
                    repair_acceptance("ticket-reviewer-1"),
                    repair_acceptance("ticket-reviewer-2"),
                    repair_acceptance("ticket-reviewer-3"),
                ],
            }
        ),
        encoding="utf-8",
    )

    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert blocked.returncode == 2, blocked.stdout
    before = load_only_run_state(git_repo)
    run_id = before["run_id"]
    job_before = before["ticket_jobs"]["3"]
    assert before["status"] == "progress_exhausted"
    assert job_before["blocked_reason"] == "modification_budget_exhausted"
    assert job_before.get("pending_semantic_attempt") is None
    assert job_before["review_budget"]["window"] == 1
    assert job_before["review_budget"]["development_attempts"] == 4
    assert job_before["review_budget"]["final_ci_fix_used"] is True
    assert job_before["modification_attempts"] == 5
    first_failure_head = job_before["final_ci_fix_failure_head"]
    latest_failure_head = job_before["publication_sha"]
    assert latest_failure_head != first_failure_head
    assert job_before["ci_evidence"]["head_sha"] == latest_failure_head
    budget_before = json.dumps(job_before["review_budget"], sort_keys=True)
    budget_history_before = json.dumps(
        job_before["review_budget_history"], sort_keys=True
    )
    attempts_before = json.dumps(
        job_before["semantic_attempt_history"], sort_keys=True
    )
    delivery_before = json.dumps(
        json.loads(fixture.read_text(encoding="utf-8"))["delivery"],
        sort_keys=True,
    )
    empty_agents = git_repo / "no-implicit-budget-work.json"
    empty_agents.write_text(
        json.dumps(
            {
                "developments": [],
                "publications": [],
                "reviews": [],
                "run_reviews": [],
                "run_publications": [],
            }
        ),
        encoding="utf-8",
    )

    polled = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(empty_agents),
    )

    assert polled.returncode == 2, polled.stdout
    after = load_only_run_state(git_repo)
    job_after = after["ticket_jobs"]["3"]
    assert after["run_id"] == run_id
    assert job_after["review_budget"]["window"] == 1
    assert json.dumps(job_after["review_budget"], sort_keys=True) == budget_before
    assert (
        json.dumps(job_after["review_budget_history"], sort_keys=True)
        == budget_history_before
    )
    assert (
        json.dumps(job_after["semantic_attempt_history"], sort_keys=True)
        == attempts_before
    )
    assert (
        json.dumps(
            json.loads(fixture.read_text(encoding="utf-8"))["delivery"],
            sort_keys=True,
        )
        == delivery_before
    )

    recovery_data = final_run_agents()
    recovery_data["developments"] = [
        {
            "expected_thread_id": "ticket-budget",
            "thread_id": "ticket-budget",
            "summary": "Implemented the explicitly authorized Budget Window 2 repair.",
            "write_files": {"feature.txt": "window 2\n"},
        }
    ]
    recovery_agents = git_repo / "explicit-budget-resume.json"
    recovery_agents.write_text(json.dumps(recovery_data), encoding="utf-8")

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert resumed.returncode == 0, resumed.stdout
    completed = load_only_run_state(git_repo)
    completed_job = completed["ticket_jobs"]["3"]
    assert completed_job["review_budget"]["window"] == 2
    assert completed_job["review_budget"]["development_attempts"] == 1
    assert "final_ci_fix_failure_head" not in completed_job
    assert completed_job["ci_evidence"]["head_sha"] == latest_failure_head
    assert completed_job["review_budget_history"][0][
        "final_ci_fix_failure_head"
    ] == first_failure_head
    resume_event = completed["resume_audit"]["history"][-1]
    assert resume_event["kind"] == "budget_checkpoint"
    assert resume_event["semantic_attempt_id"] is not None
    assert any(
        attempt["attempt_id"] == resume_event["semantic_attempt_id"]
        and attempt["budget_window"] == 2
        for attempt in completed_job["semantic_attempt_history"]
    )


def test_resume_human_blocker_records_bounded_response_and_reuses_development_thread(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    blocked_agents = git_repo / "blocked-agents.json"
    blocked_agents.write_text(
        json.dumps(
            {
                "developments": [human_blocker_step("parent-developer")],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(blocked_agents),
    )
    assert blocked.returncode == 2

    resumed_agents = git_repo / "resumed-agents.json"
    resumed_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": "parent-developer",
                        "thread_id": "parent-developer",
                        "summary": "Access was restored and the Parent request is complete.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance("parent-reviewer", "The resumed candidate passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--message",
        "  Access has been granted.  ",
        "--agent-fixture",
        str(resumed_agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    job = load_only_run_state(git_repo)["parent_job"]
    assert job["development_thread_id"] == "parent-developer"
    assert job["human_response_history"] == [
        {
            "generation": 1,
            "human_blockers": [HUMAN_BLOCKER],
            "response": "Access has been granted.",
        }
    ]
    assert job["phase"] == "ready_for_approval"
    invocations = load_only_run_state(git_repo)["agent_invocation_history"]
    assert [item["role"] for item in invocations[-3:]] == [
        "development",
        "fresh_acceptance",
        "publication",
    ]


def test_ticket_human_response_reaches_fresh_acceptance(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    blocked_agents = git_repo / "blocked-ticket-agents.json"
    blocked_agents.write_text(
        json.dumps(
            {
                "developments": [human_blocker_step("ticket-developer")],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(blocked_agents),
    )
    assert blocked.returncode == 2

    response_history = [
        {
            "generation": 1,
            "human_blockers": [HUMAN_BLOCKER],
            "response": "Issue read access has been granted.",
        }
    ]
    resumed_agents = git_repo / "resumed-ticket-agents.json"
    resumed_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": "ticket-developer",
                        "thread_id": "ticket-developer",
                        "summary": "Completed the Ticket after access was granted.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    {
                        **passing_acceptance(
                            "ticket-reviewer", "The resumed Ticket passed."
                        ),
                        "expected_human_response_history": response_history,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--message",
        response_history[0]["response"],
        "--agent-fixture",
        str(resumed_agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert job["human_response_history"] == response_history
    assert job["phase"] == "completed"


@pytest.mark.parametrize(
    ("resume_args", "expected_thread", "successor_thread"),
    [
        ((), "ticket-reviewer-1", "ticket-reviewer-1"),
        (("--new-thread",), None, "ticket-reviewer-2"),
    ],
)
def test_ticket_fresh_acceptance_failure_resume_uses_requested_thread(
    git_repo: Path,
    resume_args: tuple[str, ...],
    expected_thread: str | None,
    successor_thread: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    failed_agents = git_repo / "failed-ticket-review.json"
    failed_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Completed the Ticket candidate.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [],
                "reviews": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-reviewer-1",
                        "artifact": {"checks": {}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    failed = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(failed_agents),
    )
    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    failed_job = load_only_run_state(git_repo)["active_ticket_job"]
    failed_attempt = failed_job["pending_semantic_attempt"]
    assert failed_attempt["role"] == "reviewer"

    resumed_agents = git_repo / "resumed-ticket-review.json"
    resumed_agents.write_text(
        json.dumps(
            {
                "developments": [],
                "publications": [publication()],
                "reviews": [
                    {
                        **passing_acceptance(
                            successor_thread,
                            "The resumed Fresh Acceptance passed.",
                        ),
                        "expected_thread_id": expected_thread,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        *resume_args,
        "--agent-fixture",
        str(resumed_agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert job["phase"] == "completed"
    assert job["reviewer_thread_ids"] == [successor_thread]
    assert any(
        attempt["attempt_id"] == failed_attempt["attempt_id"]
        for attempt in job["semantic_attempt_history"]
    )


def test_last_reviewer_ordinal_process_failure_resumes_without_new_budget(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    failed_agents = git_repo / "last-reviewer-failure.json"
    failed_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Completed candidate 1.",
                        "write_files": {"feature.txt": "candidate 1\n"},
                    },
                    {
                        "expected_thread_id": "ticket-developer",
                        "thread_id": "ticket-developer",
                        "summary": "Completed candidate 2.",
                        "write_files": {"feature.txt": "candidate 2\n"},
                    },
                    {
                        "expected_thread_id": "ticket-developer",
                        "thread_id": "ticket-developer",
                        "summary": "Completed candidate 3.",
                        "write_files": {"feature.txt": "candidate 3\n"},
                    },
                ],
                "publications": [],
                "reviews": [
                    repair_acceptance("ticket-reviewer-1"),
                    repair_acceptance("ticket-reviewer-2"),
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-reviewer-3",
                        "error": "fixture reviewer process failed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    failed = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(failed_agents),
    )

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    failed_job = load_only_run_state(git_repo)["active_ticket_job"]
    pending = failed_job["pending_semantic_attempt"]
    assert pending["role"] == "reviewer"
    assert pending["ordinal"] == 3
    assert failed_job["review_budget"]["reviewer_invocations"] == 2

    recovery = final_run_agents()
    recovery["developments"] = []
    recovery["reviews"] = [
        {
            **passing_acceptance(
                "ticket-reviewer-3", "The resumed final Reviewer passed."
            ),
            "expected_thread_id": "ticket-reviewer-3",
        }
    ]
    recovery["publications"] = [publication()]
    recovery_agents = git_repo / "last-reviewer-recovery.json"
    recovery_agents.write_text(json.dumps(recovery), encoding="utf-8")

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert resumed.returncode == 0, resumed.stdout
    completed_job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert completed_job["review_budget"]["reviewer_invocations"] == 3
    matching = [
        attempt
        for attempt in completed_job["semantic_attempt_history"]
        if attempt["attempt_id"] == pending["attempt_id"]
    ]
    assert len(matching) == 1
    assert matching[0]["ordinal"] == 3


def assert_human_status_and_history(
    git_repo: Path,
    fixture: Path,
    run_id: str,
    thread_id: str,
    *,
    expected_status: str = "ready_for_human",
) -> None:
    status = stdout_json(
        run_cli(git_repo, fixture, "status", run_id, "--json")
    )
    assert status["status"] == expected_status
    if expected_status == "ready_for_human":
        assert any(
            diagnostic.get("message") == HUMAN_BLOCKER
            for diagnostic in status["diagnostics"]
        )
    else:
        assert expected_status == "progress_exhausted"
        assert any(
            HUMAN_BLOCKER in remaining.get("human_blockers", [])
            for diagnostic in status["diagnostics"]
            for remaining in diagnostic.get("remaining_tickets", [])
        )
    history = stdout_json(
        run_cli(git_repo, fixture, "history", run_id, "--json")
    )
    assert any(
        event.get("human_blockers") == [HUMAN_BLOCKER]
        and event.get("thread_id") == thread_id
        for event in history["timeline"]
    )


def parent_publication() -> dict[str, str]:
    return {
        "commit_message": "feat(parent): deliver the standalone parent request",
        "pr_title": "feat(parent): deliver the standalone parent request",
        "pr_body_markdown": """
## What Problem This Solves

独立的 Parent 需求此前没有可交付路径。

## Why This Change Was Made

Parent-only 流程复用候选与独立验收门禁，并保持普通合并边界。

## User Impact

维护者可以直接审查并批准一张 Parent PR。

## Evidence

脚本化 CLI 流程完成独立验收、检查和显式批准。
""".strip(),
    }


@pytest.mark.parametrize(
    ("display_outcome", "expected_display_status"),
    [
        ("linked", "linked"),
        ("api_error", "unavailable"),
        ("empty", "unavailable"),
        ("missing_readback", "unavailable"),
    ],
)
def test_parent_only_cli_delivers_to_default_branch_after_explicit_approval(
    git_repo: Path, display_outcome: str, expected_display_status: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"linked_branch_display_outcomes": display_outcome},
    )
    agent_fixture = git_repo / "parent-only-agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance(
                        "parent-reviewer-1",
                        "parent-feature.txt is present in the candidate.",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    started = run_cli(git_repo, fixture, "start", "1")
    assert started.returncode == 0, started.stderr
    run_id = stdout_json(started)["run_id"]
    state = load_only_run_state(git_repo)
    assert state["delivery_type"] == "parent_only"
    assert state["parent_branch"] == f"agent-run/{run_id}/parent"
    assert "run_branch" not in state
    seeded = json.loads(fixture.read_text(encoding="utf-8"))
    seeded.setdefault("delivery", {}).setdefault("published_branches", {})[
        state["parent_branch"]
    ] = state["base"]["sha"]
    fixture.write_text(json.dumps(seeded), encoding="utf-8")

    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert delivered.returncode == 0, delivered.stderr
    assert stdout_json(delivered)["status"] == "parent_approval_pending"
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    parent_job = load_only_run_state(git_repo)["parent_job"]
    assert parent_job["phase"] == "ready_for_approval"
    assert mutable_fixture["delivery"]["linked_branches"] == (
        {"1": parent_job["parent_branch"]}
        if expected_display_status == "linked"
        else {}
    )
    assert parent_job["linked_branch_display"] == {
        "display_attempted": True,
        "status": expected_display_status,
    }
    assert len(mutable_fixture["delivery"]["pull_requests"]) == 1
    pull = mutable_fixture["delivery"]["pull_requests"][0]
    assert pull["scope"] == "parent_only"
    assert pull["base_branch"] == "main"
    assert pull["body"].startswith("Parent Issue: #1\nDelivery Type: Parent-only\n\n")
    assert mutable_fixture["delivery"]["closed_issues"] == []

    resumed = run_cli(git_repo, fixture, "resume", run_id)
    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "parent_approval_pending"

    approved = run_cli(git_repo, fixture, "approve", run_id)

    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"
    completed = load_only_run_state(git_repo)
    assert completed["parent_job"]["phase"] == "completed"
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert mutable_fixture["delivery"]["closed_issues"] == [1]
    assert [mutation["action"] for mutation in mutable_fixture["delivery"]["mutations"]] == [
        "parent_completion_comment",
        "close_parent_issue",
        "delete_managed_branch",
    ]
    assert state["parent_branch"] not in mutable_fixture["delivery"]["published_branches"]
    assert subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{state['parent_branch']}"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    ).returncode != 0
    replayed = run_cli(git_repo, fixture, "resume", run_id)
    assert replayed.returncode == 2
    assert stdout_json(replayed)["status"] == "completed"
    abandoned = run_cli(git_repo, fixture, "abandon", run_id)
    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "completed"
    assert load_only_run_state(git_repo)["status"] == "completed"
    assert subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{state['parent_branch']}"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    ).returncode != 0


@pytest.mark.parametrize(
    "crash_key",
    [
        "crash_after_ensure_change_branch_once",
        "crash_after_ensure_change_pr_once",
        "crash_after_link_issue_branch_display_once",
    ],
)
def test_parent_only_cli_recovers_lost_change_response_without_duplicate_worker(
    git_repo: Path, crash_key: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={crash_key: True},
    )
    agents = git_repo / "parent-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented parent work.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer-1", "passed")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    interrupted = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert interrupted.returncode == 2
    recovered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "parent_approval_pending"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert len(delivery["linked_branch_display_attempts"]) == 1
    assert load_only_run_state(git_repo)["parent_job"]["linked_branch_display"] == {
        "display_attempted": True,
        "status": "indeterminate" if crash_key.endswith("display_once") else "linked",
    }


def test_parent_only_cli_rejects_same_named_foreign_ref_before_development(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "empty-parent-agents.json"
    agents.write_text(
        json.dumps({"developments": [], "publications": [], "reviews": []}),
        encoding="utf-8",
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    state = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {}).setdefault("published_branches", {})[
        state["parent_branch"]
    ] = "foreign"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    failed = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery.get("pull_requests", []) == []


@pytest.mark.parametrize(
    "identity_error",
    [
        {"head_sha": "foreign"},
        {"base_sha": "foreign"},
        {"base_branch": "foreign"},
        {"head_repository": "foreign/project"},
        {"base_repository": "foreign/project"},
    ],
)
def test_parent_only_cli_recovery_rejects_foreign_pr_identity_without_new_effects(
    git_repo: Path, identity_error: dict[str, str]
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"crash_after_ensure_change_pr_once": True},
    )
    agents = git_repo / "parent-pr-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [{
                    "expected_thread_id": None,
                    "thread_id": "parent-developer-1",
                    "summary": "Implemented parent work.",
                    "write_files": {"parent-feature.txt": "done\n"},
                }],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer-1", "passed")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    interrupted = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert interrupted.returncode == 2
    before_job = load_only_run_state(git_repo)["parent_job"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["pull_requests"][0].update(identity_error)
    expected_delivery = json.loads(json.dumps(data["delivery"]))
    fixture.write_text(json.dumps(data), encoding="utf-8")
    failed = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == expected_delivery
    after_job = load_only_run_state(git_repo)["parent_job"]
    for key in ("development_thread_id", "modification_attempts", "pending_attempt"):
        assert after_job.get(key) == before_job.get(key)

def test_parent_only_development_human_blocker_stops_before_candidate(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-development-human.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    human_blocker_step("parent-development-blocked")
                ],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "ready_for_human"
    state = load_only_run_state(git_repo)
    job = state["parent_job"]
    assert job["phase"] == "blocked"
    assert job["human_blocker_phase"] == "developing"
    assert job["development_thread_id"] == "parent-development-blocked"
    assert "candidate_sha" not in job
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ] == []
    assert_human_status_and_history(
        git_repo, fixture, run_id, "parent-development-blocked"
    )


def test_parent_only_repair_human_blocker_stops_before_new_candidate(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    blocked_repair = human_blocker_step("parent-developer")
    blocked_repair["expected_thread_id"] = "parent-developer"
    agents = git_repo / "parent-repair-human.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer",
                        "summary": "Implemented the Parent request.",
                        "write_files": {"parent-feature.txt": "first\n"},
                    },
                    blocked_repair,
                ],
                "publications": [],
                "reviews": [repair_acceptance("parent-reviewer")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    blocked = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "ready_for_human"
    job = load_only_run_state(git_repo)["parent_job"]
    assert job["phase"] == "blocked"
    assert job["human_blocker_phase"] == "repairing"
    assert job["modification_attempts"] == 1
    assert job["pending_attempt"] == 2
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ] == []
    assert_human_status_and_history(
        git_repo, fixture, run_id, "parent-developer"
    )


def test_parent_only_publication_human_blocker_stops_before_pr_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    publication_blocker = human_blocker_step("parent-developer")
    publication_blocker["expected_thread_id"] = "parent-developer"
    agents = git_repo / "parent-publication-human.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer",
                        "summary": "Implemented the Parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [publication_blocker],
                "reviews": [
                    passing_acceptance("parent-reviewer", "Candidate passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "ready_for_human"
    job = load_only_run_state(git_repo)["parent_job"]
    assert job["phase"] == "blocked"
    assert job["human_blocker_phase"] == "accepted"
    assert job["publication_thread_id"] == "parent-developer"
    assert job["publication_attempts"] == 1
    pending_publication_attempt = job["pending_semantic_attempt"]["attempt_id"]
    assert "publication_sha" not in job
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ] == []
    assert_human_status_and_history(
        git_repo, fixture, run_id, "parent-developer"
    )
    agents.write_text(
        json.dumps(
            {
                "developments": [],
                "publications": [parent_publication()],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    resumed_job = load_only_run_state(git_repo)["parent_job"]
    assert resumed_job["publication_attempts"] == 1
    assert resumed_job["semantic_attempt_history"][-1]["attempt_id"] == (
        pending_publication_attempt
    )
    assert resumed_job["phase"] == "ready_for_approval"

def test_parent_only_malformed_publication_is_execution_failed_and_resumes(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agent_fixture = git_repo / "parent-only-agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [{"invalid": "publication"}],
                "reviews": [passing_acceptance("parent-reviewer-1", "candidate passed")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    failed = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agent_fixture),
    )

    assert failed.returncode == 2, failed.stderr
    assert stdout_json(failed)["status"] == "execution_failed"
    failed_state = load_only_run_state(git_repo)
    assert failed_state["terminal_kind"] == "execution_failed"
    failed_job = failed_state["parent_job"]
    assert failed_job["phase"] == "accepted"
    assert failed_job["modification_attempts"] == 1
    assert failed_job["validation_attempts"] == 1
    assert failed_job["publication_attempts"] == 1
    failed_attempt = failed_job["pending_semantic_attempt"]
    assert failed_attempt["role"] == "publication"
    accepted_boundary = {
        "candidate_sha": failed_job["candidate_sha"],
        "acceptance_record": failed_job["acceptance_record"],
    }

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["error"] = {"code": "github_read_failed", "message": "offline"}
    fixture.write_text(json.dumps(data), encoding="utf-8")

    unreadable = run_cli(git_repo, fixture, "resume", run_id)

    assert unreadable.returncode == 0, unreadable.stderr
    assert stdout_json(unreadable)["status"] == "waiting_external"
    assert load_only_run_state(git_repo)["parent_job"] == failed_job
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"] == []

    data.pop("error")
    fixture.write_text(json.dumps(data), encoding="utf-8")

    agent_fixture.write_text(
        json.dumps(
            {"developments": [], "publications": [parent_publication()], "reviews": []}
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "parent_approval_pending"
    resumed_state = load_only_run_state(git_repo)
    resumed_job = resumed_state["parent_job"]
    assert resumed_job["modification_attempts"] == 1
    assert resumed_job["validation_attempts"] == 1
    assert resumed_job["publication_attempts"] == 1
    assert {
        "candidate_sha": resumed_job["candidate_sha"],
        "acceptance_record": resumed_job["acceptance_record"],
    } == accepted_boundary
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1
    refresh_resume = resumed_state["resume_audit"]["history"][-1]
    assert refresh_resume["kind"] == "github_refresh_retry"
    successor = next(
        invocation
        for invocation in resumed_state["agent_invocation_history"]
        if invocation.get("resume_id") == refresh_resume["resume_id"]
    )
    assert successor["started_at"] == refresh_resume["successor_invocation_started_at"]
    assert successor["resume_sequence"] == refresh_resume["sequence"]
    assert (
        successor["semantic_attempt"]["attempt_id"]
        == refresh_resume["semantic_attempt_id"]
    )


def test_parent_only_approve_recovers_after_closeout_response_loss(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"crash_after_close_parent_issue_once": True},
    )
    agent_fixture = git_repo / "parent-only-agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer-1", "candidate passed")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agent_fixture)
    )
    assert delivered.returncode == 0, delivered.stderr

    interrupted = run_cli(git_repo, fixture, "approve", run_id)

    assert interrupted.returncode == 2
    first = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert first["pull_requests"][0]["state"] == "MERGED"
    assert first["closed_issues"] == [1]

    recovered = run_cli(git_repo, fixture, "approve", run_id)

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "completed"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["closed_issues"] == [1]
    assert [mutation["action"] for mutation in delivery["mutations"]] == [
        "parent_completion_comment",
        "close_parent_issue",
        "delete_managed_branch",
    ]


def test_resume_freezes_parent_closeout_assets_after_graph_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={},
        delivery={"crash_after_close_parent_issue_once": True},
    )
    agent_fixture = git_repo / "parent-only-agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance(
                        "parent-reviewer-1", "candidate passed"
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )
    assert delivered.returncode == 0, delivered.stderr
    interrupted = run_cli(git_repo, fixture, "approve", run_id)
    assert interrupted.returncode == 2

    before_state = load_only_run_state(git_repo)
    before_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    parent_job = before_state["parent_job"]
    branch = parent_job["parent_branch"]
    frozen_local = {
        "candidate_sha": parent_job["candidate_sha"],
        "acceptance_record": parent_job["acceptance_record"],
        "branch_sha": subprocess.run(
            ["git", "rev-parse", branch],
            cwd=git_repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip(),
    }
    frozen_remote = {
        "published_branch": before_fixture["delivery"]["published_branches"][
            branch
        ],
        "pull_requests": before_fixture["delivery"]["pull_requests"],
        "mutations": before_fixture["delivery"]["mutations"],
    }

    before_fixture["parent"]["sub_issues"] = [3]
    before_fixture["issues"] = {"3": ticket()}
    fixture.write_text(json.dumps(before_fixture), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "run", "1")

    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "unsupported_scope_change"
    after_state = load_only_run_state(git_repo)
    after_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert after_state["parent_job"]["candidate_sha"] == frozen_local[
        "candidate_sha"
    ]
    assert after_state["parent_job"]["acceptance_record"] == frozen_local[
        "acceptance_record"
    ]
    assert subprocess.run(
        ["git", "rev-parse", branch],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == frozen_local["branch_sha"]
    assert after_fixture["delivery"]["published_branches"][branch] == (
        frozen_remote["published_branch"]
    )
    assert after_fixture["delivery"]["pull_requests"] == frozen_remote[
        "pull_requests"
    ]
    assert after_fixture["delivery"]["mutations"] == frozen_remote[
        "mutations"
    ]


def test_parent_only_approve_supervises_unrepairable_required_checks(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-only-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer-1", "candidate passed")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert delivered.returncode == 0, delivered.stderr
    before_state = load_only_run_state(git_repo)
    before = before_state["parent_job"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    nonrepairable = repairable_required_check_failure()
    nonrepairable_job = nonrepairable["job"]
    assert isinstance(nonrepairable_job, dict)
    nonrepairable["job"] = {
        **nonrepairable_job,
        "steps": [
            {
                "name": "Provision runner",
                "status": "completed",
                "conclusion": "failure",
                "number": 1,
            }
        ],
    }
    data["delivery"]["required_checks"] = ["none", "fail"]
    data["delivery"]["required_check_evidence"] = {
        "pr_number": 1,
        "checks": [nonrepairable],
    }
    fixture.write_text(json.dumps(data), encoding="utf-8")

    approval = run_cli(git_repo, fixture, "approve", run_id)

    assert approval.returncode == 0, approval.stderr
    assert stdout_json(approval)["status"] == "waiting_external"
    state = load_only_run_state(git_repo)
    job = state["parent_job"]
    assert job["phase"] == "waiting_checks"
    assert job["required_checks_evidence"] == {
        "pr_number": job["pr_number"],
        "head_sha": job["publication_sha"],
        "result": "fail",
        "checks": job["ci_evidence"]["checks"],
    }
    assert state["diagnostics"][0]["code"] == (
        "github_check_failure_not_repairable"
    )
    assert {
        "candidate_sha": job["candidate_sha"],
        "development_thread_id": job["development_thread_id"],
        "modification_attempts": job["modification_attempts"],
        "validation_attempts": job["validation_attempts"],
        "review_budget": job["review_budget"],
    } == {
        "candidate_sha": before["candidate_sha"],
        "development_thread_id": before["development_thread_id"],
        "modification_attempts": before["modification_attempts"],
        "validation_attempts": before["validation_attempts"],
        "review_budget": before["review_budget"],
    }
    assert state["agent_invocation_history"] == before_state[
        "agent_invocation_history"
    ]
    pulls = json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ]
    assert len(pulls) == 1
    assert pulls[0]["state"] == "OPEN"


def test_parent_only_approve_queues_only_exact_repairable_failure(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-only-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance("parent-reviewer-1", "candidate passed")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert delivered.returncode == 0, delivered.stderr
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"].update(
        {
            "required_checks": ["fail"],
            "check_position": 0,
            "required_check_evidence": {
                "pr_number": 1,
                "checks": [repairable_required_check_failure()],
            },
        }
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")

    approval = run_cli(git_repo, fixture, "approve", run_id)

    assert approval.returncode == 0, approval.stderr
    assert stdout_json(approval)["status"] == "parent_delivery_pending"
    job = load_only_run_state(git_repo)["parent_job"]
    assert job["phase"] == "repairing"
    assert job["repair_source"] == "required_checks"
    assert "required_checks_evidence" not in job
    assert job["ci_evidence"]["result"] == "fail"
    assert job["ci_evidence"]["pr_number"] == job["pr_number"]
    assert job["ci_evidence"]["head_sha"] == job["publication_sha"]
    assert "approval_grant" not in job


@pytest.mark.parametrize("check_result", ["pass", "none"])
def test_parent_only_approve_blocks_default_base_drift_before_merge(
    git_repo: Path, check_result: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-only-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance("parent-reviewer-1", "candidate passed")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert delivered.returncode == 0, delivered.stderr
    before = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"].update(
        {
            "required_checks": [check_result],
            "check_position": 0,
            "default_base_drift_after_required_checks_once": True,
        }
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")

    approval = run_cli(git_repo, fixture, "approve", run_id)

    assert approval.returncode == 2
    assert stdout_json(approval)["status"] == "blocked"
    state = load_only_run_state(git_repo)
    assert state["diagnostics"][0]["code"] == "published_head_mismatch"
    assert state["parent_job"]["phase"] == "blocked"
    assert "integrated_sha" not in state["parent_job"]
    assert state["parent_job"]["publication_sha"] == before["parent_job"][
        "publication_sha"
    ]
    pull = json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ][0]
    assert pull["state"] == "OPEN"
    assert "integrated_sha" not in pull


def test_parent_only_approve_blocks_head_drift_before_merge(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-only-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance("parent-reviewer-1", "candidate passed")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert delivered.returncode == 0, delivered.stderr
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"].update(
        {"required_checks": ["pass"], "live_head_override": "f" * 40}
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")

    approval = run_cli(git_repo, fixture, "approve", run_id)

    assert approval.returncode == 2
    assert stdout_json(approval)["status"] == "blocked"
    state = load_only_run_state(git_repo)
    assert state["diagnostics"][0]["code"] == "published_head_mismatch"
    assert "integrated_sha" not in state["parent_job"]
    pull = json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ][0]
    assert pull["state"] == "OPEN"


@pytest.mark.parametrize(
    "waiting_case",
    [
        "pending",
        "unknown",
        "live_unavailable",
        "snapshot_unavailable",
        "evidence_unavailable",
    ],
)
def test_parent_only_approve_waits_and_recovers_without_duplicate_delivery(
    git_repo: Path, waiting_case: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-only-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance("parent-reviewer-1", "candidate passed")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert delivered.returncode == 0, delivered.stderr
    before_state = load_only_run_state(git_repo)
    before = before_state["parent_job"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    if waiting_case in {"pending", "unknown"}:
        data["delivery"]["required_checks"] = [waiting_case]
        data["delivery"]["check_position"] = 0
    elif waiting_case == "live_unavailable":
        data["delivery"]["open_live_pull_request_failures"] = [
            {
                "scope": "parent_only",
                "code": "github_timeout",
                "message": "live PR unavailable",
            }
        ]
    elif waiting_case == "snapshot_unavailable":
        data["delivery"]["run_required_checks_read_failures"] = [
            {"code": "github_timeout", "message": "snapshot unavailable"}
        ]
    else:
        data["delivery"].update(
            {
                "required_checks": ["fail"],
                "check_position": 0,
                "required_check_evidence": {
                    "pr_number": 1,
                    "checks": [repairable_required_check_failure()],
                },
                "required_check_evidence_failures": [
                    {
                        "scope": "parent_only",
                        "code": "github_timeout",
                        "message": "failure evidence unavailable",
                    }
                ],
            }
        )
    fixture.write_text(json.dumps(data), encoding="utf-8")

    approval = run_cli(git_repo, fixture, "approve", run_id)

    assert approval.returncode == 0, approval.stderr
    expected_status = "waiting_checks" if waiting_case == "pending" else "waiting_external"
    assert stdout_json(approval)["status"] == expected_status
    waiting = load_only_run_state(git_repo)
    waiting_job = waiting["parent_job"]
    assert waiting_job["phase"] == "waiting_checks"
    if waiting_case not in {"live_unavailable", "snapshot_unavailable"}:
        expected_result = (
            "fail" if waiting_case == "evidence_unavailable" else waiting_case
        )
        assert waiting_job["required_checks_evidence"]["result"] == expected_result
        assert waiting_job["required_checks_evidence"]["head_sha"] == (
            waiting_job["publication_sha"]
        )
    assert {
        "candidate_sha": waiting_job["candidate_sha"],
        "development_thread_id": waiting_job["development_thread_id"],
        "modification_attempts": waiting_job["modification_attempts"],
        "validation_attempts": waiting_job["validation_attempts"],
        "review_budget": waiting_job["review_budget"],
    } == {
        "candidate_sha": before["candidate_sha"],
        "development_thread_id": before["development_thread_id"],
        "modification_attempts": before["modification_attempts"],
        "validation_attempts": before["validation_attempts"],
        "review_budget": before["review_budget"],
    }
    assert waiting["agent_invocation_history"] == before_state[
        "agent_invocation_history"
    ]

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"].update({"required_checks": ["pass"], "check_position": 0})
    data["delivery"].pop("open_live_pull_request_failures", None)
    data["delivery"].pop("run_required_checks_read_failures", None)
    data["delivery"].pop("required_check_evidence_failures", None)
    fixture.write_text(json.dumps(data), encoding="utf-8")

    recovered = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert recovered.returncode == 0, recovered.stderr
    assert stdout_json(recovered)["status"] == "completed"
    completed_job = load_only_run_state(git_repo)["parent_job"]
    assert completed_job["candidate_sha"] == before["candidate_sha"]
    assert completed_job["development_thread_id"] == before["development_thread_id"]
    assert completed_job["modification_attempts"] == before["modification_attempts"]
    assert completed_job["validation_attempts"] == before["validation_attempts"]
    assert "supervision_window" not in load_only_run_state(git_repo)
    pulls = json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ]
    assert len(pulls) == 1
    assert pulls[0]["state"] == "MERGED"


def test_parent_only_approve_requires_explicit_requeue_for_stale_parent_revision(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-only-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer-1", "candidate passed")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert delivered.returncode == 0, delivered.stderr
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Clarified parent requirement."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    approval = run_cli(git_repo, fixture, "approve", run_id)

    assert approval.returncode == 2, approval.stderr
    assert stdout_json(approval)["status"] == "requeue_required"
    state = load_only_run_state(git_repo)
    assert state["parent_job"]["phase"] == "ready_for_approval"
    assert state["requeue_required"]["work_subject"].startswith("parent-only:")
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"][0]["state"] == "OPEN"


def test_parent_only_requeue_replaces_the_branch_and_closes_old_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    first_agents = git_repo / "parent-first.json"
    first_agents.write_text(
        json.dumps(
            {
                "developments": [{
                    "expected_thread_id": None,
                    "thread_id": "parent-old",
                    "summary": "Initial parent implementation.",
                    "write_files": {"parent-feature.txt": "old\n"},
                }],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer-old", "Initial pass.")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    first = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(first_agents)
    )
    assert stdout_json(first)["status"] == "parent_approval_pending"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Changed parent requirement."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    stale = run_cli(git_repo, fixture, "approve", run_id)
    assert stdout_json(stale)["status"] == "requeue_required"

    replacement_agents = git_repo / "parent-replacement.json"
    replacement_agents.write_text(
        json.dumps(
            {
                "developments": [{
                    "expected_thread_id": None,
                    "thread_id": "parent-new",
                    "summary": "Replacement parent implementation.",
                    "write_files": {"parent-feature.txt": "new\n"},
                }],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer-new", "Replacement pass.")],
            }
        ),
        encoding="utf-8",
    )
    requeued = run_cli(
        git_repo,
        fixture,
        "requeue",
        run_id,
        "--agent-fixture",
        str(replacement_agents),
    )
    assert requeued.returncode == 0, requeued.stderr
    assert stdout_json(requeued)["status"] == "parent_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["parent_job"]["parent_generation"] == 2
    assert state["parent_job"]["development_thread_id"] == "parent-new"
    assert state["retired_job_generations"][0]["thread_ids"] == [
        "parent-old",
        "parent-reviewer-old",
    ]
    pulls = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
    assert [pull["state"] for pull in pulls] == ["CLOSED", "OPEN"]


def test_parent_only_requeue_blocks_an_externally_closed_old_pr(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [{
                    "expected_thread_id": None,
                    "thread_id": "parent-developer",
                    "summary": "Parent implementation.",
                    "write_files": {"parent-feature.txt": "old\n"},
                }],
                "publications": [parent_publication()],
                "reviews": [passing_acceptance("parent-reviewer", "Parent pass.")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    assert stdout_json(
        run_internal_stage(git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents))
    )["status"] == "parent_approval_pending"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Changed parent requirement."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    assert stdout_json(run_cli(git_repo, fixture, "approve", run_id))["status"] == (
        "requeue_required"
    )
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["pull_requests"][0]["state"] = "CLOSED"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    blocked = run_cli(git_repo, fixture, "requeue", run_id)

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "blocked"
    assert stdout_json(blocked)["diagnostics"][0]["code"] == (
        "change_pr_closed_or_merged_externally"
    )


def test_child_addition_cannot_continue_parent_only_delivery(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"] = [3]
    data["issues"] = {"3": ticket()}
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = git_repo / "agents.json"
    agents.write_text(json.dumps({"developments": []}), encoding="utf-8")

    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )

    assert delivered.returncode == 2
    delivered_json = stdout_json(delivered)
    assert delivered_json["status"] == "unsupported_scope_change"
    assert delivered_json["scope_change"]["graph_change_summary"][
        "added_tickets"
    ] == [3]
    assert json.loads(fixture.read_text(encoding="utf-8")).get("delivery", {}).get("pull_requests", []) == []

    blocked = load_only_run_state(git_repo)
    accepted = blocked["unsupported_scope_change"]["accepted_graph_revision"]
    observed = blocked["unsupported_scope_change"]["observed_graph_revision"]
    for command, identifier, extra in (
        ("run", "1", ()),
        ("resume", run_id, ()),
        ("accept-run", run_id, ()),
        ("publish-run", run_id, ()),
        ("approve", run_id, ()),
        ("revise", run_id, ("--message", "do not absorb drift")),
    ):
        result = (
            run_internal_stage(git_repo, fixture, command, identifier, *extra)
            if command in {"accept-run", "publish-run"}
            else run_cli(git_repo, fixture, command, identifier, *extra)
        )
        assert result.returncode == 2, (command, result.stdout, result.stderr)
        assert stdout_json(result)["status"] == "unsupported_scope_change"

    status = stdout_json(
        run_cli(git_repo, fixture, "status", run_id, "--json")
    )
    assert status["scope_change"]["accepted_graph_revision"] == accepted
    assert status["scope_change"]["observed_graph_revision"] == observed
    assert "abandon" in status["next_action"]
    status_text = run_cli(git_repo, fixture, "status", run_id).stdout
    assert f"accepted={accepted}" in status_text
    assert f"observed={observed}" in status_text
    assert "Ticket 图变化：新增 1" in status_text
    history = stdout_json(
        run_cli(git_repo, fixture, "history", run_id, "--json")
    )
    scope_events = [
        event
        for event in history["timeline"]
        if event.get("kind") == "unsupported_scope_change"
    ]
    assert len(scope_events) == 1
    assert scope_events[0]["accepted_graph_revision"] == accepted
    assert scope_events[0]["observed_graph_revision"] == observed
    assert scope_events[0]["graph_change_summary"]["added_tickets"] == [3]
    assert "abandon" in history["next_action"]
    history_text = run_cli(git_repo, fixture, "history", run_id).stdout
    assert f"accepted={accepted}" in history_text
    assert f"observed={observed}" in history_text
    assert "新增 Ticket [3]" in history_text
    final = json.loads(fixture.read_text(encoding="utf-8"))
    assert final.get("delivery", {}).get("mutations", []) == []


def test_abandon_closes_parent_pr_after_graph_drift(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-only-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-1",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance(
                        "parent-reviewer-1",
                        "parent-feature.txt is present in the candidate.",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert delivered.returncode == 0, delivered.stderr
    assert stdout_json(delivered)["status"] == "parent_approval_pending"
    before_drift = json.loads(fixture.read_text(encoding="utf-8"))
    assert before_drift["delivery"]["pull_requests"][0]["state"] == "OPEN"

    before_drift["parent"]["sub_issues"] = [3]
    before_drift["issues"] = {"3": ticket()}
    fixture.write_text(json.dumps(before_drift), encoding="utf-8")
    blocked = run_cli(git_repo, fixture, "run", "1")
    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "unsupported_scope_change"

    pending = json.loads(fixture.read_text(encoding="utf-8"))
    pending["delivery"]["crash_after_abandon_parent_pr_once"] = True
    fixture.write_text(json.dumps(pending), encoding="utf-8")
    interrupted = run_cli(git_repo, fixture, "abandon", run_id)
    assert interrupted.returncode == 2
    assert stdout_json(interrupted)["status"] == "abandonment_pending"
    frozen_pending = json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "mutations"
    ]
    blocked_replay = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert blocked_replay.returncode == 2
    assert stdout_json(blocked_replay)["status"] == "abandonment_pending"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "mutations"
    ] == frozen_pending

    abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "abandoned"
    abandoned_state = load_only_run_state(git_repo)
    assert abandoned_state["parent_job"]["phase"] == "abandoned"
    after = json.loads(fixture.read_text(encoding="utf-8"))
    assert after["delivery"]["pull_requests"][0]["state"] == "CLOSED"
    assert {"action": "close_parent_pr", "pr_number": 1} in after["delivery"][
        "mutations"
    ]
    assert not (git_repo / ".agent-run" / "worktrees" / run_id).exists()
    assert str(git_repo / ".agent-run" / "worktrees" / run_id) not in subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    frozen_mutations = after["delivery"]["mutations"]

    replayed = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert replayed.returncode == 0, replayed.stderr
    assert stdout_json(replayed)["status"] == "abandoned"
    replayed_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert replayed_fixture["delivery"]["mutations"] == frozen_mutations
    assert not (git_repo / ".agent-run" / "worktrees" / run_id).exists()


def test_abandon_requires_explicit_authorization_to_discard_dirty_checkout(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-only-dirty-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "parent-developer-dirty",
                        "summary": "Implemented the standalone parent request.",
                        "write_files": {"parent-feature.txt": "done\n"},
                    }
                ],
                "publications": [parent_publication()],
                "reviews": [
                    passing_acceptance(
                        "parent-reviewer-dirty",
                        "parent-feature.txt is present in the candidate.",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert delivered.returncode == 0, delivered.stderr
    checkout = git_repo / ".agent-run" / "worktrees" / run_id / "parent"
    current = load_only_run_state(git_repo)
    GitRepository(git_repo).prepare_ticket_checkout(
        branch=current["parent_job"]["parent_branch"],
        base_sha=current["parent_job"]["base_sha"],
        checkout=checkout,
    )
    (checkout / "README.md").write_text("unsaved delivery\n", encoding="utf-8")
    mutations_before = list(
        json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["mutations"]
    )

    refused = run_cli(git_repo, fixture, "abandon", run_id)

    assert refused.returncode == 2
    refusal = stdout_json(refused)
    assert refusal["diagnostics"][0]["code"] == "dirty_managed_checkout"
    refusal_message = refusal["diagnostics"][0]["message"]
    assert str(checkout) in refusal_message
    assert "tracked modifications" in refusal_message
    assert f"agent-run resume {run_id}" in refusal_message
    assert (
        json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["mutations"]
        == mutations_before
    )
    assert checkout.exists()

    discarded = run_cli(git_repo, fixture, "abandon", run_id, "--discard-worktree")

    assert discarded.returncode == 0, discarded.stderr
    assert stdout_json(discarded)["status"] == "abandoned"
    assert not checkout.exists()
    assert {"action": "close_parent_pr", "pr_number": 1} in json.loads(
        fixture.read_text(encoding="utf-8")
    )["delivery"]["mutations"]


@pytest.mark.parametrize(
    ("display_outcome", "expected_status"),
    [
        ("linked", "linked"),
        ("api_error", "unavailable"),
        ("empty", "unavailable"),
        ("missing_readback", "unavailable"),
    ],
)
def test_scripted_cli_delivers_active_ticket_end_to_end(
    git_repo: Path, display_outcome: str, expected_status: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"linked_branch_display_outcomes": display_outcome},
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-1",
                        "summary": "Implemented the first candidate.",
                        "write_files": {"feature.txt": "first\n"},
                    },
                    {
                        "expected_thread_id": "developer-1",
                        "thread_id": "developer-1",
                        "summary": "Applied the acceptance repair.",
                        "write_files": {"feature.txt": "first\nrepaired\n"},
                    },
                ],
                "publications": [publication(), publication()],
                "reviews": [
                    repair_acceptance("reviewer-1"),
                    passing_acceptance(
                        "reviewer-2", "feature.txt contains the repair."
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]

    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert delivered.returncode == 0, delivered.stderr
    assert stdout_json(delivered)["status"] == "run_acceptance_pending"
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["3"]
    assert job["development_thread_id"] == "developer-1"
    assert job["reviewer_thread_ids"] == ["reviewer-1", "reviewer-2"]
    assert job["modification_attempts"] == 2
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = mutable_fixture["delivery"]
    assert delivery["linked_branch_display_attempts"] == [
        {
            "issue_number": 3,
            "branch": job["ticket_branch"],
            "head_sha": job["publication_sha"],
        }
    ]
    assert job["linked_branch_display"] == {
        "display_attempted": True,
        "status": expected_status,
    }
    assert delivery["linked_branches"] == (
        {"3": job["ticket_branch"]} if expected_status == "linked" else {}
    )
    assert len(delivery["pull_requests"]) == 1
    assert delivery["closed_issues"] == [3]
    assert [
        mutation["action"] for mutation in delivery["mutations"]
    ] == ["completion_comment", "close_issue", "delete_managed_branch"]
    assert job["ticket_branch"] not in delivery["published_branches"]
    assert subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{job['ticket_branch']}"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    ).returncode != 0
    assert mutable_fixture["issues"]["3"]["state"] == "CLOSED"
    count = subprocess.run(
        [
            "git",
            "rev-list",
            "--count",
            f"{state['base']['sha']}..{state['run_branch']}",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert count == "1"
    assert not (git_repo / ".agent-run" / "worktrees" / run_id).exists()

    replayed = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )
    assert replayed.returncode == 0, replayed.stderr
    assert stdout_json(replayed)["status"] == "run_acceptance_pending"
    assert (
        load_only_run_state(git_repo)["status"]
        == "run_acceptance_pending"
    )
    replay_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(replay_fixture["delivery"]["pull_requests"]) == 1
    assert replay_fixture["delivery"]["closed_issues"] == [3]


def test_forward_candidate_repair_removes_prior_out_of_scope_file(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = git_repo / "forward-candidate-repair.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-1",
                        "summary": "Created the initial candidate.",
                        "write_files": {
                            "feature.txt": "intended\n",
                            "out-of-scope.txt": "remove me\n",
                        },
                    },
                    {
                        "expected_thread_id": "developer-1",
                        "thread_id": "developer-1",
                        "expected_files": {
                            "feature.txt": "intended\n",
                            "out-of-scope.txt": "remove me\n",
                        },
                        "delete_files": ["out-of-scope.txt"],
                        "summary": "Removed the out-of-scope file.",
                    },
                ],
                "publications": [publication(), publication()],
                "reviews": [
                    out_of_scope_repair_acceptance("reviewer-1"),
                    passing_acceptance(
                        "reviewer-2", "The reduced candidate contains only feature.txt."
                    ),
                ],
                "run_reviews": [
                    passing_acceptance("run-reviewer-1", "The repaired Run passed.")
                ],
                "run_publications": [
                    {
                        "commit_message": "feat(run): publish the repaired delivery",
                        "pr_title": "feat(run): publish the repaired delivery",
                        "pr_body_markdown": "## What Problem This Solves\n\nRepair removed an out-of-scope file.\n\n## Why This Change Was Made\n\nCandidate repair keeps only requested content.\n\n## User Impact\n\nThe Run is ready for approval.\n\n## Evidence\n\nThe public Run flow passed.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    delivered = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents)
    )

    assert delivered.returncode == 0, delivered.stderr
    assert stdout_json(delivered)["status"] == "run_approval_pending"
    run_id = stdout_json(delivered)["run_id"]
    job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    candidate_sha = str(job["candidate_sha"])
    assert job["modification_attempts"] == 2
    assert "human_blockers" not in job
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{candidate_sha}:feature.txt"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    ).returncode == 0
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{candidate_sha}:out-of-scope.txt"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    ).returncode != 0
    assert subprocess.run(
        ["git", "diff", "--name-status", str(job["base_sha"]), candidate_sha],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines() == ["A\tfeature.txt"]
    assert subprocess.run(
        ["git", "log", "--format=%s", "-2", candidate_sha],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines() == [
        "chore(ticket-3): candidate 2",
        "chore(ticket-3): candidate 1",
    ]
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ]


@pytest.mark.parametrize(
    ("display_outcome", "expected_status"),
    [
        ("linked", "linked"),
        ("api_error", "unavailable"),
        ("empty", "unavailable"),
        ("missing_readback", "unavailable"),
    ],
)
def test_run_cli_records_one_final_parent_linked_branch_display(
    git_repo: Path, display_outcome: str, expected_status: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"linked_branch_display_outcomes": display_outcome},
    )
    agents = git_repo / "final-run-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")
    before_config = subprocess.run(
        ["git", "config", "--local", "--list"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout

    delivered = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert delivered.returncode == 0, delivered.stderr
    assert stdout_json(delivered)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    final = state["run_publication"]
    assert final["linked_branch_display"] == {
        "display_attempted": True,
        "status": expected_status,
    }
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    parent_attempts = [
        attempt
        for attempt in delivery["linked_branch_display_attempts"]
        if attempt["issue_number"] == 1
    ]
    assert parent_attempts == [
        {
            "issue_number": 1,
            "branch": state["run_branch"],
            "head_sha": state["run_acceptance"]["acceptance_record"]["reviewed_head_sha"],
        }
    ]
    after_config = subprocess.run(
        ["git", "config", "--local", "--list"],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert after_config == before_config


def test_run_cli_recovers_post_display_crash_without_replaying_parent_display(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"crash_after_final_link_issue_branch_display_once": True},
    )
    agents = git_repo / "final-run-display-crash-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    delivered = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert delivered.returncode == 0, delivered.stderr
    assert stdout_json(delivered)["status"] == "run_approval_pending"
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["linked_branch_display"] == {
        "display_attempted": True,
        "status": "indeterminate",
    }
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(
        [
            attempt
            for attempt in delivery["linked_branch_display_attempts"]
            if attempt["issue_number"] == 1
        ]
    ) == 1


def test_run_cli_fails_closed_when_run_ref_expected_absent_cas_loses_race(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"expected_absent_ref_races": ["*"]},
    )
    agents = git_repo / "empty-run-agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [],
                "publications": [],
                "reviews": [],
                "run_reviews": [],
                "run_publications": [],
            }
        ),
        encoding="utf-8",
    )

    failed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery.get("pull_requests", []) == []
    assert len(delivery["published_branches"]) == 1


@pytest.mark.parametrize("outcome", ["foreign", "cas_race"])
def test_run_cli_fails_closed_before_final_pr_on_final_run_ref_authority_error(
    git_repo: Path, outcome: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"final_run_ref_outcomes": [outcome]},
    )
    agents = git_repo / "final-run-ref-authority-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    failed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "ready_for_human"
    state = load_only_run_state(git_repo)
    assert state["run_publication"]["phase"] == "ready_for_human"
    assert state["run_publication"]["publication_attempts"] == 1
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert not [
        pull for pull in delivery["pull_requests"] if pull.get("scope") == "final_run"
    ]


def test_run_cli_recovers_lost_final_run_ref_response_by_exact_readback(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"final_run_ref_outcomes": ["lost_response"]},
    )
    agents = git_repo / "lost-final-run-ref-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    waiting = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "run_approval_pending"
    final = load_only_run_state(git_repo)["run_publication"]
    assert final["publication_attempts"] == 1
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert len(
        [pull for pull in delivery["pull_requests"] if pull.get("scope") == "final_run"]
    ) == 1


@pytest.mark.parametrize(
    ("base_branch", "head_sha", "identity_error"),
    [
        ("main", "f" * 40, {}),
        ("release", None, {}),
        ("main", None, {"head_repository": "foreign/project"}),
        ("main", None, {"head_ref": "foreign-run-ref"}),
    ],
    ids=("head", "base", "head-repository", "head-ref"),
)
def test_final_run_pr_drift_returns_to_fresh_acceptance_without_rewriting_pr(
    git_repo: Path,
    base_branch: str,
    head_sha: str | None,
    identity_error: dict[str, str],
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    ticket_agents = git_repo / "ticket-agents.json"
    ticket_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Delivered the Ticket.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [passing_acceptance("ticket-reviewer", "Ticket passed.")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(ticket_agents)
    )
    assert stdout_json(delivered)["status"] == "run_acceptance_pending"
    run_agents = git_repo / "run-agents.json"
    run_agents.write_text(
        json.dumps(
            {"reviews": [passing_acceptance("run-reviewer", "Run passed.")]}
        ),
        encoding="utf-8",
    )
    accepted = run_internal_stage(
        git_repo, fixture, "accept-run", run_id, "--agent-fixture", str(run_agents)
    )
    assert stdout_json(accepted)["status"] == "run_publication_pending"

    state = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    actual_head = state["run_acceptance"]["acceptance_record"]["reviewed_head_sha"]
    if base_branch != "main":
        subprocess.run(["git", "branch", base_branch, "main"], cwd=git_repo, check=True)
    data.setdefault("delivery", {}).setdefault("pull_requests", []).append(
        {
            "number": 99,
            "branch": state["run_branch"],
            "base_branch": base_branch,
            "state": "OPEN",
            "scope": "final_run",
            "title": "preserve this title",
            "body": "preserve this body",
            "head_sha": head_sha or actual_head,
            **identity_error,
        }
    )
    data["delivery"].setdefault("published_branches", {})[state["run_branch"]] = (
        head_sha or actual_head
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")
    final_agents = git_repo / "final-agents.json"
    final_agents.write_text(
        json.dumps(
            {
                "run_publications": [
                    {
                        "commit_message": "feat(run): publish the completed delivery",
                        "pr_title": "feat(run): publish the completed delivery",
                        "pr_body_markdown": (
                            "## What Problem This Solves\n\nThe completed Ticket needs one review boundary.\n\n"
                            "## Why This Change Was Made\n\nThe Run branch keeps the standard delivery route.\n\n"
                            "## User Impact\n\nMaintainers can approve the complete Parent delivery.\n\n"
                            "## Evidence\n\nThe independent expected-merge review passed."
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    stale = run_internal_stage(
        git_repo, fixture, "publish-run", run_id, "--agent-fixture", str(final_agents)
    )

    assert stale.returncode == 2
    assert stdout_json(stale)["status"] == "execution_failed"
    preserved = next(
        pull
        for pull in json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
        if pull["number"] == 99
    )
    assert preserved["title"] == "preserve this title"
    assert preserved["body"] == "preserve this body"


@pytest.mark.parametrize(
    "identity_error",
    [
        {"head_repository": "foreign/project"},
        {"head_ref": "foreign-run-ref"},
    ],
    ids=("head-repository", "head-ref"),
)
def test_persisted_final_run_pr_recovery_rejects_foreign_identity_without_effects(
    git_repo: Path, identity_error: dict[str, str]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = git_repo / "final-run-identity-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    assert stdout_json(completed)["status"] == "run_approval_pending"
    before_state = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    final = next(
        pull for pull in data["delivery"]["pull_requests"] if pull.get("scope") == "final_run"
    )
    final.update(identity_error)
    expected_delivery = json.loads(json.dumps(data["delivery"]))
    fixture.write_text(json.dumps(data), encoding="utf-8")

    resumed_publication = json.loads(json.dumps(before_state))
    resumed_publication["status"] = "waiting_external"
    resumed_publication["terminal_kind"] = "waiting_external"
    resumed_publication["run_publication"]["phase"] = "waiting_external"
    StateStore(git_repo / ".agent-run").save_run(
        str(before_state["run_id"]), resumed_publication
    )

    failed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == expected_delivery
    after_state = load_only_run_state(git_repo)
    assert after_state["terminal_kind"] == "deterministic_contradiction"
    assert after_state["diagnostics"][0]["code"] == "foreign_run_pr"
    assert after_state["run_publication"]["pr_number"] == before_state["run_publication"]["pr_number"]
    assert after_state["agent_invocation_history"] == before_state["agent_invocation_history"]
    status = run_cli(git_repo, fixture, "status", str(before_state["run_id"]), "--json")
    history = run_cli(git_repo, fixture, "history", str(before_state["run_id"]), "--json")
    assert stdout_json(status)["status"] == "deterministic_contradiction"
    assert "agent-run run" not in stdout_json(status)["next_action"]
    assert "agent-run resume" not in stdout_json(history)["next_action"]


def test_public_run_fails_closed_for_an_ambiguous_final_pr_read(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = git_repo / "ambiguous-final-run-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    completed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert completed.returncode == 0, completed.stderr
    before_state = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["run_required_checks_read_failures"] = [
        {"code": "ambiguous_run_pr", "message": "multiple current Final Run PRs"}
    ]
    expected_delivery = json.loads(json.dumps(data["delivery"]))
    expected_delivery["run_required_checks_read_failures"] = []
    fixture.write_text(json.dumps(data), encoding="utf-8")
    resumed_publication = json.loads(json.dumps(before_state))
    resumed_publication["status"] = "waiting_external"
    resumed_publication["terminal_kind"] = "waiting_external"
    resumed_publication["run_publication"]["phase"] = "waiting_external"
    StateStore(git_repo / ".agent-run").save_run(
        str(before_state["run_id"]), resumed_publication
    )

    failed = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == expected_delivery
    after_state = load_only_run_state(git_repo)
    assert after_state["diagnostics"][0]["code"] == "ambiguous_run_pr"
    assert after_state["agent_invocation_history"] == before_state["agent_invocation_history"]


@pytest.mark.parametrize(
    "identity_error",
    [
        {"head_repository": "foreign/project"},
        {"head_ref": "foreign-run-ref"},
    ],
    ids=("head-repository", "head-ref"),
)
def test_explicit_approval_rejects_foreign_final_run_pr_identity_without_writes(
    git_repo: Path, identity_error: dict[str, str]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = git_repo / "final-run-approval-identity-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    published = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert published.returncode == 0, published.stderr
    assert stdout_json(published)["status"] == "run_approval_pending"
    before_state = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    final = next(
        pull for pull in data["delivery"]["pull_requests"] if pull.get("scope") == "final_run"
    )
    final.update(identity_error)
    expected_delivery = json.loads(json.dumps(data["delivery"]))
    fixture.write_text(json.dumps(data), encoding="utf-8")

    failed = run_cli(git_repo, fixture, "approve", str(before_state["run_id"]))

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    state = load_only_run_state(git_repo)
    assert state["diagnostics"][0]["code"] == "foreign_run_pr"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"] == expected_delivery
    after_state = load_only_run_state(git_repo)
    assert after_state["run_publication"] == before_state["run_publication"]
    assert after_state["agent_invocation_history"] == before_state["agent_invocation_history"]


@pytest.mark.parametrize(
    "identity_error",
    [
        {"head_repository": "foreign/project"},
        {"head_ref": "foreign-run-ref"},
        {"base_repository": "foreign/project"},
    ],
    ids=("head-repository", "head-ref", "base-repository"),
)
def test_merged_final_run_recovery_rejects_foreign_identity_without_writes(
    git_repo: Path, identity_error: dict[str, str]
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = git_repo / "merged-final-run-identity-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    published = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert published.returncode == 0, published.stderr
    before_merge = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["crash_after_normal_merge_once"] = True
    fixture.write_text(json.dumps(data), encoding="utf-8")
    interrupted = run_cli(git_repo, fixture, "approve", str(before_merge["run_id"]))
    assert interrupted.returncode == 0
    assert stdout_json(interrupted)["status"] == "waiting_external"

    data = json.loads(fixture.read_text(encoding="utf-8"))
    final = next(
        pull for pull in data["delivery"]["pull_requests"] if pull.get("scope") == "final_run"
    )
    assert final["state"] == "MERGED"
    final.update(identity_error)
    expected_delivery = json.loads(json.dumps(data["delivery"]))
    expected_parent = json.loads(json.dumps(data["parent"]))
    before_recovery = load_only_run_state(git_repo)
    fixture.write_text(json.dumps(data), encoding="utf-8")

    failed = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    recovered_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert recovered_data["delivery"] == expected_delivery
    assert recovered_data["parent"] == expected_parent
    after_state = load_only_run_state(git_repo)
    assert after_state["run_publication"] == before_recovery["run_publication"]
    assert after_state["agent_invocation_history"] == before_recovery["agent_invocation_history"]


@pytest.mark.parametrize(
    "identity_error",
    [
        {"head_repository": "foreign/project"},
        {"head_ref": "foreign-run-ref"},
        {"base_repository": "foreign/project"},
    ],
    ids=("head-repository", "head-ref", "base-repository"),
)
def test_post_merge_readback_rejects_foreign_identity_before_parent_closeout(
    git_repo: Path, identity_error: dict[str, str]
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"normal_merge_readback_identity_error": identity_error},
    )
    agents = git_repo / "post-merge-final-run-identity-agents.json"
    agents.write_text(json.dumps(final_run_agents()), encoding="utf-8")

    published = run_cli(git_repo, fixture, "run", "1", "--agent-fixture", str(agents))

    assert published.returncode == 0, published.stderr
    run_id = stdout_json(published)["run_id"]
    failed = run_cli(git_repo, fixture, "approve", run_id)

    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "deterministic_contradiction"
    state = load_only_run_state(git_repo)
    assert state["diagnostics"][0]["code"] == "foreign_run_pr"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    final = next(
        pull for pull in data["delivery"]["pull_requests"] if pull.get("scope") == "final_run"
    )
    assert final["state"] == "MERGED"
    assert data["parent"].get("state") != "CLOSED"
    assert data["delivery"].get("closed_issues", []) == [3]


def test_one_ticket_run_reaches_final_parent_closeout(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    ticket_agents = git_repo / "ticket-agents.json"
    ticket_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Delivered the one Ticket.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [passing_acceptance("ticket-reviewer", "The Ticket flow passed.")],
            }
        ),
        encoding="utf-8",
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    delivered = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(ticket_agents)
    )
    assert stdout_json(delivered)["status"] == "run_acceptance_pending"

    run_agents = git_repo / "run-agents.json"
    run_agents.write_text(
        json.dumps(
            {"reviews": [passing_acceptance("run-reviewer", "The expected merge passed.")]},
        ),
        encoding="utf-8",
    )
    accepted = run_internal_stage(
        git_repo, fixture, "accept-run", run_id, "--agent-fixture", str(run_agents)
    )
    assert stdout_json(accepted)["status"] == "run_publication_pending"

    final_agents = git_repo / "final-agents.json"
    final_agents.write_text(
        json.dumps(
            {
                "run_publications": [
                    {
                        "commit_message": "feat(run): publish the completed delivery",
                        "pr_title": "feat(run): publish the completed delivery",
                        "pr_body_markdown": (
                            "## What Problem This Solves\n\nThe completed Ticket needs one review boundary.\n\n"
                            "## Why This Change Was Made\n\nThe Run branch keeps the standard delivery route.\n\n"
                            "## User Impact\n\nMaintainers can approve the complete Parent delivery.\n\n"
                            "## Evidence\n\nThe independent expected-merge review passed."
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    published = run_internal_stage(
        git_repo, fixture, "publish-run", run_id, "--agent-fixture", str(final_agents)
    )
    assert stdout_json(published)["status"] == "run_approval_pending"
    approved = run_cli(git_repo, fixture, "approve", run_id)
    assert approved.returncode == 0, approved.stderr
    assert stdout_json(approved)["status"] == "completed"

    data = json.loads(fixture.read_text(encoding="utf-8"))
    pulls = data["delivery"]["pull_requests"]
    assert len(pulls) == 2
    final = next(pull for pull in pulls if pull.get("scope") == "final_run")
    assert final["branch"] == load_only_run_state(git_repo)["run_branch"]
    assert final["body"].startswith("Parent Issue: #1\nDelivery Type: Final Run\n\n")
    assert "## Completed Tickets\n\n- [#3: Complete one ticket autonomously]" in final["body"]
    assert data["delivery"]["closed_issues"] == [3, 1]
    assert data["parent"]["state"] == "CLOSED"
    assert load_only_run_state(git_repo)["run_branch"] not in data["delivery"]["published_branches"]
    assert subprocess.run(
        [
            "git",
            "show-ref",
            "--verify",
            f"refs/heads/{load_only_run_state(git_repo)['run_branch']}",
        ],
        cwd=git_repo,
        capture_output=True,
        check=False,
    ).returncode != 0

    completed_state = load_only_run_state(git_repo)
    completed_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    added = ticket()
    added.update({"number": 4, "title": "Added after completion"})
    completed_fixture["parent"]["sub_issues"] = [3, 4]
    completed_fixture["issues"]["4"] = added
    fixture.write_text(json.dumps(completed_fixture), encoding="utf-8")
    frozen_mutations = completed_fixture["delivery"]["mutations"]

    for command in ("resume", "deliver", "abandon"):
        replayed = (
            run_internal_stage(
                git_repo, fixture, command, run_id, "--agent-fixture", str(ticket_agents)
            )
            if command == "deliver"
            else run_cli(git_repo, fixture, command, run_id)
        )
        assert replayed.returncode == (2 if command == "resume" else 0), replayed.stderr
        assert stdout_json(replayed)["status"] == "completed"
        assert load_only_run_state(git_repo) == completed_state
        replayed_fixture = json.loads(fixture.read_text(encoding="utf-8"))
        assert replayed_fixture["delivery"]["mutations"] == frozen_mutations


def test_pending_required_checks_resume_without_duplicate_pr_or_attempt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending", "pass"]},
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-1",
                        "summary": "Implemented and tested.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    passing_acceptance(
                        "reviewer-1", "The exact candidate passed."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]

    waiting = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )
    assert waiting.returncode == 0, waiting.stderr
    assert stdout_json(waiting)["status"] == "waiting_checks"
    waiting_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert waiting_fixture["delivery"]["agent_run_status"] == [
        {
            "pr_number": 1,
            "scope": "ticket-3",
            "base_sha": load_only_run_state(git_repo)["ticket_jobs"]["3"]["base_sha"],
            "candidate_sha": load_only_run_state(git_repo)["ticket_jobs"]["3"]["candidate_sha"],
            "validation_outcome": "pass",
            "lane_statuses": {"e2e": "pass", "standards": "pass", "spec": "pass"},
            "required_checks": "pending",
            "next_action": "wait for Required Checks",
        }
    ]
    checkout = git_repo / ".agent-run" / "worktrees" / run_id / "ticket-3"
    assert checkout.is_dir()
    git_link = (checkout / ".git").read_text(encoding="utf-8")

    completed = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert completed.returncode == 0, completed.stderr
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_acceptance_pending"
    assert state["ticket_jobs"]["3"]["modification_attempts"] == 1
    assert git_link
    assert not checkout.exists()
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(mutable_fixture["delivery"]["pull_requests"]) == 1
    assert len(mutable_fixture["delivery"]["agent_run_status"]) == 1
    assert mutable_fixture["delivery"]["agent_run_status"][0]["required_checks"] == "pass"
    assert mutable_fixture["delivery"]["closed_issues"] == [3]


def test_ticket_repair_human_blocker_stops_before_new_candidate(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    blocked_repair = human_blocker_step("ticket-developer")
    blocked_repair["expected_thread_id"] = "ticket-developer"
    agents = git_repo / "ticket-repair-human.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Implemented the Ticket.",
                        "write_files": {"feature.txt": "first\n"},
                    },
                    blocked_repair,
                ],
                "publications": [],
                "reviews": [repair_acceptance("ticket-reviewer")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "progress_exhausted"
    job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert job["phase"] == "blocked"
    assert job["human_blocker_phase"] == "repairing"
    assert job["modification_attempts"] == 1
    assert job["pending_attempt"] == 2
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ] == []
    assert load_only_run_state(git_repo)["terminal_kind"] == "waiting_human"
    assert_human_status_and_history(
        git_repo,
        fixture,
        run_id,
        "ticket-developer",
        expected_status="progress_exhausted",
    )


def test_ticket_publication_human_blocker_stops_before_pr_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agents = git_repo / "ticket-publication-human.json"
    publication_blocker = human_blocker_step("ticket-developer")
    publication_blocker["expected_thread_id"] = "ticket-developer"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Implemented the Ticket.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [publication_blocker],
                "reviews": [
                    passing_acceptance("ticket-reviewer", "Candidate passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    blocked = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agents),
    )

    assert blocked.returncode == 2
    assert stdout_json(blocked)["status"] == "progress_exhausted"
    job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert job["phase"] == "blocked"
    assert job["human_blocker_phase"] == "accepted"
    assert job["publication_thread_id"] == "ticket-developer"
    assert job["publication_attempts"] == 1
    assert "publication_sha" not in job
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ] == []
    assert load_only_run_state(git_repo)["terminal_kind"] == "waiting_human"
    assert_human_status_and_history(
        git_repo,
        fixture,
        run_id,
        "ticket-developer",
        expected_status="progress_exhausted",
    )
    agents.write_text(
        json.dumps(
            {
                "developments": [],
                "publications": [publication()],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )

    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    resumed_job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert resumed_job["publication_attempts"] == 1
    assert resumed_job["phase"] == "completed"


@pytest.mark.parametrize(
    ("resume_args", "expected_thread", "successor_thread"),
    [
        ((), "publication-thread-1", "publication-thread-1"),
        (("--new-thread",), None, "publication-thread-2"),
    ],
)
def test_malformed_publication_is_execution_failed_and_resumes_without_revalidation(
    git_repo: Path,
    resume_args: tuple[str, ...],
    expected_thread: str | None,
    successor_thread: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    agent_fixture = git_repo / "agents.json"
    invalid_publications = [
        {"invalid": "publication", "thread_id": "publication-thread-1"}
    ]
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-1",
                        "summary": "Implemented and tested.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": invalid_publications,
                "reviews": [
                    passing_acceptance("reviewer-1", "The exact candidate passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]

    failed = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agent_fixture),
    )

    assert failed.returncode == 2, failed.stderr
    assert stdout_json(failed)["status"] == "execution_failed"
    failed_state = load_only_run_state(git_repo)
    failed_job = failed_state["ticket_jobs"]["3"]
    assert failed_job["phase"] == "accepted"
    assert failed_job["modification_attempts"] == 1
    assert failed_job["validation_attempts"] == 1
    assert failed_job["publication_attempts"] == 1
    failed_attempt = failed_job["pending_semantic_attempt"]
    assert failed_attempt["role"] == "publication"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"] == []
    bypass = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agent_fixture),
    )
    assert bypass.returncode == 2
    assert stdout_json(bypass)["status"] == "execution_failed"

    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [],
                "publications": [
                    {
                        **publication(),
                        "expected_thread_id": expected_thread,
                        "thread_id": successor_thread,
                    }
                ],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
        *resume_args,
    )

    assert resumed.returncode == 0, resumed.stderr
    assert stdout_json(resumed)["status"] == "run_acceptance_pending"
    completed_job = load_only_run_state(git_repo)["ticket_jobs"]["3"]
    assert completed_job["modification_attempts"] == 1
    assert completed_job["validation_attempts"] == 1
    assert completed_job["publication_attempts"] == 1
    assert (
        len(
            json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]
        )
        == 1
    )
    completed_state = load_only_run_state(git_repo)
    successor = completed_state["active_agent_invocation"]
    assert successor["status"] == "completed"
    assert successor["mode"] == (
        "resume" if expected_thread else "new-thread"
    )
    assert successor["requested_thread_id"] == expected_thread
    assert successor["reported_thread_id"] == successor_thread
    assert (
        successor["semantic_attempt"]["attempt_id"]
        == failed_attempt["attempt_id"]
    )
    assert successor["work_subject"] == "ticket:3"
    assert successor["generation"] == completed_job["ticket_branch_generation"]
    assert successor["input_fingerprint"].startswith("sha256:")
    assert len(successor["input_fingerprint"]) == 71
    acceptance = completed_job["acceptance_record"]
    assert successor["currentness_boundary"] == {
        "base_sha": completed_job["base_sha"],
        "candidate_sha": completed_job["candidate_sha"],
        "candidate_tree": acceptance["reviewed_candidate_tree"],
        "effective_revision": completed_job["effective_revision"],
    }
    assert completed_state["agent_invocation_history"][-1] == successor
    assert any(
        attempt["attempt_id"] == failed_attempt["attempt_id"]
        for attempt in completed_job["semantic_attempt_history"]
    )


@pytest.mark.parametrize(
    ("resume_args", "expected_thread", "successor_mode"),
    [
        ((), "run-repair-publication-1", "resume"),
        (("--new-thread",), None, "new-thread"),
    ],
)
def test_resume_targets_failed_run_repair_publication_not_completed_ticket(
    git_repo: Path,
    resume_args: tuple[str, ...],
    expected_thread: str | None,
    successor_mode: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
    ticket_agents = git_repo / "ticket-agents.json"
    ticket_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Delivered the Ticket candidate.",
                        "write_files": {"feature.txt": "ticket\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    passing_acceptance("ticket-reviewer", "Ticket passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(ticket_agents),
    )
    assert delivered.returncode == 0, delivered.stderr

    failed_agents = git_repo / "failed-run-repair-agents.json"
    failed_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "run-repair-developer",
                        "summary": "Repaired the accumulated Run.",
                        "write_files": {"run-repair.txt": "repaired\n"},
                    }
                ],
                "publications": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "run-repair-publication-1",
                        "invalid": "publication",
                    }
                ],
                "run_reviews": [
                    repair_acceptance("run-reviewer-1"),
                    passing_acceptance(
                        "run-repair-reviewer", "Run repair candidate passed."
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    failed = run_internal_stage(
        git_repo,
        fixture,
        "accept-run",
        run_id,
        "--agent-fixture",
        str(failed_agents),
    )
    assert failed.returncode == 2, failed.stderr
    assert stdout_json(failed)["status"] == "execution_failed"

    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    failed_state = load_only_run_state(git_repo)
    completed_ticket = failed_state["ticket_jobs"]["3"]
    completed_ticket["publication_thread_id"] = "completed-ticket-publication"
    repair_job = failed_state["run_acceptance"]["repair_job"]
    repair_job["publication_thread_id"] = "run-repair-publication-1"
    original_invocation = failed_state["active_agent_invocation"]
    failed_state["active_agent_invocation"] = {
        "work_subject": f"run-repair:{run_id}",
        "generation": repair_job["repair_generation"],
        "role": "publication",
        "phase": "publication",
        "mode": "fresh",
        "input_fingerprint": "fixture",
        "currentness_boundary": original_invocation["currentness_boundary"],
        "semantic_attempt": repair_job["pending_semantic_attempt"],
        "status": "failed",
        "requested_thread_id": None,
        "reported_thread_id": "run-repair-publication-1",
        "attempt_count": 1,
        "started_at": "2026-08-11T00:00:00+00:00",
        "ended_at": "2026-08-11T00:00:01+00:00",
        "error": "invalid publication",
        "return_code": 0,
        "signal": None,
    }
    state_path.write_text(
        json.dumps(failed_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    resume_agents = git_repo / "resume-run-repair-agents.json"
    resume_agents.write_text(
        json.dumps(
            {
                "developments": [],
                "publications": [
                    {
                        **publication(),
                        "expected_thread_id": expected_thread,
                        "thread_id": (
                            expected_thread or "run-repair-publication-2"
                        ),
                    }
                ],
                "run_reviews": [
                    passing_acceptance("run-reviewer-2", "Complete Run passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(resume_agents),
        *resume_args,
    )

    assert resumed.returncode == 0, resumed.stderr
    resumed_state = load_only_run_state(git_repo)
    assert resumed_state["status"] == "run_publication_pending"
    assert (
        resumed_state["ticket_jobs"]["3"]["publication_thread_id"]
        == "completed-ticket-publication"
    )
    # Candidate promotion proceeds directly to Run Publication, so the last
    # repair invocation is the resumed publication Worker; no duplicate
    # whole-Run Reviewer follows it.
    repair_successor = resumed_state["agent_invocation_history"][-1]
    assert repair_successor["work_subject"] == f"run-repair:{run_id}"
    assert repair_successor["mode"] == successor_mode
    assert repair_successor["requested_thread_id"] == expected_thread
    assert repair_successor["reported_thread_id"] == (
        expected_thread or "run-repair-publication-2"
    )
    # The completed repair is already the formal Run Acceptance boundary.
    assert resumed_state["active_agent_invocation"]["work_subject"] == (
        f"run-repair:{run_id}"
    )


def test_published_head_drift_blocks_merge_and_close(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"live_head_override": "f" * 40},
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-1",
                        "summary": "Implemented and tested.",
                        "write_files": {"feature.txt": "done\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    passing_acceptance(
                        "reviewer-1", "The exact candidate passed."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]

    result = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert result.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "progress_exhausted"
    assert state["terminal_kind"] == "waiting_human"
    assert state["diagnostics"][0]["remaining_tickets"] == [
        {"ticket_number": 3, "reason": "published_head_mismatch"}
    ]
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert mutable_fixture["delivery"]["closed_issues"] == []
    assert mutable_fixture["delivery"]["pull_requests"][0]["state"] == "OPEN"


def test_ticket_revision_change_requires_requeue_instead_of_reusing_job(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending", "pass"]},
    )
    first_agents = git_repo / "agents-first.json"
    first_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-1",
                        "summary": "Implemented revision one.",
                        "write_files": {"feature.txt": "revision one\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    passing_acceptance(
                        "reviewer-1", "Revision one passed."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    waiting = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(first_agents),
    )
    assert stdout_json(waiting)["status"] == "waiting_checks"
    old_state = load_only_run_state(git_repo)
    old_revision = old_state["active_ticket_job"]["effective_revision"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["3"]["body"] += "\nNew authoritative requirement."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    second_agents = git_repo / "agents-second.json"
    second_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented revision two.",
                        "write_files": {"feature.txt": "revision two\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    passing_acceptance(
                        "reviewer-2", "The replacement candidate passed."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    rejected = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(second_agents),
    )
    assert rejected.returncode != 0
    assert stdout_json(rejected)["diagnostics"][0]["code"] == "ticket_requirements_changed"
    state = load_only_run_state(git_repo)
    assert state["status"] == "requeue_required"
    assert state["active_ticket_job"]["effective_revision"] == old_revision
    assert state["active_ticket_job"]["modification_attempts"] == 1
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(mutable_fixture["delivery"]["pull_requests"]) == 1
    mutable_fixture["delivery"]["crash_after_abandon_change_pr_once"] = True
    fixture.write_text(json.dumps(mutable_fixture), encoding="utf-8")

    interrupted = run_cli(
        git_repo,
        fixture,
        "requeue",
        run_id,
        "--agent-fixture",
        str(second_agents),
    )
    assert interrupted.returncode == 2
    assert stdout_json(interrupted)["status"] == "requeue_required"
    prepared = load_only_run_state(git_repo)
    assert prepared["status"] == "requeue_required"
    assert prepared["requeue_transition"]["retired"]["pr_number"] == 1

    requeued = run_cli(
        git_repo,
        fixture,
        "requeue",
        run_id,
        "--agent-fixture",
        str(second_agents),
    )
    assert requeued.returncode == 0, requeued.stderr
    state = load_only_run_state(git_repo)
    replacement = state["ticket_jobs"]["3"]
    assert replacement["ticket_branch_generation"] == 2
    assert replacement["development_thread_id"] == "developer-2"
    assert state["retired_job_generations"][0]["thread_ids"] == [
        "developer-1",
        "reviewer-1",
    ]
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    pulls = delivery["pull_requests"]
    assert [pull["state"] for pull in pulls] == ["CLOSED", "MERGED"]
    assert delivery["mutations"].count(
        {"action": "close_change_pr", "pr_number": 1}
    ) == 1


def test_run_stops_for_an_operator_to_requeue_a_stale_generation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending", "pending"]},
    )
    first_agents = git_repo / "first-agents.json"
    first_agents.write_text(
        json.dumps(
            {
                "developments": [{
                    "expected_thread_id": None,
                    "thread_id": "developer-old",
                    "summary": "Initial implementation.",
                    "write_files": {"feature.txt": "old\n"},
                }],
                "publications": [publication()],
                "reviews": [passing_acceptance("reviewer-old", "Initial pass.")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    waiting = run_internal_stage(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(first_agents)
    )
    assert stdout_json(waiting)["status"] == "waiting_checks"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["3"]["body"] += "\nNew requirement."
    fixture.write_text(json.dumps(data), encoding="utf-8")
    replacement_agents = git_repo / "replacement-agents.json"
    replacement_agents.write_text(
        json.dumps(
            {
                "developments": [{
                    "expected_thread_id": None,
                    "thread_id": "developer-new",
                    "summary": "Replacement implementation.",
                    "write_files": {"feature.txt": "new\n"},
                }],
                "publications": [publication()],
                "reviews": [passing_acceptance("reviewer-new", "Replacement pass.")],
            }
        ),
        encoding="utf-8",
    )

    stopped = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(replacement_agents)
    )

    assert stopped.returncode == 2
    assert stdout_json(stopped)["status"] == "requeue_required"
    state = load_only_run_state(git_repo)
    assert state["active_ticket_job"]["ticket_branch_generation"] == 1
    assert state.get("retired_job_generations") is None
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert [pull["state"] for pull in delivery["pull_requests"]] == ["OPEN"]


def test_run_stops_when_a_replacement_generation_drifts_again(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"3": ticket()},
        delivery={"required_checks": ["pending", "pending"]},
    )
    first_agents = git_repo / "first-agents.json"
    first_agents.write_text(
        json.dumps(
            {
                "developments": [{
                    "expected_thread_id": None,
                    "thread_id": "developer-old",
                    "summary": "Initial implementation.",
                    "write_files": {"feature.txt": "old\n"},
                }],
                "publications": [publication()],
                "reviews": [passing_acceptance("reviewer-old", "Initial pass.")],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    assert stdout_json(
        run_internal_stage(
            git_repo, fixture, "deliver", run_id, "--agent-fixture", str(first_agents)
        )
    )["status"] == "waiting_checks"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["3"]["body"] += "\nFirst replacement requirement."
    data["delivery"]["drift_after"] = {
        "action": "publish_branch",
        "kind": "ticket_content",
        "ticket_number": 3,
    }
    fixture.write_text(json.dumps(data), encoding="utf-8")
    replacement_agents = git_repo / "replacement-agents.json"
    replacement_agents.write_text(
        json.dumps(
            {
                "developments": [{
                    "expected_thread_id": None,
                    "thread_id": "developer-new",
                    "summary": "Replacement implementation.",
                    "write_files": {"feature.txt": "new\n"},
                }],
                "publications": [publication()],
                "reviews": [passing_acceptance("reviewer-new", "Replacement pass.")],
            }
        ),
        encoding="utf-8",
    )

    stopped = run_cli(
        git_repo, fixture, "run", "1", "--agent-fixture", str(replacement_agents)
    )

    assert stopped.returncode == 2
    assert stdout_json(stopped)["status"] == "requeue_required"
    state = load_only_run_state(git_repo)
    assert state["active_ticket_job"]["ticket_branch_generation"] == 1
    assert state.get("retired_job_generations") is None
