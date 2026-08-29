from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_run.state import StateStore
from agent_run.resume_audit_contract import resume_event_digest, resume_history_digest
from agent_run.state_contract import require_current_run_state
from agent_run.state_errors import IncompatibleRunStateError
from conftest import write_fixture
from test_cli import run_internal_stage, load_only_run_state, run_cli, stdout_json
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
    ] == ["completion_comment", "close_issue", "delete_managed_branch"]


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

    interrupted = run_internal_stage(
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
    invocation = interrupted_state.get("active_agent_invocation")
    invocation_thread = (
        invocation.get("reported_thread_id") or invocation.get("requested_thread_id")
        if isinstance(invocation, dict)
        else None
    )
    expected_thread = (
        active.get("development_thread_id")
        if isinstance(active, dict)
        and isinstance(active.get("development_thread_id"), str)
        else (
            invocation_thread
            if isinstance(invocation, dict)
            and invocation.get("role") == "development"
            and isinstance(invocation_thread, str)
            else None
        )
    )
    recovery_agents = _write_agents(
        git_repo / "agents-recovery.json",
        reviewer=(
            invocation_thread
            if isinstance(invocation, dict)
            and invocation.get("role") == "fresh_acceptance"
            and isinstance(invocation_thread, str)
            else "reviewer-recovery"
        ),
        expected_thread_id=expected_thread,
    )

    recovered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert recovered.returncode == 0, recovered.stdout
    _assert_exactly_once_delivery(git_repo, fixture)


def test_public_resume_recovers_completed_invocation_with_pending_attempt(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket()}
    )
    first_agents = _write_agents(
        git_repo / "agents-first.json", reviewer="reviewer-first"
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    interrupted = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(first_agents),
        "--crash-after-save",
        "8",
    )

    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    invocation = interrupted_state["active_agent_invocation"]
    assert interrupted_state["status"] == "execution_failed"
    assert invocation["status"] == "completed"
    assert invocation["semantic_attempt"]["status"] == "pending"
    recovery_agents = _write_agents(
        git_repo / "agents-recovery.json",
        reviewer="reviewer-recovery",
        expected_thread_id="developer-2",
    )

    recovered = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert recovered.returncode == 0, recovered.stdout
    _assert_exactly_once_delivery(git_repo, fixture)


