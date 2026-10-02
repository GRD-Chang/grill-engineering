from __future__ import annotations

from copy import deepcopy

import pytest

from agent_run.guidance_actions import NextAction, next_action_fact
from agent_run.operator_action_facts import operator_action_facts
from agent_run.operator_action_presentation import operator_action_views


def parallel_state() -> dict:
    return {
        "run_id": "r1", "language": "zh", "repository": "example/project", "parent": {"number": 1},
        "status": "active", "delivery_type": "ticket_run",
        "active_ticket_job": {"ticket_number": 2, "phase": "developing", "ticket_branch_generation": 1},
        "ticket_jobs": {
            "2": {"ticket_number": 2, "phase": "developing", "ticket_branch_generation": 1},
            "3": {"ticket_number": 3, "phase": "blocked", "ticket_branch_generation": 2},
        },
    }


@pytest.mark.parametrize(("reason", "kind", "phase", "status_code"), [
    ("agent_requires_human", "human_blocker", "blocked", "ready_for_human"),
    ("reviewer_requires_human", "human_blocker", "blocked", "ready_for_human"),
    ("ticket_execution_failed", "execution_failure", "blocked", "execution_failed"),
    ("modification_budget_exhausted", "review_budget_checkpoint", "blocked", "progress_exhausted"),
    ("review_budget_exhausted", "review_budget_checkpoint", "blocked", "progress_exhausted"),
    ("ci_inner_budget_exhausted", "review_budget_checkpoint", "blocked", "progress_exhausted"),
    ("ci_outer_budget_exhausted", "review_budget_checkpoint", "blocked", "progress_exhausted"),
    ("integration_resync_exhausted", "review_budget_checkpoint", "blocked", "progress_exhausted"),
    ("ticket_supervision_timeout", "supervision_timeout", "blocked", "supervision_timeout"),
    ("ticket_paused", "operator_stopped", "blocked", "operator_stopped"),
    (None, "publication_retry_exhausted", "publication_pending", "publication_pending"),
])
def test_nonselected_ticket_action_comes_from_its_own_evidence(
    reason: str | None, kind: str, phase: str, status_code: str,
) -> None:
    state = parallel_state()
    job = state["ticket_jobs"]["3"]
    job.update(phase=phase, blocked_reason=reason)
    if kind == "human_blocker":
        job.update(human_blockers=["Original reviewer evidence"], human_blocker_phase="reviewing")
    if kind == "execution_failure":
        job.update(execution_failure="git fetch failed: remote host unavailable", execution_resume_phase="developing")
    before = deepcopy(state)
    facts = operator_action_facts(state)
    assert len(facts) == 1
    fact = facts[0]
    assert (fact["work_subject"], fact["generation"], fact["scope"]) == ("ticket:3", 2, "local")
    assert (fact["action_kind"], fact["status_code"]) == (kind, status_code)
    assert fact["action"] == NextAction("command", "resume", "r1", 3)
    assert next_action_fact(state) == fact["action"]
    if kind == "human_blocker":
        assert fact["reasons"] == ["Original reviewer evidence"]
        assert fact["evidence"]["human_blockers"] == ["Original reviewer evidence"]
    if kind == "execution_failure":
        assert fact["reasons"] == [job["execution_failure"]]
        assert fact["evidence"]["execution_failure"] == job["execution_failure"]
        assert fact["phase"] == "developing"
    assert state == before


