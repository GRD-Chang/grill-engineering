"""Lightweight effort checks using the public History fact projection."""
from copy import deepcopy
from typing import Any

import pytest

from agent_run.history_facts import history_records
from agent_run.ticket_effort import ticket_effort


def invocation(identity: str, role: str = "development", *, start: int = 0,
               model: str = "model-a", effort: str = "low") -> dict[str, Any]:
    return {
        "semantic_attempt": {"attempt_id": identity, "role": role,
                             "work_subject": "ticket:269", "ordinal": 1},
        "work_subject": "ticket:269", "role": role,
        "reported_thread_id": "actual-thread",
        "started_at": f"2026-01-01T00:{start:02d}:00+00:00",
        "ended_at": f"2026-01-01T00:{start:02d}:30+00:00",
        "execution_segments": [{"started_at": f"2026-01-01T00:{start:02d}:00+00:00",
                                "ended_at": f"2026-01-01T00:{start:02d}:30+00:00"}],
        "status": "completed", "model": model, "reasoning_effort": effort,
    }


def aggregate(values: list[dict[str, Any]], *, truncated: bool = False) -> dict[str, Any]:
    state = {"agent_invocation_history": values, "timeline_at_capacity": truncated}
    audit = {"agent_invocations": values}
    original = deepcopy(state)
    result = ticket_effort(state, history_records(state, audit), "ticket:269")
    assert state == original
    return result


def test_semantic_rounds_deduplicate_resume_and_reset_ordinals() -> None:
    values = [invocation("initial"), invocation("initial", start=10),
              invocation("repair", start=20), invocation("review", "review", start=30),
              invocation("publish", "publication", start=40)]
    values[1]["attempt_count"] = 3  # Output repair belongs to the same Invocation.
    result = aggregate(values)
    assert result["development"] == {
        "rounds": 2, "rounds_complete": True, "execution_seconds": 90, "shared_rounds": False,
        "execution_timing_complete": True, "recorded_execution_seconds": 90,
        "configurations_complete": True,
        "configurations": [{"model": "model-a", "reasoning_effort": "low",
                            "rounds": 2, "rounds_complete": True, "execution_seconds": 90,
                            "execution_timing_complete": True, "recorded_execution_seconds": 90}],
    }
    assert result["review"]["rounds"] == 1
    assert result["review"]["execution_seconds"] == 30
    assert "publication" not in result


def test_configuration_participation_does_not_sum_into_round_count() -> None:
    values = [invocation("initial"), invocation("initial", start=10, effort="high"),
              invocation("repair", start=20, model="model-b")]
    result = aggregate(values)["development"]
    assert result["rounds"] == 2
    assert result["shared_rounds"] is True
    assert [(group["model"], group["reasoning_effort"], group["rounds"],
             group["execution_seconds"]) for group in result["configurations"]] == [
        ("model-a", "low", 1, 30), ("model-a", "high", 1, 30), ("model-b", "low", 1, 30)]


def test_waits_excluded_and_timeline_capacity_does_not_erase_independent_facts() -> None:
    value = invocation("initial")
    value["recovery_wait_intervals"] = [{"started_at": "2026-01-01T00:00:05+00:00",
                                          "ended_at": "2026-01-01T00:00:25+00:00"}]
    value["execution_segments"] = [
        {"started_at": "2026-01-01T00:00:00+00:00", "ended_at": "2026-01-01T00:00:05+00:00"},
        {"started_at": "2026-01-01T00:00:25+00:00", "ended_at": "2026-01-01T00:00:30+00:00"},
    ]
    assert aggregate([value], truncated=True)["development"]["execution_seconds"] == 10


def test_missing_timing_preserves_rounds_configuration_and_other_group_time() -> None:
    values = [invocation("initial"), invocation("repair", start=10, model="model-b")]
    del values[0]["ended_at"]
    del values[0]["execution_segments"][0]["ended_at"]
    result = aggregate(values)["development"]
    assert result["rounds"] == 2
    assert "execution_seconds" not in result
    assert "execution_seconds" not in result["configurations"][0]
    assert result["configurations"][1]["execution_seconds"] == 30


