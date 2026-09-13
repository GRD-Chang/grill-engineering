from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_fixture import FixtureAgentBackend
from agent_run.agents import DevelopmentResult, ReviewResult
from agent_run.controller import Controller
from agent_run.cli_presentation import _operator_action_view
from agent_run.delivery import TicketDeliveryEngine
from agent_run.state_contract import (
    IncompatibleRunStateError,
    require_current_run_state,
)
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubPublisher, FixtureGitHubReader
from agent_run.operator_gate import (
    active_ticket_gate_mirror_is_consistent,
    has_run_operator_gate,
    operator_gate_identity_is_consistent,
    operator_gate_subject_count,
    operator_gate_subjects,
)
from agent_run.run_orchestration import DeliveryRunEngine
from agent_run.run_driver import DirectRunOperations
from agent_run.state import StateStore
from agent_run.semantic_attempt import allocate_semantic_attempt
from agent_run.review_budget import new_budget
from conftest import seed_run, write_fixture
from support.inprocess_cli import invoke_cli_inprocess
from test_cli import run_internal_stage, load_only_run_state, run_cli, stdout_json
from test_cli_delivery import parent_round_agents, passing_acceptance


def _ticket(
    number: int, *, blocked_by: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Deliver ticket {number}",
        "body": f"Implement ticket {number}.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": blocked_by or [],
    }


@pytest.mark.parametrize("legacy_protocol", [None, 1, 2.0, "2"])
def test_legacy_branch_authority_state_is_rejected_before_reuse(
    git_repo: Path, legacy_protocol: object
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    if legacy_protocol is None:
        state.pop("branch_authority_protocol")
    else:
        state["branch_authority_protocol"] = legacy_protocol
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match="branch authority"):
        controller.resume(str(state["run_id"]))

    archive = states.root / "archived-runs"
    archive.mkdir()
    (states.runs_directory / f"{state['run_id']}.json").rename(
        archive / f"{state['run_id']}.json"
    )
    replacement, resumed = controller.start_or_resume_unfinished(1)

    assert not resumed
    assert replacement["branch_authority_protocol"] == 2
    assert states.load_current_run(str(replacement["run_id"])) is not None


@pytest.mark.parametrize("legacy_protocol", [None, 0, 1.0, "1"])
def test_legacy_semantic_attempt_state_is_rejected_before_reuse(
    git_repo: Path, legacy_protocol: object
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    if legacy_protocol is None:
        state.pop("semantic_attempt_protocol")
    else:
        state["semantic_attempt_protocol"] = legacy_protocol
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match="Semantic Agent Attempt"):
        controller.resume(str(state["run_id"]))


def test_legacy_state_with_multiple_operator_gates_fails_closed(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
    )
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    for ticket_number in (2, 3):
        job = state["ticket_jobs"].setdefault(
            str(ticket_number), {"ticket_number": ticket_number}
        )
        job.update(
            {
                "phase": "blocked" if ticket_number == 2 else "publication_pending",
                "policy_snapshot": dict(state["policy_snapshot"]),
                "review_budget": new_budget(),
                "review_budget_history": [],
            }
        )
    state["ticket_jobs"]["2"].update(
        {
            "blocked_reason": "agent_requires_human",
            "human_blocker_phase": "candidate",
            "human_blockers": ["Ticket 2 needs maintainer input."],
        }
    )

    with pytest.raises(IncompatibleRunStateError, match="multiple current"):
        require_current_run_state(state)


def test_active_only_human_gate_projects_the_current_ticket() -> None:
    active = {
        "ticket_number": 3,
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_blocker_phase": "developing",
        "human_blockers": ["Maintainer input is required."],
    }
    state: dict[str, Any] = {
        "status": "ready_for_human",
        "ticket_jobs": {},
        "active_ticket_job": active,
        "parent": {"number": 1},
        "repository": "example/project",
    }

    assert operator_gate_subjects(state) == [("ticket:3", active)]
    action = _operator_action_view(state)
    assert action is not None
    assert action["type"] == "Human Blocker"
    assert action["object"] == "Ticket #3"
    assert action["phase"] == "developing"
    assert action["reasons"] == ["Maintainer input is required."]


def test_abandonment_pending_overrides_a_local_human_blocker_action() -> None:
    active = {
        "ticket_number": 3,
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_blockers": ["Maintainer input is required."],
    }
    state: dict[str, Any] = {
        "run_id": "run-1",
        "status": "abandonment_pending",
        "ticket_jobs": {"3": deepcopy(active)},
        "active_ticket_job": deepcopy(active),
        "parent": {"number": 1},
        "repository": "example/project",
    }

    action = _operator_action_view(state)

    assert action is not None
    assert action["type"] == "Abandonment Recovery"
    assert action["object"] == "Ticket #3"
    assert action["phase"] == "blocked"
    assert action["reasons"] == ["Run abandonment recovery is incomplete."]
    assert action["next_action"] == "agent-run abandon run-1"


def test_run_repair_supersedes_a_retained_publication_approval_phase() -> None:
    state: dict[str, Any] = {
        "status": "run_acceptance_pending",
        "run_acceptance": {
            "phase": "repairing",
            "repair_job": {"phase": "developing"},
        },
        "run_publication": {"phase": "ready_for_approval"},
    }

    assert operator_gate_subjects(state) == []
    assert not has_run_operator_gate(state)
    assert not DirectRunOperations._cannot_advance(state)
    assert _operator_action_view(state) is None


def test_ordinary_run_cannot_advance_an_operator_stopped_boundary() -> None:
    state: dict[str, Any] = {"status": "operator_stopped"}

    assert has_run_operator_gate(state)
    assert DirectRunOperations._cannot_advance(state)


def test_pending_acceptance_cannot_claim_a_publication_approval_gate() -> None:
    state: dict[str, Any] = {
        "status": "run_acceptance_pending",
        "run_acceptance": {"phase": "pending"},
        "run_publication": {"phase": "ready_for_approval"},
    }

    assert operator_gate_subject_count(state) == 1
    assert not operator_gate_identity_is_consistent(state)


@pytest.mark.parametrize(
    "publication_phase", ["blocked", "ready_for_human", "publication_pending"]
)
def test_run_repair_does_not_hide_a_publication_gate(
    publication_phase: str,
) -> None:
    state: dict[str, Any] = {
        "status": "ready_for_human",
        "run_acceptance": {
            "phase": "repairing",
            "repair_job": {"phase": "developing"},
        },
        "run_publication": {"phase": publication_phase},
    }

    assert [location for location, _subject in operator_gate_subjects(state)] == [
        "run_publication"
    ]
    assert has_run_operator_gate(state)


def test_run_repair_and_publication_gates_fail_closed_together() -> None:
    state: dict[str, Any] = {
        "status": "ready_for_human",
        "run_acceptance": {
            "phase": "repairing",
            "repair_job": {"phase": "blocked"},
        },
        "run_publication": {"phase": "blocked"},
    }

    assert operator_gate_subject_count(state) == 2


def test_consistent_active_ticket_gate_mirror_counts_once() -> None:
    active = {
        "ticket_number": 3,
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_blockers": ["Maintainer input is required."],
    }
    state: dict[str, Any] = {
        "status": "ready_for_human",
        "ticket_jobs": {"3": deepcopy(active)},
        "active_ticket_job": deepcopy(active),
    }

    assert operator_gate_subject_count(state) == 1
    assert [location for location, _subject in operator_gate_subjects(state)] == [
        "ticket:3"
    ]


