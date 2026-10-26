"""Semantic Development continuation is bounded work, never output repair."""
import pytest
from dataclasses import replace
import json

from agent_run.contracts.artifact import ArtifactContractError, parse_artifact, role_schema
from agent_run.model import wire_json
from agent_run.core.attempt_continuation import load_continuation
from agent_run.errors import KernelError
from agent_run.core.kernel import Kernel
from agent_run.model import DeliveryKind, Policy
from support.offline import Scenario
from support.core_operations import prepare_scripted_agent
from test_kernel_attempt_continuation import _native_boundary, _read_operations
from test_kernel_agent_preparation import preparation


INCOMPLETE = wire_json({"result_kind": "incomplete", "summary": "Implementation is unfinished; continue the current task.", "human_blockers": None})


def test_only_development_accepts_structured_incomplete():
    assert "incomplete" in role_schema("development")["properties"]["result_kind"]["enum"]
    assert parse_artifact("development", INCOMPLETE)["result_kind"] == "incomplete"
    with pytest.raises(ArtifactContractError):
        parse_artifact("publication", INCOMPLETE)


def test_incomplete_uses_same_attempt_thread_and_deadline_with_new_invocation(tmp_path):
    kernel, owner, operations, attempt_id, original = _native_boundary(tmp_path, return_body=INCOMPLETE)
    before = kernel.status(original.subject)["budgets"]
    kernel.clock = lambda: 103.0
    step = operations.once()
    resumed = step.call
    assert resumed is not None
    assert resumed.attempt_id == attempt_id and resumed.thread_id == original.thread_id
    assert resumed.invocation_id != original.invocation_id
    assert resumed.deadline_at == original.deadline_at == 107.0
    assert resumed.mode == "resume" and resumed.output_ordinal == 0
    assert resumed.context["readonly"] is False
    assert kernel.status(original.subject)["budgets"] == before
    with kernel.storage.transaction(write=False) as tx:
        facts = load_continuation(tx, attempt_id)
        assert facts.semantic_used == 1 and facts.semantic_deadline == original.deadline_at
        assert facts.ordinary_used == facts.capacity_count == facts.output_ordinal == 0
        assert not tx.rows("SELECT id FROM effects")
        assert tx.get(tx.one("SELECT raw_id FROM invocation_outputs WHERE invocation=?", (original.invocation_id,))[0], original.subject) == INCOMPLETE


