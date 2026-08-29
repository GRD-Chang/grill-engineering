from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.cli_presentation import _publication_operation_retries
from agent_run.change_delivery import ChangeDeliveryEngine
from agent_run.publication_operation_retry import (
    begin_publication_operation_attempt,
    record_publication_operation_failure,
    require_publication_operation_retry,
)
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt
from agent_run.review_budget import TICKET_POLICY, new_budget
from agent_run.state_contract import IncompatibleRunStateError, require_current_run_state
from conftest import write_fixture
from test_cli import issue, load_only_run_state, run_cli, stdout_json


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"attempts": True, "limit": 5},
        {"attempts": 0, "limit": 5},
        {"attempts": -1, "limit": 5},
        {"attempts": 6, "limit": 5},
        {"attempts": 1, "limit": 4},
        {"attempts": 1, "limit": 6},
        {"attempts": 1, "limit": 5, "extra": 1},
    ],
)
def test_publication_operation_retry_rejects_noncanonical_values(
    value: object,
) -> None:
    with pytest.raises(ValueError):
        require_publication_operation_retry(value)


@pytest.mark.parametrize("attempts", [1, 5])
def test_publication_operation_retry_accepts_canonical_values(attempts: int) -> None:
    assert require_publication_operation_retry(
        {"attempts": attempts, "limit": 5}
    ) == {"attempts": attempts, "limit": 5}


def test_publication_operation_retry_advances_to_one_fixed_exhaustion_boundary() -> None:
    owner: dict[str, object] = {}

    for attempt in range(1, 6):
        exhausted = record_publication_operation_failure(owner, OSError("offline"))
        assert owner["publication_operation_retry"] == {
            "attempts": attempt,
            "limit": 5,
        }
        assert exhausted is (attempt == 5)

    before = deepcopy(owner)
    with pytest.raises(ValueError, match="already exhausted"):
        record_publication_operation_failure(owner, OSError("still offline"))
    assert owner == before


def test_closed_publication_attempt_archives_its_operation_retry() -> None:
    owner: dict[str, object] = {}
    attempt = allocate_semantic_attempt(
        owner,
        role="publication",
        work_subject="ticket:2",
        generation=1,
        currentness_boundary={"candidate_sha": "candidate-1"},
        ordinal=1,
    )
    owner["publication_operation_retry"] = {"attempts": 4, "limit": 5}

    close_semantic_attempt(owner, attempt, outcome="currentness_invalidated")
    owner.pop("publication_operation_retry")

    assert owner["semantic_attempt_history"][-1][
        "publication_operation_retry"
    ] == {"attempts": 4, "limit": 5}


def test_unstarted_publication_credential_wait_releases_its_retry_owner() -> None:
    owner: dict[str, object] = {
        "phase": "accepted",
        "publication_attempts": 1,
        "review_budget": new_budget(),
        "review_budget_history": [],
    }
    allocate_semantic_attempt(
        owner,
        role="publication",
        work_subject="ticket:2",
        generation=1,
        currentness_boundary={"candidate_sha": "candidate-1"},
        ordinal=1,
    )
    owner["publication_operation_retry"] = {"attempts": 1, "limit": 5}
    owner["last_publication_error"] = "context offline"
    engine = SimpleNamespace(review_budget_policy=lambda: TICKET_POLICY)

    ChangeDeliveryEngine._refund_unstarted_credential_invocation(
        engine, {}, owner
    )

    assert owner["publication_attempts"] == 0
    assert "pending_semantic_attempt" not in owner
    assert "publication_operation_retry" not in owner
    assert "last_publication_error" not in owner


