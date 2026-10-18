"""受控阶段提前失败时，真实交付必须在关闭通知前停止并回收。"""
from __future__ import annotations

from pathlib import Path
import subprocess
from threading import Event, Lock, Thread, current_thread, enumerate as threads
from typing import Any

import pytest

from agent_run.controller import Controller
from agent_run.executor_admission import check_executor_cancelled
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.notifications import Notifications
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from agent_run.task_control import TaskControlBusyError
from support.background_delivery import background_delivery
from support.cli import issue, stdout_json
from support.seeding import seed_run, write_fixture
from support.state import persist_run_fixture, snapshot_run
from support.ticket_parallel import TicketAgents, ticket_engine
from support.workspace import managed_repo, managed_state
from support.ticket_process import isolated_ticket_test


@isolated_ticket_test
@pytest.mark.parametrize("stage", ["development", "review", "publication"])
def test_early_failure_reaps_delivery_before_closing_dependencies(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    # 一张目标票和一张在途兄弟票，确认清理覆盖整个执行链，而非只退出目标票。
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): issue(number) for number in (2, 3)
    })
    seeded = seed_run(git_repo, fixture, "1", "--ticket-development-workers", "2")
    assert seeded.returncode == 0, seeded.stderr
    run_id = stdout_json(seeded)["run_id"]
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    state = states.load_current_run(run_id)
    assert state is not None
    state["notifications"] = {
        "enabled": True, "profile": "test", "open_id": "ou_test", "app_id": "cli_test",
    }
    persist_run_fixture(states, run_id, state)
    monkeypatch.setattr("agent_run.notifications.send", lambda *_args, **_kwargs: {
        "outcome": "success", "reason": None,
    })
    sender = Notifications(states.root, state)
    entered = {number: Event() for number in (2, 3)}
    release = {number: Event() for number in (2, 3)}
    cancelled = {number: Event() for number in (2, 3)}
    observed_threads: set[Thread] = set()
    children: list[subprocess.Popen[Any]] = []
    late_writes: list[dict[str, Any]] = []
    lock = Lock()

    def gate(number: int) -> None:
        with lock:
            observed_threads.add(current_thread())
        entered[number].set()
        assert release[number].wait(15), f"{stage} gate was not released"
        try:
            check_executor_cancelled()
        except TaskControlBusyError:
            cancelled[number].set()
            raise
        pytest.fail("提前失败后的 Agent 越过取消边界")

    def observe(saved: dict[str, Any]) -> None:
        with lock:
            observed_threads.add(current_thread())
            if sender.closed:
                late_writes.append(saved)
        sender.observe(saved)

    states.run_saved_observer = observe

    def develop(number: int) -> None:
        if number == 3 or stage == "development":
            gate(number)

    agents = TicketAgents(
        git_repo, (2, 3), develop,
        before_review=gate if stage == "review" else None,
        before_publication=gate if stage == "publication" else None,
    )
    engine = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=ticket_engine(git, states, fixture, agents),
    )
    popen = subprocess.Popen

    def track_child(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        child = popen(*args, **kwargs)
        with lock:
            children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", track_child)
    baseline_threads = set(threads())
    with pytest.raises(AssertionError, match="controlled early failure"):
        with background_delivery(
            git_repo, run_id, lambda: engine.deliver(run_id), sender, release.values(),
        ) as execution:
            close = sender.close

            def close_after_delivery() -> None:
                assert execution.returned.is_set()
                assert not execution.thread.is_alive()
                assert execution.ownership_process.poll() is not None
                with lock:
                    assert not [item for item in observed_threads - baseline_threads if item.is_alive()]
                    assert all(child.poll() is not None for child in children)
                close()

            monkeypatch.setattr(sender, "close", close_after_delivery)
            assert all(event.wait(10) for event in entered.values()), execution.errors
            assert execution.thread.is_alive() and execution.ownership_process.poll() is None
            assert not any(event.is_set() for event in release.values()) and not sender.closed
            raise AssertionError("controlled early failure")

    assert all(event.is_set() for event in cancelled.values()), "受控 Agent 未观察到真实 Executor 取消信号"
    assert execution.returned.is_set() and not execution.thread.is_alive()
    assert sender.closed and sender.thread is not None and not sender.thread.is_alive()
    assert children and all(child.poll() is not None for child in children)
    assert not [item for item in threads() if item not in baseline_threads]
    current = execution.control.load(execution.task)
    assert current is not None and current["executor"]["ticket_resume_closed"] is True
    assert current["executor"]["failure"] == "controlled early failure"
    assert all(isinstance(error, TaskControlBusyError) for error in execution.errors), execution.errors
    current_run = states.load_current_run(run_id)
    assert current_run is not None
    require_current_run_state(current_run)
    assert all(item["lease_id"] is None for item in current_run["ticket_work_queue"])
    fixture_role = {"development": "developments", "review": "reviews", "publication": "publications"}[stage]
    assert agents.backends[2].positions[fixture_role] == 0
    assert agents.backends[3].positions["developments"] == 0
    assert late_writes == []
    saved = snapshot_run((states.root, run_id))
    for event in release.values():
        event.set()
    assert snapshot_run((states.root, run_id)) == saved