def test_each_public_resume_is_a_distinct_attempt_bound_audit_event(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket()})
    failed_agents = git_repo / "agents-failed.json"
    failed_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "error_after_writes": "fixture process failed",
                        "write_files": {"partial.txt": "preserve\n"},
                    }
                ],
                "publications": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1", "--development-deadline", "42s")
    )["run_id"]
    failed = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(failed_agents),
    )
    assert failed.returncode == 2
    failed_state = load_only_run_state(git_repo)
    failed_invocation = failed_state["active_agent_invocation"]
    attempt_id = failed_invocation["semantic_attempt"]["attempt_id"]
    assert failed_invocation["deadline_seconds"] == 42
    assert failed_state["ticket_jobs"]["2"]["review_budget"][
        "development_attempts"
    ] == 1

    recovery_agents = _write_agents(
        git_repo / "agents-recovery.json",
        reviewer="reviewer-recovery",
        expected_thread_id="developer-2",
    )
    interrupted_authorization = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
        "--crash-after-save",
        "1",
    )
    assert interrupted_authorization.returncode == 2
    after_first = load_only_run_state(git_repo)
    assert after_first["resume_audit"]["total"] == 1
    assert after_first["resume_audit"]["history"][0][
        "semantic_attempt_id"
    ] == attempt_id
    # This is the durable shape left by an abrupt exit after Controller
    # authorization and before the successor Invocation reports ``started``.
    after_first["status"] = "active"
    after_first["terminal_kind"] = None
    after_first["active_agent_invocation"]["status"] = "resuming"
    started_at = after_first["active_agent_invocation"]["started_at"]
    for invocation in after_first["agent_invocation_history"]:
        if invocation.get("started_at") == started_at:
            invocation["status"] = "resuming"
    StateStore(git_repo / ".agent-run").save_run(run_id, after_first)

    interrupted_again = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
        "--crash-after-save",
        "1",
    )
    assert interrupted_again.returncode == 2
    after_second = load_only_run_state(git_repo)
    assert after_second["resume_audit"]["total"] == 2
    assert after_second["active_agent_invocation"]["status"] == "resuming"

    recovered = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )
    assert recovered.returncode == 0, recovered.stdout
    final_state = load_only_run_state(git_repo)
    resumes = final_state["resume_audit"]["history"]
    assert [event["sequence"] for event in resumes] == [1, 2, 3]
    assert {event["semantic_attempt_id"] for event in resumes} == {attempt_id}
    assert final_state["resume_audit"]["total"] == 3
    successor = next(
        invocation
        for invocation in final_state["agent_invocation_history"]
        if invocation.get("resume_id") == resumes[-1]["resume_id"]
    )
    assert successor["semantic_attempt"]["attempt_id"] == attempt_id
    assert successor["resume_sequence"] == resumes[-1]["sequence"]
    assert successor["deadline_seconds"] == 42
    assert datetime.fromisoformat(successor["deadline_at"]) - datetime.fromisoformat(
        successor["started_at"]
    ) == timedelta(seconds=42)
    development_invocations = [
        invocation
        for invocation in final_state["agent_invocation_history"]
        if invocation.get("role") == "development"
        and invocation.get("semantic_attempt", {}).get("attempt_id") == attempt_id
    ]
    assert len(development_invocations) == 2
    assert final_state["ticket_jobs"]["2"]["review_budget"][
        "development_attempts"
    ] == 1
    require_current_run_state(final_state)
    successor_index = final_state["agent_invocation_history"].index(successor)
    for field, value in (
        ("event_started_at", "2099-01-01T00:00:00+00:00"),
        ("event_attempt_id", "sha256:" + "0" * 64),
        ("invocation_resume_id", "sha256:" + "1" * 64),
    ):
        malformed = deepcopy(final_state)
        if field == "event_started_at":
            malformed["resume_audit"]["history"][-1][
                "successor_invocation_started_at"
            ] = value
        elif field == "event_attempt_id":
            malformed["resume_audit"]["history"][-1][
                "semantic_attempt_id"
            ] = value
        else:
            malformed["agent_invocation_history"][successor_index][
                "resume_id"
            ] = value
        with pytest.raises(IncompatibleRunStateError):
            require_current_run_state(malformed)

    fabricated = deepcopy(final_state)
    fabricated_successor = fabricated["agent_invocation_history"][successor_index]
    fabricated_successor["resume_id"] = "sha256:" + "2" * 64
    fabricated_successor["resume_sequence"] = 4
    with pytest.raises(IncompatibleRunStateError):
        require_current_run_state(fabricated)

    for mutation in ("failure_code", "rolling_digest", "unknown_field"):
        malformed_audit = deepcopy(final_state)
        if mutation == "failure_code":
            malformed_audit["resume_audit"]["history"][0][mutation] = "forged"
        elif mutation == "rolling_digest":
            malformed_audit["resume_audit"][mutation] = "sha256:" + "3" * 64
        else:
            malformed_audit["resume_audit"]["history"][0][mutation] = "private"
        with pytest.raises(IncompatibleRunStateError):
            require_current_run_state(malformed_audit)

    forged_identity = deepcopy(final_state)
    forged_event = forged_identity["resume_audit"]["history"][0]
    forged_event["resume_id"] = "sha256:" + "4" * 64
    forged_event["event_digest"] = resume_event_digest(forged_event)
    forged_identity["resume_audit"]["rolling_digest"] = resume_history_digest(
        forged_identity["resume_audit"]["history"]
    )
    with pytest.raises(IncompatibleRunStateError, match="noncanonical"):
        require_current_run_state(forged_identity)

    status = stdout_json(run_cli(git_repo, fixture, "status", run_id, "--json"))
    history = stdout_json(run_cli(git_repo, fixture, "history", run_id, "--json"))
    assert status["latest_resume"]["sequence"] == 3
    assert history["agent_resumes"] == resumes
    assert history["resume_audit"]["total"] == 3


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

    interrupted = run_internal_stage(
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

    recovered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert recovered.returncode == 0, recovered.stdout
    _assert_exactly_once_delivery(git_repo, fixture)


def test_abandon_recovers_ticket_after_close_response_loss(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket()},
        delivery={"crash_after_close_once": True},
    )
    agents = _write_agents(
        git_repo / "agents.json", reviewer="reviewer-first"
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]
    interrupted = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    assert interrupted_state["ticket_jobs"]["2"]["phase"] == "merged"
    interrupted_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert interrupted_fixture["issues"]["2"]["state"] == "CLOSED"

    abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "abandoned"
    recovered_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert recovered_fixture["issues"]["2"]["state"] == "OPEN"
    assert recovered_fixture["delivery"]["closed_issues"] == []


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

    result = run_internal_stage(
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

    interrupted = run_internal_stage(
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
                        "expected_thread_id": "unused-after-error",
                        "thread_id": "unused-after-error",
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
        "resume",
        run_id,
        "--agent-fixture",
        str(recovery_agents),
    )

    assert publisher_interrupted.returncode == 2
    assert stdout_json(publisher_interrupted)["status"] == "execution_failed"
    assert (checkout / "uncommitted.txt").read_text(
        encoding="utf-8"
    ) == "survives worker error\n"

    recovered = run_internal_stage(
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
