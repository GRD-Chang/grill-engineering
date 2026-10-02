from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from agent_run.external_supervision import ExternalSupervisor
from agent_run.guidance_actions import NextAction
from agent_run.operator_action_facts import operator_action_facts


def _expire_wait(state: dict[str, Any]) -> None:
    clock = [100.0]
    supervisor = ExternalSupervisor(now=lambda: clock[0], sleeper=lambda _: None)
    window = supervisor.observe(state)
    assert window is not None
    clock[0] = float(window["deadline"])
    assert not supervisor.retry_due(state)
    assert state["status"] == "supervision_timeout"


@pytest.mark.parametrize("location", ["ticket", "parent", "run_repair", "run_publication"])
def test_timed_out_pr_keeps_its_work_object_phase_and_generation(location: str) -> None:
    job = {"phase": "waiting_checks", "pr_number": 8}
    state: dict[str, Any] = {"run_id": "r1", "status": "waiting_checks"}
    if location == "ticket":
        job.update(ticket_number=2, ticket_branch_generation=3)
        state.update(active_ticket_job=job, ticket_jobs={"2": job})
        expected_subject = "ticket:2"
    elif location == "parent":
        job["parent_generation"] = 3
        state["parent_job"] = job
        expected_subject = "parent-only:r1"
    elif location == "run_repair":
        state["run_acceptance"] = {"phase": "repairing", "repair_generation": 3, "repair_job": job}
        state["run_publication"] = {"phase": "ready_for_approval", "pr_number": 7}
        expected_subject = "run-repair:r1"
    else:
        state.update(run_publication=job, run_acceptance={"phase": "accepted", "acceptance_generation": 3})
        expected_subject = "run-publication:r1"
    _expire_wait(state)
    before = deepcopy(state)

    fact, = operator_action_facts(state)

    assert (fact["work_subject"], fact["generation"], fact["phase"]) == (expected_subject, 3, "waiting_checks")
    assert (fact["scope"], fact["action_kind"], fact["status_code"]) == ("global", "supervision_timeout", "supervision_timeout")
    assert fact["action"] == NextAction("command", "resume", "r1")
    assert fact["evidence"]["diagnostics"] == state["diagnostics"]
    assert state == before


@pytest.mark.parametrize("location", ["parent", "run_acceptance", "run_repair", "run_publication"])
def test_credential_timeout_uses_the_waiting_worker_not_a_retained_pr(location: str) -> None:
    state: dict[str, Any] = {
        "run_id": "r1", "status": "waiting_external",
        "run_publication": {"phase": "ready_for_approval", "pr_number": 7},
    }
    job: dict[str, Any] = {"phase": "pending"}
    if location == "parent":
        job["parent_generation"] = 3
        state["parent_job"] = job
        label, subject = "parent-only", "parent-only:r1"
    elif location == "run_acceptance":
        job["acceptance_generation"] = 3
        state["run_acceptance"] = job
        label, subject = "run-acceptance", "run-acceptance:r1"
    elif location == "run_repair":
        job["repair_attempt"] = 2
        state["run_acceptance"] = {"phase": "repairing", "repair_generation": 3, "repair_job": job}
        label, subject = "run-repair-2", "run-repair:r1"
    else:
        state["run_publication"] = job
        state["run_acceptance"] = {"phase": "accepted", "acceptance_generation": 3}
        label, subject = "run-publication", "run-publication:r1"
    state["credential_availability"] = {"change_job": label, "phase": "pending", "failure_class": "credential_unavailable"}
    _expire_wait(state)

    fact, = operator_action_facts(state)

    assert (fact["work_subject"], fact["generation"], fact["phase"]) == (subject, 3, "pending")
    assert fact["scope"] == "global"


@pytest.mark.parametrize("binding", ["absent", "stale", "unbound"])
def test_unbound_timeout_stays_an_independent_run_gate(binding: str) -> None:
    local = {"ticket_number": 2, "phase": "blocked", "blocked_reason": "agent_requires_human", "human_blockers": ["Local input"]}
    state: dict[str, Any] = {
        "run_id": "r1", "status": "waiting_external", "ticket_jobs": {"2": local},
    }
    if binding != "unbound":
        state["run_publication"] = {"phase": "waiting_checks", "pr_number": 7}
    _expire_wait(state)
    if binding == "absent":
        state.pop("supervision_wait")
    elif binding == "stale":
        state["run_publication"]["pr_number"] = 8

    global_fact, local_fact = operator_action_facts(state)

    assert (global_fact["work_subject"], global_fact["scope"]) == ("run:r1", "global")
    assert (local_fact["work_subject"], local_fact["scope"]) == ("ticket:2", "local")
    assert local_fact["reasons"] == ["Local input"]