@pytest.mark.parametrize("active", [None, {"ticket_number": 3, "phase": "completed"}])
def test_canonical_ticket_gate_requires_a_current_active_mirror(
    active: dict[str, Any] | None,
) -> None:
    blocked = {
        "ticket_number": 3,
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_blockers": ["Maintainer input is required."],
    }
    state: dict[str, Any] = {
        "status": "ready_for_human",
        "ticket_jobs": {"3": blocked},
        "active_ticket_job": active,
    }

    assert not active_ticket_gate_mirror_is_consistent(state)


@pytest.mark.parametrize("active_kind", ["missing", "terminal"])
def test_canonical_ticket_gate_without_current_active_mirror_fails_closed(
    git_repo: Path,
    active_kind: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    blocked = state["ticket_jobs"]["2"]
    blocked.update(
        {
            "phase": "blocked",
            "blocked_reason": "agent_requires_human",
            "human_blockers": ["Maintainer input is required."],
        }
    )
    state["active_ticket_job"] = (
        None
        if active_kind == "missing"
        else {"ticket_number": 2, "phase": "completed"}
    )
    state["status"] = "ready_for_human"
    state["terminal_kind"] = "waiting_human"

    with pytest.raises(IncompatibleRunStateError, match="active Ticket gate mirror"):
        require_current_run_state(state)


def test_different_active_and_canonical_ticket_gates_count_twice() -> None:
    def blocked_ticket(number: int) -> dict[str, Any]:
        return {
            "ticket_number": number,
            "phase": "blocked",
            "blocked_reason": "agent_requires_human",
            "human_blockers": [f"Ticket {number} needs input."],
        }

    state: dict[str, Any] = {
        "status": "ready_for_human",
        "ticket_jobs": {"2": blocked_ticket(2)},
        "active_ticket_job": blocked_ticket(3),
    }

    assert operator_gate_subject_count(state) == 2
    assert [location for location, _subject in operator_gate_subjects(state)] == [
        "ticket:3",
        "ticket:2",
    ]


def test_unbound_execution_failure_is_distinct_from_ticket_human_gate() -> None:
    blocked = {
        "ticket_number": 2,
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_blockers": ["Ticket 2 needs input."],
    }
    state: dict[str, Any] = {
        "status": "execution_failed",
        "terminal_kind": "execution_failed",
        "ticket_jobs": {"2": deepcopy(blocked)},
        "active_ticket_job": deepcopy(blocked),
        "active_agent_invocation": None,
        "diagnostics": [{"code": "command_failed", "message": "runner failed"}],
    }

    assert operator_gate_subject_count(state) == 2
    assert [location for location, _subject in operator_gate_subjects(state)] == [
        "ticket:2",
        "run",
    ]


def test_unbound_execution_failure_and_ticket_human_gate_fail_closed(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        states,
    )
    state, _ = controller.start(1)
    blocked = state["ticket_jobs"]["2"]
    blocked.update(
        {
            "phase": "blocked",
            "blocked_reason": "agent_requires_human",
            "human_blockers": ["Ticket 2 needs input."],
        }
    )
    state["active_ticket_job"] = deepcopy(blocked)
    state.update(
        {
            "status": "execution_failed",
            "terminal_kind": "execution_failed",
            "active_agent_invocation": None,
            "diagnostics": [
                {"code": "command_failed", "message": "runner failed"}
            ],
        }
    )

    with pytest.raises(IncompatibleRunStateError, match="multiple current"):
        require_current_run_state(state)

    run_id = str(state["run_id"])
    states.save_run(run_id, state)
    state_path = states.runs_directory / f"{run_id}.json"
    state_before = state_path.read_bytes()
    fixture_before = fixture.read_bytes()
    commands = (
        ("status", run_id, "--json"),
        ("history", run_id, "--json"),
        ("resume", run_id),
        ("run", "1"),
    )
    for command in commands:
        rejected = run_cli(git_repo, fixture, *command)
        assert rejected.returncode == 2
        assert stdout_json(rejected)["status"] == "incompatible_run_state", (
            command,
            rejected.stdout,
            rejected.stderr,
        )
        assert state_path.read_bytes() == state_before
        assert fixture.read_bytes() == fixture_before


def test_ticket_gate_cannot_coexist_with_another_active_ticket() -> None:
    blocked = {
        "ticket_number": 2,
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_blockers": ["Ticket 2 needs input."],
    }
    state: dict[str, Any] = {
        "status": "ready_for_human",
        "ticket_jobs": {"2": blocked, "3": {"ticket_number": 3, "phase": "developing"}},
        "active_ticket_job": {"ticket_number": 3, "phase": "developing"},
    }

    assert not active_ticket_gate_mirror_is_consistent(state)


def test_ticket_gate_and_another_active_ticket_fail_closed(git_repo: Path) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2), "3": _ticket(3)}
    )
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    blocked = state["ticket_jobs"]["2"]
    blocked.update(
        {
            "phase": "blocked",
            "blocked_reason": "agent_requires_human",
            "human_blockers": ["Ticket 2 needs input."],
        }
    )
    active = {"ticket_number": 3, "phase": "developing"}
    state["ticket_jobs"]["3"] = deepcopy(active)
    state["active_ticket_job"] = deepcopy(active)
    state["status"] = "ready_for_human"
    state["terminal_kind"] = "waiting_human"

    with pytest.raises(IncompatibleRunStateError, match="active Ticket gate mirror"):
        require_current_run_state(state)


@pytest.mark.parametrize("location", ["run_acceptance", "run_publication"])
def test_run_gate_cannot_coexist_with_an_active_ticket(location: str) -> None:
    state: dict[str, Any] = {
        "status": "ready_for_human",
        "ticket_jobs": {"3": {"ticket_number": 3, "phase": "developing"}},
        "active_ticket_job": {"ticket_number": 3, "phase": "developing"},
    }
    state[location] = {
        "phase": "ready_for_human",
        "blocked_reason": "reviewer_requires_human",
        "human_blockers": ["Run needs input."],
    }

    assert not active_ticket_gate_mirror_is_consistent(state)


def test_run_gate_and_an_active_ticket_fail_closed(git_repo: Path) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    active = state["active_ticket_job"]
    assert isinstance(active, dict)
    active["phase"] = "developing"
    state["ticket_jobs"]["2"] = deepcopy(active)
    state["run_acceptance"] = {
        "phase": "ready_for_human",
        "blocked_reason": "reviewer_requires_human",
        "human_blockers": ["Run needs input."],
    }
    state["status"] = "ready_for_human"
    state["terminal_kind"] = "waiting_human"

    with pytest.raises(IncompatibleRunStateError, match="active Ticket gate mirror"):
        require_current_run_state(state)


def test_diagnostic_gate_evidence_projects_execution_failure_subject() -> None:
    ticket = {
        "ticket_number": 3,
        "phase": "reviewing",
        "candidate_sha": "candidate-sha",
    }
    state: dict[str, Any] = {
        "status": "execution_failed",
        "ticket_jobs": {"3": ticket},
        "active_ticket_job": deepcopy(ticket),
        "active_agent_invocation": None,
        "parent": {"number": 1},
        "repository": "example/project",
        "diagnostics": [
            {
                "code": "command_failed",
                "message": "Runner failed after preserving the candidate.",
                "operator_gate": {
                    "work_subject": "ticket:3",
                    "action_kind": "execution_failure",
                    "phase": "reviewing",
                    "reason": "command_failed",
                },
            }
        ],
    }

    assert operator_gate_subject_count(state) == 1
    action = _operator_action_view(state)
    assert action is not None
    assert action["type"] == "Execution Failure"
    assert action["object"] == "Ticket #3"
    assert action["phase"] == "reviewing"
    assert action["reasons"] == ["Runner failed after preserving the candidate."]
    assert action["preserved"] == "Candidate candidate-sha"
    assert action["next_action"] == "agent-run resume 1 --repo example/project"


