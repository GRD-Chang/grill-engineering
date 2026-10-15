from __future__ import annotations

from typing import Any

import pytest

from agent_run.graph import state_from_graph
from agent_run.models import DeliveryGraph, Issue, ParentIssue


def _graph(*, parent_only: bool = False) -> DeliveryGraph:
    ticket = Issue(2, "Ticket", "Implement", "CLOSED", frozenset(), ())
    numbers = () if parent_only else (2,)
    return DeliveryGraph(ParentIssue(1, "Parent", "Requirements", numbers, True),
                         {} if parent_only else {2: ticket})


@pytest.mark.parametrize(("parent_only", "records", "status"), [
    (True, {}, "parent_delivery_pending"),
    (True, {"parent_job": {"phase": "completed"}}, "completed"),
    (True, {"parent_job": {"phase": "ready_for_approval"}}, "parent_approval_pending"),
    (True, {"parent_job": {"phase": "merging"}}, "parent_closeout_pending"),
    (True, {"parent_job": {"phase": "publication_pending"}}, "publication_pending"),
    (True, {"parent_job": {"phase": "waiting_checks"}}, "waiting_checks"),
    (True, {"parent_job": {"phase": "blocked", "execution_resume_phase": "waiting_checks"}}, "blocked"),
    (False, {}, "run_acceptance_pending"),
    (False, {"run_acceptance": {"phase": "repairing"}, "run_publication": {"phase": "ready_for_approval"}}, "run_acceptance_pending"),
    (False, {"run_acceptance": {"phase": "accepted"}}, "run_publication_pending"),
    (False, {"run_acceptance": {"phase": "accepted"}, "run_publication": {"phase": "ready_for_approval"}}, "run_approval_pending"),
    (False, {"run_acceptance": {"phase": "accepted"}, "run_publication": {"phase": "waiting_checks"}}, "waiting_checks"),
    (False, {"run_acceptance": {"phase": "accepted"}, "run_publication": {"phase": "merged"}}, "parent_closeout_pending"),
    (False, {"run_acceptance": {"phase": "accepted"}, "run_publication": {"phase": "publication_pending"}}, "publication_pending"),
])
def test_graph_projects_ready_parent_from_existing_stage_records(
    parent_only: bool, records: dict[str, Any], status: str,
) -> None:
    diagnostic = {"code": "agent_requires_human", "message": "Credentials"}
    previous = {"run_id": "run-1", "diagnostics": [diagnostic],
                "ticket_jobs": {"2": {"ticket_number": 2, "phase": "completed"}},
                **records}
    result = state_from_graph(previous, _graph(parent_only=parent_only))

    assert result["status"] == status
    assert result["active_ticket_job"] is None
    assert result["frontier"] == []
    if status == "blocked":
        assert result["diagnostics"] == [diagnostic]
    if status == "publication_pending":
        assert result["diagnostics"][0]["code"] == "publication_pending"
    assert not any(key in result for key in ("parent_ready", "parent_stage"))


def test_closed_issue_without_ticket_completion_cannot_enter_parent_stage() -> None:
    previous = {"run_acceptance": {"phase": "accepted"},
                "run_publication": {"phase": "ready_for_approval"}}
    result = state_from_graph(previous, _graph())

    assert result["status"] == "progress_exhausted"
    assert result["terminal_kind"] == "temporarily_no_work"
    assert result["diagnostics"][0]["remaining_tickets"] == [
        {"ticket_number": 2, "reason": "ticket_closed"},
    ]
