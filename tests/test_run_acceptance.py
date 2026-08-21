from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, HumanBlockerResult, ReviewResult
from agent_run.agent_invocation import canonical_fingerprint
from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_currentness import ticket_completion_records

from test_cli import run_internal_stage, run_cli, stdout_json

from run_acceptance_test_support import (
    ScriptedRunAgents,
    _BLOCKED_EVIDENCE,
    _candidate_finding_artifact,
    _completed_run,
    _failed_invocation,
    _human_artifact,
    _passing_artifact,
    _repair_artifact,
)

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
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="failed-run-reviewer",
    )
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

def test_second_reviewer_attempt_keeps_the_run_acceptance_generation(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 2,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="second-run-reviewer",
    )
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]))

    assert resumed["status"] == "run_acceptance_pending"
    assert resumed["run_acceptance"]["validation_attempts"] == 2
    assert resumed["active_agent_invocation"]["generation"] == 1
    assert resumed["active_agent_invocation"]["status"] == "resuming"

@pytest.mark.parametrize(
    ("role", "work_subject"),
    [
        ("reviewer", "run-acceptance:{run_id}"),
        ("final_publication", "run-publication:{run_id}"),
    ],
)
def test_resume_rejects_stale_run_invocation_before_agent_start(
    git_repo: Path, role: str, work_subject: str
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    if role == "final_publication":
        state["run_acceptance"]["phase"] = "accepted"
        state["run_publication"] = {"phase": "publishing"}
    state["active_agent_invocation"] = _failed_invocation(
        role=role,
        phase="run_acceptance" if role == "reviewer" else "run_publication",
        work_subject=work_subject.format(run_id=state["run_id"]),
        generation=1,
        requested_thread_id="failed-thread",
        reported_thread_id="failed-thread",
        currentness_boundary={"reviewed_head_sha": "stale-head"},
    )
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]))

    assert resumed["status"] == "run_acceptance_pending"
    assert resumed["terminal_kind"] == "run_acceptance_stale"
    assert resumed["run_acceptance"]["phase"] == "pending"
    assert "reviewer_resume_thread_id" not in resumed["run_acceptance"]

def test_ticket_completion_records_are_minimal_and_sorted_numerically(
    git_repo: Path,
) -> None:
    state, _states, git = _completed_run(git_repo)
    state["ticket_graph"]["tickets"]["10"] = {
        "number": 10,
        "title": "Ticket 10",
        "body": "Deliver ticket 10.",
        "content_revision": "ticket-10-revision",
    }
    state["ticket_jobs"]["10"] = {
        "ticket_number": 10,
        "phase": "completed",
        "integrated_sha": "integrated-10",
        "effective_revision": "effective-10",
        "acceptance_record": {
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-10",
            "artifact": _passing_artifact(),
        },
    }
    state["ticket_jobs"]["2"].update(
        {
            "integrated_sha": "integrated-2",
            "effective_revision": "effective-2",
            "acceptance_record": {
                "reviewed_base_sha": git.resolve(str(state["run_branch"])),
                "reviewed_candidate_tree": "tree-2",
                "artifact": _passing_artifact(),
            },
        }
    )

    records = ticket_completion_records(state)

    assert records == [
        {
            "ticket_number": 2,
            "integrated_sha": "integrated-2",
            "effective_revision": "effective-2",
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-2",
        },
        {
            "ticket_number": 10,
            "integrated_sha": "integrated-10",
            "effective_revision": "effective-10",
            "reviewed_base_sha": git.resolve(str(state["run_branch"])),
            "reviewed_candidate_tree": "tree-10",
        },
    ]

