"""Pure native event projection uses real committed Core producers."""
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_run.model import AgentCall, DeliveryKind, Issue, ParentIssue, Requirements, Role, Step
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import wire_json
from agent_run.notifications.event_projection import project_event
from support.offline import GraphScenario, Scenario
from support.notification import recorded_agents as _recorded_agents


@pytest.mark.parametrize("kind", [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_bootstrap_binding_is_not_an_actual_start_and_first_activity_is_unique(tmp_path, kind):
    from support.core_operations import CoreOperations

    scenario = Scenario(tmp_path, kind)
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    step = operations.until(lambda step: step.call is not None)
    call = step.call
    assert call is not None and kernel.dispatch_agent(call)
    operations.runner.authorize_agent(call)
    process = {"pid": 21, "pgid": 21, "start_token": "bootstrap-only",
               "host_pid": 10, "host_start_token": "synthetic-host"}
    kernel.agent_started(call, process)  # Safety binding before source/GO, not backend execution.
    for mode in ("concise", "detailed"):
        assert _project(kernel, mode) == []
    kernel.observe_worker_activity(call, "inactive", active=False, observed_at=100.0,
                                   evidence={"type": "turn.completed"})
    assert _project(kernel, "concise") == []
    kernel.agent_source_ready(call)
    kernel.observe_worker_activity(call, "active", active=True, observed_at=101.0,
                                   evidence={"type": "turn.started"})
    first = _project(kernel, "concise")
    assert [event["kind"] for event in first] == [
        "ticket_started" if kind == DeliveryKind.TICKET else "parent_started"]
    assert first[0]["attempt_id"] == call.attempt_id
    assert first[0]["started_at"] == "1970-01-01T00:01:41+00:00"
    kernel.observe_worker_activity(call, "active", active=True, observed_at=101.0,
                                   evidence={"type": "turn.started"})
    kernel.observe_worker_activity(call, "another-turn", active=True, observed_at=102.0,
                                   evidence={"type": "turn.started"})
    assert _project(kernel, "concise") == first
    with pytest.raises(KernelError, match="conflicting_activity_observation"):
        kernel.observe_worker_activity(call, "active", active=False, observed_at=101.0,
                                       evidence={"type": "turn.started"})
    assert _project(kernel, "concise") == first
    kernel.agent_exited(call, process, 1, failure="Controlled fixture finished", failure_kind="fatal")
    assert kernel.worker_settled(call)


def test_pre_go_source_rejection_reaps_its_real_bootstrap_without_start_notification(tmp_path):
    import os
    import sys
    from agent_run.runtime.execution import execute
    from support.core_operations import CoreOperations

    scenario = Scenario(tmp_path / "scenario", DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    step = operations.until(lambda step: step.call is not None)
    call = step.call
    assert call is not None
    canary = tmp_path / "argv-was-executed"
    reads = 0

    def authorize():
        nonlocal reads
        reads += 1
        operations.runner.authorize_agent(call)
        if reads == 2:
            raise KernelError("controlled_second_source_rejection")

    with pytest.raises(KernelError, match="controlled_second_source_rejection"):
        execute(kernel, call, arguments=(sys.executable, "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('executed')", str(canary)),
            cwd=Path(call.context["checkout"]), prompt="", environment=os.environ, timeout=5,
            authorize_source=authorize)
    assert reads == 2 and not canary.exists()
    assert kernel.worker_settled(call)
    assert not any(event["kind"] in {"parent_started", "stage_start", "resume"}
                   for event in _project(kernel, "detailed"))
    assert all(budget["developments_used"] == 0 for budget in kernel.status(call.subject)["budgets"].values())


@pytest.mark.parametrize("whole_run", [False, True])
@pytest.mark.parametrize("old_observer", [False, True])
def test_late_stopped_activity_does_not_take_the_first_start_or_resume_milestone(tmp_path, monkeypatch, whole_run, old_observer):
    from agent_run.queries.run import read
    from support.core_operations import CoreOperations

    scenario = Scenario(tmp_path, DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    now = [100.0]
    kernel.clock = lambda: now[0]
    if old_observer:
        record = kernel._event

        def without_observer_trigger(tx, subject, kind, identity, material=None, **kwargs):
            if kind != "worker_active":
                record(tx, subject, kind, identity, material, **kwargs)

        monkeypatch.setattr(kernel, "_event", without_observer_trigger)
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    first = operations.until(lambda step: step.call is not None).call
    assert first is not None and kernel.dispatch_agent(first)
    operations.runner.authorize_agent(first)
    kernel.bind_thread(first, "original-backend")
    process = {"pid": 21, "pgid": 21, "start_token": "original-bootstrap",
               "host_pid": 10, "host_start_token": "synthetic-host"}
    kernel.agent_started(first, process)
    kernel.agent_source_ready(first)
    scope = None if whole_run else first.subject
    now[0] = 101.0
    assert kernel.control("stop", "stop-before-activity", subject=scope) == "applied"
    kernel.observe_worker_activity(first, "late", active=True, observed_at=101.0,
                                   evidence={"type": "turn.started", "source": "late-backend"})
    assert not any(event["kind"] == "parent_started" for event in _project(kernel, "concise"))
    kernel.agent_exited(first, process, 1, failure="Controlled stopped process", failure_kind="fatal")
    if whole_run:
        kernel.confirm_executor_exit(scenario.execution)
    now[0] = 102.0
    assert kernel.control("resume", "resume-original", subject=scope, world=scenario.world()) == "applied"
    if whole_run:
        scenario.execution = kernel.claim_executor("recovered-owner")
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    resumed = operations.until(lambda step: step.call is not None).call
    assert resumed is not None and resumed.attempt_id == first.attempt_id
    assert resumed.invocation_id != first.invocation_id and kernel.dispatch_agent(resumed)
    operations.runner.authorize_agent(resumed)
    replacement = dict(process, start_token="resumed-bootstrap")
    kernel.agent_started(resumed, replacement)
    assert not any(event.get("resume_id") == "resume-original" for event in _project(kernel, "concise"))
    waiting = read(kernel.storage.path, "status").data["latest_resume"]["resume"]
    assert waiting["progress"] is None and waiting["started_at"] is None
    kernel.agent_source_ready(resumed)
    kernel.observe_worker_activity(resumed, "actual", active=True, observed_at=102.0,
                                   evidence={"type": "turn.started"})
    events = [event for event in _project(kernel, "concise") if event["kind"] == "parent_started"]
    assert len(events) == 1 and events[0]["resume_id"] == "resume-original"
    assert events[0]["invocation_id"] == resumed.invocation_id
    progress = read(kernel.storage.path, "status").data["latest_resume"]["resume"]["progress"]
    assert progress["evidence"] == ("worker_started" if old_observer else "worker_active")
    assert progress["invocation_id"] == resumed.invocation_id
    # An old owner's newly arriving turn cannot replace the current milestone.
    kernel.observe_worker_activity(first, "later-old-turn", active=True, observed_at=103.0,
                                   evidence={"type": "turn.started", "source": "late-backend"})
    assert [event for event in _project(kernel, "concise") if event["kind"] == "parent_started"] == events
    kernel.agent_exited(resumed, replacement, 1, failure="Controlled fixture finished", failure_kind="fatal")
    assert kernel.worker_settled(first) and kernel.worker_settled(resumed)


@pytest.mark.parametrize("resumed", [False, True], ids=["first", "resumed"])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["before", "at", "after"])
@pytest.mark.parametrize("old_observer", [False, True], ids=["current", "v21-no-trigger"])
def test_activity_start_respects_the_original_invocation_deadline(
    tmp_path, monkeypatch, capsys, resumed, offset, old_observer,
):
    import json
    from agent_run.cli.main import main
    from support.core_operations import CoreOperations

    scenario = Scenario(tmp_path, DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    now = [100.0]
    kernel.clock = lambda: now[0]
    if old_observer:
        record = kernel._event

        def without_observer_trigger(tx, subject, kind, identity, material=None, **kwargs):
            if kind != "worker_active":
                record(tx, subject, kind, identity, material, **kwargs)

        monkeypatch.setattr(kernel, "_event", without_observer_trigger)
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    first = operations.until(lambda step: step.call is not None).call
    assert first is not None and kernel.dispatch_agent(first)
    operations.runner.authorize_agent(first)
    kernel.bind_thread(first, "deadline-backend")
    process = {"pid": 21, "pgid": 21, "start_token": "first-bootstrap",
               "host_pid": 10, "host_start_token": "synthetic-host"}
    kernel.agent_started(first, process)
    kernel.agent_source_ready(first)
    call = first
    if resumed:
        now[0] = 101.0
        assert kernel.control("stop", "stop-before-deadline-activity", subject=first.subject) == "applied"
        kernel.agent_exited(first, process, 1, failure="Controlled stopped fixture", failure_kind="fatal")
        now[0] = 103.0
        assert kernel.control("resume", "resume-deadline", subject=first.subject,
                              world=scenario.world()) == "applied"
        call = operations.until(lambda step: step.call is not None).call
        assert call is not None and call.attempt_id == first.attempt_id
        assert call.invocation_id != first.invocation_id and call.thread_id == first.thread_id
        assert kernel.dispatch_agent(call)
        operations.runner.authorize_agent(call)
        process = dict(process, start_token="resumed-bootstrap")
        kernel.agent_started(call, process)
        kernel.agent_source_ready(call)
    deadline = call.deadline_at
    charged = kernel.status(call.subject)["budgets"]["requirements"]["developments_used"]
    assert charged == 1
    now[0] = deadline + offset
    kernel.observe_worker_activity(call, "deadline-activity", active=True, observed_at=now[0],
                                   evidence={"type": "turn.started"})
    try:
        events = [event for event in _project(kernel, "concise") if event["kind"] == "parent_started"]
        # Reading later must preserve a real in-deadline start, without granting
        # an expired observation a fresh interval or another Development charge.
        now[0] = deadline + 2
        assert main(["status", scenario.spec.run_id, "--state-dir", str(kernel.storage.path), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        if resumed:
            facts = data["latest_resume"]["resume"]
            assert (facts["progress"] is not None) is (offset < 0)
            assert (facts["started_at"] is not None) is (offset < 0)
            if offset < 0:
                assert facts["progress"]["resume_outcome"] == "started"
                assert facts["progress"]["invocation_id"] == call.invocation_id
        assert len(events) == (1 if offset < 0 else 0)
        assert kernel.status(call.subject)["budgets"]["requirements"]["developments_used"] == charged
        with kernel.storage.transaction(write=False) as tx:
            assert tx.one("SELECT deadline_at FROM invocations WHERE id=?", (call.invocation_id,))[0] == deadline
    finally:
        kernel.agent_exited(call, process, 1, failure="Controlled deadline fixture finished", failure_kind="fatal")
    assert kernel.worker_settled(call)


def test_stopped_repair_activity_is_not_presented_as_still_active(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path, DeliveryKind.FINAL_RUN, reviews=("fail", "pass"))
    stopped = []

    def started(call):
        if call.role != Role.DEVELOPMENT:
            return
        current = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "acceptance_repair"][-1]
        assert current["live"] is True
        assert scenario.kernel.control("stop", "stop-current-repair", subject=call.subject) == "applied"
        waiting = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "acceptance_repair"][-1]
        assert waiting["id"] == current["id"] and waiting["live"] is False
        stopped.append(call.attempt_id)

    _recorded_agents(scenario, monkeypatch, started=started)
    assert scenario.run().kind == "paused"
    assert len(stopped) == 1


def test_same_protocol_activity_without_observer_trigger_keeps_real_start_and_resume(tmp_path, monkeypatch):
    from agent_run.queries.run import read
    from support.core_operations import CoreOperations

    scenario = Scenario(tmp_path, DeliveryKind.PARENT_ONLY)
    kernel = scenario.kernel
    now = [100.0]
    kernel.clock = lambda: now[0]
    record = kernel._event

    def old_observer(tx, subject, kind, identity, material=None, **kwargs):
        # Reproduce the old v21 observer representation only: Core still owns
        # every business transition and the real worker_activity producer.
        if kind != "worker_active":
            record(tx, subject, kind, identity, material, **kwargs)

    monkeypatch.setattr(kernel, "_event", old_observer)
    operations = CoreOperations(kernel, scenario.execution, scenario.make_ports)
    first = operations.until(lambda step: step.call is not None).call
    assert first is not None and kernel.dispatch_agent(first)
    operations.runner.authorize_agent(first)
    kernel.bind_thread(first, "old-v21-backend")
    process = {"pid": 21, "pgid": 21, "start_token": "old-output",
               "host_pid": 10, "host_start_token": "synthetic-host"}
    kernel.agent_started(first, process)
    assert _project(kernel, "concise") == []
    kernel.agent_source_ready(first)
    now[0] = 101.0
    kernel.observe_worker_activity(first, "old-turn", active=True, observed_at=now[0], evidence={"type": "turn.started"})
    started, = _project(kernel, "concise")
    assert started["kind"] == "parent_started" and started["started_at"] == "1970-01-01T00:01:41+00:00"
    now[0] = 102.0
    assert kernel.control("stop", "old-stop", subject=first.subject) == "applied"
    kernel.agent_exited(first, process, 1, failure="Controlled old output exit", failure_kind="fatal")
    now[0] = 103.0
    assert kernel.control("resume", "old-resume", subject=first.subject, world=scenario.world()) == "applied"
    resumed = operations.until(lambda step: step.call is not None).call
    assert resumed is not None and kernel.dispatch_agent(resumed)
    operations.runner.authorize_agent(resumed)
    replacement = dict(process, start_token="old-resumed-output")
    kernel.agent_started(resumed, replacement)
    assert read(kernel.storage.path, "status").data["latest_resume"]["resume"]["progress"] is None
    kernel.agent_source_ready(resumed)
    now[0] = 104.0
    kernel.observe_worker_activity(resumed, "old-resumed-turn", active=True, observed_at=now[0], evidence={"type": "turn.started"})
    projected = _project(kernel, "concise")
    assert sum(event["kind"] == "parent_started" for event in projected) == 1
    resumed_event, = [event for event in projected if event.get("resume_id") == "old-resume"]
    assert resumed_event["invocation_id"] == resumed.invocation_id
    view = read(kernel.storage.path, "status").data["latest_resume"]["resume"]
    assert view["started_at"] == 104.0 and view["progress"]["resume_outcome"] == "started"
    kernel.agent_exited(resumed, replacement, 1, failure="Controlled fixture finished", failure_kind="fatal")


def _project(kernel: Kernel, mode: str) -> list[dict[str, Any]]:
    with kernel.storage.transaction(write=False) as tx:
        run = tx.one("SELECT id,repository,parent_issue,parent_revision,language,created_at FROM runs")
        title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                        (run["parent_issue"], run["parent_revision"]))
        state = {"run_id": run["id"], "repository": run["repository"],
                 "parent": {"number": run["parent_issue"], "title": title[0][0] if title else ""},
                 "language": run["language"], "created_at": run["created_at"]}
        frozen = deepcopy(state)
        result = [projected.content for row in tx.all("SELECT * FROM history ORDER BY position")
                  for projected in project_event(tx, dict(row), state, mode) if projected.visible]
        assert state == frozen
        return result


@pytest.mark.parametrize("mode", ["detailed", "concise"])
def test_role_events_require_real_start_and_consumption_and_do_not_duplicate_first_start(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    _recorded_agents(scenario, monkeypatch)
    consumed: list[str] = []
    for _ in range(100):
        step = scenario.step()
        if step.kind == "agent":
            # The explicit synthetic Agent seam recorded dispatch/start/raw/exit.
            # The next real Core advance has not consumed the result.
            assert step.call is not None
            before = _project(scenario.kernel, mode)
            assert sum(event["kind"] == "ticket_started" for event in before) == 1
            assert not any(event["kind"] == "stage_start" and event.get("role") == "development" for event in before)
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == step.call.attempt_id for event in before)
            assert scenario.step().kind == "progress"
            after = _project(scenario.kernel, mode)
            ended = [event for event in after if event["kind"] == "stage_end" and event.get("attempt_id") == step.call.attempt_id]
            assert len(ended) == (1 if mode == "detailed" else 0)
            if ended:
                assert ended[0]["role"] == step.call.role.value
                consumed.append(ended[0]["role"])
                if step.call.role.value == "publication":
                    assert ended[0]["status_code"] == "publication_artifact"
                    assert not any(event["kind"] == "pr_created" for event in after)
        else:
            assert step.kind not in {"paused", "waiting"}, step
        # Stop sampling this Ticket only after its actual Completion/cleanup.
        # The next global operation may legitimately admit Final; no global
        # quiescence or subject-selected transition is manufactured here.
        ticket = scenario.kernel.status("ticket:2")
        if (ticket["phase"] == "complete" and ticket["resource_cleanup"] is not None
                and ticket["resource_cleanup"]["state"] == "complete"):
            break
    else:
        raise AssertionError("The real Ticket pipeline did not finish")
    projected = _project(scenario.kernel, mode)
    assert sum(event["kind"] == "ticket_started" for event in projected) == 1
    assert sum(event["kind"] == "ticket_completed" for event in projected) == 1
    assert sum(event["kind"] == "pr_created" for event in projected) == (1 if mode == "detailed" else 0)
    assert consumed == (["development", "review", "publication"] if mode == "detailed" else [])
    assert [event["role"] for event in projected if event["kind"] == "stage_start"] == (
        ["review", "publication"] if mode == "detailed" else [])
    assert _project(scenario.kernel, mode) == projected


@pytest.mark.parametrize("mode", ["detailed", "concise"])
def test_full_run_first_review_and_repair_replace_corresponding_stage_events(tmp_path, monkeypatch, mode):
    from agent_run.notifications.envelope import event_copy

    # d847's Final fixture retains a real historical Ticket and Completion.
    # Its prefix has already passed Review; this script begins at Final Review.
    scenario = Scenario(tmp_path, DeliveryKind.FINAL_RUN, reviews=("fail", "pass"))
    assert scenario.kernel.status("ticket:2")["phase"] == "complete"
    assert len(scenario.kernel.source_status()["completions"]) == 1
    start_views = {}

    def started(call):
        if call.subject == "final:1" and call.role == Role.DEVELOPMENT:
            start_views[call.attempt_id] = _project(scenario.kernel, mode)

    _recorded_agents(scenario, monkeypatch, started=started)
    first_review = repair = None
    planned_id = None
    for _ in range(100):
        step = scenario.step()
        projected = _project(scenario.kernel, mode)
        if step.call is not None and step.call.role == Role.REVIEW and first_review is None:
            first_review = step.call.attempt_id
            started = [event for event in projected if event["kind"] == "acceptance_started"]
            assert len(started) == 1 and started[0]["attempt_id"] == first_review
            assert not any(event["kind"] == "stage_start" and event.get("attempt_id") == first_review for event in projected)
            assert not any(event["kind"] == "acceptance_repair" for event in projected)
            assert scenario.step().kind == "progress"
            planned = [event for event in _project(scenario.kernel, mode) if event["kind"] == "acceptance_repair"]
            assert len(planned) == 1 and planned[0]["live"] is False
            assert planned[0]["title"] == event_copy({"language": "zh"}, "acceptance_repair_planned")
            planned_id = planned[0]["id"]
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == first_review
                           for event in _project(scenario.kernel, mode))
        elif step.call is not None and step.call.role == Role.DEVELOPMENT:
            repair = step.call.attempt_id
            milestones = [event for event in start_views[repair] if event["kind"] == "acceptance_repair"]
            assert milestones[-1]["live"] is True
            assert milestones[-1]["title"] == event_copy({"language": "zh"}, "acceptance_repair_active")
            assert {event["id"] for event in milestones} == {planned_id}
            assert not any(event["kind"] == "stage_start" and event.get("attempt_id") == repair for event in projected)
        promoted = [event for event in projected if event["kind"] == "acceptance_passed"]
        if promoted:
            assert first_review is not None and repair is not None
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == promoted[0]["attempt_id"]
                           for event in projected)
            break
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("FullRun did not promote its actual repair Review")


@pytest.mark.parametrize("role", [Role.DEVELOPMENT, Role.REVIEW, Role.PUBLICATION])
def test_human_result_uses_original_paused_material_even_after_resume(tmp_path, monkeypatch, role):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    reason = "Original human decision / 保留原文"
    original = scenario.agent

    def result(call):
        raw = original(call)
        if call.role != role:
            return raw
        if role == Role.REVIEW:
            return wire_json({"checks": {lane: {"status": "blocked" if lane == "e2e" else "pass",
                                                "evidence": reason if lane == "e2e" else "Passed exact lane",
                                                "findings": []} for lane in ("e2e", "standards", "spec")}})
        return wire_json({"result_kind": "human_blocker", "human_blockers": [reason],
                          **({"summary": None} if role == Role.DEVELOPMENT else
                             {"commit_message": None, "pr_title": None, "pr_body_markdown": None})})

    _recorded_agents(scenario, monkeypatch, result)
    for _ in range(100):
        step = scenario.step()
        if step.call is not None and step.call.role == role:
            call = step.call
            assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == call.attempt_id
                           for event in _project(scenario.kernel, "detailed"))
            assert scenario.step().kind == "paused"
            break
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("The selected role never returned its human artifact")
    results = [event for event in _project(scenario.kernel, "detailed")
               if event["kind"] == "stage_end" and event.get("attempt_id") == call.attempt_id]
    assert len(results) == 1 and reason in results[0]["summary"]
    assert results[0]["status_code"] == ("review_blocked" if role == Role.REVIEW else "human_blocker")
    assert results[0]["color"] == "yellow" and results[0]["next_step"]
    assert not any(event["kind"] == "stage_end" for event in _project(scenario.kernel, "concise"))
    scenario.kernel.control("resume", "answer-original", subject=call.subject,
                            message="Use the original decision", world=scenario.world())
    assert [event for event in _project(scenario.kernel, "detailed") if event["id"] == results[0]["id"]] == results