def test_distinct_diagnostic_gate_bindings_count_as_multiple_actions() -> None:
    state: dict[str, Any] = {
        "status": "execution_failed",
        "terminal_kind": "execution_failed",
        "ticket_jobs": {
            "2": {"ticket_number": 2, "phase": "developing"},
            "3": {"ticket_number": 3, "phase": "reviewing"},
        },
        "active_ticket_job": None,
        "active_agent_invocation": None,
        "diagnostics": [
            {
                "code": "first_failure",
                "message": "First action failed.",
                "operator_gate": {
                    "work_subject": "ticket:2",
                    "action_kind": "execution_failure",
                    "phase": "developing",
                    "reason": "first_failure",
                },
            },
            {
                "code": "second_failure",
                "message": "Second action failed.",
                "operator_gate": {
                    "work_subject": "ticket:3",
                    "action_kind": "execution_failure",
                    "phase": "reviewing",
                    "reason": "second_failure",
                },
            },
        ],
    }

    assert operator_gate_subject_count(state) == 2


def test_duplicate_diagnostic_gate_bindings_count_as_one_action() -> None:
    binding = {
        "work_subject": "ticket:2",
        "action_kind": "execution_failure",
        "phase": "developing",
        "reason": "command_failed",
    }
    state: dict[str, Any] = {
        "status": "execution_failed",
        "ticket_jobs": {"2": {"ticket_number": 2, "phase": "developing"}},
        "active_ticket_job": None,
        "diagnostics": [
            {"operator_gate": deepcopy(binding)},
            {"operator_gate": deepcopy(binding)},
        ],
    }

    assert operator_gate_subject_count(state) == 1


def test_completed_ticket_cannot_be_rebound_by_a_diagnostic_gate(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    completed = state["ticket_jobs"]["2"]
    completed["phase"] = "completed"
    state.update(
        {
            "status": "execution_failed",
            "terminal_kind": "execution_failed",
            "active_ticket_job": deepcopy(completed),
            "diagnostics": [
                {
                    "operator_gate": {
                        "work_subject": "ticket:2",
                        "action_kind": "execution_failure",
                        "phase": "completed",
                        "reason": "command_failed",
                    }
                }
            ],
        }
    )

    assert not active_ticket_gate_mirror_is_consistent(state)
    with pytest.raises(IncompatibleRunStateError, match="current lifecycle"):
        require_current_run_state(state)


def test_distinct_diagnostic_gate_bindings_fail_canonical_validation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2), "3": _ticket(3)}
    )
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    ticket_two = state["ticket_jobs"]["2"]
    ticket_three = {**deepcopy(ticket_two), "ticket_number": 3, "phase": "reviewing"}
    ticket_two["phase"] = "developing"
    state["ticket_jobs"]["3"] = ticket_three
    state["active_ticket_job"] = None
    state["status"] = "execution_failed"
    state["terminal_kind"] = "execution_failed"
    state["active_agent_invocation"] = None
    state["diagnostics"] = [
        {
            "code": "first_failure",
            "message": "First action failed.",
            "operator_gate": {
                "work_subject": "ticket:2",
                "action_kind": "execution_failure",
                "phase": "developing",
                "reason": "first_failure",
            },
        },
        {
            "code": "second_failure",
            "message": "Second action failed.",
            "operator_gate": {
                "work_subject": "ticket:3",
                "action_kind": "execution_failure",
                "phase": "reviewing",
                "reason": "second_failure",
            },
        },
    ]

    with pytest.raises(IncompatibleRunStateError, match="multiple current"):
        require_current_run_state(state)


def test_same_ticket_human_blocker_and_execution_failure_fail_closed(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    job = state["ticket_jobs"]["2"]
    job.update(
        {
            "phase": "blocked",
            "blocked_reason": "agent_requires_human",
            "human_blocker_phase": "developing",
            "human_blockers": ["Maintainer input is required."],
        }
    )
    state.update(
        {
            "active_ticket_job": deepcopy(job),
            "status": "execution_failed",
            "terminal_kind": "execution_failed",
            "active_agent_invocation": None,
            "diagnostics": [
                {
                    "code": "command_failed",
                    "message": "A separate command failed.",
                    "operator_gate": {
                        "work_subject": "ticket:2",
                        "action_kind": "execution_failure",
                        "phase": "blocked",
                        "reason": "command_failed",
                    },
                }
            ],
        }
    )

    assert operator_gate_subject_count(state) == 2
    with pytest.raises(IncompatibleRunStateError, match="multiple current"):
        require_current_run_state(state)


@pytest.mark.parametrize(
    "status",
    [
        "active",
        "publication_pending",
        "parent_approval_pending",
        "run_approval_pending",
    ],
)
def test_local_human_blocker_requires_a_matching_top_level_status(
    git_repo: Path,
    status: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    job = state["ticket_jobs"]["2"]
    job.update(
        {
            "phase": "blocked",
            "blocked_reason": "agent_requires_human",
            "human_blockers": ["Maintainer input is required."],
        }
    )
    state["active_ticket_job"] = deepcopy(job)
    state["status"] = status

    assert operator_gate_subject_count(state) == 1
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(state)


@pytest.mark.parametrize(
    ("status", "location", "subject"),
    [
        (
            "publication_pending",
            "run_publication",
            {"phase": "publication_pending"},
        ),
        (
            "parent_approval_pending",
            "parent",
            {"phase": "ready_for_approval"},
        ),
        (
            "run_approval_pending",
            "run_publication",
            {"phase": "ready_for_approval"},
        ),
    ],
)
def test_local_gate_action_can_match_its_top_level_status(
    status: str,
    location: str,
    subject: dict[str, Any],
) -> None:
    state: dict[str, Any] = {"status": status, "ticket_jobs": {}}
    if location == "parent":
        state["parent_job"] = subject
    else:
        state["run_publication"] = subject

    assert operator_gate_subject_count(state) == 1
    assert operator_gate_identity_is_consistent(state)


@pytest.mark.parametrize(
    ("status", "action_kind"),
    [
        ("ready_for_human", "execution_failure"),
        ("abandonment_pending", "execution_failure"),
        ("supervision_timeout", "deterministic_contradiction"),
        ("run_approval_pending", "execution_failure"),
    ],
)
def test_diagnostic_gate_action_must_match_top_level_status(
    git_repo: Path,
    status: str,
    action_kind: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    job = state["ticket_jobs"]["2"]
    job["phase"] = "reviewing"
    state.update(
        {
            "active_ticket_job": deepcopy(job),
            "status": status,
            "terminal_kind": status,
            "diagnostics": [
                {
                    "operator_gate": {
                        "work_subject": "ticket:2",
                        "action_kind": action_kind,
                        "phase": "reviewing",
                        "reason": "mismatched_action",
                    }
                }
            ],
        }
    )

    assert operator_gate_subject_count(state) == 1
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(state)


def test_completed_parent_cannot_be_rebound_by_a_diagnostic_gate(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={})
    agents = git_repo / "parent-agents.json"
    agents.write_text(
        json.dumps(parent_round_agents(1, passing_last=True)),
        encoding="utf-8",
    )
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]
    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agents),
    )
    assert delivered.returncode == 0, delivered.stderr
    completed = run_cli(git_repo, fixture, "approve", run_id)
    assert completed.returncode == 0, completed.stderr
    state = load_only_run_state(git_repo)
    assert state["parent_job"]["phase"] == "completed"
    state.update(
        {
            "status": "deterministic_contradiction",
            "terminal_kind": "deterministic_contradiction",
            "diagnostics": [
                {
                    "code": "parent_rebound",
                    "message": "Completed Parent was rebound.",
                    "operator_gate": {
                        "work_subject": f"parent-only:{run_id}",
                        "action_kind": "deterministic_contradiction",
                        "phase": "completed",
                        "reason": "parent_rebound",
                    },
                }
            ],
        }
    )

    with pytest.raises(IncompatibleRunStateError, match="current lifecycle"):
        require_current_run_state(state)


