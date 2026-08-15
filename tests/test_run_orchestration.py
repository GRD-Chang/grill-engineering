from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.state_contract import IncompatibleRunStateError
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state import StateStore
from conftest import write_fixture
from test_cli import load_only_run_state, run_cli, stdout_json
from test_cli_delivery import passing_acceptance


def _ticket(
    number: int, *, blocked_by: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Deliver ticket {number}",
        "body": f"Implement ticket {number}.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": blocked_by or [],
    }


def _publication(number: int) -> dict[str, str]:
    return {
        "commit_message": f"feat(delivery): complete ticket {number}",
        "pr_title": f"feat(delivery): complete ticket {number}",
        "pr_body_markdown": f"""
## What Problem This Solves

Ticket {number} was not integrated into the Delivery Run.

## Why This Change Was Made

The scripted implementation delivers its required behavior.

## User Impact

The complete Ticket DAG can advance without another command.

## Evidence

The end-to-end scripted DAG scenario passed.
""".strip(),
    }


def _human_acceptance(thread_id: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "checks": {
            "e2e": {
                "status": "blocked",
                "evidence": "发生：产品决策缺失；尝试：已读取权威输入；人必须：作出产品决策。",
                "findings": [],
            },
            "standards": {
                "status": "pass",
                "evidence": "审查范围或基线：仓库编码规范与当前 diff；结论：未发现违反项。",
                "findings": [],
            },
            "spec": {
                "status": "pass",
                "evidence": "已核对的验收标准：当前 Ticket 的全部要求；覆盖结论：已覆盖。",
                "findings": [],
            },
        },
    }


class RevisionChangingAgents:
    def __init__(self, fixture: Path) -> None:
        self.fixture = fixture
        self.development_requests: list[dict[str, Any]] = []
        self.publication_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        checkout = Path(str(request["checkout"]))
        attempt = len(self.development_requests)
        (checkout / "revision.txt").write_text(
            f"revision {attempt}\n", encoding="utf-8"
        )
        if attempt == 1:
            data = json.loads(self.fixture.read_text(encoding="utf-8"))
            data["issues"]["2"]["body"] += "\nNew authoritative requirement."
            self.fixture.write_text(json.dumps(data), encoding="utf-8")
        return DevelopmentResult(
            thread_id="developer-2",
            summary=f"Implemented revision {attempt}.",
        )

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.publication_requests.append(request)
        return _publication(2)

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        return ReviewResult(
            thread_id="reviewer-2",
            artifact={
                key: value
                for key, value in passing_acceptance(
                    "unused", "The latest revision passed."
                ).items()
                if key != "thread_id"
            },
        )


class TicketRemovingAgents:
    def __init__(self, fixture: Path) -> None:
        self.fixture = fixture
        self.publication_calls = 0
        self.review_calls = 0

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        checkout = Path(str(request["checkout"]))
        (checkout / "removed.txt").write_text("stale\n", encoding="utf-8")
        data = json.loads(self.fixture.read_text(encoding="utf-8"))
        data["parent"]["sub_issues"].remove(2)
        self.fixture.write_text(json.dumps(data), encoding="utf-8")
        return DevelopmentResult(
            thread_id="developer-2",
            summary="The ticket disappeared while work was active.",
        )

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.publication_calls += 1
        return _publication(2)

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_calls += 1
        raise AssertionError("removed Ticket must not reach review")


