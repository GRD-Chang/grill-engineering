"""Human History consumes native Attempt/Invocation facts without another clock."""
from pathlib import Path
from dataclasses import replace
from io import StringIO
import json

import pytest

from agent_run.model import DeliveryKind, ParentIssue, Requirements, wire_json
from agent_run.queries.run import read
from support.offline import GraphScenario
from support.notification import recorded_agents as _recorded_agents
from support.worker_facts import worker_fixture, start_worker, finish_worker, consume_local_result
from support.environment import durable_snapshot as _snapshot


@pytest.fixture
def continued_round(tmp_path, request):
    timing = request.param
    kernel, operations = worker_fixture(tmp_path / "consumer")
    operations.prepare_agents = True
    kernel.configure_profiles("original-profile", overrides={
        "development_model": "original-model", "development_effort": "low"})
    now = [100.]
    kernel.clock = lambda: now[0]
    calls = []
    for continued in (False, True):
        call = operations.once().call
        calls.append(call)
        assert kernel.dispatch_agent(call)
        operations.runner.authorize_agent(call)
        process = {"pid": 21, "pgid": 21, "start_token": call.invocation_id,
                   "host_pid": 10, "host_start_token": "synthetic-host"}
        kernel.agent_started(call, process)
        if timing != "unknown":
            kernel.observe_worker_activity(call, "turn-start", active=True, observed_at=now[0] + 1,
                                          evidence={"type": "turn.started"})
            kernel.observe_worker_activity(call, "turn-end", active=False, observed_at=now[0] + (4 if continued else 3),
                                          evidence={"type": "turn.completed"})
            if timing == "partial" and not continued:
                kernel.observe_worker_activity(call, "unfinished-turn", active=True, observed_at=now[0] + 4,
                                              evidence={"type": "turn.started"})
        now[0] += 5
        raw = ({"result_kind": "development", "summary": "Original work", "human_blockers": None} if continued else
               {"result_kind": "human_blocker", "summary": None, "human_blockers": ["Keep the original decision / 保留原文"]})
        kernel.receive(call, wire_json(raw))
        kernel.agent_exited(call, process, 0)
        assert operations.once().kind == ("progress" if continued else "paused")
        if not continued:
            kernel.configure_profiles("new-profile", overrides={"development_model": "new-model", "development_effort": "high"})
            now[0] = 200.  # Human wait must never count as worker execution.
            assert kernel.control("resume", "original-answer", subject=call.subject, new_thread=True,
                                  message="原始回复\r\nKeep exactly this.") == "applied"
    assert calls[0].attempt_id == calls[1].attempt_id and calls[0].thread_id != calls[1].thread_id
    return kernel, calls, timing


