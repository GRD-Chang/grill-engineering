"""真实提交内容核验与公开 resume 的程序集成恢复回归。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult
from agent_run.controller import Controller
from agent_run.delivery import TicketDeliveryEngine
from agent_run.git import GitError, GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state_errors import MissingRunMaterialsError
from agent_run.state import StateStore
from support.delivery_agents import PassAgents, issue
from support.delivery_publisher import ScriptedPublisher
from support.seeding import write_fixture

from support.state import snapshot_run


@pytest.mark.parametrize("merge", [False, True])
@pytest.mark.parametrize(
    "contents", [b"\xef\xbb\xbfkey,value\r\n1,2\r\n", b"<<<<<<< unresolved\n"],
    ids=["bom-crlf", "conflict-marker"],
)
def test_recovered_commit_checks_actual_delta(
    git_repo: Path, tmp_path: Path, merge: bool, contents: bytes,
) -> None:
    git = GitRepository(git_repo)
    base = git.checkout_head(git_repo)
    merge_sha = None
    if merge:
        external = tmp_path / "external"
        git.prepare_validation_checkout(head_sha=base, checkout=external)
        try:
            (external / "source.txt").write_text("source\n")
            merge_sha = git.commit_candidate(external, ticket_number=3, attempt=1,
                                             expected_head=base)
        finally:
            git.remove_worktree(external)
        merged = git._run_in(git_repo, "merge", "--no-ff", "--no-commit", merge_sha)
        assert merged.returncode == 0
    (git_repo / "new.csv").write_bytes(contents)
    assert git._run_in(git_repo, "add", "--all").returncode == 0
    assert git._run_in(git_repo, "commit", "-m", "repair integration").returncode == 0
    committed = git.checkout_head(git_repo)
    assert git.commit_parents(committed) == [base] + ([merge_sha] if merge_sha else [])
    with pytest.raises(GitError) as error:
        git.commit_integration_work(git_repo, expected_head_sha=base,
                                    expected_merge_sha=merge_sha, message="repair integration")
    evidence = error.value.evidence
    assert evidence["command"] == ["git", "diff", "--check", base, committed]
    assert evidence["returncode"] != 0
    assert "new.csv" in evidence["stdout"]
    assert evidence["head_sha"] == committed
    assert evidence["merge_head_sha"] is None
    assert git.checkout_head(git_repo) == committed
    assert (git_repo / "new.csv").read_bytes() == contents


@pytest.mark.parametrize("recovery", ["invalid_manual_commit", "program_commit_before_save"])
def test_public_resume_validates_recovered_integration_commit(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": issue(3)})
    git = GitRepository(git_repo)
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    run_id = state["run_id"]
    checkout = states.root / "worktrees" / run_id / "ticket-3"
    publisher = ScriptedPublisher(git_repo)

    class Agents(PassAgents):
        integration_calls = 0
        continued = False

        def develop(self, request: dict[str, Any]) -> DevelopmentResult:
            if request.get("repair_source") != "integration":
                return super().develop(request)
            self.integration_calls += 1
            event = request["_invocation_event"]
            thread = str(request.get("thread_id") or "integration-thread")
            event("started", requested_thread_id=request.get("thread_id"), attempt_count=1)
            event("thread_started", reported_thread_id=thread)
            if (
                recovery == "invalid_manual_commit"
                and self.integration_calls == 2 and not self.continued
            ):
                event("failed", error="Interrupted Development")
                raise RuntimeError("Interrupted Development")
            if not self.continued:
                (checkout / "delivered.txt").write_text("first attempt\nexternal\n")
                (checkout / "new.csv").write_bytes(
                    b"\xef\xbb\xbfkey,value\r\n1,2\r\n" if recovery == "invalid_manual_commit"
                    else b"\xef\xbb\xbfkey,value\n1,2\n"
                )
            event("completed")
            return DevelopmentResult(thread_id=thread, summary="Integration work preserved")

    agents = Agents(checkout)
    job = TicketDeliveryEngine(git=git, states=states, github=publisher, agents=agents,
                               development_only=True).deliver(run_id)["active_ticket_job"]
    candidate = job["candidate_sha"]
    external = tmp_path / "external"
    git.prepare_validation_checkout(head_sha=job["base_sha"], checkout=external)
    try:
        (external / "delivered.txt").write_text("external\n")
        source = git.commit_candidate(external, ticket_number=3, attempt=1,
                                      expected_head=job["base_sha"])
    finally:
        git.remove_worktree(external)
    publisher.live_head = source
    original_commit = git.commit_integration_work
    if recovery == "program_commit_before_save":
        def commit_then_interrupt(*args: Any, **kwargs: Any) -> str:
            original_commit(*args, **kwargs)
            raise RuntimeError("Interrupted after program commit before state save")
        monkeypatch.setattr(git, "commit_integration_work", commit_then_interrupt)
    paused = TicketDeliveryEngine(git=git, states=states, github=publisher,
                                  agents=agents).deliver(run_id)["active_ticket_job"]
    assert paused["blocked_reason"] == "ticket_execution_failed"
    used = paused["integration_development_budget"]["used"]
    if recovery == "invalid_manual_commit":
        assert used == 2
        assert git._run_in(checkout, "add", "--all").returncode == 0
        manual_commit = git._run_in(
            checkout, "commit", "-m", "chore(ticket-3): resolve integration conflict",
        )
        assert manual_commit.returncode == 0
    else:
        assert used == 1
        monkeypatch.setattr(git, "commit_integration_work", original_commit)
    committed = git.checkout_head(checkout)
    assert git.commit_parents(committed) == [candidate, source]
    agents.continued = True
    calls_before = agents.integration_calls
    if recovery == "invalid_manual_commit":
        before = snapshot_run((states.root, str(run_id)))
        with pytest.raises(MissingRunMaterialsError, match="HEAD 不符"):
            controller.resume(run_id, explicit_resume=True, ticket_number=3, new_thread=True)
        assert snapshot_run((states.root, str(run_id))) == before
        assert agents.integration_calls == calls_before
        assert not agents.publication_requests
        assert publisher.merged_head is None
        assert git.checkout_head(checkout) == committed
        assert (checkout / "new.csv").read_bytes() == b"\xef\xbb\xbfkey,value\r\n1,2\r\n"
        assert states.load_current_run(run_id)["active_ticket_job"]["integration_development_budget"]["used"] == used
        # The same read-only reconciler rejects the actual delta and retains raw evidence.
        with pytest.raises(GitError) as error:
            git.recovered_integration_commit_matches(
                checkout, expected_head_sha=candidate, expected_merge_sha=source,
                message="chore(ticket-3): resolve integration conflict",
            )
        assert error.value.evidence["command"] == ["git", "diff", "--check", candidate, committed]
        failure = error.value.evidence
        checked = git._run("diff", "--check", candidate, committed)
        assert failure["returncode"] == checked.returncode != 0
        assert "new.csv:1: trailing whitespace" in json.loads(failure["stdout_json"])
        assert failure["head_sha"] == committed
        assert failure["merge_head_sha"] is None
        return
    controller.resume(run_id, explicit_resume=True, ticket_number=3, new_thread=True)
    result = TicketDeliveryEngine(git=git, states=StateStore(states.root), github=publisher,
                                  agents=agents).deliver(run_id)
    job = result["active_ticket_job"]
    assert job["integration_development_budget"]["used"] == used
    checked = git._run("diff", "--check", candidate, committed)
    assert checked.returncode == 0
    assert result["status"] == "ticket_completed"
    assert git.is_ancestor(committed, job["ticket_integration"]["head_sha"])
    assert job["integration_work_history"][-1]["resolution_sha"] == committed
    assert publisher.merged_head == job["publication_sha"]
    assert agents.integration_calls == calls_before
    persisted = StateStore(states.root).load_current_run(run_id)
    assert persisted is not None
    assert persisted["active_ticket_job"].get("blocked_reason") == job.get("blocked_reason")
    assert persisted["status"] == result["status"]