@pytest.mark.parametrize(
    ("work_subject", "state_subject", "expected_object"),
    [
        (
            "parent-only:run-1",
            "parent_job",
            "Parent Issue #1",
        ),
        (
            "run-repair:run-1",
            "run_repair",
            "Run Acceptance",
        ),
    ],
)
def test_diagnostic_gate_evidence_projects_each_change_job_subject(
    work_subject: str,
    state_subject: str,
    expected_object: str,
) -> None:
    job = {
        "phase": "reviewing",
        "candidate_sha": "candidate-sha",
        "pr_number": 12,
    }
    state: dict[str, Any] = {
        "run_id": "run-1",
        "status": "blocked",
        "terminal_kind": "waiting_human",
        "ticket_jobs": {},
        "active_ticket_job": None,
        "parent": {"number": 1},
        "repository": "example/project",
        "diagnostics": [
            {
                "code": "change_pr_head_changed_externally",
                "message": "Change PR changed outside the current Generation",
                "operator_gate": {
                    "work_subject": work_subject,
                    "action_kind": "deterministic_contradiction",
                    "phase": "reviewing",
                    "reason": "change_pr_head_changed_externally",
                },
            }
        ],
    }
    if state_subject == "parent_job":
        state["parent_job"] = job
    else:
        state["run_acceptance"] = {"phase": "repairing", "repair_job": job}

    action = _operator_action_view(state)

    assert action is not None
    assert action["type"] == "Deterministic Contradiction"
    assert action["object"] == expected_object
    assert action["phase"] == "reviewing"
    assert action["reasons"] == [
        "Change PR changed outside the current Generation"
    ]
    assert action["preserved"] == "Candidate candidate-sha；PR #12"
    assert action["next_action"] == (
        "修复诊断中的确定性外部矛盾后执行 "
        "agent-run run 1 --repo example/project"
    )


