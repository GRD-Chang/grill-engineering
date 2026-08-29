from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_run.change_delivery import ChangeDeliveryEngine
from agent_run.controller import (
    Controller,
    _change_job_for_invocation,
    _resume_agent_human_blocker,
)
from agent_run.parent_delivery import ParentDeliveryEngine
from agent_run.human_responses import current_human_response_history
from agent_run.cli_surface import _resume_is_ready
from agent_run.human_responses import append_human_response
from agent_run.git import GitRepository
from agent_run.github_fixture import FixtureGitHubReader
from agent_run.revisions import effective_revision
from agent_run.state import StateStore
from agent_run.state_contract import IncompatibleRunStateError
from agent_run.semantic_attempt import allocate_semantic_attempt
from conftest import write_fixture
from run_acceptance_test_support import _passing_artifact


def _canonical_budget() -> dict[str, Any]:
    return {
        "window": 1,
        "development_attempts": 0,
        "reviewer_invocations": 0,
        "final_ci_fix_used": False,
        "review_artifacts": [],
        "checkpoint_reason": None,
    }


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
        "review_budget": _canonical_budget(),
        "review_budget_history": [],
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
        ticket = state["ticket_graph"]["tickets"]["3"]
        current_revision = effective_revision(
            ticket_revision=str(ticket["content_revision"]),
            parent_revision=str(state["parent"]["revision"]),
            graph_revision=str(state["ticket_graph"]["revision"]),
        )
        job.update(
            {
                "ticket_number": 3,
                "ticket_branch_generation": 2,
                "phase": "accepted",
                "base_sha": GitRepository(git_repo).resolve(
                    str(state["run_branch"])
                ),
                "candidate_sha": "candidate-sha",
                "effective_revision": current_revision,
                "acceptance_artifact": _passing_artifact(),
                "acceptance_record": {
                    "acceptance_scope": "change_job",
                    "reviewed_base_sha": GitRepository(git_repo).resolve(
                        str(state["run_branch"])
                    ),
                    "reviewed_candidate_sha": "candidate-sha",
                    "reviewed_candidate_tree": "candidate-tree",
                    "effective_revision": current_revision,
                    "reviewer_thread_id": "reviewer-thread",
                    "artifact": _passing_artifact(),
                },
                "publication_thread_id": "current-ticket-thread",
                "review_budget": _canonical_budget(),
                "review_budget_history": [],
            }
        )
        acceptance = job["acceptance_record"]
        job["review_budget"]["reviewer_invocations"] = 1
        job["review_budget"]["review_artifacts"] = [
            {
                "reviewer_thread_id": acceptance["reviewer_thread_id"],
                "candidate_sha": acceptance["reviewed_candidate_sha"],
                "reviewed_base_sha": acceptance["reviewed_base_sha"],
                "review_identity": {
                    "reviewed_base_sha": acceptance["reviewed_base_sha"],
                    "reviewed_candidate_sha": acceptance["reviewed_candidate_sha"],
                    "reviewed_candidate_tree": acceptance["reviewed_candidate_tree"],
                },
                "artifact": acceptance["artifact"],
            }
        ]
        active_ticket = state.get("active_ticket_job")
        if isinstance(active_ticket, dict):
            active_ticket.update(job)
        role = "publication"
        work_subject = "ticket:3"
        attempt_owner = job
    elif subject_kind == "run_repair":
        state["run_acceptance"] = {
            "phase": "repairing",
            "policy_snapshot": dict(state["policy_snapshot"]),
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "repair_job": {
                "phase": "accepted",
                "policy_snapshot": dict(state["policy_snapshot"]),
                "review_budget": _canonical_budget(),
                "review_budget_history": [],
                "repair_generation": 2,
                "repair_mode": "squash",
                "publication_thread_id": "current-repair-thread",
            }
        }
        role = "publication"
        work_subject = f"run-repair:{run_id}"
        attempt_owner = state["run_acceptance"]["repair_job"]
    elif subject_kind == "final_publication":
        state["run_acceptance"] = {
            "phase": "accepted",
            "policy_snapshot": dict(state["policy_snapshot"]),
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "validation_attempts": 2,
            "acceptance_generation": 2,
        }
        state["run_publication"] = {
            "phase": "publishing",
            "thread_id": "current-final-thread",
        }
        role = "final_publication"
        work_subject = f"run-publication:{run_id}"
        attempt_owner = state["run_publication"]
    elif subject_kind == "parent_only":
        state["parent_job"] = {
            "phase": "accepted",
            "publication_thread_id": "current-parent-thread",
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
        }
        role = "publication"
        work_subject = f"parent-only:{run_id}"
        attempt_owner = state["parent_job"]
    else:
        raise AssertionError(f"unsupported test subject: {subject_kind}")

    invocation: dict[str, Any] = {
        "role": role,
        "work_subject": work_subject,
        "phase": "run_publication" if role == "final_publication" else "publication",
        "mode": "fresh",
        "input_fingerprint": "fixture",
        "currentness_boundary": {},
        "status": "failed",
        "requested_thread_id": "failed-thread",
        "reported_thread_id": "failed-thread",
        "attempt_count": 1,
        "started_at": "2026-08-13T00:00:00+00:00",
        "ended_at": "2026-08-13T00:00:01+00:00",
        "error": "fixture failure",
        "return_code": 1,
        "signal": None,
    }
    if invocation_generation is not _MISSING:
        invocation["generation"] = invocation_generation
    if type(invocation_generation) is int:
        semantic_attempt = allocate_semantic_attempt(
            attempt_owner,
            role="publication",
            work_subject=work_subject,
            generation=invocation_generation,
            currentness_boundary={},
            ordinal=1,
        )
        invocation["semantic_attempt"] = dict(semantic_attempt)
    state["active_agent_invocation"] = invocation
    state["agent_invocation_history"] = []
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

    with pytest.raises(IncompatibleRunStateError, match="stale active|owner mismatch"):
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


