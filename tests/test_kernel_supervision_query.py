"""Public supervision reads the original scoped Core waits without authorizing them."""
from dataclasses import replace
from io import StringIO

import pytest

from agent_run_kernel import DeliveryKind, Kernel
from agent_run_kernel import query
from agent_run_kernel.offline import Scenario
from agent_run_kernel.query_render import render
from test_kernel_automatic_supervision import delivery_run
from test_kernel_query_cli import _snapshot


def source_run(tmp_path, monkeypatch, language="en"):
    original_create = Kernel.create

    def create(path, spec, **kwargs):
        return original_create(path, replace(spec, language=language, repository_root=str(tmp_path.resolve())), **kwargs)

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
    run_step = kernel.begin_automatic_step(execution)
    kernel.wait_for_read(run_step, execution, "final_source_missing", "Original Run ref outage")
    ticket_step = kernel.begin_automatic_step(execution, "ticket:2")
    kernel.wait_for_read(ticket_step, execution, "github_read_failed", "Original Ticket outage")
    monkeypatch.setattr(query.time, "time", lambda: 200.5)
    first = read_only(kernel, tmp_path).data
    assert len(first["supervision_windows"]) == 2
    assert first["supervision"]["subject"] is None
    for window in first["supervision_windows"]:
        assert window["started_at"] == 100.25 and window["deadline"] == 700.25
        assert window["remaining_seconds"] == 499 and window["retry_count"] == 1
        assert window["next_retry_at"] == 105.25 and not window["timed_out"]
        assert window["scope"]["subject"] == window["subject"]
    ticket = next(item for item in first["multi_ticket"]["tickets"] if item["ticket_number"] == 2)
    assert ticket["supervision"]["latest_observation"]["message"] == "Original Ticket outage"
    monkeypatch.setattr(query.time, "time", lambda: 900)
    expired_clock = read_only(kernel, tmp_path).data
    assert all(window["remaining_seconds"] == 0 and not window["timed_out"] for window in expired_clock["supervision_windows"])
    assert expired_clock["operator_actions"] == []
    assert expired_clock["next_action"] == "agent-run status 1 --repo example/repository"


@pytest.mark.parametrize("language", ["zh", "en"])
def test_run_timeout_renders_original_evidence_and_resumes_only_after_core_applies_it(tmp_path, monkeypatch, language):
    kernel = source_run(tmp_path, monkeypatch, language)
    execution = kernel.claim_executor("owner")
    kernel.clock = lambda: 100
    operation = kernel.begin_automatic_step(execution)
    kernel.wait_for_read(operation, execution, "final_source_missing", "Original failure / 原始问题")
    kernel.clock = lambda: 700
    assert kernel.guard_automatic_step(operation, execution).reason == "supervision_timeout"
    monkeypatch.setattr(query.time, "time", lambda: 900)
    document = read_only(kernel, tmp_path)
    data = document.data
    window = data["supervision"]
    command = "agent-run resume 1 --repo example/repository"
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
    assert history["supervision"] == window
    assert any(event["kind"] == "paused" and event["evidence"].get("scope_id") == window["scope_id"]
               for event in history["events"] if isinstance(event["evidence"], dict))
    assert kernel.control("stop", "retire-original-executor") == "applied"
    kernel.confirm_executor_exit(execution)
    assert kernel.control("resume", "explicit-original-resume") == "applied"
    assert read_only(kernel, tmp_path).data["supervision_windows"] == []
    replacement = kernel.claim_executor("replacement")
    fresh = kernel.begin_automatic_step(replacement)
    kernel.clock = lambda: 800
    kernel.wait_for_read(fresh, replacement, "final_source_missing", "Fresh actual failure")
    assert read_only(kernel, tmp_path).data["supervision"]["started_at"] == 800


def test_ticket_timeout_keeps_sibling_waits_and_exact_target_resume(tmp_path, monkeypatch):
    kernel = source_run(tmp_path, monkeypatch)
    execution = kernel.claim_executor("owner")
    kernel.clock = lambda: 100
    original = kernel.begin_automatic_step(execution, "ticket:2")
    kernel.wait_for_read(original, execution, "github_read_failed", "Original Ticket error")
    kernel.clock = lambda: 200
    sibling = kernel.begin_automatic_step(execution, "ticket:3")
    kernel.wait_for_read(sibling, execution, "github_read_failed", "Independent sibling")
    kernel.clock = lambda: 700
    assert kernel.guard_automatic_step(original, execution).reason == "supervision_timeout"
    monkeypatch.setattr(query.time, "time", lambda: 750)
    data = read_only(kernel, tmp_path).data
    windows = {window["subject"]: window for window in data["supervision_windows"]}
    assert windows["ticket:2"]["timed_out"] and not windows["ticket:3"]["timed_out"]
    assert data["supervision"] is None  # No guessed current window across independent Tickets.
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
    window = read_only(scenario.kernel, tmp_path).data["supervision"]
    assert window["kind"] == "required_checks"
    assert window["deadline"] == 2800 and window["remaining_seconds"] == 2459
    assert not window["timed_out"]
    assert window["timeout_resume_action"] == "agent-run resume 1 --repo example/repository --ticket 2"
