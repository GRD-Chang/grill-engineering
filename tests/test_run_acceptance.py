from __future__ import annotations

import subprocess
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, HumanBlockerResult, ReviewResult
from agent_run.agent_invocation import canonical_fingerprint
from agent_run.controller import Controller
from agent_run.git import GitError, GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.state import StateStore

from conftest import write_fixture
from test_cli import run_cli, stdout_json


def _passing_artifact() -> dict[str, object]:
    return {
        "verdict": "pass",
        "checks": {
            name: {"status": "pass", "evidence": f"{name} passed."}
            for name in ("e2e", "standards", "spec")
        },
        "findings": [],
        "human_blockers": [],
    }


def _repair_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "fail",
        "evidence": "The accumulated flow loses the first Ticket behavior.",
    }
    artifact["verdict"] = "request_changes"
    artifact["findings"] = [
        {
            "id": "RUN-1",
            "problem": "The integrated flow is incomplete.",
            "evidence": "The end-to-end scenario fails after both Tickets combine.",
            "required_outcome": "Restore the complete combined flow.",
            "verification": "Run the full accumulated scenario.",
        }
    ]
    return artifact


def _human_artifact() -> dict[str, object]:
    artifact = _passing_artifact()
    checks = artifact["checks"]
    assert isinstance(checks, dict)
    checks["e2e"] = {
        "status": "blocked",
        "evidence": "GitHub denied access to the Parent Issue.",
    }
    artifact["verdict"] = "human"
    artifact["human_blockers"] = [
        "GitHub denied access; tried gh issue view; grant Issue read access."
    ]
    return artifact


class ScriptedRunAgents:
    def __init__(self) -> None:
        self.development_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []
        # The repair itself needs a shared-engine Fresh Validation before it
        # can merge, then the whole Run needs a different Fresh Validation.
        self._reviews = [
            _repair_artifact(),
            _passing_artifact(),
            _passing_artifact(),
        ]

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        checkout = Path(str(request["checkout"]))
        (checkout / "run-repair.txt").write_text("repaired\n", encoding="utf-8")
        return DevelopmentResult(
            thread_id="run-repair-developer",
            summary="Repaired the accumulated behavior.",
        )

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        checkout = Path(str(request["checkout"]))
        assert subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip() == request["base_sha"]
        if request.get("repair_scope") == "run_repair":
            assert (checkout / "run-repair.txt").read_text(encoding="utf-8") == "repaired\n"
            assert request["expected_merge_result"]["checkout_state"].startswith(
                "Run Branch plus repair publication"
            )
        return ReviewResult(
            thread_id=f"run-reviewer-{len(self.review_requests)}",
            artifact=self._reviews.pop(0),
        )

    def publication(self, request: dict[str, Any]) -> dict[str, str]:
        return {
            "commit_message": "fix(run): repair accumulated delivery behavior",
            "pr_title": "fix(run): repair accumulated delivery behavior",
            "pr_body_markdown": (
                "## What Problem This Solves\n\nThe combined Run failed.\n\n"
                "## Why This Change Was Made\n\nThe repair restores the combined behavior.\n\n"
                "## User Impact\n\nThe complete delivery works together.\n\n"
                "## Evidence\n\nThe fresh Run validation will recheck it."
            ),
        }


def _completed_run(git_repo: Path) -> tuple[dict[str, Any], StateStore, GitRepository]:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": {
                "number": 2,
                "title": "Ticket 2",
                "body": "Deliver ticket 2.",
                "state": "CLOSED",
                "labels": [],
                "blocked_by": [],
            }
        },
    )
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    state["active_ticket_job"] = None
    state["frontier"] = []
    state["status"] = "run_acceptance_pending"
    state["terminal_kind"] = "all_tickets_completed"
    state["ticket_jobs"] = {
        "2": {
            "ticket_number": 2,
            "phase": "completed",
            "development_thread_id": "ticket-developer",
            "development_thread_history": [],
            "reviewer_thread_ids": ["ticket-reviewer"],
            "modification_attempts": 1,
            "acceptance_record": {"artifact": _passing_artifact()},
            "integrated_sha": git.resolve(str(state["run_branch"])),
        }
    }
    states.save_run(str(state["run_id"]), state)
    return state, states, git


