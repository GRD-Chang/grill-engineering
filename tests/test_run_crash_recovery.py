from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import passing_acceptance


def _ticket(
    number: int = 2, *, labels: list[str] | None = None
) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Deliver crash-safe orchestration",
        "body": "Complete the Ticket without duplicate external effects.",
        "state": "OPEN",
        "labels": labels if labels is not None else ["ready-for-agent"],
        "blocked_by": [],
    }


def _publication() -> dict[str, str]:
    return {
        "commit_message": "feat(delivery): complete crash-safe ticket",
        "pr_title": "feat(delivery): complete crash-safe ticket",
        "pr_body_markdown": """
## What Problem This Solves

The Ticket needs a crash-safe delivery path.

## Why This Change Was Made

The candidate implements the requested behavior.

## User Impact

Delivery can resume without duplicate effects.

## Evidence

The public CLI recovery matrix passed.
""".strip(),
    }


def _write_agents(
    path: Path,
    *,
    reviewer: str,
    expected_thread_id: str | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": expected_thread_id,
                        "thread_id": expected_thread_id or "developer-2",
                        "summary": "Implemented the crash-safe Ticket.",
                        "write_files": {"crash-safe.txt": "done\n"},
                    }
                ],
                "publications": [_publication()],
                "reviews": [
                    passing_acceptance(
                        reviewer, "The real CLI delivery path passed."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _assert_exactly_once_delivery(repo: Path, fixture: Path) -> None:
    state = load_only_run_state(repo)
    job = state["ticket_jobs"]["2"]
    assert state["status"] == "run_acceptance_pending"
    assert job["phase"] == "completed"
    assert job["validation_attempts"] >= 1
    record = job["acceptance_record"]
    assert record["reviewed_candidate_sha"] == job["candidate_sha"]
    assert record["effective_revision"] == job["effective_revision"]

    live = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = live["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["acceptance_records"] == []
    assert len(delivery["agent_run_status"]) == 1
    assert delivery["closed_issues"] == [2]
    assert [
        mutation["action"] for mutation in delivery["mutations"]
    ] == ["completion_comment", "close_issue"]


@pytest.mark.parametrize("save_number", range(1, 16))
def test_public_cli_recovers_after_every_durable_save_boundary(
    git_repo: Path, save_number: int
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket()}
    )
    first_agents = _write_agents(
        git_repo / "agents-first.json", reviewer="reviewer-first"
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]

    interrupted = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(first_agents),
        "--crash-after-save",
        str(save_number),
    )

    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    active = interrupted_state.get("active_ticket_job")
    expected_thread = (
        active.get("development_thread_id")
        if isinstance(active, dict)
        and isinstance(active.get("development_thread_id"), str)
        else None
    )
    recovery_agents = _write_agents(
        git_repo / "agents-recovery.json",
        reviewer="reviewer-recovery",
        expected_thread_id=expected_thread,
    )

    recovered = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert recovered.returncode == 0, recovered.stdout
    _assert_exactly_once_delivery(git_repo, fixture)


@pytest.mark.parametrize(
    "action",
    [
        "ensure_ticket_branch",
        "publish_branch",
        "ensure_ticket_pr",
        "record_agent_run_status",
        "squash_merge",
        "sync_run_branch",
        "close",
    ],
)
def test_public_cli_recovers_after_external_response_loss(
    git_repo: Path, action: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={f"crash_after_{action}_once": True},
    )
    first_agents = _write_agents(
        git_repo / "agents-first.json", reviewer="reviewer-first"
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]

    interrupted = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(first_agents),
    )

    assert interrupted.returncode == 2
    active = load_only_run_state(git_repo).get("active_ticket_job")
    expected_thread = (
        active.get("development_thread_id")
        if isinstance(active, dict)
        and isinstance(active.get("development_thread_id"), str)
        else None
    )
    recovery_agents = _write_agents(
        git_repo / "agents-recovery.json",
        reviewer="reviewer-recovery",
        expected_thread_id=expected_thread,
    )

    recovered = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert recovered.returncode == 0, recovered.stdout
    _assert_exactly_once_delivery(git_repo, fixture)