def test_human_revision_after_a_new_generation_is_not_the_old_automatic_repair(tmp_path, monkeypatch):
    from dataclasses import replace

    # d847's Final fixture retains a real historical Ticket and Completion.
    # Its prefix has already passed Review; this script begins at Final Review.
    scenario = Scenario(tmp_path, DeliveryKind.FINAL_RUN, reviews=("fail", "pass"))
    assert scenario.kernel.status("ticket:2")["phase"] == "complete"
    assert len(scenario.kernel.source_status()["completions"]) == 1
    start_views = {}

    def started(call):
        if call.subject == "final:1" and call.role == Role.DEVELOPMENT:
            start_views[call.attempt_id] = _project(scenario.kernel, "detailed")

    _recorded_agents(scenario, monkeypatch, started=started)
    for _ in range(100):
        step = scenario.step()
        if step.call is not None and step.call.role == Role.REVIEW:
            assert scenario.step().kind == "progress"
            break
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("The first overall Review did not return its failure")
    original_generation = scenario.kernel.status("final:1")["generation"]
    planned = [event for event in _project(scenario.kernel, "detailed") if event["kind"] == "acceptance_repair"]
    assert len(planned) == 1 and planned[0]["live"] is False
    scenario.requirements = replace(scenario.requirements, parent=replace(
        scenario.requirements.parent, body="A revised whole-Run requirement before automatic repair started"))
    fresh = scenario.world()
    assert scenario.kernel.status("final:1")["phase"] == "refresh_final"
    # A new Parent requirement needs the same explicit lifecycle action as the
    # formal entry; automatic repair must not accept it on the maintainer's behalf.
    assert scenario.kernel.control("requeue", "accept-revised-whole-run-requirement",
                                   subject="final:1", world=fresh) == "applied"
    refreshed = scenario.run()
    assert refreshed.kind == "approval", refreshed
    assert scenario.kernel.status("final:1")["generation"] == original_generation + 1
    assert scenario.kernel.control("revise", "human-revision-after-refresh", subject="final:1",
                                   world=scenario.world(), message="A separate maintainer revision") == "applied"
    step = scenario.step()
    assert step.call is not None and step.call.role == Role.DEVELOPMENT
    assert step.call.context["repair_source"] == "human_revision"
    current = start_views[step.call.attempt_id]
    assert any(event["kind"] == "stage_start" and event.get("attempt_id") == step.call.attempt_id for event in current)
    assert not any(event["kind"] == "acceptance_repair" and event["live"] for event in current)
