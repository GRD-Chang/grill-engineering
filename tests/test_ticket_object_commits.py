"""Ticket 对象提交等待时重验授权，落盘返回重开后只消费一次。"""
from __future__ import annotations

from copy import deepcopy
from functools import partial
import json
from pathlib import Path
import subprocess
from threading import Event, Thread
from typing import Any

import pytest

from agent_run.task_control import (
    ActionReconciliationError, TaskControlBusyError, TaskControlStore, TaskKey,
)
from agent_run.state import StateStore
from agent_run.ticket_pause import request_ticket_pause
from agent_run.ticket_state import TicketState
from support.cli_payloads import human_blocker_step
from support.inprocess_cli import invoke_cli_inprocess
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run
from support.state import persist_run_fixture


class _Agents(TicketAgents):
    def __init__(self, root: Path, numbers: tuple[int, ...]) -> None:
        super().__init__(root, numbers, lambda _: None)
        self.calls: dict[str, list[int]] = {
            "development": [], "reviewer": [], "publication": [],
        }
        self.returned = Event()
        self.release = Event()
        self.hold_development = False

    def develop(self, request: dict[str, Any]) -> Any:
        number = self._number(request)
        self.calls["development"].append(number)
        result = super().develop(request)
        if self.hold_development and number == 2:
            self.returned.set()
            assert self.release.wait(10), "Development return gate was not released"
        return result

    def review(self, request: dict[str, Any]) -> Any:
        self.calls["reviewer"].append(self._number(request))
        return super().review(request)

    def publication(self, request: dict[str, Any]) -> Any:
        self.calls["publication"].append(self._number(request))
        return super().publication(request)


def test_work_update_returns_isolated_current_baseline_and_rejects_intervening_sql(git_repo: Path) -> None:
    state, states, _, _ = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]

    def observe() -> None:
        loaded, baseline = states._load_work_for_update(run_id, 2, thin_timeline=True)
        assert baseline is states._repository.current_work_baseline(run_id)
        loaded["updated_at"] = "caller mutation"
        assert baseline["updated_at"] != loaded["updated_at"]
        states._repository._local.connection.execute("UPDATE runs SET status=status WHERE run_id=?", (run_id,))
        with pytest.raises(ValueError, match="current transaction read"):
            states._repository.current_work_baseline(run_id)

    states._commit_run(observe)
    assert states.load_current_run(run_id)["updated_at"] == state["updated_at"]


@pytest.mark.parametrize("custom", ["reader", "validator"])
def test_work_update_custom_hooks_cannot_substitute_a_changed_baseline(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, custom: str,
) -> None:
    from agent_run import state_contract

    state, states, _, _ = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    if custom == "reader":
        read = states.load_work_run

        def altered_read(*args, **kwargs):
            loaded = read(*args, **kwargs)
            loaded["updated_at"] = "custom hook mutation"
            return loaded

        monkeypatch.setattr(states, "load_work_run", altered_read)
    else:
        validate = state_contract.require_current_work_state

        def altered_validate(loaded, *args, **kwargs):
            validate(loaded, *args, **kwargs)
            loaded["updated_at"] = "custom hook mutation"

        monkeypatch.setattr(state_contract, "require_current_work_state", altered_validate)

    def commit() -> None:
        loaded, baseline = states._load_work_for_update(run_id, 2, thin_timeline=True)
        states._repository.save_work(run_id, loaded, baseline)

    with pytest.raises(ValueError, match="current transaction baseline"):
        states._commit_run(commit)
    assert StateStore(states.root).load_current_run(run_id)["updated_at"] == state["updated_at"]


