"""Ordinary progress compression reads original Core facts without changing them."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent_run.model import Issue, ParentIssue, Requirements, Role
from agent_run.errors import KernelError
from agent_run.model import wire_json
from agent_run.notifications.envelope import event_envelope
from agent_run.notifications.progress import obsolete_progress
from agent_run.notifications.event_projection import event_state, project_event
from support.offline import GraphScenario
from support.run_scoped_kernel import domain_kernel
from support.notification import _project, _kernel, _operations
from support.worker_facts import start_worker, finish_worker, consume_local_result, record_local_result
from support.notification import recorded_agents as _recorded_agents


def test_recovery_keeps_the_live_original_round_instead_of_its_finished_predecessor(tmp_path):
    kernel = _kernel(tmp_path)
    operations = _operations(kernel, kernel.claim_executor("notification-consumer"))
    call = operations.once().call
    process = start_worker(kernel, operations, call)
    finish_worker(kernel, call, process)
    assert operations.once().kind == "progress"
    consume_local_result(kernel, operations, call.subject, "candidate")
    consume_local_result(kernel, operations, call.subject, "validation_checkout")
    review = operations.once().call
    assert review.role == Role.REVIEW
    start_worker(kernel, operations, review)
    pending = _project(kernel, "detailed")
    original = deepcopy(pending)
    finished = {event["id"] for event in pending if event["kind"] in {"ticket_started", "stage_end"}}
    live, = [event for event in pending if event["kind"] == "stage_start"]
    assert len(finished) == 2 and live["attempt_id"] == review.attempt_id
    before = kernel.storage.path.read_bytes()
    with kernel.storage.transaction(write=False) as tx:
        row = dict(tx.one("SELECT * FROM history WHERE position=?", (live["source_position"],)))
        projected, = project_event(tx, row, event_state(tx), "detailed")
        assert projected.content == live and projected.applicable is True
        assert projected.matches(live)
        assert not projected.matches(dict(live, attempt_id="foreign-attempt"))
        assert not projected.matches(dict(live, generation=live["generation"] + 1))
        assert obsolete_progress(tx, pending, "detailed", current_boundary=False) == finished
    assert pending == original and kernel.storage.path.read_bytes() == before


def test_unknown_origin_is_preserved_and_corrupt_original_material_pauses_recovery(tmp_path):
    kernel = _kernel(tmp_path)
    operations = _operations(kernel, kernel.claim_executor("notification-consumer"))
    call = operations.once().call
    start_worker(kernel, operations, call)
    event, = _project(kernel, "concise")
    unknown = dict(event, id="unknown-source", source_position=10**9)
    missing = {key: value for key, value in dict(event, id="missing-source").items() if key != "source_material_id"}
    foreign = dict(event, id="foreign-attempt", attempt_id="not-the-original-attempt")
    with kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [event, unknown, missing, foreign], "concise", current_boundary=True) == {event["id"]}
        with pytest.raises(ValueError, match="limit"):
            obsolete_progress(tx, [event] * 33, "concise", current_boundary=False)
        original = tx.get(event["source_material_id"], call.subject)
    with kernel.storage.transaction() as tx:
        tx.execute("UPDATE material SET body=? WHERE id=?", (b"damaged original material", event["source_material_id"]))
    try:
        with kernel.storage.transaction(write=False) as tx:
            with pytest.raises(KernelError, match="material_corrupt"):
                obsolete_progress(tx, [event], "concise", current_boundary=True)
    finally:
        with kernel.storage.transaction() as tx:
            tx.execute("UPDATE material SET body=? WHERE id=?", (original, event["source_material_id"]))


def test_original_run_start_is_current_only_before_any_actual_worker_starts(tmp_path):
    kernel = _kernel(tmp_path)
    with kernel.storage.transaction(write=False) as tx:
        run = dict(tx.one("SELECT * FROM runs"))
        start = event_envelope({"run_id": run["id"], "language": run["language"]}, "run_start", run["id"], "Original Run start")
        assert obsolete_progress(tx, [start], "concise", current_boundary=False) == set()
        assert obsolete_progress(tx, [start], "concise", current_boundary=True) == {start["id"]}
    operations = _operations(kernel, kernel.claim_executor("notification-consumer"))
    start_worker(kernel, operations, operations.once().call)
    pending = [start, *_project(kernel, "concise")]
    with kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, pending, "concise", current_boundary=False) == {start["id"]}


def test_recovery_keeps_only_the_latest_actual_start_among_independent_live_tickets(tmp_path):
    kernel = _kernel(tmp_path, count=2)
    operations = _operations(kernel, kernel.claim_executor("notification-consumer"), count=2)
    calls = [operations.once().call]
    start_worker(kernel, operations, calls[0])
    calls.append(operations.once().call)
    start_worker(kernel, operations, calls[1])
    assert {kernel.delivery(call.subject).issue for call in calls} == {2, 3}
    pending = _project(kernel, "concise")
    assert len(pending) == 2
    older = next(event for event in pending if event["attempt_id"] == calls[0].attempt_id)
    newer = next(event for event in pending if event["attempt_id"] == calls[1].attempt_id)
    with kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [newer, older], "concise", current_boundary=False) == {older["id"]}
        assert obsolete_progress(tx, pending, "concise", current_boundary=True) == {older["id"], newer["id"]}


def test_worker_resume_uses_the_original_action_and_same_attempt_live_continuation(tmp_path):
    kernel = _kernel(tmp_path)
    operations = _operations(kernel, kernel.claim_executor("notification-consumer"))
    call = operations.once().call
    process = start_worker(kernel, operations, call)
    finish_worker(kernel, call, process, wire_json({"result_kind": "human_blocker", "summary": None,
                                                 "human_blockers": ["Original human decision"]}))
    assert operations.once().kind == "paused"
    assert kernel.control("resume", "resume-original", subject=call.subject, message="Original decision") == "applied"
    continued = operations.once().call
    process = start_worker(kernel, operations, continued)
    projected = _project(kernel, "concise")
    event, = [event for event in projected if event["kind"] == "resume"]
    prior = next(event for event in projected if event["kind"] == "ticket_started")
    assert prior["attempt_id"] == event["attempt_id"] == continued.attempt_id == call.attempt_id
    assert event["resume_id"] == "resume-original"
    with kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [prior, event], "concise", current_boundary=False) == {prior["id"]}
    finish_worker(kernel, continued, process)
    with kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [event], "concise", current_boundary=False) == {event["id"]}


@pytest.mark.parametrize("scope", ["subject", "run"])
def test_consumed_controller_resume_is_a_historical_result_after_recovery(tmp_path, scope):
    kernel = _kernel(tmp_path)
    operations = _operations(kernel, kernel.claim_executor("notification-consumer"))
    call = operations.once().call
    process = start_worker(kernel, operations, call)
    finish_worker(kernel, call, process)
    assert operations.once().kind == "progress"
    assert kernel.status(call.subject)["phase"] == "candidate"
    subject = call.subject if scope == "subject" else None
    assert kernel.control("stop", "stop-controller", subject=subject) == "applied"
    if scope == "run":
        kernel.confirm_executor_exit(operations.runner.execution)
    assert kernel.control("resume", "resume-controller", subject=subject) == "applied"
    if scope == "run":
        operations = _operations(kernel, kernel.claim_executor("continued-owner"))
    assert not any(event["kind"] == "resume" for event in _project(kernel, "concise"))
    effect, world = record_local_result(kernel, operations, call.subject, "candidate")
    # Prepared/sent/readback evidence is not consumption of the original target.
    assert not any(event["kind"] == "resume" for event in _project(kernel, "concise"))
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, call.subject, effect.execution)
        row = tx.one("SELECT * FROM effects WHERE run_id=? AND id=?", (effect.execution.run_id, effect.effect_id))
        assert kernel._apply_effect(tx, run, job, row, world).kind == "progress"
    event, = [event for event in _project(kernel, "concise") if event["kind"] == "resume"]
    assert event["evidence"] == "effect_consumed" and "attempt_id" not in event
    assert event["effect_id"] == effect.effect_id and event["resume_id"] == "resume-controller"
    assert event["resume_outcome"] == "started" and event["resume_scope"] == scope
    assert event["resume_subject"] == subject and event["resume_generation"] == (1 if subject else None)
    assert event["work_subject"] == call.subject and event["generation"] == 1
    assert event["task_number"] == 1
    if scope == "run":
        assert event["resumed_from_action"] == "stop-controller"
    with kernel.storage.transaction(write=False) as tx:
        assert obsolete_progress(tx, [event], "concise", current_boundary=False) == {event["id"]}


def test_original_repair_and_formal_promotion_follow_current_generation_and_approval_invalidation(tmp_path, monkeypatch):
    scenario = GraphScenario(tmp_path, Requirements(
        ParentIssue(1, "Parent", "Original scope", (2,)),
        (Issue(2, "Ticket", "Original task", "OPEN", labels=("ready-for-agent",)),)),
        reviews=("pass", "fail", "pass"))
    with domain_kernel(scenario.kernel) as scenario.kernel:
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
        for _ in range(40):
            scenario.step()
            if scenario.kernel.source_status()["final_subject"] is not None:
                break
        else:
            raise AssertionError("Real Ticket Completion did not admit Final")
        scenario.select("final:1")
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
                    original = dict(tx.one("SELECT * FROM history WHERE kind='role_consumed' AND identity=?",
                                           (promotion["attempt_id"],)))
                    suppressed, = project_event(tx, original, event_state(tx), "detailed")
                    assert not suppressed.visible and suppressed.applicable is False
                    assert obsolete_progress(tx, [suppressed.content], "detailed", current_boundary=False) == {suppressed.content["id"]}
                break
            assert step.kind not in {"paused", "waiting", "approval"}, step
        else:
            raise AssertionError("The actual repair did not reach formal promotion")
        assert scenario.run().kind == "approval"
        assert not any(event["kind"] == "approval_update" for event in _project(scenario.kernel, "concise"))
        scenario.port.external_remote_append(scenario.delivery.target_ref, {"external.txt": "New target\n"}, "approval-update")
        assert scenario.step().reason == "updating"
        update, = [event for event in _project(scenario.kernel, "concise") if event["kind"] == "approval_update"]
        with scenario.kernel.storage.transaction(write=False) as tx:
            invalidated = tx.one("SELECT * FROM history WHERE subject=? AND kind='boundary_update' ORDER BY position DESC LIMIT 1",
                                 (scenario.delivery.key,))
            assert update["source_position"] == invalidated["position"] and update["source_material_id"] == invalidated["material_id"]
            assert update["work_subject"] == scenario.delivery.key and update["generation"] == 1
            assert obsolete_progress(tx, [promotion, update], "concise", current_boundary=False) == {promotion["id"]}
        assert scenario.run().kind == "approval"
        projected = _project(scenario.kernel, "concise")
        assert [event for event in projected if event["kind"] == "approval_update"] == [update]
        assert not any(event["kind"] == "approval_ready" for event in projected)
        with scenario.kernel.storage.transaction(write=False) as tx:
            assert obsolete_progress(tx, [update], "concise", current_boundary=True) == {update["id"]}
