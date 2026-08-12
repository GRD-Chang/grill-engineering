from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_run.controller import Controller, _append_human_response
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.state import StateStore
from conftest import write_fixture


def test_human_response_history_keeps_ordered_immutable_entries() -> None:
    subject: dict[str, Any] = {}
    for attempt in range(19):
        _append_human_response(
            subject,
            [f"blocker-{attempt}"],
            f"response-{attempt}",
        )

    history = subject["human_response_history"]
    assert len(history) == 19
    assert history[0] == {
        "human_blockers": ["blocker-0"],
        "response": "response-0",
    }
    assert history[-1] == {
        "human_blockers": ["blocker-18"],
        "response": "response-18",
    }


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
        state["run_acceptance"] = {"validation_attempts": 2}
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