def test_deliver_advances_the_complete_dag_and_enters_run_acceptance(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": _ticket(2),
            "3": _ticket(
                3, blocked_by=[{"number": 2, "state": "OPEN"}]
            ),
            "4": _ticket(
                4, blocked_by=[{"number": 2, "state": "OPEN"}]
            ),
            "5": _ticket(
                5,
                blocked_by=[
                    {"number": 3, "state": "OPEN"},
                    {"number": 4, "state": "OPEN"},
                ],
            ),
        },
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented ticket 2.",
                        "write_files": {"ticket-2.txt": "done\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-3",
                        "summary": "Implemented ticket 3.",
                        "write_files": {"ticket-3.txt": "done\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-4",
                        "summary": "Implemented ticket 4.",
                        "write_files": {"ticket-4.txt": "done\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-5",
                        "summary": "Implemented ticket 5.",
                        "write_files": {"ticket-5.txt": "done\n"},
                    },
                ],
                "publications": [
                    _publication(2),
                    _publication(3),
                    _publication(4),
                    _publication(5),
                ],
                "reviews": [
                    passing_acceptance("reviewer-2", "Ticket 2 passed."),
                    passing_acceptance("reviewer-3", "Ticket 3 passed."),
                    passing_acceptance("reviewer-4", "Ticket 4 passed."),
                    passing_acceptance("reviewer-5", "Ticket 5 passed."),
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

    assert delivered.returncode == 0, delivered.stdout
    assert stdout_json(delivered)["status"] == "run_acceptance_pending"
    state = load_only_run_state(git_repo)
    assert state["active_ticket_job"] is None
    assert state["terminal_kind"] == "all_tickets_completed"
    assert [
        state["ticket_jobs"][number]["phase"]
        for number in ("2", "3", "4", "5")
    ] == ["completed", "completed", "completed", "completed"]
    assert (
        state["ticket_jobs"]["3"]["base_sha"]
        == state["ticket_jobs"]["2"]["integrated_sha"]
    )
    assert (
        state["ticket_jobs"]["4"]["base_sha"]
        == state["ticket_jobs"]["3"]["integrated_sha"]
    )
    assert (
        state["ticket_jobs"]["5"]["base_sha"]
        == state["ticket_jobs"]["4"]["integrated_sha"]
    )
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert mutable_fixture["delivery"]["closed_issues"] == [2, 3, 4, 5]
    assert len(mutable_fixture["delivery"]["pull_requests"]) == 4
    commit_count = subprocess.run(
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
    assert commit_count == "4"


def test_graph_change_fails_closed_with_auditable_revisions(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    started = run_cli(git_repo, fixture, "start", "1")
    run_id = stdout_json(started)["run_id"]
    original = load_only_run_state(git_repo)

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(3)
    data["issues"]["3"] = _ticket(
        3, blocked_by=[{"number": 2, "state": "OPEN"}]
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")

    paused = run_cli(git_repo, fixture, "start", "1")

    assert paused.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "unsupported_scope_change"
    assert state["terminal_kind"] == "unsupported_scope_change"
    assert state["active_ticket_job"] is None
    assert state["ticket_graph"] == original["ticket_graph"]
    change = state["unsupported_scope_change"]
    assert change["accepted_graph_revision"] == original["ticket_graph"]["revision"]
    assert change["observed_graph_revision"] != change["accepted_graph_revision"]
    impact = change["graph_change_summary"]
    assert impact["added_tickets"] == [3]
    assert impact["removed_tickets"] == []
    assert impact["added_dependencies"] == [
        {"ticket_number": 3, "blocked_by": 2}
    ]

    assert change["observed_ticket_graph"]["ordered_ticket_numbers"] == [2, 3]

    data["parent"]["sub_issues"] = [2]
    data["issues"].pop("3")
    fixture.write_text(json.dumps(data), encoding="utf-8")
    restored = run_cli(git_repo, fixture, "start", "1")

    assert restored.returncode == 0
    restored_state = load_only_run_state(git_repo)
    assert restored_state["status"] == "active"
    assert "unsupported_scope_change" not in restored_state


def test_later_graph_drift_updates_observed_revision_without_accepting_it(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(3)
    data["issues"]["3"] = _ticket(3)
    fixture.write_text(json.dumps(data), encoding="utf-8")
    run_cli(git_repo, fixture, "start", "1")
    first = load_only_run_state(git_repo)["unsupported_scope_change"]
    accepted = first["accepted_graph_revision"]
    first_observed = first["observed_graph_revision"]

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(4)
    data["issues"]["4"] = _ticket(4)
    fixture.write_text(json.dumps(data), encoding="utf-8")
    resumed = run_cli(git_repo, fixture, "start", "1")

    assert resumed.returncode == 2
    state = load_only_run_state(git_repo)
    change = state["unsupported_scope_change"]
    assert state["status"] == "unsupported_scope_change"
    assert change["accepted_graph_revision"] == accepted
    assert change["observed_graph_revision"] != first_observed
    assert change["graph_change_summary"]["added_tickets"] == [3, 4]


def test_legacy_run_fails_closed_before_reading_graph_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    state.pop("accepted_ticket_graph_revision")
    states.save_run(str(state["run_id"]), state)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(3)
    data["issues"]["3"] = _ticket(3)
    fixture.write_text(json.dumps(data), encoding="utf-8")

    before = states.load_run(str(state["run_id"]))
    with pytest.raises(IncompatibleRunStateError, match="legacy state"):
        controller.resume(str(state["run_id"]))

    assert states.load_run(str(state["run_id"])) == before


def test_parent_clarification_and_comments_do_not_change_the_ticket_graph(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    run_id = stdout_json(
        run_cli(git_repo, fixture, "start", "1")
    )["run_id"]
    original = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] += "\nClarify the intended outcome."
    data["issues"]["2"]["comments"] = [
        {"author": "maintainer", "body": "Useful context only."}
    ]
    data["issues"]["2"]["assignees"] = ["maintainer"]
    data["issues"]["2"]["updated_at"] = "2099-01-01T00:00:00Z"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "start", "1")

    assert resumed.returncode == 0
    state = load_only_run_state(git_repo)
    assert state["status"] == "active"
    assert state["ticket_graph"]["revision"] == (
        original["ticket_graph"]["revision"]
    )
    assert state["parent"]["revision"] != original["parent"]["revision"]
    assert (
        state["ticket_graph"]["tickets"]["2"]["content_revision"]
        == original["ticket_graph"]["tickets"]["2"]["content_revision"]
    )
    assert "pending_structure_change" not in state


def test_human_blocked_branch_does_not_stop_independent_work(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": _ticket(2),
            "3": _ticket(3),
            "4": _ticket(
                4, blocked_by=[{"number": 2, "state": "OPEN"}]
            ),
        },
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Reached the product decision boundary.",
                        "write_files": {"ticket-2.txt": "candidate\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-3",
                        "summary": "Implemented independent ticket 3.",
                        "write_files": {"ticket-3.txt": "done\n"},
                    },
                ],
                "publications": [_publication(2), _publication(3)],
                "reviews": [
                    _human_acceptance("reviewer-2"),
                    passing_acceptance("reviewer-3", "Ticket 3 passed."),
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
    assert state["active_ticket_job"] is None
    assert state["ticket_jobs"]["2"]["phase"] == "blocked"
    assert state["ticket_jobs"]["3"]["phase"] == "completed"
    assert "4" not in state["ticket_jobs"]
    remaining = state["diagnostics"][0]["remaining_tickets"]
    assert remaining == [
        {
                "ticket_number": 2,
                "reason": "reviewer_requires_human",
                "human_blockers": [
                    "发生：产品决策缺失；尝试：已读取权威输入；人必须：作出产品决策。"
                ],
        },
        {"ticket_number": 4, "reason": "blocked_by_open_issues"},
    ]


def test_nonretryable_blocked_job_yields_to_independent_frontier(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
    )
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    blocked = state["active_ticket_job"]
    blocked.update(
        {
            "phase": "blocked",
            "blocked_reason": "published_head_mismatch",
        }
    )
    states.save_run(str(state["run_id"]), state)

    resumed, _ = controller.resume(str(state["run_id"]))

    assert resumed["status"] == "active"
    assert resumed["active_ticket_job"]["ticket_number"] == 3
    assert resumed["ticket_jobs"]["2"]["blocked_reason"] == (
        "published_head_mismatch"
    )


def test_run_recovers_after_process_failure_between_tickets(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
    )
    first_agents = git_repo / "agents-first.json"
    first_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented ticket 2.",
                        "write_files": {"ticket-2.txt": "done\n"},
                    }
                ],
                "publications": [_publication(2)],
                "reviews": [
                    passing_acceptance("reviewer-2", "Ticket 2 passed.")
                ],
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
    failed_state = load_only_run_state(git_repo)
    assert failed_state["status"] == "execution_failed"
    assert failed_state["terminal_kind"] == "execution_failed"
    assert failed_state["ticket_jobs"]["2"]["phase"] == "completed"
    mutable = json.loads(fixture.read_text(encoding="utf-8"))
    assert mutable["delivery"]["closed_issues"] == [2]
    assert len(mutable["delivery"]["pull_requests"]) == 1

    second_agents = git_repo / "agents-second.json"
    second_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-3",
                        "summary": "Implemented ticket 3 after recovery.",
                        "write_files": {"ticket-3.txt": "done\n"},
                    }
                ],
                "publications": [_publication(3)],
                "reviews": [
                    passing_acceptance("reviewer-3", "Ticket 3 passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    recovered = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(second_agents),
    )

    assert recovered.returncode == 0, recovered.stdout
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_acceptance_pending"
    mutable = json.loads(fixture.read_text(encoding="utf-8"))
    assert mutable["delivery"]["closed_issues"] == [2, 3]
    assert len(mutable["delivery"]["pull_requests"]) == 2


def test_inflight_ticket_revision_requires_a_fresh_requeue(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    agents = RevisionChangingAgents(fixture)
    tickets = TicketDeliveryEngine(
        git=git,
        states=states,
        github=FixtureGitHubPublisher(fixture, git),
        agents=agents,
    )

    completed = DeliveryRunEngine(
        controller=controller, tickets=tickets
    ).deliver(str(state["run_id"]))

    assert completed["status"] == "requeue_required"
    job = completed["ticket_jobs"]["2"]
    assert job["ticket_branch_generation"] == 1
    assert job["development_thread_id"] == "developer-2"
    assert len(agents.development_requests) == 1
    assert agents.publication_requests == []
    assert agents.review_requests == []


def test_close_response_loss_recovers_completed_job_without_duplicates(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2)},
        delivery={"crash_after_close_once": True},
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented ticket 2.",
                        "write_files": {"ticket-2.txt": "done\n"},
                    }
                ],
                "publications": [_publication(2)],
                "reviews": [
                    passing_acceptance("reviewer-2", "Ticket 2 passed.")
                ],
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
        str(agent_fixture),
    )

    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    assert interrupted_state["ticket_jobs"]["2"]["phase"] == "merged"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data["issues"]["2"]["state"] == "CLOSED"

    recovered = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert recovered.returncode == 0, recovered.stdout
    assert stdout_json(recovered)["status"] == "run_acceptance_pending"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert len(data["delivery"]["pull_requests"]) == 1
    assert data["delivery"]["closed_issues"] == [2]
    assert [
        mutation["action"] for mutation in data["delivery"]["mutations"]
    ] == ["completion_comment", "close_issue", "delete_managed_branch"]


