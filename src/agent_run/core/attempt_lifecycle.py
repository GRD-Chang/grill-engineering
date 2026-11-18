"""Attempt-owned continuation decisions and atomic Invocation admission.

The Core supplies a transaction and the current work facts. This module does
not select work, authorize an executor, charge budgets or call the controller.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Mapping

from agent_run.core.attempt_continuation import ContinuationFacts, load_continuation
from agent_run.errors import KernelError
from agent_run.model import AgentCall, AgentPreparation
from agent_run.storage.run_scope import run_statement, transaction_scope
from agent_run.storage.sqlite import Transaction


@dataclass(frozen=True)
class WorkerFailurePolicy:
    recovery_kind: str | None
    delay_seconds: float = 0
    terminal: bool = False


_FAILURE_POLICIES = {
    "ordinary": WorkerFailurePolicy("ordinary"),
    "capacity": WorkerFailurePolicy("capacity", delay_seconds=30),
    "fatal": WorkerFailurePolicy(None, terminal=True),
}


def worker_failure_policy(kind: str) -> WorkerFailurePolicy:
    """Map physical classifications to the existing bounded recovery families."""
    try:
        return _FAILURE_POLICIES[kind]
    except KeyError:
        raise KernelError("invalid_failure_kind") from None


@dataclass(frozen=True)
class ContinuationPlan:
    kind: str = "allocate"
    reason: str | None = None
    gate: str | None = None
    evidence: str | None = None
    recovery_kind: str | None = None
    retain_period: bool = False
    retain_deadline: bool = False
    output_repair: bool = False


@dataclass(frozen=True)
class AttemptWait:
    reason: str
    retry_at: float | None = None


def continuation_wait(facts: ContinuationFacts | None, *, explicit: bool,
                      leased: bool, now: float) -> AttemptWait | None:
    prior = None if facts is None else facts.prior
    if prior is not None:
        if prior.status != "running" and prior.dispatch_state in {"sent", "started", "unknown"}:
            return AttemptWait("previous_invocation_exit")
        if prior.status == "running" and not (prior.dispatch_state == "allocated" and now >= prior.deadline_at):
            return AttemptWait("agent_running")
        if prior.recovery_ready_at is not None and now < prior.recovery_ready_at:
            deadline = facts.semantic_deadline if facts is not None and not explicit else None
            if deadline is None or now < deadline:
                return AttemptWait("capacity_recovery_wait", prior.recovery_ready_at if deadline is None
                                   else min(deadline, prior.recovery_ready_at))
    if leased and not (prior is not None and prior.dispatch_state == "allocated" and now >= prior.deadline_at):
        return AttemptWait("previous_invocation_exit")
    return None


class AttemptLifecycle:
    """One transaction's exact Attempt and physical continuation facts."""

    def __init__(self, tx: Transaction, attempt: sqlite3.Row,
                 facts: ContinuationFacts | None = None) -> None:
        if attempt["run_id"] != transaction_scope(tx).run_id:
            raise KernelError("invocation_binding")
        self.attempt = attempt
        self.facts = facts if facts is not None else load_continuation(tx, attempt["id"])
        self.backend = tx.one(*run_statement(tx, 'SELECT backend_id FROM threads WHERE id=? AND run_id=?',
                              (self.facts.thread_id,), run_last=True))[0]

    def recover(self, now: float, *, leased: bool) -> ContinuationPlan:
        previous = self.facts.prior
        assert previous is not None
        if leased:
            return ContinuationPlan("waiting", "previous_invocation_exit")
        try:
            policy = worker_failure_policy(previous.failure_kind or "")
        except KernelError:
            policy = WorkerFailurePolicy(None, terminal=True)
        kind = policy.recovery_kind
        if (previous.dispatch_state != "exited" or previous.process_identity is None or self.backend is None
                or kind is None or kind == "ordinary" and self.facts.ordinary_used):
            return ContinuationPlan("paused", "execution_recovery_unavailable", "execution_failed", previous.start_evidence)
        if self.facts.semantic_deadline is not None and now >= self.facts.semantic_deadline:
            return ContinuationPlan("paused", "invocation_deadline_exceeded", "execution_failed", previous.start_evidence)
        if kind == "capacity" and (previous.recovery_ready_at is None or now < previous.recovery_ready_at):
            return ContinuationPlan("waiting", "capacity_recovery_wait")
        return ContinuationPlan(recovery_kind=kind)

    def incomplete(self, now: float) -> ContinuationPlan:
        prior, facts, attempt = self.facts.prior, self.facts, self.attempt
        reason = None
        if (prior is None or prior.raw_id != attempt["result_id"] or prior.dispatch_state != "exited"
                or prior.exit_code != 0 or prior.process_identity is None or not self.backend):
            reason = "semantic_continuation_unavailable"
        elif attempt["resume_action"] is not None:
            return ContinuationPlan()
        elif facts.context.get("readonly") or facts.output_ordinal:
            reason = "semantic_incomplete_readonly"
        elif facts.semantic_used:
            reason = "semantic_incomplete_limit"
        elif now >= prior.deadline_at:
            reason = "invocation_deadline_exceeded"
        if reason is not None:
            return ContinuationPlan("paused", reason, "execution_failed")
        return ContinuationPlan(recovery_kind="semantic", retain_deadline=True)

    def repair_output(self, now: float, *, leased: bool) -> ContinuationPlan:
        previous, facts, attempt = self.facts.prior, self.facts, self.attempt
        if previous is None:
            return ContinuationPlan("paused", "continuation_exit_missing")
        if facts.semantic_deadline is not None and now >= facts.semantic_deadline and attempt["resume_action"] is None:
            return ContinuationPlan("paused", "invocation_deadline_exceeded", "execution_failed", previous.start_evidence)
        if previous.recovery_ready_at is not None and now < previous.recovery_ready_at:
            return ContinuationPlan("waiting", "capacity_recovery_wait")
        if attempt["resume_action"] is not None and previous.failure_kind is not None:
            return ContinuationPlan()
        if previous.exit_code != 0 and previous.failure_kind is not None:
            return self.recover(now, leased=leased)
        if (previous.dispatch_state != "exited" or previous.exit_code != 0 or previous.process_identity is None
                or self.backend is None or facts.output_ordinal >= 2):
            return ContinuationPlan("paused", "invalid_artifact", "invalid_artifact", attempt["result_id"])
        if now >= previous.deadline_at:
            if attempt["resume_action"] is not None:
                return ContinuationPlan(output_repair=True)
            return ContinuationPlan("paused", "invocation_deadline_exceeded", "execution_failed", attempt["result_id"])
        return ContinuationPlan(output_repair=True, retain_period=True)


