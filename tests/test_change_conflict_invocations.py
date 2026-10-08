from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_run.agents import DevelopmentResult
from agent_run.state import StateStore
from agent_run.state_contract import IncompatibleRunStateError
from support.conflict_invocations import prepared_conflict, run_conflict


# Ticket CI now finishes Development–Review before integration. Its live
# integration/conflict boundary is accepted, never an unreviewed candidate;
# candidate entry remains applicable to the unchanged Parent paths.
@pytest.mark.parametrize(("kind", "phase"), [
    ("ticket", "accepted"), ("parent_only", "accepted"),
    ("parent_only", "candidate"), ("run_repair", "accepted"), ("run_repair", "candidate"),
])
def test_conflict_events_round_trip_before_and_after_business_closeout(git_repo: Path, kind: str, phase: str) -> None:
    state, store, job, stage = prepared_conflict(git_repo, kind, phase)
    budget = deepcopy(job["review_budget"])
    seen = []

    def develop(request: dict[str, Any]) -> DevelopmentResult:
        event = request["_invocation_event"]
        for kind, facts in [
            ("started", {"requested_thread_id": "conflict-thread", "attempt_count": 1}),
            ("thread_started", {"reported_thread_id": "conflict-thread"}),
            ("failed", {"error": "controlled interruption"}),
            ("recovery_waiting", {}),
            ("recovery_started", {}),
            ("completed", {}),
        ]:
            event(kind, **facts)
            if kind in {"failed", "recovery_waiting"}:
                state.update(status="blocked", terminal_kind=None, diagnostics=[])
                job.update(phase="blocked", blocked_reason="ticket_execution_failed", execution_resume_phase=phase)
                if stage.kind == "ticket":
                    state["ticket_integration_holder"] = None
                stage.save(state)
            elif kind == "recovery_started":
                state.update(status="active" if stage.kind == "ticket" else "parent_delivery_pending", terminal_kind=None)
                job["phase"] = phase
                if stage.kind == "ticket":
                    state["ticket_integration_holder"] = 3
                job.pop("blocked_reason", None)
                job.pop("execution_resume_phase", None)
                stage.save(state)
            loaded = StateStore(store.root).load_current_run(state["run_id"])
            assert loaded is not None
            seen.append(loaded["active_agent_invocation"]["status"])
            assert loaded["active_agent_invocation"]["phase"] == "integration_conflict"
            assert job["pending_semantic_attempt"] == loaded["active_agent_invocation"]["semantic_attempt"]
        return DevelopmentResult(thread_id="conflict-thread", summary="Resolved conflict")

    stage.agents = SimpleNamespace(develop=develop)
    assert run_conflict(stage, state, job, git_repo)
    assert seen == ["running", "running", "failed", "resuming", "running", "completed"]
    assert "pending_semantic_attempt" not in job
    assert job["review_budget"] == budget
    job.pop(stage.conflict_key, None)
    stage.save(state)
    loaded = StateStore(store.root).load_current_run(state["run_id"])
    assert loaded is not None
    assert loaded["agent_invocation_history"][-1]["phase"] == "integration_conflict"


@pytest.mark.parametrize("kind", ["ticket", "parent_only", "run_repair"])
@pytest.mark.parametrize("damage", ["context", "candidate", "stage", "paths", "owner_phase", "pending", "generation", "budget", "role", "pending_mismatch", "merge_head", "boundary", "ordinary_phase", "subject"])
def test_active_conflict_rejects_inconsistent_owner_and_context(git_repo: Path, kind: str, damage: str) -> None:
    state, store, job, stage = prepared_conflict(git_repo, kind)

    def develop(request: dict[str, Any]) -> DevelopmentResult:
        request["_invocation_event"]("started", requested_thread_id="conflict-thread", attempt_count=1)
        if damage == "context":
            job.pop(stage.conflict_key)
        elif damage in {"candidate", "stage", "paths"}:
            job[stage.conflict_key][{"candidate": "candidate_sha", "stage": "stage", "paths": "paths"}[damage]] = "wrong"
        elif damage == "owner_phase":
            job["phase"] = "developing"
            if kind == "ticket":
                state["ticket_integration_holder"] = None
        elif damage == "pending":
            job.pop("pending_semantic_attempt")
        elif damage == "pending_mismatch":
            job["pending_semantic_attempt"] = {**job["pending_semantic_attempt"], "attempt_id": "sha256:" + "0" * 64}
        elif damage == "merge_head":
            job[stage.conflict_key]["merge_head_sha"] = "other"
        elif damage == "boundary":
            job["base_sha"] = "other"
        elif damage == "subject":
            for invocation in [state["active_agent_invocation"], *state["agent_invocation_history"]]:
                invocation["work_subject"] = "ticket:999"
        elif damage == "ordinary_phase":
            for invocation in [state["active_agent_invocation"], *state["agent_invocation_history"]]:
                invocation["phase"] = "developing"
        elif damage == "generation":
            job[{"ticket": "ticket_branch_generation", "parent_only": "parent_generation", "run_repair": "repair_generation"}[kind]] = 2
        elif damage == "budget":
            job["integration_development_budget" if kind in {"ticket", "parent_only"} else "review_budget"]["window"] = 2
        else:
            state["active_agent_invocation"]["role"] = "publication"
        stage.save(state)
        with pytest.raises(IncompatibleRunStateError) as caught:
            StateStore(store.root).load_current_run(state["run_id"])
        if damage != "budget":
            assert "legacy" not in str(caught.value)
        raise InterruptedError("stop before business closeout")

    stage.agents = SimpleNamespace(develop=develop)
    with pytest.raises(InterruptedError):
        run_conflict(stage, state, job, git_repo)


@pytest.mark.parametrize("kind", ["ticket", "parent_only", "run_repair"])
def test_candidate_conflict_requires_its_production_entry_conditions(git_repo: Path, kind: str) -> None:
    state, store, job, stage = prepared_conflict(git_repo, kind, "candidate")

    def develop(request: dict[str, Any]) -> DevelopmentResult:
        request["_invocation_event"]("started", requested_thread_id="conflict-thread", attempt_count=1)
        if kind == "ticket":
            job.pop("attempt_kind")
            state["ticket_integration_holder"] = None
        elif kind == "parent_only":
            job.pop("parent_integration")
        else:
            job["integrated_sha"] = job["candidate_sha"]
        stage.save(state)
        with pytest.raises(IncompatibleRunStateError, match="owner, generation, phase or conflict context"):
            StateStore(store.root).load_current_run(state["run_id"])
        raise InterruptedError("invalid candidate")

    stage.agents = SimpleNamespace(develop=develop)
    with pytest.raises(InterruptedError):
        run_conflict(stage, state, job, git_repo)
