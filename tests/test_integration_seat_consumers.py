"""Seat release rules consume declared conflict facts on real scoped SQLite.

The fixture is not a Git conflict or process-exit proof. Allocation, control,
raw consumption and the waiting Ticket's seat acquisition are real Core code.
"""
from contextlib import closing
from dataclasses import asdict

import pytest

from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import Work
from agent_run.errors import KernelError
from agent_run.model import Commit, Execution, Policy, ResourceInspection, World, wire_json
from agent_run.storage.run_scope import RunTransaction
from agent_run.storage.sqlite import Transaction
from support.core_operations import prepare_scripted_agent
from support.offline import fixture_inputs
from support.repository_scope import add_subject, connection, insert, seed
from support.run_scoped_kernel import ScopedKernel


RUN, REPAIR, WAITER = "conflict", "ticket:2", "ticket:3"
HEAD = Commit("a" * 40, "b" * 40)
TARGET = Commit("c" * 40, "d" * 40)
WORLD = World("r", "g", "r", TARGET, HEAD)
EXECUTION = Execution(RUN, "owner", 1)


@pytest.fixture
def conflict():
    with closing(connection()) as db:
        db.execute("BEGIN")
        seed(db, RUN, subject=REPAIR)
        add_subject(db, RUN, WAITER)
        tx = RunTransaction(Transaction(db), RUN)
        inputs = fixture_inputs()
        profile = tx.put(REPAIR, "profile", wire_json(inputs.profiles))
        db.execute("UPDATE profile_revisions SET material_id=? WHERE run_id=?", (profile, RUN))
        frozen = asdict(inputs)
        del frozen["profiles"]
        material = tx.put(REPAIR, "creation_inputs", wire_json(frozen))
        insert(db, "run_inputs", {"run_id": RUN, "subject": REPAIR, "material_id": material})
        db.execute("UPDATE runs SET executor=?,epoch=1,concurrency=2,integration_holder=?,policy=? WHERE id=?",
                   (EXECUTION.owner, REPAIR, wire_json(Kernel._policy_values(Policy(concurrency=2))), RUN))
        failure = tx.put(REPAIR, "declared_integration_failure", wire_json({
            "disposition": "failure", "commit": asdict(HEAD),
            "raw": {"merge_conflict": True, "head": HEAD.sha, "merge_head": TARGET.sha}}))
        for number, subject in ((2, REPAIR), (3, WAITER)):
            db.execute("UPDATE subjects SET kind='ticket',issue=?,phase=?,purpose='integration',"
                       "checkout=?,owned_root='/fixture',common_git_dir='/fixture/.git',"
                       "source_ref=?,target_ref='refs/heads/run',workspace_sha=?,candidate_sha=?,candidate_tree=? "
                       "WHERE run_id=? AND id=?",
                       (number, "development" if subject == REPAIR else "integration",
                        f"/fixture/{number}", f"refs/heads/ticket-{number}", HEAD.sha, HEAD.sha, HEAD.tree,
                        RUN, subject))
            for purpose in ("requirements", "integration", "ci"):
                insert(db, "budgets", {"run_id": RUN, "subject": subject, "purpose": purpose,
                    "window": 1, "development_limit": 3, "review_limit": 3, "review_required": 1})
        db.execute("UPDATE subjects SET repair_source='integration',last_failure=? WHERE run_id=? AND id=?",
                   (failure, RUN, REPAIR))
        insert(db, "integration_queue", {"run_id": RUN, "subject": WAITER, "ready": 1})
        db.commit()
        kernel = ScopedKernel(db, RUN)
        work = Work(EXECUTION, "consumer", REPAIR, False, True, None, None)
        with kernel.storage.transaction() as tx:
            run, job = kernel._owned(tx, REPAIR, EXECUTION)
            preparation = kernel._allocate(tx, run, job, work, WORLD)
        call = prepare_scripted_agent(kernel, preparation).call
        assert call is not None and call.context["integration_evidence"]["raw"]["merge_conflict"] is True
        with kernel.storage.transaction() as tx:
            run, waiter = kernel._owned(tx, WAITER, EXECUTION)
            assert kernel._integration_seat(tx, run, waiter) is False
        yield db, kernel, work, call


