"""Detailed History retains each physical return after semantic consumption."""
from dataclasses import replace
from io import StringIO
import json
from collections import Counter

import pytest

from agent_run.cli.render import render
from agent_run.core.kernel import Kernel
from agent_run.model import DeliveryKind, Role, wire_json
from agent_run.queries.run import read
from support.offline import review_result
from support.review_facts import requirement_review
from support.worker_facts import start_worker, finish_worker
from support.notification import continued_round
from support.environment import durable_snapshot as _snapshot


@pytest.mark.parametrize("returns,marker,count", [
    ((wire_json({"bad-original": "JSON-RETURN"}),) * 2, "JSON-RETURN", 2),
    ((b"\x1b[31mTEXT-RETURN\x1b[0m\x07", b"null"), "TEXT-RETURN", 1),
], ids=["repeated-json", "text-and-null"])
def test_consumed_review_keeps_each_original_format_return(tmp_path, monkeypatch, returns, marker, count):
    # Generate original invocations and raw returns in Core, then reopen the
    # native database for the read-only History/CLI projection.
    with requirement_review(tmp_path / "scenario", kind=DeliveryKind.PARENT_ONLY) as (kernel, operations):
        development = operations.until(lambda step: step.call is not None).call
        assert development.role == Role.DEVELOPMENT
        process = start_worker(kernel, operations, development)
        finish_worker(kernel, development, process)
        calls = []
        for ordinal, raw in enumerate((*returns, wire_json(review_result("pass")))):
            review = operations.until(lambda step: step.call is not None).call
            assert review.role == Role.REVIEW and review.output_ordinal == ordinal
            calls.append(review)
            process = start_worker(kernel, operations, review)
            kernel.bind_thread(review, review.reported_thread or "original-review-backend")
            finish_worker(kernel, review, process, raw)
        assert operations.once().kind == "progress"
        assert len({call.attempt_id for call in calls}) == len({call.invocation_id for call in calls}) == 1
        native = Kernel(kernel.storage.path, run_id=kernel.storage.run_id)
    before = _snapshot(tmp_path)
    document = read(native.storage.path, 'history', storage=native.storage)
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
        page = read(native.storage.path, 'history', after=event['position'] - 1, limit=1, storage=native.storage)
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
    document = read(scenario.kernel.storage.path, 'history', storage=scenario.kernel.storage)
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
