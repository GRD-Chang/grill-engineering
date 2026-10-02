"""Low-cost combinations for per-object notification boundaries and outbox recovery."""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.notification_cards import card
from agent_run.notification_events import events, recovery_events
from agent_run.notifications import Notifications, read_notifications
from support.notification import state_with_round


def parallel_state(reason: str = "agent_requires_human", mode: str = "concise") -> dict[str, Any]:
    running = {"ticket_number": 2, "phase": "developing", "generation": 1}
    return {
        "run_id": "r", "language": "zh", "repository": "example/project",
        "parent": {"number": 1, "title": "Parent"}, "status": "active",
        "notifications": {"enabled": True, "mode": mode},
        "ticket_graph": {"tickets": {"2": {"title": "Running"}, "3": {"title": "原始标题"}}},
        "active_ticket_job": running,
        "ticket_jobs": {"2": running, "3": {
            "ticket_number": 3, "generation": 7, "phase": "blocked", "blocked_reason": reason,
            "human_blockers": ["External account required."], "execution_resume_phase": "developing",
        }},
    }


@pytest.mark.parametrize("mode", ["concise", "detailed"])
@pytest.mark.parametrize("reason,phase,kind", [
    ("agent_requires_human", "blocked", "human_blocker"),
    ("ticket_execution_failed", "blocked", "execution_failure"),
    ("modification_budget_exhausted", "blocked", "review_budget_checkpoint"),
    ("ticket_supervision_timeout", "blocked", "supervision_timeout"),
    ("ticket_paused", "blocked", "operator_stopped"),
    ("publication_retry_exhausted", "publication_pending", "publication_retry_exhausted"),
])
def test_nonselected_ticket_has_its_own_boundary(mode: str, reason: str, phase: str, kind: str) -> None:
    state = parallel_state(reason, mode)
    state["ticket_jobs"]["3"]["phase"] = phase
    if kind == "execution_failure":
        state["ticket_jobs"]["3"]["execution_failure"] = "git fetch failed: remote host unavailable"
    before = deepcopy(state)
    boundary, = [event for event in events(state) if event["current"]]
    assert boundary["work_subject"] == "ticket:3"
    assert boundary["generation"] == 7
    assert boundary["scope"] == "local"
    assert boundary["action_kind"] == kind
    assert boundary["next_step"] == "agent-run resume 1 --repo example/project --ticket 3"
    rendered = json.dumps(card(boundary), ensure_ascii=False)
    assert "原始标题" in rendered and "仅影响此子任务" in rendered
    if kind == "execution_failure":
        failure = state["ticket_jobs"]["3"]["execution_failure"]
        assert boundary["summary"] == failure
        assert boundary["evidence"]["execution_failure"] == failure
        assert failure in rendered
    assert state == before
    # Another selection, refreshed wording and display language do not create a
    # new incident. A changed Generation or cause does.
    state["active_ticket_job"] = state["ticket_jobs"]["3"]
    state["ticket_jobs"]["3"]["human_blockers"] = ["Updated original evidence."]
    state["language"] = "en"
    assert next(event for event in events(state) if event["current"])["id"] == boundary["id"]
    state["ticket_jobs"]["3"]["generation"] += 1
    assert next(event for event in events(state) if event["current"])["id"] != boundary["id"]


def test_global_and_local_boundaries_recover_without_combining_identity() -> None:
    state = parallel_state()
    state["status"] = "operator_stopped"
    projected = [event for event in events(state) if event["current"]]
    assert {(event["work_subject"], event["scope"]) for event in projected} == {
        ("ticket:3", "local"), ("run:r", "global"),
    }
    assert {event["id"] for event in recovery_events(state, projected)} == {event["id"] for event in projected}
    global_gate = next(event for event in projected if event["scope"] == "global")
    assert "--ticket" not in global_gate["next_step"]
    state["status"] = "active"
    state["ticket_jobs"]["3"].update(phase="waiting_checks", blocked_reason=None)
    assert not any(event["current"] for event in events(state))
    assert recovery_events(state, projected) == []


