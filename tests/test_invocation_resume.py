from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_run.change_delivery import ChangeDeliveryEngine
from agent_run.controller import Controller, _change_job_for_invocation, _resume_agent_human_blocker
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.human_responses import current_human_response_history
from agent_run.cli_surface import _resume_is_ready
from agent_run.human_responses import append_human_response
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from conftest import write_fixture


def test_human_response_history_keeps_ordered_immutable_entries() -> None:
    subject: dict[str, Any] = {}
    for attempt in range(19):
        append_human_response(
            subject,
            [f"blocker-{attempt}"],
            f"response-{attempt}",
            generation=1,
        )

    history = subject["human_response_history"]
    assert len(history) == 19
    assert history[0] == {
        "generation": 1,
        "human_blockers": ["blocker-0"],
        "response": "response-0",
    }
    assert history[-1] == {
        "generation": 1,
        "human_blockers": ["blocker-18"],
        "response": "response-18",
    }


def test_old_or_mixed_generation_response_history_is_not_reusable() -> None:
    subject: dict[str, Any] = {
        "human_response_generation": 1,
        "human_response_history": [{"generation": 1, "response": "old"}],
    }
    assert current_human_response_history(subject, generation=2) is None

    subject["human_response_generation"] = 2
    with pytest.raises(ValueError, match="mixed Job Generations"):
        current_human_response_history(subject, generation=2)


def test_resume_fails_closed_when_multiple_human_blockers_are_current() -> None:
    blocker = {
        "phase": "blocked",
        "blocked_reason": "agent_requires_human",
        "human_blockers": ["Need maintainer input."],
    }
    state: dict[str, Any] = {"ticket_jobs": {"2": blocker, "3": dict(blocker)}}

    assert _resume_is_ready(state) is False


def test_run_acceptance_keeps_multiple_human_responses_in_one_generation() -> None:
    state: dict[str, Any] = {
        "run_acceptance": {
            "phase": "ready_for_human",
            "blocked_reason": "reviewer_requires_human",
            "human_blocker_phase": "pending",
            "human_blockers": ["Need access."],
        }
    }

    assert _resume_agent_human_blocker(state, "Access granted.")
    acceptance = state["run_acceptance"]
    acceptance.update(
        {
            "phase": "ready_for_human",
            "blocked_reason": "reviewer_requires_human",
            "human_blockers": ["Need approval."],
        }
    )

    assert _resume_agent_human_blocker(state, "Approval granted.")
    assert [entry["response"] for entry in acceptance["human_response_history"]] == [
        "Access granted.",
        "Approval granted.",
    ]


def test_parent_revision_reset_starts_a_new_human_response_generation() -> None:
    state: dict[str, Any] = {"base": {"sha": "base"}}
    job: dict[str, Any] = {
        "parent_generation": 1,
        "human_response_generation": 1,
        "human_response_history": [{"generation": 1, "response": "old"}],
        "prior_human_blockers": ["old blocker"],
    }

    ParentDeliveryEngine._reset_for_revision(state, job, "new-revision")

    assert job["parent_generation"] == 2
    assert "human_response_history" not in job
    assert "prior_human_blockers" not in job


def test_parent_revision_reset_binds_invocations_to_the_new_generation() -> None:
    state: dict[str, Any] = {"run_id": "run-1"}
    job: dict[str, Any] = {"parent_generation": 2}

    assert ChangeDeliveryEngine._invocation_identity(state, job) == (
        "parent-only:run-1",
        2,
    )
    state["parent_job"] = job
    assert _change_job_for_invocation(
        state,
        {"work_subject": "parent-only:run-1", "generation": 2},
    ) is job
    with pytest.raises(ValueError, match="generation is stale"):
        _change_job_for_invocation(
            state,
            {"work_subject": "parent-only:run-1", "generation": 1},
        )


def _ticket(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Ticket {number}",
        "body": "Deliver the Ticket.",
        "state": "OPEN",
        "labels": ["ready-for-agent"],
        "blocked_by": [],
    }


