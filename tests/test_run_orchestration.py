from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
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
        "verdict": "human",
        "checks": {
            "e2e": {
                "status": "blocked",
                "evidence": "A product decision is required.",
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
        "findings": [],
        "human_blockers": [
            "A product decision is absent from authoritative inputs."
        ],
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


def test_graph_change_pauses_with_impact_until_exact_revision_is_confirmed(
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

    paused = run_cli(git_repo, fixture, "resume", run_id)

    assert paused.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "structure_change_pending"
    assert state["terminal_kind"] == "structure_change_pending"
    assert state["active_ticket_job"] is None
    assert state["ticket_graph"] == original["ticket_graph"]
    impact = state["pending_structure_change"]["graph_change_summary"]
    assert impact["added_tickets"] == [3]
    assert impact["removed_tickets"] == []
    assert impact["added_dependencies"] == [
        {"ticket_number": 3, "blocked_by": 2}
    ]

    confirmed = run_cli(
        git_repo, fixture, "confirm-structure", run_id
    )

    assert confirmed.returncode == 0, confirmed.stdout
    confirmed_state = load_only_run_state(git_repo)
    assert confirmed_state["status"] == "active"
    assert confirmed_state["active_ticket_job"]["ticket_number"] == 2
    assert confirmed_state["ticket_graph"]["ordered_ticket_numbers"] == [2, 3]
    assert "pending_structure_change" not in confirmed_state


def test_structure_confirmation_never_authorizes_a_later_graph(
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
    run_cli(git_repo, fixture, "resume", run_id)
    first_pending = load_only_run_state(git_repo)[
        "pending_structure_change"
    ]["proposed_revision"]

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(4)
    data["issues"]["4"] = _ticket(4)
    fixture.write_text(json.dumps(data), encoding="utf-8")
    confirmed = run_cli(
        git_repo, fixture, "confirm-structure", run_id
    )

    assert confirmed.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "structure_change_pending"
    assert (
        state["pending_structure_change"]["accepted_revision"]
        == first_pending
    )
    assert (
        state["pending_structure_change"]["proposed_revision"]
        != first_pending
    )
    assert state["pending_structure_change"]["graph_change_summary"][
        "added_tickets"
    ] == [4]


def test_legacy_run_does_not_silently_accept_graph_drift(
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

    resumed, _ = controller.resume(str(state["run_id"]))

    assert resumed["status"] == "structure_change_pending"
    assert resumed["ticket_graph"]["ordered_ticket_numbers"] == [2]
    assert resumed["pending_structure_change"]["graph_change_summary"][
        "added_tickets"
    ] == [3]


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
    fixture.write_text(json.dumps(data), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "resume", run_id)

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
                "A product decision is absent from authoritative inputs."
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


def test_inflight_ticket_revision_discards_stale_work_and_restarts_same_job(
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

    assert completed["status"] == "run_acceptance_pending"
    job = completed["ticket_jobs"]["2"]
    assert job["development_thread_id"] == "developer-2"
    assert job["modification_attempts"] == 1
    assert len(agents.development_requests) == 2
    assert len(agents.publication_requests) == 1
    assert len(agents.review_requests) == 1
    first_revision = agents.development_requests[0]["effective_revision"]
    second_revision = agents.development_requests[1]["effective_revision"]
    assert first_revision != second_revision


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


def test_active_ticket_removal_pauses_structure_in_the_same_command(
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
    tickets = TicketDeliveryEngine(
        git=git,
        states=states,
        github=FixtureGitHubPublisher(fixture, git),
        agents=agents,
    )

    paused = DeliveryRunEngine(
        controller=controller, tickets=tickets
    ).deliver(str(state["run_id"]))

    assert paused["status"] == "structure_change_pending"
    assert paused["active_ticket_job"] is None
    impact = paused["pending_structure_change"]["graph_change_summary"]
    assert impact["removed_tickets"] == [2]
    assert agents.publication_calls == 0
    assert agents.review_calls == 0
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data.get("delivery", {}).get("pull_requests", []) == []
    assert data.get("delivery", {}).get("closed_issues", []) == []
    checkout = (
        states.root
        / "worktrees"
        / str(state["run_id"])
        / "ticket-2"
    )
    old_branch = f"agent-run/{state['run_id']}/ticket-2"
    assert (checkout / "removed.txt").is_file()

    removed, _ = controller.confirm_structure(str(state["run_id"]))

    assert removed["status"] == "parent_delivery_pending"
    assert not checkout.exists()
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{old_branch}"],
            cwd=git_repo,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"] = [2]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    paused_again, _ = controller.resume(str(state["run_id"]))
    assert paused_again["status"] == "structure_change_pending"
    readded, _ = controller.confirm_structure(str(state["run_id"]))
    assert readded["status"] == "active"

    recovery_fixture = git_repo / "agents-readded.json"
    recovery_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-readded",
                        "summary": "Rebuilt the re-added Ticket cleanly.",
                        "absent_files": ["removed.txt"],
                        "write_files": {"readded.txt": "clean\n"},
                    }
                ],
                "publications": [_publication(2)],
                "reviews": [
                    passing_acceptance(
                        "reviewer-readded",
                        "The clean re-added Ticket passed.",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    completed = DeliveryRunEngine(
        controller=controller,
        tickets=TicketDeliveryEngine(
            git=git,
            states=states,
            github=FixtureGitHubPublisher(fixture, git),
            agents=FixtureAgentBackend(recovery_fixture),
        ),
    ).deliver(str(state["run_id"]))

    assert completed["status"] == "run_acceptance_pending"
    assert completed["ticket_jobs"]["2"]["ticket_branch"].endswith(
        "/ticket-2-generation-2"
    )


def test_stale_removal_confirmation_does_not_retire_readded_ticket(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    started, _ = controller.start(1)
    run_id = str(started["run_id"])
    paused = DeliveryRunEngine(
        controller=controller,
        tickets=TicketDeliveryEngine(
            git=git,
            states=states,
            github=FixtureGitHubPublisher(fixture, git),
            agents=TicketRemovingAgents(fixture),
        ),
    ).deliver(run_id)
    assert paused["status"] == "structure_change_pending"
    checkout = states.root / "worktrees" / run_id / "ticket-2"
    branch = f"agent-run/{run_id}/ticket-2"
    assert (checkout / "removed.txt").is_file()

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"] = [2]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    stale_confirmation, _ = controller.confirm_structure(run_id)

    assert stale_confirmation["status"] == "structure_change_pending"
    assert (checkout / "removed.txt").is_file()
    assert git.resolve(branch)
    assert stale_confirmation.get("retired_ticket_generations", {}) == {}
    assert (
        stale_confirmation["pending_structure_change"]["graph_change_summary"][
            "added_tickets"
        ]
        == [2]
    )

    confirmed, _ = controller.confirm_structure(run_id)

    assert confirmed["status"] == "active"
    assert confirmed["active_ticket_job"]["ticket_number"] == 2
    assert confirmed["ticket_jobs"]["2"]["ticket_branch"] == branch
    assert (checkout / "removed.txt").is_file()


def test_confirmed_removal_is_retired_after_another_graph_change(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    started, _ = controller.start(1)
    run_id = str(started["run_id"])
    paused = DeliveryRunEngine(
        controller=controller,
        tickets=TicketDeliveryEngine(
            git=git,
            states=states,
            github=FixtureGitHubPublisher(fixture, git),
            agents=TicketRemovingAgents(fixture),
        ),
    ).deliver(run_id)
    assert paused["status"] == "structure_change_pending"
    checkout = states.root / "worktrees" / run_id / "ticket-2"
    branch = f"agent-run/{run_id}/ticket-2"
    assert (checkout / "removed.txt").is_file()

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"] = [3]
    data["issues"]["3"] = _ticket(3)
    fixture.write_text(json.dumps(data), encoding="utf-8")
    later_graph, _ = controller.confirm_structure(run_id)

    assert later_graph["status"] == "structure_change_pending"
    assert later_graph["pending_ticket_retirements"]["2"] == {
        "branch": branch,
        "generation": 1,
    }
    assert (checkout / "removed.txt").is_file()

    confirmed, _ = controller.confirm_structure(run_id)

    assert confirmed["status"] == "active"
    assert not checkout.exists()
    assert confirmed["retired_ticket_generations"]["2"] == 1
    assert "pending_ticket_retirements" not in confirmed
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
            cwd=git_repo,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
