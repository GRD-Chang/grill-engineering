from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json


HUMAN_BLOCKER = (
    "GitHub denied access; tried gh issue view; grant Issue read access."
)


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


def human_blocker_step(thread_id: str) -> dict[str, object]:
    return {
        "expected_thread_id": None,
        "thread_id": thread_id,
        "human_blockers": [HUMAN_BLOCKER],
    }


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
        "deliver",
        run_id,
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
        "deliver",
        run_id,
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
                        "artifact": {"verdict": "pass"},
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
        "deliver",
        run_id,
        "--agent-fixture",
        str(failed_agents),
    )
    assert failed.returncode == 2
    assert stdout_json(failed)["status"] == "execution_failed"

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


def test_parent_only_cli_delivers_to_default_branch_after_explicit_approval(
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

    delivered = run_cli(
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
    assert mutable_fixture["delivery"]["linked_branches"] == {
        "1": state["parent_branch"]
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
        "deliver",
        run_id,
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

    blocked = run_cli(
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
        "deliver",
        run_id,
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
    assert resumed_job["publication_attempts"] == 2
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
        "deliver",
        run_id,
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
    accepted_boundary = {
        "candidate_sha": failed_job["candidate_sha"],
        "acceptance_record": failed_job["acceptance_record"],
    }

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["error"] = {"code": "github_read_failed", "message": "offline"}
    fixture.write_text(json.dumps(data), encoding="utf-8")

    unreadable = run_cli(git_repo, fixture, "resume", run_id)

    assert unreadable.returncode == 2
    assert stdout_json(unreadable)["status"] == "execution_failed"
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
    resumed_job = load_only_run_state(git_repo)["parent_job"]
    assert resumed_job["modification_attempts"] == 1
    assert resumed_job["validation_attempts"] == 1
    assert resumed_job["publication_attempts"] == 2
    assert {
        "candidate_sha": resumed_job["candidate_sha"],
        "acceptance_record": resumed_job["acceptance_record"],
    } == accepted_boundary
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1


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
    delivered = run_cli(
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
    delivered = run_cli(
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


def test_parent_only_approve_rechecks_required_checks_before_merge(
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
    delivered = run_cli(
        git_repo, fixture, "deliver", run_id, "--agent-fixture", str(agents)
    )
    assert delivered.returncode == 0, delivered.stderr
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["required_checks"] = ["none", "fail"]
    fixture.write_text(json.dumps(data), encoding="utf-8")

    approval = run_cli(git_repo, fixture, "approve", run_id)

    assert approval.returncode == 0, approval.stderr
    assert stdout_json(approval)["status"] == "parent_delivery_pending"
    state = load_only_run_state(git_repo)
    assert state["parent_job"]["phase"] == "repairing"
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"][0]["state"] == "OPEN"


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
    delivered = run_cli(
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
    first = run_cli(
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

    delivered = run_cli(
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
        result = run_cli(git_repo, fixture, command, identifier, *extra)
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
    delivered = run_cli(
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
    blocked_replay = run_cli(
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

    replayed = run_cli(
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
        replayed = run_cli(
            git_repo,
            fixture,
            command,
            run_id,
            *("--agent-fixture", str(ticket_agents))
            if command == "deliver"
            else (),
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
        "deliver",
        run_id,
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
        "deliver",
        run_id,
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
    assert resumed_job["publication_attempts"] == 2
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
    invalid_publications = [{"invalid": "publication"}]
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
        "deliver",
        run_id,
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
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"] == []
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    failed_state["active_agent_invocation"] = {
        "work_subject": "ticket:3",
        "generation": failed_job["ticket_branch_generation"],
        "role": "publication",
        "phase": "publication",
        "mode": "fresh",
        "status": "failed",
        "requested_thread_id": None,
        "reported_thread_id": "publication-thread-1",
        "attempt_count": 1,
        "started_at": "2026-08-11T00:00:00+00:00",
        "ended_at": "2026-08-11T00:00:01+00:00",
        "error": "invalid publication",
        "return_code": 0,
        "signal": None,
    }
    failed_job["publication_thread_id"] = "publication-thread-1"
    active_job = failed_state.get("active_ticket_job")
    if isinstance(active_job, dict):
        active_job["publication_thread_id"] = "publication-thread-1"
    state_path.write_text(
        json.dumps(failed_state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    bypass = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
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
    assert completed_job["publication_attempts"] == 2
    assert len(json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["pull_requests"]) == 1
    completed_state = load_only_run_state(git_repo)
    successor = completed_state["active_agent_invocation"]
    assert successor["status"] == "completed"
    assert successor["mode"] == (
        "resume" if expected_thread else "new-thread"
    )
    assert successor["requested_thread_id"] == expected_thread
    assert successor["reported_thread_id"] == successor_thread
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
    delivered = run_cli(
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
    failed = run_cli(
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
    failed_state["active_agent_invocation"] = {
        "work_subject": f"run-repair:{run_id}",
        "generation": repair_job["repair_generation"],
        "role": "publication",
        "phase": "publication",
        "mode": "fresh",
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
    repair_successor = resumed_state["agent_invocation_history"][-2]
    assert repair_successor["work_subject"] == f"run-repair:{run_id}"
    assert repair_successor["mode"] == successor_mode
    assert repair_successor["requested_thread_id"] == expected_thread
    assert repair_successor["reported_thread_id"] == (
        expected_thread or "run-repair-publication-2"
    )
    # The completed repair is followed by the new whole-Run Fresh Acceptance.
    assert resumed_state["active_agent_invocation"]["work_subject"] == (
        f"run-acceptance:{run_id}"
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

    rejected = run_cli(
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