def inherited_period(facts: ContinuationFacts | None, *, explicit: bool,
                     retain_period: bool, retain_deadline: bool, now: float) -> bool:
    inherited = retain_period or retain_deadline or facts is not None and facts.semantic_deadline is not None and not explicit
    previous = None if facts is None else facts.prior
    if inherited and previous is None:
        raise KernelError("continuation_deadline_missing")
    if inherited and previous is not None and now >= previous.deadline_at:
        raise KernelError("invocation_deadline_exceeded")
    return inherited


def admit_preparation(tx: Transaction, preparation: AgentPreparation, data: Mapping[str, Any],
                      scope_id: str, *, now: float, new_invocation_id: str) -> AgentCall:
    """Transfer the reserved seat to one Invocation, output and lease atomically.

    The caller checks current executor/work authority in this same transaction.
    All identities and deadlines are the original durable preparation's values.
    """
    attempt_id, subject, execution = preparation.attempt_id, preparation.subject, preparation.execution
    if execution.run_id != transaction_scope(tx).run_id:
        raise KernelError("preparation_binding")
    invocation_id = data["previous_invocation"] if data["owned_period"] else new_invocation_id
    deadline = data["retained_deadline"] if data["retained_deadline"] is not None else now + preparation.deadline_seconds
    if now >= deadline:
        raise KernelError("invocation_deadline_exceeded")
    if data["owned_period"]:
        tx.execute(*run_statement(tx, "UPDATE invocations SET status='running' WHERE id=? AND run_id=?",
                   (invocation_id,), run_last=True))
    else:
        kind = data["recovery_kind"] or ("explicit" if data["resume_action"] is not None else None)
        tx.execute(*run_statement(tx, "INSERT INTO invocations(id,attempt,thread,executor,epoch,status,allocated_at,deadline_seconds,deadline_at,predecessor,recovery_kind,action_id,run_id) VALUES (?,?,?,?,?,'running',?,?,?,?,?,?,?)",
                   (invocation_id, attempt_id, preparation.thread_id, execution.owner, execution.epoch, now,
                    preparation.deadline_seconds, deadline, data["previous_invocation"], kind, data["resume_action"]), run_last=True))
    tx.execute(*run_statement(tx, 'INSERT INTO invocation_outputs(invocation,ordinal,context_id,lease,allocated_at,mode,requested_thread,run_id) VALUES (?,?,?,?,?,?,?,?)',
               (invocation_id, preparation.output_ordinal, data["context_id"], preparation.reservation_id, now,
                preparation.mode, preparation.reported_thread), run_last=True))
    tx.execute(*run_statement(tx, 'INSERT INTO leases(id,subject,invocation,executor,epoch,run_id) VALUES (?,?,?,?,?,?)',
               (preparation.reservation_id, subject, invocation_id, execution.owner, execution.epoch), run_last=True))
    tx.execute(*run_statement(tx, "DELETE FROM waits WHERE scope_id=? AND kind='agent_preparation' AND run_id=?",
               (scope_id,), run_last=True))
    tx.execute(*run_statement(tx, "UPDATE attempts SET current_invocation=?,thread_id=?,context_id=?,result_id=NULL,status='running',new_thread=0,resume_action=NULL WHERE id=? AND run_id=?",
               (invocation_id, preparation.thread_id, data["context_id"], attempt_id), run_last=True))
    return AgentCall(attempt_id, invocation_id, subject, preparation.role, preparation.purpose,
                     preparation.thread_id, preparation.reservation_id, execution, preparation.context, preparation.profile,
                     preparation.deadline_seconds, deadline, preparation.mode, preparation.reported_thread,
                     preparation.output_ordinal, preparation.profile_revision, preparation.profile_reference)
