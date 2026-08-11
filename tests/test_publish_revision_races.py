from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import passing_acceptance


def _ticket() -> dict[str, Any]:
    return {
        "number": 2,
        "title": "Guard publish revisions",
        "body": "Do not publish stale evidence.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def _publication(label: str) -> dict[str, str]:
    return {
        "commit_message": f"fix(run): publish {label} revision",
        "pr_title": f"fix(run): publish {label} revision",
        "pr_body_markdown": f"""
## What Problem This Solves

Stale publication evidence must not advance.

## Why This Change Was Made

This is the {label} candidate.

## User Impact

Only the current revision can merge.

## Evidence

The public CLI publish-race scenario passed.
""".strip(),
    }


def _write_agents(path: Path, *, two_revisions: bool) -> Path:
    developments = [
        {
            "expected_thread_id": None,
            "thread_id": "developer-2",
            "summary": "Implemented the original revision.",
            "write_files": {"revision.txt": "original\n"},
        }
    ]
    publications = [_publication("original")]
    reviews = [
        passing_acceptance(
            "reviewer-original", "The original candidate passed."
        )
    ]
    if two_revisions:
        developments.append(
            {
                "expected_thread_id": "developer-2",
                "thread_id": "developer-2",
                "summary": "Rebuilt against the changed revision.",
                "write_files": {"revision.txt": "current\n"},
            }
        )
        publications.append(_publication("current"))
        reviews.append(
            passing_acceptance(
                "reviewer-current", "The current candidate passed."
            )
        )
    path.write_text(
        json.dumps(
            {
                "developments": developments,
                "publications": publications,
                "reviews": reviews,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_recovery_agents(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": "developer-2",
                        "thread_id": "developer-2",
                        "summary": (
                            "Rebuilt after the invalidated revision returned."
                        ),
                        "write_files": {"revision.txt": "rebuilt\n"},
                    }
                ],
                "publications": [_publication("rebuilt")],
                "reviews": [
                    passing_acceptance(
                        "reviewer-rebuilt",
                        "Fresh review passed after ABA recovery.",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "action", ["publish_branch", "ensure_ticket_pr", "required_checks"]
)
def test_content_change_at_publish_boundary_restarts_before_merge(
    git_repo: Path, action: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={
            "drift_after": {
                "action": action,
                "kind": "ticket_content",
            },
            "required_checks": (
                ["pending", "none"]
                if action == "required_checks"
                else ["none"]
            ),
        },
    )
    agents = _write_agents(
        git_repo / "agents.json", two_revisions=True
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]

    delivered = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert delivered.returncode == 0, delivered.stdout
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["2"]
    assert state["status"] == "run_acceptance_pending"
    assert job["phase"] == "completed"
    assert job["modification_attempts"] == 1
    assert job["validation_attempts"] == 1
    assert job["reviewer_thread_ids"] == [
        "reviewer-original",
        "reviewer-current",
    ]
    assert job["acceptance_record"]["reviewer_thread_id"] == (
        "reviewer-current"
    )
    assert job["acceptance_record"]["effective_revision"] == (
        job["effective_revision"]
    )
    live = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(live["delivery"]["pull_requests"]) == 1
    assert live["delivery"]["acceptance_records"] == []
    assert len(live["delivery"]["agent_run_status"]) == 1
    assert live["delivery"]["closed_issues"] == [2]


@pytest.mark.parametrize(
    "action", ["publish_branch", "ensure_ticket_pr", "required_checks"]
)
def test_ticket_removal_at_publish_boundary_pauses_same_command(
    git_repo: Path, action: str
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={
            "drift_after": {
                "action": action,
                "kind": "ticket_removal",
            },
            "required_checks": ["pending"],
        },
    )
    agents = _write_agents(
        git_repo / "agents.json", two_revisions=False
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]

    delivered = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )

    assert delivered.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "unsupported_scope_change"
    assert state["active_ticket_job"] is None
    assert state["unsupported_scope_change"]["graph_change_summary"][
        "removed_tickets"
    ] == [2]
    live = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = live["delivery"]
    assert delivery.get("acceptance_records", []) == []
    assert delivery.get("closed_issues", []) == []
    assert delivery.get("mutations", []) == []
    assert not any(
        pr.get("state") == "MERGED"
        for pr in delivery.get("pull_requests", [])
        if isinstance(pr, dict)
    )


def test_resume_freezes_completed_ticket_assets_after_graph_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={"required_checks": ["none"]},
    )
    agents = _write_agents(git_repo / "agents.json", two_revisions=False)
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    delivered = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert delivered.returncode == 0, delivered.stderr

    before_state = load_only_run_state(git_repo)
    before_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    job = before_state["ticket_jobs"]["2"]
    run_branch = before_state["run_branch"]
    ticket_completion_record = {
        "ticket_number": 2,
        "integrated_sha": job["integrated_sha"],
        "effective_revision": job["effective_revision"],
        "acceptance_record": job["acceptance_record"],
    }
    frozen = {
        "candidate_sha": job["candidate_sha"],
        "acceptance_record": job["acceptance_record"],
        "ticket_completion_record": ticket_completion_record,
        "local_run_branch_sha": subprocess.run(
            ["git", "rev-parse", run_branch],
            cwd=git_repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip(),
        "remote_run_branch_sha": before_fixture["delivery"][
            "published_branches"
        ][run_branch],
        "pull_requests": before_fixture["delivery"]["pull_requests"],
        "mutations": before_fixture["delivery"]["mutations"],
    }

    added = _ticket()
    added.update({"number": 4, "title": "Unexpected added ticket"})
    before_fixture["parent"]["sub_issues"] = [2, 4]
    before_fixture["issues"]["4"] = added
    fixture.write_text(json.dumps(before_fixture), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "resume", run_id)

    assert resumed.returncode == 2
    assert stdout_json(resumed)["status"] == "unsupported_scope_change"
    after_state = load_only_run_state(git_repo)
    after_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    after_job = after_state["ticket_jobs"]["2"]
    assert after_job["candidate_sha"] == frozen["candidate_sha"]
    assert after_job["acceptance_record"] == frozen["acceptance_record"]
    assert {
        "ticket_number": 2,
        "integrated_sha": after_job["integrated_sha"],
        "effective_revision": after_job["effective_revision"],
        "acceptance_record": after_job["acceptance_record"],
    } == frozen["ticket_completion_record"]
    assert subprocess.run(
        ["git", "rev-parse", run_branch],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip() == frozen["local_run_branch_sha"]
    assert after_fixture["delivery"]["published_branches"][run_branch] == (
        frozen["remote_run_branch_sha"]
    )
    assert after_fixture["delivery"]["pull_requests"] == frozen[
        "pull_requests"
    ]
    assert after_fixture["delivery"]["mutations"] == frozen["mutations"]

    abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "abandoned"
    abandoned_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert abandoned_fixture["issues"]["2"]["state"] == "OPEN"
    assert abandoned_fixture["delivery"]["closed_issues"] == []
    assert [
        mutation["action"]
        for mutation in abandoned_fixture["delivery"]["mutations"]
        if mutation["action"].startswith("abandonment_")
    ] == ["abandonment_recovery_comment", "abandonment_reopen_issue"]
    frozen_after_abandon = abandoned_fixture["delivery"]["mutations"]

    for command in ("resume", "deliver"):
        replayed = run_cli(
            git_repo,
            fixture,
            command,
            run_id,
            *("--agent-fixture", str(agents)) if command == "deliver" else (),
        )
        assert replayed.returncode == 0, replayed.stderr
        assert stdout_json(replayed)["status"] == "abandoned"
    replayed_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert replayed_fixture["delivery"]["mutations"] == frozen_after_abandon


def test_abandon_closes_active_ticket_pr_after_graph_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={"required_checks": ["pending"]},
    )
    agents = _write_agents(git_repo / "agents.json", two_revisions=False)
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    waiting = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert stdout_json(waiting)["status"] == "waiting_checks"
    before_drift = json.loads(fixture.read_text(encoding="utf-8"))
    assert before_drift["delivery"]["pull_requests"][0]["state"] == "OPEN"

    added = _ticket()
    added.update({"number": 4, "title": "Unexpected added ticket"})
    before_drift["parent"]["sub_issues"] = [2, 4]
    before_drift["issues"]["4"] = added
    fixture.write_text(json.dumps(before_drift), encoding="utf-8")
    blocked = run_cli(git_repo, fixture, "resume", run_id)
    assert blocked.returncode == 2

    abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "abandoned"
    after = json.loads(fixture.read_text(encoding="utf-8"))
    assert after["delivery"]["pull_requests"][0]["state"] == "CLOSED"
    assert {"action": "close_change_pr", "pr_number": 1} in after["delivery"][
        "mutations"
    ]


@pytest.mark.parametrize(
        ("action", "mismatch_save"),
        [
            ("publish_branch", 12),
            ("ensure_ticket_pr", 13),
            ("required_checks", 13),
        ],
)
def test_aba_revision_after_crash_still_forces_fresh_rebuild(
    git_repo: Path, action: str, mismatch_save: int
) -> None:
    original_body = _ticket()["body"]
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={
            "drift_after": {
                "action": action,
                "kind": "ticket_content",
            },
            "required_checks": (
                ["pending", "none"]
                if action == "required_checks"
                else ["none"]
            ),
        },
    )
    first_agents = _write_agents(
        git_repo / "agents-first.json", two_revisions=False
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
        str(mismatch_save),
    )

    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    assert interrupted_state["ticket_jobs"]["2"]["phase"] == "blocked"
    assert interrupted_state["ticket_jobs"]["2"]["blocked_reason"] == (
        "effective_revision_mismatch"
    )
    live = json.loads(fixture.read_text(encoding="utf-8"))
    live["issues"]["2"]["body"] = original_body
    fixture.write_text(json.dumps(live), encoding="utf-8")
    recovery_agents = _write_recovery_agents(
        git_repo / "agents-recovery.json"
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
    state = load_only_run_state(git_repo)
    job = state["ticket_jobs"]["2"]
    assert state["status"] == "run_acceptance_pending"
    assert job["phase"] == "completed"
    assert job["reviewer_thread_ids"] == [
        "reviewer-original",
        "reviewer-rebuilt",
    ]
    assert job["acceptance_record"]["reviewer_thread_id"] == (
        "reviewer-rebuilt"
    )
    assert job["acceptance_record"]["effective_revision"] == (
        job["effective_revision"]
    )
    live = json.loads(fixture.read_text(encoding="utf-8"))
    delivery = live["delivery"]
    assert len(delivery["pull_requests"]) == 1
    assert delivery["acceptance_records"] == []
    assert len(delivery["agent_run_status"]) == 1
    assert delivery["closed_issues"] == [2]
    assert [
        mutation["action"] for mutation in delivery["mutations"]
    ] == ["completion_comment", "close_issue", "delete_managed_branch"]
