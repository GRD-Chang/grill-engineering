"""Persisted Effect domain on current relations; no physical or public routing.

Only relational creation prerequisites are supplied. Intent, dispatch/readback,
receipt and consumption use existing Core methods and the real transaction owner.
External results are synthetic; no adapter writes or Host/process is executed.
"""
from contextlib import closing
from dataclasses import asdict, replace
import sqlite3
import json

import pytest

from agent_run.core import kernel as core
from agent_run.core.operation_contract import EffectIO, Work
from agent_run.errors import KernelError
from agent_run.model import Commit, EffectResult, Execution, Policy, Step, World, wire_json
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.offline import fixture_inputs
from support.repository_scope import connection, insert, seed
from support.run_scoped_kernel import ScopedKernel, effect_runs


SUBJECT = "parent:1"
BASE = Commit("a" * 40, "b" * 40)
WORLD = World("r", "g", "r", BASE, BASE, publisher_actor="controlled-actor",
    publisher_identity={"source": "host"})


@pytest.fixture
def scenario(monkeypatch):
    with effect_runs(monkeypatch) as value:
        yield value


def _work(run):
    return Work(Execution(run, "same-owner", 1), "pending", SUBJECT, False, True, None, None)


def _rows(db, run):
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: [tuple(row) for row in db.execute(
        f"SELECT * FROM {name} WHERE {'id' if name == 'runs' else 'run_id'}=? ORDER BY rowid", (run,))]
        for name in names if name != "meta"}


def _consume(kernel, effect):
    with kernel.storage.transaction() as tx:
        run, job = kernel._owned(tx, SUBJECT, effect.execution)
        row = tx.one("SELECT * FROM effects WHERE run_id=? AND id=?", (effect.execution.run_id, effect.effect_id))
        return kernel._apply_effect(tx, run, job, row, WORLD)


def _prepare(kernel, run, *, operation="workspace", world=WORLD, **kwargs):
    with kernel.storage.transaction() as tx:
        header, job = kernel._owned(tx, SUBJECT, _work(run).execution)
        return kernel._prepare_effect(tx, header, job, _work(run), world, operation,
            before=BASE.sha, **kwargs)


def _request(kernel, run):
    with kernel.storage.transaction(write=False) as tx:
        row = tx.one("SELECT * FROM effects WHERE run_id=?", (run,))
        return kernel._effect_work(tx, _work(run), row)


def test_review_checkout_intent_roundtrip_then_preparation_is_scoped_and_zero_fee(scenario, monkeypatch):
    db, kernels = scenario
    before_b = _rows(db, "B")
    with kernels["A"].storage.transaction() as tx:
        run, job = kernels["A"]._owned(tx, SUBJECT, _work("A").execution)
        request = kernels["A"]._allocate(tx, run, job, _work("A"), WORLD)
    assert isinstance(request, EffectIO) and request.effect.operation == "validation_checkout"
    assert _rows(db, "B") == before_b

    assert kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    result = EffectResult("target", commit=BASE)
    assert kernels["A"]._reconcile(request, result).kind == "progress"
    assert _consume(kernels["A"], request.effect).kind == "progress"
    monkeypatch.setattr(core, "identity", iter(["attempt", "thread", "reservation"]).__next__)
    with kernels["A"].storage.transaction() as tx:
        run, job = kernels["A"]._owned(tx, SUBJECT, _work("A").execution)
        prepared = kernels["A"]._allocate(tx, run, job, _work("A"), WORLD).preparation
    assert prepared is not None and prepared.context["checkout"] == "/fixture/review-same-effect"
    assert prepared.context["readonly"] is True and prepared.deadline_seconds == 9.
    assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0
    assert db.execute("SELECT developments_used,reviews_used FROM budgets WHERE run_id='A'").fetchone()[:] == (0, 0)
    assert _rows(db, "B") == before_b