@pytest.mark.parametrize("mirror_kind", ["missing", "divergent"])
def test_active_ticket_gate_mirror_must_match_canonical_job(
    git_repo: Path,
    mirror_kind: str,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    active = {
        "ticket_number": 2,
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_blocker_phase": "developing",
        "human_blockers": ["Maintainer input is required."],
    }
    state["status"] = "ready_for_human"
    state["terminal_kind"] = "waiting_human"
    state["active_ticket_job"] = deepcopy(active)
    if mirror_kind == "missing":
        state["ticket_jobs"].pop("2", None)
    else:
        state["ticket_jobs"]["2"] = {
            **deepcopy(active),
            "human_blockers": ["Different persisted blocker."],
        }

    with pytest.raises(IncompatibleRunStateError, match="active Ticket gate mirror"):
        require_current_run_state(state)


def test_global_active_ticket_gate_requires_an_exact_canonical_mirror(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    active = {
        "ticket_number": 2,
        "phase": "developing",
        "human_blockers": [],
    }
    state["status"] = "supervision_timeout"
    state["terminal_kind"] = "supervision_timeout"
    state["active_ticket_job"] = deepcopy(active)
    state["ticket_jobs"]["2"] = {**deepcopy(active), "phase": "candidate"}

    assert not active_ticket_gate_mirror_is_consistent(state)
    with pytest.raises(IncompatibleRunStateError, match="active Ticket gate mirror"):
        require_current_run_state(state)


def test_deterministic_contradiction_keeps_the_triggering_work_subject() -> None:
    state: dict[str, Any] = {
        "status": "deterministic_contradiction",
        "ticket_jobs": {
            "3": {
                "ticket_number": 3,
                "phase": "reviewing",
                "candidate_sha": "candidate-sha",
            }
        },
        "active_agent_invocation": {
            "work_subject": "ticket:3",
            "phase": "reviewing",
        },
        "diagnostics": [
            {"code": "foreign_ticket_pr", "message": "PR identity changed."}
        ],
    }

    action = _operator_action_view(state)

    assert action is not None
    assert action["type"] == "Deterministic Contradiction"
    assert action["object"] == "Ticket #3"
    assert action["phase"] == "reviewing"
    assert action["preserved"] == "Candidate candidate-sha"


def test_historical_mechanical_revision_does_not_hide_current_execution_gate() -> None:
    current = {
        "ticket_number": 3,
        "phase": "reviewing",
        "candidate_sha": "current-candidate",
    }
    state: dict[str, Any] = {
        "status": "execution_failed",
        "ticket_jobs": {
            "2": {
                "ticket_number": 2,
                "phase": "blocked",
                "blocked_reason": "effective_revision_mismatch",
            },
            "3": current,
        },
        "active_ticket_job": current,
        "active_agent_invocation": {
            "work_subject": "ticket:3",
            "phase": "reviewing",
        },
        "diagnostics": [{"message": "Current reviewer process failed."}],
    }

    assert has_run_operator_gate(state)
    action = _operator_action_view(state)
    assert action is not None
    assert action["type"] == "Execution Failure"
    assert action["object"] == "Ticket #3"


def test_mechanical_revision_becomes_global_gate_at_requeue_boundary() -> None:
    job = {
        "ticket_number": 2,
        "phase": "blocked",
        "blocked_reason": "effective_revision_mismatch",
    }
    state: dict[str, Any] = {
        "status": "requeue_required",
        "ticket_jobs": {"2": job},
        "active_ticket_job": job,
        "requeue_required": {"work_subject": "ticket:2"},
    }

    assert has_run_operator_gate(state)
    assert operator_gate_subject_count(state) == 1
    action = _operator_action_view(state)
    assert action is not None
    assert action["type"] == "Requeue Required"
    assert action["object"] == "Ticket #2"
    assert action["phase"] == "blocked"


def test_requeue_gate_generation_must_match_the_current_change_job(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    controller = Controller(
        FixtureGitHubReader(fixture),
        GitRepository(git_repo),
        StateStore(git_repo / ".agent-run"),
    )
    state, _ = controller.start(1)
    job = state["ticket_jobs"]["2"]
    job.update(
        {
            "ticket_branch_generation": 2,
            "ticket_branch": f"agent-run/{state['run_id']}/ticket-2",
            "phase": "blocked",
            "blocked_reason": "effective_revision_mismatch",
            "policy_snapshot": deepcopy(state["policy_snapshot"]),
            "review_budget": new_budget(),
            "review_budget_history": [],
        }
    )
    state.update(
        {
            "status": "requeue_required",
            "terminal_kind": "requeue_required",
            "active_ticket_job": deepcopy(job),
            "diagnostics": [
                {
                    "code": "ticket_requirements_changed",
                    "message": "Ticket requirements changed; run requeue",
                }
            ],
            "requeue_required": {
                "work_subject": "ticket:2",
                "generation": 1,
                "reason": "ticket_requirements_changed",
            },
        }
    )

    assert not operator_gate_identity_is_consistent(state)
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(state)

    state["requeue_required"]["generation"] = 2

    assert operator_gate_identity_is_consistent(state)
    require_current_run_state(state)

    state["requeue_required"]["reason"] = "fabricated_reason"
    assert not operator_gate_identity_is_consistent(state)
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(state)

    state["requeue_required"]["reason"] = "ticket_requirements_changed"
    state["diagnostics"][0]["code"] = "ticket_base_changed"
    assert not operator_gate_identity_is_consistent(state)
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(state)


def test_pending_semantic_attempt_owner_mismatch_is_rejected_without_invocation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    job = state["ticket_jobs"]["2"]
    job["review_budget"] = new_budget()
    job["review_budget_history"] = []
    allocate_semantic_attempt(
        job,
        role="development",
        work_subject="ticket:999",
        generation=1,
        currentness_boundary={"base_sha": state["base"]["sha"]},
        ordinal=1,
        budget_window=job["review_budget"]["window"],
    )
    state["active_ticket_job"] = dict(job)
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match="owner mismatch"):
        controller.resume(str(state["run_id"]))


def test_controller_reprepare_authority_is_rejected_outside_run_repair(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": _ticket(2)})
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    state["ticket_jobs"]["2"]["controller_candidate_reprepare"] = {
        "kind": "controller_currentness_reprepare"
    }
    states.save_run(str(state["run_id"]), state)

    with pytest.raises(IncompatibleRunStateError, match="outside Run Repair"):
        controller.resume(str(state["run_id"]))


def _publication(number: int) -> dict[str, str]:
    return {
        "commit_message": f"feat(delivery): complete ticket {number}",
        "pr_title": f"feat(delivery): complete ticket {number}",
        "pr_body_markdown": f"""
## What Problem This Solves

Ticket {number} was not integrated into the Delivery Run.

## Why This Change Was Made

The scripted implementation delivers its required behavior.

## User Impact

The complete Ticket DAG can advance without another command.

## Evidence

The end-to-end scripted DAG scenario passed.
""".strip(),
    }


def _human_acceptance(thread_id: str) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "checks": {
            "e2e": {
                "status": "blocked",
                "evidence": "发生：产品决策缺失；尝试：已读取权威输入；人必须：作出产品决策。",
                "findings": [],
            },
            "standards": {
                "status": "pass",
                "evidence": "审查范围或基线：仓库编码规范与当前 diff；结论：未发现违反项。",
                "findings": [],
            },
            "spec": {
                "status": "pass",
                "evidence": "已核对的验收标准：当前 Ticket 的全部要求；覆盖结论：已覆盖。",
                "findings": [],
            },
        },
    }


class RevisionChangingAgents:
    def __init__(self, fixture: Path) -> None:
        self.fixture = fixture
        self.development_requests: list[dict[str, Any]] = []
        self.publication_requests: list[dict[str, Any]] = []
        self.review_requests: list[dict[str, Any]] = []

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        self.development_requests.append(request)
        checkout = Path(str(request["checkout"]))
        attempt = len(self.development_requests)
        (checkout / "revision.txt").write_text(
            f"revision {attempt}\n", encoding="utf-8"
        )
        if attempt == 1:
            data = json.loads(self.fixture.read_text(encoding="utf-8"))
            data["issues"]["2"]["body"] += "\nNew authoritative requirement."
            self.fixture.write_text(json.dumps(data), encoding="utf-8")
        return DevelopmentResult(
            thread_id="developer-2",
            summary=f"Implemented revision {attempt}.",
        )

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.publication_requests.append(request)
        return _publication(2)

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_requests.append(request)
        return ReviewResult(
            thread_id="reviewer-2",
            artifact={
                key: value
                for key, value in passing_acceptance(
                    "unused", "The latest revision passed."
                ).items()
                if key != "thread_id"
            },
        )


class TicketRemovingAgents:
    def __init__(self, fixture: Path) -> None:
        self.fixture = fixture
        self.publication_calls = 0
        self.review_calls = 0

    def develop(self, request: dict[str, Any]) -> DevelopmentResult:
        checkout = Path(str(request["checkout"]))
        (checkout / "removed.txt").write_text("stale\n", encoding="utf-8")
        data = json.loads(self.fixture.read_text(encoding="utf-8"))
        data["parent"]["sub_issues"].remove(2)
        self.fixture.write_text(json.dumps(data), encoding="utf-8")
        return DevelopmentResult(
            thread_id="developer-2",
            summary="The ticket disappeared while work was active.",
        )

    def publication(self, request: dict[str, Any]) -> dict[str, Any]:
        self.publication_calls += 1
        return _publication(2)

    def review(self, request: dict[str, Any]) -> ReviewResult:
        self.review_calls += 1
        raise AssertionError("removed Ticket must not reach review")


def test_deliver_advances_the_complete_dag_and_enters_run_acceptance(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": _ticket(2),
            "3": _ticket(
                3, blocked_by=[{"number": 2, "state": "OPEN"}]
            ),
            "4": _ticket(
                4, blocked_by=[{"number": 2, "state": "OPEN"}]
            ),
            "5": _ticket(
                5,
                blocked_by=[
                    {"number": 3, "state": "OPEN"},
                    {"number": 4, "state": "OPEN"},
                ],
            ),
        },
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented ticket 2.",
                        "write_files": {"ticket-2.txt": "done\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-3",
                        "summary": "Implemented ticket 3.",
                        "write_files": {"ticket-3.txt": "done\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-4",
                        "summary": "Implemented ticket 4.",
                        "write_files": {"ticket-4.txt": "done\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-5",
                        "summary": "Implemented ticket 5.",
                        "write_files": {"ticket-5.txt": "done\n"},
                    },
                ],
                "publications": [
                    _publication(2),
                    _publication(3),
                    _publication(4),
                    _publication(5),
                ],
                "reviews": [
                    passing_acceptance("reviewer-2", "Ticket 2 passed."),
                    passing_acceptance("reviewer-3", "Ticket 3 passed."),
                    passing_acceptance("reviewer-4", "Ticket 4 passed."),
                    passing_acceptance("reviewer-5", "Ticket 5 passed."),
                ],
            }
        ),
        encoding="utf-8",
    )
    started = seed_run(git_repo, fixture, "1")
    run_id = stdout_json(started)["run_id"]

    delivered = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert delivered.returncode == 0, delivered.stdout
    assert stdout_json(delivered)["status"] == "run_acceptance_pending"
    state = load_only_run_state(git_repo)
    assert state["active_ticket_job"] is None
    assert state["terminal_kind"] == "all_tickets_completed"
    assert [
        state["ticket_jobs"][number]["phase"]
        for number in ("2", "3", "4", "5")
    ] == ["completed", "completed", "completed", "completed"]
    assert (
        state["ticket_jobs"]["3"]["base_sha"]
        == state["ticket_jobs"]["2"]["integrated_sha"]
    )
    assert (
        state["ticket_jobs"]["4"]["base_sha"]
        == state["ticket_jobs"]["3"]["integrated_sha"]
    )
    assert (
        state["ticket_jobs"]["5"]["base_sha"]
        == state["ticket_jobs"]["4"]["integrated_sha"]
    )
    mutable_fixture = json.loads(fixture.read_text(encoding="utf-8"))
    assert mutable_fixture["delivery"]["closed_issues"] == [2, 3, 4, 5]
    assert len(mutable_fixture["delivery"]["pull_requests"]) == 4
    commit_count = subprocess.run(
        [
            "git",
            "rev-list",
            "--count",
            f"{state['base']['sha']}..{state['run_branch']}",
        ],
        cwd=git_repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert commit_count == "4"


def test_graph_change_fails_closed_with_auditable_revisions(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    started = seed_run(git_repo, fixture, "1")
    run_id = stdout_json(started)["run_id"]
    original = load_only_run_state(git_repo)

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(3)
    data["issues"]["3"] = _ticket(
        3, blocked_by=[{"number": 2, "state": "OPEN"}]
    )
    fixture.write_text(json.dumps(data), encoding="utf-8")

    paused = seed_run(git_repo, fixture, "1")

    assert paused.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "unsupported_scope_change"
    assert state["terminal_kind"] == "unsupported_scope_change"
    assert state["active_ticket_job"] is None
    assert state["ticket_graph"] == original["ticket_graph"]
    change = state["unsupported_scope_change"]
    assert change["accepted_graph_revision"] == original["ticket_graph"]["revision"]
    assert change["observed_graph_revision"] != change["accepted_graph_revision"]
    impact = change["graph_change_summary"]
    assert impact["added_tickets"] == [3]
    assert impact["removed_tickets"] == []
    assert impact["added_dependencies"] == [
        {"ticket_number": 3, "blocked_by": 2}
    ]

    assert change["observed_ticket_graph"]["ordered_ticket_numbers"] == [2, 3]
    missing_action = deepcopy(state)
    missing_action.pop("unsupported_scope_change")
    assert not operator_gate_identity_is_consistent(missing_action)
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(missing_action)
    mismatched_reason = deepcopy(state)
    mismatched_reason["diagnostics"][0]["code"] = "completed_ticket_reopened"
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(mismatched_reason)
    incomplete_summary = deepcopy(state)
    incomplete_summary["unsupported_scope_change"]["graph_change_summary"] = {}
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(incomplete_summary)
    malformed_observed_graph = deepcopy(state)
    malformed_observed_graph["unsupported_scope_change"]["observed_ticket_graph"][
        "ordered_ticket_numbers"
    ] = ["2", "3"]
    with pytest.raises(IncompatibleRunStateError, match="action identity"):
        require_current_run_state(malformed_observed_graph)

    data["parent"]["sub_issues"] = [2]
    data["issues"].pop("3")
    fixture.write_text(json.dumps(data), encoding="utf-8")
    restored = seed_run(git_repo, fixture, "1")

    assert restored.returncode == 0
    restored_state = load_only_run_state(git_repo)
    assert restored_state["status"] == "active"
    assert "unsupported_scope_change" not in restored_state


def test_later_graph_drift_updates_observed_revision_without_accepting_it(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1")
    )["run_id"]
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(3)
    data["issues"]["3"] = _ticket(3)
    fixture.write_text(json.dumps(data), encoding="utf-8")
    seed_run(git_repo, fixture, "1")
    first = load_only_run_state(git_repo)["unsupported_scope_change"]
    accepted = first["accepted_graph_revision"]
    first_observed = first["observed_graph_revision"]

    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(4)
    data["issues"]["4"] = _ticket(4)
    fixture.write_text(json.dumps(data), encoding="utf-8")
    resumed = seed_run(git_repo, fixture, "1")

    assert resumed.returncode == 2
    state = load_only_run_state(git_repo)
    change = state["unsupported_scope_change"]
    assert state["status"] == "unsupported_scope_change"
    assert change["accepted_graph_revision"] == accepted
    assert change["observed_graph_revision"] != first_observed
    assert change["graph_change_summary"]["added_tickets"] == [3, 4]


def test_legacy_run_fails_closed_before_reading_graph_drift(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    state.pop("accepted_ticket_graph_revision")
    states.save_run(str(state["run_id"]), state)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["sub_issues"].append(3)
    data["issues"]["3"] = _ticket(3)
    fixture.write_text(json.dumps(data), encoding="utf-8")

    before = states.load_run(str(state["run_id"]))
    with pytest.raises(IncompatibleRunStateError, match="legacy state"):
        controller.resume(str(state["run_id"]))

    assert states.load_run(str(state["run_id"])) == before


def test_parent_clarification_and_comments_do_not_change_the_ticket_graph(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1")
    )["run_id"]
    original = load_only_run_state(git_repo)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    data["parent"]["body"] += "\nClarify the intended outcome."
    data["issues"]["2"]["comments"] = [
        {"author": "maintainer", "body": "Useful context only."}
    ]
    data["issues"]["2"]["assignees"] = ["maintainer"]
    data["issues"]["2"]["updated_at"] = "2099-01-01T00:00:00Z"
    fixture.write_text(json.dumps(data), encoding="utf-8")

    resumed = seed_run(git_repo, fixture, "1")

    assert resumed.returncode == 0
    state = load_only_run_state(git_repo)
    assert state["status"] == "active"
    assert state["ticket_graph"]["revision"] == (
        original["ticket_graph"]["revision"]
    )
    assert state["parent"]["revision"] != original["parent"]["revision"]
    assert (
        state["ticket_graph"]["tickets"]["2"]["content_revision"]
        == original["ticket_graph"]["tickets"]["2"]["content_revision"]
    )
    assert "pending_structure_change" not in state


def test_human_blocked_ticket_gates_independent_work_until_resume(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={
            "2": _ticket(2),
            "3": _ticket(3),
            "4": _ticket(
                4, blocked_by=[{"number": 2, "state": "OPEN"}]
            ),
        },
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Reached the product decision boundary.",
                        "write_files": {"ticket-2.txt": "candidate\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-3",
                        "summary": "Implemented independent ticket 3.",
                        "write_files": {"ticket-3.txt": "done\n"},
                    },
                ],
                "publications": [_publication(2), _publication(3)],
                "reviews": [
                    _human_acceptance("reviewer-2"),
                    passing_acceptance("reviewer-3", "Ticket 3 passed."),
                ],
            }
        ),
        encoding="utf-8",
    )
    started = seed_run(git_repo, fixture, "1", idle_control=True)
    run_id = stdout_json(started)["run_id"]

    result = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agent_fixture),
    )

    assert result.returncode == 2
    state = load_only_run_state(git_repo)
    assert state["status"] == "ready_for_human"
    assert state["terminal_kind"] == "waiting_human"
    assert state["active_ticket_job"]["ticket_number"] == 2
    assert state["ticket_jobs"]["2"]["phase"] == "blocked"
    assert "3" not in state["ticket_jobs"]
    assert "4" not in state["ticket_jobs"]
    status_view = invoke_cli_inprocess(git_repo, fixture, "status", run_id)
    assert status_view.returncode == 0
    assert "仓库:       example/project" in status_view.stdout
    assert "整体需求:   #1 Parent spec" in status_view.stdout
    assert "需要你处理" in status_view.stdout
    assert "类型: 需要人工处理" in status_view.stdout
    assert "对象: 子任务 #2" in status_view.stdout
    assert "阶段: 代码已准备，等待验收" in status_view.stdout
    assert _human_acceptance("reviewer-2")["checks"]["e2e"]["evidence"] in status_view.stdout
    assert "触发阻塞的 Agent: 验收 Agent" in status_view.stdout
    assert "模型 gpt-6-astra" in status_view.stdout
    assert "推理强度 low" in status_view.stdout
    assert "本轮时长:" in status_view.stdout
    assert "已保留成果: 当前代码版本已保存" in status_view.stdout
    assert "整项任务已暂停，其他子任务也不会继续。" in status_view.stdout
    assert "下一步: agent-run resume 1 --repo example/project" in status_view.stdout
    for internal_value in (
        run_id,
        str(state["ticket_jobs"]["2"]["candidate_sha"]),
        "reviewer-2",
        "Semantic Agent Attempt",
        "Thread ",
        "binding",
        "digest",
    ):
        assert internal_value not in status_view.stdout
    history_view = invoke_cli_inprocess(git_repo, fixture, "history", run_id)
    assert history_view.returncode == 0
    assert "类型: 需要人工处理" in history_view.stdout
    assert "对象: 子任务 #2" in history_view.stdout
    assert _human_acceptance("reviewer-2")["checks"]["e2e"]["evidence"] in history_view.stdout
    for command in ("status", "history"):
        json_view = stdout_json(
            invoke_cli_inprocess(git_repo, fixture, command, run_id, "--json")
        )
        assert json_view["operator_action"]["type"] == "Human Blocker"
        assert json_view["operator_action"]["object"] == "Ticket #2"
        assert json_view["operator_action"]["phase"] == "candidate"
        assert json_view["operator_action"]["next_action"] == (
            "agent-run resume 1 --repo example/project"
        )
        assert json_view["next_action"] == json_view["operator_action"][
            "next_action"
        ]
        assert json_view["run_id"] == run_id
        if command == "history":
            assert json_view["events"]
            assert json_view["events"] == sorted(
                json_view["events"], key=lambda event: event["at"]
            )
            assert any(
                event["kind"] == "human_blocker"
                and event["details"]
                == [_human_acceptance("reviewer-2")["checks"]["e2e"]["evidence"]]
                for event in json_view["events"]
            )

    repeated = run_cli(
        git_repo,
        fixture,
        "run",
        "1",
        "--agent-fixture",
        str(agent_fixture),
    )

    assert repeated.returncode == 2
    repeated_state = load_only_run_state(git_repo)
    assert repeated_state["status"] == "ready_for_human"
    assert repeated_state["active_ticket_job"]["ticket_number"] == 2
    assert "3" not in repeated_state["ticket_jobs"]

    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-3",
                        "summary": "Implemented independent ticket 3.",
                        "write_files": {"ticket-3.txt": "done\n"},
                    },
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-4",
                        "summary": "Implemented ticket 4 after ticket 2 closed.",
                        "write_files": {"ticket-4.txt": "done\n"},
                    },
                ],
                "publications": [_publication(2), _publication(3), _publication(4)],
                "reviews": [
                    {
                        **passing_acceptance(
                            "reviewer-2", "Ticket 2 passed after the operator resumed it."
                        ),
                        "expected_thread_id": "reviewer-2",
                    },
                    passing_acceptance("reviewer-3", "Ticket 3 passed."),
                    passing_acceptance("reviewer-4", "Ticket 4 passed."),
                ],
                "run_reviews": [
                    passing_acceptance("run-reviewer", "The complete Run passed.")
                ],
                "run_publications": [_publication(1)],
            }
        ),
        encoding="utf-8",
    )

    long_response = "允许在隔离沙箱中写入临时测试数据。" * 20
    resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--message",
        long_response,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_approval_pending"
    assert state["ticket_jobs"]["2"]["phase"] == "completed"
    assert state["ticket_jobs"]["3"]["phase"] == "completed"
    assert state["ticket_jobs"]["4"]["phase"] == "completed"
    history_json = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
    )
    assert any(
        event["kind"] == "resume" and event["details"] == [long_response]
        for event in history_json["events"]
    )
    state["timeline"] = [state["timeline"][0]]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    stale_timeline_history = stdout_json(
        invoke_cli_inprocess(git_repo, fixture, "history", run_id, "--json")
    )
    assert any(
        event["kind"] == "resume" and event["details"] == [long_response]
        for event in stale_timeline_history["events"]
    )
    history_text = invoke_cli_inprocess(git_repo, fixture, "history", run_id).stdout
    assert "…（已截断；完整内容见 --json）" in history_text
    assert long_response not in history_text