@pytest.mark.parametrize(
    "recovery_case",
    [
        "response_loss",
        "response_loss_event_lag",
        "prepared_only",
        "dispatch_pending",
        "external_reopen",
    ],
)
def test_provisional_close_intent_can_abandon(
    git_repo: Path, recovery_case: str
) -> None:
    crash_flag = {
        "response_loss": "crash_after_close_once",
        "response_loss_event_lag": "crash_after_close_once",
        "prepared_only": "crash_before_close_dispatch_once",
        "dispatch_pending": "crash_after_close_dispatch_boundary_once",
        "external_reopen": "crash_after_close_dispatch_boundary_once",
    }[recovery_case]
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2)},
        delivery={crash_flag: True},
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented ticket 2.",
                        "write_files": {"ticket-2.txt": "done\n"},
                    }
                ],
                "publications": [_publication(2)],
                "reviews": [
                    passing_acceptance("reviewer-2", "Ticket 2 passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(run_cli(git_repo, fixture, "start", "1"))["run_id"]

    interrupted = run_cli(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert interrupted.returncode == 2
    interrupted_job = load_only_run_state(git_repo)["ticket_jobs"]["2"]
    assert interrupted_job["phase"] == "merged"
    assert isinstance(interrupted_job["ticket_close_intent"], dict)
    assert "ticket_close_ownership" not in interrupted_job
    interrupted_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert interrupted_data["issues"]["2"]["state"] == (
        "CLOSED"
        if recovery_case in {"response_loss", "response_loss_event_lag"}
        else "OPEN"
    )
    assert (
        interrupted_job["ticket_close_intent"].get("dispatch_attempted") is True
    ) == (recovery_case != "prepared_only")
    if recovery_case == "external_reopen":
        interrupted_data["delivery"]["external_ticket_transitions"] = [2]
        fixture.write_text(json.dumps(interrupted_data), encoding="utf-8")
    elif recovery_case == "response_loss_event_lag":
        interrupted_data["delivery"]["ticket_close_event_lag_reads"] = 2
        fixture.write_text(json.dumps(interrupted_data), encoding="utf-8")

    abandoned = run_cli(git_repo, fixture, "abandon", run_id)
    if recovery_case in {"dispatch_pending", "response_loss_event_lag"}:
        assert abandoned.returncode == 2
        assert stdout_json(abandoned)["status"] == "abandonment_pending"
        pending_data = json.loads(fixture.read_text(encoding="utf-8"))
        assert pending_data["delivery"]["mutations"] == interrupted_data[
            "delivery"
        ]["mutations"]
        abandoned = run_cli(git_repo, fixture, "abandon", run_id)
        if recovery_case == "dispatch_pending":
            assert abandoned.returncode == 2
            assert stdout_json(abandoned)["status"] == "abandonment_pending"
            repeated_data = json.loads(fixture.read_text(encoding="utf-8"))
            assert repeated_data["delivery"]["mutations"] == interrupted_data[
                "delivery"
            ]["mutations"]
            return
        assert abandoned.returncode == 2
        assert stdout_json(abandoned)["status"] == "abandonment_pending"
        if recovery_case == "response_loss_event_lag":
            abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "abandoned"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data["issues"]["2"]["state"] == "OPEN"
    abandonment_mutations = [
        mutation["action"]
        for mutation in data["delivery"]["mutations"]
        if mutation["action"].startswith("abandonment_")
    ]
    assert abandonment_mutations == (
        ["abandonment_reopen_issue", "abandonment_recovery_comment"]
        if recovery_case in {"response_loss", "response_loss_event_lag"}
        else []
    )
    close_mutations = [
        mutation["action"]
        for mutation in data["delivery"]["mutations"]
        if mutation["action"] == "close_issue"
    ]
    assert close_mutations == (
        ["close_issue"]
        if recovery_case in {"response_loss", "response_loss_event_lag"}
        else []
    )


def test_active_ticket_removal_stops_agents_and_preserves_work(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    agents = TicketRemovingAgents(fixture)

    blocked = DeliveryRunEngine(
        controller=controller,
        tickets=TicketDeliveryEngine(
            git=git,
            states=states,
            github=FixtureGitHubPublisher(fixture, git),
            agents=agents,
        ),
    ).deliver(str(state["run_id"]))

    assert blocked["status"] == "unsupported_scope_change"
    assert blocked["active_ticket_job"] is None
    assert blocked["unsupported_scope_change"]["graph_change_summary"][
        "removed_tickets"
    ] == [2]
    assert agents.publication_calls == 0
    assert agents.review_calls == 0
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data.get("delivery", {}).get("pull_requests", []) == []
    assert data.get("delivery", {}).get("closed_issues", []) == []
    checkout = states.root / "worktrees" / str(state["run_id"]) / "ticket-2"
    assert (checkout / "removed.txt").is_file()