@pytest.mark.parametrize("boundary", ["stop", "human", "unavailable", "abandon"])
def test_definite_pause_releases_the_original_conflict_seat(conflict, boundary):
    db, kernel, work, call = conflict
    original_failure = db.execute("SELECT last_failure FROM subjects WHERE run_id=? AND id=?", (RUN, REPAIR)).fetchone()[0]
    assert db.execute("SELECT integration_holder FROM runs WHERE id=?", (RUN,)).fetchone()[0] == REPAIR
    if boundary == "stop":
        assert kernel.control("stop", "stop-conflict", subject=REPAIR) == "applied"
        kernel.agent_unavailable(call, "controlled_unstarted_cancel", {}, before_dispatch=True)
    elif boundary == "unavailable":
        kernel.agent_unavailable(call, "controlled_unstarted_failure", {}, before_dispatch=True)
    elif boundary == "abandon":
        inspections = tuple(ResourceInspection(resource, "dirty", resource.head_sha)
                            for resource in kernel.generation_resources())
        assert kernel.control("abandon", "abandon-conflict", discard_worktree=True,
                              resource_inspections=inspections) == "pending"
        kernel.agent_unavailable(call, "controlled_unstarted_cancel", {}, before_dispatch=True)
        with pytest.raises(KernelError, match="executor_fenced"):
            kernel.guard_agent(call)
    else:
        kernel.receive(call, wire_json({"result_kind": "human_blocker", "summary": None,
                                       "human_blockers": ["Operator resolution is needed"]}))
        with kernel.storage.transaction() as tx:
            run, job = kernel._owned(tx, REPAIR, EXECUTION)
            attempt = tx.one("SELECT * FROM attempts WHERE run_id=? AND id=?", (RUN, call.attempt_id))
            result = kernel._consume(tx, run, job, attempt, WORLD, work)
        assert (result.kind, result.reason) == ("paused", "human_blocker")
    assert db.execute("SELECT integration_holder FROM runs WHERE id=?", (RUN,)).fetchone()[0] is None
    assert db.execute("SELECT count(*) FROM leases WHERE run_id=?", (RUN,)).fetchone()[0] == 0
    assert db.execute("SELECT developments_used FROM budgets WHERE run_id=? AND subject=? AND purpose='integration'",
                      (RUN, REPAIR)).fetchone()[0] == int(boundary == "human")
    assert db.execute("SELECT last_failure FROM subjects WHERE run_id=? AND id=?", (RUN, REPAIR)).fetchone()[0] == original_failure
    assert db.execute("SELECT count(*) FROM effects WHERE run_id=?", (RUN,)).fetchone()[0] == 0
    if boundary == "abandon":
        assert db.execute("SELECT count(*) FROM integration_queue WHERE run_id=?", (RUN,)).fetchone()[0] == 0
        with pytest.raises(KernelError, match="executor_fenced"):
            with kernel.storage.transaction() as tx:
                kernel._owned(tx, WAITER, EXECUTION)
    else:
        reason = {"stop": "operator_stopped", "human": "human_blocker", "unavailable": "worker_unavailable"}[boundary]
        assert db.execute("SELECT code FROM gates WHERE run_id=? AND subject=?", (RUN, REPAIR)).fetchone()[0] == reason
        with kernel.storage.transaction() as tx:
            run, waiter = kernel._owned(tx, WAITER, EXECUTION)
            assert kernel._integration_seat(tx, run, waiter) is True
        assert db.execute("SELECT integration_holder FROM runs WHERE id=?", (RUN,)).fetchone()[0] == WAITER
    assert db.execute("PRAGMA foreign_key_check").fetchone() is None
