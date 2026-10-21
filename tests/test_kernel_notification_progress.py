"""Ordinary progress compression reads original Core facts without changing them."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run_kernel import Issue, ParentIssue, Requirements, Role
from agent_run_kernel.model import KernelError, wire_json
from agent_run.notification_event_facts import event_envelope
from agent_run_kernel.notification_progress import obsolete_progress
from agent_run_kernel.offline import GraphScenario
from test_kernel_notification_events import _project, _recorded_agents


def test_recovery_keeps_the_live_original_round_instead_of_its_finished_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    inspected = []

    def started(call):
        if call.role != Role.REVIEW:
            return
        pending = _project(scenario.kernel, "detailed")
        original = deepcopy(pending)
        finished = {event["id"] for event in pending
                    if event["kind"] in {"ticket_started", "stage_end"}}
        live = [event for event in pending if event["kind"] == "stage_start"]
        assert len(finished) == 2 and len(live) == 1
        assert live[0]["attempt_id"] == call.attempt_id
        before = scenario.kernel.storage.path.read_bytes()
        with scenario.kernel.storage.transaction(write=False) as tx:
            assert obsolete_progress(tx, pending, "detailed", current_boundary=False) == finished
        assert pending == original
        assert scenario.kernel.storage.path.read_bytes() == before
        inspected.append(call.attempt_id)

    _recorded_agents(scenario, monkeypatch, started=started)
    for _ in range(30):
        scenario.step()
        if inspected:
            break
    assert len(inspected) == 1


def test_unknown_origin_is_preserved_and_corrupt_original_material_pauses_recovery(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    inspected = []

    def started(call):
        event, = _project(scenario.kernel, "concise")
        unknown = dict(event, id="unknown-source", source_position=10**9)
        missing = {key: value for key, value in dict(event, id="missing-source").items()
                   if key != "source_material_id"}
        foreign = dict(event, id="foreign-attempt", attempt_id="not-the-original-attempt")
        with scenario.kernel.storage.transaction(write=False) as tx:
            assert obsolete_progress(tx, [event, unknown, missing, foreign], "concise", current_boundary=True) == {event["id"]}
            with pytest.raises(ValueError, match="limit"):
                obsolete_progress(tx, [event] * 33, "concise", current_boundary=False)
            original = tx.get(event["source_material_id"], call.subject)
        with scenario.kernel.storage.transaction() as tx:
            tx.execute("UPDATE material SET body=? WHERE id=?", (b"damaged original material", event["source_material_id"]))
        try:
            with scenario.kernel.storage.transaction(write=False) as tx:
                with pytest.raises(KernelError, match="material_corrupt"):
                    obsolete_progress(tx, [event], "concise", current_boundary=True)
        finally:
            with scenario.kernel.storage.transaction() as tx:
                tx.execute("UPDATE material SET body=? WHERE id=?", (original, event["source_material_id"]))
        inspected.append(call.attempt_id)

    _recorded_agents(scenario, monkeypatch, started=started)
    for _ in range(20):
        scenario.step()
        if inspected:
            break
    assert len(inspected) == 1


def test_original_run_start_is_current_only_before_any_actual_worker_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    with scenario.kernel.storage.transaction(write=False) as tx:
        run = dict(tx.one("SELECT * FROM runs"))
        start = event_envelope({"run_id": run["id"], "language": run["language"]},
                               "run_start", run["id"], "Original Run start")
        assert obsolete_progress(tx, [start], "concise", current_boundary=False) == set()
        assert obsolete_progress(tx, [start], "concise", current_boundary=True) == {start["id"]}
    inspected = []

    def started(call):
        pending = [start, *_project(scenario.kernel, "concise")]
        with scenario.kernel.storage.transaction(write=False) as tx:
            assert obsolete_progress(tx, pending, "concise", current_boundary=False) == {start["id"]}
        inspected.append(call.attempt_id)

    _recorded_agents(scenario, monkeypatch, started=started)
    for _ in range(20):
        scenario.step()
        if inspected:
            break
    assert len(inspected) == 1


def test_recovery_keeps_only_the_latest_actual_start_among_independent_live_tickets(tmp_path):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2, 3)),
        tuple(Issue(number, f"Ticket {number}", "Original task", "OPEN", labels=("ready-for-agent",))
              for number in (2, 3))))
    calls = []
    for subject in ("ticket:2", "ticket:3"):
        scenario.select(subject)
        for _ in range(20):
            step = scenario.kernel.advance(subject, scenario.execution, scenario.world())
            if step.call is not None:
                call = step.call
                assert scenario.kernel.dispatch_agent(call)
                scenario.kernel.agent_started(call, {"pid": 21 + len(calls), "pgid": 21 + len(calls),
                    "start_token": subject, "host_pid": 10, "host_start_token": "synthetic-host"})
                calls.append(call)
                break
            scenario.handle(step)
        else:
            raise AssertionError("Independent Ticket did not actually start")
    pending = _project(scenario.kernel, "concise")
    assert len(pending) == 2
    older = next(event for event in pending if event["attempt_id"] == calls[0].attempt_id)
    newer = next(event for event in pending if event["attempt_id"] == calls[1].attempt_id)
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [newer, older], "concise", current_boundary=False) == {older["id"]}
        assert obsolete_progress(tx, pending, "concise", current_boundary=True) == {older["id"], newer["id"]}


def test_worker_resume_uses_the_original_action_and_same_attempt_live_continuation(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    original = scenario.agent
    first = [True]
    resumed = []

    def result(call):
        if first[0]:
            first[0] = False
            return wire_json({"result_kind": "human_blocker", "summary": None,
                              "human_blockers": ["Original human decision"]})
        return original(call)

    def started(call):
        projected = _project(scenario.kernel, "concise")
        resume = [event for event in projected if event["kind"] == "resume"]
        if not resume:
            return
        event, = resume
        prior = next(event for event in projected if event["kind"] == "ticket_started")
        assert prior["attempt_id"] == event["attempt_id"] == call.attempt_id
        assert event["resume_id"] == "resume-original"
        with scenario.kernel.storage.transaction(write=False) as tx:
            assert obsolete_progress(tx, [prior, event], "concise", current_boundary=False) == {prior["id"]}
        resumed.append(event)

    _recorded_agents(scenario, monkeypatch, result=result, started=started)
    assert scenario.run().kind == "paused"
    assert scenario.kernel.control("resume", "resume-original", subject="ticket:2", message="Original decision",
                                   world=scenario.world()) == "applied"
    assert scenario.step().call is not None
    assert len(resumed) == 1
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, resumed, "concise", current_boundary=False) == {resumed[0]["id"]}


def test_consumed_controller_resume_is_a_historical_result_after_recovery(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)))
    _recorded_agents(scenario, monkeypatch)
    for _ in range(20):
        if scenario.step().call is not None:
            break
    assert scenario.step().kind == "progress"
    assert scenario.kernel.status("ticket:2")["phase"] == "candidate"
    assert scenario.kernel.control("stop", "stop-controller", subject="ticket:2") == "applied"
    assert scenario.kernel.control("resume", "resume-controller", subject="ticket:2", world=scenario.world()) == "applied"
    prepared = scenario.step()
    assert prepared.effect is not None and prepared.effect.operation == "candidate"
    assert scenario.step().kind == "progress"
    event, = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "resume"]
    assert event["evidence"] == "effect_consumed" and "attempt_id" not in event
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [event], "concise", current_boundary=False) == {event["id"]}


def test_original_repair_and_formal_promotion_follow_current_generation_and_approval_invalidation(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)),
        reviews=("pass", "fail", "pass"))
    active = []

    def started(call):
        if call.subject != "final:1" or call.role != Role.DEVELOPMENT:
            return
        repair = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "acceptance_repair"][-1]
        assert repair["live"] is True and repair["attempt_id"] == call.attempt_id
        with scenario.kernel.storage.transaction(write=False) as tx:
            assert obsolete_progress(tx, [repair], "concise", current_boundary=False) == set()
        active.append(repair)

    _recorded_agents(scenario, monkeypatch, started=started)
    assert scenario.run().kind == "complete"
    scenario.select(scenario.activate_final())
    planned = None
    for _ in range(100):
        step = scenario.step()
        projected = _project(scenario.kernel, "concise")
        repairs = [event for event in projected if event["kind"] == "acceptance_repair"]
        if repairs and planned is None:
            planned, = repairs
            assert planned["live"] is False
            with scenario.kernel.storage.transaction(write=False) as tx:
                assert obsolete_progress(tx, [planned], "concise", current_boundary=False) == set()
                assert obsolete_progress(tx, [planned], "concise", current_boundary=True) == {planned["id"]}
        promoted = [event for event in projected if event["kind"] == "acceptance_passed"]
        if promoted:
            promotion, = promoted
            assert planned is not None and active and active[0]["id"] == planned["id"]
            with scenario.kernel.storage.transaction(write=False) as tx:
                assert obsolete_progress(tx, [active[0], promotion], "concise", current_boundary=False) == {planned["id"]}
            break
        assert step.kind not in {"paused", "waiting", "approval"}, step
    else:
        raise AssertionError("The actual repair did not reach formal promotion")
    assert scenario.run().kind == "approval"
    scenario.port.external_remote_append(scenario.delivery.target_ref, {"external.txt": "New target\n"}, "approval-update")
    assert scenario.step().reason == "updating"
    update, = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "approval_update"]
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [promotion, update], "concise", current_boundary=False) == {promotion["id"]}
    assert scenario.run().kind == "approval"
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [update], "concise", current_boundary=True) == {update["id"]}
