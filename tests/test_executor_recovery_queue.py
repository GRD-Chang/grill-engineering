"""Executor recovery preserves durable work and independent Ticket gates."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.agent_invocation import record_operator_stop, record_session_interruption
from agent_run.controller import Controller, apply_resume_to_state
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.resume_intent import bind_resume_intent
from agent_run.ticket_pause import TicketStopRequested, request_ticket_pause
from agent_run.ticket_work import TicketWorkAdmission
from support.ticket_parallel import TicketAgents, ticket_engine, ticket_run


@pytest.mark.parametrize("local_pause", ["none", "integration", "reviewer", "completed"])
@pytest.mark.parametrize("boundary", ["interruption", "operator_stop"])
def test_executor_recovery_preserves_fifo_and_local_pause(
    git_repo: Path, local_pause: str, boundary: str,
) -> None:
    state, states, git, fixture = ticket_run(git_repo, (2, 3, 4), workers=1)
    run_id = state["run_id"]
    if local_pause != "none":
        def review(number: int) -> None:
            if local_pause == "reviewer":
                request_ticket_pause(states, run_id, number)
                raise TicketStopRequested()

        agents = TicketAgents(git_repo, (2,), lambda _number: None, before_review=review)
        ticket_engine(
            git, states, fixture, agents, development_only=local_pause != "completed",
        ).deliver_ticket(run_id, 2)
        if local_pause != "completed":
            request_ticket_pause(states, run_id, 2)
    admission = TicketWorkAdmission(states, run_id)
    admission.seed([4, 3])
    old_lease = admission.acquire(4, 1, "development")
    assert old_lease is not None
    before = states.load_current_run(run_id)
    assert before is not None
    paused_job = deepcopy(before["ticket_jobs"].get("2"))
    paused_invocation = deepcopy(before["ticket_agent_invocations"].get("2"))
    record = record_session_interruption if boundary == "interruption" else record_operator_stop
    record(before, save=lambda value: states.save_run(run_id, value))
    interrupted = states.load_current_run(run_id)
    assert interrupted is not None
    assert [item["ticket_number"] for item in interrupted["ticket_work_queue"]] == [4, 3]
    assert all(item["lease_id"] is None for item in interrupted["ticket_work_queue"])
    assert bind_resume_intent(interrupted)["authorization"] == "continue_existing_work"

    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True,
    )

    assert resumed["ticket_jobs"].get("2") == paused_job
    assert resumed["ticket_agent_invocations"].get("2") == paused_invocation
    assert [item["ticket_number"] for item in resumed["ticket_work_queue"]] == [4, 3]
    assert admission.acquire(3, 1, "development") is None
    new_lease = admission.acquire(4, 1, "development")
    assert new_lease is not None and new_lease != old_lease
    admission.release(old_lease)
    assert states.load_current_run(run_id)["ticket_work_queue"][0]["lease_id"] == new_lease


@pytest.mark.parametrize("status", ["execution_failed", "operator_stopped"])
@pytest.mark.parametrize("gate_count", [1, 2])
def test_run_recovery_does_not_renew_or_resume_sibling_budget_gate(
    status: str, gate_count: int,
) -> None:
    policy = DeliveryPolicy(ticket_review_rounds=1)
    job = {
        "ticket_number": 2, "phase": "blocked",
        "blocked_reason": "modification_budget_exhausted",
        "review_budget": {"window": 1, "checkpoint_reason": "modification_budget_exhausted"},
        "candidate_sha": "preserved", "pending_semantic_attempt": None,
    }
    invocation = {"work_subject": "ticket:2", "status": "failed", "error": "session_interrupted"}
    state = {
        "status": status, "ticket_graph": {"tickets": {"2": {}}},
        "ticket_jobs": {"2": job}, "active_ticket_job": job,
        "ticket_agent_invocations": {"2": invocation}, "active_agent_invocation": invocation,
    }
    if gate_count == 2:
        state["ticket_graph"]["tickets"]["3"] = {}
        state["ticket_jobs"]["3"] = {
            "ticket_number": 3, "phase": "blocked", "blocked_reason": "ticket_paused",
            "candidate_sha": "paused-candidate", "pause_resume_phase": "reviewing",
        }
    before = deepcopy(state)
    assert bind_resume_intent(state)["authorization"] == "continue_existing_work"
    apply_resume_to_state(
        state, explicit_resume=True, resume_human_blocker=False, new_thread=False,
        human_response=None, resume_budget_checkpoint=False, budget_policy=policy,
        ticket_number=None, source_status=status,
    )
    assert state == before


@pytest.mark.parametrize("boundary", ["interruption", "operator_stop"])
def test_global_resume_binds_ticket_authority_not_active_projection(
    git_repo: Path, boundary: str,
) -> None:
    from agent_run.resume_intent import ResumeIntentError, validate_resume_intent

    state, states, git, fixture = ticket_run(git_repo, (2, 3), workers=1)
    run_id = state["run_id"]
    agents = TicketAgents(git_repo, (3, 2), lambda _number: None)
    engine = ticket_engine(git, states, fixture, agents, development_only=True)
    for number in (3, 2):
        engine.deliver_ticket(run_id, number)
    paused = states.load_current_run(run_id)
    assert paused is not None
    assert paused["active_agent_invocation"]["work_subject"] == "ticket:2"
    assert paused["ticket_integration_queue"][0]["ticket_number"] == 3
    record = record_session_interruption if boundary == "interruption" else record_operator_stop
    record(paused, save=lambda value: states.save_run(run_id, value))
    paused = states.load_current_run(run_id)
    assert paused is not None
    intent = bind_resume_intent(paused)
    # Even the non-selected sibling remains part of the accepted authority.
    for field in ("candidate_sha", "ticket_branch_generation", "review_budget"):
        changed = deepcopy(paused)
        job = changed["ticket_jobs"]["3"]
        if field == "review_budget":
            job[field]["window"] += 1
        elif field == "ticket_branch_generation":
            job[field] += 1
        else:
            job[field] = "f" * 40
        with pytest.raises(ResumeIntentError, match="暂停对象或授权已变化"):
            validate_resume_intent(changed, intent)
    for field in ("binding_id", "semantic_attempt", "currentness_boundary"):
        changed = deepcopy(paused)
        invocation = changed["ticket_agent_invocations"]["3"]
        if field == "binding_id":
            invocation[field] = "replacement-binding"
        elif field == "semantic_attempt":
            invocation[field]["attempt_id"] = "replacement-attempt"
        else:
            invocation[field]["candidate_sha"] = "f" * 40
        with pytest.raises(ResumeIntentError, match="暂停对象或授权已变化"):
            validate_resume_intent(changed, intent)
    for field in ("accepted_parent_spec_revision", "accepted_ticket_graph_revision"):
        changed = {**paused, field: "changed"}
        with pytest.raises(ResumeIntentError, match="暂停对象或授权已变化"):
            validate_resume_intent(changed, intent)
    identities = deepcopy(paused["ticket_agent_invocations"])
    jobs = deepcopy(paused["ticket_jobs"])
    resumed, _ = Controller(FixtureGitHubReader(fixture), git, states).resume(
        run_id, explicit_resume=True,
        validate_resume_state=lambda current: validate_resume_intent(current, intent),
    )
    assert resumed["active_agent_invocation"]["work_subject"] == "ticket:3"
    assert resumed["ticket_agent_invocations"] == identities
    for number, job in jobs.items():
        assert {key: resumed["ticket_jobs"][number][key] for key in job} == job
    assert resumed["status"] == "active"