def test_closed_activity_without_worker_exit_does_not_prove_complete_execution(tmp_path: Path) -> None:
    from agent_run.queries.execution import invocation_timing
    kernel, operations = worker_fixture(tmp_path / "consumer")
    kernel.clock = lambda: 100.0
    operations.prepare_agents = True
    call = operations.once().call
    runner = operations.runner
    assert call is not None and kernel.dispatch_agent(call)
    runner.authorize_agent(call)
    # Explicit Core process-observation seam, without launching or killing an
    # OS process. A closed Agent activity segment is not an exit observation.
    kernel.agent_started(call, {"pid": 23, "pgid": 23, "start_token": "synthetic-original",
                                "host_pid": 10, "host_start_token": "synthetic-host"})
    kernel.observe_worker_activity(call, "start", active=True, observed_at=101.0, evidence={"type": "turn.started"})
    kernel.observe_worker_activity(call, "end", active=False, observed_at=103.0, evidence={"type": "turn.completed"})
    before = _snapshot(tmp_path)
    attempt, = read(kernel.storage.path, 'history', storage=kernel.storage).data["history"]["attempts"]
    assert invocation_timing(attempt["invocations"]) == {
        "execution_timing_complete": False, "recorded_execution_seconds": 2}
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("continued_round", ["complete", "partial", "unknown"], indirect=True)
def test_history_renders_one_round_with_original_configurations_and_true_timing(continued_round, tmp_path: Path) -> None:
    from agent_run.cli.render import render
    from agent_run.queries.execution import invocation_timing

    kernel, calls, timing = continued_round
    document = read(kernel.storage.path, 'history', storage=kernel.storage)
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
def test_history_keeps_unconsumed_malformed_review_as_raw_evidence_without_a_verdict(tmp_path: Path, raw: bytes, marker: str) -> None:
    from agent_run.cli.render import render

    kernel, operations = worker_fixture(tmp_path / "consumer", kind=DeliveryKind.PARENT_ONLY)
    operations.prepare_agents = True
    development = operations.once().call
    process = start_worker(kernel, operations, development)
    finish_worker(kernel, development, process)
    assert operations.once().kind == "progress"
    consume_local_result(kernel, operations, development.subject, "candidate")
    consume_local_result(kernel, operations, development.subject, "validation_checkout")
    call = operations.once().call
    assert call.role == "review"
    process = start_worker(kernel, operations, call)
    kernel.bind_thread(call, "history-review-backend")
    finish_worker(kernel, call, process, raw)
    before = _snapshot(tmp_path)
    document = read(kernel.storage.path, 'history', storage=kernel.storage)
    review = next(row for row in document.data["history"]["attempts"] if row["role"] == "review")
    assert review["status"] == "returned" and review["outcome"] is None
    for details in (False, True):
        output = StringIO()
        render(replace(document, language="en"), style="plain", details=details, stream=output)
        assert "awaiting Controller consumption" in output.getvalue()
        assert "Acceptance passed" not in output.getvalue()
        assert (marker in output.getvalue()) is details
    assert _snapshot(tmp_path) == before
    repair = operations.once()
    assert repair.call is not None and repair.call.output_ordinal == 1
    process = start_worker(kernel, operations, repair.call)
    finish_worker(kernel, repair.call, process, raw)
    full_document = read(kernel.storage.path, 'history', storage=kernel.storage)
    full = full_document.data["history"]
    output = StringIO()
    render(replace(full_document, language="en"), style="plain", details=True, stream=output)
    assert output.getvalue().count(f"#1 Review Agent #{review['ordinal']}") == 1
    assert "Output continuations: 1, included in this round" in output.getvalue()
    event = next(item for item in full["events"] if item["kind"] == "output_repair_allocated")
    before = _snapshot(tmp_path)
    page = read(kernel.storage.path, 'history', after=event['position'] - 1, limit=1, storage=kernel.storage)
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
    document = read(scenario.kernel.storage.path, 'history', storage=scenario.kernel.storage)
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
        assert (finding in old) is details
        assert "ORIGINAL-REVIEW-FINDING" in old and "ORIGINAL-REVIEW-FINDING" not in new
        assert ("original evidence" in old) is details
        assert ("original fix" in old) is details
        assert ("original retest" in old) is details
        if not details:
            assert "Findings (1)" in old
        assert "Budget at round start: not recorded" in old
        assert value.rstrip().endswith(f"Task elapsed seconds: {document.data['elapsed_seconds']}")
        assert "Role rounds in this history page" in value
        assert "Parent-only · Review Agent: 2" in value
        assert "Acceptance failed" in old and "Acceptance passed" in new
        for name in ("e2e", "standards", "spec"):
            assert (f"REVIEW-1-{name}-EVIDENCE" in old) is details
            assert (f"REVIEW-2-{name}-EVIDENCE" in new) is details
    assert _snapshot(tmp_path) == before


def test_default_history_bounds_human_text_without_changing_details_or_audit(tmp_path):
    from agent_run.cli.render import render

    kernel, operations = worker_fixture(tmp_path / "consumer")
    operations.prepare_agents = True
    blocker = "ORIGINAL-BLOCKER " + "长文 x " * 300 + " BLOCKER-END"
    reply = "ORIGINAL-REPLY\r\n" + "回复 y " * 300 + " REPLY-END"
    call = operations.once().call
    process = start_worker(kernel, operations, call)
    finish_worker(kernel, call, process, wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": [blocker]}))
    assert operations.once().kind == "paused"
    assert kernel.control("resume", "long-original-answer", subject=call.subject, message=reply) == "applied"
    assert operations.once().call is not None
    before = _snapshot(tmp_path)
    document = read(kernel.storage.path, 'history', storage=kernel.storage)
    audit = json.dumps(document.data, ensure_ascii=False, sort_keys=True)
    for language, marker in (("en", "truncated; --details retains the original"),
                             ("zh", "已截断；--details 保留原文")):
        for details in (False, True):
            output = StringIO()
            render(replace(document, language=language), style="plain", details=details, stream=output)
            value = output.getvalue()
            assert ("BLOCKER-END" in value) is details
            assert ("REPLY-END" in value) is details
            assert (marker in value) is not details
            assert "ORIGINAL-BLOCKER" in value and "ORIGINAL-REPLY" in value
        assert json.dumps(document.data, ensure_ascii=False, sort_keys=True) == audit
    assert blocker in audit and "REPLY-END" in audit
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("finding,problem", [
    ("问题：精确问题\n证据：证据原文\n必须修复：修复原文\n复验：复验原文", "精确问题"),
    ("Problem: exact problem\r\nEvidence: original evidence\r\nRequired fix: original fix\r\nRetest: original retest", "exact problem"),
    ("unstructured original", None),
    ("Problem: original\nEvidence: only two parts", None),
    ("Problem: original\nEvidence: one\nEvidence: repeated\nRequired fix: fix\nRetest: test", None),
    ("preface\nProblem: original\nEvidence: one\nRequired fix: fix\nRetest: test", None),
    ("Problem: original\nEvidence:\nRequired fix: fix\nRetest: test", None),
])
def test_history_problem_extraction_is_conservative_and_does_not_translate_originals(finding, problem):
    from agent_run.cli.render import _history_problem
    from agent_run.contracts.copy import text

    for language in ("zh", "en"):
        expected = problem if problem is not None else text(
            "presentation.history.unsplit_finding", language=language, finding=finding)
        assert _history_problem(finding, language) == expected