def test_fresh_publication_attempt_does_not_inherit_prior_operation_retry() -> None:
    owner: dict[str, object] = {"publication_attempts": 1}
    first = allocate_semantic_attempt(
        owner,
        role="publication",
        work_subject="ticket:2",
        generation=1,
        currentness_boundary={"candidate_sha": "candidate-1"},
        ordinal=1,
    )
    record_publication_operation_failure(owner, OSError("context offline"))
    close_semantic_attempt(owner, first, outcome="publication_artifact")
    for _ in range(3):
        record_publication_operation_failure(owner, OSError("publish offline"))

    begin_publication_operation_attempt(owner)
    owner["publication_attempts"] = 2
    second = allocate_semantic_attempt(
        owner,
        role="publication",
        work_subject="ticket:2",
        generation=1,
        currentness_boundary={"candidate_sha": "candidate-2"},
        ordinal=2,
    )
    record_publication_operation_failure(owner, OSError("fresh offline"))

    assert first["attempt_id"] != second["attempt_id"]
    assert owner["publication_operation_retry"] == {"attempts": 1, "limit": 5}
    assert owner["semantic_attempt_history"][0][
        "publication_operation_retry"
    ] == {"attempts": 4, "limit": 5}
    assert _publication_operation_retries({"active_ticket_job": owner}) == [
        {
            "semantic_attempt_id": second["attempt_id"],
            "work_subject": "ticket:2",
            "attempts": 1,
            "limit": 5,
        },
        {
            "semantic_attempt_id": first["attempt_id"],
            "work_subject": "ticket:2",
            "attempts": 4,
            "limit": 5,
        },
    ]


def test_state_contract_rejects_conflicting_live_and_archived_retry_authority(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    owner: dict[str, object] = {"phase": "pending", "publication_attempts": 1}
    attempt = allocate_semantic_attempt(
        owner,
        role="publication",
        work_subject=f"run-publication:{state['run_id']}",
        generation=1,
        currentness_boundary={"candidate_sha": "candidate-1"},
        ordinal=1,
    )
    owner["publication_operation_retry"] = {"attempts": 1, "limit": 5}
    close_semantic_attempt(owner, attempt, outcome="publication_artifact")
    owner["publication_operation_retry"] = {"attempts": 2, "limit": 5}
    state["run_publication"] = owner

    with pytest.raises(IncompatibleRunStateError, match="conflicting"):
        require_current_run_state(state)


@pytest.mark.parametrize(
    ("owner", "message"),
    [
        (
            {
                "phase": "publication_pending",
            },
            "without its Operation Retry",
        ),
        (
            {
                "phase": "pending",
                "publication_operation_retry": {"attempts": 5, "limit": 5},
            },
            "outside a terminal boundary",
        ),
        (
            {
                "phase": "publication_pending",
                "publication_operation_retry": {"attempts": 4, "limit": 5},
            },
            "non-exhausted",
        ),
    ],
)
def test_state_contract_binds_publication_pending_to_exhaustion(
    git_repo: Path, owner: dict[str, object], message: str
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    run_cli(git_repo, fixture, "start", "1")
    state = load_only_run_state(git_repo)
    state["active_ticket_job"] = None
    if owner.get("publication_operation_retry") is not None:
        owner["publication_attempts"] = 1
        allocate_semantic_attempt(
            owner,
            role="publication",
            work_subject=f"run-publication:{state['run_id']}",
            generation=1,
            currentness_boundary={"candidate_sha": "candidate-1"},
            ordinal=1,
        )
    state["run_publication"] = owner

    with pytest.raises(IncompatibleRunStateError, match=message):
        require_current_run_state(state)


def test_public_command_rejects_corrupt_publication_retry_before_mutation(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"2": issue(2)})
    started = run_cli(git_repo, fixture, "start", "1")
    assert started.returncode == 0, started.stderr
    run_id = stdout_json(started)["run_id"]
    state_path = next((git_repo / ".agent-run" / "runs").glob("*.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["active_ticket_job"]["publication_operation_retry"] = {
        "attempts": 1,
        "limit": 999,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    state_before = state_path.read_bytes()
    fixture_before = fixture.read_bytes()
    head_before = (git_repo / ".git" / "HEAD").read_bytes()

    rejected = run_cli(git_repo, fixture, "run", "1")

    assert rejected.returncode == 2
    assert stdout_json(rejected)["status"] == "incompatible_run_state"
    assert state_path.read_bytes() == state_before
    assert fixture.read_bytes() == fixture_before
    assert (git_repo / ".git" / "HEAD").read_bytes() == head_before