@pytest.mark.parametrize(("location", "key", "generation_key", "subject_id"), [
    ("parent", "parent_job", "parent_generation", "parent-only:r1"),
    ("run_acceptance", "run_acceptance", "acceptance_generation", "run-acceptance:r1"),
    ("run_repair", "repair_job", "repair_generation", "run-repair:r1"),
    ("run_publication", "run_publication", "publication_generation", "run-publication:r1"),
])
def test_global_work_objects_keep_identity_and_generation(location: str, key: str, generation_key: str, subject_id: str) -> None:
    subject = {"phase": "blocked", "blocked_reason": "reviewer_requires_human",
               "human_blockers": ["Original evidence"], generation_key: 3}
    state = {"run_id": "r1", "status": "ready_for_human", key: subject}
    if location == "run_repair":
        state = {"run_id": "r1", "status": "ready_for_human", "run_acceptance": {
            "phase": "repairing", "repair_generation": 3,
            "repair_job": {k: v for k, v in subject.items() if k != generation_key},
        }}
    fact, = operator_action_facts(state)
    assert (fact["location"], fact["work_subject"], fact["generation"], fact["scope"]) == (location, subject_id, 3, "global")
    assert fact["action"] == NextAction("command", "resume", "r1")


@pytest.mark.parametrize("status", ["operator_stopped", "supervision_timeout", "unsupported_scope_change", "abandonment_pending", "execution_failed"])
def test_global_gate_coexists_with_local_and_keeps_stable_identity(status: str) -> None:
    state = parallel_state()
    state.update(status=status)
    state["ticket_jobs"]["3"].update(blocked_reason="agent_requires_human", human_blockers=["Local input"])
    state["active_ticket_job"] = deepcopy(state["ticket_jobs"]["3"])
    state["diagnostics"] = [{"code": "global_failure", "message": "Global evidence"}]
    before = operator_action_facts(state)
    assert {fact["scope"] for fact in before} == {"local", "global"}
    global_before = next(fact for fact in before if fact["scope"] == "global")
    assert global_before["work_subject"] == "run:r1"
    assert global_before["reasons"] == ["Global evidence"]
    state["ticket_jobs"]["3"].update(phase="developing", blocked_reason=None)
    state["active_ticket_job"] = deepcopy(state["ticket_jobs"]["3"])
    after = operator_action_facts(state)
    assert len(after) == 1
    assert after[0]["work_subject"] == global_before["work_subject"]
    assert after[0]["action_kind"] == global_before["action_kind"]


@pytest.mark.parametrize(("location", "old_reason"), [
    (location, reason)
    for location in ("ticket:2", "parent", "run_repair")
    for reason in ("agent_requires_human", "reviewer_requires_human", "modification_budget_exhausted")
] + [
    ("ticket:2", reason)
    for reason in ("ticket_execution_failed", "ticket_paused", "publication_retry_exhausted")
])
def test_requeue_supersedes_its_stale_gate_without_hiding_other_tickets(location: str, old_reason: str) -> None:
    state = parallel_state()
    state["ticket_jobs"]["3"].update(blocked_reason="agent_requires_human", human_blockers=["Independent request"])
    generation_key, subject_id, reason = {
        "ticket:2": ("ticket_branch_generation", "ticket:2", "ticket_requirements_changed"),
        "parent": ("parent_generation", "parent-only:r1", "parent_requirements_changed"),
        "run_repair": ("repair_generation", "run-repair:r1", "run_repair_parent_changed"),
    }[location]
    job = {generation_key: 4, "phase": "blocked", "blocked_reason": old_reason,
           "human_blockers": ["Stale request"], "human_blocker_phase": "developing"}
    if old_reason == "publication_retry_exhausted":
        job["phase"] = "publication_pending"
    if location == "ticket:2":
        job["ticket_number"] = 2
        state["active_ticket_job"] = state["ticket_jobs"]["2"] = job
    elif location == "parent":
        state.update(delivery_type="parent_only", active_ticket_job=None, ticket_jobs={})
        state["parent_job"] = job
    else:
        state.update(active_ticket_job=None, ticket_jobs={})
        state["run_acceptance"] = {"phase": "repairing", "repair_generation": 4, "repair_job": job}
    state.update(status="requeue_required", requeue_required={
        "work_subject": subject_id, "generation": 4, "reason": reason,
    }, diagnostics=[{"code": reason, "message": "Change Job Generation is stale; run requeue"}])
    before = deepcopy(state)

    facts = operator_action_facts(state)

    requeue, = [fact for fact in facts if fact["work_subject"] == subject_id]
    assert facts[0] == requeue
    assert (requeue["action_kind"], requeue["status_code"], requeue["scope"]) == ("requeue_required", "requeue_required", "global")
    assert requeue["reason"] == reason
    assert requeue["generation"] == 4
    assert requeue["action"] == NextAction("command", "requeue", "r1")
    assert requeue["reasons"] == [state["diagnostics"][0]["message"]]
    assert requeue["evidence"]["diagnostics"] == state["diagnostics"]
    assert requeue["evidence"]["human_blockers"] is None
    if location == "ticket:2":
        sibling, = [fact for fact in facts if fact["work_subject"] == "ticket:3"]
        assert sibling["reasons"] == ["Independent request"]
        assert sibling["action"] == NextAction("command", "resume", "r1", 3)
        assert sibling["scope"] == "local"
        assert len(facts) == 2
    else:
        assert len(facts) == 1
        assert next_action_fact(state) == requeue["action"]
    assert state == before


