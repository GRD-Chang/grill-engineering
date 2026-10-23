"""Public supervision reads the original scoped Core waits without authorizing them."""
from agent_run.core.operation_runner import OperationRunner
from dataclasses import replace
from io import StringIO

import pytest

from agent_run.model import DeliveryKind, Issue, ParentIssue, Requirements
from agent_run.core.kernel import Kernel
import agent_run.queries.run as query
from support.offline import GraphScenario, Scenario
from agent_run.core.operation_contract import ReadUnavailable
from support.core_operations import CoreOperations
from agent_run.cli.render import render
from test_kernel_automatic_supervision import delivery_run, source_operations
from test_kernel_query_cli import _snapshot


def source_run(tmp_path, monkeypatch, language="en"):
    original_create = Kernel.create

    def create(path, spec, **kwargs):
        return original_create(path, replace(spec, language=language), **kwargs)

    monkeypatch.setattr(Kernel, "create", staticmethod(create))
    return delivery_run(tmp_path / "kernel.sqlite3")[0]


def read_only(kernel, tmp_path, kind="status"):
    before = _snapshot(tmp_path)
    document = query.read(kernel.storage.path, kind)
    assert _snapshot(tmp_path) == before
    return document


def test_wait_deadline_does_not_invent_a_timeout_and_keeps_all_scopes(tmp_path, monkeypatch):
    kernel = source_run(tmp_path, monkeypatch)
    execution = kernel.claim_executor("owner")
    kernel.clock = lambda: 100.25
    operations = source_operations(kernel, Requirements(ParentIssue(1, "Parent", "Requirement", (2, 3)), (
        Issue(2, "First", "First work", "OPEN", frozenset({"ready-for-agent"})),
        Issue(3, "Second", "Second work", "OPEN", frozenset({"ready-for-agent"})),)), execution)
    def failed(parent):
        raise ReadUnavailable("github_read_failed", "Original Ticket outage")
    monkeypatch.setattr(operations.runner.ports, "source", failed)
    assert operations.once().retry_at == 105.25
    assert operations.once().retry_at == 105.25
    monkeypatch.setattr(query.time, "time", lambda: 200.5)
    first = read_only(kernel, tmp_path).data
    assert len(first["supervision_windows"]) == 2
    assert "supervision" not in first  # Independent real Ticket scopes have no guessed current window.
    for window in first["supervision_windows"]:
        assert window["started_at"] == 100.25 and window["deadline"] == 700.25
        assert window["remaining_seconds"] == 499 and window["retry_count"] == 1
        assert window["next_retry_at"] == 105.25 and not window["timed_out"]
        assert window["scope"]["subject"] == window["subject"]
    ticket = next(item for item in first["subjects"] if item["issue"] == 2)
    assert ticket["supervision_windows"][0]["latest_observation"]["message"] == "Original Ticket outage"
    monkeypatch.setattr(query.time, "time", lambda: 900)
    expired_clock = read_only(kernel, tmp_path).data
    assert all(window["remaining_seconds"] == 0 and not window["timed_out"] for window in expired_clock["supervision_windows"])
    assert expired_clock["operator_actions"] == []
    assert expired_clock["next_action"] == "agent-run status 1 --repo example/repository"


