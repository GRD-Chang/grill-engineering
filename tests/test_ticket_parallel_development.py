"""Real Ticket Engine and StateStore boundaries for bounded Dev/Review lanes."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from typing import Any, Callable

import pytest

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agent_invocation import record_operator_stop
from agent_run.controller import Controller
from agent_run import cli
from agent_run.delivery import TicketDeliveryEngine
from agent_run.delivery_status import _multi_ticket_lines, status_progress_view
from agent_run.delivery_history import history_progress_view
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.git import GitRepository
from agent_run.guidance_actions import next_action_fact
from agent_run.executor_host import HostObservation
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.state import StateStore
from agent_run.state_contract import require_current_run_state
from agent_run.task_control import TaskControlBusyError
from agent_run.resume_intent import bind_resume_intent
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.ticket_development_state import TicketDevelopmentState
from agent_run.ticket_pause import request_ticket_pause
from agent_run.user_defaults import UserDefaultsStore
from support.cli_payloads import (
    human_blocker_step,
    passing_acceptance,
    publication,
    repair_acceptance,
)
from support.seeding import seed_run, write_fixture
from support.cli import run_cli, stdout_json
from support.inprocess_cli import invoke_cli_inprocess
from support.workspace import managed_repo, managed_state


class _TicketAgents:
    def __init__(
        self, root: Path, numbers: tuple[int, ...],
        before_develop: Callable[[int], None],
        before_publication: Callable[[int], None] | None = None,
        before_review: Callable[[int], None] | None = None,
    ) -> None:
        self.before_develop = before_develop
        self.before_publication = before_publication
        self.before_review = before_review
        self.backends: dict[int, FixtureAgentBackend] = {}
        for number in numbers:
            path = root / f"ticket-{number}-agents.json"
            path.write_text(json.dumps({
                "developments": [{
                    "thread_id": f"developer-{number}",
                    "summary": f"Ticket {number}",
                    "write_files": {f"ticket-{number}.txt": "done\n"},
                }],
                "reviews": [passing_acceptance(
                    f"reviewer-{number}", f"Ticket {number} passed."
                )],
                "publications": [{**publication(), "thread_id": f"developer-{number}"}],
            }), encoding="utf-8")
            self.backends[number] = FixtureAgentBackend(path)

    @staticmethod
    def _number(request: dict[str, Any]) -> int:
        return int(request["ticket"]["number"])

    def develop(self, request: dict[str, Any]) -> Any:
        number = self._number(request)
        self.before_develop(number)
        return self.backends[number].develop(request)

    def review(self, request: dict[str, Any]) -> Any:
        number = self._number(request)
        if self.before_review is not None:
            self.before_review(number)
        return self.backends[number].review(request)

    def publication(self, request: dict[str, Any]) -> Any:
        number = self._number(request)
        if self.before_publication is not None:
            self.before_publication(number)
        return self.backends[number].publication(request)


def _run(
    git_repo: Path, numbers: tuple[int, ...],
    *, blocked_by: dict[int, list[int]] | None = None,
    workers: int = 3,
    ci_repairs_per_round: int = 3,
) -> tuple[dict[str, Any], StateStore, GitRepository, Path]:
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): {
            "number": number,
            "title": f"Deliver ticket {number}",
            "body": f"Implement ticket {number}.",
            "state": "OPEN",
            "labels": ["ready-for-agent"],
            "blocked_by": [
                {"number": blocker, "state": "OPEN"}
                for blocker in (blocked_by or {}).get(number, [])
            ],
        }
        for number in numbers
    })
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(
        FixtureGitHubReader(fixture), git, states,
        delivery_policy=DeliveryPolicy(
            ticket_development_workers=workers,
            ci_repairs_per_round=ci_repairs_per_round,
        ),
    )
    state, _ = controller.start(1)
    return state, states, git, fixture


def _engine(
    git: GitRepository, states: StateStore, fixture: Path,
    agents: _TicketAgents,
    *, development_only: bool = False,
) -> TicketDeliveryEngine:
    return TicketDeliveryEngine(
        git=git, states=states,
        github=FixtureGitHubPublisher(fixture, git),
        agents=agents,
        development_only=development_only,
    )


def test_fixture_revision_read_does_not_erase_parallel_ticket_branch(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _, git, fixture = _run(git_repo, (2, 3))
    publisher = FixtureGitHubPublisher(fixture, git)
    read_complete, release_read = Event(), Event()
    original = FixtureGitHubReader.delivery_graph

    def delayed_graph(reader: FixtureGitHubReader, parent_number: int) -> Any:
        graph = original(reader, parent_number)
        read_complete.set()
        assert release_read.wait(10)
        return graph

    monkeypatch.setattr(FixtureGitHubReader, "delivery_graph", delayed_graph)
    errors: list[BaseException] = []

    def read_revision() -> None:
        try:
            publisher.current_effective_revision(
                parent_number=1, ticket_number=2, expected_revision="",
            )
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=read_revision)
    thread.start()
    try:
        assert read_complete.wait(10)
        base_sha = git.resolve(state["run_branch"])
        for number in (2, 3):
            if number == 3:
                release_read.set()
                thread.join(timeout=10)
                assert not thread.is_alive()
                assert not errors, errors
            publisher.ensure_ticket_branch(
                ticket_number=number,
                branch=f"agent-run/test/ticket-{number}",
                base_branch=state["run_branch"],
                expected_base_sha=base_sha,
                expected_remote_sha=base_sha,
                recovery_remote_sha=base_sha,
            )
    finally:
        release_read.set()
        thread.join(timeout=10)
    heads = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["published_branches"]
    assert heads["agent-run/test/ticket-2"] == base_sha
    assert heads["agent-run/test/ticket-3"] == base_sha


def test_independent_ticket_development_overlaps_and_merges_state(git_repo: Path) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    barrier = Barrier(2)
    agents = _TicketAgents(git_repo, (2, 3), lambda _number: barrier.wait(timeout=10))
    engine = _engine(git, states, fixture, agents)

    assert engine.develop_frontier(str(state["run_id"]), state)

    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert {number: saved["ticket_jobs"][str(number)]["phase"] for number in (2, 3)} == {
        2: "ready_for_integration", 3: "ready_for_integration",
    }
    assert {entry["work_subject"] for entry in saved["agent_invocation_history"]} == {
        "ticket:2", "ticket:3",
    }
    assert set(saved["ticket_agent_invocations"]) == {"2", "3"}
    assert len(saved["ticket_integration_queue"]) == 2
    assert saved["ticket_integration_holder"] is None


def test_one_ticket_worker_failure_pauses_only_that_ticket(git_repo: Path) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))

    def fail_first(number: int) -> None:
        if number == 2:
            raise RuntimeError("Ticket 2 Worker exited")

    agents = _TicketAgents(git_repo, (2, 3), fail_first)
    assert _engine(git, states, fixture, agents).develop_frontier(
        str(state["run_id"]), state
    )
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"]["2"]["phase"] == "blocked"
    assert saved["ticket_jobs"]["2"]["blocked_reason"] == "ticket_execution_failed"
    assert saved["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    assert bind_resume_intent(saved, ticket_number=2)["authorization"] == "continue_ticket"
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    resumed, _ = controller.resume(
        str(state["run_id"]), explicit_resume=True, ticket_number=2
    )
    assert resumed["ticket_jobs"]["2"]["phase"] == "developing"
    assert resumed["ticket_jobs"]["3"]["phase"] == "ready_for_integration"


@pytest.mark.parametrize("second_fails", [False, True])
def test_branch_failure_refresh_preserves_running_lane(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, second_fails: bool,
) -> None:
    state, states, git, fixture = _run(
        git_repo, (2, 3, 4), blocked_by={4: [2, 3]},
    )
    entered = Event()
    release = Event()
    refreshes: list[dict[str, Any]] = []
    preparation_states: list[dict[str, Any]] = []
    original_branch = FixtureGitHubPublisher.ensure_ticket_branch

    def prepare_branch(publisher: FixtureGitHubPublisher, **kwargs: Any) -> None:
        if kwargs["ticket_number"] == 2:
            current = states.load_current_run(str(state["run_id"]))
            assert current is not None
            preparation_states.append(current)
            raise RuntimeError("Ticket 2 branch preparation failed")
        original_branch(publisher, **kwargs)

    monkeypatch.setattr(FixtureGitHubPublisher, "ensure_ticket_branch", prepare_branch)

    def develop(number: int) -> None:
        if number == 3:
            entered.set()
            assert release.wait(timeout=10)
            if second_fails:
                raise RuntimeError("Ticket 3 Worker exited")

    def after_lane(lanes: frozenset[int]) -> None:
        if 3 not in lanes:
            return
        assert entered.wait(timeout=10)
        refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
            str(state["run_id"])
        )
        refreshes.append(refreshed)
        release.set()

    try:
        assert _engine(
            git, states, fixture, _TicketAgents(git_repo, (2, 3, 4), develop)
        ).develop_frontier(str(state["run_id"]), state, after_lane=after_lane)
    finally:
        release.set()
    assert refreshes, "Controller did not refresh after the failed lane"
    assert preparation_states
    preparing = preparation_states[0]
    assert preparing["ticket_jobs"]["2"]["phase"] == "developing"
    assert "2" not in preparing["ticket_agent_invocations"]
    preparing_view = status_progress_view(preparing, {
        "status": preparing["status"],
        "executor_control": {"activity": "not_running"},
    })
    preparing_progress = preparing_view["multi_ticket"]
    assert preparing_progress["tickets"][0]["phase"] == "developing"
    assert preparing_progress["executor_activity"] == "not_running"
    assert preparing_view["execution_activity"] == "interrupted"
    assert status_progress_view(preparing, {
        "status": preparing["status"],
        "executor_control": {"activity": "unknown"},
    })["execution_activity"] == "unknown"
    assert next_action_fact({
        **preparing, "_executor_control": {"activity": "not_running"},
    }).command == "resume"
    assert history_progress_view(preparing, {
        "executor_control": {"activity": "not_running"},
    })["execution_activity"] == "interrupted"
    assert history_progress_view(preparing, {
        "executor_control": {"activity": "unknown"},
    })["execution_activity"] == "unknown"
    for activity in ("running", "unknown"):
        assert next_action_fact({
            **preparing, "_executor_control": {"activity": activity},
        }).command == "run"
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"]["2"]["blocked_reason"] == "ticket_execution_failed"
    assert saved["ticket_jobs"]["3"]["phase"] == (
        "blocked" if second_fails else "ready_for_integration"
    )
    assert saved["ticket_jobs"].get("4", {}).get("phase") is None
    assert 4 not in saved["frontier"]
    assert saved["status"] not in {"execution_failed", "operator_stopped"}
    require_current_run_state(saved)
    before_query = json.dumps(saved, sort_keys=True)
    for executor_activity, expected in (
        ("not_running", "执行器：已退出"),
        ("running", "执行器：正在运行"),
        ("unknown", "执行器：无法确认"),
    ):
        audit = {
            "status": saved["status"],
            "executor_control": {"activity": executor_activity},
        }
        progress = status_progress_view(saved, audit)
        multi = progress["multi_ticket"]
        first = next(item for item in multi["tickets"] if item["ticket_number"] == 2)
        assert first["phase"] == "blocked"
        assert first["invocation"] is None
        assert any(expected in line for line in _multi_ticket_lines(progress))
        if executor_activity == "not_running":
            assert multi["development_workers_active"] == 0
        if executor_activity == "unknown":
            assert multi["development_workers_active"] is None
    assert json.dumps(saved, sort_keys=True) == before_query


def test_branch_preparation_status_queries_observe_real_control_without_invocation(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={
        "2": {
            "number": 2, "title": "Ticket 2", "body": "Deliver Ticket 2.",
            "state": "OPEN", "labels": ["ready-for-agent"], "blocked_by": [],
        },
    })
    seeded = seed_run(git_repo, fixture, "1", idle_control=True)
    assert seeded.returncode == 0, seeded.stderr
    run_id = stdout_json(seeded)["run_id"]
    states = StateStore(managed_state(git_repo))
    control_path = next((states.root / "task-control").glob("*.json"))
    run_path = states.runs_directory / f"{run_id}.json"
    evidence_path = git_repo / "branch-preparation-observed.json"
    observed: list[str] = []

    def inspect_preparation(_publisher: FixtureGitHubPublisher, **_kwargs: Any) -> None:
        preparing = states.load_current_run(run_id)
        assert preparing is not None
        assert preparing["ticket_jobs"]["2"]["phase"] == "developing"
        assert "2" not in preparing["ticket_agent_invocations"]
        original_control = control_path.read_bytes()
        original_run = run_path.read_bytes()
        try:
            for activity in ("not_running", "running", "unknown"):
                if activity == "unknown":
                    control_path.unlink()
                else:
                    control = json.loads(original_control)
                    control["executor"].update(
                        status="exited" if activity == "not_running" else "running",
                        pid=123 if activity == "running" else control["executor"].get("pid"),
                        handshake_at="2026-09-27T00:00:00+00:00",
                    )
                    control_path.write_text(json.dumps(control), encoding="utf-8")
                with monkeypatch.context() as host_patch:
                    if activity == "running":
                        host_patch.setattr(
                            cli, "observe_systemd_executor",
                            lambda spec, *args, **kwargs: HostObservation(
                                "running", spec.generation, 123, True
                            ),
                        )
                    before_control = (
                        control_path.read_bytes() if control_path.exists() else None
                    )
                    for command in ("status", "history"):
                        result = invoke_cli_inprocess(
                            git_repo, fixture, command, run_id, "--json"
                        )
                        assert result.returncode == 0, result.stderr
                        view = stdout_json(result)
                        assert view["executor_control"]["activity"] == activity
                        if command == "status":
                            assert view["progress"]["multi_ticket"]["tickets"][0]["invocation"] is None
                            assert view["progress"]["execution_activity"] == {
                                "not_running": "interrupted",
                                "running": "not_running",
                                "unknown": "unknown",
                            }[activity]
                            if activity == "not_running":
                                assert view["next_action"].startswith("agent-run resume ")
                        else:
                            assert view["execution_activity"] == {
                                "not_running": "interrupted",
                                "running": "not_running",
                                "unknown": "unknown",
                            }[activity]
                    assert run_path.read_bytes() == original_run
                    assert (control_path.read_bytes() if control_path.exists() else None) == before_control
                    observed.append(activity)
        finally:
            control_path.write_bytes(original_control)
        evidence_path.write_text(json.dumps(observed), encoding="utf-8")
        raise RuntimeError("branch preparation probe complete")

    monkeypatch.setattr(
        FixtureGitHubPublisher, "ensure_ticket_branch", inspect_preparation
    )
    agents = git_repo / "branch-preparation-agents.json"
    agents.write_text(json.dumps({"developments": [], "reviews": [], "publications": []}), encoding="utf-8")
    started = invoke_cli_inprocess(
        git_repo, fixture, "run", "1", "--agent-fixture", str(agents), "--json"
    )
    assert started.returncode == 2
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == [
        "not_running", "running", "unknown",
    ], started.stdout + started.stderr


@pytest.mark.parametrize("changed", ["job", "invocation", "demand", "global_stop"])
@pytest.mark.parametrize("failure", [False, True])
def test_lane_rejects_real_concurrent_changes(
    git_repo: Path, changed: str, failure: bool,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    if changed == "invocation":
        agents = _TicketAgents(git_repo, (2, 3), lambda _number: None)
        assert _engine(git, states, fixture, agents).develop_frontier(
            str(state["run_id"]), state
        )
    run_id = str(state["run_id"])
    lane = TicketDevelopmentState(states, run_id, 2)
    snapshot = lane.load_current_run(run_id)
    assert snapshot is not None
    latest = states.load_current_run(run_id)
    assert latest is not None
    if changed == "job":
        latest["ticket_jobs"]["2"]["base_sha"] = "concurrent-head"
        latest["active_ticket_job"] = latest["ticket_jobs"]["2"]
    elif changed == "invocation":
        latest["ticket_agent_invocations"]["2"]["concurrent_marker"] = "changed"
        if latest["active_ticket_job"]["ticket_number"] == 2:
            latest["active_agent_invocation"] = latest["ticket_agent_invocations"]["2"]
    elif changed == "demand":
        latest["ticket_graph"]["tickets"]["2"]["content_revision"] = "changed"
    else:
        latest["status"] = "operator_stopped"
    states.save_run(run_id, latest)
    with pytest.raises(TaskControlBusyError):
        if failure:
            lane.block_execution_failure(RuntimeError("Worker exited"))
        else:
            lane.save_run(run_id, snapshot)
    saved = states.load_current_run(run_id)
    assert saved is not None
    assert saved["status"] == latest["status"]


@pytest.mark.parametrize("failure", [False, True])
def test_lane_preserves_documentation_label_refresh(
    git_repo: Path, failure: bool,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    run_id = str(state["run_id"])
    started, other_finished, release = Event(), Event(), Event()

    def develop(number: int) -> None:
        if number == 2:
            started.set()
            assert release.wait(timeout=10)
            if failure:
                raise RuntimeError("Worker exited")

    agents = _TicketAgents(git_repo, (2, 3), develop)
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            assert _engine(git, states, fixture, agents).develop_frontier(
                run_id, state,
                after_lane=lambda lanes: other_finished.set()
                if lanes == frozenset({2}) else None,
            )
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert started.wait(timeout=10)
        assert other_finished.wait(timeout=10)
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        fixture_data["issues"]["2"]["labels"].append("documentation")
        fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
        refreshed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(run_id)
        assert refreshed["ticket_graph"]["tickets"]["2"]["labels"] == [
            "documentation", "ready-for-agent",
        ]
        assert refreshed["ticket_graph"]["tickets"]["2"]["eligibility"]["eligible"]
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors

    saved = states.load_current_run(run_id)
    assert saved is not None
    assert saved["ticket_graph"]["tickets"]["2"]["labels"] == [
        "documentation", "ready-for-agent",
    ]
    assert saved["ticket_jobs"]["2"]["phase"] == (
        "blocked" if failure else "ready_for_integration"
    )
    if failure:
        assert saved["ticket_jobs"]["2"]["blocked_reason"] == "ticket_execution_failed"


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("labels", [["documentation"], ["ready-for-agent", "needs-info"]])
def test_lane_rejects_label_qualification_change_after_refresh(
    git_repo: Path, failure: bool, labels: list[str],
) -> None:
    state, states, _, fixture = _run(git_repo, (2, 3))
    run_id = str(state["run_id"])
    lane = TicketDevelopmentState(states, run_id, 2)
    snapshot = lane.load_current_run(run_id)
    assert snapshot is not None
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data["issues"]["2"]["labels"] = labels
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")

    refreshed, _ = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states,
    ).resume(run_id)
    assert not refreshed["ticket_graph"]["tickets"]["2"]["eligibility"]["eligible"]
    with pytest.raises(TaskControlBusyError):
        if failure:
            lane.block_execution_failure(RuntimeError("Worker exited"))
        else:
            lane.save_run(run_id, snapshot)


def test_local_failure_remains_resumable_after_other_ticket_completes(
    git_repo: Path,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))

    def fail_first(number: int) -> None:
        if number == 2:
            raise RuntimeError("Ticket 2 Worker exited")

    agents = _TicketAgents(git_repo, (2, 3), fail_first)
    engine = _engine(git, states, fixture, agents)
    assert engine.develop_frontier(str(state["run_id"]), state)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    ready, _ = controller.resume(str(state["run_id"]))
    DeliveryRunEngine(controller=controller, tickets=engine).deliver_from_state(
        str(state["run_id"]), ready,
    )
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"]["2"]["blocked_reason"] == "ticket_execution_failed"
    assert saved["ticket_jobs"]["3"]["phase"] == "completed"
    require_current_run_state(saved)
    restarted = Controller(FixtureGitHubReader(fixture), git, StateStore(states.root))
    observed, _ = restarted.resume(str(state["run_id"]))
    assert observed["ticket_jobs"]["2"]["blocked_reason"] == "ticket_execution_failed"
    resumed, _ = restarted.resume(
        str(state["run_id"]), explicit_resume=True, ticket_number=2,
    )
    assert resumed["ticket_jobs"]["2"]["phase"] == "developing"


def test_ticket_stop_pauses_only_queued_ticket_and_resumes(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): {
            "number": number, "title": f"Ticket {number}",
            "body": f"Implement {number}.", "state": "OPEN",
            "labels": ["ready-for-agent"], "blocked_by": [],
        }
        for number in (2, 3)
    })
    seeded = seed_run(git_repo, fixture, "1", idle_control=True)
    assert seeded.returncode == 0
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    state = states.load_current_run(stdout_json(seeded)["run_id"])
    assert state is not None
    agents = _TicketAgents(git_repo, (2, 3), lambda _number: None)
    assert _engine(git, states, fixture, agents).develop_frontier(
        str(state["run_id"]), state
    )

    stopped = run_cli(git_repo, fixture, "stop", "1", "--ticket", "2", "--json")
    assert stopped.returncode == 0, stopped.stdout
    assert stdout_json(stopped)["ticket_phase"] == "blocked"
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"]["2"]["blocked_reason"] == "ticket_paused"
    assert saved["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    with pytest.raises(ValueError, match="no active delivery work"):
        request_ticket_pause(states, str(state["run_id"]), 2)
    assert bind_resume_intent(saved, ticket_number=2)["authorization"] == "continue_ticket"
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    resumed, _ = controller.resume(
        str(state["run_id"]), explicit_resume=True, ticket_number=2
    )
    assert resumed["ticket_jobs"]["2"]["phase"] == "ready_for_integration"
    assert resumed["ticket_jobs"]["3"]["phase"] == "ready_for_integration"


@pytest.mark.parametrize("run_language", ["zh", "en"])
def test_ticket_pause_receipts_use_frozen_run_language(
    git_repo: Path, run_language: str,
) -> None:
    UserDefaultsStore().configure(language=run_language)
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): {
            "number": number, "title": f"Ticket {number}",
            "body": f"Implement {number}.", "state": "OPEN",
            "labels": ["ready-for-agent"], "blocked_by": [],
        }
        for number in (2, 3)
    })
    seeded = seed_run(git_repo, fixture, "1", idle_control=True)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    state = states.load_current_run(stdout_json(seeded)["run_id"])
    assert state is not None
    UserDefaultsStore().configure(language="en" if run_language == "zh" else "zh")
    help_result = invoke_cli_inprocess(git_repo, None, "stop", "--help")
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert (
        "Pause only the selected Ticket" if run_language == "zh"
        else "只暂停指定 Ticket"
    ) in help_result.stdout
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    agents = _TicketAgents(git_repo, (2, 3), lambda _number: None)
    engine = _engine(git, states, fixture, agents)
    assert engine.develop_frontier(str(state["run_id"]), state)
    ready, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(str(state["run_id"]))
    held_number = ready["ticket_integration_queue"][0]["ticket_number"]
    queued_number = 3 if held_number == 2 else 2
    engine.deliver(str(state["run_id"]))
    waiting = states.load_current_run(str(state["run_id"]))
    assert waiting is not None
    assert waiting["ticket_jobs"][str(held_number)]["phase"] == "waiting_checks"

    requested_json = invoke_cli_inprocess(
        git_repo, fixture, "stop", "1", "--ticket", str(held_number), "--json",
    )
    assert requested_json.returncode == 0, requested_json.stdout + requested_json.stderr
    receipt = stdout_json(requested_json)
    assert set(receipt) == {
        "run_id", "status", "ticket_number", "pause_requested", "ticket_phase",
    }
    assert receipt["pause_requested"] is True
    assert receipt["ticket_phase"] == "waiting_checks"
    requested_plain = invoke_cli_inprocess(
        git_repo, fixture, "stop", "1", "--ticket", str(held_number),
    )
    assert requested_plain.returncode == 0, requested_plain.stdout + requested_plain.stderr
    assert requested_plain.stdout.strip() == (
        f"Ticket #{held_number}：已请求暂停" if run_language == "zh"
        else f"Ticket #{held_number}: pause requested"
    )
    paused_plain = invoke_cli_inprocess(
        git_repo, fixture, "stop", "1", "--ticket", str(queued_number),
    )
    assert paused_plain.returncode == 0, paused_plain.stdout + paused_plain.stderr
    assert paused_plain.stdout.strip() == (
        f"Ticket #{queued_number}：已暂停" if run_language == "zh"
        else f"Ticket #{queued_number}: paused"
    )
    paused = states.load_current_run(str(state["run_id"]))
    assert paused is not None
    assert paused["ticket_jobs"][str(queued_number)]["blocked_reason"] == "ticket_paused"


def test_pause_request_waits_for_active_development_boundary(git_repo: Path) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    entered = Event()
    release = Event()

    def hold(number: int) -> None:
        if number == 2:
            entered.set()
            assert release.wait(timeout=10)

    agents = _TicketAgents(git_repo, (2, 3), hold)
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            assert _engine(git, states, fixture, agents).develop_frontier(
                str(state["run_id"]), state
            )
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert entered.wait(timeout=10)
        paused = request_ticket_pause(states, str(state["run_id"]), 2)
        assert "2" in paused["ticket_pause_requests"]
        assert paused["ticket_jobs"]["2"]["phase"] == "developing"
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"]["2"]["blocked_reason"] == "ticket_paused"
    assert saved["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    assert "2" not in saved["ticket_pause_requests"]


@pytest.mark.parametrize("held_phase", ["accepted", "waiting_checks"])
def test_pausing_integration_holder_requeues_original_phase(
    git_repo: Path, held_phase: str
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    agents = _TicketAgents(git_repo, (2,), lambda _number: None)
    _engine(git, states, fixture, agents, development_only=True).deliver(
        str(state["run_id"])
    )
    state = states.load_current_run(str(state["run_id"]))
    assert state is not None
    job = state["ticket_jobs"]["2"]
    job["phase"] = "accepted"
    state["active_ticket_job"] = job
    state["ticket_integration_holder"] = 2
    state["ticket_integration_queue"] = []
    states.save_run(str(state["run_id"]), state)
    if held_phase == "waiting_checks":
        fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
        fixture_data.setdefault("delivery", {})["required_checks"] = ["pending"]
        fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
        _engine(git, states, fixture, agents).deliver(str(state["run_id"]))
        waiting = states.load_current_run(str(state["run_id"]))
        assert waiting is not None
        assert waiting["ticket_jobs"]["2"]["phase"] == "waiting_checks"

    request_ticket_pause(states, str(state["run_id"]), 2)
    _engine(git, states, fixture, agents).deliver(str(state["run_id"]))
    paused = states.load_current_run(str(state["run_id"]))
    assert paused is not None
    assert paused["ticket_jobs"]["2"]["blocked_reason"] == "ticket_paused"
    assert paused["ticket_integration_holder"] is None

    controller = Controller(FixtureGitHubReader(fixture), git, states)
    resumed, _ = controller.resume(
        str(state["run_id"]), explicit_resume=True, ticket_number=2
    )
    assert resumed["ticket_jobs"]["2"]["phase"] == "ready_for_integration"
    assert resumed["ticket_jobs"]["2"]["integration_resume_phase"] == held_phase
    assert resumed["ticket_integration_holder"] is None
    assert resumed["ticket_integration_queue"][0]["ticket_number"] == 2
    require_current_run_state(resumed)


def test_completed_lane_drives_integration_while_another_lane_is_running(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    first_ready = Event()
    release_second = Event()
    resume_loaded = Event()
    second_save_attempted = Event()
    allow_resume_save = Event()
    observed: list[tuple[str, str]] = []

    original_save = TicketDevelopmentState.save_run

    def track_save(
        scoped: TicketDevelopmentState, run_id: str, current: dict[str, Any]
    ) -> None:
        if scoped.ticket_number == 3 and release_second.is_set():
            second_save_attempted.set()
        original_save(scoped, run_id, current)

    monkeypatch.setattr(TicketDevelopmentState, "save_run", track_save)

    class ControllerView:
        def resume(self, run_id: str) -> tuple[dict[str, Any], bool]:
            current = states.load_current_run(run_id)
            assert current is not None
            if not resume_loaded.is_set():
                resume_loaded.set()
                assert second_save_attempted.wait(timeout=10)
                assert allow_resume_save.wait(timeout=10)
            states.save_run(run_id, current)
            return current, True

    class IntegrationView:
        def deliver(self, run_id: str) -> dict[str, Any]:
            current = states.load_current_run(run_id)
            assert current is not None
            observed.append((
                current["ticket_jobs"]["2"]["phase"],
                current["ticket_jobs"].get("3", {}).get("phase", "new"),
            ))
            return current

    orchestrator = DeliveryRunEngine(
        controller=ControllerView(), tickets=IntegrationView(),  # type: ignore[arg-type]
    )

    def hold(number: int) -> None:
        if number == 3:
            assert release_second.wait(timeout=10)

    def after_lane(lanes: frozenset[int]) -> None:
        orchestrator._integrate_ready_ticket(str(state["run_id"]), lanes)
        first_ready.set()

    agents = _TicketAgents(git_repo, (2, 3), hold)
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            assert _engine(git, states, fixture, agents).develop_frontier(
                str(state["run_id"]), state, after_lane=after_lane
            )
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert resume_loaded.wait(timeout=10)
        release_second.set()
        assert second_save_attempted.wait(timeout=10)
        allow_resume_save.set()
        assert first_ready.wait(timeout=10)
        assert observed[0] == ("ready_for_integration", "developing")
    finally:
        release_second.set()
        allow_resume_save.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"]["3"]["phase"] == "ready_for_integration"


def test_real_orchestrator_reaches_ci_while_other_ticket_develops(
    git_repo: Path,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    reached_ci = Event()
    release_second = Event()

    def hold(number: int) -> None:
        if number == 3:
            assert release_second.wait(timeout=10)

    def observe(current: dict[str, Any]) -> None:
        job = current["ticket_jobs"].get("2")
        if isinstance(job, dict) and job.get("phase") == "waiting_checks":
            reached_ci.set()

    states.run_saved_observer = observe
    agents = _TicketAgents(git_repo, (2, 3), hold)
    orchestrator = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=_engine(git, states, fixture, agents),
    )
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            orchestrator.deliver_from_state(str(state["run_id"]), state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert reached_ci.wait(timeout=10)
        current = states.load_current_run(str(state["run_id"]))
        assert current is not None
        assert current["ticket_integration_holder"] == 2
        assert current["ticket_jobs"]["3"]["phase"] == "developing"
    finally:
        release_second.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    assert saved["ticket_integration_holder"] == 2


def test_other_ticket_reviews_while_publication_agent_is_waiting(
    git_repo: Path,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    publication_entered = Event()
    release_publication = Event()
    release_second = Event()
    second_ready = Event()

    def develop(number: int) -> None:
        if number == 3:
            assert release_second.wait(timeout=10)

    def publication_gate(number: int) -> None:
        if number == 2:
            publication_entered.set()
            assert release_publication.wait(timeout=10)

    def observe(current: dict[str, Any]) -> None:
        second = current["ticket_jobs"].get("3")
        if isinstance(second, dict) and second.get("phase") == "ready_for_integration":
            second_ready.set()

    states.run_saved_observer = observe
    agents = _TicketAgents(
        git_repo, (2, 3), develop, before_publication=publication_gate,
    )
    orchestrator = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=_engine(git, states, fixture, agents),
    )
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            orchestrator.deliver_from_state(str(state["run_id"]), state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert publication_entered.wait(timeout=10)
        release_second.set()
        assert second_ready.wait(timeout=3), "Ticket 3 could not save during Publication"
        concurrent = states.load_current_run(str(state["run_id"]))
        assert concurrent is not None
        assert concurrent["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
        assert concurrent["ticket_integration_holder"] == 2
    finally:
        release_second.set()
        release_publication.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    assert saved["ticket_jobs"]["2"]["phase"] == "waiting_checks"
    assert saved["ticket_integration_queue"][0]["ticket_number"] == 3
    assert {"2", "3"} <= set(saved["ticket_agent_invocations"])
    assert {"ticket:2", "ticket:3"} <= {
        entry["work_subject"] for entry in saved["agent_invocation_history"]
    }


def test_queued_ticket_pause_survives_another_ticket_publication_and_restart(
    git_repo: Path,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data.setdefault("delivery", {})["required_checks"] = ["pending"]
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    publication_entered = Event()
    release_publication = Event()
    published: list[int] = []
    held_number: int | None = None

    def publication_gate(number: int) -> None:
        published.append(number)
        if number == held_number:
            publication_entered.set()
            assert release_publication.wait(timeout=10)

    agents = _TicketAgents(
        git_repo, (2, 3), lambda _number: None,
        before_publication=publication_gate,
    )
    engine = _engine(git, states, fixture, agents)
    assert engine.develop_frontier(str(state["run_id"]), state)
    ready = states.load_current_run(str(state["run_id"]))
    assert ready is not None
    queued = [entry["ticket_number"] for entry in ready["ticket_integration_queue"]]
    assert set(queued) == {2, 3}
    branch_heads = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["published_branches"]
    assert {ready["ticket_jobs"][str(number)]["ticket_branch"] for number in (2, 3)} <= set(branch_heads)
    held_number, paused_number = queued
    orchestrator = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=engine,
    )
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            orchestrator.deliver_from_state(str(state["run_id"]), ready)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert publication_entered.wait(timeout=10), errors
        independent_store = StateStore(states.root)
        paused = request_ticket_pause(independent_store, str(state["run_id"]), paused_number)
        assert paused["ticket_jobs"][str(paused_number)]["blocked_reason"] == "ticket_paused"
        assert all(
            entry["ticket_number"] != paused_number
            for entry in paused["ticket_integration_queue"]
        )
    finally:
        release_publication.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_jobs"][str(paused_number)]["blocked_reason"] == "ticket_paused"
    assert saved["ticket_jobs"][str(paused_number)]["phase"] == "blocked"
    assert all(
        entry["ticket_number"] != paused_number
        for entry in saved["ticket_integration_queue"]
    )
    require_current_run_state(saved)
    restarted = Controller(
        FixtureGitHubReader(fixture), git, StateStore(states.root),
    )
    observed, _ = restarted.resume(str(state["run_id"]))
    assert observed["ticket_jobs"][str(paused_number)]["phase"] == "blocked"
    restarted_engine = DeliveryRunEngine(
        controller=restarted,
        tickets=_engine(git, StateStore(states.root), fixture, agents),
    )
    restarted_engine.deliver_from_state(str(state["run_id"]), observed)
    after_restart = states.load_current_run(str(state["run_id"]))
    assert after_restart is not None
    assert after_restart["ticket_jobs"][str(paused_number)]["blocked_reason"] == "ticket_paused"
    assert published == [held_number]
    explicitly_resumed, _ = restarted.resume(
        str(state["run_id"]), explicit_resume=True, ticket_number=paused_number,
    )
    assert explicitly_resumed["ticket_jobs"][str(paused_number)]["phase"] == "ready_for_integration"


@pytest.mark.parametrize("pause_queue_index", [0, 1])
def test_controller_refresh_keeps_queued_ticket_pause_from_graph_read(
    git_repo: Path, pause_queue_index: int,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    agents = _TicketAgents(git_repo, (2, 3), lambda _number: None)
    engine = _engine(git, states, fixture, agents)
    assert engine.develop_frontier(str(state["run_id"]), state)
    ready = states.load_current_run(str(state["run_id"]))
    assert ready is not None
    queued = [entry["ticket_number"] for entry in ready["ticket_integration_queue"]]
    assert set(queued) == {2, 3}
    branch_heads = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]["published_branches"]
    assert {ready["ticket_jobs"][str(number)]["ticket_branch"] for number in (2, 3)} <= set(branch_heads)
    paused_number = queued[pause_queue_index]
    graph_entered = Event()
    release_graph = Event()

    class DelayedGraphReader(FixtureGitHubReader):
        def delivery_graph(self, parent_number: int) -> Any:
            graph_entered.set()
            assert release_graph.wait(timeout=10)
            return super().delivery_graph(parent_number)

    controller = Controller(DelayedGraphReader(fixture), git, states)
    result: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def refresh() -> None:
        try:
            refreshed, _ = controller.resume(str(state["run_id"]))
            result.append(refreshed)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=refresh)
    thread.start()
    try:
        assert graph_entered.wait(timeout=10), errors
        independent_store = StateStore(states.root)
        paused = request_ticket_pause(independent_store, str(state["run_id"]), paused_number)
        assert paused["ticket_jobs"][str(paused_number)]["blocked_reason"] == "ticket_paused"
    finally:
        release_graph.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    assert result
    refreshed = result[0]
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    for current in (refreshed, saved):
        assert current["ticket_jobs"][str(paused_number)]["blocked_reason"] == "ticket_paused"
        assert current["ticket_jobs"][str(paused_number)]["phase"] == "blocked"
        assert all(
            entry["ticket_number"] != paused_number
            for entry in current["ticket_integration_queue"]
        )
    require_current_run_state(saved)

    publications: list[int] = []
    agents.before_publication = publications.append
    DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, StateStore(states.root)),
        tickets=engine,
    ).deliver_from_state(str(state["run_id"]), refreshed)
    assert paused_number not in publications
    restarted = Controller(FixtureGitHubReader(fixture), git, StateStore(states.root))
    observed, _ = restarted.resume(str(state["run_id"]))
    assert observed["ticket_jobs"][str(paused_number)]["blocked_reason"] == "ticket_paused"
    explicitly_resumed, _ = restarted.resume(
        str(state["run_id"]), explicit_resume=True, ticket_number=paused_number,
    )
    assert explicitly_resumed["ticket_jobs"][str(paused_number)]["phase"] == "ready_for_integration"


def test_controller_refresh_does_not_reopen_concurrent_operator_stop(
    git_repo: Path,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    entered = Event()
    release = Event()

    class DelayedGraphReader(FixtureGitHubReader):
        def delivery_graph(self, parent_number: int) -> Any:
            entered.set()
            assert release.wait(timeout=10)
            return super().delivery_graph(parent_number)

    controller = Controller(DelayedGraphReader(fixture), git, states)
    result: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def refresh() -> None:
        try:
            observed, _ = controller.resume(str(state["run_id"]))
            result.append(observed)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=refresh)
    thread.start()
    try:
        assert entered.wait(timeout=10), errors
        independent_store = StateStore(states.root)
        current = independent_store.load_current_run(str(state["run_id"]))
        assert current is not None
        record_operator_stop(
            current,
            save=lambda stopped: independent_store.save_run(str(state["run_id"]), stopped),
        )
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    assert result and result[0]["status"] == "operator_stopped"
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["status"] == "operator_stopped"
    require_current_run_state(saved)


def test_controller_refresh_reselects_sibling_after_local_human_gate(
    git_repo: Path,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    entered = Event()
    release = Event()

    class DelayedGraphReader(FixtureGitHubReader):
        def delivery_graph(self, parent_number: int) -> Any:
            entered.set()
            assert release.wait(timeout=10)
            return super().delivery_graph(parent_number)

    controller = Controller(DelayedGraphReader(fixture), git, states)
    result: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def refresh() -> None:
        try:
            observed, _ = controller.resume(str(state["run_id"]))
            result.append(observed)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=refresh)
    thread.start()
    try:
        assert entered.wait(timeout=10), errors
        agents = _TicketAgents(git_repo, (2, 3), lambda _number: None)
        agents.backends[2].data["developments"] = [human_blocker_step("developer-2")]
        assert _engine(git, StateStore(states.root), fixture, agents).develop_frontier(
            str(state["run_id"]), state,
        )
        concurrent = states.load_current_run(str(state["run_id"]))
        assert concurrent is not None
        assert concurrent["status"] == "ready_for_human"
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    assert result
    observed = result[0]
    assert observed["ticket_jobs"]["2"]["blocked_reason"] == "agent_requires_human"
    assert observed["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    assert observed["status"] != "ready_for_human"
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["status"] == observed["status"]
    publications: list[int] = []
    agents.before_publication = publications.append
    DeliveryRunEngine(
        controller=controller, tickets=_engine(git, states, fixture, agents),
    ).deliver_from_state(str(state["run_id"]), observed)
    assert publications == [3]


def test_controller_refresh_observes_operator_stop_after_its_save(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    saved_by_controller = Event()
    stopped = Event()
    states.run_saved_observer = lambda _state: saved_by_controller.set()
    original_load = states.load_run

    def load_with_stop(run_id: str) -> dict[str, Any] | None:
        if saved_by_controller.is_set() and not stopped.is_set():
            stopped.set()
            independent_store = StateStore(states.root)
            current = independent_store.load_current_run(run_id)
            assert current is not None
            record_operator_stop(
                current, save=lambda value: independent_store.save_run(run_id, value),
            )
        return original_load(run_id)

    monkeypatch.setattr(states, "load_run", load_with_stop)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    observed, _ = controller.resume(str(state["run_id"]))
    assert saved_by_controller.is_set() and stopped.is_set()
    assert observed["status"] == "operator_stopped"
    current = states.load_current_run(str(state["run_id"]))
    assert current is not None
    assert current["status"] == "operator_stopped"


def test_pause_during_exhausted_ci_review_keeps_budget_gate(git_repo: Path) -> None:
    state, states, git, fixture = _run(
        git_repo, (2,), ci_repairs_per_round=1,
    )
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))
    fixture_data.setdefault("delivery", {}).update({
        "required_checks": ["fail", "fail", "pending"],
        "required_check_evidence": {
            "pr_number": 1,
            "checks": [{
                "name": "quality", "workflow": "CI", "bucket": "fail",
                "state": "FAILURE", "description": "The configured test step failed.",
                "link": "https://example.invalid/checks/quality",
                "job": {
                    "head_sha": "$CURRENT_HEAD", "name": "quality",
                    "workflow_name": "CI", "status": "completed",
                    "conclusion": "failure", "steps": [{
                        "name": "Run tests", "status": "completed",
                        "conclusion": "failure", "number": 6,
                    }],
                },
            }],
        },
    })
    fixture.write_text(json.dumps(fixture_data), encoding="utf-8")
    review_entered = Event()
    release_review = Event()
    reviews = {2: 0}

    def review_gate(number: int) -> None:
        if number != 2:
            return
        reviews[2] += 1
        if reviews[2] == 2:
            review_entered.set()
            assert release_review.wait(timeout=10)

    agents = _TicketAgents(
        git_repo, (2,), lambda _number: None, before_review=review_gate,
    )
    path = git_repo / "ticket-2-agents.json"
    agent_data = json.loads(path.read_text(encoding="utf-8"))
    agent_data["developments"] = [
        {"thread_id": "developer-2",
         "expected_thread_id": None if index == 1 else "developer-2",
         "summary": f"Ticket 2 attempt {index}",
         "write_files": {"ticket-2.txt": f"attempt {index}\n"}}
        for index in range(1, 4)
    ]
    agent_data["reviews"] = [
        passing_acceptance("reviewer-2", "Demand passed."),
        repair_acceptance("reviewer-2-ci"),
    ]
    agent_data["publications"] = [
        {**publication(), "thread_id": "developer-2"} for _ in range(3)
    ]
    path.write_text(json.dumps(agent_data), encoding="utf-8")
    agents.backends[2] = FixtureAgentBackend(path)

    orchestrator = DeliveryRunEngine(
        controller=Controller(FixtureGitHubReader(fixture), git, states),
        tickets=_engine(git, states, fixture, agents),
    )
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            orchestrator.deliver_from_state(str(state["run_id"]), state)
            for _ in range(5):
                if review_entered.is_set():
                    break
                orchestrator.deliver(str(state["run_id"]))
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert review_entered.wait(timeout=10), f"second CI Reviewer did not start: {errors!r}"
        requested = request_ticket_pause(states, str(state["run_id"]), 2)
        assert "2" in requested["ticket_pause_requests"]
    finally:
        release_review.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    job = saved["ticket_jobs"]["2"]
    assert job["phase"] == "blocked"
    assert job["blocked_reason"] == "ci_inner_budget_exhausted"
    assert job["ci_recovery"]["checkpoint_reason"] == "ci_inner_budget_exhausted"
    assert "2" not in saved["ticket_pause_requests"]
    assert bind_resume_intent(saved, ticket_number=2)["authorization"] == "renew_ci_recovery"
    observed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        str(state["run_id"])
    )
    assert observed["ticket_jobs"]["2"]["blocked_reason"] == "ci_inner_budget_exhausted"
    assert observed["ticket_jobs"]["2"]["ci_recovery"]["repairs_used"] == 1


def test_one_worker_uses_serial_ticket_delivery(git_repo: Path) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3), workers=1)
    started: list[int] = []
    agents = _TicketAgents(git_repo, (2, 3), started.append)
    engine = _engine(git, states, fixture, agents, development_only=True)

    assert state["policy_snapshot"]["ticket_development_workers"] == 1
    assert engine.develop_frontier(str(state["run_id"]), state) is False
    assert started == []
    engine.deliver(str(state["run_id"]))

    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert started == [2]
    assert saved["ticket_jobs"]["2"]["phase"] == "ready_for_integration"
    assert "3" not in saved["ticket_agent_invocations"]


def test_default_capacity_starts_three_without_starting_fourth(
    git_repo: Path,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3, 4, 5))
    release = Event()
    all_started = Event()
    lock = Lock()
    started: list[int] = []

    def hold(number: int) -> None:
        with lock:
            started.append(number)
            if len(started) == 3:
                all_started.set()
        assert release.wait(timeout=10), "development gate was not released"

    agents = _TicketAgents(git_repo, (2, 3, 4, 5), hold)
    engine = _engine(git, states, fixture, agents)
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            assert engine.develop_frontier(str(state["run_id"]), state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert all_started.wait(timeout=10), "three development lanes did not start"
        with lock:
            assert set(started) == {2, 3, 4}
        current = states.load_current_run(str(state["run_id"]))
        assert current is not None
        assert "5" not in current["ticket_agent_invocations"]
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert all(
        saved["ticket_jobs"][str(number)]["phase"] == "ready_for_integration"
        for number in (2, 3, 4)
    )
    assert "5" not in saved["ticket_agent_invocations"]


def test_dependency_gate_and_reloaded_ticket_projections(git_repo: Path) -> None:
    state, states, git, fixture = _run(
        git_repo, (2, 3, 4), blocked_by={3: [2]},
    )
    assert state["frontier"] == [2, 4]
    barrier = Barrier(2)
    agents = _TicketAgents(
        git_repo, (2, 3, 4), lambda _number: barrier.wait(timeout=10),
    )
    engine = _engine(git, states, fixture, agents)

    assert engine.develop_frontier(str(state["run_id"]), state)

    run_id = str(state["run_id"])
    reloaded_states = StateStore(git_repo / ".agent-run")
    for number in (2, 4):
        local = TicketDevelopmentState(reloaded_states, run_id, number).load_current_run(run_id)
        assert local is not None
        assert local["active_ticket_job"]["ticket_number"] == number
        assert local["active_agent_invocation"] == local["ticket_agent_invocations"][str(number)]
    saved = reloaded_states.load_current_run(run_id)
    assert saved is not None
    assert "3" not in saved["ticket_agent_invocations"]
    assert {entry["work_subject"] for entry in saved["agent_invocation_history"]} == {
        "ticket:2", "ticket:4",
    }
    # A fresh Engine sees the durable ready phases and does not replay them.
    reloaded_engine = _engine(git, reloaded_states, fixture, agents)
    assert reloaded_engine.develop_frontier(run_id, saved) is False
    assert reloaded_states.load_current_run(run_id) == saved


def test_reverse_completion_preserves_each_ticket_result(git_repo: Path) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3))
    release_first = Event()
    second_saved = Event()

    def gate(number: int) -> None:
        if number == 2:
            assert release_first.wait(timeout=10), "first Ticket gate was not released"

    def saved(current: dict[str, Any]) -> None:
        job = current["ticket_jobs"].get("3")
        if isinstance(job, dict) and job.get("phase") == "ready_for_integration":
            second_saved.set()

    states.run_saved_observer = saved
    agents = _TicketAgents(git_repo, (2, 3), gate)
    engine = _engine(git, states, fixture, agents)
    errors: list[BaseException] = []

    def advance() -> None:
        try:
            assert engine.develop_frontier(str(state["run_id"]), state)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=advance)
    thread.start()
    try:
        assert second_saved.wait(timeout=10), "second Ticket did not finish first"
        middle = states.load_current_run(str(state["run_id"]))
        assert middle is not None
        assert middle["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
        assert middle["ticket_jobs"]["2"]["phase"] == "developing"
        assert {item["ticket_number"] for item in middle["ticket_integration_queue"]} == {3}
    finally:
        release_first.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    saved_state = states.load_current_run(str(state["run_id"]))
    assert saved_state is not None
    assert {item["ticket_number"] for item in saved_state["ticket_integration_queue"]} == {2, 3}
    assert saved_state["ticket_jobs"]["2"]["phase"] == "ready_for_integration"
    assert saved_state["ticket_jobs"]["3"]["phase"] == "ready_for_integration"
    assert set(saved_state["ticket_agent_invocations"]) == {"2", "3"}


def test_integration_holder_does_not_block_other_ticket_development(
    git_repo: Path,
) -> None:
    state, states, git, fixture = _run(git_repo, (2, 3, 4))
    first_agents = _TicketAgents(git_repo, (2,), lambda _number: None)
    _engine(git, states, fixture, first_agents, development_only=True).deliver(
        str(state["run_id"])
    )
    state = states.load_current_run(str(state["run_id"]))
    assert state is not None
    holder = state["ticket_jobs"]["2"]
    holder["phase"] = "accepted"
    state["active_ticket_job"] = holder
    state["ticket_integration_holder"] = 2
    state["ticket_integration_queue"] = []
    require_current_run_state(state)
    states.save_run(str(state["run_id"]), state)

    barrier = Barrier(2)
    agents = _TicketAgents(
        git_repo, (3, 4), lambda _number: barrier.wait(timeout=10),
    )
    engine = _engine(git, states, fixture, agents)
    assert engine.develop_frontier(str(state["run_id"]), state)

    saved = states.load_current_run(str(state["run_id"]))
    assert saved is not None
    assert saved["ticket_integration_holder"] == 2
    assert saved["ticket_jobs"]["2"]["phase"] == "accepted"
    assert all(
        saved["ticket_jobs"][str(number)]["phase"] == "ready_for_integration"
        for number in (3, 4)
    )
    assert {item["ticket_number"] for item in saved["ticket_integration_queue"]} == {3, 4}
