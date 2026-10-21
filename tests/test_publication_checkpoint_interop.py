"""A durable Publication checkpoint must survive a fresh engine/code reader.

The optional cross-version group needs AGENT_RUN_PUBLICATION_ORACLE_SRC pointing
at the unmodified oracle's project/src. It runs the identical checkpoints with
that reader in a separate interpreter; it never mixes loaded product modules.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

import agent_run
from agent_run.agents import PublicationResult
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import SimulatedProcessCrash, StateStore
from support.delivery_agents import PassAgents, issue
from support.run_publication import InvocationRunPublicationAgents
from support.run_scenarios import _accepted_run
from support.seeding import write_fixture


_CASES = [
    ("ticket", "raw"), ("parent", "raw"), ("final", "raw"),
    ("ticket", "commit_intent"), ("parent", "normalized"),
    ("final", "completed"),
    ("ticket", "retry"), ("parent", "retry"), ("final", "retry"),
]


class InvocationPublicationAgents(PassAgents):
    def publication(self, request: dict[str, Any]) -> PublicationResult:
        artifact = {
            "result_kind": "publication", **super().publication(request),
            "human_blockers": None,
        }
        thread = str(request.get("thread_id") or "checkpoint-publication-thread")
        event = request["_invocation_event"]
        event("started", requested_thread_id=request.get("thread_id"), attempt_count=0)
        event("thread_started", reported_thread_id=thread, attempt_count=1)
        event("completed", reported_thread_id=thread, attempt_count=1)
        return PublicationResult(thread, artifact)


class LostCreateResponse(FixtureGitHubPublisher):
    """The remote effect is real; only its first response is lost."""

    lost = False

    def ensure_change_pr(self, **authority: Any) -> int:
        result = super().ensure_change_pr(**authority)
        self._lose_once(change=True)
        return result

    def ensure_ticket_pr(self, **authority: Any) -> int:
        result = super().ensure_ticket_pr(**authority)
        self._lose_once(change=True)
        return result

    def ensure_run_pr(self, **authority: Any) -> int:
        result = super().ensure_run_pr(**authority)
        self._lose_once()
        return result

    def _lose_once(self, *, change: bool = False) -> None:
        if not self.lost:
            self.lost = True
            if change:
                raise OSError("checkpoint create response lost")
            raise GitHubReadError("github_write_failed", "checkpoint create response lost")


def _owner(state: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "ticket":
        return state["ticket_jobs"]["3"]
    return state["parent_job" if kind == "parent" else "run_publication"]


def _at_checkpoint(owner: dict[str, Any], checkpoint: str) -> bool:
    pending = owner.get("pending_semantic_attempt", {})
    if checkpoint == "raw":
        return pending.get("role") == "publication" and "returned_result" in pending
    if checkpoint == "normalized":
        return isinstance(owner.get("pending_publication_result"), dict)
    if checkpoint == "commit_intent":
        return isinstance(owner.get("publication_commit_intent"), dict)
    if checkpoint == "completed":
        return isinstance(owner.get("artifact"), dict) and "pending_semantic_attempt" not in owner
    retry = owner.get("publication_operation_retry")
    return isinstance(retry, dict) and retry.get("attempts") == 1


def _bound_returns(database: Path, run_id: str) -> list[tuple[Any, ...]]:
    # This pilot retains the existing durable relation protocol. These are
    # independent SQL observations, not a projection/private carrier oracle.
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT v.work_subject,v.started_at,v.attempt_id,v.attempt_version_id,"
            "v.raw_result_evidence_id,r.evidence_id,r.sequence "
            "FROM work_invocations v JOIN work_attempts a USING(run_id,attempt_id) "
            "JOIN work_attempt_returns r USING(run_id,attempt_id) "
            "WHERE v.run_id=? AND a.role='publication' "
            "ORDER BY v.work_subject,v.started_at,r.sequence", (run_id,),
        ).fetchall()


_CONTINUE = r'''
import json
from pathlib import Path
import sys

source, root, run_id, kind, output = sys.argv[1:]
sys.path.insert(0, source)
import agent_run
assert Path(agent_run.__file__).resolve().is_relative_to(Path(source).resolve())
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore

class NoNewAgent:
    def develop(self, request):
        raise AssertionError("checkpoint restarted Development")
    def review(self, request):
        raise AssertionError("checkpoint restarted Review")
    def publication(self, request):
        raise AssertionError("checkpoint repeated the Publication Agent")
    def run_publication(self, request):
        raise AssertionError("checkpoint repeated the Final Publication Agent")

root = Path(root)
states = StateStore(root / ".agent-run")
git = GitRepository(root)
publisher = FixtureGitHubPublisher(root / "github.json", git)
if kind == "final":
    result = RunPublicationEngine(
        git=git, states=states, agents=NoNewAgent(), github=publisher,
        default_branch="main", default_head_sha=git.resolve("main"),
    ).publish(run_id)
else:
    engine = ParentDeliveryEngine if kind == "parent" else TicketDeliveryEngine
    result = engine(git=git, states=states, agents=NoNewAgent(), github=publisher).deliver(run_id)
assert result["status"] == {
    "ticket": "ticket_completed", "parent": "parent_approval_pending",
    "final": "run_approval_pending",
}[kind], result
persisted = states.load_run(run_id)
assert persisted is not None
Path(output).write_text(json.dumps({
    "state": persisted,
    "delivery": publisher.data["delivery"],
    "source": str(Path(agent_run.__file__).resolve()),
}, ensure_ascii=False), encoding="utf-8")
'''


@pytest.mark.parametrize(("kind", "checkpoint"), _CASES,
                         ids=[f"{kind}-{point}" for kind, point in _CASES])
@pytest.mark.parametrize("reader", ["same-source", "unmodified-oracle"])
def test_publication_checkpoint_continues_without_recalling_agent(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    kind: str, checkpoint: str, reader: str,
) -> None:
    source = Path(agent_run.__file__).resolve().parent.parent
    if reader == "unmodified-oracle":
        configured = os.environ.get("AGENT_RUN_PUBLICATION_ORACLE_SRC")
        if not configured:
            pytest.skip("cross-version oracle requires AGENT_RUN_PUBLICATION_ORACLE_SRC")
        source = Path(configured).resolve()
        assert (source / "agent_run" / "state.py").is_file()

    if kind == "final":
        state, states, git, _ = _accepted_run(git_repo)
        agents = InvocationRunPublicationAgents()
    else:
        fixture = write_fixture(git_repo / "github.json", issues={} if kind == "parent" else {"3": issue(3)})
        git = GitRepository(git_repo)
        states = StateStore(git_repo / ".agent-run")
        state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
        agents = InvocationPublicationAgents(
            states.root / "worktrees" / str(state["run_id"]) / ("parent" if kind == "parent" else "ticket-3"),
        )
    run_id = str(state["run_id"])
    publisher_type = LostCreateResponse if checkpoint == "retry" else FixtureGitHubPublisher
    publisher = publisher_type(git_repo / "github.json", git)
    captured: list[dict[str, Any]] = []
    captured_bindings: list[list[tuple[Any, ...]]] = []

    def after_durable_save() -> None:
        current = states.load_run(run_id)
        assert current is not None
        if not captured and _at_checkpoint(_owner(current, kind), checkpoint):
            captured.append(deepcopy(current))
            captured_bindings.append(_bound_returns(states.root / "state.sqlite3", run_id))
            raise SimulatedProcessCrash(f"durable publication checkpoint: {checkpoint}")

    with monkeypatch.context() as cut:
        cut.setattr(states, "_after_run_saved", after_durable_save)
        with pytest.raises(SimulatedProcessCrash, match="durable publication checkpoint"):
            if kind == "final":
                RunPublicationEngine(
                    git=git, states=states, agents=agents, github=publisher,
                    default_branch="main", default_head_sha=git.resolve("main"),
                ).publish(run_id)
            else:
                engine = ParentDeliveryEngine if kind == "parent" else TicketDeliveryEngine
                engine(git=git, states=states, agents=agents, github=publisher).deliver(run_id)
    assert len(captured) == 1, "fault must hit exactly one real durable boundary"
    # Ticket lease cleanup may save again while unwinding. The cut is one-shot,
    # and the consumer must receive the final durable checkpoint, not a captured
    # in-memory proposal from before that cleanup.
    before = states.load_run(run_id)
    assert before is not None
    cut_owner, durable_owner = _owner(captured[0], kind), _owner(before, kind)
    for field in (
        "phase", "pending_semantic_attempt", "pending_publication_result", "artifact",
        "publication_commit_intent", "publication_attempts", "candidate_sha",
        "review_budget", "publication_operation_retry", "write_intent", "ticket_write_intent",
    ):
        assert (field in durable_owner, durable_owner.get(field)) == (field in cut_owner, cut_owner.get(field))
    original = _owner(before, kind)
    assert original["publication_attempts"] == 1
    assert len(agents.requests if kind == "final" else agents.publication_requests) == 1
    bindings = _bound_returns(states.root / "state.sqlite3", run_id)
    assert bindings == captured_bindings[0]
    assert len(bindings) == 1
    assert bindings[0][4] == bindings[0][5] and bindings[0][4]

    # Preserve an immutable checkpoint and restore a separate continuation copy
    # at the same paths: native Git worktree metadata contains absolute paths.
    # No producer is running and all production transaction/read scopes exited.
    snapshot = tmp_path / "publication-checkpoint"
    shutil.copytree(git_repo, snapshot)
    with closing(sqlite3.connect(states.root / "state.sqlite3")) as incoming:
        with closing(sqlite3.connect(snapshot / ".agent-run" / "state.sqlite3")) as saved:
            incoming.backup(saved)
    shutil.rmtree(git_repo)
    shutil.copytree(snapshot, git_repo)
    output = tmp_path / "continued.json"
    result = subprocess.run(
        [sys.executable, "-c", _CONTINUE, str(source), str(git_repo), run_id, kind, str(output)],
        cwd=tmp_path, env=dict(os.environ), text=True, capture_output=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    continued = json.loads(output.read_text(encoding="utf-8"))
    assert Path(continued["source"]).is_relative_to(source)
    after = continued["state"]
    owner = _owner(after, kind)
    assert owner["publication_attempts"] == 1
    assert "pending_semantic_attempt" not in owner
    closed = [item for item in owner["semantic_attempt_history"] if item["role"] == "publication"]
    assert len(closed) == 1
    prior = original.get("pending_semantic_attempt") or next(
        item for item in original["semantic_attempt_history"] if item["role"] == "publication"
    )
    assert closed[0]["attempt_id"] == prior["attempt_id"]
    assert closed[0]["currentness_boundary_fingerprint"] == prior["currentness_boundary_fingerprint"]
    assert closed[0]["outcome"] == "publication_artifact"
    assert _bound_returns(states.root / "state.sqlite3", run_id) == bindings
    assert len(continued["delivery"]["pull_requests"]) == 1
    assert "write_intent" not in owner
    assert "ticket_write_intent" not in owner
    assert "publication_commit_intent" not in owner
    if kind == "final":
        assert after["run_acceptance"] == before["run_acceptance"]
        assert after["ticket_jobs"] == before["ticket_jobs"]
        assert owner["phase"] == "ready_for_approval"
        assert "approval_grant" not in owner
    else:
        assert owner["candidate_sha"] == original["candidate_sha"]
        assert owner["acceptance_record"] == original["acceptance_record"]
        assert owner["review_budget"] == original["review_budget"]
    if checkpoint == "normalized":
        assert owner["publication"] == original["pending_publication_result"]
    if checkpoint == "commit_intent":
        assert owner["publication_sha"] == original["publication_commit_intent"]["expected_sha"]
    if checkpoint == "retry":
        assert publisher.lost
        assert original["publication_operation_retry"] == {"attempts": 1, "limit": 5}
        if kind == "final":
            assert owner["publication_operation_retry"] == original["publication_operation_retry"]
        else:
            # Change Required Checks deliberately clear the live read retry;
            # the completed Publication Attempt retains the prior failure.
            assert "publication_operation_retry" not in owner
        assert closed[0]["publication_operation_retry"] == original["publication_operation_retry"]
        assert len(publisher.data["delivery"]["pull_requests"]) == 1
