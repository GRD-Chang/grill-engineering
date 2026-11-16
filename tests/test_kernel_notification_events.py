"""Pure native event projection uses real committed Core producers."""
from pathlib import Path

import pytest

from agent_run.model import AgentCall, Commit, Delivery, DeliveryKind, Issue, ParentIssue, Requirements, Role, RunSpec, Step
from agent_run.core.kernel import Kernel
from agent_run.errors import KernelError
from agent_run.model import wire_json
from support.offline import GraphScenario, Scenario
from support.run_scoped_kernel import domain_kernel, domain_scenario
from support.notification import recorded_agents as _recorded_agents, _project


def _activity_kernel(root: Path, kind: DeliveryKind = DeliveryKind.PARENT_ONLY):
    """Real allocation/activity facts; these tests never need a Git effect."""
    from support.core_operations import native_agent_fixture

    base = Commit("base", "tree")
    delivery = Delivery("activity", kind, 2 if kind == DeliveryKind.TICKET else 1,
                        "requirement", base, "refs/heads/source", "refs/heads/target")
    return native_agent_fixture(root / "kernel.sqlite3", RunSpec(
        "activity-run", "example/project", 1, "parent", "graph", (delivery,)))


@pytest.mark.parametrize("kind", [DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY])
def test_bootstrap_binding_is_not_an_actual_start_and_first_activity_is_unique(tmp_path, kind):
    from support.core_operations import CoreOperations

    kernel, ports = _activity_kernel(tmp_path, kind)
    operations = CoreOperations(kernel, kernel.claim_executor("activity-owner"), lambda _binding: ports)
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
    from agent_run.core.operation_runner import observe_control
    from support.core_operations import CoreOperations

    kernel, ports = _activity_kernel(tmp_path)
    execution = kernel.claim_executor("activity-owner")
    now = [100.0]
    kernel.clock = lambda: now[0]
    if old_observer:
        record = kernel._event

        def without_observer_trigger(tx, subject, kind, identity, material=None, **kwargs):
            if kind != "worker_active":
                record(tx, subject, kind, identity, material, **kwargs)

        monkeypatch.setattr(kernel, "_event", without_observer_trigger)
    operations = CoreOperations(kernel, execution, lambda _binding: ports)
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
        kernel.confirm_executor_exit(execution)
    now[0] = 102.0
    world = observe_control(kernel, first.subject, lambda _binding: ports)
    assert kernel.control("resume", "resume-original", subject=scope, world=world) == "applied"
    if whole_run:
        execution = kernel.claim_executor("recovered-owner")
    operations = CoreOperations(kernel, execution, lambda _binding: ports)
    resumed = operations.until(lambda step: step.call is not None).call
    assert resumed is not None and resumed.attempt_id == first.attempt_id
    assert resumed.invocation_id != first.invocation_id and kernel.dispatch_agent(resumed)
    operations.runner.authorize_agent(resumed)
    replacement = dict(process, start_token="resumed-bootstrap")
    kernel.agent_started(resumed, replacement)
    assert not any(event.get("resume_id") == "resume-original" for event in _project(kernel, "concise"))
    waiting = read(kernel.storage.path, 'status', storage=kernel.storage).data["latest_resume"]["resume"]
    assert waiting["progress"] is None and waiting["started_at"] is None
    kernel.agent_source_ready(resumed)
    kernel.observe_worker_activity(resumed, "actual", active=True, observed_at=102.0,
                                   evidence={"type": "turn.started"})
    events = [event for event in _project(kernel, "concise") if event["kind"] == "parent_started"]
    assert len(events) == 1 and events[0]["resume_id"] == "resume-original"
    assert events[0]["invocation_id"] == resumed.invocation_id
    progress = read(kernel.storage.path, 'status', storage=kernel.storage).data["latest_resume"]["resume"]["progress"]
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
    from agent_run.queries.run import read
    from agent_run.core.operation_runner import observe_control
    from support.core_operations import CoreOperations

    kernel, ports = _activity_kernel(tmp_path)
    execution = kernel.claim_executor("activity-owner")
    now = [100.0]
    kernel.clock = lambda: now[0]
    if old_observer:
        record = kernel._event

        def without_observer_trigger(tx, subject, kind, identity, material=None, **kwargs):
            if kind != "worker_active":
                record(tx, subject, kind, identity, material, **kwargs)

        monkeypatch.setattr(kernel, "_event", without_observer_trigger)
    operations = CoreOperations(kernel, execution, lambda _binding: ports)
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
                              world=observe_control(kernel, first.subject, lambda _binding: ports)) == "applied"
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
        if resumed and old_observer and offset == -1:
            # One public CLI contract; the rule matrix uses its real query directly.
            assert main(["status", kernel.storage.run_id, "--state-dir", str(kernel.storage.path), "--json"]) == 0
            data = json.loads(capsys.readouterr().out)
        else:
            data = read(kernel.storage.path, "status", storage=kernel.storage).data
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
    assert read(kernel.storage.path, 'status', storage=kernel.storage).data["latest_resume"]["resume"]["progress"] is None
    kernel.agent_source_ready(resumed)
    now[0] = 104.0
    kernel.observe_worker_activity(resumed, "old-resumed-turn", active=True, observed_at=now[0], evidence={"type": "turn.started"})
    projected = _project(kernel, "concise")
    assert sum(event["kind"] == "parent_started" for event in projected) == 1
    resumed_event, = [event for event in projected if event.get("resume_id") == "old-resume"]
    assert resumed_event["invocation_id"] == resumed.invocation_id
    view = read(kernel.storage.path, 'status', storage=kernel.storage).data["latest_resume"]["resume"]
    assert view["started_at"] == 104.0 and view["progress"]["resume_outcome"] == "started"
    kernel.agent_exited(resumed, replacement, 1, failure="Controlled fixture finished", failure_kind="fatal")


