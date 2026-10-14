"""共享故障后的显式恢复保留实际调用的 Thread 与独立对象事实。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import local

import pytest

from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state import SimulatedProcessCrash, StateStore
from agent_run.state_contract import require_current_run_state
from support.cli_payloads import human_blocker_step
from support.cli import run_cli, stdout_json
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("first_role", ["development", "fresh_acceptance"])
@pytest.mark.parametrize("sibling_boundary", ["running", "completed", "returned"])
def test_run_resume_restores_nonselected_threads_without_consuming_local_gate(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, sibling_boundary: str, first_role: str,
) -> None:
    initial, states, git, fixture = ticket_run(git_repo, (2, 3, 4, 5), workers=2)
    run_id = initial["run_id"]
    lane = local()

    def developing(number: int) -> None:
        lane.number, lane.injected = str(number), False

    agents = TicketAgents(git_repo, (2, 3, 4, 5), developing)
    agents.backends[5].data["developments"] = [human_blocker_step("developer-5")]
    engine = ticket_engine(git, states, fixture, agents)
    engine.deliver_ticket(run_id, 2)
    engine.deliver_ticket(run_id, 5)
    before = StateStore(states.root).load_current_run(run_id)
    assert before["ticket_jobs"]["2"]["phase"] == "completed"
    assert before["active_ticket_job"]["ticket_number"] == 2
    assert before["ticket_jobs"]["5"]["blocked_reason"] == "agent_requires_human"

    def crash_after_reported_thread() -> None:
        number = getattr(lane, "number", None)
        if number not in {"3", "4"} or lane.injected:
            return
        saved = states.load_current_run(run_id)
        invocation = saved["ticket_agent_invocations"].get(number, {})
        if invocation.get("role") != (first_role if number == "3" else "development"):
            return
        boundary = "running" if number == "3" else sibling_boundary
        returned = saved["ticket_jobs"][number].get("pending_semantic_attempt", {}).get("returned_result")
        reached = (
            invocation.get("status") == "running" if boundary == "running"
            else invocation.get("status") == "completed" and (boundary != "returned" or isinstance(returned, dict))
        )
        if reached and invocation.get("reported_thread_id"):
            lane.injected = True
            raise SimulatedProcessCrash("after durable Thread identity")

    # 在真实 SQLite 提交后中断；等待两个执行线程均退出，再报告共享故障。
    with monkeypatch.context() as crash:
        crash.setattr(states, "_after_run_saved", crash_after_reported_thread)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(engine.deliver_ticket, run_id, number) for number in (3, 4)]
            for future in pending:
                with pytest.raises(SimulatedProcessCrash):
                    future.result(timeout=10)

    interrupted = StateStore(states.root).load_current_run(run_id)
    for number in ("3", "4"):
        expected_status = "running" if number == "3" or sibling_boundary == "running" else "completed"
        assert interrupted["ticket_agent_invocations"][number]["status"] == expected_status
        reviewing = number == "3" and first_role == "fresh_acceptance"
        assert interrupted["ticket_agent_invocations"][number]["reported_thread_id"] == f"{'reviewer' if reviewing else 'developer'}-{number}"
        assert interrupted["ticket_jobs"][number].get("development_thread_id") == ("developer-3" if reviewing else None)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    assert controller.record_execution_failure(run_id, "shared execution interrupted")
    failed = StateStore(states.root).load_current_run(run_id)
    assert failed["diagnostics"][0]["operator_gate"]["work_subject"] == f"run:{run_id}"
    assert failed["ticket_jobs"]["2"] == before["ticket_jobs"]["2"]
    # 故障报告不是退出证明，不靠它把观察对象或兄弟调用改成 failed。
    assert failed["ticket_agent_invocations"] == interrupted["ticket_agent_invocations"]

    # 公开查询保持门禁和未收口调用；显式 Run 恢复才消费退出事实。
    for command in ("status", "history"):
        query = run_cli(git_repo, fixture, command, run_id, "--json", "--state-dir", str(states.root))
        assert query.returncode == 0, query.stdout + query.stderr
        assert any(item["work_subject"] == f"run:{run_id}" for item in stdout_json(query)["operator_actions"])
    assert StateStore(states.root).load_current_run(run_id) == failed
    resumed, _ = controller.resume(run_id, explicit_resume=True)
    reopened = StateStore(states.root).load_current_run(run_id)
    for field in (
        "status", "terminal_kind", "diagnostics", "active_ticket_job",
        "active_agent_invocation", "ticket_jobs", "ticket_agent_invocations",
    ):
        assert reopened[field] == resumed[field]
    # Controller 返回相关调用投影，完整查询仍须保留全部已归档调用。
    assert all(item in reopened["agent_invocation_history"]
               for item in resumed["agent_invocation_history"])
    assert len(reopened["agent_invocation_history"]) == len(failed["agent_invocation_history"])
    assert all(item in reopened["agent_invocation_history"]
               for item in failed["agent_invocation_history"]
               if item["work_subject"] not in {"ticket:3", "ticket:4"})
    require_current_run_state(reopened)
    for number in ("3", "4"):
        job = reopened["ticket_jobs"][number]
        invocation = reopened["ticket_agent_invocations"][number]
        original = failed["ticket_agent_invocations"][number]
        still_running = number == "3" or sibling_boundary == "running"
        assert invocation["status"] == ("resuming" if still_running else "completed")
        if not still_running:
            assert invocation == original
        assert invocation["started_at"] == original["started_at"]
        assert invocation["semantic_attempt"] == original["semantic_attempt"]
        assert job["pending_semantic_attempt"] == failed["ticket_jobs"][number]["pending_semantic_attempt"]
        assert job["review_budget"] == failed["ticket_jobs"][number]["review_budget"]
        assert job["development_thread_id"] == f"developer-{number}"
        if number == "3" and first_role == "fresh_acceptance":
            assert job["review_resume_thread_id"] == "reviewer-3"
        closed = next(item for item in reopened["agent_invocation_history"]
                      if item["work_subject"] == f"ticket:{number}"
                      and item["started_at"] == original["started_at"])
        assert closed["status"] == ("failed" if still_running else "completed")
    for number in ("2", "5"):
        assert reopened["ticket_jobs"][number] == before["ticket_jobs"][number]
        assert reopened["ticket_agent_invocations"][number] == before["ticket_agent_invocations"][number]

    def recover(number: int) -> None:
        assert sibling_boundary != "returned" or number != 4, "persisted result was replayed"
        assert first_role != "fresh_acceptance" or number != 3, "saved Candidate was redeveloped"

    recovered_agents = TicketAgents(git_repo, (3, 4), recover)
    for number in (3, 4):
        reviewing = number == 3 and first_role == "fresh_acceptance"
        recovered_agents.backends[number].data["reviews" if reviewing else "developments"][0]["expected_thread_id"] = f"{'reviewer' if reviewing else 'developer'}-{number}"
    DeliveryRunEngine(
        controller=controller, tickets=ticket_engine(git, states, fixture, recovered_agents),
    ).deliver_from_state(run_id, resumed)
    result = StateStore(states.root).load_current_run(run_id)
    for number in ("3", "4"):
        assert result["ticket_jobs"][number]["phase"] == "completed"
        assert result["ticket_jobs"][number]["review_budget"]["development_attempts"] == 1
        calls = [item for item in result["agent_invocation_history"]
                 if item["work_subject"] == f"ticket:{number}" and item["role"] == (first_role if number == "3" else "development")]
        cached = number == "4" and sibling_boundary == "returned"
        assert len(calls) == (1 if cached else 2)
        if not cached:
            assert calls[-1]["requested_thread_id"] == f"{'reviewer' if number == '3' and first_role == 'fresh_acceptance' else 'developer'}-{number}"
        assert calls[-1]["semantic_attempt"]["attempt_id"] == failed["ticket_jobs"][number]["pending_semantic_attempt"]["attempt_id"]
    assert result["ticket_jobs"]["2"] == before["ticket_jobs"]["2"]
    assert result["ticket_jobs"]["5"] == before["ticket_jobs"]["5"]
    require_current_run_state(StateStore(states.root).load_current_run(run_id))
