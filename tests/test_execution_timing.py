"""Trusted timing keeps partial work separate from complete semantic totals."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from agent_run.agent_invocation import invocation_event_recorder, record_operator_stop
from agent_run.execution_timing import (
    aggregate_invocation_timing,
    execution_totals,
    invocation_started,
    invocation_execution_seconds,
    record_execution_timing,
)
from agent_run.history_facts import history_records
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.notification_cards import card
from agent_run.notification_events import events
from agent_run.status_actions import status_timing


def invocation(start: str = "00:00:00", end: str | None = "00:01:00", **facts: Any) -> dict[str, Any]:
    return {
        "started_at": f"2026-09-21T{start}+00:00",
        "ended_at": f"2026-09-21T{end}+00:00" if end else None,
        "execution_segments": [{"started_at": f"2026-09-21T{start}+00:00",
                                "ended_at": f"2026-09-21T{end}+00:00" if end else None}],
        "reported_thread_id": "thread-1", "status": "completed",
        "model": "model-a", "reasoning_effort": "high",
        "semantic_attempt": {
            "attempt_id": "attempt-1", "role": "development",
            "work_subject": "ticket:3", "generation": 1, "ordinal": 1,
        },
        **facts,
    }


def project(values: list[dict[str, Any]], **state_facts: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = {"agent_invocation_history": values, **state_facts}
    return state, history_records(state, {"agent_invocations": values})


@pytest.mark.parametrize("facts", [
    {"reported_thread_id": None}, {"reported_thread_id": " "}, {"started_at": "invalid"},
])
def test_actual_start_requires_both_timestamp_and_reported_thread(facts: dict[str, Any]) -> None:
    assert invocation_started(invocation())
    assert not invocation_started(invocation(**facts))


def test_same_round_sums_execution_segments_excluding_resume_and_recovery_waits() -> None:
    first = invocation()
    second = invocation("01:00:00", "01:02:00", model="model-b", recovery_wait_intervals=[{
        "started_at": "2026-09-21T01:00:30+00:00",
        "ended_at": "2026-09-21T01:01:00+00:00",
    }], predecessor_invocation_started_at=first["started_at"])
    second["execution_segments"] = [
        {"started_at": "2026-09-21T01:00:00+00:00", "ended_at": "2026-09-21T01:00:30+00:00"},
        {"started_at": "2026-09-21T01:01:00+00:00", "ended_at": "2026-09-21T01:02:00+00:00"},
    ]
    state, records = project([first, second])
    before = deepcopy(state)
    assert len(records) == 1
    assert records[0]["execution_seconds"] == 150
    assert records[0]["execution_timing_complete"] is True
    assert len(records[0]["configurations"]) == 2
    assert execution_totals(state, records, "2026-09-21T04:00:00+00:00")["total_seconds"] == 150
    assert state == before


@pytest.mark.parametrize("missing", ["end", "predecessor", "resume_source"])
def test_missing_segments_keep_only_recorded_duration_and_configuration(missing: str) -> None:
    first = invocation(end=None, interruption_observed_at="2026-09-21T00:30:00+00:00")
    second = invocation("01:00:00", "01:01:00", model="model-b")
    state, records = project([first, second] if missing == "end" else [second])
    if missing == "predecessor":
        second["predecessor_invocation_started_at"] = first["started_at"]
    elif missing == "resume_source":
        state["resume_audit"] = {"history": [{
            "semantic_attempt_id": "attempt-1",
            "source_invocation_started_at": first["started_at"],
            "successor_invocation_started_at": second["started_at"],
        }]}
    facts = record_execution_timing(state, records[0])
    assert facts == {
        "execution_seconds": None,
        "execution_timing_complete": False,
        "recorded_execution_seconds": 60,
    }
    assert records[0]["configurations"][-1]["model"] == "model-b"
    totals = execution_totals(state, records, "2026-09-21T04:00:00+00:00")
    assert totals["execution_timing_complete"] is False
    assert totals["recorded_execution_seconds"] == 60
    assert "total_seconds" not in totals


def test_unassociated_execution_cannot_be_a_complete_total() -> None:
    state, records = project([invocation(semantic_attempt={})])
    facts = execution_totals(state, records, "2026-09-21T04:00:00+00:00")
    assert facts == {"execution_timing_complete": False, "recorded_execution_seconds": 60}


def test_unknown_duration_does_not_become_zero() -> None:
    assert aggregate_invocation_timing([invocation(end=None)]) == {
        "execution_seconds": None, "execution_timing_complete": False,
        "recorded_execution_seconds": None,
    }
    assert aggregate_invocation_timing([invocation(end="00:00:00")])["execution_seconds"] == 0


@pytest.mark.parametrize("segments", [None, [], "invalid"])
def test_invocation_timestamps_and_waits_cannot_replace_worker_boundaries(segments: Any) -> None:
    legacy = invocation(execution_segments=segments, recovery_wait_intervals=[{
        "started_at": "2026-09-21T00:00:10+00:00", "ended_at": "2026-09-21T00:00:20+00:00",
    }])
    assert aggregate_invocation_timing([legacy]) == {
        "execution_seconds": None, "execution_timing_complete": False,
        "recorded_execution_seconds": None,
    }


@pytest.mark.parametrize("broken", [
    {"started_at": "2026-09-21T00:02:00+00:00"},
    {"ended_at": "2026-09-21T00:03:00+00:00"},
    {"started_at": "2026-09-21T00:03:00+00:00", "ended_at": "2026-09-21T00:02:00+00:00"},
    {"started_at": "2026-09-21T00:00:30+00:00", "ended_at": "2026-09-21T00:02:00+00:00"},
])
def test_missing_or_conflicting_segment_keeps_only_proven_closed_work(broken: dict[str, Any]) -> None:
    value = invocation(end="00:04:00")
    value["execution_segments"] = [invocation()["execution_segments"][0], broken]
    assert aggregate_invocation_timing([value]) == {
        "execution_seconds": None, "execution_timing_complete": False,
        "recorded_execution_seconds": 60,
    }


def test_live_time_uses_only_open_worker_segment_and_closed_work(monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 9, 21, 1, 0, 15, tzinfo=UTC)

    monkeypatch.setattr("agent_run.execution_timing.datetime", Clock)
    value = invocation(status="running", end=None)
    value["execution_segments"] = [invocation()["execution_segments"][0],
                                   {"started_at": "2026-09-21T01:00:00+00:00", "ended_at": None}]
    assert invocation_execution_seconds(value, allow_open=True) == 75
    assert aggregate_invocation_timing([value])["recorded_execution_seconds"] == 60
    value["status"] = "failed"
    value["ended_at"] = "2026-09-21T02:00:00+00:00"
    assert invocation_execution_seconds(value, allow_open=True) is None


def test_closed_worker_segments_are_not_extended_by_controller_lifetime() -> None:
    value = invocation(end=None)
    value["execution_segments"] = invocation()["execution_segments"]
    assert invocation_execution_seconds(value) == 60
    value["ended_at"] = "2026-09-21T01:00:00+00:00"
    assert invocation_execution_seconds(value) == 60


def test_parallel_subjects_are_summed_without_using_wall_clock_or_timeline_capacity() -> None:
    second = invocation("00:00:00", "00:02:00")
    second["semantic_attempt"].update(attempt_id="attempt-2", work_subject="ticket:4")
    state, records = project([invocation(), second], timeline_at_capacity=True)
    total = execution_totals(state, records, "2026-09-21T04:00:00+00:00")
    assert total["total_seconds"] == 180
    selected = execution_totals(state, records, "2026-09-21T04:00:00+00:00", work_subject="ticket:3")
    assert selected["total_seconds"] == 60


def test_successor_persists_predecessor_for_gap_detection() -> None:
    first = invocation(status="failed")
    state: dict[str, Any] = {"active_agent_invocation": first, "agent_invocation_history": [first]}
    recorder = invocation_event_recorder(
        state, role="development", phase="developing", work_subject="ticket:3", generation=1,
        invocation_input={}, currentness_boundary={}, semantic_attempt=first["semantic_attempt"],
        save=lambda _state: None,
    )
    recorder("started")
    recorder("execution_started")
    recorder("thread_started", reported_thread_id="thread-2")
    recorder("execution_finished")
    recorder("completed")
    successor = state["agent_invocation_history"][-1]
    assert successor["predecessor_invocation_started_at"] == first["started_at"]
    complete = record_execution_timing(state, {"attempt_id": "attempt-1", "invocations": [first, successor]})
    assert complete["execution_timing_complete"] is True
    incomplete = record_execution_timing(state, {"attempt_id": "attempt-1", "invocations": [successor]})
    assert incomplete["execution_timing_complete"] is False


def test_operator_stop_records_observation_without_inventing_end() -> None:
    active = invocation(end=None, status="running", work_subject="parent-only:r")
    state: dict[str, Any] = {"status": "developing", "active_agent_invocation": active}
    record_operator_stop(state, save=lambda _state: None)
    stopped = state["active_agent_invocation"]
    assert stopped["status"] == "failed"
    assert stopped["ended_at"] is None
    assert stopped["interruption_observed_at"]
    assert state["agent_invocation_history"] == [stopped]
    assert aggregate_invocation_timing([stopped])["recorded_execution_seconds"] is None


@pytest.mark.parametrize("container", ["active_agent_invocation", "ticket_agent_invocations"])
def test_current_invocation_proves_missing_latest_segment(container: str) -> None:
    state, records = project([invocation()])
    latest = invocation("01:00:00", "01:01:00")
    state[container] = latest if container == "active_agent_invocation" else {"3": latest}
    assert record_execution_timing(state, records[0])["execution_timing_complete"] is False
    latest["semantic_attempt"]["attempt_id"] = "another-attempt"
    assert record_execution_timing(state, records[0])["execution_timing_complete"] is True


@pytest.mark.parametrize("facts", [{}, {"attempt_count": 0}, {"attempt_count": 1},
                                  {"status": "failed", "attempt_count": 0},
                                  {"requested_thread_id": "prior-thread", "attempt_count": 0}])
def test_unproven_execution_is_unknown_even_with_closed_timestamps(facts: dict[str, Any]) -> None:
    unknown = invocation(reported_thread_id=None, **facts)
    assert invocation_execution_seconds(unknown) is None
    assert invocation_execution_seconds(unknown, allow_open=True) is None
    assert aggregate_invocation_timing([unknown, invocation("01:00:00", "01:00:30")]) == {
        "execution_seconds": None, "execution_timing_complete": False,
        "recorded_execution_seconds": 30,
    }


@pytest.mark.parametrize("failure", ["currentness", "deadline", "after_output"])
@pytest.mark.parametrize("output_attempt", [1, 2])
def test_preparation_gates_and_resume_count_only_proven_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, output_attempt: int,
) -> None:
    clock = [datetime(2026, 10, 1, tzinfo=UTC)]
    origin = clock[0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return clock[0]

    monkeypatch.setattr("agent_run.agent_invocation.datetime", Clock)
    monkeypatch.setattr("agent_run.codex.time.monotonic", lambda: (clock[0] - origin).total_seconds())
    attempt = {**invocation()["semantic_attempt"], "status": "pending"}
    state: dict[str, Any] = {"language": "zh", "run_id": "r", "status": "developing",
                             "semantic_agent_attempts": [attempt],
                             "active_ticket_job": {"ticket_number": 3, "phase": "developing",
                                                   "ticket_branch_generation": 1}}
    state["ticket_jobs"] = {"3": state["active_ticket_job"]}
    backend = CodexCliBackend()
    calls = []

    def invoke(**options: Any) -> tuple[str, str]:
        calls.append(options)
        options["on_execution"]("execution_started")
        options["on_thread"]("actual-thread")
        clock[0] += timedelta(seconds=30)
        options["on_execution"]("execution_finished")
        if failure == "after_output" and len(calls) == 1:
            return "{}", "actual-thread"
        return json.dumps({"result_kind": "development", "summary": "done", "human_blockers": None}), "actual-thread"

    monkeypatch.setattr(backend, "_invoke", invoke)

    def recorder() -> Any:
        return invocation_event_recorder(
            state, role="development", phase="developing", work_subject="ticket:3", generation=1,
            invocation_input={}, currentness_boundary={}, semantic_attempt=attempt, save=lambda _state: None,
        )

    def currentness() -> bool:
        if failure == "after_output":
            return not calls
        clock[0] += timedelta(seconds=60)
        return failure == "deadline"

    record = recorder()
    record.recovery_state["output_attempt"] = output_attempt
    with pytest.raises(CodexProcessError, match="currentness changed|Invocation Deadline"):
        backend.develop({"checkout": str(tmp_path), "_invocation_event": record,
                         "_currentness_check": currentness, "_invocation_deadline_seconds": 30})
    already_executed = failure == "after_output"
    assert len(calls) == int(already_executed)
    assert invocation_started(state["active_agent_invocation"]) is already_executed

    def assert_timing(expected: int) -> None:
        before = deepcopy(state)
        records = history_records(state, {"agent_invocations": state["agent_invocation_history"]})
        assert len(records) == 1
        assert records[0]["execution_timing_complete"] is True
        assert records[0]["execution_seconds"] == expected
        timing = status_timing(state, state["active_agent_invocation"])
        assert timing["execution_timing_complete"] is True
        assert timing["total_seconds"] == timing["round_seconds"] == expected
        ended = next(event for event in events(state) if event["kind"] == "stage_end")
        assert ended["duration_seconds"] == expected
        content = card(ended)["body"]["elements"][0]["content"]
        duration = {0: "0 秒", 30: "30 秒", 60: "1 分 0 秒"}[expected]
        assert f"**本轮执行耗时**：{duration}" in content
        assert "耗时记录不完整" not in content
        assert state == before

    assert_timing(30 if already_executed else 0)
    # A new Invocation of the same semantic round follows an hour of waiting.
    clock[0] = origin + timedelta(hours=1)
    state["status"] = "developing"
    backend.develop({"checkout": str(tmp_path), "_invocation_event": recorder(),
                     "_currentness_check": lambda: True})
    attempt.update(status="completed", outcome="candidate")
    assert len(calls) == 1 + int(already_executed)
    assert len(state["agent_invocation_history"]) == 2
    assert_timing(60 if already_executed else 30)
