"""Public queries distinguish native preparation from external supervision."""
import json
import os
import sys

import pytest

from agent_run.cli.main import main
from agent_run.core.operation_contract import ReadUnavailable
from agent_run.errors import KernelError
from agent_run.model import Issue, ParentIssue, Policy, Requirements
from agent_run.queries import read_facts
from agent_run.queries.read_facts import QueryError
from agent_run.queries.run import read
from agent_run.runtime.execution import execute
from support.core_operations import agent_preparation_fixture, prepare_scripted_agent
from support.offline import GraphScenario


def _durable(kernel):
    return {p.name: p.read_bytes() for p in kernel.storage.path.parent.iterdir()
            if p.is_file() and not p.name.endswith("-shm")
            and not (p.name.endswith("-wal") and p.stat().st_size == 0)}


def test_status_reads_ordinary_recovery_preparation_without_a_supervision_window(tmp_path, capsys):
    kernel, runner, _, initial, _ = agent_preparation_fixture(tmp_path)
    original = prepare_scripted_agent(kernel, initial).call
    assert original is not None
    kernel.bind_thread(original, "original-worker-thread")
    failed = execute(kernel, original, arguments=[sys.executable, "-I", "-c", "raise SystemExit(23)"],
        cwd=tmp_path, prompt="", environment={"PATH": os.defpath}, timeout=5,
        authorize_source=lambda: runner.authorize_agent(original))
    assert failed is not None and failed.returncode == 23 and kernel.worker_settled(original)
    pending = runner.run_once().preparation
    assert pending is not None and pending.attempt_id == original.attempt_id
    assert pending.thread_id == original.thread_id
    with kernel.storage.transaction(write=False) as tx:
        wait = tx.one("SELECT w.kind,m.subject,m.purpose,m.body FROM waits w JOIN material m ON m.id=w.scope_id")
        assert (wait["kind"], wait["subject"], wait["purpose"]) == (
            "agent_preparation", pending.subject, "agent_preparation")
        assert json.loads(wait["body"])["recovery_kind"] == "ordinary"
    budget = kernel.status(pending.subject)["budgets"]

    before = _durable(kernel)
    document = read(kernel.storage.path, "status")
    assert document.data["supervision_windows"] == []
    assert pending.subject in {job["subject"] for job in document.data["subjects"]}
    arguments = ["status", "preparation", "--repo", "example/repository",
                 "--state-dir", str(kernel.storage.path)]
    assert main([*arguments, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["supervision_windows"] == []
    assert main([*arguments, "--plain"]) == 0
    human = capsys.readouterr().out
    assert human and "incompatible_run_state" not in human
    assert _durable(kernel) == before
    assert runner.state().preparing == (pending.reservation_id,)
    assert kernel.status(pending.subject)["budgets"] == budget

    recovered = prepare_scripted_agent(kernel, pending).call
    assert recovered is not None and recovered.invocation_id != original.invocation_id
    raw = b'{"result_kind":"development","summary":"Original work recovered","human_blockers":null}'
    completed = execute(kernel, recovered, arguments=[sys.executable, "-I", "-c",
        f"import sys; sys.stdout.buffer.write({raw!r})"], cwd=tmp_path, prompt="",
        environment={"PATH": os.defpath}, timeout=5,
        authorize_source=lambda: runner.authorize_agent(recovered))
    assert completed is not None and completed.returncode == 0 and kernel.worker_settled(pending)
    assert kernel.status(pending.subject)["budgets"] == budget
    assert read(kernel.storage.path, "status").data["supervision_windows"] == []


def test_initial_preparation_is_not_an_external_supervision_timeout(tmp_path):
    kernel, runner, _, step, clock = agent_preparation_fixture(tmp_path)
    request = step.preparation
    assert request is not None
    clock[0] = request.preparation_deadline + 1
    before = _durable(kernel)
    status = read(kernel.storage.path, "status").data
    assert status["supervision_windows"] == [] and status["operator_actions"] == []
    assert runner.state().preparing == (request.reservation_id,)
    assert not kernel.worker_settled(request)
    assert _durable(kernel) == before


def test_sibling_required_checks_still_has_its_original_window_during_preparation(tmp_path):
    source = Requirements(ParentIssue(1, "Parent", "Original work", (2, 3)), (
        Issue(2, "First", "First work", "OPEN", labels=("ready-for-agent",)),
        Issue(3, "Second", "Second work", "OPEN", labels=("ready-for-agent",))))
    scenario = GraphScenario(tmp_path / "scenario", source, policy=Policy(concurrency=2))
    scenario.checks[:] = ["pending"]
    kernel, runner = scenario.kernel, scenario.runner()
    held = None
    observed_roles = set()
    for _ in range(80):
        step = runner.run_once(agent_capacity=1)
        if step.preparation is not None:
            observed_roles.add(step.preparation.role)
            assert read(kernel.storage.path, "status").data["supervision_windows"] == []
            if step.subject == "ticket:3":
                held = step.preparation
            else:
                call = prepare_scripted_agent(kernel, step).call
                assert call is not None
                runner.authorize_agent(call)
                kernel.receive(call, scenario.agent(call))
        if step.reason == "required_checks":
            break
    else:
        pytest.fail("Normal Ticket did not reach its Required Checks window")
    for _ in range(10):
        if held is not None:
            break
        sibling = runner.run_once(agent_capacity=1)
        if sibling.preparation is not None:
            assert sibling.subject == "ticket:3"
            held = sibling.preparation
    assert held is not None and step.subject == "ticket:2"
    assert observed_roles == {"development", "review", "publication"}
    with kernel.storage.transaction(write=False) as tx:
        original = dict(tx.one("SELECT * FROM waits WHERE kind='required_checks'"))
    before = _durable(kernel)
    windows = read(kernel.storage.path, "status").data["supervision_windows"]
    assert len(windows) == 1
    assert (windows[0]["subject"], windows[0]["kind"], windows[0]["scope_id"]) == (
        "ticket:2", "required_checks", original["scope_id"])
    assert (windows[0]["started_at"], windows[0]["deadline"], windows[0]["retry_count"]) == (
        original["started_at"], original["deadline"], original["retry_count"])
    assert not windows[0]["timed_out"]
    assert runner.state().preparing == (held.reservation_id,)
    assert _durable(kernel) == before


@pytest.mark.parametrize("family,fault", [
    ("preparation", "kind"), ("preparation", "scope_purpose"),
    ("preparation", "scope_owner"), ("preparation", "scope"),
    ("preparation", "foreign_execution"), ("preparation", "foreign_attempt"),
    ("preparation", "minimal_scope"),
    ("supervision", "kind"), ("supervision", "preparation_kind"),
    ("supervision", "scope_purpose"), ("supervision", "scope_owner"),
    ("supervision", "scope"), ("supervision", "scope_subject"),
    ("supervision", "scope_run"), ("supervision", "evidence_purpose"),
    ("supervision", "evidence_owner"),
])
def test_query_rejects_invalid_typed_wait_reads_without_mutating_records(tmp_path, monkeypatch, family, fault):
    """Fault only the query's read seam; no prepared DB or proof is edited."""
    kernel, runner, _, step, _ = agent_preparation_fixture(tmp_path)
    request = step.preparation
    assert request is not None
    if family == "supervision":
        kernel.preparation_failed(request, "cancelled", {"closed": True})
        kernel.control("resume", "retry-original-preparation", subject=request.subject)

        def unavailable(_parent):
            raise ReadUnavailable("github_read_failed", "Controlled source outage")

        monkeypatch.setattr(runner.ports, "source", unavailable)
        assert runner.run_once().kind == "waiting"
    with kernel.storage.transaction(write=False) as tx:
        scope_id = tx.one("SELECT scope_id FROM waits")[0]
    read(kernel.storage.path, "status")  # The actual stored producer is valid.
    before = _durable(kernel)
    original_rows, original_material = read_facts.all_rows, read_facts.material
    observed = []

    def rows(tx, sql, values=()):
        result = original_rows(tx, sql, values)
        if sql.startswith("SELECT w.*,m.subject"):
            row, = result
            observed.append(row["scope_id"])
            if fault == "kind":
                row["kind"] = "unknown_wait"
            elif fault == "preparation_kind":
                row["kind"] = "agent_preparation"
            elif fault in {"scope_purpose", "evidence_purpose"}:
                row[fault] = "unexpected_material"
            elif fault in {"scope_owner", "evidence_owner"}:
                row[fault] = "fixture-final"
        return result

    def material(tx, identity, subject):
        value = original_material(tx, identity, subject)
        if identity == scope_id:
            if fault == "scope":
                return []
            if fault == "scope_subject":
                return {**value, "subject": "fixture-final"}
            if fault == "scope_run":
                return {**value, "run": "foreign-run"}
            if fault == "foreign_execution":
                return {**value, "execution": {**value["execution"], "run_id": "foreign-run"}}
            if fault == "foreign_attempt":
                return {**value, "attempt_id": "foreign-attempt"}
            if fault == "minimal_scope":
                return {"subject": subject}
        return value

    monkeypatch.setattr(read_facts, "all_rows", rows)
    monkeypatch.setattr(read_facts, "material", material)
    with pytest.raises(QueryError) as rejected:
        read(kernel.storage.path, "status")
    assert rejected.value.code == "incompatible_run_state"
    assert observed == [scope_id]
    assert _durable(kernel) == before


def test_recorded_preparation_remains_readable_after_its_execution_epoch_is_revoked(tmp_path):
    kernel, runner, _, step, _ = agent_preparation_fixture(tmp_path)
    request = step.preparation
    assert request is not None
    kernel.control("stop", "stop-held-preparation")
    with kernel.storage.transaction(write=False) as tx:
        assert tx.one("SELECT epoch FROM runs")[0] != request.execution.epoch
    with pytest.raises(KernelError):
        kernel.guard_preparation(request)
    before = _durable(kernel)
    document = read(kernel.storage.path, "status")
    assert document.data["supervision_windows"] == []
    assert runner.state().preparing == (request.reservation_id,)
    assert not kernel.worker_settled(request)
    assert _durable(kernel) == before


@pytest.mark.parametrize("repair", ["output-repair", "new-thread"])
def test_status_preserves_pending_continuation_bindings(tmp_path, repair):
    kernel, runner, _, first, _ = agent_preparation_fixture(tmp_path)
    original = prepare_scripted_agent(kernel, first).call
    assert original is not None
    kernel.bind_thread(original, "original-worker-thread")
    raw = (b'{"summary":"missing required result kind"}' if repair == "output-repair" else
           b'{"result_kind":"human_blocker","summary":null,"human_blockers":["Choose formatting"]}')
    returned = execute(kernel, original, arguments=[sys.executable, "-I", "-c",
        f"import sys; sys.stdout.buffer.write({raw!r})"], cwd=tmp_path, prompt="",
        environment={"PATH": os.defpath}, timeout=5,
        authorize_source=lambda: runner.authorize_agent(original))
    assert returned is not None and returned.returncode == 0 and kernel.worker_settled(original)
    if repair == "new-thread":
        assert runner.run_once().reason == "human_blocker"
        kernel.control("resume", "answer-original-attempt", subject=original.subject,
                       message="Use the original formatting", new_thread=True)
    pending = runner.run_once().preparation
    assert pending is not None and pending.attempt_id == original.attempt_id
    assert (pending.thread_id != original.thread_id) == (repair == "new-thread")
    if repair == "output-repair":
        assert pending.mode == repair and pending.output_ordinal == 1
    before = _durable(kernel)
    assert read(kernel.storage.path, "status").data["supervision_windows"] == []
    assert runner.state().preparing == (pending.reservation_id,)
    assert kernel.status(pending.subject)["budgets"]["requirements"]["developments_used"] == 1
    assert _durable(kernel) == before
