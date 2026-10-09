"""Parent 集成开发在真实 Git 检查失败后保留现场并共用累计额度。"""
from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult, HumanBlockerResult
from agent_run import cli_presentation
from agent_run.attempt_projection import semantic_attempt_history
from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.graph import state_from_graph
from agent_run.github import GitHubReadError
from agent_run.history_facts import history_records
from agent_run.review_budget import review_budget_owner
from agent_run.run_acceptance import RunAcceptanceEngine
from support.run_agents import ScriptedRunAgents
from support.run_artifacts import _passing_artifact, _repair_artifact
from support.run_scenarios import _completed_run


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=checkout, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _two_ticket_run(git_repo: Path, tmp_path: Path, *, conflict: bool = True):
    state, states, git = _completed_run(git_repo)
    fixture = git_repo / "github.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["issues"]["3"] = {
        **data["issues"]["2"], "number": 3, "title": "Ticket 3",
        "body": "Deliver the combined behavior.",
    }
    data["parent"]["sub_issues"] = [2, 3]
    fixture.write_text(json.dumps(data), encoding="utf-8")
    projected = state_from_graph(state, FixtureGitHubReader(fixture).delivery_graph(1))
    state["ticket_graph"] = projected["ticket_graph"]
    state["accepted_ticket_graph_revision"] = state["ticket_graph"]["revision"]
    state["accepted_parent_spec_revision"] = state["parent"]["revision"]
    original = state["ticket_jobs"]["2"]
    first = original["integrated_sha"]
    checkout = tmp_path / "completed-ticket"
    _git(git_repo, "worktree", "add", "--detach", str(checkout), state["base"]["sha"])
    try:
        (checkout / "ticket-2.txt").write_text("first ticket outcome\n", encoding="utf-8")
        _git(checkout, "add", "ticket-2.txt")
        _git(checkout, "commit", "-m", "feat(ticket): deliver first Ticket")
        actual_first = _git(checkout, "rev-parse", "HEAD")
        (checkout / "ticket-3.txt").write_text("combined ticket outcome\n", encoding="utf-8")
        if conflict:
            (checkout / "README.md").write_text("ticket combination\n", encoding="utf-8")
        _git(checkout, "add", ".")
        _git(checkout, "commit", "-m", "feat(ticket): deliver second Ticket")
        second = _git(checkout, "rev-parse", "HEAD")
    finally:
        _git(git_repo, "worktree", "remove", "--force", str(checkout))
    replacements = {
        first: actual_first,
        git.resolve(f"{first}^{{tree}}"): git.resolve(f"{actual_first}^{{tree}}"),
    }

    def replace(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    original = replace(original)
    original["deterministic_integration_record"]["integrated_message"] = git.commit_subject(actual_first)
    state["ticket_jobs"]["2"] = original
    first = actual_first
    replacements = {
        first: second, state["base"]["sha"]: first,
        git.resolve(f"{first}^{{tree}}"): git.resolve(f"{second}^{{tree}}"),
        original["effective_revision"]: state["ticket_graph"]["tickets"]["3"]["content_revision"],
    }

    job = replace(deepcopy(original))
    job["ticket_number"] = 3
    record = job["deterministic_integration_record"]
    record["integrated_message"] = git.commit_subject(second)
    record["pr_number"] = record["pr"]["number"] = 2
    record["required_checks_evidence"]["pr_number"] = 2
    state["ticket_jobs"]["3"] = job
    _git(git_repo, "update-ref", f"refs/heads/{state['run_branch']}", second)
    publisher = FixtureGitHubPublisher(fixture, git)
    publisher.sync_run_branch(run_branch=state["run_branch"], integrated_sha=second)
    if conflict:
        (git_repo / "README.md").write_text("default target\n", encoding="utf-8")
        _git(git_repo, "add", "README.md")
        _git(git_repo, "commit", "-m", "advance default target")
        publisher.data["default_head_sha"] = git.resolve("main")
        publisher._save()
    states.save_run(state["run_id"], state)
    return state, states, git, publisher


class CsvIntegrationAgents(ScriptedRunAgents):
    def __init__(self, failures: int, *, findings: bool = False) -> None:
        super().__init__()
        self.failures = failures
        self.events: list[str] = []
        self._reviews = [_repair_artifact(), _passing_artifact()] if findings else [_passing_artifact()]

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.events.append("development")
        if request.get("repair_source") != "integration":
            return super().develop(request)
        self.development_requests.append(request)
        evidence = json.loads(request["integration_evidence"])
        checkout = Path(request["checkout"])
        assert {record["ticket_number"] for record in request["ticket_completion_records"]} == {2, 3}
        if len(self.development_requests) > 1:
            failure = evidence["failure"]
            assert "diff --cached --check" in failure["error"]
            assert "market.csv" in failure["error"]
            assert "trailing whitespace" in failure["error"]
            assert (checkout / "market.csv").read_bytes().startswith(b"\xef\xbb\xbf")
        (checkout / "README.md").write_text("resolved combined behavior\n", encoding="utf-8")
        contents = b"\xef\xbb\xbfcode,value\r\nA,1\r\n" if len(self.development_requests) <= self.failures else b"code,value\nA,1\n"
        (checkout / "market.csv").write_bytes(contents)
        return DevelopmentResult("run-integration-developer", "修复组合成果与 Git 集成检查。")

    def review(self, request: dict[str, Any]):
        self.events.append("review")
        checkout = Path(request["checkout"])
        assert (checkout / "ticket-2.txt").read_text(encoding="utf-8") == "first ticket outcome\n"
        assert (checkout / "ticket-3.txt").read_text(encoding="utf-8") == "combined ticket outcome\n"
        return super().review(request)


def test_completed_tickets_enter_parent_review_without_initial_development(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git, publisher = _two_ticket_run(git_repo, tmp_path, conflict=False)
    jobs = deepcopy(state["ticket_jobs"])
    agents = CsvIntegrationAgents(0)
    result = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher).accept(state["run_id"])
    assert result["status"] == "run_publication_pending"
    assert agents.events == ["review"]
    assert result["ticket_jobs"] == jobs
    assert {item["ticket_number"] for item in agents.review_requests[0]["ticket_completion_records"]} == {2, 3}
    assert result["run_acceptance"]["review_budget"]["development_attempts"] == 0


def test_failed_git_check_returns_raw_csv_error_to_next_integration_development(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git, publisher = _two_ticket_run(git_repo, tmp_path)
    jobs = deepcopy(state["ticket_jobs"])
    agents = CsvIntegrationAgents(1)
    result = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher).accept(state["run_id"])
    assert result["status"] == "run_publication_pending"
    assert agents.events == ["development", "development", "review"]
    assert result["run_acceptance"]["review_budget"]["development_attempts"] == 2
    assert result["ticket_jobs"] == jobs
    assert _git(git_repo, "show", f"{result['run_branch']}:market.csv") == "code,value\nA,1"
    integration_rows = [row for row in history_records(result, {
        "semantic_agent_attempts": semantic_attempt_history(result, include_history_facts=True),
    }) if str(row.get("work_subject", "")).startswith("run-integration:")]
    assert len(integration_rows) == 2
    for row in integration_rows:
        assert row["object"] == "Run Integration"
        assert row["development_summary"] == "修复组合成果与 Git 集成检查。"
        assert row["budget_facts"]["development_limit"] == 3


def test_git_check_failure_without_merge_conflict_enters_run_development(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, publisher = _two_ticket_run(git_repo, tmp_path, conflict=False)
    original = git.integrate_remote_heads
    failed = False

    def integrate(checkout: Path, **arguments: Any):
        nonlocal failed
        if not failed:
            failed = True
            (checkout / "market.csv").write_bytes(b"\xef\xbb\xbfcode,value\r\nA,1\r\n")
            # Execute the actual managed Git check, with no MERGE_HEAD. The
            # operation adapter must hand its original failure to Development.
            git.commit_integration_work(
                checkout, expected_head_sha=git.checkout_head(checkout),
                expected_merge_sha=None, message="integration work",
            )
        return original(checkout, **arguments)

    monkeypatch.setattr(git, "integrate_remote_heads", integrate)
    agents = CsvIntegrationAgents(0)
    completed = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher).accept(state["run_id"])
    assert completed["status"] == "run_publication_pending"
    assert agents.events == ["development", "review"]
    evidence = json.loads(agents.development_requests[0]["integration_evidence"])
    assert evidence["stage"] == "git_check"
    assert evidence["merge_head_sha"] is None
    assert "diff --cached --check" in evidence["failure"]["error"]
    assert "market.csv" in evidence["failure"]["error"]
    assert completed["run_acceptance"]["review_budget"]["development_attempts"] == 1


@pytest.mark.parametrize("default_append", [False, True])
def test_exhausted_integration_development_public_resume_preserves_scene_and_review_budget(
    git_repo: Path, tmp_path: Path, default_append: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    state, states, git, publisher = _two_ticket_run(git_repo, tmp_path)
    agents = CsvIntegrationAgents(3)
    engine = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher)
    paused = engine.accept(state["run_id"])
    run = paused["run_acceptance"]
    assert paused["status"] == "ready_for_human"
    assert run["blocked_reason"] == "modification_budget_exhausted"
    assert agents.events == ["development"] * 3
    cli_presentation._print_status(paused, as_json=True)
    status = json.loads(capsys.readouterr().out)
    assert status["worker"]["phase"] == "integration"
    assert status["worker"]["attempt"] == 3
    assert status["review_budget"]["development_attempts"] == 3
    before = states.load_current_run(state["run_id"])["run_acceptance"]
    checkout = states.root / "worktrees" / state["run_id"] / "run-integration"
    scene = (checkout / "market.csv").read_bytes()
    head = _git(checkout, "rev-parse", "HEAD")
    index = _git(checkout, "write-tree")
    if default_append:
        (git_repo / "later-default.txt").write_text("later default outcome\n", encoding="utf-8")
        _git(git_repo, "add", "later-default.txt")
        _git(git_repo, "commit", "-m", "append default while integration paused")
        publisher.data["default_head_sha"] = git.resolve("main")
        publisher._save()
    resumed, _ = Controller(FixtureGitHubReader(git_repo / "github.json"), git, states).resume(
        state["run_id"], explicit_resume=True, resume_budget_checkpoint=True,
    )
    current = resumed["run_acceptance"]
    assert current["run_integration_conflict"] == before["run_integration_conflict"]
    assert current["integration_work_history"] == before["integration_work_history"]
    assert current["review_budget"]["development_attempts"] == 0
    assert current["review_budget"]["reviewer_invocations"] == before["review_budget"]["reviewer_invocations"]
    assert current["review_budget_history"][-1]["review_budget"]["development_attempts"] == 3
    assert (checkout / "market.csv").read_bytes() == scene
    assert _git(checkout, "rev-parse", "HEAD") == head
    assert _git(checkout, "write-tree") == index
    assert current["integration_conflict_agent_ordinal"] == 3
    completed = engine.accept(state["run_id"])
    assert completed["status"] == "run_publication_pending"
    assert agents.events == ["development"] * 4 + ["review"]
    assert completed["ticket_jobs"] == state["ticket_jobs"]
    if default_append:
        assert _git(git_repo, "show", f"{completed['run_branch']}:later-default.txt") == "later default outcome"
        assert completed["run_acceptance"]["run_integration"]["target_sha"] == git.resolve("main")


def test_git_work_and_parent_findings_share_development_allowance(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git, publisher = _two_ticket_run(git_repo, tmp_path)
    agents = CsvIntegrationAgents(1, findings=True)
    result = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher).accept(state["run_id"])
    assert result["status"] == "run_publication_pending"
    assert agents.events == ["development", "development", "review", "development", "review"]
    budget = review_budget_owner(result["run_acceptance"])["review_budget"]
    assert budget["development_attempts"] == 3
    assert budget["reviewer_invocations"] == 2
    assert result["ticket_jobs"] == state["ticket_jobs"]


def test_closed_ticket_without_completion_cannot_start_parent_integration(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git, publisher = _two_ticket_run(git_repo, tmp_path)
    state["ticket_jobs"].pop("3")
    states.save_run(state["run_id"], state)
    before = git.resolve(state["run_branch"])
    agents = CsvIntegrationAgents(0)
    with pytest.raises(ValueError, match="every Ticket to be completed"):
        RunAcceptanceEngine(
            git=git, states=states, agents=agents, github=publisher,
        ).accept(state["run_id"])
    assert agents.events == []
    assert git.resolve(state["run_branch"]) == before
    assert states.load_current_run(state["run_id"])["ticket_jobs"] == state["ticket_jobs"]


def test_public_resume_renews_review_only_and_retains_integration_development(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git, publisher = _two_ticket_run(git_repo, tmp_path)
    agents = CsvIntegrationAgents(1)
    accepted = RunAcceptanceEngine(
        git=git, states=states, agents=agents, github=publisher,
    ).accept(state["run_id"])
    run = accepted["run_acceptance"]
    budget = run["review_budget"]
    budget["reviewer_invocations"] = 4
    budget["checkpoint_reason"] = "review_budget_exhausted"
    run.update({
        "phase": "ready_for_human", "blocked_reason": "review_budget_exhausted",
        "validation_attempts": 4,
    })
    accepted.update({"status": "ready_for_human", "terminal_kind": "waiting_human"})
    states.save_run(state["run_id"], accepted)
    before = states.load_current_run(state["run_id"])["run_acceptance"]
    resumed, _ = Controller(FixtureGitHubReader(git_repo / "github.json"), git, states).resume(
        state["run_id"], explicit_resume=True, resume_budget_checkpoint=True,
    )
    current = resumed["run_acceptance"]
    assert current["review_budget"]["reviewer_invocations"] == 0
    assert current["review_budget"]["development_attempts"] == 2
    assert current["review_budget_history"][-1]["review_budget"]["reviewer_invocations"] == 4
    assert current["run_integration"] == before["run_integration"]
    assert current["integration_work_history"] == before["integration_work_history"]
    assert current["integration_conflict_agent_ordinal"] == 2
    assert resumed["ticket_jobs"] == accepted["ticket_jobs"]


def test_integration_human_response_returns_to_same_work_and_retains_origin(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git, publisher = _two_ticket_run(git_repo, tmp_path)

    class HumanAgents(CsvIntegrationAgents):
        def develop(self, request: dict[str, Any]):
            if not self.development_requests:
                self.development_requests.append(request)
                return HumanBlockerResult("run-integration-developer", ("需要确认 CSV 编码",))
            assert request["prior_human_blockers"] == ["需要确认 CSV 编码"]
            assert request["human_response_history"][-1]["response"] == "使用 UTF-8 LF"
            assert request["thread_id"] == "run-integration-developer"
            self.development_requests.append(request)
            (Path(request["checkout"]) / "README.md").write_text("resolved encoding contract\n", encoding="utf-8")
            return DevelopmentResult("run-integration-developer", "按人工确认完成组合修复。")

    agents = HumanAgents(0)
    engine = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher)
    paused = engine.accept(state["run_id"])
    assert paused["run_acceptance"]["blocked_reason"] == "agent_requires_human"
    original_attempt = deepcopy(paused["run_acceptance"]["pending_semantic_attempt"])
    Controller(FixtureGitHubReader(git_repo / "github.json"), git, states).resume(
        state["run_id"], explicit_resume=True, resume_human_blocker=True,
        human_response="使用 UTF-8 LF",
    )
    completed = engine.accept(state["run_id"])
    run = completed["run_acceptance"]
    assert completed["status"] == "run_publication_pending"
    assert run["review_budget"]["development_attempts"] == 1
    assert run["semantic_attempt_history"][0]["attempt_id"] == original_attempt["attempt_id"]
    assert run["integration_work_history"][-1]["human_blockers"] == ["需要确认 CSV 编码"]


def test_internal_repair_base_publication_recovers_lost_response_without_repeat_write(
    git_repo: Path, tmp_path: Path,
) -> None:
    state, states, git, original_publisher = _two_ticket_run(git_repo, tmp_path)

    class LostResponsePublisher(FixtureGitHubPublisher):
        writes = 0

        def publish_branch(self, branch: str, sha: str, *, expected_remote_sha: str | None = None):
            result = super().publish_branch(branch, sha, expected_remote_sha=expected_remote_sha)
            if branch == state["run_branch"]:
                self.writes += 1
                raise GitHubReadError("lost_write_response", "Lost Run integration publication response")
            return result

    publisher = LostResponsePublisher(original_publisher.path, git)
    agents = CsvIntegrationAgents(0, findings=True)
    engine = RunAcceptanceEngine(git=git, states=states, agents=agents, github=publisher)
    with pytest.raises(GitHubReadError, match="Lost Run integration"):
        engine.accept(state["run_id"])
    interrupted = states.load_current_run(state["run_id"])
    intent = interrupted["run_acceptance"]["integration_publication_intent"]
    assert publisher.live_branch_heads(state["run_branch"], "main")[0] == intent["head_sha"]
    completed = engine.accept(state["run_id"])
    assert completed["status"] == "run_publication_pending"
    assert publisher.writes == 1
    assert "integration_publication_intent" not in completed["run_acceptance"]
    assert len(agents.review_requests) == 2
    assert completed["ticket_jobs"] == state["ticket_jobs"]
