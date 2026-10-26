"""Human History consumes native Attempt/Invocation facts without another clock."""
from pathlib import Path
from dataclasses import replace
from io import StringIO
import json

import pytest

from agent_run.model import Issue, ParentIssue, Requirements, wire_json
from agent_run.queries.run import read
from support.offline import GraphScenario
from test_kernel_notification_events import _recorded_agents
from test_kernel_query_cli import _snapshot


@pytest.fixture
def continued_round(tmp_path: Path, monkeypatch, request):
    timing = request.param
    scenario = GraphScenario(tmp_path / "scenario", Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    scenario.kernel.configure_profiles("original-profile", overrides={
        "development_model": "original-model", "development_effort": "low"})
    now = [100.0]
    scenario.kernel.clock = lambda: now[0]
    calls = []
    original_agent = scenario.agent

    def started(call):
        start = now[0]
        if timing != "unknown":
            scenario.kernel.observe_worker_activity(call, "turn-start", active=True,
                observed_at=start + 1, evidence={"type": "turn.started"})
            scenario.kernel.observe_worker_activity(call, "turn-end", active=False,
                observed_at=start + (3 if not calls else 4), evidence={"type": "turn.completed"})
            if timing == "partial" and not calls:
                scenario.kernel.observe_worker_activity(call, "unfinished-turn", active=True,
                    observed_at=start + 4, evidence={"type": "turn.started"})
        now[0] += 5

    def agent(call):
        calls.append(call)
        if len(calls) == 1:
            return wire_json({"result_kind": "human_blocker", "summary": None,
                              "human_blockers": ["Keep the original decision / 保留原文"]})
        return original_agent(call)

    _recorded_agents(scenario, monkeypatch, agent, started)
    assert scenario.run().kind == "paused"
    scenario.kernel.configure_profiles("new-profile", overrides={
        "development_model": "new-model", "development_effort": "high"})
    now[0] = 200.0  # This human wait must never become Agent execution time.
    assert scenario.kernel.control("resume", "original-answer", subject="ticket:2", new_thread=True,
                                   message="原始回复\r\nKeep exactly this.") == "applied"
    assert scenario.step().call is not None
    assert scenario.step().kind == "progress"
    assert len(calls) == 2 and calls[0].attempt_id == calls[1].attempt_id
    assert calls[0].thread_id != calls[1].thread_id
    return scenario, calls, timing


@pytest.mark.parametrize("continued_round", ["complete", "partial", "unknown"], indirect=True)
def test_history_timing_distinguishes_complete_partial_and_unknown_without_counting_human_wait(
    continued_round, tmp_path: Path,
) -> None:
    from agent_run.queries.execution import invocation_timing

    scenario, calls, timing = continued_round
    before = _snapshot(tmp_path)
    document = read(scenario.kernel.storage.path, "history")
    attempt, = document.data["history"]["attempts"]
    assert attempt["id"] == calls[0].attempt_id
    assert [(item["model"], item["effort"]) for item in attempt["invocations"]] == [
        ("original-model", "low"), ("new-model", "high")]
    facts = invocation_timing(attempt["invocations"])
    assert facts["execution_timing_complete"] is (timing == "complete")
    if timing == "complete":
        assert facts["execution_seconds"] == 5
    else:
        assert "execution_seconds" not in facts
    if timing != "unknown":
        assert facts["recorded_execution_seconds"] == 5
    else:
        assert "recorded_execution_seconds" not in facts
    assert _snapshot(tmp_path) == before


def test_closed_activity_without_worker_exit_does_not_prove_complete_execution(tmp_path: Path) -> None:
    from agent_run.queries.execution import invocation_timing
    from test_kernel_query_cli import _create, _operations

    kernel = _create(tmp_path)
    kernel.clock = lambda: 100.0
    operations = _operations(kernel, kernel.claim_executor("original-query-executor"))
    call = operations.once().call
    assert call is not None and kernel.dispatch_agent(call)
    operations.runner.authorize_agent(call)
    # Explicit Core process-observation seam, without launching or killing an
    # OS process. A closed Agent activity segment is not an exit observation.
    kernel.agent_started(call, {"pid": 23, "pgid": 23, "start_token": "synthetic-original",
                                "host_pid": 10, "host_start_token": "synthetic-host"})
    kernel.observe_worker_activity(call, "start", active=True, observed_at=101.0, evidence={"type": "turn.started"})
    kernel.observe_worker_activity(call, "end", active=False, observed_at=103.0, evidence={"type": "turn.completed"})
    before = _snapshot(tmp_path)
    attempt, = read(kernel.storage.path, "history").data["history"]["attempts"]
    assert invocation_timing(attempt["invocations"]) == {
        "execution_timing_complete": False, "recorded_execution_seconds": 2}
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("continued_round", ["complete", "partial", "unknown"], indirect=True)
def test_history_renders_one_round_with_original_configurations_and_true_timing(continued_round, tmp_path: Path) -> None:
    from agent_run.cli.render import render

    scenario, calls, timing = continued_round
    document = read(scenario.kernel.storage.path, "history")
    original = json.dumps(document.data, ensure_ascii=False, sort_keys=True)
    before = _snapshot(tmp_path)
    for language, role, elapsed, recorded in (("en", "Development Agent", "Round execution time", "Recorded execution time"),
                                               ("zh", "开发 Agent", "本轮执行耗时", "已记录执行时长")):
        for details in (False, True):
            for style in ("plain", "rich"):
                output = StringIO()
                render(replace(document, language=language), style=style, details=details, stream=output)
                value = output.getvalue()
                timing_text = value.replace("：", ": ")
                assert value.count(f"#2 {role} #1") == 1
                assert value.count("original-model") == 1 and value.count("new-model") == 1
                assert "Keep exactly this." in value and "原始回复" in value
                assert calls[0].attempt_id not in value
                if timing == "complete":
                    assert f"{elapsed}: 00:00:05" in timing_text
                    assert "耗时记录不完整" not in value and "Execution timing incomplete" not in value
                else:
                    assert f"{elapsed}: 00:00:05" not in timing_text
                    if timing == "partial":
                        assert f"{recorded}: 00:00:05" in timing_text
                    else:
                        assert recorded + ":" not in value
                        assert "00:00:00" not in value
                assert json.dumps(document.data, ensure_ascii=False, sort_keys=True) == original
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("raw,marker", [(wire_json({"unvalidated-review": "原始材料"}), "unvalidated-review"),
    (b"unvalidated-review / non-JSON text", "unvalidated-review"), (b"null", "\nnull\n")], ids=["json", "text", "null"])
def test_history_keeps_unconsumed_malformed_review_as_raw_evidence_without_a_verdict(tmp_path: Path, monkeypatch, raw: bytes, marker: str) -> None:
    from agent_run.cli.render import render

    scenario = GraphScenario(tmp_path / "scenario", Requirements(ParentIssue(1, "Parent", "Original scope"), ()))
    original = scenario.agent

    def agent(call):
        if call.role == "review":
            # Format repair requires the backend thread actually reported by
            # this normal synthetic Agent, not merely a logical Thread row.
            scenario.kernel.bind_thread(call, call.reported_thread or "history-review-backend")
        return raw if call.role == "review" else original(call)

    _recorded_agents(scenario, monkeypatch, agent)
    for _ in range(30):
        step = scenario.step()
        if step.call is not None and step.call.role == "review":
            break
    else:
        raise AssertionError("The normal pipeline did not reach Review")
    before = _snapshot(tmp_path)
    document = read(scenario.kernel.storage.path, "history")
    review = next(row for row in document.data["history"]["attempts"] if row["role"] == "review")
    assert review["status"] == "returned" and review["outcome"] is None
    for details in (False, True):
        output = StringIO()
        render(replace(document, language="en"), style="plain", details=details, stream=output)
        assert "awaiting Controller consumption" in output.getvalue()
        assert "Acceptance passed" not in output.getvalue()
        assert (marker in output.getvalue()) is details
    assert _snapshot(tmp_path) == before
    repair = scenario.step()
    assert repair.call is not None and repair.call.output_ordinal == 1
    full_document = read(scenario.kernel.storage.path, "history")
    full = full_document.data["history"]
    output = StringIO()
    render(replace(full_document, language="en"), style="plain", details=True, stream=output)
    assert output.getvalue().count(f"#1 Review Agent #{review['ordinal']}") == 1
    assert "Output continuations: 1, included in this round" in output.getvalue()
    event = next(item for item in full["events"] if item["kind"] == "output_repair_allocated")
    before = _snapshot(tmp_path)
    page = read(scenario.kernel.storage.path, "history", after=event["position"] - 1, limit=1)
    assert len(page.data["history"]["events"]) == 1 and page.data["history"]["attempts"] == []
    output = StringIO()
    render(replace(page, language="en"), style="plain", details=True, stream=output)
    assert "Round details unavailable on this page" in output.getvalue()
    assert "--after" in output.getvalue()
    assert _snapshot(tmp_path) == before


def test_history_binds_each_review_findings_and_evidence_to_its_original_candidate(tmp_path: Path, monkeypatch) -> None:
    from agent_run.cli.render import render

    scenario = GraphScenario(tmp_path / "scenario", Requirements(ParentIssue(1, "Parent", "Original scope"), ()),
                             reviews=("fail", "pass"))
    original = scenario.agent
    reviews = []
    finding = "问题：ORIGINAL-REVIEW-FINDING\n证据：original evidence\n必须修复：original fix\n复验：original retest"

    def agent(call):
        result = json.loads(original(call))
        if call.role == "review":
            reviews.append(call)
            for name, lane in result["checks"].items():
                lane["evidence"] = f"REVIEW-{len(reviews)}-{name}-EVIDENCE"
                if lane["status"] == "fail":
                    lane["findings"] = [finding]
        return wire_json(result)

    _recorded_agents(scenario, monkeypatch, agent)
    for _ in range(60):
        step = scenario.step()
        if step.call is not None and step.call.role == "review" and len(reviews) == 2:
            assert scenario.step().kind == "progress"
            break
    else:
        raise AssertionError("The normal repair path did not reach the second consumed Review")
    assert reviews[0].context["candidate_sha"] != reviews[1].context["candidate_sha"]
    assert scenario.run().kind == "approval"
    before = _snapshot(tmp_path)
    document = read(scenario.kernel.storage.path, "history")
    attempts = {item["id"]: item for item in document.data["history"]["attempts"]}
    first, second = (attempts[call.attempt_id] for call in reviews)
    publication, = (item for item in attempts.values() if item["role"] == "publication")
    assert (first["outcome"], second["outcome"]) == ("fail", "pass")
    assert [item["candidate_sha"] for item in (first, second)] == [call.context["candidate_sha"] for call in reviews]
    for details in (False, True):
        output = StringIO()
        render(replace(document, language="en"), style="plain", details=details, stream=output)
        value = output.getvalue()
        assert "Current findings" not in value
        assert publication["result"]["pr_title"] in value
        if details:
            assert publication["result"]["commit_message"] in value
            assert publication["result"]["pr_body_markdown"] in value
        first_heading = f"#1 Review Agent #{first['ordinal']}"
        second_heading = f"#1 Review Agent #{second['ordinal']}"
        assert value.count(first_heading) == value.count(second_heading) == 1
        old, new = value.split(first_heading, 1)[1].split(second_heading, 1)
        assert finding in old and finding not in new
        assert "Acceptance failed" in old and "Acceptance passed" in new
        for name in ("e2e", "standards", "spec"):
            assert (f"REVIEW-1-{name}-EVIDENCE" in old) is details
            assert (f"REVIEW-2-{name}-EVIDENCE" in new) is details
    assert _snapshot(tmp_path) == before
