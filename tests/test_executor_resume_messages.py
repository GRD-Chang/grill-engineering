"""Global recovery messages follow runnable work, preserving local pauses."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_run.agent_invocation import record_session_interruption
from agent_run.controller import _bind_resume_message
from agent_run.git import GitRepository
from agent_run.human_responses import append_human_response
from agent_run.resume_audit import append_explicit_resume_audit
from agent_run.state import StateStore
from agent_run.ticket_pause import request_ticket_pause
from support.cli import run_cli, stdout_json
from support.cli_payloads import ticket
from support.seeding import seed_run, write_fixture
from support.ticket_parallel import TicketAgents, ticket_engine
from support.workspace import managed_repo, managed_state


@pytest.mark.parametrize("status", ["execution_failed", "operator_stopped"])
@pytest.mark.parametrize("reason", ["ticket_paused", "agent_requires_human", "modification_budget_exhausted"])
def test_executor_message_and_audit_keep_independent_gates_out_of_target(
    status: str, reason: str,
) -> None:
    sibling = {"ticket_number": 2, "ticket_branch_generation": 1,
               "phase": "blocked", "blocked_reason": reason,
               "human_response_history": [{"generation": 1, "human_blockers": [], "response": "local context"}],
               "human_response_generation": 1, "review_budget": {"window": 1}}
    job = {"ticket_number": 3, "ticket_branch_generation": 2, "phase": "accepted"}
    state = {"run_id": "run", "status": status,
             "ticket_graph": {"tickets": {"2": {}, "3": {}}}, "frontier": [2, 3],
             "ticket_jobs": {"2": sibling, "3": job}, "active_ticket_job": sibling,
             "active_agent_invocation": {"work_subject": "ticket:2", "role": "development",
                                         "status": "failed", "generation": 1},
             "ticket_agent_invocations": {}, "ticket_integration_holder": 3,
             "ticket_integration_queue": [], "ticket_work_queue": []}
    original = deepcopy(sibling)
    assert _bind_resume_message(state, "global context") == "appended"
    event = append_explicit_resume_audit(state, new_thread=False, human_response_supplied=True)
    assert event["work_subject"] == "ticket:3"
    assert event["generation"] == 2
    assert event["kind"] == ("operator_stopped" if status == "operator_stopped" else "execution_failure")
    assert job["human_response_history"][-1]["response"] == "global context"
    assert sibling == original
    assert state["active_ticket_job"] == original


@pytest.mark.parametrize("paused_numbers", [(2,), (2, 4)])
@pytest.mark.parametrize("with_invocation", [False, True])
def test_public_executor_resume_message_reaches_runnable_publication(
    git_repo: Path, paused_numbers: tuple[int, ...], with_invocation: bool,
) -> None:
    numbers = (3, *paused_numbers)
    fixture = write_fixture(git_repo / "github.json", issues={
        str(number): {**ticket(), "number": number} for number in numbers
    })
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    states = StateStore(managed_state(git_repo))
    git = GitRepository(managed_repo(git_repo))
    agents = TicketAgents(git_repo, numbers, lambda _number: None)
    engine = ticket_engine(git, states, fixture, agents, development_only=True)
    for number in numbers:
        engine.deliver_ticket(run_id, number)
        if number in paused_numbers:
            request_ticket_pause(states, run_id, number)
    paused = states.load_current_run(run_id)
    assert paused is not None
    for number in paused_numbers:
        job = paused["ticket_jobs"][str(number)]
        append_human_response(job, [], f"Context for Ticket {number} only.",
                              generation=job["ticket_branch_generation"])
        if paused["active_ticket_job"]["ticket_number"] == number:
            paused["active_ticket_job"] = job
    if with_invocation:
        assert paused["active_agent_invocation"]["status"] == "completed"
    else:
        paused["active_agent_invocation"] = None
    record_session_interruption(paused, save=lambda value: states.save_run(run_id, value))
    before = states.load_current_run(run_id)
    assert before is not None
    siblings = {str(number): deepcopy(before["ticket_jobs"][str(number)]) for number in paused_numbers}
    job_before = before["ticket_jobs"]["3"]
    assert job_before["phase"] == "ready_for_integration"
    message = "Only continue the interrupted original work."
    expected_history = [{"generation": job_before["ticket_branch_generation"],
                         "human_blockers": [], "response": message}]
    agent_path = git_repo / "ticket-3-agents.json"
    script = json.loads(agent_path.read_text(encoding="utf-8"))
    script["publications"][0]["expected_human_response_history"] = expected_history
    agent_path.write_text(json.dumps(script), encoding="utf-8")

    resumed = run_cli(git_repo, fixture, "resume", run_id, "--message", message,
                      "--agent-fixture", str(agent_path))

    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    after = states.load_current_run(run_id)
    assert after is not None
    job = after["ticket_jobs"]["3"]
    assert job["phase"] == "completed", after.get("diagnostics")
    assert job["human_response_history"] == expected_history
    for key in ("candidate_sha", "acceptance_record", "review_budget", "ci_recovery", "ticket_branch_generation"):
        assert job[key] == job_before[key]
    for number, sibling in siblings.items():
        assert after["ticket_jobs"][number] == sibling
    event = after["resume_audit"]["history"][-1]
    assert event["work_subject"] == "ticket:3"
    assert event["generation"] == job["ticket_branch_generation"]
    assert event["human_response_supplied"] is True
    assert isinstance(event["successor_invocation_started_at"], str)
    delivery = json.loads(fixture.read_text(encoding="utf-8"))["delivery"]
    assert delivery["closed_issues"] == [3]
    assert len(delivery["pull_requests"]) == 1