def test_closed_ticket_edits_do_not_change_its_completion_revision(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    original_ticket = dict(state["ticket_graph"]["tickets"]["2"])
    original_completion = ticket_completion_records(state)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["2"]["title"] = "Edited after completion"
    data["issues"]["2"]["body"] = "This edit is outside the completed Ticket."
    data["parent"]["body"] = "The live Parent changed after Ticket completion."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["ticket_graph"]["tickets"]["2"] == original_ticket
    assert ticket_completion_records(refreshed) == original_completion

def test_parent_drift_immediately_stales_completed_run_acceptance(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=ScriptedRunAgents(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))
    assert accepted["run_acceptance"]["phase"] == "accepted"
    accepted["run_acceptance"].update(
        {
            "prior_human_blockers": ["An old blocker must not cross generations."],
            "human_response_history": [{"generation": 1, "response": "old"}],
            "reviewer_resume_thread_id": "old-run-reviewer",
        }
    )
    accepted["run_publication"] = {"phase": "pending"}
    states.save_run(str(state["run_id"]), accepted)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Updated after all Tickets completed."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "run_acceptance_pending"
    assert refreshed["terminal_kind"] == "run_acceptance_stale"
    assert refreshed["run_acceptance"]["phase"] == "pending"
    assert "prior_human_blockers" not in refreshed["run_acceptance"]
    assert "human_response_history" not in refreshed["run_acceptance"]
    assert "reviewer_resume_thread_id" not in refreshed["run_acceptance"]
    assert refreshed["run_publication"]["phase"] == "stale"

def test_reopened_completed_ticket_fails_closed(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["2"]["state"] = "OPEN"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )

    assert refreshed["status"] == "unsupported_scope_change"
    assert refreshed["terminal_kind"] == "unsupported_scope_change"
    assert refreshed["diagnostics"] == [
        {
            "code": "completed_ticket_reopened",
            "message": "Completed Ticket #2 was reopened outside Run Abandonment Recovery",
            "ticket_numbers": [2],
        }
    ]

def test_run_acceptance_new_thread_resume_omits_failed_reviewer_thread(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["run_acceptance"] = {
        "phase": "reviewing",
        "acceptance_generation": 1,
        "modification_attempts": 0,
        "validation_attempts": 1,
        "development_thread_id": None,
        "development_thread_history": [],
        "reviewer_thread_ids": [],
    }
    state["active_agent_invocation"] = _failed_invocation(
        role="reviewer",
        phase="run_acceptance",
        work_subject=f"run-acceptance:{state['run_id']}",
        generation=1,
        requested_thread_id=None,
        reported_thread_id="failed-run-reviewer",
    )
    state["status"] = "execution_failed"
    states.save_run(str(state["run_id"]), state)

    resumed, _ = Controller(
        FixtureGitHubReader(git_repo / "github.json"), git, states
    ).resume(str(state["run_id"]), new_thread=True)

    class NewThreadReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            assert request["thread_id"] is None
            event = request["_invocation_event"]
            event("started", requested_thread_id=None, attempt_count=0)
            event("thread_started", reported_thread_id="new-run-reviewer", attempt_count=1)
            event("completed", reported_thread_id="new-run-reviewer", attempt_count=1)
            return ReviewResult("new-run-reviewer", _passing_artifact())

    accepted = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=NewThreadReviewer(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(resumed["run_id"]))

    assert accepted["status"] == "run_publication_pending"
    assert accepted["run_acceptance"]["reviewer_thread_ids"] == [
        "new-run-reviewer"
    ]

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
            assert request["prior_human_blockers"] == [_BLOCKED_EVIDENCE]
            assert request["human_response_history"] == [
                {
                    "generation": 1,
                    "human_blockers": [
                        _BLOCKED_EVIDENCE
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
        _BLOCKED_EVIDENCE
    ]
    assert resumed["run_acceptance"]["human_response_history"] == [
        {
            "generation": 1,
            "human_blockers": [
                _BLOCKED_EVIDENCE
            ],
            "response": "Issue read access has been granted.",
        }
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
                _BLOCKED_EVIDENCE
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
        "repair_mode": "squash",
        "acceptance_artifact": _repair_artifact(),
    }
    run["repair_job"] = job

    engine._invalidate_stale_repair(
        state, job, git_repo / "unused-checkout"
    )

    assert job["phase"] == "stale"
    assert "repair_job" not in run
    assert state["status"] == "run_acceptance_pending"
    assert run["phase"] == "pending"

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

def test_run_repair_rejects_current_reviewer_thread_as_development(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    class ReusedCandidateReviewer(ScriptedRunAgents):
        def __init__(self) -> None:
            super().__init__()
            self._reviews = [_repair_artifact(), _candidate_finding_artifact()]

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if not self.development_requests:
                return super().develop(request)
            self.development_requests.append(request)
            checkout = Path(str(request["checkout"]))
            (checkout / "follow-up-repair.txt").write_text(
                "repaired reviewer finding\n", encoding="utf-8"
            )
            return DevelopmentResult(
                "run-reviewer-2", "Illegally reused the Candidate Reviewer."
            )

    with pytest.raises(ValueError, match="not independent"):
        RunAcceptanceEngine(
            git=git,
            states=states,
            agents=ReusedCandidateReviewer(),
            github=FixtureGitHubPublisher(git_repo / "github.json", git),
        ).accept(str(state["run_id"]))

def test_run_acceptance_discards_a_review_when_its_parent_snapshot_drifts(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)

    fixture = git_repo / "github.json"

    class DriftingReviewer:
        def review(self, request: dict[str, Any]) -> ReviewResult:
            data = json.loads(fixture.read_text(encoding="utf-8"))
            data["parent"]["body"] = "Changed while the Reviewer was running."
            fixture.write_text(json.dumps(data), encoding="utf-8")
            return ReviewResult("run-reviewer", _passing_artifact())

    pending = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=DriftingReviewer(),
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert pending["run_acceptance"]["phase"] == "pending"
    assert "acceptance_artifact" not in pending["run_acceptance"]
    assert "acceptance_record" not in pending["run_acceptance"]

def test_run_acceptance_refreshes_currentness_before_reusing_an_accepted_run(
    git_repo: Path,
) -> None:
    state, states, git = _completed_run(git_repo)
    initial = ScriptedRunAgents()
    initial._reviews = [_passing_artifact()]
    RunAcceptanceEngine(
        git=git,
        states=states,
        agents=initial,
        github=FixtureGitHubPublisher(git_repo / "github.json", git),
    ).accept(str(state["run_id"]))

    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] = "Changed after acceptance."
    fixture.write_text(json.dumps(data), encoding="utf-8")

    class FreshReviewer:
        def __init__(self) -> None:
            self.review_requests: list[dict[str, Any]] = []

        def review(self, request: dict[str, Any]) -> ReviewResult:
            self.review_requests.append(request)
            return ReviewResult("fresh-run-reviewer", _passing_artifact())

    refreshed = FreshReviewer()

    result = RunAcceptanceEngine(
        git=git,
        states=states,
        agents=refreshed,
        github=FixtureGitHubPublisher(fixture, git),
        currentness_reader=FixtureGitHubReader(fixture),
    ).accept(str(state["run_id"]))

    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["validation_attempts"] == 2
    assert len(refreshed.review_requests) == 1

def test_run_acceptance_fixture_repairs_malformed_output_in_same_thread(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    agents = git_repo / "repair-run-review.json"
    agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "run-reviewer-thread",
                        "thread_id": "run-reviewer-thread",
                        "artifact": _passing_artifact(),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    accepted = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert accepted.returncode == 0, accepted.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    invocation = persisted["active_agent_invocation"]
    assert invocation["status"] == "completed"
    assert invocation["reported_thread_id"] == "run-reviewer-thread"
    assert invocation["attempt_count"] == 2

def test_run_acceptance_fixture_missing_thread_marks_invocation_failed(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    agents = git_repo / "missing-run-review-thread.json"
    agents.write_text(
        json.dumps(
            {"run_reviews": [{"no_thread": True, "artifact": _passing_artifact()}]}
        ),
        encoding="utf-8",
    )

    failed = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert failed.returncode == 2
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    assert persisted["active_agent_invocation"]["status"] == "failed"

def test_run_acceptance_fixture_records_a_fresh_thread_without_expectation(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    agents = git_repo / "fresh-run-review.json"
    agents.write_text(
        json.dumps(
            {"run_reviews": [{"thread_id": "fresh-run-reviewer", "artifact": _passing_artifact()}]}
        ),
        encoding="utf-8",
    )

    accepted = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(agents),
    )

    assert accepted.returncode == 0, accepted.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    invocation = persisted["active_agent_invocation"]
    assert invocation["status"] == "completed"
    assert invocation["requested_thread_id"] is None
    assert invocation["reported_thread_id"] == "fresh-run-reviewer"

def test_run_acceptance_fixture_resume_gets_a_fresh_repair_budget(
    git_repo: Path,
) -> None:
    state, states, _git = _completed_run(git_repo)
    failed_agents = git_repo / "failed-run-review.json"
    failed_agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": {"invalid": "acceptance"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    failed = run_internal_stage(
        git_repo,
        git_repo / "github.json",
        "accept-run",
        str(state["run_id"]),
        "--agent-fixture",
        str(failed_agents),
    )
    assert failed.returncode == 2

    resumed_agents = git_repo / "resumed-run-review.json"
    resumed_agents.write_text(
        json.dumps(
            {
                "run_reviews": [
                    {
                        "expected_thread_id": "failed-run-reviewer-thread",
                        "thread_id": "failed-run-reviewer-thread",
                        "artifact": _passing_artifact(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    resumed = run_cli(
        git_repo,
        git_repo / "github.json",
        "resume",
        str(state["run_id"]),
        "--agent-fixture",
        str(resumed_agents),
    )

    assert resumed.returncode == 0, resumed.stderr
    persisted = states.load_run(str(state["run_id"]))
    assert persisted is not None
    history = persisted["agent_invocation_history"]
    assert history[-2]["status"] == "failed"
    assert history[-2]["attempt_count"] == 3
    assert history[-1]["status"] == "completed"
    assert history[-1]["attempt_count"] == 1