def test_completed_ticket_effort_spans_budget_renewal_without_recounting_resume(tmp_path, monkeypatch):
    import json
    from agent_run.model import Policy
    from agent_run.notifications.cards import card
    from agent_run.queries.run import read

    from support.notification import _enabled_scenario
    from support.status_comments import assert_completed_ticket_sender

    scenario = _enabled_scenario(tmp_path, monkeypatch, DeliveryKind.TICKET,
        policy=Policy(ticket_ci_developments=1, ticket_ci_review=True))
    subject = scenario.delivery.key
    native = scenario.kernel
    with domain_kernel(native) as scenario.kernel:
        scenario.checks = ["fail", "fail", "none"]
        now, calls = [100.0], []
        scenario.kernel.clock = lambda: now[0]

        def started(call):
            calls.append(call)
            seconds = {Role.DEVELOPMENT: 2, Role.REVIEW: 3, Role.PUBLICATION: 5}[call.role]
            scenario.kernel.observe_worker_activity(call, "start", active=True, observed_at=now[0] + 1,
                                                    evidence={"type": "turn.started"})
            scenario.kernel.observe_worker_activity(call, "end", active=False, observed_at=now[0] + 1 + seconds,
                                                    evidence={"type": "turn.completed"})
            now[0] += seconds + 2

        _recorded_agents(scenario, monkeypatch, started=started, activity=False)
        assert scenario.run().kind == "paused"
        before = scenario.kernel.status(subject)["budgets"]
        assert (before["ci"]["window"], before["ci"]["developments_used"], before["ci"]["reviews_used"]) == (1, 1, 1)
        prior_attempts = {call.attempt_id for call in calls}
        now[0] += 100  # Human waiting is not Agent execution.
        assert scenario.kernel.control("resume", "renew-ci-once", subject=subject) == "applied"
        assert scenario.kernel.control("resume", "renew-ci-once", subject=subject) == "applied"
        renewed = scenario.kernel.status(subject)["budgets"]
        assert (renewed["ci"]["window"], renewed["ci"]["developments_used"], renewed["ci"]["reviews_used"]) == (2, 0, 1)
        assert renewed["requirements"] == before["requirements"]
        assert scenario.run().kind == "complete"
        assert scenario.kernel.status(subject)["resource_cleanup"]["state"] == "complete"
        assert scenario.port.objects.issue(scenario.delivery.issue)["state"] == "closed"
        projected = _project(scenario.kernel, "concise")
        event, = [item for item in projected if item["kind"] == "ticket_completed"]
        attempts = read(scenario.kernel.storage.path, 'history', storage=scenario.kernel.storage).data["history"]["attempts"]
        assert prior_attempts <= {attempt["id"] for attempt in attempts}
        for role, seconds in ((Role.DEVELOPMENT, 6), (Role.REVIEW, 9)):
            actual = {call.attempt_id for call in calls if call.role == role}
            assert len(actual) == event["effort"][role.value]["rounds"] == 3
            assert actual == {attempt["id"] for attempt in attempts if attempt["role"] == role.value}
            assert event["effort"][role.value]["execution_seconds"] == seconds
        assert sum(call.role == Role.PUBLICATION for call in calls) == 3
        assert event["total_seconds"] == 30 and event["execution_timing_complete"] is True
        for language, count in (("zh", "3 轮"), ("en", "3 rounds")):
            rendered = json.dumps(card({**event, "language": language}), ensure_ascii=False)
            assert rendered.count(count) == 2
        assert _project(scenario.kernel, "concise") == projected

    scenario.kernel = native  # The actual sender owns a different thread/SQLite reader.
    assert_completed_ticket_sender(scenario, summary="Changed synthetic behavior 3", completed=event)


