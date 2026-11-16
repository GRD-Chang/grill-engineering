"""Recorded exact Worker termination is not a fresh Host liveness observation."""
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path

import pytest

from agent_run.cli.render import render
from agent_run.model import ParentIssue, Requirements
from agent_run.queries.run import read
from support.environment import durable_snapshot
from support.notification import recorded_agents
from support.offline import GraphScenario


@pytest.mark.parametrize("record_exit", [False, True], ids=["raw-only-unknown", "core-recorded-exit"])
def test_status_distinguishes_recorded_worker_exit_from_raw_return_and_current_host_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_exit: bool,
) -> None:
    scenario = GraphScenario(tmp_path / "scenario", Requirements(ParentIssue(1, "Parent", "Scope"), ()))
    starts = []
    if record_exit:
        # The existing controlled observer records through dispatch/start/exit
        # Core APIs. It is a synthetic process contract, NOT physical Host proof.
        def on_start(_call):
            starts.append(read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage))

        recorded_agents(scenario, monkeypatch, started=on_start)
    for _ in range(30):
        step = scenario.step()
        if step.call is not None:
            break
        assert step.kind not in {"paused", "waiting", "complete", "approval"}
    else:
        raise AssertionError("The normal producer did not allocate Development")
    assert step.call.role == "development"
    before = durable_snapshot(tmp_path)
    document = read(scenario.kernel.storage.path, "status", storage=scenario.kernel.storage)
    job, = document.data["subjects"]
    invocation, = job["pending_attempt"]["invocations"]
    output, = invocation["outputs"]
    assert output["raw_id"] is not None
    assert (output["exited_at"] is not None) is record_exit
    assert (output["started_at"] is not None) is record_exit
    assert document.data["executor_control"]["live_status"] == "unknown"
    assert invocation["live_status"] == "unknown"
    audit = json.dumps(document.data, ensure_ascii=False, sort_keys=True)
    labels = (("en", "Recorded Worker exit", "Recorded started; current liveness unconfirmed"),
              ("zh", "已记录 Worker 退出", "已记录启动；当前活性未确认"))
    for language, exit_label, started in labels:
        for style in ("plain", "rich"):
            target = StringIO()
            render(replace(document, language=language), style=style, details=False, stream=target)
            value = target.getvalue()
            assert (exit_label in value) is record_exit
            assert started not in value
            assert ("Executor liveness is unconfirmed" if language == "en" else "执行者当前活性未确认") in value
            assert step.call.invocation_id not in value
        assert json.dumps(document.data, ensure_ascii=False, sort_keys=True) == audit
    assert durable_snapshot(tmp_path) == before

    assert len(starts) == int(record_exit)
    for started_document in starts:
        for language, exit_label, started in labels:
            target = StringIO()
            render(replace(started_document, language=language), style="plain", details=False, stream=target)
            assert exit_label not in target.getvalue()
            assert started in target.getvalue()
        assert started_document.data["executor_control"]["live_status"] == "unknown"