def test_deterministic_ticket_conflict_gates_independent_frontier(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
    )
    states = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), states
    )
    state, _ = controller.start(1)
    blocked = state["active_ticket_job"]
    blocked.update(
        {
            "phase": "blocked",
            "blocked_reason": "published_head_mismatch",
            "review_budget": {
                "window": 1,
                "development_attempts": 0,
                "reviewer_invocations": 0,
                "final_ci_fix_used": False,
                "review_artifacts": [],
                "checkpoint_reason": None,
            },
            "review_budget_history": [],
        }
    )
    state.update(
        {
            "status": "blocked",
            "terminal_kind": "waiting_human",
            "diagnostics": [
                {
                    "code": "published_head_mismatch",
                    "message": "Published-Head Gate rejected live PR state",
                    "ticket_number": 2,
                }
            ],
        }
    )
    states.save_run(str(state["run_id"]), state)

    resumed, _ = controller.resume(str(state["run_id"]))

    assert resumed["status"] == "blocked"
    assert resumed["active_ticket_job"]["ticket_number"] == 2
    assert resumed["ticket_jobs"]["2"]["blocked_reason"] == (
        "published_head_mismatch"
    )
    assert "3" not in resumed["ticket_jobs"]


def test_run_recovers_after_process_failure_between_tickets(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2), "3": _ticket(3)},
    )
    first_agents = git_repo / "agents-first.json"
    first_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented ticket 2.",
                        "write_files": {"ticket-2.txt": "done\n"},
                    }
                ],
                "publications": [_publication(2)],
                "reviews": [
                    passing_acceptance("reviewer-2", "Ticket 2 passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", idle_control=True)
    )["run_id"]

    interrupted = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(first_agents),
    )

    assert interrupted.returncode == 2
    failed_state = load_only_run_state(git_repo)
    assert failed_state["status"] == "execution_failed"
    assert failed_state["terminal_kind"] == "execution_failed"
    assert failed_state["ticket_jobs"]["2"]["phase"] == "completed"
    mutable = json.loads(fixture.read_text(encoding="utf-8"))
    assert mutable["delivery"]["closed_issues"] == [2]
    assert len(mutable["delivery"]["pull_requests"]) == 1

    second_agents = git_repo / "agents-second.json"
    second_agents.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-3",
                        "summary": "Implemented ticket 3 after recovery.",
                        "write_files": {"ticket-3.txt": "done\n"},
                    }
                ],
                "publications": [_publication(3)],
                "reviews": [
                    passing_acceptance("reviewer-3", "Ticket 3 passed.")
                ],
                "run_reviews": [
                    passing_acceptance("run-reviewer", "The complete Run passed.")
                ],
                "run_publications": [_publication(1)],
            }
        ),
        encoding="utf-8",
    )
    recovered = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(second_agents),
    )

    assert recovered.returncode == 0, recovered.stdout
    state = load_only_run_state(git_repo)
    assert state["status"] == "run_approval_pending"
    mutable = json.loads(fixture.read_text(encoding="utf-8"))
    assert mutable["delivery"]["closed_issues"] == [2, 3]
    assert len(mutable["delivery"]["pull_requests"]) == 3


