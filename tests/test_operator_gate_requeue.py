"""Requeue identity checks with independent, retained Ticket gates."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from agent_run.operator_gate import operator_gate_identity_is_consistent


@pytest.fixture
def requeue_state() -> dict[str, Any]:
    target = {"ticket_number": 3, "ticket_branch_generation": 2, "phase": "waiting_checks"}
    return {
        "run_id": "r1", "status": "requeue_required", "active_ticket_job": target,
        "ticket_jobs": {
            "2": {"ticket_number": 2, "ticket_branch_generation": 1, "phase": "blocked",
                  "blocked_reason": "reviewer_requires_human", "human_blockers": ["Original blocker"]},
            "3": target,
        },
        "requeue_required": {"work_subject": "ticket:3", "generation": 2, "reason": "ticket_requirements_changed"},
        "diagnostics": [{"code": "ticket_requirements_changed", "message": "Ticket requirements changed; run requeue"}],
    }


@pytest.mark.parametrize("target_phase", ["waiting_checks", "blocked"])
@pytest.mark.parametrize("sibling_phase", ["blocked", "publication_pending"])
@pytest.mark.parametrize("active_ticket", [None, "2", "3"])
def test_requeue_identity_is_independent_of_local_gate_order(
    requeue_state: dict[str, Any], target_phase: str, sibling_phase: str, active_ticket: str | None,
) -> None:
    state = requeue_state
    state["ticket_jobs"]["3"].update(phase=target_phase, blocked_reason="agent_requires_human")
    state["ticket_jobs"]["2"]["phase"] = sibling_phase
    state["active_ticket_job"] = state["ticket_jobs"].get(active_ticket)
    for _ in range(2):
        before = deepcopy(state)
        assert operator_gate_identity_is_consistent(state)
        assert state == before
        state["ticket_jobs"] = dict(reversed(state["ticket_jobs"].items()))


@pytest.mark.parametrize("fault", [
    "missing_record", "wrong_subject", "wrong_generation", "boolean_generation",
    "wrong_reason", "wrong_diagnostic", "missing_diagnostic", "duplicate_diagnostic",
    "empty_message", "foreign_action_binding", "unrelated_global_gate",
])
def test_independent_ticket_gate_does_not_relax_requeue_identity(
    requeue_state: dict[str, Any], fault: str,
) -> None:
    state = requeue_state
    if fault == "missing_record":
        state.pop("requeue_required")
    elif fault == "wrong_subject":
        state["requeue_required"]["work_subject"] = "ticket:99"
    elif fault == "wrong_generation":
        state["requeue_required"]["generation"] = 1
    elif fault == "boolean_generation":
        state["requeue_required"]["generation"] = True
    elif fault == "wrong_reason":
        state["requeue_required"]["reason"] = "fabricated_reason"
    elif fault == "wrong_diagnostic":
        state["diagnostics"][0]["code"] = "ticket_base_changed"
    elif fault == "missing_diagnostic":
        state["diagnostics"] = []
    elif fault == "duplicate_diagnostic":
        state["diagnostics"] *= 2
    elif fault == "empty_message":
        state["diagnostics"][0]["message"] = ""
    elif fault == "foreign_action_binding":
        state["diagnostics"][0]["operator_gate"] = {
            "work_subject": "ticket:2", "action_kind": "execution_failure",
            "phase": "blocked", "reason": "runner_credential_failed",
        }
    elif fault == "unrelated_global_gate":
        # An earlier matching gate must not hide an incompatible later object.
        state["ticket_jobs"]["3"].update(phase="blocked", blocked_reason="agent_requires_human")
        state["parent_job"] = {"phase": "ready_for_approval", "parent_generation": 1}
    before = deepcopy(state)

    assert not operator_gate_identity_is_consistent(state)
    assert state == before
