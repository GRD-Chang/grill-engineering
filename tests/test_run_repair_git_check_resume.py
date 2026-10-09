"""Run Repair Git failures retain a loadable, exact resumable Invocation."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, HumanBlockerResult
from agent_run.controller import Controller
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.run_repair_cycle import repair_checkout_is_active
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from agent_run.state_errors import MissingRunMaterialsError
from support.run_agents import ScriptedRunAgents
from support.run_artifacts import _passing_artifact
from support.run_scenarios import _completed_run


@pytest.mark.parametrize(
    ("phase", "scene", "reason", "expected"),
    [
        ("developing", None, "agent_requires_human", True),
        ("repairing", None, "agent_requires_human", True),
        ("accepted", {"stage": "source"}, "agent_requires_human", True),
        ("accepted", {"stage": "target"}, "agent_requires_human", True),
        ("accepted", {"stage": "git_check"}, "agent_requires_human", True),
        ("accepted", None, "agent_requires_human", False),
        ("accepted", {"stage": "git_check"}, "reviewer_requires_human", False),
    ],
)
def test_repair_checkout_retains_unfinished_integration_human_work(
    phase: str, scene: dict[str, str] | None, reason: str, expected: bool,
) -> None:
    job: dict[str, Any] = {
        "phase": "blocked", "blocked_reason": reason, "human_blocker_phase": phase,
    }
    if scene is not None:
        job["run_repair_integration_conflict"] = scene
    assert repair_checkout_is_active(job) is expected


def test_run_repair_hook_failure_reloads_and_resumes_same_work(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git = _completed_run(
        git_repo, delivery_policy=DeliveryPolicy(run_repair_rounds=3)
    )
    run_id = str(state["run_id"])
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    hook = git_repo / ".git" / "hooks" / "pre-merge-commit"

    class HookAgents(ScriptedRunAgents):
        active_snapshot: dict[str, Any]

        def develop(self, request: dict[str, Any]) -> DevelopmentResult | HumanBlockerResult:
            if request.get("repair_source") != "integration":
                return super().develop(request)
            self.development_requests.append(request)
            event = request["_invocation_event"]
            event("started", requested_thread_id="run-repair-developer", attempt_count=1)
            event("thread_started", reported_thread_id="run-repair-developer")
            loaded = StateStore(states.root).load_current_run(run_id)
            assert loaded is not None
            scene = loaded["run_acceptance"]["repair_job"]["run_repair_integration_conflict"]
            assert scene["stage"] == "git_check"
            assert "pre-merge-commit" in scene["failure"]["error"]
            assert scene["failure"]["returncode"] != 0
            assert scene["failure"]["command"][:2] == ["git", "merge"]
            assert scene["candidate_sha"] == loaded["run_acceptance"]["repair_job"]["candidate_sha"]
            if len(self.development_requests) == 2:
                self.active_snapshot = deepcopy(loaded)
                (Path(request["checkout"]) / "preserved-work.txt").write_text(
                    "work before human response\n", encoding="utf-8"
                )
                event("completed")
                return HumanBlockerResult("run-repair-developer", ("Confirm hook fix",))
            assert request["prior_human_blockers"] == ["Confirm hook fix"]
            assert request["human_response_history"][-1]["response"] == "Remove failing hook"
            assert (Path(request["checkout"]) / "preserved-work.txt").read_text(
                encoding="utf-8"
            ) == "work before human response\n"
            hook.unlink()
            event("completed")
            return DevelopmentResult("run-repair-developer", "Fixed integration hook")

    agents = HookAgents()
    first = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(run_id)
    job = first["run_acceptance"]["repair_job"]
    assert job["phase"] == "waiting_checks"
    original_pr = job["pr_number"]

    def command(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=git_repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

    target_checkout = tmp_path / "target-append"
    git.prepare_validation_checkout(head_sha=str(job["base_sha"]), checkout=target_checkout)
    try:
        (target_checkout / "target-append.txt").write_text("target contribution\n", encoding="utf-8")
        target = git.commit_candidate(
            target_checkout, ticket_number=3, attempt=1, expected_head=str(job["base_sha"]),
        )
    finally:
        git.remove_worktree(target_checkout)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["delivery"]["published_branches"][str(state["run_branch"])] = target
    data["delivery"]["pull_requests"][0]["base_sha"] = target
    data["delivery"]["required_checks"] = ["none"]
    data["delivery"]["check_position"] = 0
    fixture.write_text(json.dumps(data), encoding="utf-8")
    hook.write_text("#!/bin/sh\necho pre-merge-commit rejected >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o700)
    agents._reviews = [_passing_artifact()]
    paused = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(run_id)
    assert "repair_job" in paused["run_acceptance"], paused.get("diagnostics")
    paused_job = paused["run_acceptance"]["repair_job"]
    assert paused_job["blocked_reason"] == "agent_requires_human"
    attempt = deepcopy(paused_job["pending_semantic_attempt"])
    budget = deepcopy(paused["run_acceptance"]["review_budget"])
    assert budget["development_attempts"] == 2

    # Exercise rejection of damaged identities on the actual production
    # thread_started snapshot, rather than fabricating a legal Job by hand.
    for damage in ("candidate", "boundary", "generation", "failure", "head", "merge_head", "attempts"):
        invalid = deepcopy(agents.active_snapshot)
        invalid_job = invalid["run_acceptance"]["repair_job"]
        scene = invalid_job["run_repair_integration_conflict"]
        if damage == "candidate":
            scene["candidate_sha"] = target
        elif damage == "boundary":
            invalid["active_agent_invocation"]["currentness_boundary"]["base_sha"] = target
        elif damage == "generation":
            invalid["active_agent_invocation"]["generation"] += 1
        elif damage == "failure":
            scene["failure"] = {}
        elif damage == "head":
            scene["head_sha"] = ""
        elif damage == "merge_head":
            scene["merge_head_sha"] = 42
        else:
            scene["attempts"] = 0
        with pytest.raises(MissingRunMaterialsError):
            require_current_run_state(invalid)

    resumed, resumed_existing = Controller(
        FixtureGitHubReader(fixture), git, states,
    ).resume(
        run_id, explicit_resume=True, resume_human_blocker=True,
        human_response="Remove failing hook",
    )
    assert resumed_existing
    assert resumed["status"] == "run_acceptance_pending", resumed.get("diagnostics")
    resumed_job = resumed["run_acceptance"]["repair_job"]
    assert resumed_job["candidate_sha"] == paused_job["candidate_sha"]
    assert resumed_job["pending_semantic_attempt"]["attempt_id"] == attempt["attempt_id"]
    assert resumed["run_acceptance"]["review_budget"] == budget
    result = RunAcceptanceEngine(
        git=git, states=states, agents=agents,
        github=FixtureGitHubPublisher(fixture, git),
    ).accept(run_id)
    assert result["status"] == "run_publication_pending", result.get("diagnostics")
    loaded = StateStore(states.root).load_current_run(run_id)
    assert loaded is not None
    assert loaded["run_acceptance"]["repair_pr_number"] == original_pr
    assert loaded["run_acceptance"]["review_budget"]["development_attempts"] == 2
    assert any(item["semantic_attempt"]["attempt_id"] == attempt["attempt_id"]
               and item["status"] == "completed"
               for item in loaded["agent_invocation_history"])
    assert command("show", f"{state['run_branch']}:target-append.txt") == "target contribution"
    assert command("show", f"{state['run_branch']}:preserved-work.txt") == "work before human response"