def test_run_acceptance_repairs_then_rechecks_the_whole_run(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    agents = ScriptedRunAgents()

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(
        str(state["run_id"])
    )

    assert result["status"] == "run_publication_pending"
    run = result["run_acceptance"]
    assert run["phase"] == "accepted"
    assert run["modification_attempts"] == 1
    assert run["reviewer_thread_ids"] == [
        "run-reviewer-1",
        "run-reviewer-2",
        "run-reviewer-3",
    ]


    assert run["reviewed_head_sha"] == git.resolve(str(state["run_branch"]))
    assert result["ticket_jobs"]["2"]["modification_attempts"] == 1
    assert len(agents.development_requests) == 1
    assert len(agents.review_requests) == 3
    fixture_data = json.loads((git_repo / "github.json").read_text(encoding="utf-8"))
    repair_prs = fixture_data["delivery"]["pull_requests"]
    assert len(repair_prs) == 1
    assert repair_prs[0]["scope"] == "run_repair"
    assert repair_prs[0]["base_branch"] == state["run_branch"]
    assert repair_prs[0]["body"].startswith(
        "Parent Issue: #1\nDelivery Type: Run Repair\n\n"
    )
    assert fixture_data["delivery"]["acceptance_records"] == []
    repair_statuses = fixture_data["delivery"]["agent_run_status"]
    assert repair_statuses[0]["scope"] == "run-repair-1"
    assert repair_statuses[0]["candidate_sha"] == run["candidate_sha"]
    assert fixture_data["delivery"]["closed_issues"] == []
    assert not (
        states.root / "worktrees" / str(state["run_id"]) / "run-repair"
    ).exists()
    repair_branch = run["completed_repair_jobs"][0]["repair_branch"]
    assert repair_branch not in fixture_data["delivery"]["published_branches"]
    with pytest.raises(GitError):
        git.resolve(repair_branch)


def test_run_acceptance_invocation_binds_the_reviewed_run_identity(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class InvocationReviewer:
        def __init__(self) -> None:
            self.request: dict[str, Any] | None = None

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.request = request
            event = request["_invocation_event"]
            event("started", requested_thread_id=None, attempt_count=0)
            event("thread_started", reported_thread_id="run-reviewer", attempt_count=1)
            event("completed", reported_thread_id="run-reviewer", attempt_count=1)
            return ReviewResult("run-reviewer", _passing_artifact())

    agents = InvocationReviewer()
    completed = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))

    assert agents.request is not None
    invocation = completed["active_agent_invocation"]
    acceptance = completed["run_acceptance"]["acceptance_record"]
    assert invocation["status"] == "completed"
    assert invocation["role"] == "reviewer"
    assert invocation["phase"] == "run_acceptance"
    assert invocation["work_subject"] == f"run-acceptance:{state['run_id']}"
    assert invocation["generation"] == 1
    assert invocation["input_fingerprint"] == canonical_fingerprint(agents.request)
    assert invocation["currentness_boundary"] == {
        "reviewed_head_sha": acceptance["reviewed_head_sha"],
        "reviewed_default_base_sha": acceptance["reviewed_default_base_sha"],
        "expected_merge_tree": acceptance["expected_merge_tree"],
        "parent_revision": acceptance["parent_revision"],
        "ticket_graph_revision": acceptance["ticket_graph_revision"],
        "ticket_completion_records_fingerprint": canonical_fingerprint(
            acceptance["ticket_completion_records"]
        ),
    }


