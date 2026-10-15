"""真实完成记录、局部失败与 Run 门禁在 SQLite 重开后保持准确归属。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event
import subprocess
from typing import Any

import pytest

from agent_run.agent_invocation import record_session_interruption
from agent_run.controller import Controller
from agent_run.github import GitHubReadError
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.operator_action_facts import operator_action_facts
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.run_driver import DirectRunOperations, RunDriver
from agent_run.external_supervision import ExternalSupervisor
from agent_run.runner_credentials import RunnerCredentialError
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from agent_run.object_state import save_owned
from support.cli import run_cli, stdout_json
from support.state_commits import observe_run_commits
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run
from support.state import persist_run_fixture


def _select_completed(states: StateStore, run_id: str) -> None:
    state = states.load_current_run(run_id)
    assert state is not None
    assert state["ticket_jobs"]["2"]["phase"] == "completed"
    state["active_ticket_job"] = state["ticket_jobs"]["2"]
    state["active_agent_invocation"] = state["ticket_agent_invocations"]["2"]
    state["status"] = "ticket_completed"
    state["terminal_kind"] = None
    state["diagnostics"] = []
    persist_run_fixture(states, run_id, state)


@pytest.mark.parametrize("failure", ["worker", "save"])
def test_failure_while_observing_completed_ticket_preserves_independent_delivery(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    state, states, git, fixture = ticket_run(
        git_repo, (2, 3, 4, 5), blocked_by={5: [3]}, workers=2,
    )
    run_id = state["run_id"]
    failed, sibling_started, owner_finished = Event(), Event(), Event()

    def develop(number: int) -> None:
        if number == 3:
            assert sibling_started.wait(10)
            _select_completed(states, run_id)
        elif number == 4:
            sibling_started.set()
            assert failed.wait(10)
            assert owner_finished.wait(10)
            selected, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(run_id)
            assert selected["active_ticket_job"]["ticket_number"] == 4

    agents = TicketAgents(git_repo, (2, 3, 4, 5), develop)
    engine = ticket_engine(git, states, fixture, agents)
    engine.deliver_ticket(run_id, 2)
    completed = deepcopy(states.load_current_run(run_id)["ticket_jobs"]["2"])
    if failure == "worker":
        agents.backends[3].data["developments"][0]["error_after_writes"] = "B Worker failed"
    injected = False

    def before_save(saved_run: str, value: dict[str, Any]) -> None:
        nonlocal injected
        if (failure == "save" and not injected
                and value["ticket_jobs"].get("3", {}).get("phase") == "committing_candidate"):
            injected = True
            raise OSError("B result save failed once")

    def observed(value: dict[str, Any]) -> None:
        if not failed.is_set() and value["ticket_jobs"].get("3", {}).get("blocked_reason") == "ticket_execution_failed":
            assert value["active_ticket_job"]["ticket_number"] == 2
            failed.set()

    states.run_saved_observer = observed
    observe_run_commits(monkeypatch, states, before=before_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(engine.deliver_ticket, run_id, number) for number in (3, 4)]
        futures[0].add_done_callback(lambda _: owner_finished.set())
        try:
            for future in futures:
                future.result(timeout=15)
        finally:
            failed.set()
            sibling_started.set()
            owner_finished.set()
    states.run_saved_observer = None
    assert injected == (failure == "save")
    reopened = StateStore(states.root)
    saved = reopened.load_current_run(run_id)
    require_current_run_state(saved)
    assert saved["ticket_jobs"]["2"] == completed
    assert saved["ticket_jobs"]["4"]["phase"] == "completed"
    assert saved["active_ticket_job"]["ticket_number"] == 4
    assert "5" not in saved["ticket_jobs"]
    job = saved["ticket_jobs"]["3"]
    assert job["blocked_reason"] == "ticket_execution_failed"
    assert job["execution_resume_phase"] == "developing"
    assert job["execution_failure"] == (
        "B Worker failed" if failure == "worker" else "B result save failed once"
    )
    assert job["review_budget"]["development_attempts"] == 1
    invocation = saved["ticket_agent_invocations"]["3"]
    assert invocation["work_subject"] == "ticket:3"
    assert invocation["semantic_attempt"]["attempt_id"] == job["pending_semantic_attempt"]["attempt_id"]
    assert invocation["status"] == ("failed" if failure == "worker" else "completed")
    assert invocation in saved["agent_invocation_history"]
    actions = operator_action_facts(saved)
    assert [(item["work_subject"], item["scope"]) for item in actions] == [("ticket:3", "local")]
    assert (reopened.root / "worktrees" / run_id / "ticket-3" / "ticket-3.txt").read_text() == "done\n"
    assert git.resolve(completed["publication_sha"]) == completed["publication_sha"]
    assert subprocess.check_output(
        ["git", "show", f"{saved['run_branch']}:ticket-2.txt"], cwd=git_repo, text=True,
    ) == "done\n"
    completed_c = deepcopy(saved["ticket_jobs"]["4"])
    mutations = json.loads(fixture.read_text())["delivery"]["mutations"]

    # 查询进入独立解释器，只读取真实 SQLite，不能依赖 lane 的内存投影。
    for command in ("status", "history"):
        queried = run_cli(git_repo, fixture, command, run_id, "--json", "--state-dir", str(states.root))
        assert queried.returncode == 0, queried.stdout + queried.stderr
        output = stdout_json(queried)
        assert [(item["work_subject"], item["scope"]) for item in output["operator_actions"]] == [("ticket:3", "local")]
        assert output["operator_actions"][0]["reasons"] == [job["execution_failure"]]
        if command == "history":
            failed_invocations = [item for item in output["agent_invocations"] if item["work_subject"] == "ticket:3"]
            assert len(failed_invocations) == 1
            assert failed_invocations[0]["status"] == invocation["status"]
    assert reopened.load_current_run(run_id) == saved

    if failure == "worker":
        agents.backends[3].data["developments"].append({
            "thread_id": "developer-3", "expected_thread_id": "developer-3",
            "summary": "B recovered", "expected_files": {"ticket-3.txt": "done\n"},
        })
    controller = Controller(FixtureGitHubReader(fixture), git, reopened)
    resumed, _ = controller.resume(run_id, explicit_resume=True, ticket_number=3)
    DeliveryRunEngine(
        controller=controller, tickets=ticket_engine(git, reopened, fixture, agents),
    ).deliver_from_state(run_id, resumed)
    # 调度返回当前对象投影；完整调用历史由真实 SQLite 重开查询核验。
    result = StateStore(states.root).load_current_run(run_id)
    assert all(result["ticket_jobs"][str(number)]["phase"] == "completed" for number in (2, 3, 4, 5))
    assert result["ticket_jobs"]["2"] == completed
    assert result["ticket_jobs"]["4"] == completed_c
    assert result["ticket_jobs"]["3"]["review_budget"]["development_attempts"] == 1
    developments = [item for item in result["agent_invocation_history"]
                    if item["work_subject"] == "ticket:3" and item["role"] == "development"]
    assert len(developments) == (2 if failure == "worker" else 1)
    assert {item["semantic_attempt"]["attempt_id"] for item in developments} == {
        job["pending_semantic_attempt"]["attempt_id"],
    }
    # 完成对象不再次发布、合入或关闭；直接核验 fixture 记录的外部写入。
    after_mutations = json.loads(fixture.read_text())["delivery"]["mutations"]
    for number in (2, 4):
        assert [item for item in after_mutations if item.get("ticket_number") == number] == [
            item for item in mutations if item.get("ticket_number") == number
        ]
    require_current_run_state(StateStore(states.root).load_current_run(run_id))


@pytest.mark.parametrize("failure", ["execution", "credential", "lane_credential", "session", "contradiction"])
def test_shared_failure_belongs_to_run_with_completed_selection(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    engine = ticket_engine(git, states, fixture, agents)
    engine.deliver_ticket(run_id, 2)
    _select_completed(states, run_id)
    before = states.load_current_run(run_id)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    if failure == "session":
        record_session_interruption(before, save=lambda value: save_owned(states, run_id, value, fields={'active_agent_invocation', 'agent_invocation_history', 'diagnostics', 'github_refresh_resume_status', 'operator_stop', 'status', 'terminal_kind', 'ticket_agent_invocations', 'ticket_work_queue'}))
    elif failure == "contradiction":
        assert controller.record_deterministic_contradiction(run_id, "github_invalid_response", "Shared repository response invalid")
    elif failure == "lane_credential":
        def unavailable(**_kwargs: Any) -> None:
            raise RunnerCredentialError("Shared credential lost")
        monkeypatch.setattr(git, "prepare_ticket_checkout", unavailable)
        engine.deliver_ticket(run_id, 3)
    else:
        assert controller.record_execution_failure(
            run_id, "Shared execution lost", runner_credential_failure=failure == "credential",
        )
    saved = StateStore(states.root).load_current_run(run_id)
    require_current_run_state(saved)
    assert saved["ticket_jobs"]["2"] == before["ticket_jobs"]["2"]
    assert saved["ticket_agent_invocations"]["2"] == before["ticket_agent_invocations"]["2"]
    assert saved["active_ticket_job"]["ticket_number"] == 2
    actions = operator_action_facts(saved)
    assert [(item["work_subject"], item["scope"]) for item in actions] == [(f"run:{run_id}", "global")]
    for command in ("status", "history"):
        queried = run_cli(git_repo, fixture, command, run_id, "--json", "--state-dir", str(states.root))
        assert queried.returncode == 0, queried.stdout + queried.stderr
        output = stdout_json(queried)
        assert [(item["work_subject"], item["scope"]) for item in output["operator_actions"]] == [(f"run:{run_id}", "global")]


@pytest.mark.parametrize("path", ["driver", "refresh"])
def test_shared_read_contradiction_is_run_owned_before_ticket_work(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    reader = FixtureGitHubReader(fixture)
    controller = Controller(reader, git, states)
    error = GitHubReadError("github_invalid_response", "Shared GitHub response is not an object")

    def invalid_response(*_args: Any) -> Any:
        raise error

    if path == "driver":
        operations = DirectRunOperations(
            controller=controller, states=states, git=git, github_reader=reader,
            publisher_factory=lambda: FixtureGitHubPublisher(fixture, git),
            agents=TicketAgents(git_repo, (2, 3), lambda _: None),
        )
        driver = RunDriver(operations=operations, states=states, supervisor=ExternalSupervisor())
        monkeypatch.setattr(reader, "repository", invalid_response)
        with pytest.raises(GitHubReadError) as caught:
            reader.repository()
        assert driver.record_failure(run_id, caught.value)
    else:
        monkeypatch.setattr(reader, "delivery_graph", invalid_response)
        controller.resume(run_id)
    saved = StateStore(states.root).load_current_run(run_id)
    require_current_run_state(saved)
    assert saved["status"] == "deterministic_contradiction"
    assert saved["ticket_jobs"] == state["ticket_jobs"]
    assert saved["active_ticket_job"] == state["active_ticket_job"]
    action, = operator_action_facts(saved)
    assert action["work_subject"] == f"run:{run_id}"
    assert action["scope"] == "global"
    assert action["reason"] == "github_invalid_response"