def _prepared_resume(
    git_repo: Path,
    *,
    subject_kind: str,
    invocation_generation: object,
) -> tuple[Controller, StateStore, str]:
    issues = {"3": _ticket(3)} if subject_kind == "ticket" else {}
    fixture = write_fixture(git_repo / "github.json", issues=issues)
    store = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), store
    )
    state, _ = controller.start(1)
    run_id = str(state["run_id"])

    if subject_kind == "ticket":
        job = state["ticket_jobs"]["3"]
        job.update(
            {
                "ticket_number": 3,
                "ticket_branch_generation": 2,
                "publication_thread_id": "current-ticket-thread",
            }
        )
        role = "publication"
        work_subject = "ticket:3"
    elif subject_kind == "run_repair":
        state["run_acceptance"] = {
            "repair_job": {
                "repair_generation": 2,
                "publication_thread_id": "current-repair-thread",
            }
        }
        role = "publication"
        work_subject = f"run-repair:{run_id}"
    elif subject_kind == "final_publication":
        state["run_acceptance"] = {
            "validation_attempts": 2,
            "acceptance_generation": 2,
        }
        state["run_publication"] = {"thread_id": "current-final-thread"}
        role = "final_publication"
        work_subject = f"run-publication:{run_id}"
    elif subject_kind == "parent_only":
        state["parent_job"] = {
            "publication_thread_id": "current-parent-thread"
        }
        role = "publication"
        work_subject = f"parent-only:{run_id}"
    else:
        raise AssertionError(f"unsupported test subject: {subject_kind}")

    invocation: dict[str, Any] = {
        "role": role,
        "work_subject": work_subject,
        "status": "failed",
        "requested_thread_id": "failed-thread",
        "reported_thread_id": "failed-thread",
    }
    if invocation_generation is not _MISSING:
        invocation["generation"] = invocation_generation
    state["active_agent_invocation"] = invocation
    state["agent_invocation_history"] = [{"marker": "before-resume"}]
    state["status"] = "execution_failed"
    state["terminal_kind"] = "execution_failed"
    store.save_run(run_id, state)
    return controller, store, run_id


@pytest.mark.parametrize(
    "subject_kind", ["ticket", "run_repair", "final_publication"]
)
@pytest.mark.parametrize("new_thread", [False, True])
def test_resume_fails_closed_for_stale_publication_generation(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    subject_kind: str,
    new_thread: bool,
) -> None:
    controller, store, run_id = _prepared_resume(
        git_repo, subject_kind=subject_kind, invocation_generation=1
    )
    before = store.load_run(run_id)
    assert before is not None
    save_calls: list[str] = []
    monkeypatch.setattr(
        store,
        "save_run",
        lambda saved_run_id, _state: save_calls.append(saved_run_id),
    )

    with pytest.raises(ValueError, match="generation is stale"):
        controller.resume(run_id, new_thread=new_thread)

    assert save_calls == []
    assert store.load_run(run_id) == before


@pytest.mark.parametrize("new_thread", [False, True])
def test_parent_only_resume_accepts_generation_one(
    git_repo: Path,
    new_thread: bool,
) -> None:
    controller, store, run_id = _prepared_resume(
        git_repo, subject_kind="parent_only", invocation_generation=1
    )

    resumed, reused = controller.resume(run_id, new_thread=new_thread)

    assert reused
    parent = resumed["parent_job"]
    if new_thread:
        assert "publication_thread_id" not in parent
        assert parent["publication_new_thread"] is True
    else:
        assert parent["publication_thread_id"] == "failed-thread"
        assert "publication_new_thread" not in parent
    assert store.load_run(run_id) == resumed


@pytest.mark.parametrize(
    ("subject_kind", "invocation_generation"),
    [
        ("ticket", 2),
        ("run_repair", 2),
        ("final_publication", 2),
        ("parent_only", 1),
    ],
)
@pytest.mark.parametrize("new_thread", [False, True])
def test_publication_failure_resume_marks_the_successor_mode(
    git_repo: Path,
    subject_kind: str,
    invocation_generation: int,
    new_thread: bool,
) -> None:
    controller, _store, run_id = _prepared_resume(
        git_repo,
        subject_kind=subject_kind,
        invocation_generation=invocation_generation,
    )

    resumed, _ = controller.resume(run_id, new_thread=new_thread)
    if subject_kind == "final_publication":
        publication = resumed["run_publication"]
    elif subject_kind == "run_repair":
        publication = resumed["run_acceptance"]["repair_job"]
    elif subject_kind == "ticket":
        publication = resumed["ticket_jobs"]["3"]
    else:
        publication = resumed["parent_job"]
    if new_thread:
        assert "publication_failure_resume" not in publication
    else:
        assert publication["publication_failure_resume"] is True


_MISSING = object()


@pytest.mark.parametrize("invocation_generation", [_MISSING, True, "1"])
@pytest.mark.parametrize("new_thread", [False, True])
def test_parent_only_resume_rejects_invalid_generation_without_mutation(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    invocation_generation: object,
    new_thread: bool,
) -> None:
    controller, store, run_id = _prepared_resume(
        git_repo,
        subject_kind="parent_only",
        invocation_generation=invocation_generation,
    )
    before = store.load_run(run_id)
    assert before is not None
    save_calls: list[str] = []
    monkeypatch.setattr(
        store,
        "save_run",
        lambda saved_run_id, _state: save_calls.append(saved_run_id),
    )

    with pytest.raises(ValueError, match="generation is invalid"):
        controller.resume(run_id, new_thread=new_thread)

    assert save_calls == []
    assert store.load_run(run_id) == before