def test_same_effect_id_has_independent_intent_dispatch_receipt_and_consumption(scenario):
    db, kernels = scenario
    a, b = [_prepare(kernels[run], run) for run in ("A", "B")]
    assert a.effect.effect_id == b.effect.effect_id == "same-effect"
    assert a.effect.payload["checkout"] != b.effect.payload["checkout"]
    before_b = _rows(db, "B")
    assert kernels["A"]._dispatch(a.effect, a.work.execution, WORLD, token=a.token)
    assert not kernels["A"]._dispatch(a.effect, a.work.execution, WORLD, token=a.token)
    kernels["A"]._guard_effect(a.effect, a.work.execution, WORLD, token=a.token)
    kernels["A"]._record_readback(a.effect, EffectResult("target", commit=BASE))
    kernels["A"]._reconcile(a, EffectResult("target", commit=BASE))
    assert _consume(kernels["A"], a.effect).kind == "progress"
    assert _rows(db, "B") == before_b
    assert db.execute("SELECT status,attempts FROM effects WHERE run_id='A'").fetchone()[:] == ("confirmed", 1)
    assert db.execute("SELECT status,attempts FROM effects WHERE run_id='B'").fetchone()[:] == ("prepared", 0)
    before_a = _rows(db, "A")
    assert kernels["B"]._dispatch(b.effect, b.work.execution, WORLD, token=b.token)
    kernels["B"]._reconcile(b, EffectResult("target", commit=BASE))
    _consume(kernels["B"], b.effect)
    assert _rows(db, "A") == before_a
    assert db.execute("PRAGMA foreign_key_check").fetchone() is None


@pytest.mark.parametrize("callback", ["dispatch", "guard", "readback", "reconcile", "failure", "resources", "observation", "already_observed", "apply"])
def test_foreign_effect_cannot_read_complete_or_revoke_same_named_local_effect(scenario, callback):
    db, kernels = scenario
    a, b = [_prepare(kernels[run], run) for run in ("A", "B")]
    if callback == "already_observed":
        assert kernels["A"]._dispatch(a.effect, a.work.execution, WORLD, token=a.token)
        kernels["A"]._reconcile(a, EffectResult("target", commit=BASE))
    before = {run: _rows(db, run) for run in ("A", "B")}
    kernel = kernels["A"]
    with pytest.raises(KernelError):
        if callback == "dispatch":
            kernel._dispatch(b.effect, b.work.execution, WORLD, token=b.token)
        elif callback == "guard":
            kernel._guard_effect(b.effect, b.work.execution, WORLD, token=b.token)
        elif callback == "readback":
            kernel._record_readback(b.effect, EffectResult("target", commit=BASE))
        elif callback in {"reconcile", "already_observed"}:
            kernel._reconcile(b, EffectResult("target", commit=BASE))
        elif callback == "failure":
            kernel.observe_effect_failure(b.effect, {"returncode": 23})
        elif callback == "resources":
            kernel.effect_resources(b.effect)
        elif callback == "observation":
            kernel.begin_effect_observation(b.effect)
        else:
            with kernel.storage.transaction() as tx:
                run, job = kernel._owned(tx, SUBJECT, a.work.execution)
                row = tx.one("SELECT * FROM effects WHERE run_id='B'")
                kernel._apply_effect(tx, run, job, row, WORLD)
    assert {run: _rows(db, run) for run in ("A", "B")} == before