@pytest.mark.parametrize("role", [Role.DEVELOPMENT, Role.REVIEW, Role.PUBLICATION])
def test_human_result_uses_original_paused_material_even_after_resume(tmp_path, monkeypatch, role):
    from agent_run.core.operation_runner import observe_control
    from support.review_facts import requirement_review
    from support.worker_facts import start_worker, finish_worker
    from support.offline import review_result

    with requirement_review(tmp_path, kind=DeliveryKind.TICKET) as (kernel, operations):
        reason = "Original human decision / 保留原文"
        for expected in (Role.DEVELOPMENT, Role.REVIEW, Role.PUBLICATION):
            call = operations.until(lambda step: step.call is not None).call
            assert call.role == expected
            process = start_worker(kernel, operations, call)
            if expected == role:
                if role == Role.REVIEW:
                    result = review_result("blocked")
                    result["checks"]["spec"]["evidence"] = reason
                else:
                    result = {"result_kind": "human_blocker", "human_blockers": [reason],
                              **({"summary": None} if role == Role.DEVELOPMENT else
                                 {"commit_message": None, "pr_title": None, "pr_body_markdown": None})}
                finish_worker(kernel, call, process, wire_json(result))
                break
            finish_worker(kernel, call, process, wire_json(review_result("pass")) if expected == Role.REVIEW else None)
        assert not any(event["kind"] == "stage_end" and event.get("attempt_id") == call.attempt_id
                       for event in _project(kernel, "detailed"))
        assert operations.once().kind == "paused"
        results = [event for event in _project(kernel, "detailed")
                   if event["kind"] == "stage_end" and event.get("attempt_id") == call.attempt_id]
        assert len(results) == 1 and reason in results[0]["summary"]
        assert results[0]["status_code"] == ("review_blocked" if role == Role.REVIEW else "human_blocker")
        assert results[0]["color"] == "yellow" and results[0]["next_step"]
        assert not any(event["kind"] == "stage_end" for event in _project(kernel, "concise"))
        kernel.control("resume", "answer-original", subject=call.subject,
                                message="Use the original decision", world=observe_control(kernel, call.subject, lambda _: operations.runner.ports))
        assert [event for event in _project(kernel, "detailed") if event["id"] == results[0]["id"]] == results


def test_human_revision_after_a_new_generation_is_not_the_old_automatic_repair(monkeypatch, notification_failed_final_review):
    from dataclasses import replace

    scenario, _ = notification_failed_final_review
    assert scenario.kernel.status("ticket:2")["phase"] == "complete"
    assert len(scenario.kernel.source_status()["completions"]) == 1
    start_views = {}

    def started(call):
        if call.subject == "final:1" and call.role == Role.DEVELOPMENT:
            start_views[call.attempt_id] = _project(scenario.kernel, "detailed")

    _recorded_agents(scenario, monkeypatch, started=started)
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