def finish(kernel, call, body, *, failure_kind="ordinary"):
    assert kernel.dispatch_agent(call)
    kernel.bind_thread(call, call.reported_thread or "retained-backend")
    process = {"pid": 77, "start_token": call.invocation_id, "pgid": 77,
               "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(call, process)
    kernel.agent_source_ready(call)
    if body is not None:
        kernel.receive(call, body)
    kernel.agent_exited(call, process, 0 if body is not None else 1, failure_kind=failure_kind)


def test_second_incomplete_pauses_and_reopen_does_not_reset_the_one_successor(tmp_path):
    kernel, owner, operations, attempt, original = _native_boundary(tmp_path, return_body=INCOMPLETE)
    resumed = operations.once().call
    assert resumed is not None
    finish(kernel, resumed, INCOMPLETE)
    stopped = operations.once()
    assert stopped.kind == "paused" and stopped.reason == "semantic_incomplete_limit"
    reopened = Kernel(kernel.storage.path)
    reopened.clock = kernel.clock
    continued = _read_operations(reopened, owner, operations.runner.ports.requirements)
    assert continued.once().call is None
    with reopened.storage.transaction(write=False) as tx:
        assert load_continuation(tx, attempt).semantic_used == 1
        assert tx.one("SELECT count(*) FROM invocations")[0] == 2
        assert not tx.rows("SELECT id FROM effects")
    reopened.control("resume", "explicit-new-period", subject=original.subject)
    reopened.clock = lambda: 104.0
    explicit = continued.once().call
    assert explicit is not None and explicit.attempt_id == attempt
    assert explicit.deadline_at == 111.0 and explicit.thread_id == original.thread_id
    with reopened.storage.transaction(write=False) as tx:
        facts = load_continuation(tx, attempt)
        assert facts.semantic_used == 1 and facts.semantic_deadline is None
        assert tx.one("SELECT deadline_at FROM invocations WHERE id=?", (original.invocation_id,))[0] == 107.0


def test_semantic_preparation_cannot_refresh_the_original_deadline(tmp_path):
    kernel, _, operations, _, original = _native_boundary(tmp_path, return_body=INCOMPLETE)
    kernel.clock = lambda: 106.5
    request = operations.runner.run_once().preparation
    assert request is not None and request.preparation_deadline == 107.0
    kernel.clock = lambda: 107.0
    with pytest.raises(KernelError, match="invocation_deadline_exceeded"):
        kernel.admit_agent(request)
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM invocations")[0] == 1
        assert tx.one("SELECT deadline_at FROM invocations WHERE id=?", (original.invocation_id,))[0] == 107.0
    kernel.preparation_failed(request, "invocation_deadline_exceeded", {"closed": True})
    assert kernel.worker_settled(request)


@pytest.mark.parametrize("kind", ["ordinary", "capacity"])
def test_automatic_failure_recovery_cannot_extend_semantic_time_or_reset_counts(tmp_path, kind):
    kernel, _, operations, attempt, original = _native_boundary(tmp_path, return_body=INCOMPLETE)
    kernel.clock = lambda: 103.0
    resumed = operations.once().call
    assert resumed is not None
    kernel.clock = lambda: 104.0
    finish(kernel, resumed, None, failure_kind=kind)
    if kind == "capacity":
        assert operations.once().call is None
        with kernel.storage.transaction(write=False) as tx:
            assert load_continuation(tx, attempt).prior.recovery_ready_at == 134.0
            assert tx.one("SELECT count(*) FROM invocations")[0] == 2
        assert operations.runner.state().wake_at == 107.0
        kernel.clock = lambda: 107.0
        expired = operations.once()
        assert expired.call is None and expired.kind == "paused"
        assert expired.reason == "invocation_deadline_exceeded"
    else:
        recovered = operations.once().call
        assert recovered is not None and recovered.deadline_at == original.deadline_at
        assert recovered.mode == "resume" and recovered.output_ordinal == 0
    with kernel.storage.transaction(write=False) as tx:
        facts = load_continuation(tx, attempt)
        assert facts.semantic_used == 1 and facts.semantic_deadline == 107.0
        assert (facts.ordinary_used, facts.capacity_count) == ((1, 0) if kind == "ordinary" else (0, 0))


def test_stop_revokes_semantic_preparation_and_late_raw_is_only_history(tmp_path):
    kernel, _, operations, _, original = _native_boundary(tmp_path, return_body=INCOMPLETE)
    request = operations.runner.run_once().preparation
    assert request is not None
    kernel.control("stop", "stop-semantic", subject=original.subject)
    kernel.receive(original, INCOMPLETE)
    with pytest.raises(KernelError):
        kernel.admit_agent(request)
    kernel.preparation_failed(request, "cancelled", {"closed": True})
    assert operations.once().call is None
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM invocations")[0] == 1
        assert not tx.rows("SELECT id FROM effects")


def test_changed_generation_cannot_continue_an_old_incomplete(tmp_path):
    kernel, _, operations, attempt, original = _native_boundary(tmp_path, return_body=INCOMPLETE)
    ports = operations.runner.ports
    ports.requirements = replace(ports.requirements, parent=replace(ports.requirements.parent, body="New explicit requirement"))
    world = operations.runner.observe(original.subject)
    delivery = kernel.delivery(original.subject)
    replacement = replace(delivery, source_ref=delivery.source_ref + "-g2",
                          checkout=str(tmp_path / "work-g2"), base=world.target)
    kernel.requeue(original.subject, "replace-incomplete-generation", replacement, world)
    kernel.receive(original, INCOMPLETE)
    assert kernel.status(original.subject)["generation"] == 2
    assert kernel.status(original.subject)["pending_attempt"] is None
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM invocations")[0] == 1
        assert tx.one("SELECT generation FROM attempts WHERE id=?", (attempt,))[0] == 1


def test_incomplete_in_readonly_format_output_does_not_start_writable_continuation(tmp_path):
    kernel, _, operations, attempt, original = _native_boundary(tmp_path, malformed=True)
    repair = operations.once().call
    assert repair is not None and repair.context["readonly"] is True and repair.output_ordinal == 1
    finish(kernel, repair, INCOMPLETE)
    paused = operations.once()
    assert paused.kind == "paused" and paused.reason == "semantic_incomplete_readonly"
    with kernel.storage.transaction(write=False) as tx:
        facts = load_continuation(tx, attempt)
        assert facts.semantic_used == 0 and facts.output_ordinal == 1
        assert tx.one("SELECT count(*) FROM invocations")[0] == 1
        assert not tx.rows("SELECT id FROM effects")


def test_missing_exit_proof_never_creates_a_semantic_successor(tmp_path):
    kernel, _, operations, _, original = _native_boundary(tmp_path, return_body=INCOMPLETE, exit_proven=False)
    for _ in range(3):
        assert operations.once().call is None
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM invocations")[0] == 1
        assert tx.one("SELECT count(*) FROM leases")[0] == 1
        assert not tx.rows("SELECT id FROM effects")


def test_final_incomplete_without_reported_backend_thread_pauses_without_a_successor(tmp_path):
    kernel, runner, _, step, _ = preparation(tmp_path)
    call = kernel.admit_agent(step.preparation).call
    assert call is not None and call.reported_thread is None
    assert kernel.dispatch_agent(call)
    process = {"pid": 77, "start_token": "missing-thread", "pgid": 77,
               "host_pid": 11, "host_start_token": "host-start"}
    kernel.agent_started(call, process)
    kernel.agent_source_ready(call)
    kernel.receive(call, INCOMPLETE)
    kernel.agent_exited(call, process, 0)
    paused = runner.run_once()
    assert paused.kind == "paused" and paused.reason == "semantic_continuation_unavailable"
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM invocations")[0] == 1
        assert not tx.rows("SELECT id FROM effects")


def test_no_change_cannot_use_ticket_fallback_instead_of_independent_review(tmp_path):
    scenario = Scenario(tmp_path / "run", DeliveryKind.TICKET, reviews=("fail",),
                        policy=Policy(ticket_rounds=1), edits=({"implemented.txt": "Real requested work.\n"}, {}))
    paused = scenario.run(max_steps=80)
    state = scenario.kernel.status(scenario.delivery.key)
    assert paused.kind == "paused" and state["phase"] == "review"
    assert state["gates"] == ["review_budget_exhausted"]
    assert state["gate_mode"] != "deterministic_fallback" and state["pr_number"] is None
    with scenario.kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM attempts WHERE role='development'")[0] == 2
        assert tx.one("SELECT count(*) FROM attempts WHERE role='review'")[0] == 1
        assert not tx.rows("SELECT id FROM effects WHERE operation='pr_write'")


def test_human_response_without_code_change_reaches_fresh_review_as_raw_evidence(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path / "run", DeliveryKind.TICKET, reviews=("fail", "pass"),
                        policy=Policy(ticket_rounds=2), edits=({"implemented.txt": "Real requested work.\n"}, {}))
    agent = scenario.agent
    blocker = "Confirm whether the current Finding requires another code change."
    def blocked_development(call):
        raw = agent(call)
        if call.role == "development" and scenario.development_count == 2:
            return wire_json({"result_kind": "human_blocker", "summary": None, "human_blockers": [blocker]})
        return raw
    with monkeypatch.context() as patch:
        patch.setattr(scenario, "agent", blocked_development)
        assert scenario.run(max_steps=80).kind == "paused"
    kernel, subject = scenario.kernel, scenario.delivery.key
    before = kernel.status(subject)
    assert before["gates"] == ["human_blocker"]
    with kernel.storage.transaction(write=False) as tx:
        prior = tx.one("SELECT thread_id,result_id FROM attempts WHERE role='review'")
        original_review = tx.get(prior["result_id"], subject)
        old_review_thread = prior["thread_id"]
        old_attempt_count = tx.one("SELECT count(*) FROM attempts WHERE role='development'")[0]
        blocker_id = tx.one("SELECT result_id FROM attempts WHERE id=?", (before["pending_attempt"],))[0]
        original_blocker = tx.get(blocker_id, subject)
    response = "The original Finding is addressed without another code change.\r\nKeep the original evidence."
    kernel.control("resume", "respond-to-current-finding", subject=subject, message=response)
    runner, fresh_review = scenario.runner(), None
    for _ in range(60):
        step = prepare_scripted_agent(kernel, runner.run_once())
        if step.call is not None:
            call = step.call
            if call.role == "review":
                fresh_review = call
                assert call.thread_id != old_review_thread
                assert call.context["human_responses"] == [response]
                assert call.context["review_context"]["facts"]["previous_acceptance_artifact"]["checks"]["spec"]["findings"]
                assert kernel.status(subject)["pr_number"] is None
                assert kernel.status(subject)["gate_mode"] != "deterministic_fallback"
            runner.authorize_agent(call)
            kernel.receive(call, scenario.agent(call))
            if fresh_review is not None:
                break
    assert fresh_review is not None
    assert runner.run_once().kind == "progress"
    after = kernel.status(subject)
    assert after["gate_mode"] == "review" and after["phase"] == "integration"
    assert after["budgets"]["requirements"]["developments_used"] == before["budgets"]["requirements"]["developments_used"] == 2
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT count(*) FROM attempts WHERE role='development'")[0] == old_attempt_count
        assert tx.get(prior["result_id"], subject) == original_review
        assert tx.get(blocker_id, subject) == original_blocker
        assert tx.one("SELECT count(*) FROM attempts WHERE role='review'")[0] == 2
        candidates = [json.loads(tx.get(row[0], subject)) for row in tx.all(
            "SELECT receipt_id FROM effects WHERE subject=? AND operation='candidate' ORDER BY rowid", (subject,))]
        assert len(candidates) == 2 and candidates[-1]["raw"]["no_change"] is True
        assert candidates[0]["commit"]["sha"] == candidates[1]["commit"]["sha"]