def test_ready_for_human_remainder_is_reported_after_independent_work(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": _ticket(2, labels=["ready-for-human"]),
            "3": _ticket(3),
        },
    )
    agents = git_repo / "agents.json"
    agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-3",
                        "summary": "Completed the independent Ticket.",
                        "write_files": {"ticket-3.txt": "done\n"},
                    }
                ],
                "publications": [
                    {
                        **_publication(),
                        "commit_message": (
                            "feat(delivery): complete independent ticket"
                        ),
                        "pr_title": (
                            "feat(delivery): complete independent ticket"
                        ),
                        "pr_body_markdown": _publication()[
                            "pr_body_markdown"
                        ].replace("#2", "#3"),
                    }
                ],
                "reviews": [
                    passing_acceptance(
                        "reviewer-3", "The independent Ticket passed."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]

    result = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert result.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["ticket_jobs"]["3"]["phase"] == "completed"
    assert state["terminal_kind"] == "waiting_human"
    assert state["diagnostics"][0]["remaining_tickets"] == [
        {
            "ticket_number": 2,
            "reason": "disqualifying_label:ready-for-human",
        }
    ]


@pytest.mark.parametrize(
    "error_field",
    ["error_after_writes", "sandbox_error_after_writes"],
)
def test_public_cli_preserves_uncommitted_work_after_worker_error(
    git_repo: Path, error_field: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket()}
    )
    first_agents = git_repo / "agents-first.json"
    first_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "unused-after-error",
                        "summary": "unused",
                        "write_files": {
                            "uncommitted.txt": "survives worker error\n"
                        },
                        error_field: "simulated Development Worker timeout",
                    }
                ],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]

    interrupted = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(first_agents),
    )

    assert interrupted.returncode == 2
    interrupted_output = stdout_json(interrupted)
    assert interrupted_output["status"] == "execution_failed"
    interrupted_state = load_only_run_state(git_repo)
    assert interrupted_state["status"] == "execution_failed"
    assert interrupted_state["terminal_kind"] == "execution_failed"
    assert interrupted_state["diagnostics"] == [
        {
            "code": "command_failed",
            "message": "simulated Development Worker timeout",
        }
    ]
    checkout = (
        git_repo
        / ".agent-run"
        / "worktrees"
        / run_id
        / "ticket-2"
    )
    assert (checkout / "uncommitted.txt").read_text(
        encoding="utf-8"
    ) == "survives worker error\n"
    live = json.loads(fixture.read_text(encoding="utf-8"))
    live["delivery"]["crash_after_ensure_ticket_branch_once"] = True
    fixture.write_text(json.dumps(live), encoding="utf-8")
    recovery_agents = git_repo / "agents-recovery.json"
    recovery_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-recovered",
                        "summary": "Completed the preserved work.",
                        "expected_files": {
                            "uncommitted.txt": "survives worker error\n"
                        },
                        "write_files": {"completed.txt": "done\n"},
                    }
                ],
                "publications": [_publication()],
                "reviews": [
                    passing_acceptance(
                        "reviewer-recovered",
                        "Fresh review covered the preserved work.",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    publisher_interrupted = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert publisher_interrupted.returncode == 2
    assert stdout_json(publisher_interrupted)["status"] == "execution_failed"
    assert (checkout / "uncommitted.txt").read_text(
        encoding="utf-8"
    ) == "survives worker error\n"

    recovered = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert recovered.returncode == 0, recovered.stdout
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_acceptance_pending"
    assert state["ticket_jobs"]["2"]["reviewer_thread_ids"] == [
        "reviewer-recovered"
    ]
    assert not checkout.exists()