def test_unknown_write_holds_original_intent_and_late_target_never_creates_a_retry(scenario):
    db, kernels = scenario
    request = _prepare(kernels["A"], "A", operation="push", after=BASE.sha)
    before_b = _rows(db, "B")
    assert kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    original = tuple(db.execute("SELECT payload_id,attempts FROM effects WHERE run_id='A'").fetchone())
    paused = kernels["A"]._reconcile(request, EffectResult("unknown"))
    assert (paused.kind, paused.reason) == ("paused", "unknown")
    assert db.execute("SELECT status FROM effects WHERE run_id='A'").fetchone()[0] == "sent"
    assert _request(kernels["A"], "A").kind == "readback"
    assert not kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    with pytest.raises(KernelError, match="operation_cancelled"):
        kernels["A"]._guard_effect(request.effect, request.work.execution, WORLD, token=request.token)
    # Owner retirement is a separate controller event. An exact late original
    # target receipt remains settleable without granting another physical write.
    db.execute("UPDATE runs SET executor=NULL,epoch=2,stopped=1 WHERE id='A'")
    assert kernels["A"]._reconcile(request, EffectResult("target", commit=BASE)).kind == "progress"
    assert tuple(db.execute("SELECT payload_id,attempts FROM effects WHERE run_id='A'").fetchone()) == original
    assert db.execute("SELECT status FROM effects WHERE run_id='A'").fetchone()[0] == "observed"
    assert db.execute("SELECT code FROM gates WHERE run_id='A'").fetchone()[0] == "write_result_unknown"
    assert _rows(db, "B") == before_b


def test_definite_prior_rearms_only_the_original_intent_and_keeps_three_attempt_limit(scenario):
    db, kernels = scenario
    request = _prepare(kernels["A"], "A", operation="push", after=BASE.sha)
    before_b = _rows(db, "B")
    payload = request.effect.payload
    for serial in (1, 2, 3):
        request = _request(kernels["A"], "A")
        assert request.kind == "execute" and request.effect.dispatch_serial == serial
        assert request.effect.payload == payload
        assert kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
        result = kernels["A"]._reconcile(request, EffectResult("prior"), WORLD, token=request.token)
        assert result.kind == ("progress" if serial < 3 else "paused")
    assert result.reason == "publication_retry_exhausted"
    assert db.execute("SELECT attempts,status FROM effects WHERE run_id='A'").fetchone()[:] == (3, "sent")
    assert _rows(db, "B") == before_b


def test_failure_audit_is_idempotent_per_run_and_never_becomes_a_receipt(scenario):
    db, kernels = scenario
    a, b = [_prepare(kernels[run], run) for run in ("A", "B")]
    for run, request in (("A", a), ("B", b)):
        assert kernels[run]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    before_b = _rows(db, "B")
    kernels["A"].observe_effect_failure(a.effect, {"returncode": 23, "category": "write_failed"})
    before_a = _rows(db, "A")
    kernels["A"].observe_effect_failure(a.effect, {"returncode": 23, "category": "write_failed"})
    assert _rows(db, "A") == before_a
    with pytest.raises(KernelError, match="conflicting_effect_failure"):
        kernels["A"].observe_effect_failure(a.effect, {"returncode": 24})
    assert _rows(db, "A") == before_a and _rows(db, "B") == before_b
    assert db.execute("SELECT status,receipt_id FROM effects WHERE run_id='A'").fetchone()[:] == ("sent", None)


def test_wrong_checkout_receipt_cannot_be_consumed_or_promote_sibling_validation(scenario):
    db, kernels = scenario
    request = _prepare(kernels["A"], "A", operation="validation_checkout", payload={"reviewed_commit": BASE.sha})
    assert kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    kernels["A"]._reconcile(request, EffectResult("target", commit=Commit("c" * 40, BASE.tree)))
    before = {run: _rows(db, run) for run in ("A", "B")}
    with pytest.raises(KernelError, match="validation_commit_changed"):
        _consume(kernels["A"], request.effect)
    assert {run: _rows(db, run) for run in ("A", "B")} == before