@pytest.mark.parametrize("phase", ["queued", "developing", "waiting_checks", "waiting_merge", "integration_wait", "completed"])
def test_normal_wait_and_work_is_not_an_operator_action(phase: str) -> None:
    state = parallel_state()
    state["ticket_jobs"]["3"].update(phase=phase)
    assert operator_action_facts(state) == []


@pytest.mark.parametrize("status", ["active", "run_acceptance_pending", "completed", "abandoned"])
def test_retained_approval_does_not_reappear_during_repair_or_after_completion(status: str) -> None:
    state = {"run_id": "r1", "status": status,
             "run_acceptance": {"phase": "repairing", "repair_job": {"phase": "developing"}},
             "run_publication": {"phase": "ready_for_approval", "generation": 2}}
    assert operator_action_facts(state) == []


def test_matching_diagnostic_enriches_the_same_gate_without_a_duplicate_run_action() -> None:
    state = parallel_state()
    state["active_ticket_job"] = None
    state["ticket_jobs"]["3"].update(blocked_reason="published_head_mismatch")
    state.update(status="deterministic_contradiction", diagnostics=[{
        "message": "Original remote head evidence", "operator_gate": {
            "work_subject": "ticket:3", "action_kind": "deterministic_contradiction",
            "phase": "publishing", "reason": "published_head_mismatch",
        },
    }])
    fact, = operator_action_facts(state)
    assert fact["phase"] == "publishing"
    assert fact["scope"] == "global"
    assert fact["reasons"] == ["Original remote head evidence"]
    assert fact["evidence"]["operator_gate"] == state["diagnostics"][0]["operator_gate"]


def test_each_presentation_action_preserves_its_own_command_and_candidate() -> None:
    state = parallel_state()
    state["ticket_jobs"]["3"].update(blocked_reason="ticket_execution_failed", candidate_sha="candidate-3")
    state["ticket_jobs"]["4"] = {"ticket_number": 4, "phase": "blocked", "ticket_branch_generation": 4,
                                 "blocked_reason": "ticket_paused", "candidate_sha": "candidate-4"}
    actions = operator_action_views(state)
    assert [action["object"] for action in actions] == ["Ticket #3", "Ticket #4"]
    for action, number in zip(actions, [3, 4], strict=True):
        assert action["next_action"] == f"agent-run resume 1 --repo example/project --ticket {number}"
        assert action["preserved"] == f"Candidate candidate-{number}"
        assert not action["run_paused"]


