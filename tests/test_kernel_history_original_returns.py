"""Detailed History retains each physical return after semantic consumption."""
from dataclasses import replace
from io import StringIO
import json
from collections import Counter

import pytest

from agent_run.cli.render import render
from agent_run.model import ParentIssue, Requirements, wire_json
from agent_run.queries.run import read
from support.offline import GraphScenario
from support.notification import recorded_agents as _recorded_agents, continued_round
from support.environment import durable_snapshot as _snapshot


@pytest.mark.parametrize("returns,marker,count", [
    ((wire_json({"bad-original": "JSON-RETURN"}),) * 2, "JSON-RETURN", 2),
    ((b"\x1b[31mTEXT-RETURN\x1b[0m\x07", b"null"), "TEXT-RETURN", 1),
], ids=["repeated-json", "text-and-null"])
def test_consumed_review_keeps_each_original_format_return(tmp_path, monkeypatch, returns, marker, count):
    scenario = GraphScenario(tmp_path / "scenario", Requirements(ParentIssue(1, "Parent", "Scope"), ()))
    original = scenario.agent
    calls = []

    def agent(call):
        if call.role == "review":
            scenario.kernel.bind_thread(call, call.reported_thread or "original-review-backend")
            calls.append(call)
            if call.output_ordinal < len(returns):
                return returns[call.output_ordinal]
        return original(call)

    _recorded_agents(scenario, monkeypatch, agent)
    for _ in range(60):
        step = scenario.step()
        if step.call is not None and step.call.role == "review" and step.call.output_ordinal == 2:
            assert scenario.step().kind == "progress"
            break
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("The actual Review did not consume its repaired result")
    assert len(calls) == 3
    assert len({call.attempt_id for call in calls}) == len({call.invocation_id for call in calls}) == 1
    before = _snapshot(tmp_path)
    document = read(scenario.kernel.storage.path, "history")
    payload = json.dumps(document.data, ensure_ascii=False, sort_keys=True)
    review, = [attempt for attempt in document.data["history"]["attempts"] if attempt["role"] == "review"]
    assert review["status"] == "consumed" and review["outcome"] == "pass"
    output = review["invocations"][0]["outputs"]
    assert [item["ordinal"] for item in output] == [0, 1, 2]
    assert output[0]["raw_return"] == (json.loads(returns[0]) if marker == "JSON-RETURN" else returns[0].decode())
    assert output[1]["raw_return"] == (output[0]["raw_return"] if marker == "JSON-RETURN" else None)
    for language in ("zh", "en"):
        for style, width in (("plain", 80), ("rich", 60), ("rich", 80), ("rich", 120)):
            monkeypatch.setenv("COLUMNS", str(width))
            human = StringIO()
            render(replace(document, language=language), style=style, details=True, stream=human)
            value = human.getvalue()
            assert value.count(marker) == count
            assert "\x1b" not in value and "\x07" not in value
            for identity, occurrences in Counter(item["raw_id"] for item in output).items():
                assert "".join(value.split()).count(identity) == occurrences
            if marker == "TEXT-RETURN":
                assert "\nnull\n" in value
            if style == "rich":
                from rich.cells import cell_len
                assert all(cell_len(line) <= width for line in value.splitlines())
            assert value.count("原始返回" if language == "zh" else "Original return") == 4  # Development + three Review outputs.
            compact = StringIO()
            render(replace(document, language=language), style=style, details=False, stream=compact)
            assert marker not in compact.getvalue()
    events = [event for event in document.data["history"]["events"]
              if event["kind"] == "raw_returned" and event["identity"] == calls[0].invocation_id]
    assert len(events) == 3
    for event in events:
        page = read(scenario.kernel.storage.path, "history", after=event["position"] - 1, limit=1)
        assert page.data["history"]["attempts"] == [review]
        human = StringIO()
        render(replace(page, language="en"), style="plain", details=True, stream=human)
        assert human.getvalue().count(marker) == count
        assert human.getvalue().count("Original return") == 3
        assert human.getvalue().count(f"#1 Review Agent #{review['ordinal']}") == 1
        assert "Changed synthetic behavior" not in human.getvalue()
    assert json.dumps(document.data, ensure_ascii=False, sort_keys=True) == payload
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("continued_round", ["complete"], indirect=True)
def test_original_returns_remain_under_their_exact_invocation_after_new_thread_resume(continued_round, tmp_path):
    scenario, calls, _ = continued_round
    document = read(scenario.kernel.storage.path, "history")
    attempt, = document.data["history"]["attempts"]
    assert attempt["id"] == calls[0].attempt_id == calls[1].attempt_id
    assert len(attempt["invocations"]) == 2
    before = _snapshot(tmp_path)
    human = StringIO()
    render(replace(document, language="en"), style="plain", details=True, stream=human)
    value = human.getvalue()
    originals = []
    for invocation in attempt["invocations"]:
        physical, = invocation["outputs"]
        label = f"Invocation {invocation['id']}"
        assert value.count(label) == 1
        # The original JSON follows its exact Invocation/output identity, not
        # the latest semantic result or the shared Attempt's current pointer.
        raw = value.split(label, 1)[1].split("\n", 1)[1].split("\n", 1)[0]
        assert json.loads(raw) == physical["raw_return"]
        originals.append(json.loads(raw))
    assert originals[0]["result_kind"] == "human_blocker"
    assert originals[1]["result_kind"] == "development"
    assert "Keep the original decision / 保留原文" in originals[0]["human_blockers"]
    assert originals[1]["summary"] == "Changed synthetic behavior 1"
    assert "Round execution time: 00:00:05" in value
    assert _snapshot(tmp_path) == before