@pytest.mark.parametrize("point", ["prepare", "dispatch", "receipt", "audit", "consume"])
def test_sql_failure_at_each_effect_stage_preserves_owner_poison_and_exact_rollback(scenario, point):
    db, kernels = scenario
    request = None
    if point != "prepare":
        request = _prepare(kernels["A"], "A")
    if point in {"receipt", "audit", "consume"}:
        assert kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    if point == "consume":
        kernels["A"]._reconcile(request, EffectResult("target", commit=BASE))
    before = {run: _rows(db, run) for run in ("A", "B")}
    # Fail the final history INSERT after each stage's preceding durable writes.
    db.set_authorizer(lambda operation, table, *_: sqlite3.SQLITE_DENY
        if operation == sqlite3.SQLITE_INSERT and table == "history" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            if point == "prepare":
                _prepare(kernels["A"], "A")
            elif point == "dispatch":
                kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
            elif point == "receipt":
                kernels["A"]._reconcile(request, EffectResult("target", commit=BASE))
            elif point == "audit":
                kernels["A"].observe_effect_failure(request.effect, {"returncode": 23})
            else:
                _consume(kernels["A"], request.effect)
    finally:
        db.set_authorizer(None)
    assert kernels["A"].owners[-1].failed
    assert {run: _rows(db, run) for run in ("A", "B")} == before


def test_operation_tokens_and_original_read_deadline_do_not_borrow_sibling_facts(scenario):
    db, kernels = scenario
    a, b = [_prepare(kernels[run], run) for run in ("A", "B")]
    assert kernels["A"]._dispatch(a.effect, a.work.execution, WORLD, token=a.token)
    assert kernels["B"]._dispatch(b.effect, b.work.execution, WORLD, token=b.token)
    before_b = _rows(db, "B")
    first = kernels["A"]._wait_for_read(a.token, a.work.execution, "read_failed", "controlled outage", readback=a.effect)
    assert first.kind == "waiting"
    wait = db.execute("SELECT started_at,deadline,retry_count FROM waits WHERE run_id='A'").fetchone()
    assert wait[:] == (100., 700., 1)
    kernels["A"].clock = lambda: 120.
    second = kernels["A"]._wait_for_read(a.token, a.work.execution, "read_failed", "controlled outage", readback=a.effect)
    assert second.kind == "waiting"
    assert db.execute("SELECT started_at,deadline FROM waits WHERE run_id='A'").fetchone()[:] == (100., 700.)
    assert _rows(db, "B") == before_b
    before = {run: _rows(db, run) for run in ("A", "B")}
    for check in (
        lambda: kernels["A"]._guard_automatic_step(b.token, a.work.execution),
        lambda: kernels["A"]._finish_operation_read(b.token, a.work.execution),
        lambda: kernels["A"]._finish_automatic_step(b.token, a.work.execution, Step("progress", SUBJECT)),
    ):
        with pytest.raises(KernelError):
            check()
    assert kernels["A"]._wait_for_read(b.token, a.work.execution, "read_failed", "stale").reason == "observation_superseded"
    assert {run: _rows(db, run) for run in ("A", "B")} == before
    kernels["A"].clock = lambda: 700.
    assert kernels["A"]._finish_operation_read(a.token, a.work.execution).reason == "supervision_timeout"
    assert db.execute("SELECT attempts,status FROM effects WHERE run_id='A'").fetchone()[:] == (1, "sent")
    assert _rows(db, "B") == before_b


def test_sibling_change_does_not_change_progress_but_local_effect_does(scenario):
    db, kernels = scenario
    a, b = [_prepare(kernels[run], run) for run in ("A", "B")]
    before = json.loads(a.token)["progress"]
    assert kernels["B"]._dispatch(b.effect, b.work.execution, WORLD, token=b.token)
    assert json.loads(_request(kernels["A"], "A").token)["progress"] == before
    assert kernels["A"]._dispatch(a.effect, a.work.execution, WORLD, token=a.token)
    assert json.loads(_request(kernels["A"], "A").token)["progress"] != before


def test_final_action_failure_only_retires_selected_run_binding(scenario):
    db, kernels = scenario
    for run in ("A", "B"):
        insert(db, "actions", {"run_id": run, "id": "same-action", "kind": "approve", "subject": SUBJECT, "status": "pending"})
        db.execute("UPDATE runs SET final_action='same-action' WHERE id=?", (run,))
    before_b = _rows(db, "B")
    with kernels["A"].storage.transaction() as tx:
        header = tx.one("SELECT * FROM runs WHERE id='A'")
        kernels["A"]._finish_final_failure(tx, header)
    assert db.execute("SELECT status FROM actions WHERE run_id='A'").fetchone()[0] == "failed"
    assert db.execute("SELECT final_action FROM runs WHERE id='A'").fetchone()[0] is None
    assert _rows(db, "B") == before_b


def test_integration_snapshot_insert_and_correlated_update_stay_with_selected_run(scenario):
    db, kernels = scenario
    for run in ("A", "B"):
        insert(db, "resync_allowances", {"run_id": run, "subject": SUBJECT, "generation": 1, "allowance": 3})
        with kernels[run].storage.transaction() as tx:
            assert kernels[run]._observe_integration_snapshot(tx, kernels[run]._job(tx, SUBJECT), WORLD)
    before_b = _rows(db, "B")
    request = _prepare(kernels["A"], "A", operation="integrate", payload={"source_sha": BASE.sha})
    assert kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    kernels["A"]._reconcile(request, EffectResult("target", commit=BASE, expected_tree=BASE.tree))
    _consume(kernels["A"], request.effect)
    assert db.execute("SELECT open FROM integration_snapshots WHERE run_id='A'").fetchone()[0] == 0
    assert _rows(db, "B") == before_b

    appended = replace(WORLD, target=Commit("c" * 40, "d" * 40), ancestry=((BASE.sha, "c" * 40),))
    with kernels["A"].storage.transaction() as tx:
        assert kernels["A"]._observe_integration_snapshot(tx, kernels["A"]._job(tx, SUBJECT), appended)
    assert db.execute("SELECT used FROM resync_allowances WHERE run_id='A'").fetchone()[0] == 1
    assert db.execute("SELECT window FROM integration_snapshots WHERE run_id='A' ORDER BY window").fetchall()[1][0] == 2
    assert _rows(db, "B") == before_b

def test_stale_checkout_receipt_records_only_owned_cleanup_without_authorizing_new_work(scenario):
    db, kernels = scenario
    a, b = [_prepare(kernels[run], run, operation="validation_checkout", payload={"reviewed_commit": BASE.sha})
        for run in ("A", "B")]
    assert kernels["A"]._dispatch(a.effect, a.work.execution, WORLD, token=a.token)
    kernels["A"]._reconcile(a, EffectResult("target", commit=BASE))
    before_b = _rows(db, "B")
    with kernels["A"].storage.transaction() as tx:
        row = tx.one("SELECT * FROM effects WHERE run_id='A'")
        assert kernels["A"]._settle_stale_effect(tx, kernels["A"]._job(tx, SUBJECT), row).reason == "original_outcome_retained"
    assert db.execute("SELECT validation_checkout,validation_cleanup,phase FROM subjects WHERE run_id='A'").fetchone()[:] == (
        None, "/fixture/review-same-effect", "review")
    assert db.execute("SELECT status FROM effects WHERE run_id='A'").fetchone()[0] == "confirmed"
    assert _rows(db, "B") == before_b


def test_controlled_effect_identity_and_inspection_receipt_are_owned_by_original_action_run(scenario):
    db, kernels = scenario
    for run in ("A", "B"):
        with kernels[run].storage.transaction() as tx:
            payload = tx.put(SUBJECT, "control_intent", b"{}")
            receipt = tx.put(SUBJECT, "control_receipt", wire_json({"abandonment": {
                "unsettled": [], "resources": [], "generations": [[SUBJECT, 1]], "discard_worktree": False,
                "execution": asdict(_work(run).execution)}}))
            tx.execute("INSERT INTO actions(id,run_id,kind,subject,payload_id,status,receipt_id) VALUES (?,?,'abandon',?,?,'pending',?)",
                ("same-action", run, SUBJECT, payload, receipt))
            tx.execute("UPDATE runs SET epoch=2 WHERE id=?", (run,))
    before_b = _rows(db, "B")
    with kernels["A"].storage.transaction() as tx:
        run = tx.one("SELECT * FROM runs WHERE id='A'")
        action = kernels["A"]._abandon_action(tx)
        request = kernels["A"]._prepare_abandon_effect(tx, run, action, SUBJECT, 1, _work("A"), WORLD,
            "inspect_resources", before=BASE.sha, payload={"resources": [], "discard_worktree": False})
        assert request.effect.payload["control_action"] == "same-action"
        assert len(kernels["A"]._abandon_effects(tx, action)) == 1
        foreign = tx.one("SELECT * FROM actions WHERE run_id='B'")
        with pytest.raises(KernelError, match="effect_binding"):
            kernels["A"]._abandon_effects(tx, foreign)
    assert kernels["A"]._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
    kernels["A"]._reconcile(request, EffectResult("target", raw={"resources": []}))
    with kernels["A"].storage.transaction(write=False) as tx:
        action = kernels["A"]._abandon_action(tx)
        observed = kernels["A"]._abandon_inspection(tx, action)
        assert observed["run_id"] == "A" and observed["status"] == "observed"
        kernels["A"]._check_abandon_inspection(tx, action, observed)
    assert _rows(db, "B") == before_b


def test_ticket_completion_rows_cleanup_and_final_action_are_scoped_to_original_effects(scenario, monkeypatch):
    """Consumer seam with persisted prior delivery facts, not a Ticket E2E claim."""
    from support.repository_scope import add_subject
    db, kernels = scenario
    subject = "ticket:2"
    monkeypatch.setattr(core, "identity", iter(["comment", "close", "comment", "close"]).__next__)
    for run in ("A", "B"):
        with kernels[run].storage.transaction() as tx:
            add_subject(db, run, subject)
            tx.execute("UPDATE subjects SET kind='ticket',issue=2,phase='completion_comment',publication_sha=?,publication_tree=?,merge_sha=?,pr_number=2,gate_mode='ci_waiver',owned_root=?,common_git_dir=?,checkout=?,workspace_sha=? WHERE run_id=? AND id=?",
                (BASE.sha, BASE.tree, BASE.sha, '/fixture/owned-' + run, '/fixture/common-' + run,
                 '/fixture/ticket-' + run, BASE.sha, run, subject))
            tx.execute("INSERT INTO source_observations(run_id,parent_revision,graph_revision,body) VALUES (?,'r','g',?)", (run, b'{}'))
            sequence = tx.one("SELECT sequence FROM source_observations WHERE run_id=?", (run,))[0]
            tx.execute("UPDATE runs SET source_sequence=? WHERE id=?", (sequence, run))
            context = tx.put(subject, "role_context", b'{}')
            insert(db, "attempts", {"run_id": run, "id": "prior-development", "subject": subject,
                "generation": 1, "ordinal": 1, "role": "development", "purpose": "requirements", "window": 1,
                "parent_revision": "r", "graph_revision": "g", "revision": "r", "base_sha": BASE.sha,
                "completion_revision": "no-tickets", "context_id": context, "status": "consumed"})
            insert(db, "actions", {"run_id": run, "id": "same-final-action", "kind": "approve", "subject": subject, "status": "pending"})
            tx.execute("UPDATE runs SET final_action='same-final-action' WHERE id=?", (run,))
    for run in ("A", "B"):
        sibling = "B" if run == "A" else "A"
        before = _rows(db, sibling)
        kernel = kernels[run]
        work = replace(_work(run), subject=subject)
        intent = None
        for operation in ("completion_comment", "close"):
            with kernel.storage.transaction() as tx:
                header, job = kernel._owned(tx, subject, work.execution)
                request = kernel._prepare_effect(tx, header, job, work, WORLD, operation, before=BASE.sha,
                    payload={"issue": 2} if intent is None else {"close_intent": intent})
            assert kernel._dispatch(request.effect, work.execution, WORLD, token=request.token)
            if operation == "completion_comment":
                intent = {"run_id": run, "subject": subject, "issue": 2, "merge_sha": BASE.sha,
                    "actor": WORLD.publisher_actor, "effect_id": "comment", "pr_number": 2, "kind": "ticket",
                    "baseline_event_id": 0, "comment_id": 101, "comment_created_at": "2026-10-26T00:00:00Z",
                    "comment_body": "Original " + run}
                result = EffectResult("target", raw={"close_intent": intent})
            else:
                result = EffectResult("target")
            kernel._reconcile(request, result)
            with kernel.storage.transaction() as tx:
                header, job = kernel._owned(tx, subject, work.execution)
                row = tx.one("SELECT * FROM effects WHERE run_id=? AND id=?", (run, request.effect.effect_id))
                kernel._apply_effect(tx, header, job, row, WORLD)
        record = db.execute("SELECT * FROM completions WHERE run_id=?", (run,)).fetchone()
        assert (record['subject'], record['comment_effect'], record['close_effect'], record['development_attempt']) == (
            subject, 'comment', 'close', 'prior-development')
        assert db.execute("SELECT status FROM actions WHERE run_id=?", (run,)).fetchone()[0] == 'complete'
        assert db.execute("SELECT count(*) FROM resource_cleanup WHERE run_id=?", (run,)).fetchone()[0] == 1
        assert _rows(db, sibling) == before
    assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_publisher_actor_is_frozen_per_run_and_sibling_actor_cannot_fence_it(scenario):
    db, kernels = scenario
    _prepare(kernels['B'], 'B', operation='push', after=BASE.sha,
        world=replace(WORLD, publisher_actor='original-B-actor'))
    before_b = _rows(db, 'B')
    request = _prepare(kernels['A'], 'A', operation='push', after=BASE.sha)
    assert request.effect.actor == 'controlled-actor' and _rows(db, 'B') == before_b
    before_a = _rows(db, 'A')
    with pytest.raises(KernelError, match='publisher_identity_changed'):
        with kernels['A'].storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs WHERE id='A'")
            kernels['A']._publisher_actor(tx, run, replace(WORLD, publisher_actor='changed-A-actor'))
    assert _rows(db, 'A') == before_a and _rows(db, 'B') == before_b


def test_reopen_committed_unknown_does_not_allocate_or_replay_the_effect(scenario, tmp_path):
    """Real file/handles, not public file admission or COMMIT-ack-loss proof."""
    initial, _ = scenario
    path = tmp_path / 'repository.sqlite3'
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        initial.backup(db)  # Fixture prerequisites only; all Effect writes start below.
        kernel = ScopedKernel(db, 'A')
        request = _prepare(kernel, 'A', operation='push', after=BASE.sha)
        assert kernel._dispatch(request.effect, request.work.execution, WORLD, token=request.token)
        assert kernel._reconcile(request, EffectResult('unknown')).kind == 'paused'
        payload = request.effect.payload
        before_b = _rows(db, 'B')
    with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        kernel = ScopedKernel(db, 'A')
        recovered = _request(kernel, 'A')
        assert recovered.kind == 'readback' and recovered.effect.dispatch_serial == 1
        assert recovered.effect.payload == payload
        assert not kernel._dispatch(recovered.effect, recovered.work.execution, WORLD, token=recovered.token)
        assert db.execute("SELECT count(*) FROM effects WHERE run_id='A'").fetchone()[0] == 1
        assert db.execute("SELECT attempts,status FROM effects WHERE run_id='A'").fetchone()[:] == (1, 'sent')
        assert kernel._reconcile(recovered, EffectResult('target', commit=BASE)).kind == 'progress'
        assert db.execute("SELECT attempts,status FROM effects WHERE run_id='A'").fetchone()[:] == (1, 'observed')
        assert _rows(db, 'B') == before_b