@pytest.mark.parametrize("blocked_ticket", [2, 3])
def test_human_blocker_history_retains_actual_ticket_and_review_evidence(
    git_repo: Path, blocked_ticket: int,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = str(state["run_id"])
    selected_ticket = state["active_ticket_job"]["ticket_number"]
    assert selected_ticket == 2
    agents = TicketAgents(git_repo, (2, 3), lambda _: None)
    evidence = f"Confirm deployment access for Ticket {blocked_ticket}."
    agents.backends[blocked_ticket].data["reviews"][0]["checks"]["e2e"] = {
        "status": "blocked", "evidence": evidence, "findings": [],
    }
    assert ticket_engine(git, states, fixture, agents).develop_frontier(run_id, state)
    reopened = StateStore(states.root).load_current_run(run_id)
    assert reopened["active_ticket_job"]["ticket_number"] == selected_ticket
    assert reopened["ticket_jobs"][str(blocked_ticket)]["blocked_reason"] == "reviewer_requires_human"
    assert reopened["ticket_jobs"][str(5 - blocked_ticket)]["phase"] == "ready_for_integration"
    response = invoke_cli_inprocess(
        git_repo, fixture, "history", run_id, "--state-dir", str(states.root), "--json",
    )
    assert response.returncode == 0, response.stderr
    events = json.loads(response.stdout)["events"]
    blockers = [event for event in events if event["kind"] == "human_blocker"]
    assert blockers and all(event["object"] == f"Ticket #{blocked_ticket}" for event in blockers)
    assert any(event["details"] == [evidence] for event in blockers)


def test_stop_between_durable_return_and_consumption_preserves_return_without_new_gate(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = state["run_id"]
    agents = _Agents(git_repo, (2, 3))
    agents.backends[2].data["developments"] = [human_blocker_step("developer-2")]
    saved, release = Event(), Event()
    original_save = TicketState.save_run
    errors: list[BaseException] = []

    def hold(lane: TicketState, identifier: str, current: dict[str, Any]) -> None:
        original_save(lane, identifier, current)
        attempt = current["active_ticket_job"].get("pending_semantic_attempt", {})
        if "returned_result" in attempt and not saved.is_set():
            saved.set()
            assert release.wait(10), "Durable return consumption gate was not released"

    monkeypatch.setattr(TicketState, "save_run", hold)

    def deliver() -> None:
        try:
            ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(run_id, 2)
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=deliver)
    worker.start()
    try:
        assert saved.wait(10), errors
        before = StateStore(states.root).load_current_run(run_id)
        result = deepcopy(before["ticket_jobs"]["2"]["pending_semantic_attempt"]["returned_result"])
        request_ticket_pause(states, run_id, 2)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not errors, errors
    observed = StateStore(states.root).load_current_run(run_id)
    job = observed["ticket_jobs"]["2"]
    assert job["blocked_reason"] == "ticket_paused"
    assert job["pending_semantic_attempt"]["returned_result"] == result
    assert job["review_budget"]["development_attempts"] == 1
    assert agents.calls == {"development": [2], "reviewer": [], "publication": []}


def test_reverse_parallel_returns_keep_candidate_thread_attempt_and_budget_owners(
    git_repo: Path,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = str(state["run_id"])
    agents = _Agents(git_repo, (2, 3))
    agents.hold_development = True
    sibling_ready = Event()
    observations: list[dict[str, Any]] = []

    def observe(current: dict[str, Any]) -> None:
        if current["ticket_jobs"].get("3", {}).get("phase") == "ready_for_integration":
            observations.append(deepcopy(current))
            sibling_ready.set()

    states.run_saved_observer = observe
    errors: list[BaseException] = []

    def develop() -> None:
        try:
            assert ticket_engine(git, states, fixture, agents).develop_frontier(run_id, state)
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=develop)
    worker.start()
    try:
        assert agents.returned.wait(10), errors
        assert sibling_ready.wait(10), errors
        middle = StateStore(states.root).load_current_run(run_id)
        sibling = deepcopy(middle["ticket_jobs"]["3"])
        assert middle["ticket_jobs"]["2"]["phase"] == "developing"
        assert [item["ticket_number"] for item in middle["ticket_integration_queue"]] == [3]
        history = observations[0]["agent_invocation_history"]
        assert {(entry["work_subject"], entry["role"]) for entry in history} == {
            ("ticket:2", "development"), ("ticket:3", "development"),
            ("ticket:3", "fresh_acceptance"),
        }
    finally:
        agents.release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert not errors, errors
    saved = StateStore(states.root).load_current_run(run_id)
    assert saved["ticket_jobs"]["3"] == sibling
    assert [item["ticket_number"] for item in saved["ticket_integration_queue"]] == [3, 2]
    for number in (2, 3):
        job = saved["ticket_jobs"][str(number)]
        assert job["phase"] == "ready_for_integration"
        assert job["development_summary"] == f"Ticket {number}"
        assert job["development_thread_id"] == f"developer-{number}"
        assert job["review_budget"]["development_attempts"] == 1
        assert job["review_budget"]["reviewer_invocations"] == 1
        contents = subprocess.run(
            ["git", "show", f"{job['candidate_sha']}:ticket-{number}.txt"],
            cwd=git_repo, capture_output=True, text=True, check=True,
        ).stdout
        assert contents == "done\n"
        assert {attempt["work_subject"] for attempt in job["semantic_attempt_history"]} == {f"ticket:{number}"}
        history = [item for item in saved["agent_invocation_history"]
                   if item["work_subject"] == f"ticket:{number}"]
        assert len(history) == 2
        assert {item["reported_thread_id"] for item in history} == {
            f"developer-{number}", f"reviewer-{number}",
        }
        events = [event for event in saved["timeline"]
                  if event.get("ticket") == number
                  and event.get("phase") == "ready_for_integration"]
        assert events and events[-1]["commit_sha"] == job["candidate_sha"]
        reviews = [event for event in saved["timeline"]
                   if event.get("ticket") == number
                   and event.get("semantic_attempt_role") == "reviewer"]
        assert reviews and reviews[-1]["commit_sha"] == job["candidate_sha"]
        assert reviews[-1]["semantic_attempt_id"] in {
            attempt["attempt_id"] for attempt in job["semantic_attempt_history"]
        }
    assert sorted(agents.calls["development"]) == [2, 3]
    assert sorted(agents.calls["reviewer"]) == [2, 3]
    assert agents.calls["publication"] == []


@pytest.mark.parametrize("revocation", ["stop", "job", "executor_generation"])
def test_waiting_ticket_return_revalidates_authority_without_recalling_agent(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, revocation: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = str(state["run_id"])
    agents = _Agents(git_repo, (2, 3))
    agents.hold_development = True
    control = TaskControlStore(states.root)
    task = TaskKey(git_repo, str(state["repository"]), 1)
    claim = control.claim_action(task, kind="run", payload={})
    generation = control.begin_executor(task, action_id=claim.action_id, run_id=run_id).generation
    identity = {"action_id": claim.action_id, "generation": generation}
    control.bind_run(task, claim.action_id, run_id, generation=generation)
    control.mark_handshake(task, **identity, pid=900, process_start_token="executor")
    control.record_application(task, **identity, run_id=run_id,
                               payload_digest=claim.action["payload_digest"])
    control.complete_action(task, **identity, result_status="active")
    states._set_write_guard(None, transaction=partial(
        control._executor_current_transaction, task, **identity, run_id=run_id,
    ))
    waiting, saving_return = Event(), Event()
    original_wait = states._coordinator._condition.wait
    original_save = TicketState.save_run

    def wait(timeout: float | None = None) -> bool:
        if saving_return.is_set():
            waiting.set()
        return original_wait(timeout)

    def save(lane: TicketState, identifier: str, current: dict[str, Any]) -> None:
        if "returned_result" in current["active_ticket_job"].get("pending_semantic_attempt", {}):
            saving_return.set()
        original_save(lane, identifier, current)

    monkeypatch.setattr(states._coordinator._condition, "wait", wait)
    monkeypatch.setattr(TicketState, "save_run", save)
    errors: list[BaseException] = []

    def deliver() -> None:
        try:
            ticket_engine(git, states, fixture, agents, development_only=True).deliver_ticket(run_id, 2)
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=deliver)
    worker.start()
    try:
        assert agents.returned.wait(10), errors
        with states.commit_admission():
            agents.release.set()
            assert waiting.wait(10), "Agent return did not wait for a commit seat"
            pending = states.load_current_run(run_id)
            assert "returned_result" not in pending["ticket_jobs"]["2"]["pending_semantic_attempt"]
            assert agents.calls["development"] == [2]
            if revocation == "stop":
                request_ticket_pause(states, run_id, 2)
            elif revocation == "job":
                pending["ticket_jobs"]["2"]["base_sha"] = "replacement-head"
                pending["active_ticket_job"] = pending["ticket_jobs"]["2"]
                persist_run_fixture(states, run_id, pending)
            else:
                control.finish_executor(task, **identity)
                successor = control.claim_action(task, kind="resume", payload={})
                assert control.begin_executor(task, action_id=successor.action_id, run_id=run_id).generation > generation
    finally:
        agents.release.set()
        worker.join(10)
    assert not worker.is_alive(), "Ticket worker did not leave the rejected commit"
    saved = StateStore(states.root).load_current_run(run_id)
    job = saved["ticket_jobs"]["2"]
    assert "returned_result" not in job["pending_semantic_attempt"]
    assert agents.calls == {"development": [2], "reviewer": [], "publication": []}
    assert saved["ticket_integration_queue"] == []
    if revocation == "stop":
        assert not errors, errors
        assert job["blocked_reason"] == "ticket_paused"
    elif revocation == "job":
        assert job["base_sha"] == "replacement-head"
        assert errors and isinstance(errors[0], TaskControlBusyError), errors
    else:
        assert errors and isinstance(errors[0], ActionReconciliationError), errors


@pytest.mark.parametrize("role", ["development", "reviewer", "publication"])
def test_reopened_ticket_consumes_durable_return_and_duplicate_commit_once(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3))
    run_id = str(state["run_id"])
    agents = _Agents(git_repo, (2, 3))
    original_save = TicketState.save_run
    interrupted = False

    def interrupt(lane: TicketState, identifier: str, current: dict[str, Any]) -> None:
        nonlocal interrupted
        original_save(lane, identifier, current)
        attempt = current["active_ticket_job"].get("pending_semantic_attempt", {})
        if not interrupted and attempt.get("role") == role and "returned_result" in attempt:
            interrupted = True
            raise KeyboardInterrupt("interrupted after durable Ticket return")

    with monkeypatch.context() as fault:
        fault.setattr(TicketState, "save_run", interrupt)
        with pytest.raises(KeyboardInterrupt, match="durable Ticket return"):
            ticket_engine(git, states, fixture, agents).deliver_ticket(run_id, 2)
    assert interrupted
    reopened = StateStore(states.root)
    durable = reopened.load_current_run(run_id)
    job = durable["ticket_jobs"]["2"]
    attempt = deepcopy(job["pending_semantic_attempt"])
    before_calls = deepcopy(agents.calls)
    candidate_sha = job.get("candidate_sha")
    budget = deepcopy(job["review_budget"])
    invocation = deepcopy(durable["ticket_agent_invocations"]["2"])
    sibling = deepcopy(durable["ticket_jobs"].get("3"))
    ticket_engine(git, reopened, fixture, agents).deliver_ticket(run_id, 2)
    completed = StateStore(states.root).load_current_run(run_id)
    job = completed["ticket_jobs"]["2"]
    assert job["phase"] == "completed"
    assert agents.calls[role] == before_calls[role] == [2]
    archived = [entry for entry in job["semantic_attempt_history"]
                if entry["attempt_id"] == attempt["attempt_id"]]
    assert len(archived) == 1 and archived[0]["status"] == "completed"
    assert "returned_result" not in archived[0]
    assert job["review_budget"]["development_attempts"] == budget["development_attempts"] == 1
    assert job["review_budget"]["reviewer_invocations"] == 1
    assert job["publication_attempts"] == 1
    assert job["development_thread_id"] == "developer-2"
    returned = attempt["returned_result"]
    if role == "development":
        assert job["development_summary"] == returned["summary"]
    elif role == "reviewer":
        assert job["acceptance_artifact"] == returned["artifact"]
    else:
        pull_requests = json.loads(fixture.read_text())["delivery"]["pull_requests"]
        assert len(pull_requests) == 1
        assert pull_requests[0]["title"] == returned["artifact"]["pr_title"]
    if candidate_sha is not None:
        assert job["candidate_sha"] == candidate_sha
    assert completed["ticket_jobs"].get("3") == sibling
    history = [entry for entry in completed["agent_invocation_history"]
               if entry["started_at"] == invocation["started_at"]]
    assert history == [invocation]
    lane = TicketState(reopened, run_id, 2)
    snapshot = lane.load_current_run(run_id)
    lane.save_run(run_id, snapshot)
    lane.save_run(run_id, snapshot)
    assert StateStore(states.root).load_current_run(run_id) == completed
    assert agents.calls == {"development": [2], "reviewer": [2], "publication": [2]}
