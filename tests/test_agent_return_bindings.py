"""真实角色入口的原始返回须原子绑定准确 Invocation。"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
from textwrap import dedent
from typing import Any, NamedTuple

import pytest

from agent_run.agents import ReviewResult
from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.sqlite_work_evidence import get_evidence
from agent_run.state import SimulatedProcessCrash, StateStore
from support.run_artifacts import _human_artifact, _passing_artifact
from support.run_publication import InvocationRunPublicationAgents
from support.run_scenarios import _accepted_run, _completed_run
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


class _Reviewer:
    def __init__(self, artifacts: list[dict[str, Any]]) -> None:
        self.artifacts = deepcopy(artifacts)
        self.requests: list[dict[str, Any]] = []

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.requests.append(request)
        event = request["_invocation_event"]
        event("started", requested_thread_id=request.get("thread_id"), attempt_count=0)
        event("thread_started", reported_thread_id="return-reviewer", attempt_count=1)
        event("completed", reported_thread_id="return-reviewer", attempt_count=1)
        return ReviewResult("return-reviewer", self.artifacts.pop(0))


class _BoundReturn(NamedTuple):
    started_at: str
    attempt_id: str
    evidence_id: str
    result: dict[str, Any]


def _bound_returns(
    states: StateStore, run_id: str, work_subject: str, *, role: str = "reviewer",
) -> list[_BoundReturn]:
    with closing(sqlite3.connect(f"file:{states.root / 'state.sqlite3'}?mode=ro", uri=True)) as reader:
        rows = reader.execute(
            "SELECT started_at,status,attempt_id,raw_result_evidence_id FROM work_invocations "
            "WHERE run_id=? AND work_subject=? AND role=? ORDER BY started_at",
            (run_id, work_subject, role),
        ).fetchall()
        saved_raw_returns = reader.execute(
            "SELECT COUNT(*) FROM work_evidence WHERE run_id=? AND kind='returned_result'",
            (run_id,),
        ).fetchone()[0]
        assert rows and all(row[1] == "completed" and row[3] is not None for row in rows), (
            f"saved_raw_returns={saved_raw_returns}; invocations={rows}"
        )
        return [
            _BoundReturn(started, attempt_id, evidence_id,
                         get_evidence(reader, run_id, evidence_id,
                                      owner_id="attempt:" + attempt_id, kind="returned_result"))
            for started, _status, attempt_id, evidence_id in rows
        ]


def test_run_acceptance_binds_raw_return_after_completed_event(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    run_id = state["run_id"]
    agents = _Reviewer([_passing_artifact()])
    accepted = RunAcceptanceEngine(git=git, states=states, agents=agents).accept(run_id)
    assert accepted["status"] == "run_publication_pending"
    reopened = StateStore(states.root)
    saved = reopened.load_current_run(run_id)
    assert saved is not None
    assert len(agents.requests) == 1
    assert saved["run_acceptance"]["review_budget"]["reviewer_invocations"] == 1
    returned = _bound_returns(reopened, run_id, f"run-acceptance:{run_id}")
    assert len(returned) == 1
    assert returned[0].result == {"kind": "review", "thread_id": "return-reviewer", "artifact": _passing_artifact()}


def test_reopened_run_acceptance_reuses_bound_return_without_calling_or_charging_again(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git = _completed_run(git_repo)
    run_id = state["run_id"]
    agents = _Reviewer([_passing_artifact()])

    def interrupt_after_return_save() -> None:
        current = StateStore(states.root).load_current_run(run_id)
        assert current is not None
        attempt = current.get("run_acceptance", {}).get("pending_semantic_attempt", {})
        if "returned_result" in attempt:
            raise SimulatedProcessCrash("interrupted after durable Reviewer return")

    with monkeypatch.context() as interruption:
        interruption.setattr(states, "_after_run_saved", interrupt_after_return_save)
        with pytest.raises(SimulatedProcessCrash, match="durable Reviewer return"):
            RunAcceptanceEngine(git=git, states=states, agents=agents).accept(run_id)

    reopened = StateStore(states.root)
    durable = reopened.load_current_run(run_id)
    assert durable is not None
    pending = durable["run_acceptance"]["pending_semantic_attempt"]
    original = _bound_returns(reopened, run_id, f"run-acceptance:{run_id}")
    assert len(original) == 1 and original[0].attempt_id == pending["attempt_id"]
    assert original[0].result == pending["returned_result"]
    assert "pending_review_result" not in durable["run_acceptance"]
    assert "acceptance_record" not in durable["run_acceptance"]
    # Consume the saved return in a fresh interpreter with no callable Agent.
    code = dedent("""\
        import sys
        from pathlib import Path
        from agent_run.git import GitRepository
        from agent_run.run_acceptance import RunAcceptanceEngine
        from agent_run.state import StateStore

        class NoNewReview:
            def review(self, request):
                raise AssertionError('Saved Reviewer return must be reused')

        result = RunAcceptanceEngine(git=GitRepository(Path(sys.argv[1])),
            states=StateStore(Path(sys.argv[2])), agents=NoNewReview()).accept(sys.argv[3])
        assert result['status'] == 'run_publication_pending', result
    """)
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.run([sys.executable, "-c", code, str(git_repo), str(states.root), run_id],
                             env=environment, capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stdout + process.stderr
    engine = RunAcceptanceEngine(git=git, states=reopened, agents=agents)
    for _ in range(2):
        assert engine.accept(run_id)["status"] == "run_publication_pending"
    saved = StateStore(states.root).load_current_run(run_id)
    assert saved is not None
    acceptance = saved["run_acceptance"]
    assert len(agents.requests) == 1
    assert acceptance["review_budget"]["reviewer_invocations"] == 1
    assert [attempt["attempt_id"] for attempt in acceptance["semantic_attempt_history"]] == [pending["attempt_id"]]
    assert _bound_returns(reopened, run_id, f"run-acceptance:{run_id}") == original


def test_human_continuations_keep_each_run_reviewer_return_bound_to_its_invocation(git_repo: Path) -> None:
    state, states, git = _completed_run(git_repo)
    run_id = state["run_id"]
    artifacts = [_human_artifact(), _human_artifact(), _passing_artifact()]
    for index, artifact in enumerate(artifacts):
        artifact["checks"]["spec"]["evidence"] = f"Original requirement evidence {index + 1}"
    agents = _Reviewer(artifacts)
    original_bindings: list[_BoundReturn] = []
    for index in range(3):
        reopened = StateStore(states.root)
        result = RunAcceptanceEngine(git=git, states=reopened, agents=agents).accept(run_id)
        current_bindings = _bound_returns(reopened, run_id, f"run-acceptance:{run_id}")
        assert current_bindings[:index] == original_bindings
        assert len(current_bindings) == index + 1
        assert current_bindings[-1].result == {
            "kind": "review", "thread_id": "return-reviewer", "artifact": artifacts[index],
        }
        assert len({binding.attempt_id for binding in current_bindings}) == 1
        original_bindings = current_bindings
        assert len(agents.requests) == index + 1
        if index < 2:
            assert result["status"] == "ready_for_human"
            Controller(FixtureGitHubReader(git_repo / "github.json"), git, reopened).resume(
                run_id, resume_human_blocker=True, human_response=f"Reply {index + 1}",
            )
            assert _bound_returns(StateStore(states.root), run_id, f"run-acceptance:{run_id}") == original_bindings
    saved = StateStore(states.root).load_current_run(run_id)
    assert saved is not None
    assert result["status"] == "run_publication_pending"
    acceptance = saved["run_acceptance"]
    assert acceptance["review_budget"]["reviewer_invocations"] == 1
    assert len(acceptance["semantic_attempt_history"]) == 1
    assert acceptance["semantic_attempt_history"][0]["attempt_id"] == original_bindings[0].attempt_id
    assert [record["semantic_attempt"]["review_context"] for record in saved["agent_invocation_history"]] == [
        request["review_context"] for request in agents.requests
    ]
    assert _bound_returns(states, run_id, f"run-acceptance:{run_id}") == original_bindings


@pytest.mark.parametrize("role", ["development", "publication"])
def test_other_role_entries_bind_raw_return_after_completed_event(git_repo: Path, role: str) -> None:
    if role == "development":
        state, states, git, fixture = ticket_run(git_repo, (2,))
        agents = TicketAgents(git_repo, (2,), lambda _: None)
        ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(state["run_id"], 2)
        subject, invocation_role = "ticket:2", "development"
    else:
        state, states, git, publisher = _accepted_run(git_repo)
        agents = InvocationRunPublicationAgents()
        published = RunPublicationEngine(
            git=git, states=states, agents=agents, github=publisher,
            default_branch="main", default_head_sha=git.resolve("main"),
        ).publish(state["run_id"])
        assert published["status"] == "run_approval_pending"
        assert len(agents.requests) == 1
        subject, invocation_role = f"run-publication:{state['run_id']}", "final_publication"
    reopened = StateStore(states.root)
    saved = reopened.load_current_run(state["run_id"])
    assert saved is not None
    bound = _bound_returns(reopened, state["run_id"], subject, role=invocation_role)
    assert len(bound) == 1
    if role == "development":
        job = saved["ticket_jobs"]["2"]
        assert bound[0].result == {
            "kind": "development", "thread_id": "developer-2",
            "summary": job["development_summary"], "replaced_thread_id": None,
        }
        assert job["review_budget"]["development_attempts"] == 1
    else:
        publication = saved["run_publication"]
        assert bound[0].result == {
            "kind": "publication_wire",
            "artifact": {
                **{key: publication["artifact"][key] for key in ("commit_message", "pr_title", "pr_body_markdown")},
                "result_kind": "publication", "human_blockers": None,
                "_thread_id": "final-publication-thread",
            },
        }
        assert publication["publication_attempts"] == 1
