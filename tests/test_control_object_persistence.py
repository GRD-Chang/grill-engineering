"""普通控制和事实刷新通过对象事务保留已保存工作。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.controller import Controller
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.run_driver import ControlRunOperation, DirectRunOperations
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("history_size", [0, 300])
def test_refresh_and_run_stop_preserve_history_without_run_save_bridge(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, history_size: int,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=2)
    run_id = state["run_id"]
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(run_id, 2)
    retained = states.load_current_run(run_id)
    assert retained is not None
    for index in range(history_size):
        archived = deepcopy(retained["agent_invocation_history"][0])
        archived["started_at"] = f"2025-01-01T00:00:{index:06d}+00:00"
        retained["agent_invocation_history"].append(archived)
    states.save_run(run_id, retained)
    before = StateStore(states.root).load_current_run(run_id)
    assert before is not None

    def reject_bridge(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ordinary control used the whole Run save bridge")

    monkeypatch.setattr(states, "_save_run_unlocked", reject_bridge)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    refreshed, _ = controller.resume(run_id)
    assert refreshed["ticket_jobs"]["2"]["candidate_sha"] == before["ticket_jobs"]["2"]["candidate_sha"]
    after_refresh = StateStore(states.root).load_current_run(run_id)
    assert after_refresh is not None
    operations = DirectRunOperations(
        controller=controller, states=states, git=git,
        github_reader=FixtureGitHubReader(fixture), publisher_factory=lambda: None,
        agents=SimpleNamespace(),
    )
    stopped = operations.apply_control(run_id, ControlRunOperation(kind="stop", target_executor=None))
    assert stopped.state["status"] == "operator_stopped"
    saved = StateStore(states.root).load_current_run(run_id)
    assert saved is not None
    assert saved["ticket_jobs"] == after_refresh["ticket_jobs"]
    assert saved["agent_invocation_history"] == before["agent_invocation_history"]
    assert saved["operator_stop"]["resume_status"] == refreshed["status"]
    require_current_run_state(saved)

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, StateStore(states.root)).resume(
        run_id, explicit_resume=True,
    )
    assert resumed["status"] != "operator_stopped"
    assert resumed["ticket_jobs"]["2"]["candidate_sha"] == before["ticket_jobs"]["2"]["candidate_sha"]
    reopened = StateStore(states.root).load_current_run(run_id)
    assert reopened is not None
    assert reopened["agent_invocation_history"] == before["agent_invocation_history"]


def test_external_wait_cannot_overwrite_a_stop_saved_during_observation(
    git_repo: Path,
) -> None:
    from agent_run.agent_invocation import record_operator_stop
    from agent_run.external_supervision import ExternalSupervisor, wait_for_github_convergence
    from agent_run.run_driver import RunDriver

    state, states, _git, _fixture = ticket_run(git_repo, (2,))
    run_id = state["run_id"]
    wait_for_github_convergence(state, code="read_unknown", message="unknown response", waiting_for="GitHub")
    states.save_run(run_id, state)
    stopped: list[dict[str, object]] = []

    class StopDuringObservation(ExternalSupervisor):
        def observe(self, current):
            window = super().observe(current)
            latest = states.load_current_run(run_id)
            assert latest is not None
            record_operator_stop(latest, save=lambda value: states.save_run(run_id, value))
            stopped.append(deepcopy(latest))
            return window

    class Operations:
        controller = object()

        @staticmethod
        def classify(current):
            return DirectRunOperations.classify(current)

        @staticmethod
        def dispatch(_step, _run_id):
            return DirectRunOperations.classify(state)

    result = RunDriver(
        operations=Operations(), states=states,
        supervisor=StopDuringObservation(now=lambda: 0.0),
    ).advance(state)
    assert result["status"] == "operator_stopped"
    assert len(stopped) == 1
    reopened = StateStore(states.root).load_current_run(run_id)
    assert reopened == stopped[0]
    require_current_run_state(reopened)


def test_driver_wait_survives_reload_of_the_selected_ticket_lane(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.external_supervision import ExternalSupervisor, wait_for_github_convergence
    from agent_run.object_state import object_store
    from agent_run.run_driver import RunDriver
    from agent_run.ticket_state import TicketState

    state, states, _git, _fixture = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    wait_for_github_convergence(state, code="read_unknown", message="waiting", waiting_for="GitHub")
    clock = [0.0]
    supervisor = ExternalSupervisor(now=lambda: clock[0], sleeper=lambda _: None)
    supervisor.observe(state)
    states.save_run(run_id, state)
    # Scheduling retains the lane's wait while projecting an active Run.
    selected = states.load_current_run(run_id)
    assert selected is not None
    selected["status"] = "active"
    states.save_run(run_id, selected)

    def reject_bridge(*_args, **_kwargs):
        raise AssertionError("Ticket wait entered the whole-Run save bridge")

    monkeypatch.setattr(states, "_save_run_unlocked", reject_bridge)
    scoped = object_store(states)
    driver = RunDriver(operations=SimpleNamespace(), states=scoped, supervisor=supervisor)
    for retries in (1, 2):
        lane = TicketState(scoped, run_id, 2).load_current_run(run_id)
        assert lane is not None
        expected = driver._wait_facts(lane)
        assert supervisor.retry_due(lane)
        driver._save_wait(run_id, lane, expected)
        reopened = StateStore(states.root).load_current_run(run_id)
        assert reopened is not None
        assert reopened["active_ticket_job"]["external_wait"]["supervision_window"] == lane["supervision_window"]
        restarted = TicketState(scoped, run_id, 2).load_current_run(run_id)
        assert restarted is not None
        assert restarted["supervision_window"]["retry_count"] == retries
        assert restarted["supervision_window"]["deadline"] == 600.0
        clock[0] = restarted["supervision_window"]["next_retry_at"]


@pytest.mark.parametrize("history_size", [0, 300])
def test_ordinary_control_projection_omits_completed_sibling_and_retirement_archive(
    git_repo: Path, history_size: int,
) -> None:
    from agent_run.object_state import object_store

    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    engine = ticket_engine(git, states, fixture, TicketAgents(git_repo, (2, 3), lambda _: None))
    engine.deliver_ticket(run_id, 3)
    Controller(FixtureGitHubReader(fixture), git, states).resume(run_id)
    full = StateStore(states.root).load_current_run(run_id)
    assert full is not None
    assert full["ticket_jobs"]["3"]["phase"] == "completed"
    full["retired_job_generations"] = [
        {"work_subject": f"ticket:{100 + number}", "generation": 1,
         "reason": "requeue", "evidence": "archived evidence " * 64}
        for number in range(history_size)
    ]
    states.save_run(run_id, full)
    scoped = object_store(states).load_current_run(run_id)
    assert scoped is not None
    assert set(scoped["ticket_jobs"]) == {"2"}
    assert "retired_job_generations" not in scoped
    scoped["updated_at"] = "2026-10-13T00:00:00+00:00"
    object_store(states).save_run(run_id, scoped)
    reopened = StateStore(states.root).load_current_run(run_id)
    assert reopened is not None
    assert reopened["ticket_jobs"]["3"] == full["ticket_jobs"]["3"]
    assert reopened["retired_job_generations"] == full["retired_job_generations"]
    assert reopened["updated_at"] == scoped["updated_at"]


def test_executor_notification_reads_complete_facts_after_scoped_commit(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_run.executor import DeliveryExecutor
    from agent_run.notifications import Notifications
    from agent_run.object_state import object_store

    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    ticket_engine(git, states, fixture, TicketAgents(git_repo, (2, 3), lambda _: None)).deliver_ticket(run_id, 3)
    Controller(FixtureGitHubReader(fixture), git, states).resume(run_id)
    complete = states.load_current_run(run_id)
    assert complete is not None
    complete["notifications"] = {"enabled": True, "mode": "concise"}
    states.save_run(run_id, complete)
    observations = []

    def observe(_sender, current, **_kwargs):
        observations.append(deepcopy(current))

    monkeypatch.setattr(Notifications, "_observe", observe)
    scoped = object_store(states)

    def advance(current):
        assert set(current["ticket_jobs"]) == {"2"}
        current["updated_at"] = "2026-10-13T00:00:00+00:00"
        scoped.save_run(run_id, current)
        return current

    DeliveryExecutor(
        states=scoped, driver_factory=lambda *_: SimpleNamespace(advance=advance),
    ).execute(run_id)
    assert len(observations) >= 2
    assert all(current["ticket_jobs"] == complete["ticket_jobs"] for current in observations)
    assert all(current["agent_invocation_history"] == complete["agent_invocation_history"]
               for current in observations)
    assert observations[-1]["updated_at"] == "2026-10-13T00:00:00+00:00"
    assert scoped.run_saved_observer is None