def test_inflight_ticket_revision_requires_a_fresh_requeue(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    agents = RevisionChangingAgents(fixture)
    tickets = TicketDeliveryEngine(
        git=git,
        states=states,
        github=FixtureGitHubPublisher(fixture, git),
        agents=agents,
    )

    completed = DeliveryRunEngine(
        controller=controller, tickets=tickets
    ).deliver(str(state["run_id"]))

    assert completed["status"] == "requeue_required"
    job = completed["ticket_jobs"]["2"]
    assert job["ticket_branch_generation"] == 1
    assert job["development_thread_id"] == "developer-2"
    assert len(agents.development_requests) == 1
    assert agents.publication_requests == []
    assert agents.review_requests == []


def test_close_response_loss_recovers_completed_job_without_duplicates(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2)},
        delivery={"crash_after_close_once": True},
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented ticket 2.",
                        "write_files": {"ticket-2.txt": "done\n"},
                    }
                ],
                "publications": [_publication(2)],
                "reviews": [
                    passing_acceptance("reviewer-2", "Ticket 2 passed.")
                ],
                "run_reviews": [
                    passing_acceptance(
                        "run-reviewer", "The complete Run passed."
                    )
                ],
                "run_publications": [_publication(1)],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(
        seed_run(git_repo, fixture, "1", idle_control=True)
    )["run_id"]

    interrupted = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert interrupted.returncode == 2
    interrupted_state = load_only_run_state(git_repo)
    assert interrupted_state["ticket_jobs"]["2"]["phase"] == "merged"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data["issues"]["2"]["state"] == "CLOSED"

    explicitly_resumed = run_cli(
        git_repo,
        fixture,
        "resume",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )
    assert explicitly_resumed.returncode == 0, explicitly_resumed.stderr
    assert stdout_json(explicitly_resumed)["status"] == "run_approval_pending"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert [
        pull_request["number"]
        for pull_request in data["delivery"]["pull_requests"]
    ] == [1, 2]
    assert data["delivery"]["closed_issues"] == [2]
    assert [
        mutation["action"] for mutation in data["delivery"]["mutations"]
    ] == ["completion_comment", "close_issue", "delete_managed_branch"]