def test_missing_binding_preserves_independent_role_totals() -> None:
    value = invocation("initial")
    del value["model"]
    result = aggregate([value])["development"]
    assert result["execution_seconds"] == 30
    assert result["rounds"] == 1
    assert result["configurations"][0]["reasoning_effort"] == "low"
    assert result["configurations"][0]["model"] is None
    assert result["configurations_complete"] is False


@pytest.mark.parametrize("missing", ["attempt_id", "invocation"])
def test_missing_association_does_not_report_partial_total(missing: str) -> None:
    values = [invocation("initial"), invocation("repair", start=10)]
    state = {"agent_invocation_history": values}
    records = history_records(state, {"agent_invocations": values})
    if missing == "attempt_id":
        records[1]["attempt_id"] = None
    else:
        records.pop()
    result = ticket_effort(state, records, "ticket:269")["development"]
    assert result["execution_timing_complete"] is False
    assert "execution_seconds" not in result
    assert result["recorded_execution_seconds"] == 60
    assert result["rounds_complete"] is False
    assert result["configurations"][0]["model"] == "model-a"


def test_attempt_without_invocation_preserves_only_count() -> None:
    attempt = invocation("initial")["semantic_attempt"]
    records = history_records({}, {"semantic_agent_attempts": [attempt]})
    assert ticket_effort({}, records, "ticket:269") == {
        "development": {"rounds": 1, "rounds_complete": True, "shared_rounds": False,
                        "execution_timing_complete": False}}


def test_unclassified_invocation_prevents_incomplete_role_totals() -> None:
    unknown = invocation("unknown", start=10)
    del unknown["role"]
    del unknown["semantic_attempt"]
    result = aggregate([invocation("initial"), unknown])["development"]
    assert result["rounds"] == 1
    assert result["rounds_complete"] is False
    assert result["execution_timing_complete"] is False
    assert result["recorded_execution_seconds"] == 30
    assert result["configurations"][0]["model"] == "model-a"


def test_resume_reference_to_missing_segment_preserves_reliable_facts() -> None:
    values = [invocation("initial", start=10)]
    state = {"agent_invocation_history": values, "resume_audit": {"history": [{
        "semantic_attempt_id": "initial",
        "source_invocation_started_at": "2026-01-01T00:00:00+00:00",
        "successor_invocation_started_at": values[0]["started_at"],
    }]}}
    records = history_records(state, {"agent_invocations": values})
    result = ticket_effort(state, records, "ticket:269")["development"]
    assert result["rounds"] == 1
    assert "execution_seconds" not in result
    assert result["execution_timing_complete"] is False
    assert result["recorded_execution_seconds"] == 30
    assert result["configurations"][0]["model"] == "model-a"


@pytest.mark.parametrize("missing", ["model", "reasoning_effort"])
def test_each_configuration_field_survives_independently(missing: str) -> None:
    from agent_run.notification_cards import card

    value = invocation("initial")
    del value[missing]
    del value["ended_at"]
    effort = aggregate([value])
    rendered = str(card({"language": "zh", "title": "Completed", "effort": effort}))
    assert ("low" if missing == "model" else "model-a") in rendered
    assert "未记录" in rendered
    assert "耗时记录不完整" in rendered


def test_partial_configuration_is_not_folded_into_complete_role_total() -> None:
    from agent_run.notification_cards import card

    values = [invocation("initial"), invocation("repair", start=10)]
    del values[1]["model"]
    del values[1]["reasoning_effort"]
    effort = aggregate(values)
    rendered = card({"language": "zh", "title": "Completed", "effort": effort})
    lines = rendered["body"]["elements"][0]["content"].splitlines()
    total = next(line for line in lines if "开发投入" in line)
    config = next(line for line in lines if "model-a" in line)
    assert "2 轮" in total and "1 分 0 秒" in total
    assert "model-a" not in total
    assert "已记录 1 轮" in config
    assert "已记录执行时长：30 秒" in config
