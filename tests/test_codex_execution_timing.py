"""Worker boundaries exclude Controller checks, setup and recovery gaps."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

import pytest

from agent_run.agent_invocation import invocation_event_recorder
from agent_run.codex import CodexCliBackend, CodexProcessError
from agent_run.history_facts import history_records
from agent_run.notification_cards import card
from agent_run.notification_events import events
from agent_run.status_actions import status_timing


@pytest.mark.parametrize("scenario", ["success", "result_rejected", "output_repair", "automatic_recovery", "manual_resume"])
def test_only_worker_segments_count_across_slow_controller_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    clock = [datetime(2026, 10, 2, tzinfo=UTC)]
    origin = clock[0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return clock[0]

    monkeypatch.setattr("agent_run.agent_invocation.datetime", Clock)
    monkeypatch.setattr("agent_run.codex.time.monotonic", lambda: (clock[0] - origin).total_seconds())
    monkeypatch.setattr("agent_run.codex.bubblewrap_command", lambda arguments, **_options: arguments)
    calls: list[dict[str, Any]] = []

    def run_worker(arguments: list[str], *, on_process_started: Any, on_process_released: Any,
                   on_stdout_line: Any, **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(options)
        on_process_started(1234)
        # A registered bootstrap can still be waiting on Controller checks.
        clock[0] += timedelta(seconds=60)
        on_process_released()
        line = json.dumps({"type": "thread.started", "thread_id": "actual-thread"})
        on_stdout_line(line)
        clock[0] += timedelta(seconds=30)
        if scenario == "automatic_recovery" and len(calls) == 1:
            return subprocess.CompletedProcess(arguments, 1, line, "transient process failure")
        result = {"result_kind": "development", "summary": "done", "human_blockers": None}
        if scenario == "output_repair" and len(calls) == 1:
            result = {"invalid": True}
        Path(arguments[arguments.index("--output-last-message") + 1]).write_text(json.dumps(result))
        return subprocess.CompletedProcess(arguments, 0, line, "")

    monkeypatch.setattr("agent_run.codex.run_worker_process", run_worker)

    def slow_setup() -> str:
        clock[0] += timedelta(seconds=60)
        return "isolated-reader"

    backend = CodexCliBackend(credential_provider=slow_setup)
    attempt = {"attempt_id": "a1", "role": "development", "work_subject": "ticket:3",
               "ordinal": 1, "generation": 1, "status": "pending"}
    state: dict[str, Any] = {"language": "zh", "run_id": "r", "status": "developing",
                             "semantic_agent_attempts": [attempt],
                             "active_ticket_job": {"ticket_number": 3, "phase": "developing",
                                                   "ticket_branch_generation": 1}}
    state["ticket_jobs"] = {"3": state["active_ticket_job"]}
    checks = 0

    def currentness() -> bool:
        nonlocal checks
        checks += 1
        clock[0] += timedelta(seconds=60)
        return not (scenario == "result_rejected" and checks == 2)

    def recorder() -> Any:
        record = invocation_event_recorder(
            state, role="development", phase="developing", work_subject="ticket:3", generation=1,
            invocation_input={}, currentness_boundary={}, semantic_attempt=attempt, save=lambda _state: None,
        )
        # This test supplies the Controller's authorization for one same-round recovery.
        record.recovery_allowed = lambda: True
        return record

    request = {"checkout": str(tmp_path), "_invocation_event": recorder(), "_currentness_check": currentness}
    if scenario == "result_rejected":
        with pytest.raises(CodexProcessError, match="before result application"):
            backend.develop(request)
    else:
        backend.develop(request)
    if scenario == "manual_resume":
        clock[0] += timedelta(hours=1)
        backend.develop({**request, "thread_id": "actual-thread", "_invocation_event": recorder()})
    if scenario != "result_rejected":
        attempt.update(status="completed", outcome="candidate")
    expected = 60 if scenario in {"output_repair", "automatic_recovery", "manual_resume"} else 30
    assert len(calls) == expected // 30
    before = deepcopy(state)
    records = history_records(state, {"agent_invocations": state["agent_invocation_history"]})
    assert len(records) == 1
    assert records[0]["execution_seconds"] == expected
    assert records[0]["execution_timing_complete"] is True
    timing = status_timing(state, state["active_agent_invocation"])
    assert timing["round_seconds"] == timing["total_seconds"] == expected
    assert timing["execution_timing_complete"] is True
    ended = next(event for event in events(state) if event["kind"] == "stage_end")
    assert ended["duration_seconds"] == expected
    content = card(ended)["body"]["elements"][0]["content"]
    assert f"**本轮执行耗时**：{('30 秒' if expected == 30 else '1 分 0 秒')}" in content
    assert "耗时记录不完整" not in content
    assert state == before
    if scenario == "output_repair":
        # An Invocation end cannot fill a lost Worker end. Retain the other
        # closed segment consistently in all three consumers.
        state["agent_invocation_history"][0]["execution_segments"][-1]["ended_at"] = None
        records = history_records(state, {"agent_invocations": state["agent_invocation_history"]})
        timing = status_timing(state)
        ended = next(event for event in events(state) if event["kind"] == "stage_end")
        for facts in (records[0], timing, ended):
            assert facts["execution_timing_complete"] is False
            assert facts["recorded_execution_seconds"] == 30
        assert "耗时记录不完整" in str(card(ended))
        assert "**已记录执行时长**：30 秒" in str(card(ended))


@pytest.mark.parametrize("scenario", ["success", "result_rejected", "output_repair", "gate_rejected"])
def test_real_worker_timing_starts_after_bootstrap_gate_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    # Exercise the real bootstrap and bubblewrap. A process registration is
    # deliberately not evidence that its payload has been released.
    worker = tmp_path / "worker"
    marker = tmp_path / "payload-starts"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    worker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "count = len(marker.read_text().splitlines()) if marker.exists() else 0\n"
        "with marker.open('a') as stream: stream.write('started\\n')\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'real-worker'}))\n"
        "result = {'result_kind': 'development', 'summary': 'done', 'human_blockers': None}\n"
        f"if {scenario == 'output_repair'!r} and count == 0: result = {{'invalid': True}}\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(result))\n",
        encoding="utf-8",
    )
    worker.chmod(0o700)
    offset = timedelta()

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime.now(UTC) + offset

    monkeypatch.setattr("agent_run.agent_invocation.datetime", Clock)
    pending_gate = False
    gate_ends: list[datetime] = []
    process_ids: list[int] = []

    def registered(pid: int) -> None:
        nonlocal pending_gate, offset
        process_ids.append(pid)
        pending_gate = True
        # Model slow registration/persistence without an expensive real wait.
        offset += timedelta(seconds=60)

    def control_check() -> None:
        nonlocal pending_gate, offset
        if not pending_gate:
            return
        pending_gate = False
        count = len(marker.read_text().splitlines()) if marker.exists() else 0
        assert count == len(gate_ends)
        # One physical delay proves the payload stays behind the gate; the
        # other cases use a controlled clock to keep the regression bounded.
        if scenario == "success":
            time.sleep(1.1)
        offset += timedelta(seconds=60)
        assert (len(marker.read_text().splitlines()) if marker.exists() else 0) == count
        gate_ends.append(Clock.now())
        if scenario == "gate_rejected":
            raise RuntimeError("operator stopped before gate release")

    monkeypatch.setattr("agent_run.worker_control.check_worker_control", control_check)
    attempt = {"attempt_id": "real-a1", "role": "development", "work_subject": "ticket:3",
               "ordinal": 1, "generation": 1, "status": "pending"}
    state: dict[str, Any] = {"language": "zh", "run_id": "r", "status": "developing",
                             "semantic_agent_attempts": [attempt],
                             "active_ticket_job": {"ticket_number": 3, "phase": "developing",
                                                   "ticket_branch_generation": 1}}
    state["ticket_jobs"] = {"3": state["active_ticket_job"]}
    record = invocation_event_recorder(
        state, role="development", phase="developing", work_subject="ticket:3", generation=1,
        invocation_input={}, currentness_boundary={}, semantic_attempt=attempt, save=lambda _state: None,
    )
    checks = 0

    def currentness() -> bool:
        nonlocal checks
        checks += 1
        return not (scenario == "result_rejected" and checks == 2)

    backend = CodexCliBackend(executable=str(worker), credential_provider=lambda: "isolated-reader",
                              on_worker_started=registered)
    request = {"checkout": str(checkout), "_invocation_event": record, "_currentness_check": currentness}
    if scenario == "gate_rejected":
        with pytest.raises(RuntimeError, match="before gate release"):
            backend.develop(request)
        assert not marker.exists()
        assert not any(segment.get("started_at") for segment in state["active_agent_invocation"]["execution_segments"])
        assert status_timing(state)["execution_timing_complete"] is False
    else:
        if scenario == "result_rejected":
            with pytest.raises(CodexProcessError, match="before result application"):
                backend.develop(request)
        else:
            backend.develop(request)
            attempt.update(status="completed", outcome="candidate")
        segments = state["active_agent_invocation"]["execution_segments"]
        assert len(segments) == len(gate_ends) == (2 if scenario == "output_repair" else 1)
        for segment, gate_end in zip(segments, gate_ends, strict=True):
            assert datetime.fromisoformat(segment["started_at"]) >= gate_end
        expected = int(sum((datetime.fromisoformat(segment["ended_at"])
                            - datetime.fromisoformat(segment["started_at"])).total_seconds()
                           for segment in segments))
        records = history_records(state, {"agent_invocations": state["agent_invocation_history"]})
        timing = status_timing(state, state["active_agent_invocation"])
        ended = next(event for event in events(state) if event["kind"] == "stage_end")
        assert records[0]["execution_seconds"] == timing["round_seconds"] == ended["duration_seconds"] == expected
        assert all(facts["execution_timing_complete"] for facts in (records[0], timing, ended))
        assert "耗时记录不完整" not in str(card(ended))
    for pid in process_ids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