def test_resume_failure_does_not_replace_another_tickets_boundary() -> None:
    state = parallel_state()
    state["ticket_jobs"]["4"] = {**state["ticket_jobs"]["3"], "ticket_number": 4}
    state["_manual_resume_result"] = {"id": "resume-3", "outcome": "failed", "evidence": "preflight",
                                      "reason": "resume failed", "work_subject": "ticket:3"}
    boundaries = {event["work_subject"]: event for event in events(state) if event["current"]}
    assert boundaries["ticket:3"]["title"] == "人工恢复失败"
    assert boundaries["ticket:4"]["title"] == "任务暂停，需要你处理"
    assert boundaries["ticket:4"]["next_step"].endswith("--ticket 4")


@pytest.mark.parametrize("role,label", [("development", "开发 Agent"), ("reviewer", "验收 Agent")])
def test_human_card_keeps_the_triggering_role_for_its_own_generation(role: str, label: str) -> None:
    state = parallel_state()
    history = state_with_round(role)
    attempt = history["semantic_agent_attempts"][0]
    attempt.update(work_subject="ticket:3", generation=7)
    invocation = history["agent_invocation_history"][0]
    invocation["semantic_attempt"] = attempt.copy()
    state.update(semantic_agent_attempts=[attempt], agent_invocation_history=[invocation])
    unrelated = {**attempt, "attempt_id": "unrelated", "work_subject": "ticket:2", "role": "publication"}
    state["semantic_agent_attempts"].append(unrelated)
    state["agent_invocation_history"].append({**invocation, "semantic_attempt": unrelated})
    event, = [event for event in events(state) if event["current"]]
    assert event["trigger_role"] == label
    assert label in json.dumps(card(event), ensure_ascii=False)
    state["ticket_jobs"]["3"]["generation"] = 8
    event, = [event for event in events(state) if event["current"]]
    assert event["trigger_role"] is None


def test_outbox_recovers_each_unsent_gate_and_only_realerts_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    offline = True

    def send(_config: Any, rendered: dict[str, Any], _identity: str, _cancel: Any) -> dict[str, Any]:
        calls.append(rendered)
        return {"outcome": "failed" if offline else "success", "reason": "offline" if offline else None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    state = parallel_state()
    state["status"] = "operator_stopped"
    sender = Notifications(tmp_path, state)
    sender.close()
    assert len(read_notifications(tmp_path, "r")["pending"]) == 3
    offline = False
    calls.clear()
    sender = Notifications(tmp_path, state)
    sender.close()
    assert len(calls) == 2  # Both current boundaries; obsolete run_start omitted.
    assert {item["header"]["subtitle"]["content"] for item in calls} == {
        "example/project · #1", "example/project · #3",
    }
    sender = Notifications(tmp_path, state)
    sender.close()
    assert len(calls) == 2
    resolved = deepcopy(state)
    resolved["ticket_jobs"]["3"]["phase"] = "developing"
    sender = Notifications(tmp_path, resolved)
    sender.observe(state)
    sender.close()
    assert len(calls) == 3
    assert calls[-1]["header"]["subtitle"]["content"] == "example/project · #3"


def test_resolved_gate_is_removed_while_other_transport_is_inflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[dict[str, Any]] = []

    def send(_config: Any, rendered: dict[str, Any], _identity: str, _cancel: Any) -> dict[str, Any]:
        calls.append(rendered)
        entered.set()
        assert release.wait(5)
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    state = parallel_state()
    sender = Notifications(tmp_path, state)
    try:
        assert entered.wait(3)
        state["ticket_jobs"]["3"]["phase"] = "developing"
        sender.observe(state)
    finally:
        release.set()
        sender.close()
    assert len(calls) == 1
    assert calls[0]["header"]["title"]["content"] == "任务已开始"
    assert read_notifications(tmp_path, "r")["pending"] == []


def test_current_ticket_set_is_not_evicted_by_progress_backlog_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = parallel_state()
    for number in range(4, 44):
        state["ticket_jobs"][str(number)] = {**state["ticket_jobs"]["3"], "ticket_number": number}
        state["ticket_graph"]["tickets"][str(number)] = {"title": f"Ticket {number}"}
    calls: list[str] = []

    def send(_config: Any, rendered: dict[str, Any], _identity: str, _cancel: Any) -> dict[str, Any]:
        calls.append(rendered["header"]["subtitle"]["content"])
        return {"outcome": "success", "reason": None}

    monkeypatch.setattr("agent_run.notifications.send", send)
    sender = Notifications(tmp_path, state)
    sender.close()
    assert {f"example/project · #{number}" for number in range(3, 44)} <= set(calls)
    assert len(calls) == len(set(calls))
    assert not read_notifications(tmp_path, "r")["pending"]