def test_resume_reopens_a_development_invocation_after_credential_failure(
    git_repo: Path,
) -> None:
    fixture = write_fixture(git_repo / "github.json", issues={"3": _ticket(3)})
    store = StateStore(git_repo / ".agent-run")
    controller = Controller(
        FixtureGitHubReader(fixture), GitRepository(git_repo), store
    )
    state, _ = controller.start(1)
    run_id = str(state["run_id"])
    job = state["ticket_jobs"]["3"]
    job.update(
        {
            "phase": "developing",
            "ticket_branch_generation": 1,
            "development_thread_id": "development-thread",
            "review_budget": _canonical_budget(),
            "review_budget_history": [],
            "pending_attempt": 1,
        }
    )
    job["review_budget"]["development_attempts"] = 1
    semantic_attempt = allocate_semantic_attempt(
        job,
        role="development",
        work_subject="ticket:3",
        generation=1,
        currentness_boundary={},
        ordinal=1,
        budget_window=1,
    )
    state.update(
        {
            "status": "execution_failed",
            "terminal_kind": "execution_failed",
            "diagnostics": [
                {
                    "code": "worker_credential_renewal_failed",
                    "message": "worker credential renewal failed",
                }
            ],
            "active_agent_invocation": {
                "role": "development",
                "work_subject": "ticket:3",
                "generation": 1,
                "phase": "developing",
                "mode": "fresh",
                "input_fingerprint": "sha256:" + "0" * 64,
                "currentness_boundary": {},
                "semantic_attempt": dict(semantic_attempt),
                "status": "failed",
                "requested_thread_id": "development-thread",
                "reported_thread_id": "development-thread",
                "attempt_count": 1,
                "started_at": "2026-08-17T00:00:00+00:00",
                "ended_at": "2026-08-17T00:01:00+00:00",
                "error": "worker credential renewal failed",
                "return_code": 1,
                "signal": None,
            },
        }
    )
    store.save_run(run_id, state)

    resumed, reused = controller.resume(run_id)

    assert reused
    assert resumed["status"] == "active"
    assert resumed["terminal_kind"] is None
    assert resumed["diagnostics"] == []
    assert resumed["active_agent_invocation"]["status"] == "resuming"
    assert resumed["ticket_jobs"]["3"]["development_failure_resume"] is True


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

    with pytest.raises(IncompatibleRunStateError, match="generation"):
        controller.resume(run_id, new_thread=new_thread)

    assert save_calls == []
    assert store.load_run(run_id) == before
