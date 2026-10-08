"""Real Git/StateStore at Ticket delivery and public recovery entry points."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, HumanBlockerResult
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.development_prompts import development_prompt
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from support.delivery_agents import PassAgents, issue
from support.delivery_publisher import ScriptedPublisher
from support.seeding import write_fixture


@pytest.mark.parametrize("mode", ["direct", "quota", "interrupted", "human"])
def test_git_check_returns_raw_failure_to_development_then_delivers(
    git_repo: Path, tmp_path: Path, mode: str,
) -> None:
    limit = 1 if mode == "quota" else 3
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(FixtureGitHubReader(fixture), git, states,
                            delivery_policy=DeliveryPolicy(ticket_integration_development_limit=limit))
    state, _ = controller.start(1)
    run_id = state["run_id"]
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    publisher = ScriptedPublisher(git_repo)

    class Agents(PassAgents):
        integration_calls = 0
        continued = False
        prompts: list[str] = []
        failures: list[dict[str, Any]] = []

        def develop(self, request: dict[str, Any]) -> DevelopmentResult | HumanBlockerResult:
            if request.get("repair_source") != "integration":
                return super().develop(request)
            self.integration_calls += 1
            self.development_requests.append(request)
            event = request["_invocation_event"]
            thread = str(request.get("thread_id") or "integration-new-thread")
            event("started", requested_thread_id=request.get("thread_id"), attempt_count=1)
            event("thread_started", reported_thread_id=thread)
            current = StateStore(states.root).load_current_run(run_id)
            assert current is not None
            owned = [item for item in current["ticket_work_queue"] if item["ticket_number"] == 3]
            assert len(owned) == 1 and owned[0]["lease_id"] is not None
            scene = json.loads(request["integration_evidence"])
            assert scene["source_sha"] == source
            assert scene["target_sha"] == job["base_sha"]
            assert "acceptance_artifact" not in request and "ci_evidence" not in request
            self.prompts.append(development_prompt({**request, "_invocation_mode": "new-thread"}))
            (checkout / "delivered.txt").write_text("first attempt\nexternal\n")
            if self.integration_calls == 1:
                (checkout / "new.csv").write_bytes(b"\xef\xbb\xbfkey,value\r\n1,2\r\n")
            else:
                if mode in {"interrupted", "human"} and not self.continued:
                    if mode == "interrupted":
                        event("failed", error="Controlled integration interruption")
                        raise RuntimeError("Controlled integration interruption")
                    event("completed")
                    return HumanBlockerResult(thread_id=thread, human_blockers=("Confirm which CSV content to preserve",))
                failure = scene["failure"]
                assert failure["command"] == ["git", "diff", "--cached", "--check"]
                assert failure["returncode"] != 0
                assert "new.csv" in json.loads(failure["stdout_json"])
                assert "\r\n" in json.loads(failure["stdout_json"])
                assert failure["head_sha"] == scene["head_sha"]
                assert failure["merge_head_sha"] == source
                self.failures.append(failure)
                (checkout / "new.csv").write_bytes(b"\xef\xbb\xbfkey,value\n1,2\n")
            event("completed")
            return DevelopmentResult(thread_id=thread, summary="Integration handled and tested")

    agents = Agents(checkout)
    state = TicketDeliveryEngine(git=git, states=states, github=publisher,
                                  agents=agents, development_only=True).deliver(run_id)
    job = state["active_ticket_job"]
    original_review = deepcopy(job["review_budget"])
    original_ci = deepcopy(job["ci_recovery"])
    original_candidate = job["candidate_sha"]
    external = tmp_path / "external"
    git.prepare_validation_checkout(head_sha=job["base_sha"], checkout=external)
    try:
        (external / "delivered.txt").write_text("external\n")
        source = git.commit_candidate(external, ticket_number=3, attempt=1,
                                      expected_head=job["base_sha"])
        assert source
    finally:
        git.remove_worktree(external)
    publisher.live_head = source
    engine = TicketDeliveryEngine(git=git, states=states, github=publisher, agents=agents)
    result = engine.deliver(run_id)
    if mode in {"interrupted", "human"}:
        paused = result["active_ticket_job"]
        assert paused["blocked_reason"] == ("ticket_execution_failed" if mode == "interrupted" else "agent_requires_human")
        budget_before = deepcopy(paused["integration_development_budget"])
        semantic_id = paused["pending_semantic_attempt"]["attempt_id"]
        assert budget_before["used"] == 2
        agents.continued = True
        controller.resume(run_id, explicit_resume=True, ticket_number=3, new_thread=True,
                          resume_human_blocker=mode == "human",
                          message="Preserve the existing CSV data" if mode == "human" else None)
        result = TicketDeliveryEngine(git=git, states=StateStore(states.root), github=publisher,
                                       agents=agents).deliver(run_id)
        assert result["active_ticket_job"]["integration_development_budget"] == budget_before
        assert result["active_ticket_job"]["semantic_attempt_history"][-2]["attempt_id"] == semantic_id
        if mode == "human":
            assert "Preserve the existing CSV data" in agents.prompts[-1]
    if limit == 1:
        job = result["active_ticket_job"]
        assert job["blocked_reason"] == "integration_development_exhausted"
        assert agents.integration_calls == 1
        assert job["integration_development_budget"]["used"] == 1
        assert job["candidate_sha"] == original_candidate
        retained = deepcopy({key: job[key] for key in ("review_budget", "ci_recovery", "integration_resync")})
        assert StateStore(states.root).load_current_run(run_id)["active_ticket_job"]["integration_development_budget"]["used"] == 1
        resumed, _ = controller.resume(run_id, explicit_resume=True, ticket_number=3,
                                       new_thread=True)
        renewed = resumed["ticket_jobs"]["3"]
        assert renewed["integration_development_budget"]["used"] == 1
        assert renewed["integration_development_budget"]["limit"] == 2
        assert {key: renewed[key] for key in retained} == retained
        result = TicketDeliveryEngine(git=git, states=StateStore(states.root), github=publisher,
                                       agents=agents).deliver(run_id)
    assert result["status"] == "ticket_completed", result.get("diagnostics")
    job = result["active_ticket_job"]
    assert agents.integration_calls == (3 if mode in {"interrupted", "human"} else 2)
    assert agents.review_count == 1
    assert len(agents.publication_requests) == 1
    assert job["candidate_sha"] == original_candidate
    assert job["review_budget"] == original_review
    assert job["ci_recovery"] == original_ci
    assert job["integration_development_budget"]["used"] == 2
    assert job["integration_resync"]["used"] == 0
    assert git.is_ancestor(source, job["ticket_integration"]["head_sha"])
    assert git.is_ancestor(original_candidate, job["ticket_integration"]["head_sha"])
    assert publisher.merged_head == job["publication_sha"]
    assert agents.failures and "new.csv" in agents.prompts[-1]
    assert "不重新开发整套需求" in agents.prompts[-1]
    assert "程序随后重新执行实际 Git 检查" in agents.prompts[-1]
    assert git._run("show", f"{job['integrated_sha']}:new.csv").stdout == "\ufeffkey,value\n1,2\n"
    assert StateStore(states.root).load_current_run(run_id) is not None


def test_non_conflict_git_failure_is_not_screened_by_a_repair_whitelist(
    git_repo: Path, tmp_path: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = state["run_id"]
    checkout = states.root / "worktrees" / run_id / "ticket-3"

    class Agents(PassAgents):
        integration_calls = 0

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("repair_source") != "integration":
                return super().develop(request)
            self.integration_calls += 1
            scene = json.loads(request["integration_evidence"])
            assert scene["stage"] == "git_check"
            assert scene["merge_head_sha"] is None
            assert not scene["paths"]
            assert scene["failure"]["command"][:4] == ["git", "merge", "--no-ff", "--no-edit"]
            assert "integration marker missing" in json.loads(scene["failure"]["stderr_json"])
            (checkout / "integration-ready").write_text("Required marker\n")
            return DevelopmentResult(thread_id="development-thread-1", summary="Prepared required integration marker")

    agents = Agents(checkout)
    publisher = ScriptedPublisher(git_repo)
    state = TicketDeliveryEngine(git=git, states=states, github=publisher,
                                  agents=agents, development_only=True).deliver(run_id)
    job = state["active_ticket_job"]
    external = tmp_path / "external"
    git.prepare_validation_checkout(head_sha=job["base_sha"], checkout=external)
    try:
        (external / "external.txt").write_text("External contribution\n")
        source = git.commit_candidate(external, ticket_number=3, attempt=1,
                                      expected_head=job["base_sha"])
    finally:
        git.remove_worktree(external)
    publisher.live_head = source
    hook = git_repo / ".git" / "hooks" / "pre-merge-commit"
    hook.write_text('#!/bin/sh\nif [ ! -f integration-ready ]; then echo "integration marker missing" >&2; exit 1; fi\n')
    hook.chmod(0o755)
    try:
        result = TicketDeliveryEngine(git=git, states=states, github=publisher, agents=agents).deliver(run_id)
    finally:
        hook.unlink()
    assert result["status"] == "ticket_completed", result.get("diagnostics")
    assert agents.integration_calls == 1 and agents.review_count == 1
    job = result["active_ticket_job"]
    assert job["integration_development_budget"]["used"] == 1
    assert git._run("show", f"{job['ticket_integration']['head_sha']}:external.txt").stdout == "External contribution\n"


def test_real_fetch_failure_reaches_integration_development_with_exact_snapshot(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run import github_retry
    # Keep the real failing Git command, without repeating the network-read
    # retry policy already covered by its own tests.
    monkeypatch.setattr(github_retry, "MAX_READ_ATTEMPTS", 1)
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    state, _ = Controller(FixtureGitHubReader(fixture), git, states).start(1)
    run_id = state["run_id"]
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    missing_source = "f" * 40

    class Agents(PassAgents):
        def develop(self, request: dict[str, Any]) -> DevelopmentResult | HumanBlockerResult:
            if request.get("repair_source") != "integration":
                return super().develop(request)
            self.development_requests.append(request)
            scene = json.loads(request["integration_evidence"])
            assert scene["source_sha"] == missing_source
            assert scene["target_sha"] == base
            assert scene["failure"]["command"] == ["git", "fetch", "--no-tags", "origin", missing_source]
            assert scene["failure"]["returncode"] != 0
            assert "fatal:" in json.loads(scene["failure"]["stderr_json"])
            return HumanBlockerResult(thread_id="development-thread-1", human_blockers=("Restore access to the recorded source object",))

    agents = Agents(checkout)
    publisher = ScriptedPublisher(git_repo)
    state = TicketDeliveryEngine(git=git, states=states, github=publisher,
                                  agents=agents, development_only=True).deliver(run_id)
    base = state["active_ticket_job"]["base_sha"]
    publisher.live_head = missing_source
    paused = TicketDeliveryEngine(git=git, states=states, github=publisher, agents=agents).deliver(run_id)
    job = paused["active_ticket_job"]
    assert job["blocked_reason"] == "agent_requires_human"
    assert job["integration_development_budget"]["used"] == 1
    assert len(agents.development_requests) == 2 and agents.review_count == 1
    assert not agents.publication_requests
    assert StateStore(states.root).load_current_run(run_id) is not None
