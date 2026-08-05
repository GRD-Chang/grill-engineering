from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json


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


def passing_acceptance(thread_id: str, evidence: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "verdict": "pass",
        "checks": {
            "e2e": {"status": "pass", "evidence": evidence},
            "standards": {
                "status": "pass",
                "evidence": "The standards review passed.",
            },
            "spec": {
                "status": "pass",
                "evidence": "The spec review passed.",
            },
        },
        "findings": [],
        "human_blockers": [],
    }


def repair_acceptance(thread_id: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "verdict": "request_changes",
        "checks": {
            "e2e": {
                "status": "fail",
                "evidence": "The first candidate lacks the repair.",
            },
            "standards": {
                "status": "pass",
                "evidence": "The standards review passed.",
            },
            "spec": {
                "status": "pass",
                "evidence": "The spec review passed.",
            },
        },
        "findings": [
            {
                "id": "F1",
                "problem": "The repair is missing.",
                "evidence": "feature.txt has one line.",
                "required_outcome": "Add the repair.",
                "verification": "Inspect feature.txt.",
            }
        ],
        "human_blockers": [],
    }


def test_scripted_cli_delivers_active_ticket_end_to_end(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": ticket()})
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

    delivered = run_cli(
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
    assert delivery["linked_branches"]["parent"] == state["run_branch"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["closed_issues"] == [3]
    assert [
        mutation["action"] for mutation in delivery["mutations"]
    ] == ["completion_comment", "close_issue"]
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

    replayed = run_cli(
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
    delivered = run_cli(
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
    accepted = run_cli(
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
    published = run_cli(
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

    waiting = run_cli(
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
            "validation_verdict": "pass",
            "lane_statuses": {"e2e": "pass", "standards": "pass", "spec": "pass"},
            "required_checks": "pending",
            "next_action": "wait for Required Checks",
        }
    ]
    checkout = git_repo / ".agent-run" / "worktrees" / run_id / "ticket-3"
    assert checkout.is_dir()
    git_link = (checkout / ".git").read_text(encoding="utf-8")

    completed = run_cli(
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

    result = run_cli(
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


def test_ticket_revision_change_invalidates_old_acceptance_and_reuses_job(
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
    waiting = run_cli(
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
                        "expected_thread_id": "developer-1",
                        "thread_id": "developer-1",
                        "summary": "Implemented revision two.",
                        "write_files": {"feature.txt": "revision two\n"},
                    }
                ],
                "publications": [publication()],
                "reviews": [
                    passing_acceptance(
                        "reviewer-1", "A reused reviewer must be rejected."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    rejected = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(second_agents),
    )
    assert rejected.returncode != 0
    assert "new Reviewer Thread" in rejected.stdout

    final_agents = git_repo / "agents-final.json"
    final_agents.write_text(
        json.dumps(
                {
                    "developments": [],
                    "publications": [publication()],
                "reviews": [
                    passing_acceptance(
                        "reviewer-2", "Revision two passed."
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    completed = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(final_agents),
    )

    assert completed.returncode == 0, completed.stderr
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["3"]
    assert job["effective_revision"] != old_revision
    assert job["development_thread_id"] == "developer-1"
    assert job["reviewer_thread_ids"] == ["reviewer-1", "reviewer-2"]
    assert job["modification_attempts"] == 1
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(mutable_fixture["delivery"]["pull_requests"]) == 1
