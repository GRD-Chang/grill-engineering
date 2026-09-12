from __future__ import annotations

from copy import deepcopy
from typing import Any
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_run.agent_invocation import invocation_event_recorder
from agent_run.change_delivery_threads import require_development_thread
from agent_run.change_delivery_development import develop
from agent_run.agents import DevelopmentResult
from agent_run.review_budget import TICKET_POLICY, new_budget, mark_development, reset_budget
from agent_run.delivery_policy import DeliveryPolicy
from agent_run.semantic_attempt import allocate_semantic_attempt


@pytest.mark.parametrize("source", ["acceptance", "required_checks", "git_integrity", "human_revision", "merge_conflict", "final_ci_fix", "new_budget_window"])
def test_shared_development_allocates_once_and_preserves_pending_work(source: str, tmp_path: Path) -> None:
    state: dict[str, Any] = {"policy_snapshot": DeliveryPolicy(development_thread_policy="new-per-attempt").snapshot()}
    used = 4 if source == "final_ci_fix" else 1
    repair_source = {"final_ci_fix": "required_checks", "new_budget_window": "acceptance"}.get(source, source)
    job: dict[str, Any] = {
        "development_thread_id": "old", "development_thread_history": [], "reviewer_thread_ids": [],
        "repair_source": repair_source, "candidate_sha": "candidate", "pr_number": 12,
        "modification_attempts": used, "validation_attempts": 0, "phase": "repairing", "review_budget": new_budget(development_attempts=used),
        "review_budget_history": [], "checkout": str(tmp_path),
    }
    if source == "final_ci_fix":
        job["next_attempt_kind"] = "final_ci_fix"
    if source == "new_budget_window":
        reset_budget(job, TICKET_POLICY)
    (tmp_path / "uncommitted.txt").write_text("existing work")
    saved: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []

    def worker(request: dict[str, Any]) -> DevelopmentResult:
        requests.append(request)
        assert saved[-1]["pending_semantic_attempt"]["status"] == "pending"
        assert (tmp_path / "uncommitted.txt").read_text() == "existing work"
        return DevelopmentResult(thread_id="new", summary="Work retained")

    stage = SimpleNamespace(
        git=SimpleNamespace(checkout_head=lambda checkout: "current-head"),
        adapter=SimpleNamespace(
            invocation_identity=lambda state, job: ("ticket:1", 1),
            development_request=lambda state, job, checkout: {
                "thread_id": job.get("development_thread_id"), "repair_source": job["repair_source"],
                "checkout": str(checkout),
            },
            development_thread_is_allowed=lambda state, thread: True,
        ),
        agents=SimpleNamespace(develop=worker), contract=SimpleNamespace(label="ticket:1"),
        mark_development_attempt=lambda job, attempt_kind: mark_development(job, TICKET_POLICY, attempt_kind=attempt_kind),
        review_budget_policy=lambda: TICKET_POLICY,
        _reject_stale=lambda *args: None, _invocation_boundary=lambda job: {"base_sha": "base"},
        _invocation_events=lambda *args, **kwargs: lambda *args, **kwargs: None,
        _agent_is_current=lambda *args: True, save=lambda state: saved.append(deepcopy(job)),
    )
    develop(stage, state, job, tmp_path)
    first_attempt = deepcopy(job["pending_semantic_attempt"])
    first_budget = deepcopy(job["review_budget"])
    assert requests[0]["thread_id"] is None
    assert requests[0]["repair_source"] == repair_source
    assert job["development_thread_history"] == ["old"]
    assert first_attempt["ordinal"] == (1 if source == "new_budget_window" else used + 1)
    assert first_attempt["budget_window"] == (2 if source == "new_budget_window" else 1)
    assert first_budget["development_attempts"] == (4 if source == "final_ci_fix" else 1 if source == "new_budget_window" else 2)
    assert first_budget["final_ci_fix_used"] is (source == "final_ci_fix")
    # Simulate re-entry with the persisted pending allocation, before closeout.
    job.update(deepcopy(saved[-1]))
    job["phase"] = "repairing"
    develop(stage, state, job, tmp_path)
    assert requests[1]["thread_id"] == "new"
    assert job["pending_semantic_attempt"] == first_attempt
    assert job["review_budget"] == first_budget
    assert job["candidate_sha"] == "candidate"
    assert job["pr_number"] == 12


@pytest.mark.parametrize('thread', ['old', 'reviewer'])
def test_fresh_development_rejects_reserved_identity(thread: str) -> None:
    with pytest.raises(
        ValueError,
        match=r'^Change Job Development Thread is not independent: .*cannot reuse',
    ):
        require_development_thread(
            {'development_thread_history': ['old'], 'reviewer_thread_ids': ['reviewer']},
            thread, requested_thread=None,
        )


@pytest.mark.parametrize('same_attempt', [False, True])
@pytest.mark.parametrize('requested_thread', ['old', None])
def test_recovery_counters_belong_to_attempt_not_thread(same_attempt: bool, requested_thread: str | None) -> None:
    owner: dict[str, Any] = {}
    attempt = allocate_semantic_attempt(owner, role='development', work_subject='ticket:1', generation=1,
                                        currentness_boundary={'base_sha': 'base'}, ordinal=2, budget_window=1)
    previous = deepcopy(attempt)
    if not same_attempt:
        previous['attempt_id'] = 'previous-attempt'
    state = {'active_agent_invocation': {
        'semantic_attempt': previous, 'output_attempt': 3, 'ordinary_recovery_used': True,
        'capacity_recovery_count': 4, 'status': 'completed', 'reported_thread_id': 'old',
        'validation_error': 'old format error',
    }}
    record = invocation_event_recorder(
        state, role='development', phase='developing', work_subject='ticket:1', generation=1,
        invocation_input={'thread_id': requested_thread}, currentness_boundary={'base_sha': 'base'},
        semantic_attempt=attempt, save=lambda value: None,
    )
    record('started', requested_thread_id=requested_thread)
    active = state['active_agent_invocation']
    assert active['output_attempt'] == (3 if same_attempt else 1)
    assert active['ordinary_recovery_used'] is same_attempt
    assert active['capacity_recovery_count'] == (4 if same_attempt else 0)
    assert active['validation_error'] == ''
