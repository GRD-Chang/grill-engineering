"""真实 Git/StateStore 下证据复用与事实漂移的交付边界。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

from agent_run.agents import ReviewResult
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_publication import RunPublicationEngine
from agent_run.state import StateStore
from support.run_artifacts import _passing_artifact
from support.run_publication import RunPublicationAgents
from support.run_scenarios import _accepted_run, _sync_completed_ticket_integrated_sha


class Reviewer:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def review(self, request: dict) -> ReviewResult:
        self.requests.append(request)
        return ReviewResult("current-reviewer", _passing_artifact())


@pytest.mark.parametrize("drift", ["none", "requirements", "graph", "completion", "baseline", "merge_result"])
def test_final_delivery_reuses_only_applicable_complete_review(git_repo: Path, drift: str) -> None:
    state, states, git, publisher = _accepted_run(git_repo)
    run_id = str(state["run_id"])
    original = deepcopy(state["run_acceptance"]["acceptance_record"])
    head = git.resolve(str(state["run_branch"]))
    tree = git.resolve(f"{head}^{{tree}}")
    default = git.resolve("main")
    if drift == "requirements":
        state["parent"]["revision"] = "new-parent-requirements"
    elif drift == "graph":
        state["ticket_graph"]["revision"] = "new-ticket-graph"
    elif drift == "completion":
        completed = subprocess.run(
            ["git", "commit-tree", tree, "-p", default, "-m", "same-tree completion"],
            cwd=git_repo, text=True, capture_output=True, check=True,
        ).stdout.strip()
        _sync_completed_ticket_integrated_sha(state, git, completed)
    elif drift == "baseline":
        default = subprocess.run(
            ["git", "commit-tree", git.resolve(f"{default}^{{tree}}"), "-p", default, "-m", "same-tree default"],
            cwd=git_repo, text=True, capture_output=True, check=True,
        ).stdout.strip()
        assert git.resolve(f"{default}^{{tree}}") == git.resolve("main^{tree}")
    elif drift == "merge_result":
        state["run_acceptance"]["acceptance_record"]["expected_merge_tree"] = "different-result"
    states.save_run(run_id, state)
    reloaded_states = StateStore(states.root)
    agents = RunPublicationAgents()
    result = RunPublicationEngine(
        git=git, states=reloaded_states, agents=agents, github=publisher,
        default_branch="main", default_head_sha=default,
    ).publish(run_id)
    assert git.resolve(f"{head}^{{tree}}") == tree
    if drift == "none":
        assert result["status"] == "run_approval_pending"
        assert len(agents.requests) == 1
        reviewer = Reviewer()
        reused = RunAcceptanceEngine(
            git=git, states=reloaded_states, agents=reviewer, github=publisher,
        ).accept(run_id)
        assert reused["run_acceptance"]["acceptance_record"] == original
        assert reviewer.requests == []
    else:
        assert result["status"] == "run_acceptance_pending"
        assert agents.requests == []
        assert publisher.data["delivery"].get("pull_requests", []) == []
        assert result["run_acceptance"]["phase"] == "pending"
    assert result["run_acceptance"]["semantic_attempt_history"] == state["run_acceptance"]["semantic_attempt_history"]


def test_candidate_promotion_proves_actual_result_and_preserves_original_review(git_repo: Path) -> None:
    from agent_run.run_candidate_acceptance import CandidateRunAcceptance
    from agent_run.run_currentness import ticket_completion_records

    state, _states, git, _publisher = _accepted_run(git_repo)
    base = git.resolve("main")
    candidate = git.resolve(str(state["run_branch"]))
    tree = git.resolve(f"{candidate}^{{tree}}")
    job = {
        "base_sha": base, "default_base_sha": base, "candidate_sha": candidate,
        "parent_revision": state["parent"]["revision"],
        "ticket_graph_revision": state["ticket_graph"]["revision"],
        "ticket_completion_records": ticket_completion_records(state),
        "repair_source": "acceptance", "repair_mode": "squash",
    }
    evidence = CandidateRunAcceptance(git, default_head_sha=base)
    job["acceptance_record"] = evidence.record(job, "candidate-reviewer", _passing_artifact())
    original = deepcopy(job["acceptance_record"])
    integrated = subprocess.run(
        ["git", "commit-tree", tree, "-p", base, "-m", "actual integrated result"],
        cwd=git_repo, text=True, capture_output=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "update-ref", f"refs/heads/{state['run_branch']}", integrated], cwd=git_repo, check=True)
    promoted = evidence.promotion_record(state, job, integrated)
    assert promoted is not None
    assert promoted["reviewed_head_sha"] == integrated
    assert promoted["reviewed_candidate_sha"] == candidate
    for field in ("reviewed_base_sha", "reviewed_default_base_sha", "expected_merge_tree",
                  "parent_revision", "ticket_graph_revision", "ticket_completion_records"):
        job["acceptance_record"] = {**original, field: [] if field == "ticket_completion_records" else "changed"}
        assert evidence.promotion_record(state, job, integrated) is None
    job["acceptance_record"] = original
    # Identical tree on a commit that is not the actual Run result cannot be promoted.
    assert evidence.promotion_record(state, job, candidate) is None
    assert job["acceptance_record"] == original
    assert job["candidate_acceptance_history"][0]["outcome"] == "accepted"