@pytest.mark.parametrize("new_thread", [False, True])
def test_run_acceptance_execution_failure_resumes_selected_thread(
    git_repo: Path, new_thread: bool
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = {
        "role": "reviewer",
        "phase": "run_acceptance",
        "work_subject": f"run-acceptance:{state['run_id']}",
        "generation": 1,
        "status": "failed",
        "requested_thread_id": None,
        "reported_thread_id": "failed-run-reviewer",
    }
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), new_thread=new_thread)

    run = resumed["run_acceptance"]
    assert resumed["status"] == "run_acceptance_pending"
    assert run["phase"] == "pending"
    if new_thread:
        assert run["reviewer_new_thread"] is True
        assert "reviewer_resume_thread_id" not in run
    else:
        assert run["reviewer_resume_thread_id"] == "failed-run-reviewer"


def test_run_repair_development_human_blocker_stops_before_candidate_or_pr(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class BlockedRunRepairDevelopment(ScriptedRunAgents):
        def develop(
            self, request: dict[str, Any]
        ) -> DevelopmentResult | HumanBlockerResult:
            self.development_requests.append(request)
            return HumanBlockerResult(
                thread_id="run-repair-development-blocked",
                human_blockers=(
                    "GitHub denied access; tried gh issue view; grant Issue read access.",
                ),
            )

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairDevelopment(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["human_blocker_phase"] == "developing"
    assert repair["development_thread_id"] == "run-repair-development-blocked"
    assert "candidate_sha" not in repair
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["pull_requests"] == []
    status = stdout_json(
        run_cli(git_repo, fixture, "status", str(state["run_id"]), "--json")
    )
    assert status["status"] == "ready_for_human"
    assert status["diagnostics"][0]["message"].startswith("GitHub denied access")
    history = stdout_json(
        run_cli(git_repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("thread_id") == "run-repair-development-blocked"
        and event.get("worker") == "开发工作代理"
        and event.get("human_blockers")
        for event in history["timeline"]
    )


def test_run_repair_reviewer_human_blocker_history_uses_reviewer_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class BlockedRunRepairReviewer(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [_repair_artifact(), _human_artifact()]

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairReviewer(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["blocked_reason"] == "reviewer_requires_human"
    assert repair["reviewer_thread_ids"][-1] == "run-reviewer-2"
    history = stdout_json(
        run_cli(git_repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("worker") == "独立验收工作代理"
        and event.get("thread_id") == "run-reviewer-2"
        and event.get("human_blockers")
        for event in history["timeline"]
    )


def test_run_repair_publication_human_blocker_stops_before_pr_mutation(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"

    class BlockedRunRepairPublication(ScriptedRunAgents):
        def publication(
            self, request: dict[str, Any]
        ) -> HumanBlockerResult:
            del request
            return HumanBlockerResult(
                thread_id="run-repair-publication-blocked",
                human_blockers=(
                    "GitHub denied access; tried gh issue view; grant Issue read access.",
                ),
            )

    blocked = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=BlockedRunRepairPublication(),
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(str(state["run_id"]))

    assert blocked["status"] == "ready_for_human"
    repair = blocked["run_acceptance"]["repair_job"]
    assert repair["phase"] == "blocked"
    assert repair["human_blocker_phase"] == "accepted"
    assert repair["publication_thread_id"] == "run-repair-publication-blocked"
    assert repair["publication_attempts"] == 1
    assert "publication_sha" not in repair
    assert json.loads(fixture.read_text(encoding="utf-8"))["delivery"][
        "pull_requests"
    ] == []
    history = stdout_json(
        run_cli(git_repo, fixture, "history", str(state["run_id"]), "--json")
    )
    assert any(
        event.get("worker") == "发布工作代理"
        and event.get("attempt") == 1
        and event.get("thread_id") == "run-repair-publication-blocked"
        and event.get("human_blockers")
        for event in history["timeline"]
    )


def test_run_acceptance_rejects_ticket_or_previous_reviewer_identity(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            return ReviewResult("ticket-reviewer", _passing_artifact())

    with pytest.raises(ValueError, match="new Reviewer Thread"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedReviewer(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))


def test_run_acceptance_human_resume_reuses_thread_and_clears_current_blocker(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class HumanThenPassingReviewer:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []
            self.checkouts: list[Path] = []

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.requests.append(request)
            self.checkouts.append(Path(str(request["checkout"])))
            if len(self.requests) == 1:
                return ReviewResult("blocked-run-reviewer", _human_artifact())
            assert request["thread_id"] == "blocked-run-reviewer"
            assert request["prior_human_blockers"] == [
                "GitHub denied access; tried gh issue view; grant Issue read access."
            ]
            assert request["human_response_history"] == [
                {
                    "generation": 1,
                    "human_blockers": [
                        "GitHub denied access; tried gh issue view; grant Issue read access."
                    ],
                    "response": "Issue read access has been granted.",
                }
            ]
            return ReviewResult("blocked-run-reviewer", _passing_artifact())

    agents = HumanThenPassingReviewer()
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )

    blocked = engine.accept(str(state["run_id"]))
    assert blocked["status"] == "ready_for_human"
    assert all(not checkout.exists() for checkout in agents.checkouts)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(
        str(state["run_id"]),
        resume_human_blocker=True,
        human_response="Issue read access has been granted.",
    )
    assert resumed["run_acceptance"]["prior_human_blockers"] == [
        "GitHub denied access; tried gh issue view; grant Issue read access."
    ]
    assert resumed["run_acceptance"]["human_responses"] == [
        "Issue read access has been granted."
    ]

    accepted = engine.accept(str(state["run_id"]))

    assert accepted["status"] == "run_publication_pending"
    run = accepted["run_acceptance"]
    assert run["reviewer_thread_ids"] == ["blocked-run-reviewer"]
    assert len(set(agents.checkouts)) == 2
    assert all(not checkout.exists() for checkout in agents.checkouts)
    assert run["human_blocker_history"] == [
        {
            "phase": "pending",
            "human_blockers": [
                "GitHub denied access; tried gh issue view; grant Issue read access."
            ],
        }
    ]
    for key in ("human_blockers", "human_blocker_phase", "prior_human_blockers"):
        assert key not in run


def test_run_acceptance_human_resume_rejects_an_older_reviewer_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class HumanThenHistoricalReviewer:
        def __init__(self) -> None:
            self.calls = 0

        def review(self, request: dict[str, Any]) -> ReviewResult:
            del request
            self.calls += 1
            if self.calls == 1:
                return ReviewResult("blocked-latest-reviewer", _human_artifact())
            return ReviewResult("older-reviewer", _passing_artifact())

    agents = HumanThenHistoricalReviewer()
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    blocked = engine.accept(str(state["run_id"]))
    run = blocked["run_acceptance"]
    run["reviewer_thread_ids"].insert(0, "older-reviewer")
    states.save_run(str(state["run_id"]), blocked)
    Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), resume_human_blocker=True)

    with pytest.raises(
        ValueError, match="Human Blocker resume requires the latest Reviewer Thread"
    ):
        engine.accept(str(state["run_id"]))


def test_stale_run_repair_publication_returns_to_fresh_run_acceptance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    engine = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    )
    run = state["run_acceptance"] = {
        "phase": "repairing",
        "modification_attempts": 0,
        "validation_attempts": 0,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
        "repair_generation": 1,
        "acceptance_artifact": _repair_artifact(),
        "acceptance_record": {"reviewed_head_sha": git.resolve(state["run_branch"])},
    }
    job: dict[str, Any] = {
        "phase": "publication_pending",
        "repair_source": "acceptance",
        "acceptance_artifact": _repair_artifact(),
    }
    run["repair_job"] = job

    engine._invalidate_stale_repair_publication(
        state, job, git_repo / "unused-checkout"
    )

    assert job["phase"] == "stale"
    assert "repair_job" not in run
    assert state["status"] == "run_acceptance_pending"
    assert run["phase"] == "repairing"
    replacement = engine._repair_job(state, run)
    assert replacement["repair_generation"] == 2
    assert replacement["repair_branch"].endswith("/1-generation-2")


def test_run_acceptance_rejects_run_repair_developer_as_a_reviewer(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedRunDeveloper(ScriptedRunAgents):
        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            artifact = self._reviews.pop(0)
            thread_id = (
                "run-reviewer-1"
                if len(self.review_requests) == 1
                else "run-repair-developer"
            )
            return ReviewResult(thread_id, artifact)

    with pytest.raises(
        ValueError, match="Fresh Acceptance cannot reuse the Development Thread"
    ):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedRunDeveloper(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))


def test_run_repair_rejects_ticket_development_thread_reuse(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedTicketDeveloper(ScriptedRunAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            result = super().develop(request)
            return DevelopmentResult("ticket-developer", result.summary)

    with pytest.raises(ValueError, match="not independent"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedTicketDeveloper(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))


def test_accept_run_cli_enters_publication_pending_after_fresh_run_review(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": {
                "number": 2,
                "title": "Ticket 2",
                "body": "Deliver ticket 2.",
                "state": "OPEN",
                "labels": ["ready-for-agent"],
                "blocked_by": [],
            }
        },
    )
    ticket_agents = git_repo / "ticket-agents.json"
    ticket_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "ticket-developer",
                        "summary": "Delivered the ticket.",
                        "write_files": {"ticket.txt": "done\n"},
                    }
                ],
                "publications": [
                    {
                        "commit_message": "feat(run): deliver the ticket outcome",
                        "pr_title": "feat(run): deliver the ticket outcome",
                        "pr_body_markdown": (
                                "## What Problem This Solves\n\nThe Ticket was pending.\n\n"
                            "## Why This Change Was Made\n\nIt completes the requested path.\n\n"
                            "## User Impact\n\nThe path is available.\n\n"
                            "## Evidence\n\nThe fixture flow passed."
                        ),
                    }
                ],
                "reviews": [{"thread_id": "ticket-reviewer", "artifact": _passing_artifact()}],
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
            {"reviews": [{"thread_id": "run-reviewer", "artifact": _passing_artifact()}]},
        ),
        encoding="utf-8",
    )

    accepted = run_cli(
        git_repo, fixture, "accept-run", run_id, "--agent-fixture", str(run_agents)
    )

    assert accepted.returncode == 0, accepted.stderr
    assert stdout_json(accepted)["status"] == "run_publication_pending"


def test_accept_run_does_not_review_after_a_github_refresh_failure(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["error"] = {"code": "github_read_failed", "message": "offline"}
    fixture.write_text(json.dumps(data), encoding="utf-8")
    agents = git_repo / "run-agents.json"
    agents.write_text(json.dumps({"reviews": []}), encoding="utf-8")

    result = run_cli(
        git_repo,
        fixture,
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert result.returncode == 2
    assert stdout_json(result)["status"] == "execution_failed"
    assert states.load_run(str(state["run_id"])) is not None


def test_interrupted_run_review_restarts_with_a_fresh_attempt(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    states.save_run(str(state["run_id"]), state)
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(
        str(state["run_id"])
    )

    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["validation_attempts"] == 2


def test_default_branch_drift_invalidates_run_acceptance_and_rechecks_merge_preview(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    first = ScriptedRunAgents()
    first._reviews = [_passing_artifact()]
    RunAcceptanceEngine(
        git=git,
        states=states,
        agents=first,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))
    (git_repo / "default-branch.txt").write_text("new default\n", encoding="utf-8")
    subprocess.run(["git", "add", "default-branch.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance default branch"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    second = ScriptedRunAgents()
    second._reviews = [_passing_artifact()]
    second.review_requests = [{}]

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=second,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["validation_attempts"] == 2
    assert result["run_acceptance"]["acceptance_record"]["reviewed_default_base_sha"] == git.resolve("main")


def test_run_acceptance_binds_the_previewed_merge_tree(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    agents = ScriptedRunAgents()
    agents._reviews = [_passing_artifact()]

    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=agents,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        default_head_sha=git.resolve("main"),
    ).accept(str(state["run_id"]))

    record = accepted["run_acceptance"]["acceptance_record"]
    assert record["expected_merge_tree"] == git.expected_merge_tree(
        default_head_sha=git.resolve("main"),
        run_head_sha=git.resolve(str(state["run_branch"])),
    )