@pytest.mark.parametrize("language", ["zh", "en"])
def test_run_timeout_renders_original_evidence_and_resumes_only_after_core_applies_it(tmp_path, monkeypatch, language):
    source_run(tmp_path, monkeypatch, language)
    source = Requirements(ParentIssue(1, "Parent", "Requirement", (2,)), (
        Issue(2, "Ticket", "Work", "OPEN", frozenset({"ready-for-agent"})),))
    scenario = GraphScenario(tmp_path / "before-final", source)
    assert scenario.run().kind == "complete"
    kernel, execution = scenario.kernel, scenario.execution
    assert kernel.source_status()["final_subject"] is None
    operations = CoreOperations(kernel, execution, scenario.make_ports)
    def failed(parent):
        raise ReadUnavailable("final_source_missing", "Original failure / 原始问题")
    monkeypatch.setattr(operations.runner.ports, "source", failed)
    kernel.clock = lambda: 100
    assert operations.once().retry_at == 105
    kernel.clock = lambda: 700
    assert operations.once().reason == "supervision_timeout"
    monkeypatch.setattr(query.time, "time", lambda: 900)
    document = read_only(kernel, tmp_path)
    data = document.data
    window = data["supervision_windows"][0]
    command = "agent-run resume 1 --repo example/project"
    assert data["status"] == "supervision_timeout" and data["next_action"] == command
    assert window["timed_out"] and window["remaining_seconds"] == 0
    assert window["timeout"]["scope_id"] == window["scope_id"]
    assert window["timeout_resume_action"] == command == window["resume_action"]
    assert window["latest_observation"] == {"code": "final_source_missing", "message": "Original failure / 原始问题"}
    action = data["operator_actions"][0]
    assert action["operation"] == "resume" and action["work_subject"] is None
    assert action["evidence"]["failure_evidence"]["supervision_windows"] == [window]
    for style in ("plain", "rich"):
        output = StringIO()
        render(document, style=style, details=False, stream=output)
        assert command in output.getvalue() and "Original failure / 原始问题" in output.getvalue()
    history = read_only(kernel, tmp_path, "history").data
    assert history["supervision_windows"][0] == window
    # The original event is also the human renderer's sole source; there is no second audit projection.
    paused, = [event for event in history["history"]["events"] if event["kind"] == "paused"
               and isinstance(event["evidence"], dict)
               and event["evidence"].get("scope_id") == window["scope_id"]]
    assert paused["identity"] == "supervision_timeout" and paused["evidence"] == window["timeout"]
    assert paused["status"] is not None and paused["generation"] >= 1
    assert kernel.control("stop", "retire-original-executor") == "applied"
    kernel.confirm_executor_exit(execution)
    assert kernel.control("resume", "explicit-original-resume") == "applied"
    assert read_only(kernel, tmp_path).data["supervision_windows"] == []
    replacement = kernel.claim_executor("replacement")
    fresh = OperationRunner(kernel, replacement, lambda binding: operations.runner.ports)
    kernel.clock = lambda: 800
    assert fresh.run_once().retry_at == 805
    assert read_only(kernel, tmp_path).data["supervision_windows"][0]["started_at"] == 800


def test_ticket_timeout_keeps_sibling_waits_and_exact_target_resume(tmp_path, monkeypatch):
    kernel = source_run(tmp_path, monkeypatch)
    execution = kernel.claim_executor("owner")
    kernel.clock = lambda: 100
    _, source = delivery_run(tmp_path / "explicit-source.sqlite3")
    operations = source_operations(kernel, source, execution)
    def failed(parent):
        raise ReadUnavailable("github_read_failed", "Original Ticket error" if kernel.clock() == 100 else "Independent sibling")
    monkeypatch.setattr(operations.runner.ports, "source", failed)
    assert operations.once().retry_at == 105
    kernel.clock = lambda: 200
    assert operations.once().retry_at == 210  # Polling does not renew ticket:2's original deadline.
    assert operations.once().retry_at == 205
    kernel.clock = lambda: 700
    assert operations.once().reason == "supervision_timeout"
    monkeypatch.setattr(query.time, "time", lambda: 750)
    data = read_only(kernel, tmp_path).data
    windows = {window["subject"]: window for window in data["supervision_windows"]}
    assert windows["ticket:2"]["timed_out"] and not windows["ticket:3"]["timed_out"]
    assert "supervision" not in data  # No guessed current window across independent Tickets.
    assert data["status"] == "active"
    assert data["next_action"] == "agent-run resume 1 --repo example/repository --ticket 2"
    assert data["operator_actions"][0]["evidence"]["failure_evidence"]["supervision_windows"] == [windows["ticket:2"]]
    assert kernel.control("resume", "only-original-ticket", subject="ticket:2") == "applied"
    remaining = read_only(kernel, tmp_path).data["supervision_windows"]
    assert remaining == [windows["ticket:3"]]


def test_required_checks_projection_keeps_the_original_45_minute_window(tmp_path, monkeypatch):
    source_run(tmp_path, monkeypatch)
    scenario = Scenario(tmp_path / "scenario", DeliveryKind.TICKET, checks=("pending",))
    scenario.kernel.clock = lambda: 100
    assert scenario.run().reason == "required_checks"
    monkeypatch.setattr(query.time, "time", lambda: 340.5)
    window = read_only(scenario.kernel, tmp_path).data["supervision_windows"][0]
    assert window["kind"] == "required_checks"
    assert window["deadline"] == 2800 and window["remaining_seconds"] == 2459
    assert not window["timed_out"]
    assert window["timeout_resume_action"] == "agent-run resume 1 --repo example/repository --ticket 2"
