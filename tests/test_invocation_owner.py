"""Locate bound work without selecting, authorizing or rewriting its owner."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from agent_run.work_facts import locate_invocation_owner


def _state() -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "ticket_jobs": {"2": {"ticket_number": 2, "ticket_branch_generation": 3}},
        "active_ticket_job": {"ticket_number": 9},
        "parent_job": {"parent_generation": 4},
        "run_acceptance": {"acceptance_generation": 5, "repair_job": {"repair_generation": 6}},
        "run_publication": {"phase": "publishing"},
    }


@pytest.mark.parametrize(("subject", "role", "generation", "owner_path"), [
    ("ticket:2", "development", 3, ("ticket_jobs", "2")),
    ("ticket:2", "fresh_acceptance", 3, ("ticket_jobs", "2")),
    ("ticket:2", "publication", 3, ("ticket_jobs", "2")),
    ("ticket:2", "other", 3, ("ticket_jobs", "2")),  # Existing message fallback.
    ("parent-only:run-1", "development", 4, ("parent_job",)),
    ("parent-only:run-1", "publication", 4, ("parent_job",)),
    ("run-repair:run-1", "development", 6, ("run_acceptance", "repair_job")),
    ("run-repair:run-1", "fresh_acceptance", 6, ("run_acceptance", "repair_job")),
    ("run-repair:run-1", "publication", 6, ("run_acceptance", "repair_job")),
    ("run-acceptance:run-1", "reviewer", 5, ("run_acceptance",)),
    ("run-integration:run-1", "development", 5, ("run_acceptance",)),
    ("run-publication:run-1", "final_publication", 5, ("run_publication",)),
])
def test_invocation_owner_follows_bound_work_not_current_selection(
    subject: str, role: str, generation: int, owner_path: tuple[str, ...],
) -> None:
    state = _state()
    invocation = {"work_subject": subject, "role": role, "generation": generation}
    before = deepcopy((state, invocation))
    owner = state
    for key in owner_path:
        owner = owner[key]

    located = locate_invocation_owner(state, invocation)

    assert located.owner is owner
    assert located.publication_mirror is None
    assert (state, invocation) == before


@pytest.mark.parametrize(("role", "selection", "has_mirror"), [
    ("publication", "same", False), ("publication", "copy", True),
    ("publication", "other", False), ("publication", "absent", False),
    ("development", "copy", False), ("fresh_acceptance", "copy", False),
])
def test_only_ticket_publication_exposes_a_distinct_same_ticket_mirror(
    role: str, selection: str, has_mirror: bool,
) -> None:
    state = _state()
    owner = state["ticket_jobs"]["2"]
    if selection == "same":
        state["active_ticket_job"] = owner
    elif selection == "copy":
        state["active_ticket_job"] = deepcopy(owner)
    elif selection == "absent":
        state.pop("active_ticket_job")
    before = deepcopy(state)

    located = locate_invocation_owner(
        state, {"role": role, "work_subject": "ticket:2", "generation": 3},
    )

    assert located.owner is owner
    assert located.publication_mirror is (state["active_ticket_job"] if has_mirror else None)
    assert state == before


@pytest.mark.parametrize(("state", "invocation", "message"), [
    ({}, {"role": "reviewer", "generation": False}, "Run Acceptance Invocation work_subject is invalid"),
    ({"run_id": "run-1"}, {"role": "reviewer", "work_subject": "run-acceptance:run-1", "generation": False},
     "current Run Acceptance job is missing"),
    ({"run_id": "run-1", "run_acceptance": {}},
     {"role": "reviewer", "work_subject": "run-acceptance:run-1", "generation": False},
     "current Agent Invocation generation is invalid"),
    ({}, {"role": "publication", "generation": False}, "current Publication Invocation work_subject is missing"),
    ({}, {"role": "publication", "work_subject": "ticket:2", "generation": False},
     "current Publication Invocation generation is invalid"),
    ({}, {"role": "publication", "work_subject": "ticket:2", "generation": 1}, "Delivery Run ID is missing"),
    ({}, {"role": "development", "work_subject": "ticket:2", "generation": False},
     "current Change Job Invocation identity is invalid"),
    ({}, {"role": "development", "work_subject": "ticket:2", "generation": 1}, "Delivery Run ID is missing"),
    ({"run_id": "run-1"}, {"role": "publication", "work_subject": "ticket:02", "generation": 1},
     "Ticket Publication Invocation work_subject is invalid"),
    ({"run_id": "run-1"}, {"role": "development", "work_subject": "ticket:02", "generation": 1},
     "current Ticket Change Job Invocation work_subject is invalid"),
    ({"run_id": "run-1"}, {"role": "publication", "work_subject": "ticket:2", "generation": 1},
     "current Ticket Publication job is missing"),
    ({"run_id": "run-1", "run_acceptance": {"acceptance_generation": 2}},
     {"role": "fresh_acceptance", "work_subject": "run-integration:run-1", "generation": 2},
     "Change Job Invocation work_subject is invalid"),
])
def test_invocation_owner_preserves_first_error_without_changing_facts(
    state: dict[str, Any], invocation: dict[str, Any], message: str,
) -> None:
    before = deepcopy((state, invocation))
    with pytest.raises(ValueError) as raised:
        locate_invocation_owner(state, invocation)
    assert str(raised.value) == message
    assert (state, invocation) == before


@pytest.mark.parametrize(("role", "subject", "location", "valid"), [
    ("development", "parent-only:run-1", "parent_job", True),
    ("publication", "parent-only:run-1", "parent_job", True),
    ("final_publication", "run-publication:run-1", "run_publication", True),
    ("reviewer", "run-acceptance:run-1", "run_acceptance", False),
    ("development", "run-integration:run-1", "run_acceptance", False),
])
def test_missing_generation_defaults_only_for_parent_and_final_publication(
    role: str, subject: str, location: str, valid: bool,
) -> None:
    state = {"run_id": "run-1", "parent_job": {}, "run_acceptance": {}, "run_publication": {}}
    invocation = {"role": role, "work_subject": subject, "generation": 1}
    if valid:
        assert locate_invocation_owner(state, invocation).owner is state[location]
    else:
        with pytest.raises(ValueError, match="^Publication Invocation generation is stale$"):
            locate_invocation_owner(state, invocation)


def test_current_owner_location_does_not_replace_the_invocation_start_version() -> None:
    state = _state()
    start = {"attempt_id": "same-attempt", "review_context": {"response": "first"}}
    current = {**deepcopy(start), "review_context": {"response": "second"}, "returned_result": {"kind": "development"}}
    state["run_acceptance"].update(phase="pending", pending_semantic_attempt=current)
    invocation = {"role": "development", "work_subject": "run-integration:run-1",
                  "generation": 5, "semantic_attempt": start}
    before = deepcopy((state, invocation))

    assert locate_invocation_owner(state, invocation).owner is state["run_acceptance"]
    assert invocation["semantic_attempt"] is start
    assert (state, invocation) == before