@pytest.mark.parametrize(
    "recovery_case",
    [
        "response_loss",
        "response_loss_event_lag",
        "prepared_only",
        "dispatch_pending",
        "external_reopen",
    ],
)
def test_provisional_close_intent_can_abandon(
    git_repo: Path, recovery_case: str
) -> None:
    crash_flag = {
        "response_loss": "crash_after_close_once",
        "response_loss_event_lag": "crash_after_close_once",
        "prepared_only": "crash_before_close_dispatch_once",
        "dispatch_pending": "crash_after_close_dispatch_boundary_once",
        "external_reopen": "crash_after_close_dispatch_boundary_once",
    }[recovery_case]
    fixture = write_fixture(
        git_repo / "github.json",
        issues={"2": _ticket(2)},
        delivery={crash_flag: True},
    )
    agent_fixture = git_repo / "agents.json"
    agent_fixture.write_text(
        json.dumps(
            {
                "developments": [
                    {
                        "expected_thread_id": None,
                        "thread_id": "developer-2",
                        "summary": "Implemented ticket 2.",
                        "write_files": {"ticket-2.txt": "done\n"},
                    }
                ],
                "publications": [_publication(2)],
                "reviews": [
                    passing_acceptance("reviewer-2", "Ticket 2 passed.")
                ],
            }
        ),
        encoding="utf-8",
    )
    run_id = stdout_json(seed_run(git_repo, fixture, "1", idle_control=True))["run_id"]

    interrupted = run_internal_stage(
        git_repo,
        fixture,
        "deliver",
        run_id,
        "--agent-fixture",
        str(agent_fixture),
    )

    assert interrupted.returncode == 2
    interrupted_job = load_only_run_state(git_repo)["ticket_jobs"]["2"]
    assert interrupted_job["phase"] == "merged"
    assert isinstance(interrupted_job["ticket_close_intent"], dict)
    assert "ticket_close_ownership" not in interrupted_job
    interrupted_data = json.loads(fixture.read_text(encoding="utf-8"))
    assert interrupted_data["issues"]["2"]["state"] == (
        "CLOSED"
        if recovery_case in {"response_loss", "response_loss_event_lag"}
        else "OPEN"
    )
    assert (
        interrupted_job["ticket_close_intent"].get("dispatch_attempted") is True
    ) == (recovery_case != "prepared_only")
    if recovery_case == "external_reopen":
        interrupted_data["delivery"]["external_ticket_transitions"] = [2]
        fixture.write_text(json.dumps(interrupted_data), encoding="utf-8")
    elif recovery_case == "response_loss_event_lag":
        interrupted_data["delivery"]["ticket_close_event_lag_reads"] = 2
        fixture.write_text(json.dumps(interrupted_data), encoding="utf-8")

    abandoned = run_cli(git_repo, fixture, "abandon", run_id)
    if recovery_case in {"dispatch_pending", "response_loss_event_lag"}:
        assert abandoned.returncode == 2
        assert stdout_json(abandoned)["status"] == "abandonment_pending"
        pending_data = json.loads(fixture.read_text(encoding="utf-8"))
        assert pending_data["delivery"]["mutations"] == interrupted_data[
            "delivery"
        ]["mutations"]
        abandoned = run_cli(git_repo, fixture, "abandon", run_id)
        if recovery_case == "dispatch_pending":
            assert abandoned.returncode == 2
            assert stdout_json(abandoned)["status"] == "abandonment_pending"
            repeated_data = json.loads(fixture.read_text(encoding="utf-8"))
            assert repeated_data["delivery"]["mutations"] == interrupted_data[
                "delivery"
            ]["mutations"]
            return
        assert abandoned.returncode == 2
        assert stdout_json(abandoned)["status"] == "abandonment_pending"
        if recovery_case == "response_loss_event_lag":
            abandoned = run_cli(git_repo, fixture, "abandon", run_id)

    assert abandoned.returncode == 0, abandoned.stderr
    assert stdout_json(abandoned)["status"] == "abandoned"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data["issues"]["2"]["state"] == "OPEN"
    abandonment_mutations = [
        mutation["action"]
        for mutation in data["delivery"]["mutations"]
        if mutation["action"].startswith("abandonment_")
    ]
    assert abandonment_mutations == (
        ["abandonment_reopen_issue", "abandonment_recovery_comment"]
        if recovery_case in {"response_loss", "response_loss_event_lag"}
        else []
    )
    close_mutations = [
        mutation["action"]
        for mutation in data["delivery"]["mutations"]
        if mutation["action"] == "close_issue"
    ]
    assert close_mutations == (
        ["close_issue"]
        if recovery_case in {"response_loss", "response_loss_event_lag"}
        else []
    )


def test_active_ticket_removal_stops_agents_and_preserves_work(
    git_repo: Path,
) -> None:
    fixture = write_fixture(
        git_repo / "github.json", issues={"2": _ticket(2)}
    )
    states = StateStore(git_repo / ".agent-run")
    git = GitRepository(git_repo)
    controller = Controller(FixtureGitHubReader(fixture), git, states)
    state, _ = controller.start(1)
    agents = TicketRemovingAgents(fixture)

    blocked = DeliveryRunEngine(
        controller=controller,
        tickets=TicketDeliveryEngine(
            git=git,
            states=states,
            github=FixtureGitHubPublisher(fixture, git),
            agents=agents,
        ),
    ).deliver(str(state["run_id"]))

    assert blocked["status"] == "unsupported_scope_change"
    assert blocked["active_ticket_job"] is None
    assert blocked["unsupported_scope_change"]["graph_change_summary"][
        "removed_tickets"
    ] == [2]
    assert agents.publication_calls == 0
    assert agents.review_calls == 0
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data.get("delivery", {}).get("pull_requests", []) == []
    assert data.get("delivery", {}).get("closed_issues", []) == []
    checkout = states.root / "worktrees" / str(state["run_id"]) / "ticket-2"
    assert (checkout / "removed.txt").is_file()
