from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.git import GitError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.operator_action_facts import operator_action_facts
from agent_run.run_acceptance import RunAcceptanceEngine
from agent_run.work_facts import work_phase
from support.run_agents import ScriptedRunAgents
from support.run_scenarios import _completed_run
from support.state import persist_run_fixture


@pytest.mark.parametrize("failure", ["permission", "lost_response", "read_before", "read_after"])
def test_parent_repair_write_reloads_and_reconciles_without_repeating_agents(
    git_repo: Path, failure: str,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["policy_snapshot"] = DeliveryPolicy(integration_resync_limit=0).snapshot()
    persist_run_fixture(states, str(state["run_id"]), state)
    fixture = git_repo / "github.json"
    run_branch = str(state["run_branch"])
    old_head = git.resolve(run_branch)

    class InterruptedPublisher(FixtureGitHubPublisher):
        writes = 0
        interrupted = False

        def live_branch_heads(self, branch: str, base_branch: str) -> tuple[str | None, str]:
            saved = states.load_current_run(str(state["run_id"]))
            saved_job = (saved or {}).get("run_acceptance", {}).get("repair_job", {})
            intent = saved_job.get("run_branch_write_intent")
            if isinstance(intent, dict) and not self.interrupted and (
                (failure == "read_before" and self.writes == 0)
                or (failure == "read_after" and self.writes == 1)
            ):
                self.interrupted = True
                raise OSError("Run Branch readback unavailable")
            return super().live_branch_heads(branch, base_branch)

        def publish_branch(self, branch: str, head_sha: str, *, expected_remote_sha: str) -> Any:
            self.writes += 1
            if not self.interrupted and failure in {"permission", "lost_response"}:
                self.interrupted = True
                if failure == "permission":
                    raise GitError("repository rule rejects Run Branch updates")
                super().publish_branch(branch, head_sha, expected_remote_sha=expected_remote_sha)
                raise OSError("push response lost")
            return super().publish_branch(branch, head_sha, expected_remote_sha=expected_remote_sha)

    class RepairAgents(ScriptedRunAgents):
        def publication(self, request: dict[str, Any]) -> dict[str, str]:
            raise AssertionError("Parent repair must not call internal Publish")

    publisher = InterruptedPublisher(fixture, git)
    agents = RepairAgents()
    engine = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher)
    blocked = engine.accept(str(state["run_id"]))
    assert blocked["status"] == "ready_for_human"
    job = blocked["run_acceptance"]["repair_job"]
    assert job["blocked_reason"] == "run_branch_write_failed"
    loaded = states.load_current_run(str(state["run_id"]))
    assert loaded is not None
    retained_job = loaded["run_acceptance"]["repair_job"]
    assert retained_job["pause_resume_phase"] == "accepted"
    assert work_phase(retained_job) == "accepted"
    action = next(item for item in operator_action_facts(loaded) if item["location"] == "run_repair")
    assert action["phase"] == "accepted"
    assert action["role"] is None
    intent = dict(job["run_branch_write_intent"])
    assert intent["base_sha"] == old_head
    assert Path(job["repair_checkout"]).exists()
    assert git.resolve(run_branch) == old_head
    assert len(agents.development_requests) == 1
    assert len(agents.review_requests) == 2
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    resumed, _changed = controller.resume(str(state["run_id"]), explicit_resume=True)
    assert resumed["run_acceptance"]["repair_job"]["run_branch_write_intent"] == intent
    assert resumed["run_acceptance"]["repair_job"]["phase"] == action["phase"]
    result = engine.accept(str(state["run_id"]))
    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["integration_resync"]["used"] == 0
    assert git.resolve(run_branch) == intent["candidate_sha"]
    assert publisher.writes == (2 if failure == "permission" else 1)
    assert len(agents.development_requests) == 1
    assert len(agents.review_requests) == 2
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["pull_requests"] == []
    assert all(not branch.startswith("agent-run-repair/") for branch in delivery["published_branches"])


def test_parent_repair_successful_push_survives_local_save_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git = _completed_run(git_repo)
    state["policy_snapshot"] = DeliveryPolicy(integration_resync_limit=0).snapshot()
    persist_run_fixture(states, str(state["run_id"]), state)
    fixture = git_repo / "github.json"
    publisher = FixtureGitHubPublisher(fixture, git)
    agents = ScriptedRunAgents()
    engine = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher)
    original_save = engine.states.save_run
    interrupted = False

    def failing_save(run_id: str, current: dict[str, Any]) -> Any:
        nonlocal interrupted
        job = current.get("run_acceptance", {}).get("repair_job", {})
        if not interrupted and job.get("integrated_sha") and job.get("run_branch_write_intent"):
            interrupted = True
            raise OSError("local StateStore save failed after push")
        return original_save(run_id, current)

    monkeypatch.setattr(engine.states, "save_run", failing_save)
    with pytest.raises(OSError, match="local StateStore save failed"):
        engine.accept(str(state["run_id"]))
    loaded = states.load_current_run(str(state["run_id"]))
    assert loaded is not None
    intent = loaded["run_acceptance"]["repair_job"]["run_branch_write_intent"]
    assert git.resolve(str(state["run_branch"])) == intent["candidate_sha"]
    class NoPrematureGraphReader(FixtureGitHubReader):
        def delivery_graph(self, parent_number: int) -> Any:
            raise AssertionError("pending Run Branch write must settle before graph refresh")

    controller = Controller(NoPrematureGraphReader(fixture), git, states)
    resumed, _changed = controller.resume(str(state["run_id"]))
    assert resumed["run_acceptance"]["repair_job"]["run_branch_write_intent"] == intent
    assert resumed["run_acceptance"]["phase"] == "repairing"
    result = engine.accept(str(state["run_id"]))
    assert result["status"] == "run_publication_pending"
    assert result["run_acceptance"]["integration_resync"]["used"] == 0
    assert len(agents.development_requests) == 1
    assert len(agents.review_requests) == 2
    assert result["run_acceptance"]["candidate_sha"] == intent["candidate_sha"]
