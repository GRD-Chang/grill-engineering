"""Human blocker summaries preserve the semantic round's timing evidence."""
from __future__ import annotations

from copy import deepcopy
from io import StringIO
import json
import sys
from typing import Any

import pytest
from rich.text import Text

from agent_run.cli_presentation import _print_history, _print_status


class TerminalBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def _blocked_round(missing: str, language: str) -> dict[str, Any]:
    attempt = {
        "attempt_id": "publication-1", "role": "publication", "ordinal": 1,
        "work_subject": "run-publication:r", "generation": 1, "status": "pending",
    }
    invocations = []
    for start, end in (("00:00:00", "00:00:10"), ("01:00:00", "01:00:05")):
        started = f"2026-09-21T{start}+00:00"
        ended = f"2026-09-21T{end}+00:00"
        invocations.append({
            "semantic_attempt": deepcopy(attempt), "work_subject": attempt["work_subject"],
            "generation": 1,
            "status": "completed", "role": "publication", "model": "blocker-model",
            "reasoning_effort": "high", "reported_thread_id": "publication-thread",
            "started_at": started, "ended_at": ended,
            "execution_segments": [{"started_at": started, "ended_at": ended}],
        })
    invocations[-1]["predecessor_invocation_started_at"] = invocations[0]["started_at"]
    if missing in {"previous_end", "current_end"}:
        invocations[0 if missing == "previous_end" else 1]["execution_segments"][0]["ended_at"] = None
    elif missing == "previous_invocation":
        invocations.pop(0)
    elif missing == "all_segments":
        for invocation in invocations:
            invocation.pop("execution_segments")
    return {
        "language": language, "run_id": "r", "repository": "example/project",
        "parent": {"number": 1, "title": "Publication blocker"}, "status": "ready_for_human",
        "run_publication": {
            "publication_generation": 1,
            "phase": "ready_for_human", "blocked_reason": "agent_requires_human",
            "human_blocker_phase": "pending", "human_blockers": ["Grant Issue read access."],
            "pending_semantic_attempt": attempt,
        },
        "agent_invocation_history": invocations,
    }


@pytest.mark.parametrize("missing, expected", [
    ("none", 15), ("previous_end", 5), ("current_end", 10),
    ("previous_invocation", 5), ("all_segments", None),
])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_blocker_timing_matches_history_in_all_query_presentations(
    missing: str, expected: int | None, language: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = TerminalBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "180")
    state = _blocked_round(missing, language)
    before = deepcopy(state)
    complete = missing == "none"
    for query in (_print_status, _print_history):
        output.seek(0)
        output.truncate(0)
        query(state, as_json=True)
        action = json.loads(output.getvalue())["operator_action"]
        invocation = action["trigger_invocation"]
        assert invocation["duration_seconds"] == (expected if complete else None)
        assert invocation["execution_timing_complete"] is complete
        assert invocation["recorded_execution_seconds"] == expected
        assert invocation["model"] == "blocker-model"
        assert invocation["reasoning_effort"] == "high"
        for plain in (True, False):
            output.seek(0)
            output.truncate(0)
            query(state, as_json=False, plain=plain)
            rendered = Text.from_ansi(output.getvalue()).plain
            assert "blocker-model" in rendered and "high" in rendered
            assert "agent-run resume 1 --repo example/project" in rendered
            assert "None 秒" not in rendered and "None seconds" not in rendered
            if complete:
                assert ("15 秒" if language == "zh" else "15 seconds") in rendered
                if query is _print_status or plain:
                    assert ("本轮时长: 15 秒" if language == "zh" else "round duration: 15 seconds") in rendered
            else:
                assert ("耗时记录不完整" if language == "zh" else "Execution timing record incomplete") in rendered
                assert ("本轮时长" if language == "zh" else "round duration") not in rendered
                if expected is not None:
                    assert (f"已记录执行时长：{expected} 秒" if language == "zh"
                            else f"Recorded execution time: {expected} seconds") in rendered
    assert state == before


@pytest.mark.parametrize("missing, expected", [
    ("previous_end", 5), ("current_end", 10), ("all_segments", None),
])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_parallel_blockers_keep_each_generations_round_timing(
    missing: str, expected: int | None, language: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _blocked_round("none", language)
    state.update(status="active", delivery_type="ticket_run", ticket_jobs={},
                 agent_invocation_history=[], ticket_agent_invocations={})
    state.pop("run_publication")
    for number, missing_part in ((2, "none"), (3, missing)):
        source = _blocked_round(missing_part, language)
        job = source["run_publication"]
        job.update(ticket_number=number, ticket_branch_generation=2, phase="blocked")
        job.pop("publication_generation")
        attempt = job["pending_semantic_attempt"]
        attempt.update(attempt_id=f"ticket-{number}-round", work_subject=f"ticket:{number}",
                       generation=2, role="development")
        for invocation in source["agent_invocation_history"]:
            invocation.update(semantic_attempt=deepcopy(attempt), work_subject=f"ticket:{number}",
                              generation=2, role="development", model=f"model-{number}")
        state["ticket_jobs"][str(number)] = job
        state["agent_invocation_history"].extend(source["agent_invocation_history"])
        state["ticket_agent_invocations"][str(number)] = source["agent_invocation_history"][-1]
    state["active_ticket_job"] = state["ticket_jobs"]["2"]
    # A later-observed record from another Generation must not replace either
    # current blocker's model or its same-round execution total.
    stale = deepcopy(state["agent_invocation_history"][-1])
    stale.update(generation=1, model="stale-model")
    stale["semantic_attempt"].update(generation=1, attempt_id="old-round")
    state["agent_invocation_history"].append(stale)
    state["active_agent_invocation"] = stale
    before = deepcopy(state)
    output = TerminalBuffer()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "200")
    for query in (_print_status, _print_history):
        output.seek(0)
        output.truncate(0)
        query(state, as_json=True)
        actions = json.loads(output.getvalue())["operator_actions"]
        assert len(actions) == 2
        for action, number, recorded in zip(actions, (2, 3), (15, expected), strict=True):
            trigger = action["trigger_invocation"]
            assert action["work_subject"] == f"ticket:{number}"
            assert action["generation"] == 2 and action["scope"] == "local"
            assert action["next_action"].endswith(f"--ticket {number}")
            assert trigger["model"] == f"model-{number}" and trigger["reasoning_effort"] == "high"
            assert trigger["execution_timing_complete"] is (number == 2)
            assert trigger["recorded_execution_seconds"] == recorded
            assert trigger["duration_seconds"] == (15 if number == 2 else None)
        for plain in (True, False):
            output.seek(0)
            output.truncate(0)
            query(state, as_json=False, plain=plain)
            rendered = Text.from_ansi(output.getvalue()).plain
            assert "model-2" in rendered and "model-3" in rendered
            assert "--ticket 2" in rendered and "--ticket 3" in rendered
            assert ("15 秒" if language == "zh" else "15 seconds") in rendered
            if query is _print_status or plain:
                assert ("本轮时长: 15 秒" if language == "zh" else "round duration: 15 seconds") in rendered
            assert ("耗时记录不完整" if language == "zh" else "Execution timing record incomplete") in rendered
            if expected is not None:
                assert (f"已记录执行时长：{expected} 秒" if language == "zh"
                        else f"Recorded execution time: {expected} seconds") in rendered
            assert "None 秒" not in rendered and "None seconds" not in rendered
            assert ("仅此子任务" if language == "zh" else "Only this Ticket") in rendered
    assert state == before
