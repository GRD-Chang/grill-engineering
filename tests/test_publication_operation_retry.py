from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from support.workspace import managed_state

from agent_run.cli_presentation import _publication_operation_retries
from agent_run.development_review import DevelopmentReview
from agent_run.publication_operation_retry import (
    begin_publication_operation_attempt,
    record_publication_operation_failure,
    require_publication_operation_retry,
)
from agent_run.semantic_attempt import allocate_semantic_attempt, close_semantic_attempt
from agent_run.review_budget import TICKET_POLICY, new_budget
from agent_run.state_contract import IncompatibleRunStateError, require_current_run_state
from support.seeding import seed_run, write_fixture
from support.cli import issue, load_only_run_state, run_cli, stdout_json

from support.state import only_run_ref, write_run_fixture


def _corrupt_retry_record(root: Path, run_id: str, job_id: str, retry: dict[str, int]) -> None:
    """Write the exact negative input below business submission validation."""
    from agent_run.sqlite_change_job_codec import presence, present, save_record
    from agent_run.sqlite_change_job_schema import JOB_FIELDS

    with closing(sqlite3.connect(root / "state.sqlite3")) as connection, connection:
        stored, owner = connection.execute(
            "SELECT presence,evidence_owner_id FROM work_jobs WHERE run_id=? AND job_id=?",
            (run_id, job_id),
        ).fetchone()
        connection.execute(
            "UPDATE work_jobs SET presence=? WHERE run_id=? AND job_id=?",
            (presence(present(stored, JOB_FIELDS) | {"publication_operation_retry"}, JOB_FIELDS), run_id, job_id),
        )
        save_record(connection, run_id, job_id, "publication_operation_retry", retry, owner_id=owner)


def _database_snapshot(root: Path) -> tuple[str, ...]:
    # Damaged records deliberately cannot be decoded through the production
    # reader. Audit all physical relations without bypassing its rejection.
    with closing(sqlite3.connect(root / "state.sqlite3")) as connection:
        return tuple(connection.iterdump())


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

    DevelopmentReview._refund_unstarted_credential_invocation(
        engine, {}, owner
    )

    assert owner["publication_attempts"] == 0
    assert "pending_semantic_attempt" not in owner
    assert "publication_operation_retry" not in owner
    assert "last_publication_error" not in owner


def test_fresh_publication_attempt_does_not_inherit_prior_operation_retry(tmp_path: Path) -> None:
    from agent_run.sqlite_change_jobs import projection_archives
    from agent_run.sqlite_state import SQLiteStateRepository

    repository = SQLiteStateRepository(tmp_path)
    owner: dict[str, object] = {"ticket_number": 2, "phase": "accepted", "publication_attempts": 1}
    first = allocate_semantic_attempt(
        owner,
        role="publication",
        work_subject="ticket:2",
        generation=1,
        currentness_boundary={"candidate_sha": "candidate-1"},
        ordinal=1,
    )
    repository.save("retry-renewal", {"ticket_jobs": {"2": owner}})
    current = repository.load_objects("retry-renewal")[0]
    owner = current["ticket_jobs"]["2"]
    first = owner["pending_semantic_attempt"]

    def save_checkpoint() -> None:
        repository.save_objects(
            "retry-renewal", current, fields={"ticket_jobs"},
            keyed_changes={"ticket_jobs": {"2"}}, archive_scope=projection_archives(current),
        )

    record_publication_operation_failure(owner, OSError("context offline"))
    save_checkpoint()
    close_semantic_attempt(owner, first, outcome="publication_artifact")
    save_checkpoint()
    for _ in range(3):
        record_publication_operation_failure(owner, OSError("publish offline"))
        save_checkpoint()

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
    save_checkpoint()
    # Only durable A/B facts are observed; no mutable history carrier or
    # side-channel retry patch is part of this test's contract.
    owner = repository.load("retry-renewal")["ticket_jobs"]["2"]

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
    seed_run(git_repo, fixture, "1")
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
    state["run_publication"] = owner
    reference = only_run_ref(managed_state(git_repo))
    write_run_fixture(reference, state)
    root, run_id = reference
    with closing(sqlite3.connect(root / "state.sqlite3")) as connection:
        job_id = connection.execute(
            "SELECT job_id FROM work_jobs WHERE run_id=? AND owner_kind='run_publication'",
            (run_id,),
        ).fetchone()[0]
    _corrupt_retry_record(root, run_id, job_id, {"attempts": 2, "limit": 5})
    before = _database_snapshot(root)
    from agent_run.state import StateStore

    with pytest.raises(IncompatibleRunStateError, match="conflict"):
        StateStore(root).load_current_run(run_id)
    assert _database_snapshot(root) == before


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
    seed_run(git_repo, fixture, "1")
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
    started = seed_run(git_repo, fixture, "1")
    assert started.returncode == 0, started.stderr
    run_id = stdout_json(started)["run_id"]
    state_ref = only_run_ref(managed_state(git_repo))
    root, _ = state_ref
    with closing(sqlite3.connect(root / "state.sqlite3")) as connection:
        job_id = connection.execute(
            "SELECT active_job_id FROM work_job_layout WHERE run_id=?", (run_id,),
        ).fetchone()[0]
    _corrupt_retry_record(root, run_id, job_id, {"attempts": 1, "limit": 999})
    state_before = _database_snapshot(root)
    fixture_before = fixture.read_bytes()
    head_before = (git_repo / ".git" / "HEAD").read_bytes()

    rejected = run_cli(git_repo, fixture, "run", "1")

    assert rejected.returncode == 2
    assert stdout_json(rejected)["status"] == "incompatible_run_state"
    assert _database_snapshot(root) == state_before
    assert fixture.read_bytes() == fixture_before
    assert (git_repo / ".git" / "HEAD").read_bytes() == head_before