@pytest.mark.parametrize(("reason", "kind"), [
    ("agent_requires_human", "human_blocker"),
    ("reviewer_requires_human", "human_blocker"),
    ("review_budget_exhausted", "review_budget_checkpoint"),
])
def test_independent_run_action_keeps_its_concrete_reason(reason: str, kind: str) -> None:
    state = {"run_id": "r1", "status": "ready_for_human", "blocked_reason": reason,
             "human_blockers": ["Original Run evidence"]}
    fact, = operator_action_facts(state)
    assert fact["action_kind"] == kind
    assert fact["work_subject"] == "run:r1"
    assert fact["scope"] == "global"
    if kind == "human_blocker":
        assert fact["reasons"] == ["Original Run evidence"]


@pytest.mark.parametrize("command", ["status", "history"])
@pytest.mark.parametrize("plain", [True, False])
def test_query_views_keep_all_actions_during_independent_execution(command: str, plain: bool, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_run import cli_presentation

    state = parallel_state()
    state["ticket_jobs"]["3"].update(blocked_reason="agent_requires_human", human_blockers=["First original request"])
    state["ticket_jobs"]["4"] = {"ticket_number": 4, "phase": "blocked", "ticket_branch_generation": 1,
                                 "blocked_reason": "agent_requires_human", "human_blockers": ["Second original request"]}
    state["_executor_control"] = {"activity": "unknown"}
    before = deepcopy(state)
    monkeypatch.setattr(cli_presentation, "_use_rich_status", lambda _plain: not plain)
    query = cli_presentation._print_status if command == "status" else cli_presentation._print_history
    query(state, as_json=False, plain=plain)
    output = capsys.readouterr().out
    assert "First original request" in output
    assert "Second original request" in output
    assert "--ticket 3" in output and "--ticket 4" in output
    assert "仅此子任务" in output
    assert "整项任务已暂停" not in output
    assert state == before


def test_execution_diagnostic_identity_survives_resolution_of_the_same_tickets_local_gate() -> None:
    state = parallel_state()
    state["ticket_jobs"]["3"].update(blocked_reason="agent_requires_human", human_blockers=["Local evidence"])
    state.update(status="execution_failed", diagnostics=[{
        "message": "Credential failure", "operator_gate": {
            "work_subject": "ticket:3", "reason": "runner_credential_failed",
            "action_kind": "execution_failure", "phase": "blocked",
        },
    }])
    facts = operator_action_facts(state)
    assert len(facts) == 2
    failure = next(fact for fact in facts if fact["scope"] == "global")
    assert failure["work_subject"] == "ticket:3"
    assert failure["reason"] == "runner_credential_failed"
    assert failure["reasons"] == ["Credential failure"]
    state["ticket_jobs"]["3"].update(phase="developing", blocked_reason=None)
    remaining, = operator_action_facts(state)
    for key in ("work_subject", "generation", "scope", "action_kind", "reason"):
        assert remaining[key] == failure[key]


@pytest.mark.parametrize("owner", ["run_acceptance", "run_publication"])
def test_global_object_keeps_its_run_diagnostic_without_assigning_it_to_tickets(owner: str) -> None:
    from types import SimpleNamespace

    from agent_run.notification_events import events
    from agent_run.run_parent_integration import _pause

    state = parallel_state()
    subject = {"phase": "pending", "acceptance_generation": 2, "publication_generation": 2}
    state[owner] = subject
    if owner == "run_acceptance":
        # Use the production transition which persists the evidence on Run,
        # not on its Acceptance object and without a diagnostic binding.
        _pause(SimpleNamespace(_save=lambda _state: None), state, subject,
               "integration_conflict_requires_human", "Need administrator to restore conflict access.")
    else:
        # Shape from FinalRunPublication's closed-PR readback boundary.
        subject.update(phase="ready_for_human", blocked_reason="final_run_pr_closed")
        state.update(status="ready_for_human", diagnostics=[{
            "code": "final_run_pr_closed", "message": "Final Run PR #22 was closed; cannot replace automatically",
        }])
    original = state["diagnostics"]
    facts = operator_action_facts(state)
    global_fact = next(fact for fact in facts if fact["location"] == owner)
    assert global_fact["reasons"] == [original[0]["message"]]
    assert global_fact["evidence"]["diagnostics"] == original
    ticket_fact = next(fact for fact in facts if fact["location"] == "ticket:3")
    assert original[0]["message"] not in ticket_fact["reasons"]
    event = next(event for event in events(state) if event.get("work_subject") == global_fact["work_subject"])
    assert original[0]["message"] in event["summary"]
    assert event["evidence"]["diagnostics"] == original


@pytest.mark.parametrize("failure", ["github_write_outcome_unknown", "final_run_ref_authority_failed"])
def test_publication_failure_transition_keeps_its_owned_diagnostic(failure: str, capsys: pytest.CaptureFixture) -> None:
    import json
    from types import SimpleNamespace

    from agent_run.git import GitError
    from agent_run.cli_presentation import _print_history, _print_status
    from agent_run.github import GitHubReadError
    from agent_run.notification_cards import card
    from agent_run.notification_events import events
    from agent_run.run_publication_flow import RunPublicationFlow

    state = parallel_state()
    state["ticket_jobs"]["3"].update(blocked_reason="ticket_execution_failed", execution_failure="Ticket-only error")
    subject = {"phase": "publishing", "publication_generation": 2}
    state["run_publication"] = subject
    # Exercise the production pause writer; the only fake is persistence.
    flow = SimpleNamespace(_save=deepcopy)
    message = "write response lost" if failure == "github_write_outcome_unknown" else "Run ref has a foreign identity"
    if failure == "github_write_outcome_unknown":
        saved = RunPublicationFlow._hold_unknown_write_outcome(
            flow, state, subject, GitHubReadError(failure, message),
        )
    else:
        saved = RunPublicationFlow._handle_final_run_ref_error(flow, state, subject, GitError(message))
    before = deepcopy(saved)
    fact = next(item for item in operator_action_facts(saved) if item["location"] == "run_publication")
    assert fact["reason"] == failure
    assert fact["reasons"] == [message]
    assert fact["evidence"]["diagnostics"] == saved["diagnostics"]
    assert fact["action"] == NextAction("human_items")
    actions = operator_action_views(saved)
    ticket_action = next(item for item in actions if item["work_subject"] == "ticket:3")
    assert ticket_action["reasons"] == ["Ticket-only error"]
    assert message not in str(ticket_action["evidence"])
    for query in (_print_status, _print_history):
        query(saved, as_json=True)
        view = json.loads(capsys.readouterr().out)
        action = next(item for item in view["operator_actions"] if item["work_subject"] == "run-publication:r1")
        assert action["reason"] == failure
        assert action["reasons"] == [message]
        assert action["evidence"]["diagnostics"] == saved["diagnostics"]
        assert action["action"]["kind"] == "human_items"
    boundary = next(event for event in events(saved) if event.get("work_subject") == "run-publication:r1")
    assert boundary["reason"] == failure
    assert boundary["evidence"]["diagnostics"] == saved["diagnostics"]
    assert message in json.dumps(card(boundary), ensure_ascii=False)
    assert saved == before


@pytest.mark.parametrize("command", ["status", "history"])
@pytest.mark.parametrize("plain", [True, False])
def test_query_action_guidance_uses_the_frozen_language(command: str, plain: bool, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_run import cli_presentation
    from agent_run.messages import text

    state = parallel_state()
    state.update(language="en", status="ready_for_human", ticket_jobs={}, active_ticket_job=None,
                 run_publication={"phase": "ready_for_human", "blocked_reason": "final_run_pr_closed"})
    monkeypatch.setattr(cli_presentation, "_use_rich_status", lambda _plain: not plain)
    query = cli_presentation._print_status if command == "status" else cli_presentation._print_history
    query(state, as_json=False, plain=plain)
    output = capsys.readouterr().out
    assert text("guidance.action.human_items", language="en") in output
    assert text("guidance.action.human_items", language="zh") not in output
