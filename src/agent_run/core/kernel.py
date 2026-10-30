"""One authoritative transaction interpreter for delivery work.

All transition, currentness, budget, FIFO and mutation-authority choices are
made here from current SQL facts. The external driver only executes the one
returned request. A raw return and its business consumption commit separately.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import cached_property
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterator, Literal, Mapping
from threading import RLock
from uuid import uuid4

from agent_run.contracts.agent_profiles import ProfileOverrides, resolve_profiles, validate_profile_options
from agent_run.contracts.identity import validate_identity_snapshot
from agent_run.contracts.pr_observation import pr_target
from agent_run.contracts.repository import same_repository
from agent_run.contracts.required_checks import same_required_checks
from agent_run.contracts.prompt import validate_resources
from agent_run.contracts.task import TaskKey

from agent_run.model import (
    AgentCall, AgentPreparation, Checks, Commit, CreationInputs, DeliveryKind, Effect, EffectResult, Execution, HostLaunch, HostObservation,
    Delivery, Policy, Profile, PullRequest, Purpose, Requirements, ResourceInspection, ResourceWitness, Role, RunSpec, Step, World, ReadWait, artifact, inspection_wire, resource_wire, review_outcome, wire_json,
)
from agent_run.errors import KernelError, ObservationSuperseded, bounded_error
from agent_run.storage.sqlite import CURRENT_LIMIT, Storage, Transaction
from agent_run.storage.run_scope import RunTransaction, run_statement, transaction_scope
from agent_run.storage.run_paths import RunPaths, run_paths
from agent_run.storage.routing import open_run
from agent_run.core.attempt_continuation import (AttemptContinuationBasis, ContinuationFacts,
                                   bind_attempt_continuation, load_continuation)
from agent_run.contracts.policy_values import checkpoint_policy
from agent_run.contracts.policy import AGENT_PREPARATION_SECONDS
from agent_run.storage.tasks import task_lock, run_is_current
from agent_run.core.operation_contract import (OperationBinding, OperationState, OperationSupervision, ReadUnavailable, ReadFence,
                                 RequirementsRead, LocalCleanupRead, LocalCleanupObservation, DeliveryObservation, DeliveryRead, DeliveryFacts,
                                 _SentRead, _ApprovalRead, _ObservationPass, Work, PrepareIO, EffectIO, ObserveIO, IORequest, IOResult,
                                 requested_ancestry)


def identity() -> str:
    return uuid4().hex


class _SupervisionWriteRequired(Exception):
    """The original expired read window needs a fresh ordered writer check."""


@dataclass(frozen=True)
class InspectionBindings:
    """Ephemeral readonly bindings, never a saved state or execution grant."""
    current: tuple[str, str | None, int]
    request: HostLaunch | None
    host: Mapping[str, Any] | None
    workers: tuple[tuple[AgentCall, Mapping[str, Any]], ...]


class _OperationSnapshot:
    """One call's current transaction facts; never a grant or an IO payload.

    Helpers share these explicit reads, not individual caches. A writer must
    construct a new view after DML; even unrelated DML conservatively ends it.
    """
    def __init__(self, kernel: Kernel, tx: Transaction, run: sqlite3.Row) -> None:
        self.kernel, self.tx, self.run = kernel, tx, run
        self.changes = tx.connection.total_changes
        self.subjects: dict[str, _SubjectSnapshot] = {}
        self.require(tx)

    def require(self, tx: Transaction) -> None:
        try:
            valid = (tx is self.tx and tx.connection.in_transaction
                     and tx.connection.total_changes == self.changes and not tx.failed)
        except sqlite3.Error as error:
            raise KernelError('operation_snapshot_closed') from error
        if not valid:
            raise KernelError('operation_snapshot_changed')
        if transaction_scope(tx).run_id != self.run['id']:
            raise KernelError('operation_snapshot_binding')

    def subject(self, subject: str, job: sqlite3.Row | None = None) -> _SubjectSnapshot:
        self.require(self.tx)
        if subject not in self.subjects:
            job = self.kernel._job(self.tx, subject) if job is None else job
            if job['id'] != subject or job['run_id'] != self.run['id']:
                raise KernelError('operation_snapshot_binding')
            if sum(len(value.encode()) if isinstance(value, str) else 16 for value in job) > CURRENT_LIMIT:
                raise KernelError('current_record_limit')
            self.subjects[subject] = _SubjectSnapshot(self, job)
        return self.subjects[subject]

    @cached_property
    def abandoned(self) -> sqlite3.Row | None:
        return self.kernel._abandon_action(self.tx)


class _SubjectSnapshot:
    """Demand-loaded subject facts belonging solely to an OperationSnapshot."""
    def __init__(self, snapshot: _OperationSnapshot, job: sqlite3.Row) -> None:
        self.snapshot, self.job = snapshot, job

    def require(self, tx: Transaction, job: sqlite3.Row) -> None:
        self.snapshot.require(tx)
        if job is not self.job:
            raise KernelError('operation_snapshot_binding')

    @cached_property
    def pending(self) -> sqlite3.Row | None:
        return self.snapshot.kernel._pending_effect(self.snapshot.tx, self.job['id'])

    @cached_property
    def gates(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.snapshot.kernel._gates(self.snapshot.tx, self.job['id']))

    @cached_property
    def requeue(self) -> sqlite3.Row | None:
        return self.snapshot.kernel._pending_requeue(self.snapshot.tx, self.job['id'])

    @cached_property
    def attempt(self) -> sqlite3.Row | None:
        if not self.job['current_attempt']:
            return None
        tx = self.snapshot.tx
        return tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                                    (self.job['current_attempt'],), run_last=True))

    @cached_property
    def continuation(self) -> ContinuationFacts | None:
        attempt = self.attempt
        return (load_continuation(self.snapshot.tx, attempt['id']) if attempt is not None
                and (attempt['thread_id'] is not None or attempt['opening_id'] is not None) else None)

    @cached_property
    def preparing(self) -> bool:
        tx = self.snapshot.tx
        return bool(tx.rows(*run_statement(tx,
            "SELECT scope_id FROM waits WHERE subject=? AND kind='agent_preparation' AND run_id=?",
            (self.job['id'],), run_last=True)))

    @cached_property
    def leased(self) -> bool:
        tx = self.snapshot.tx
        return bool(tx.rows(*run_statement(tx, 'SELECT id FROM leases WHERE subject=? AND run_id=?',
                                         (self.job['id'],), run_last=True)))

    @property
    def waiting(self) -> Step | None:
        # Time may advance inside one SQLite snapshot; reuse facts, not decisions.
        return self.snapshot.kernel._continuation_wait(self.snapshot.tx, self.job, read=self)


class Kernel:
    LOCAL_PREPARATION = ("candidate", "validation_checkout", "validation_cleanup")
    CLEANUP = ("cleanup_worktree", "cleanup_local_ref", "cleanup_remote_ref")
    ABANDON_EFFECTS = ("inspect_resources", "retire_pr", "reopen_ticket", "recovery_comment", "abandon_worktree")
    REMOTE = ("push", "run_branch", "pr_write", "merge", "completion_comment", "close", "issue_link", "cleanup_remote_ref", "retire_pr", "reopen_ticket", "recovery_comment")
    def __init__(self, path: Path, *, run_id: str) -> None:
        self._attach_storage(open_run(path, run_id=run_id))

    def _attach_storage(self, storage: Storage) -> None:
        self.storage = storage
        self.clock: Callable[[], float] = time.time
        self._observation: _ObservationPass | None = None
        self._observation_lock = RLock()

    @classmethod
    def _from_storage(cls, storage: Storage) -> Kernel:
        """Attach the existing explicit Storage owner; no new transaction or authority."""
        kernel = cls.__new__(cls)
        kernel._attach_storage(storage)
        return kernel

    def run_paths(self, *, expected_run: str | None = None) -> RunPaths:
        """Derive file locations from selected facts, granting no execution."""
        with self.storage.transaction(write=False) as tx:
            return run_paths(tx, self.storage.path, expected_run=expected_run)

    def operation_binding(self, execution: Execution | None = None) -> OperationBinding:
        """Frozen physical configuration; no source refresh or work admission."""
        with self.storage.transaction(write=False) as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            if execution is not None:
                self._automatic_owned(tx, run, execution)
            if run["source_sequence"] is None:
                raise KernelError("source_not_configured")
            inputs = CreationInputs(profiles=self._profile_document(tx, 1), **self._inputs(tx, run))
            if (not run["repository_root"] or not Path(run["repository_root"]).is_absolute()
                    or Path(run["repository_root"]).resolve() != Path(run["repository_root"]) or not inputs.repository_remote):
                raise KernelError("managed_repository_required")
            paths, variables = [self.storage.path.parent], []
            for identity in (inputs.runner_identity, inputs.worker_identity):
                pat, app = identity.get("pat", {}), identity.get("app_profile", {})
                if pat.get("file"):
                    paths.append(Path(pat["file"]))
                if pat.get("env"):
                    variables.append(pat["env"])
                if app.get("private_key_path"):
                    paths.append(Path(app["private_key_path"]))
            return OperationBinding(Path(run["repository_root"]), run["repository"], run["parent_issue"], inputs,
                                    tuple(paths), tuple(variables))

    def _read_fence(self, tx: Transaction, subject: str | None, execution: Execution | None, cleanup: bool) -> ReadFence:
        selected = transaction_scope(tx)
        if execution is not None and execution.run_id != selected.run_id:
            raise KernelError("effect_binding")
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                  (), run_last=True))
        return ReadFence(execution, subject, cleanup, wire_json(self._automatic_scope(tx, run, subject, cleanup)),
                         run["source_sequence"], tuple(run[key] for key in ("executor", "epoch", "stopped", "blocked", "final_action")))

    def begin_observation(self, subject: str | None, *, execution: Execution | None = None,
                          cleanup: bool = False, call: AgentCall | None = None) -> RequirementsRead:
        return self._begin_observation(subject, execution=execution, cleanup=cleanup, call=call)

    def _begin_observation(self, subject: str | None, *, execution: Execution | None = None,
                           cleanup: bool = False, call: AgentCall | None = None,
                           delivery: bool = True, local_effect: Effect | None = None) -> RequirementsRead:
        with self.storage.transaction(write=False) as tx:
            if call is not None:
                if (call.subject, call.execution) != (subject, execution):
                    raise KernelError("invocation_binding")
                self._agent_authority(tx, call)
            if execution is not None:
                self._automatic_owned(tx, tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                              (), run_last=True)), execution)
            fence = replace(self._read_fence(tx, subject, execution, cleanup), local_effect=local_effect)
            parent = tx.one(*run_statement(tx, 'SELECT parent_issue FROM runs WHERE id=?',
                         (), run_last=True))[0]
            query = self._delivery_read(tx, fence) if delivery else None
        if execution is not None:
            waiting = self._guard_automatic_step(fence, execution)
            if waiting is not None:
                raise ReadWait(waiting)
        return RequirementsRead(fence, parent, query)

    def _delivery_read(self, tx: Transaction, fence: ReadFence) -> DeliveryRead:
        """Project complete physical demand from this transaction's facts."""
        if fence.subject is None:
            job = tx.one(*run_statement(tx, 'SELECT * FROM final_target WHERE run_id=?',
                      (), run_last=True))
            anchors = set(self._final_ancestry_anchors(tx))
        else:
            job = self._job(tx, fence.subject)
            anchors = {job[key] for key in ("base_sha", "source_sha", "candidate_sha", "integrated_sha") if job[key] is not None}
            for row in tx.rows(*run_statement(tx, 'SELECT source_sha,target_sha FROM integration_snapshots WHERE subject=? AND run_id=? ORDER BY window DESC LIMIT 1',
                           (fence.subject,), run_last=True)):
                anchors.update(value for value in row if value is not None)
            if job["review_attempt"] is not None:
                review = tx.one(*run_statement(tx, 'SELECT context_id FROM attempts WHERE id=? AND run_id=?',
                             (job["review_attempt"],), run_last=True))
                context = json.loads(tx.get(review[0], fence.subject))
                if "review_context" in context:
                    anchors.add(context["reviewed_commit"])
        return DeliveryRead(job["source_ref"], job["target_ref"], job["pr_number"] if fence.subject is not None else None,
                            tuple(sorted(anchors)), job["source_sha"] if fence.subject is not None and job["phase"] == "checks" else None,
                            fence.subject is not None,
                            tuple((sha, "source") for sha in sorted(anchors)) if fence.subject is None else None,
                            read_checks=fence.local_effect is None)

    def accept_requirements(self, request: RequirementsRead, source: Requirements) -> DeliveryObservation:
        if source is None:
            raise KernelError("source_not_configured")
        fence = request.fence
        with self._observation_transaction(fence, write=True, source=source) as (tx, _):
            current = replace(self._read_fence(tx, fence.subject, fence.execution, fence.cleanup),
                              local_effect=fence.local_effect)
            query = self._delivery_read(tx, current)
        return DeliveryObservation(current, query)

    def accept_delivery(self, request: DeliveryObservation, facts: DeliveryFacts) -> tuple[World, ReadFence]:
        fence = request.fence
        with self._observation_transaction(fence, write=False) as (tx, run):
            required = fence.local_effect is None
            if (type(request.query.read_checks) is not bool or request.query.read_checks != required
                    or type(facts.checks_requested) is not bool or facts.checks_requested != required
                    or not required and facts.checks is not None):
                raise ReadUnavailable("checks_observation_incomplete", "Required Checks demand was not exactly observed")
            if request.query.ancestry_pairs is not None:
                checked = requested_ancestry(request.query, facts.source)
                if (facts.ancestry_checked != checked
                        or checked is None and facts.ancestry
                        or checked is not None and not set(facts.ancestry) <= set(checked)):
                    raise ReadUnavailable("ancestry_observation_incomplete", "Requested ancestry was not exactly observed")
            revision, completion = run["observed_parent_revision"], "no-tickets"
            if fence.subject is not None:
                job = self._job(tx, fence.subject)
                if job["kind"] == "ticket":
                    revision = tx.one(*run_statement(tx, 'SELECT revision FROM issue_facts WHERE issue=? AND run_id=?',
                                   (job["issue"],), run_last=True))[0]
                completion = job["completion_revision"]
            if facts.pr is not None and facts.pr.state == "OPEN" and (
                    pr_target(facts.pr, repository=run["repository"], pr_number=request.query.pr_number,
                        source_ref=request.query.source_ref, target_ref=request.query.target_ref,
                        source=facts.source, target=facts.target, ancestry=facts.ancestry) is None
                    or facts.checks is not None and (facts.checks.pr_number != facts.pr.number or facts.checks.head_sha != facts.pr.head.sha)):
                raise ReadUnavailable("github_observation_pending", "PR, refs and checks have not converged")
            return World(run["observed_parent_revision"], run["observed_graph_revision"], revision, facts.target,
                         facts.source, facts.pr, facts.checks, completion, facts.ancestry,
                         facts.publisher_actor, facts.publisher_identity), fence

    @contextmanager
    def _observation_transaction(self, fence: ReadFence, *, write: bool,
                                 source: Requirements | None = None) -> Iterator[tuple[Transaction, sqlite3.Row]]:
        """Validate and consume returned facts in the same transaction.

        An expired read window or changed Source needs the ordered writer.
        The consumer runs once, after fresh checks; a timeout
        commits before ReadWait leaves this boundary. No row or permission
        from the first snapshot is reused by that writer.
        """
        for writable in ((True,) if write and source is None else (False, True)):
            with self.storage.transaction(write=writable) as tx:
                run = self._check_operation(tx, fence, fence.execution)
                try:
                    waiting = (None if fence.execution is None else self._operation_read_wait(
                        tx, run, self._automatic_token(fence)["scope"], after_read=True, write_timeout=writable))
                except _SupervisionWriteRequired:
                    continue  # Close the reader normally; never retry a failed close or consumer.
                if waiting is None:
                    if source is not None and not self._refresh(tx, source, readonly=not writable):
                        continue
                    yield tx, run
                    return
            raise ReadWait(waiting)

    def authorize_agent_observation(self, call: AgentCall, world: World, fence: ReadFence) -> None:
        with self.storage.transaction() as tx:
            if fence.local_effect is not None:
                raise KernelError("observation_binding")
            self._check_operation(tx, fence, call.execution)
            if fence.subject != call.subject:
                raise KernelError("invocation_binding")
            run, job = self._owned(tx, call.subject, call.execution)
            if not self._current(tx, job, run, world):
                raise KernelError("agent_authority_changed")
            self._agent_authority(tx, call)

    def effect_observation_prepared(self, effect: Effect, fence: ReadFence) -> bool:
        with self.storage.transaction(write=False) as tx:
            self._check_operation(tx, fence, effect.execution)
            return bool(tx.one(*run_statement(tx, 'SELECT status FROM effects WHERE id=? AND run_id=?',
                            (effect.effect_id,), run_last=True))[0] == "prepared")

    def _local_cleanup_effect(self, tx: Transaction, effect: Effect) -> None:
        row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                  (effect.effect_id, effect.subject), run_last=True))
        job = self._job(tx, effect.subject)
        if (effect.operation not in {"cleanup_worktree", "cleanup_local_ref"}
                or self._abandon_action(tx) is not None or row["control_action"] is not None
                or row["status"] not in {"prepared", "sent"}
                or self._effect(tx, row, effect.execution) != effect or not self._cleanup_current(tx, job, row)):
            raise KernelError("local_cleanup_observation_binding")

    def begin_effect_observation(self, effect: Effect) -> RequirementsRead | LocalCleanupRead:
        """Only ordinary local retirement is independent of Delivery facts.

        Its source refresh is still mandatory at every write boundary. An
        unrelated Delivery outage is not read or converted into permission.
        """
        with self.storage.transaction(write=False) as tx:
            self._effect_owned(tx, effect, effect.execution)
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                      (effect.effect_id, effect.subject), run_last=True))
            if self._effect(tx, row, effect.execution) != effect:
                raise KernelError("effect_binding")
            local = row["operation"] in {"cleanup_worktree", "cleanup_local_ref"} and row["control_action"] is None
            if local:
                self._local_cleanup_effect(tx, effect)
        source = self._begin_observation(effect.subject, execution=effect.execution, delivery=not local,
            local_effect=effect if effect.operation in self.LOCAL_PREPARATION else None)
        return LocalCleanupRead(effect, source) if local else source

    def _local_preparation_read(self, tx: Transaction, effect: Effect, fence: ReadFence) -> None:
        """Bind a check-free read to its one already selected local Effect."""
        if (effect.operation not in self.LOCAL_PREPARATION
                or (fence.subject, fence.execution) != (effect.subject, effect.execution) or fence.cleanup):
            raise KernelError("local_preparation_observation_binding")
        self._effect_owned(tx, effect, effect.execution)
        row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                    (effect.effect_id, effect.subject), run_last=True))
        if (row["status"] not in {"prepared", "sent"} or row["control_action"] is not None
                or self._effect(tx, row, effect.execution) != effect):
            raise KernelError("local_preparation_observation_binding")

    def accept_local_cleanup_requirements(self, request: LocalCleanupRead, source: Requirements) -> LocalCleanupObservation:
        if (not isinstance(request, LocalCleanupRead)
                or not isinstance(request.source, RequirementsRead) or not isinstance(request.source.fence, ReadFence)
                or request.source.fence.subject != request.effect.subject
                or request.source.fence.execution != request.effect.execution or request.source.fence.cleanup):
            raise KernelError("local_cleanup_observation_binding")
        with self.storage.transaction(write=False) as tx:
            self._local_cleanup_effect(tx, request.effect)
        accepted = self.accept_requirements(request.source, source)
        with self.storage.transaction(write=False) as tx:
            self._check_operation(tx, accepted.fence, request.effect.execution)
            self._local_cleanup_effect(tx, request.effect)
        return LocalCleanupObservation(request.effect, accepted.fence)

    def authorize_effect_observation(self, effect: Effect, world: World | LocalCleanupObservation, fence: ReadFence, *,
                                     prepared: bool, inspections: tuple[ResourceInspection, ...] | None) -> None:
        if fence.local_effect is not None and (
                fence.local_effect != effect or not isinstance(world, World) or world.checks is not None):
            raise KernelError("local_preparation_observation_binding")
        if isinstance(world, LocalCleanupObservation):
            if (not isinstance(fence, ReadFence) or (world.effect, world.fence) != (effect, fence)
                    or fence.subject != effect.subject or fence.execution != effect.execution or fence.cleanup):
                raise KernelError("local_cleanup_observation_binding")
        elif not isinstance(world, World):
            raise KernelError("effect_observation_binding")
        waiting = self._finish_operation_read(fence, effect.execution)
        if waiting is not None:
            raise ReadWait(waiting)
        if prepared and not self._dispatch(effect, effect.execution, world, resource_inspections=inspections, token=fence):
            raise KernelError("operation_cancelled")
        self._guard_effect(effect, effect.execution, world, resource_inspections=inspections, token=fence)

    @staticmethod
    def _approval_boundary(run: sqlite3.Row, job: sqlite3.Row) -> tuple[str | int | None, ...]:
        return (run["parent_revision"], run["graph_revision"], run["observed_parent_revision"],
                run["observed_graph_revision"], *(job[key] for key in (
            "revision", "completion_revision", "publication_sha", "pr_number", "source_ref", "target_ref",
            "review_attempt", "checks_id", "grant_id")))

    def _observation_host(self, tx: Transaction, execution: Execution, action_id: str | None = None) -> sqlite3.Row:
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                  (), run_last=True))
        self._automatic_owned(tx, run, execution)
        rows = tx.rows("SELECT h.*,a.kind AS action_kind,a.status AS action_status FROM hosts h JOIN actions a ON a.id=h.action_id "
                      "WHERE h.run_id=? AND h.owner=? AND h.epoch=? AND h.status='running' AND h.usage_ready=1 AND h.usage_released=0 AND h.dispatched_at IS NOT NULL AND a.run_id=h.run_id",
                      (execution.run_id, execution.owner, execution.epoch))
        if (len(rows) != 1 or rows[0]["action_status"] not in {"applied", "pending", "updating"}
                or action_id is not None and rows[0]["action_id"] != action_id):
            raise KernelError("managed_observation_required")
        return rows[0]

    def activate_observation(self, execution: Execution) -> _ObservationPass:
        with self._observation_lock, self.storage.transaction(write=False) as tx:
            host = self._observation_host(tx, execution)
            if self._observation is not None and (self._observation.execution, self._observation.action_id) == (execution, host["action_id"]):
                return self._observation
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            pending: list[_SentRead | _ApprovalRead] = [_SentRead(row["id"], row["attempts"], row["subject"], row["generation"])
                for row in tx.all(*run_statement(tx, "SELECT id,attempts,subject,generation FROM effects WHERE status='sent' AND operation!='issue_link' AND run_id=? ORDER BY rowid",
                               (), run_last=True))]
            if not run["stopped"] and not run["blocked"]:
                pending.extend(_ApprovalRead(job["id"], job["generation"], self._approval_boundary(run, job))
                    for job in tx.all(*run_statement(tx, "SELECT * FROM subjects WHERE kind!='ticket' AND phase='approval' AND run_id=?",
                                   (), run_last=True))
                    if not job["validation_cleanup"] and not self._gates(tx, job["id"]) and self._pending_effect(tx, job["id"]) is None)
            self._observation = _ObservationPass(execution, host["action_id"], pending)
            return self._observation

    def _observation_member(self, tx: Transaction, run: sqlite3.Row, observation: _ObservationPass | None,
                            subject: str | None = None, *, snapshot: _OperationSnapshot | None = None) -> _SentRead | _ApprovalRead | None:
        if observation is None:
            return None
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or observation.execution.run_id != selected.run_id:
            raise KernelError("observation_binding")
        with observation.lock:
            try:
                self._observation_host(tx, observation.execution, observation.action_id)
            except KernelError as error:
                if error.code == "host_usage_unproven":
                    return None  # Unknown readiness is not proof of Host exit.
                if error.code not in {"executor_fenced", "managed_observation_required"}:
                    raise
                observation.pending.clear()
                return None
            for member in tuple(observation.pending):
                read = None if snapshot is None else snapshot.subject(member.subject)
                job = self._job(tx, member.subject) if read is None else read.job
                if isinstance(member, _SentRead):
                    rows = tx.rows(*run_statement(tx, 'SELECT status,attempts,subject,generation FROM effects WHERE id=? AND run_id=?',
                               (member.effect_id,), run_last=True))
                    valid = bool(rows and (rows[0]["status"], rows[0]["attempts"], rows[0]["subject"], rows[0]["generation"]) ==
                                 ("sent", member.dispatch_serial, member.subject, member.generation))
                else:
                    valid = (job["phase"] == "approval" and self._approval_boundary(run, job) == member.boundary
                             and not run["stopped"] and not run["blocked"]
                             and not any(gate["code"] != "supervision_timeout" for gate in self._gates(tx, member.subject)))
                if not valid or job["generation"] != member.generation:
                    observation.pending.remove(member)
                    continue
                if subject is not None and member.subject != subject:
                    continue
                if isinstance(member, _ApprovalRead) and self._subject_wait(tx, run, job, read=read) is not None:
                    continue
                if (run["blocked"] == "supervision_timeout" or any(gate["code"] == "supervision_timeout" for gate in self._gates(tx, member.subject))
                        or self._operation_wait(tx, run, member.subject, False) is not None):
                    continue
                return member
            return None

    def _operation_token(self, tx: Transaction, subject: str | None, cleanup: bool = False, *,
                         snapshot: _OperationSnapshot | None = None) -> str:
        if snapshot is not None:
            snapshot.require(tx)
        run = snapshot.run if snapshot is not None else tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                                                            (), run_last=True))
        read = None if snapshot is None or subject is None else snapshot.subject(subject)
        return wire_json({"scope": self._automatic_scope(tx, run, subject, cleanup, read=read),
                          "progress": self._automatic_progress(tx, run, subject, cleanup, read=read),
                          "source_sequence": run["source_sequence"],
                          "control_version": [run[key] for key in ("executor", "epoch", "stopped", "blocked", "final_action")]}).decode()

    def _check_operation(self, tx: Transaction, token: str | ReadFence, execution: Execution | None) -> sqlite3.Row:
        value, run = self._automatic_token(token), tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                                       (), run_last=True))
        if isinstance(token, ReadFence) and token.execution != execution:
            raise KernelError("observation_binding")
        if execution is not None:
            self._automatic_owned(tx, run, execution)
        scope = value["scope"]
        if (scope != self._automatic_scope(tx, run, scope["subject"], scope["cleanup"])
                or value["source_sequence"] != run["source_sequence"]
                or execution is None and value["control_version"] != [run[key] for key in ("executor", "epoch", "stopped", "blocked", "final_action")]):
            raise ObservationSuperseded("automatic_step_superseded")
        if isinstance(token, ReadFence) and token.local_effect is not None:
            self._local_preparation_read(tx, token.local_effect, token)
        return run

    def _continuation_wait(self, tx: Transaction, job: sqlite3.Row, *, read: _SubjectSnapshot | None = None) -> Step | None:
        """Use the Attempt's single native-or-Opening facts loader."""
        subject = job["id"]
        if read is not None:
            read.require(tx, job)
        if (read.preparing if read is not None else tx.rows(*run_statement(tx,
                "SELECT scope_id FROM waits WHERE subject=? AND kind='agent_preparation' AND run_id=?",
                (subject,), run_last=True))):
            return Step("waiting", subject, "agent_preparing")
        if job["current_attempt"]:
            attempt = read.attempt if read is not None else tx.one(*run_statement(tx,
                'SELECT * FROM attempts WHERE id=? AND run_id=?', (job["current_attempt"],), run_last=True))
            assert attempt is not None
            facts = (read.continuation if read is not None else load_continuation(tx, attempt["id"])
                     if attempt["thread_id"] is not None or attempt["opening_id"] is not None else None)
            prior = None if facts is None else facts.prior
            if prior is not None:
                if prior.status != "running" and prior.dispatch_state in {"sent", "started", "unknown"}:
                    return Step("waiting", subject, "previous_invocation_exit")
                if prior.status == "running" and not (prior.dispatch_state == "allocated" and self.clock() >= prior.deadline_at):
                    return Step("waiting", subject, "agent_running")
                if prior.failure_kind == "capacity" and prior.recovery_ready_at is not None and self.clock() < prior.recovery_ready_at:
                    # Explicit Resume starts its new period only after capacity returns.
                    deadline = facts.semantic_deadline if facts is not None and attempt["resume_action"] is None else None
                    if deadline is None or self.clock() < deadline:
                        retry_at = prior.recovery_ready_at if deadline is None else min(deadline, prior.recovery_ready_at)
                        return Step("waiting", subject, "capacity_recovery_wait", retry_at=retry_at)
            if (read.leased if read is not None else tx.rows(*run_statement(tx,
                    'SELECT id FROM leases WHERE subject=? AND run_id=?', (subject,), run_last=True))) and not (
                    prior is not None and prior.dispatch_state == "allocated" and self.clock() >= prior.deadline_at):
                return Step("waiting", subject, "previous_invocation_exit")
        return None

    def _subject_wait(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, *,
                      read: _SubjectSnapshot | None = None) -> Step | None:
        """The transition's read-only admission prefix, also used at Host exit."""
        subject = job["id"]
        if read is not None:
            read.require(tx, job)
        pending = read.pending if read is not None else self._pending_effect(tx, subject)
        gates = {row["code"] for row in (read.gates if read is not None else self._gates(tx, subject))}
        if pending is not None and pending["status"] == "sent":
            # Gates prohibit a fresh write, never the original sent readback.
            return Step("paused", subject, "supervision_timeout") if "supervision_timeout" in gates or run["blocked"] == "supervision_timeout" else None
        if pending is not None and self._effect_stale(tx, run, job, pending, read=read):
            return None
        requeue = read.requeue if read is not None else self._pending_requeue(tx, subject)
        if requeue is not None:
            if not self._requeue_versions_current(tx, run, job, requeue, None):
                return None
            return Step("paused", subject, self._pause_reason(tx, subject, run, read=read)) if run["stopped"] or gates - {"requeue_required"} else None
        continuation = read.waiting if read is not None else self._continuation_wait(tx, job)
        if continuation is not None and continuation.reason in {"agent_running", "previous_invocation_exit"}:
            return continuation
        refreshing = job["refresh_revision"] is not None and self._automatic_control_read(tx, run, subject, False, read=read)
        if run["stopped"] or (run["blocked"] or gates) and not refreshing:
            return Step("paused", subject, self._pause_reason(tx, subject, run, read=read))
        if pending is not None or refreshing or job["validation_cleanup"] or tx.rows(*run_statement(tx, "SELECT subject FROM issue_links l WHERE subject=? AND state='pending' AND effect_id IN (SELECT id FROM effects e WHERE e.run_id=l.run_id AND status='prepared') AND l.run_id=?",
                                                                                 (subject,), run_last=True)):
            return None
        if job["phase"] == "complete":
            return Step("complete", subject)
        eligibility = self._eligibility(tx, job)
        if eligibility is not None:
            return Step("waiting", subject, eligibility)
        if tx.rows(*run_statement(tx, "SELECT s.id FROM dependencies d JOIN subjects s ON s.run_id=d.run_id AND s.id=d.predecessor WHERE d.subject=? AND s.phase!='complete' AND d.run_id=? LIMIT 1",
               (subject,), run_last=True)):
            return Step("waiting", subject, "dependencies")
        if continuation is not None:
            return continuation
        # First admission still registers the existing FIFO queue. Once queued,
        # a seat waiter must yield to its holder's readback and completion.
        if (job["phase"] == "integration" and job["kind"] == DeliveryKind.TICKET
                and run["integration_holder"] not in (None, subject)
                and tx.rows(*run_statement(tx, 'SELECT subject FROM integration_queue WHERE subject=? AND run_id=?',
                        (subject,), run_last=True))):
            return Step("waiting", subject, "integration_seat")
        return None

    def _operation_choice(self, tx: Transaction, run: sqlite3.Row, *, agent_capacity: int = 1,
                          poll_manual: bool = False, observation: _ObservationPass | None = None,
                          snapshot: _OperationSnapshot | None = None) -> tuple[str | None, bool, _SentRead | _ApprovalRead | None] | None:
        snapshot = snapshot or _OperationSnapshot(self, tx, run)
        snapshot.require(tx)
        member = self._observation_member(tx, run, observation, snapshot=snapshot)
        if member is not None:
            return member.subject, False, member
        agent_capacity = min(agent_capacity, max(0, run["concurrency"] - self._agent_seats(tx)))
        abandoned = snapshot.abandoned
        if abandoned is not None:
            if self._abandon_wait(tx, abandoned, snapshot=snapshot) is not None:
                return None
            return tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY rowid LIMIT 1',
                       (), run_last=True))[0], False, None
        order = [row[0] for table in ("work_queue", "integration_queue") for row in tx.all(*run_statement(tx, f'SELECT subject FROM {table} WHERE run_id=? ORDER BY ready',
                                                                                        (), run_last=True))]
        order.extend(row[0] for row in tx.all(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY rowid',
                                           (), run_last=True)))
        manual_poll = None
        for subject in dict.fromkeys(order):
            read = snapshot.subject(subject)
            job = read.job
            waiting = self._subject_wait(tx, run, job, read=read)
            cleanup = waiting is not None and waiting.kind == "complete"
            if cleanup:
                if self._cleanup_wait(tx, run, job, read=read) is not None:
                    continue
            elif waiting is not None:
                continue
            if self._operation_wait(tx, run, subject, cleanup, read=read) is not None:
                continue
            pending = read.pending
            gated_readback = pending is not None and pending["status"] == "sent" and (
                run["stopped"] or run["blocked"] or read.gates)
            if cleanup:
                gated_readback = bool(tx.rows(*run_statement(tx, "SELECT c.subject FROM resource_cleanup c JOIN effects e ON e.run_id=c.run_id AND e.id=c.effect_id WHERE c.subject=? AND e.status='sent' AND c.outcome IS NOT NULL AND c.run_id=?",
                                          (subject,), run_last=True)))
            if gated_readback:
                # Like approval, an explicit poll can settle a late result;
                # a manual gate does not advertise endless background work.
                if poll_manual and manual_poll is None:
                    manual_poll = (subject, cleanup, None)
                continue
            if (job["phase"] == "approval" and not job["validation_cleanup"] and job["refresh_revision"] is None
                    and not pending and read.requeue is None):
                # An explicit operation call may poll the manual boundary once.
                # Passive supervision must not advertise perpetual background work.
                if poll_manual and manual_poll is None:
                    manual_poll = (subject, False, None)
                continue
            if not agent_capacity and job["phase"] in {"development", "review", "publish"} and not pending:
                # A ready return may still perform program work without a slot.
                if not read.leased and (
                        read.attempt is None or not read.attempt['result_id']) and tx.rows(*run_statement(tx, 'SELECT subject FROM work_queue WHERE subject=? AND run_id=?',
                                                                                                                                                     (subject,), run_last=True)):
                    continue
            return subject, cleanup, None
        tickets = tx.all(*run_statement(tx, "SELECT * FROM subjects WHERE kind='ticket' AND run_id=? ORDER BY issue",
                      (), run_last=True))
        if (not run["stopped"] and not run["blocked"] and tickets
                and tx.rows(*run_statement(tx, "SELECT id FROM final_target f WHERE id NOT IN (SELECT id FROM subjects s WHERE s.kind='final_run' AND s.run_id=f.run_id) AND f.run_id=?",
                        (), run_last=True))
                and all(self._completion(tx, job) is not None for job in tickets)
                and self._operation_wait(tx, run, None, False) is None):
            return None, False, None
        return manual_poll

    def _operation_wait(self, tx: Transaction, run: sqlite3.Row, subject: str | None, cleanup: bool, *,
                        read: _SubjectSnapshot | None = None) -> float | None:
        scope = wire_json(self._automatic_scope(tx, run, subject, cleanup, read=read))
        rows = tx.rows(*run_statement(tx, 'SELECT w.next_retry_at FROM waits w JOIN material m ON m.run_id=w.run_id AND m.id=w.scope_id WHERE w.subject IS ? AND m.body=? AND w.run_id=? ORDER BY w.deadline LIMIT 1',
                   (subject, scope), run_last=True))
        return rows[0][0] if rows and rows[0][0] is not None and self.clock() < rows[0][0] else None

    def _cleanup_wait(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, *, read: _SubjectSnapshot | None = None) -> Step | None:
        if read is not None:
            read.require(tx, job)
        rows = tx.rows(*run_statement(tx, 'SELECT c.*,e.status AS effect_status FROM resource_cleanup c LEFT JOIN effects e ON e.run_id=c.run_id AND e.id=c.effect_id WHERE c.subject=? AND c.generation=? AND c.run_id=?',
                   (job["id"], job["generation"]), run_last=True))
        if not rows or rows[0]["state"] == "complete":
            return Step("complete", job["id"], "resources_retired")
        cleanup = rows[0]
        if cleanup["effect_status"] in {"observed", "cancelled"}:
            return None
        if cleanup["effect_status"] == "sent":
            if run["blocked"] == "supervision_timeout" or any(gate["code"] == "supervision_timeout" for gate in (read.gates if read is not None else self._gates(tx, job["id"]))):
                return Step("paused", job["id"], "supervision_timeout")
            return None
        if run["stopped"] or run["blocked"] or (read.gates if read is not None else self._gates(tx, job["id"])) or cleanup["outcome"] is not None:
            return Step("paused", job["id"], "cleanup_authority")
        return None

    def _abandon_wait(self, tx: Transaction, action: sqlite3.Row, *, snapshot: _OperationSnapshot | None = None) -> Step | None:
        if snapshot is not None:
            snapshot.require(tx)
        subject = tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY rowid LIMIT 1',
                      (), run_last=True))[0]
        if action["status"] == "complete":
            return Step("complete", subject, "abandoned")
        if self._agent_seats(tx):
            return Step("waiting", subject, "worker_exit_unproven")
        if any((waiting := (self._continuation_wait(tx, job) if snapshot is None else snapshot.subject(job['id'], job).waiting)) is not None and waiting.reason in {
                "agent_running", "previous_invocation_exit"} for job in tx.all(*run_statement(tx, 'SELECT * FROM subjects WHERE run_id=? ORDER BY rowid',
                                                                            (), run_last=True))):
            return Step("waiting", subject, "worker_exit_unproven")
        if self._abandon_reconciliation_blocked(tx, action):
            return Step("paused", subject, "abandon_outcome_unproven")
        run = snapshot.run if snapshot is not None else tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                                                            (), run_last=True))
        for job in tx.all(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY rowid',
                       (), run_last=True)):
            retry_at = self._operation_wait(tx, run, job["id"], False)
            if retry_at is not None:
                return Step("waiting", job["id"], "github_convergence", retry_at=retry_at)
        inspection = self._abandon_inspection(tx, action)
        if inspection is not None and inspection["status"] in {"observed", "confirmed"}:
            try:
                self._check_abandon_inspection(tx, action, inspection)
            except KernelError as error:
                return Step("paused", subject, error.code)
        return None

    def operation_state(self, observation: _ObservationPass | None = None) -> OperationState:
        return self._operation_state(observation)

    def operation_supervision(self) -> OperationSupervision:
        """Observe every current subject without choosing unused future work."""
        with self.storage.transaction(write=False) as tx:
            observed_at = self.clock()
            snapshot = _OperationSnapshot(self, tx, tx.one(*run_statement(tx,
                'SELECT * FROM runs WHERE id=?', (), run_last=True)))
            active, preparations, wake_at, _ = self._operation_supervision_facts(tx, snapshot, observed_at)
            return self._operation_supervision_in(tx, snapshot, active, preparations, wake_at)

    def next_work(self, execution: Execution, *, agent_capacity: int = 1,
                  observation: _ObservationPass | None = None) -> Step | IORequest:
        if observation is not None and observation.execution != execution:
            raise KernelError("observation_binding")
        observed: ObserveIO | None = None
        construction_error: ReadWait | ReadUnavailable | KernelError | None = None
        try:
            with self.storage.transaction(write=False) as tx:
                run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                          (), run_last=True))
                try:
                    self._automatic_owned(tx, run, execution)
                except KernelError as error:
                    if error.code != "executor_fenced":
                        raise
                    return Step("paused", "", error.code)
                snapshot = _OperationSnapshot(self, tx, run)
                choice = self._operation_choice(tx, run, agent_capacity=agent_capacity,
                                                poll_manual=observation is None, observation=observation, snapshot=snapshot)
                if choice is None:
                    state = self._operation_state_in(tx, observation, snapshot=snapshot)
                    return Step(state.disposition, "", retry_at=state.wake_at)
                subject, cleanup, member = choice
                work = Work(execution, self._operation_token(tx, subject, cleanup, snapshot=snapshot), subject,
                            cleanup, bool(agent_capacity), observation, member)
                original = (self._effect(tx, tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                                                 (member.effect_id,), run_last=True)), execution)
                            if isinstance(member, _SentRead) else None)
                if subject is not None and not cleanup and member is None and snapshot.abandoned is None:
                    read = snapshot.subject(subject)
                    if (read.requeue is None and read.pending is None and read.job['current_attempt'] is None
                            and self._subject_wait(tx, run, read.job, read=read) is None):
                        try:
                            observed = self._observe_work(tx, work, subject)
                        except (ReadWait, ReadUnavailable, KernelError) as error:
                            construction_error = error
                            raise
        except (ReadWait, ReadUnavailable, KernelError) as error:
            # Only a construction failure follows the original fail_work path.
            # Reader admission/closing failures are never retried as a writer.
            if error is not construction_error:
                raise
        try:
            if construction_error is not None:
                raise construction_error
            if observed is not None:
                return self._complete_work(work, observed)
            if original is not None:
                waiting = self._guard_automatic_step(work.token, execution, readback=original)
                if waiting is not None:
                    raise ReadWait(waiting)
                return EffectIO(work, original, "readback", work.token)
            return self._route_work(work)
        except (ReadWait, ReadUnavailable, KernelError) as error:
            return self.fail_work(work, error)

    def _observe_work(self, tx: Transaction, work: Work, subject: str, *,
                      after: EffectIO | None = None, result: EffectResult | None = None) -> ObserveIO:
        work = replace(work, subject=subject)
        self._automatic_owned(tx, tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                      (), run_last=True)), work.execution)
        fence = self._read_fence(tx, subject, work.execution, work.cleanup)
        parent = tx.one(*run_statement(tx, 'SELECT parent_issue FROM runs WHERE id=?',
                     (), run_last=True))[0]
        return ObserveIO(work, RequirementsRead(fence, parent, self._delivery_read(tx, fence)), after, result)

    def _route_work(self, work: Work, world: World | None = None, fence: ReadFence | None = None) -> Step | IORequest:
        if work.subject is None:
            if world is None:
                return ObserveIO(work, self.begin_observation(None, execution=work.execution))
            if world.source is None or world.target is None:
                raise ReadUnavailable("final_source_missing", "The original remote Final refs are absent")
            assert fence is not None
            final = self._activate_final(work.execution, world.source, world.target, ancestry=world.ancestry, token=fence)
            return self._complete_work(work, Step("progress", final, "final_activated"))
        with self.storage.transaction() as tx:
            self._check_operation(tx, work.token if fence is None else fence, work.execution)
            result = (self._advance_cleanup(tx, work, world) if work.cleanup else self._advance(tx, work, world))
        return self._complete_work(work, result)

    def _effect_work(self, tx: Transaction, work: Work, row: sqlite3.Row) -> PrepareIO | EffectIO:
        effect = self._effect(tx, row, work.execution)
        token = self._operation_token(tx, effect.subject, False)
        if row["status"] in {"sent", "confirmed"}:
            return EffectIO(work, effect, "readback", token)
        if row["status"] != "prepared":
            raise KernelError("effect_not_prepared")
        if effect.operation == "publication_commit" and not effect.payload.get("reuse_sha") and not effect.payload.get("publication_commit_intent"):
            return PrepareIO(work, effect, "publication", token)
        if effect.operation == "retire_pr" and not effect.payload.get("retirement_intent"):
            return PrepareIO(work, effect, "retirement", token)
        return EffectIO(work, effect, "execute", token)

    def _record_readback(self, effect: Effect, result: EffectResult) -> None:
        with self.storage.transaction() as tx:
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                      (effect.effect_id,), run_last=True))
            if self._effect(tx, row, effect.execution) != effect:
                raise KernelError("effect_binding")
            evidence = tx.put(effect.subject, "effect_readback", wire_json({"dispatch_serial": effect.dispatch_serial, "result": asdict(result)}))
            self._event(tx, effect.subject, "effect_readback", effect.effect_id, evidence, generation=row["generation"])

    def _work_run(self, work: Work, request: IORequest) -> None:
        """Bind callback metadata before audit; grant no live execution authority."""
        with self.storage.transaction(write=False) as tx:
            selected = transaction_scope(tx)
            effect = (request.effect if isinstance(request, (EffectIO, PrepareIO)) else
                      None if request.after is None else request.after.effect)
            if (work.execution.run_id != selected.run_id or request.work.execution.run_id != selected.run_id
                    or effect is not None and effect.execution.run_id != selected.run_id):
                raise KernelError("effect_binding")

    def accept_work(self, request: IORequest, result: IOResult) -> Step | IORequest:
        work = request.work
        self._work_run(work, request)
        if isinstance(request, EffectIO) and isinstance(result, EffectResult):
            returncode = result.raw.get("write_returncode")
            if type(returncode) is int and returncode != 0:
                from agent_run.errors import bounded_error
                self.observe_effect_failure(request.effect, {"returncode": returncode,
                    "message": bounded_error(str(result.raw.get("write_diagnostic", "")))})
            if request.kind == "readback" or request.effect.operation != "issue_link":
                self._record_readback(request.effect, result)
        with self.storage.transaction(write=False) as tx:
            self._automatic_owned(tx, tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                          (), run_last=True)), work.execution)
        if isinstance(request, ObserveIO):
            if not isinstance(result, tuple):
                raise KernelError("observation_binding")
            world, fence = result
            if fence.local_effect is not None or (fence.execution, fence.subject, fence.cleanup) != (
                    work.execution, request.request.fence.subject, request.request.fence.cleanup):
                raise KernelError("observation_binding")
            if request.after is None:
                work = replace(work, approval_observed=isinstance(work.member, _ApprovalRead))
                return self._route_work(work, world, fence)
            prior = request.after
            if isinstance(prior, PrepareIO):
                if not isinstance(request.result, Mapping):
                    raise KernelError("effect_binding")
                effect = (self.attach_publication_commit(prior.effect, request.result, work.execution, world)
                          if prior.kind == "publication" else self.attach_retirement(prior.effect, request.result, work.execution, world))
                return EffectIO(work, effect, "execute", prior.token)
            if not isinstance(request.result, EffectResult):
                raise KernelError("effect_binding")
            step = self._reconcile(prior, request.result, world, token=fence)
            return self._complete_work(work, step)
        if isinstance(request, PrepareIO):
            if not isinstance(result, Mapping):
                raise KernelError("effect_binding")
            return ObserveIO(work, self.begin_observation(request.effect.subject, execution=work.execution), request, result)
        if not isinstance(result, EffectResult):
            raise KernelError("effect_binding")
        effect = request.effect
        if effect.operation == "issue_link" and request.kind == "execute":
            self.acknowledge_link(effect, result)
            return self._complete_work(work, Step("progress", effect.subject))
        waiting = self._finish_operation_read(request.token, work.execution)
        if waiting is not None:
            raise ReadWait(waiting)
        if isinstance(work.member, _SentRead) and result.disposition not in {"target", "unknown", "third"}:
            assert work.observation is not None
            work.observation.defer(work.member)
            return self._wait_for_read(request.token, work.execution, "original_write_readback_pending",
                "The original sent request has no final readback yet", readback=effect)
        step = self._reconcile(request, result)
        return self._complete_work(work, step)

    def _complete_work(self, work: Work, step: Step | IORequest) -> Step | IORequest:
        if isinstance(step, ObserveIO):
            waiting = self._guard_automatic_step(step.request.fence, work.execution)
            if waiting is not None:
                raise ReadWait(waiting)
            return step
        if isinstance(step, EffectIO) and step.kind == "readback":
            with self.storage.transaction(write=False) as tx:
                sent = tx.one(*run_statement(tx, 'SELECT status FROM effects WHERE id=? AND run_id=?',
                           (step.effect.effect_id,), run_last=True))[0] == "sent"
            waiting = self._guard_automatic_step(step.token, work.execution, readback=step.effect if sent else None)
            if waiting is not None:
                raise ReadWait(waiting)
            return step
        if not isinstance(step, Step) or step.call is not None or step.preparation is not None:
            return step
        step = self._finish_automatic_step(work.token, work.execution, step)
        if (isinstance(work.member, _SentRead) or work.approval_observed) and work.observation is not None and work.member is not None:
            work.observation.complete(work.member)
        return step

    def fail_work(self, work: Work, error: ReadWait | ReadUnavailable | KernelError | OSError, *, request: IORequest | None = None) -> Step:
        from agent_run.errors import bounded_error
        if request is not None:
            self._work_run(work, request)
        if isinstance(request, EffectIO) and request.kind == "execute":
            if isinstance(error, ReadUnavailable) and error.write_returncode not in (None, 0):
                self.observe_effect_failure(request.effect, {"returncode": error.write_returncode,
                    "message": bounded_error(error.write_diagnostic)})
            if isinstance(error, OSError):
                with self.storage.transaction(write=False) as tx:
                    sent = tx.one(*run_statement(tx, 'SELECT status FROM effects WHERE id=? AND run_id=?',
                               (request.effect.effect_id,), run_last=True))[0] == "sent"
                if not sent:
                    raise error
                self.observe_effect_failure(request.effect, {"type": type(error).__name__, "message": bounded_error(str(error))})
        elif isinstance(error, OSError):
            raise error
        with self.storage.transaction(write=False) as tx:
            try:
                self._automatic_owned(tx, tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                              (), run_last=True)), work.execution)
            except KernelError as fenced:
                if fenced.code != "executor_fenced":
                    raise
                return Step("paused", work.subject or "", fenced.code)
        if isinstance(error, KernelError) and not isinstance(error, ReadWait) and error.code not in {
                "automatic_step_superseded", "automatic_step_paused", "executor_fenced"}:
            raise error
        if work.observation is not None and work.member is not None:
            work.observation.defer(work.member)
        original = request.effect if isinstance(request, EffectIO) and isinstance(work.member, _SentRead) else None
        if isinstance(error, ReadWait):
            return error.step
        if isinstance(error, ReadUnavailable):
            token = request.token if isinstance(request, EffectIO) else work.token
            return self._wait_for_read(token, work.execution, error.code, error.message, readback=original)
        if isinstance(error, OSError):
            assert isinstance(request, EffectIO)
            completed = self._complete_work(work, Step("waiting", request.effect.subject, "original_write_readback_required"))
            assert isinstance(completed, Step)
            return completed
        if work.member is not None and error.code == "automatic_step_superseded":
            return self._wait_for_read(work.token, work.execution, error.code, str(error), readback=original)
        return Step("waiting" if error.code == "automatic_step_superseded" else "paused", work.subject or "", error.code)

    def _operation_state(self, observation: _ObservationPass | None = None) -> OperationState:
        with self.storage.transaction(write=False) as tx:
            return self._operation_state_in(tx, observation)

    def _operation_state_in(self, tx: Transaction, observation: _ObservationPass | None = None, *,
                            snapshot: _OperationSnapshot | None = None) -> OperationState:
        """Read scheduling facts within the caller's existing transaction."""
        # Keep a retry that becomes due while these facts are selected. A
        # later clock read must not erase the wake after a no-work decision.
        observed_at = self.clock()
        snapshot = snapshot or _OperationSnapshot(self, tx, tx.one(*run_statement(tx,
            'SELECT * FROM runs WHERE id=?', (), run_last=True)))
        snapshot.require(tx)
        run = snapshot.run
        active, preparations, wake_at, lifetime_wait = self._operation_supervision_facts(tx, snapshot, observed_at)
        ready = self._operation_choice(tx, run, observation=observation, snapshot=snapshot) is not None
        abandoned = snapshot.abandoned
        complete = (abandoned is not None and abandoned["status"] == "complete" or
            not tx.rows(*run_statement(tx, "SELECT id FROM subjects WHERE phase!='complete' AND run_id=? LIMIT 1",
                    (), run_last=True))
            and not tx.rows(*run_statement(tx, "SELECT id FROM final_target f WHERE id NOT IN (SELECT id FROM subjects s WHERE s.kind='final_run' AND s.run_id=f.run_id) AND f.run_id=?",
                        (), run_last=True))
            and not tx.rows(*run_statement(tx, "SELECT subject FROM resource_cleanup WHERE state!='complete' AND run_id=? LIMIT 1",
                        (), run_last=True))
            and not tx.rows(*run_statement(tx, "SELECT id FROM effects WHERE status IN ('prepared','sent','observed') AND operation!='issue_link' AND run_id=? LIMIT 1",
                        (), run_last=True)))
        # Selection may cross a deadline: preserve its prior wake, but cancel
        # current Worker/Preparation seats using the clock after selection.
        supervision = self._operation_supervision_in(tx, snapshot, active, preparations, wake_at)
        waiting = bool(supervision.active or supervision.preparing or lifetime_wait or supervision.wake_at is not None)
        disposition: Literal["ready", "waiting", "quiescent", "complete"] = "ready" if ready else "waiting" if waiting else "complete" if complete else "quiescent"
        return OperationState(supervision.concurrency, supervision.active, supervision.cancel,
                              supervision.wake_at, disposition, supervision.preparing)

    def _operation_supervision_facts(self, tx: Transaction, snapshot: _OperationSnapshot,
                                     observed_at: float) -> tuple[list[sqlite3.Row], list[AgentPreparation], float | None, bool]:
        """Scan current supervision before selection; never cache time decisions."""
        snapshot.require(tx)
        run = snapshot.run
        active = tx.all(*run_statement(tx, 'SELECT l.*,i.deadline_at FROM leases l JOIN invocations i ON i.run_id=l.run_id AND i.id=l.invocation WHERE l.run_id=?',
                     (), run_last=True))
        preparations = [self._preparation(tx, row)[0] for row in tx.all(*run_statement(tx, "SELECT * FROM waits WHERE kind='agent_preparation' AND run_id=?",
                                                                            (), run_last=True))]
        deadlines = [float(row["deadline_at"]) for row in active] + [p.preparation_deadline for p in preparations]
        for row in tx.all(*run_statement(tx, 'SELECT w.*,m.body FROM waits w JOIN material m ON m.run_id=w.run_id AND m.id=w.scope_id WHERE w.run_id=?',
                       (), run_last=True)):
            read = None if row['subject'] is None else snapshot.subject(row['subject'])
            if json.loads(row["body"]) in (self._automatic_scope(tx, run, row["subject"], False, read=read), self._automatic_scope(tx, run, row["subject"], True, read=read)):
                deadlines.append(float(row["next_retry_at"] or row["deadline"]))
        lifetime_wait = False
        for job in tx.all(*run_statement(tx, 'SELECT * FROM subjects WHERE run_id=? ORDER BY rowid',
                       (), run_last=True)):
            read = snapshot.subject(job['id'], job)
            job = read.job
            waiting = read.waiting
            lifetime_wait |= waiting is not None and waiting.reason in {"agent_running", "previous_invocation_exit"}
            if waiting is not None and waiting.retry_at is not None and self._subject_wait(tx, run, job, read=read) == waiting:
                deadlines.append(waiting.retry_at)
        return active, preparations, min((t for t in deadlines if t > observed_at), default=None), lifetime_wait

    def _operation_supervision_in(self, tx: Transaction, snapshot: _OperationSnapshot,
                                  active: list[sqlite3.Row], preparations: list[AgentPreparation],
                                  wake_at: float | None) -> OperationSupervision:
        """Finish the same snapshot with fresh cancellation after any selection."""
        snapshot.require(tx)
        run = snapshot.run
        abandoned = snapshot.abandoned
        cancel = tuple(row["id"] for row in active if run["stopped"] or run["blocked"] or abandoned is not None or snapshot.subject(row["subject"]).gates or self.clock() >= row["deadline_at"])
        cancel += tuple(p.reservation_id for p in preparations if run["stopped"] or run["blocked"] or abandoned is not None
                        or snapshot.subject(p.subject).gates or self.clock() >= p.preparation_deadline)
        return OperationSupervision(run["concurrency"], tuple(row["id"] for row in active), cancel,
                                    wake_at, tuple(p.reservation_id for p in preparations))

    @classmethod
    def create_task(cls, path: Path, spec: RunSpec, *, requirements: Requirements) -> Kernel:
        """Atomically freeze and bind a complete Run in its repository database."""
        if spec.repository_root is None:
            raise KernelError("task_workspace_unknown")
        from agent_run.storage.routing import RepositoryRoute
        route = RepositoryRoute(path, Path(spec.repository_root), spec.repository)
        task = TaskKey(route.repository_root, route.repository, spec.parent_issue)
        from agent_run.adapters.workspace import ManagedWorkspace
        workspace = ManagedWorkspace.for_repository(task.repository)
        if route != RepositoryRoute.for_workspace(workspace):
            raise KernelError("managed_state_identity")
        cls._creation_profiles(spec)  # Reject invalid frozen input before creating any file.
        with task_lock(task):
            if not path.exists() and not path.is_symlink():
                route.initialize()
            result = route.create(task, spec, requirements)
            return route.kernel(result.run_id)

    @staticmethod
    def _creation_profiles(spec: RunSpec) -> dict[str, Any]:
        if not spec.deliveries or len({delivery.key for delivery in spec.deliveries}) != len(spec.deliveries):
            raise ValueError("one or more uniquely identified deliveries are required")
        if spec.inputs is None:
            raise KernelError("creation_inputs_required")
        profiles = dict(spec.inputs.profiles)
        validate_profile_options(preset=spec.inputs.profiles.get("preset"))
        checked = resolve_profiles(current=spec.inputs.profiles)
        if any(field not in spec.inputs.profiles["profiles"][role.value]
               or spec.inputs.profiles["profiles"][role.value][field] != checked["profiles"][role.value][field]
               for role in Role for field in ("model", "reasoning_effort", "reference")):
            raise KernelError("creation_profile_binding_changed")
        if spec.language is None:
            raise KernelError("creation_language_required")
        validate_resources(dict(spec.inputs.prompt_resources))
        validate_identity_snapshot(spec.inputs.worker_identity)
        validate_identity_snapshot(spec.inputs.runner_identity)
        return profiles

    @classmethod
    def create(cls, path: Path, spec: RunSpec, *, requirements: Requirements) -> Kernel:
        """The library creation entry uses the same atomic Task/Run producer."""
        return cls.create_task(path, spec, requirements=requirements)

    def _create_run(self, tx: Transaction, spec: RunSpec, profiles: Mapping[str, Any], *,
                    requirements: Requirements,
                    policy_values: Mapping[str, Any] | None = None) -> None:
        """Freeze one complete Run on the caller's original writer; no physical I/O."""
        policy = self._policy_values(spec.policy) if policy_values is None else policy_values
        if requirements.parent.number != spec.parent_issue:
            raise KernelError("parent_identity")
        parent_revision, graph_revision = self._source_revisions(requirements)
        spec = replace(spec, parent_revision=parent_revision, graph_revision=graph_revision)
        tickets = {delivery.issue for delivery in spec.deliveries if delivery.kind == DeliveryKind.TICKET}
        if tickets != set(requirements.parent.sub_issue_numbers):
            raise KernelError("delivery_scope")
        if len([delivery for delivery in spec.deliveries if delivery.kind == DeliveryKind.FINAL_RUN]) != bool(tickets):
            raise KernelError("final_target_required")
        if tickets:
            final = next(delivery for delivery in spec.deliveries if delivery.kind == DeliveryKind.FINAL_RUN)
            if any(delivery.target_ref != final.source_ref or delivery.base != final.base
                   for delivery in spec.deliveries if delivery.kind == DeliveryKind.TICKET):
                raise KernelError("run_branch_identity")
        tx.execute("INSERT INTO runs(id,repository,parent_issue,parent_revision,graph_revision,accepted_parent_revision,concurrency,policy,language,repository_root,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (spec.run_id, spec.repository, spec.parent_issue, spec.parent_revision,
                    spec.graph_revision, spec.parent_revision, spec.policy.concurrency, wire_json(policy),
                    spec.language, None if spec.repository_root is None else str(Path(spec.repository_root).resolve()), self.clock()))
        tx = RunTransaction(tx, spec.run_id)
        for delivery in spec.deliveries:
            if delivery.kind == DeliveryKind.FINAL_RUN:
                tx.execute(*run_statement(tx, "INSERT INTO final_target(id,source_ref,target_ref,checkout,owned_root,common_git_dir,run_id) VALUES (?,?,?,?,?,?,?)",
                    (delivery.key, delivery.source_ref, delivery.target_ref, delivery.checkout, delivery.owned_root, delivery.common_git_dir), run_last=True))
                continue
            issue = next((issue for issue in requirements.issues if issue.number == delivery.issue), None)
            revision = spec.parent_revision if delivery.kind == DeliveryKind.PARENT_ONLY else (
                "unread" if issue is None else self._fingerprint({"title": issue.title, "body": issue.body}))
            delivery = replace(delivery, revision=revision, dependencies=())
            self._add_delivery(tx, spec.run_id, spec.policy, delivery)
            if delivery.owned_root:
                tx.execute(*run_statement(tx, "UPDATE subjects SET phase='workspace' WHERE id=? AND run_id=?", (delivery.key,), run_last=True))
        owner = tx.one(*run_statement(tx, "SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1", (), run_last=True))[0]
        self._store_profiles(tx, owner, profiles, {"operation": "create"})
        if spec.inputs is not None:
            values: dict[str, Any] = {name: dict(getattr(spec.inputs, name)) for name in (
                "prompt_resources", "worker_identity", "runner_identity", "notifications", "provenance")}
            values["repository_remote"] = spec.inputs.repository_remote
            material = tx.put(owner, "creation_inputs", wire_json(values))
            tx.execute("INSERT INTO run_inputs VALUES (?,?,?)", (spec.run_id, owner, material))
        self._refresh(tx, requirements, initial=True)

    def _add_delivery(self, tx: Transaction, run_id: str, policy: Policy, delivery: Delivery) -> None:
        selected = transaction_scope(tx)
        if run_id != selected.run_id:
            raise KernelError("allocation_binding")
        final = delivery.kind == DeliveryKind.FINAL_RUN
        tx.execute("""INSERT INTO subjects(
            id,run_id,kind,issue,revision,phase,purpose,source_ref,target_ref,
            base_sha,base_tree,source_sha,candidate_sha,candidate_tree,completion_revision,checkout,workspace_sha,owned_root,common_git_dir)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (delivery.key, run_id, delivery.kind.value, delivery.issue, delivery.revision,
                    "integration" if final else "development", Purpose.INTEGRATION.value if final else Purpose.REQUIREMENTS.value,
                    delivery.source_ref, delivery.target_ref, delivery.base.sha, delivery.base.tree,
                    delivery.base.sha, delivery.base.sha if final else None, delivery.base.tree if final else None,
                    delivery.completion_revision, delivery.checkout, delivery.base.sha, delivery.owned_root, delivery.common_git_dir))
        limits = self._budget_limits(policy, delivery.kind)
        for purpose, (development_limit, review_limit) in limits.items():
            review_required = delivery.kind != DeliveryKind.TICKET or purpose == Purpose.REQUIREMENTS or (purpose == Purpose.CI and policy.ticket_ci_review)
            tx.execute(*run_statement(tx, 'INSERT INTO budgets(subject,purpose,window,development_limit,review_limit,review_required,run_id) VALUES (?,?,1,?,?,?,?)',
                (delivery.key, purpose.value, development_limit, review_limit, review_required), run_last=True))
        tx.execute(*run_statement(tx, 'INSERT INTO resync_allowances(subject,generation,allowance,run_id) VALUES (?,1,?,?)',
            (delivery.key, policy.resync_limit), run_last=True))

    def _budget_limits(self, policy: Policy, kind: DeliveryKind) -> dict[Purpose, tuple[int, int | None]]:
        return {
            Purpose.REQUIREMENTS: (policy.ticket_rounds + 1, policy.ticket_rounds)
            if kind == DeliveryKind.TICKET else (policy.parent_rounds, policy.parent_rounds),
            Purpose.INTEGRATION: (policy.run_repairs, policy.run_repairs + 1)
            if kind == DeliveryKind.FINAL_RUN else (policy.run_repairs if kind == DeliveryKind.PARENT_ONLY else policy.integration_developments,
                                                   0 if kind == DeliveryKind.TICKET else None),
            Purpose.CI: (policy.ticket_ci_developments if kind == DeliveryKind.TICKET else policy.parent_ci_developments, None),
        }

    @staticmethod
    def _fingerprint(value: Any) -> str:
        return "sha256:" + hashlib.sha256(wire_json(value)).hexdigest()

    def _source_revisions(self, requirements: Requirements) -> tuple[str, str]:
        parent = requirements.parent
        members = sorted(parent.sub_issue_numbers)
        return (self._fingerprint({"title": parent.title, "body": parent.body}),
                self._fingerprint({"tickets": members, "dependencies": {
                    str(issue.number): sorted(blocker.number for blocker in issue.blocked_by)
                    for issue in requirements.issues if issue.number in members}}))

    def refresh(self, read: Callable[[], Requirements]) -> dict[str, Any]:
        """Observe real source facts without reopening any operator control."""
        with self.storage.transaction(write=False) as tx:
            sequence = tx.one(*run_statement(tx, 'SELECT source_sequence FROM runs WHERE id=?',
                           (), run_last=True))[0]
            if sequence is None:
                raise KernelError("source_not_configured")
        requirements = read()
        with self.storage.transaction() as tx:
            if tx.one(*run_statement(tx, 'SELECT source_sequence FROM runs WHERE id=?',
                   (), run_last=True))[0] != sequence:
                raise KernelError("source_observation_changed")
            self._refresh(tx, requirements)
            return self._source_status(tx)

    def _refresh(self, tx: Transaction, source: Requirements, *, initial: bool = False,
                 readonly: bool = False) -> bool:
        """Consume exact source bytes, or report that a readonly probe needs a writer."""
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                  (), run_last=True))
        if source.parent.number != run["parent_issue"]:
            raise KernelError("parent_identity")
        parent_revision, graph_revision = self._source_revisions(source)
        order = list(source.parent.sub_issue_numbers)
        if not source.parent.sub_issue_order_reliable:
            order.sort()
        raw = wire_json({"parent": asdict(source.parent), "issues": [
            {**asdict(issue), "labels": sorted(issue.labels)} for issue in source.issues]})
        if len(raw) > 4 * 1024 * 1024:
            raise KernelError("source_limit")
        previous = tx.rows(*run_statement(tx, 'SELECT sequence,body FROM source_observations WHERE run_id=? ORDER BY sequence DESC LIMIT 1',
                       (), run_last=True))
        if previous and previous[0]["body"] == raw:
            return True
        if readonly:
            return False
        tx.execute(*run_statement(tx, 'INSERT INTO source_observations(parent_revision,graph_revision,body,run_id) VALUES (?,?,?,?)',
            (parent_revision, graph_revision, raw), run_last=True))
        sequence = tx.one("SELECT last_insert_rowid()")[0]
        tx.execute(*run_statement(tx, 'INSERT OR IGNORE INTO requirement_versions(issue,revision,title,body,run_id) VALUES (?,?,?,?,?)',
            (source.parent.number, parent_revision, source.parent.title, source.parent.body), run_last=True))
        tx.execute(*run_statement(tx, 'DELETE FROM issue_facts WHERE run_id=?',
            (), run_last=True))
        tx.execute(*run_statement(tx, 'DELETE FROM dependency_facts WHERE run_id=?',
            (), run_last=True))
        by_number = {issue.number: issue for issue in source.issues}
        for issue in source.issues:
            revision = self._fingerprint({"title": issue.title, "body": issue.body})
            tx.execute(*run_statement(tx, 'INSERT OR IGNORE INTO requirement_versions(issue,revision,title,body,run_id) VALUES (?,?,?,?,?)',
                (issue.number, revision, issue.title, issue.body), run_last=True))
            tx.execute(*run_statement(tx, 'INSERT INTO issue_facts(issue,revision,state,labels,position,run_id) VALUES (?,?,?,?,?,?)',
                (issue.number, revision, issue.state.upper(), wire_json(sorted(issue.labels)), order.index(issue.number) if issue.number in order else -1), run_last=True))
            for blocker in issue.blocked_by:
                tx.execute(*run_statement(tx, 'INSERT INTO dependency_facts(issue,blocker,state,run_id) VALUES (?,?,?,?)',
                    (issue.number, blocker.number, blocker.state.upper()), run_last=True))
        tx.execute(*run_statement(tx, 'UPDATE runs SET source_sequence=?,observed_parent_revision=?,observed_graph_revision=? WHERE id=?',
            (sequence, parent_revision, graph_revision), run_last=True))
        if initial:
            initial_jobs = {row["issue"]: row["id"] for row in tx.all(*run_statement(tx, "SELECT id,issue FROM subjects WHERE kind='ticket' AND run_id=?",
                                                                   (), run_last=True))}
            for issue in source.issues:
                for blocker in issue.blocked_by:
                    if issue.number in initial_jobs and blocker.number in initial_jobs:
                        tx.execute(*run_statement(tx, 'INSERT INTO dependencies(subject,predecessor,run_id) VALUES (?,?,?)',
                            (initial_jobs[issue.number], initial_jobs[blocker.number]), run_last=True))
        graph_problem = "missing_ticket" if any(number not in by_number for number in order) else None
        edges = {number: {blocker.number for blocker in by_number[number].blocked_by if blocker.number in order}
                 for number in order if number in by_number}
        remaining = set(edges)
        while remaining:
            ready = {number for number in remaining if not (edges[number] & remaining)}
            if not ready:
                graph_problem = "dependency_cycle"
                break
            remaining -= ready
        jobs = tx.all(*run_statement(tx, 'SELECT * FROM subjects WHERE run_id=?',
                   (), run_last=True))
        reopened = any(job["kind"] == DeliveryKind.TICKET and job["phase"] == "complete"
                       and job["issue"] in by_number and by_number[job["issue"]].state.upper() != "CLOSED" for job in jobs)
        blocked: str | None
        if graph_revision != run["graph_revision"] or reopened:
            blocked = "unsupported_scope_change"
        else:
            blocked = graph_problem
            changed_parent = parent_revision != run["parent_revision"]
            parent_in_use = False
            for job in jobs:
                if job["phase"] == "complete":
                    continue
                facts = tx.rows(*run_statement(tx, 'SELECT revision FROM issue_facts WHERE issue=? AND run_id=?',
                            (job["issue"],), run_last=True))
                revision = parent_revision if job["kind"] != DeliveryKind.TICKET else (facts[0][0] if facts else job["revision"])
                has_work = bool(tx.rows(*run_statement(tx, 'SELECT id FROM attempts WHERE subject=? AND generation=? AND run_id=? LIMIT 1',
                                    (job["id"], job["generation"]), run_last=True)))
                if has_work and (revision != job["revision"] or changed_parent):
                    if job["kind"] == DeliveryKind.FINAL_RUN:
                        if self._final_merge_intent(tx, run, job) is not None:
                            # The original approval owns exact unknown-merge
                            # readback and successful Parent closeout first.
                            parent_in_use = True
                            continue
                        tx.execute(*run_statement(tx, "UPDATE subjects SET refresh_revision=?,phase='refresh_final',grant_id=NULL WHERE id=? AND run_id=?",
                            (parent_revision, job["id"]), run_last=True))
                    else:
                        self._gate(tx, job["id"], "requeue_required")
                    tx.execute(*run_statement(tx, 'UPDATE subjects SET grant_id=NULL WHERE id=? AND run_id=?',
                        (job["id"],), run_last=True))
                    parent_in_use = parent_in_use or changed_parent
                elif not has_work:
                    tx.execute(*run_statement(tx, 'UPDATE subjects SET revision=? WHERE id=? AND run_id=?',
                        (revision, job["id"]), run_last=True))
            if not parent_in_use:
                tx.execute(*run_statement(tx, 'UPDATE runs SET parent_revision=? WHERE id=?',
                    (parent_revision,), run_last=True))
            if tx.rows(*run_statement(tx, "SELECT subject FROM gates WHERE code='requeue_required' AND run_id=? LIMIT 1",
                   (), run_last=True)):
                blocked = "requeue_required"
            elif tx.rows(*run_statement(tx, 'SELECT id FROM subjects WHERE refresh_revision IS NOT NULL AND run_id=? LIMIT 1',
                     (), run_last=True)):
                blocked = "final_refresh"
        managed = {None, "missing_ticket", "dependency_cycle", "unsupported_scope_change", "requeue_required", "final_refresh"}
        if run["blocked"] in managed:
            tx.execute(*run_statement(tx, 'UPDATE runs SET blocked=? WHERE id=?',
                (blocked,), run_last=True))
        owner = jobs[0]["id"]
        self._event(tx, owner, "source_refreshed", str(sequence))
        return True

    def _completion(self, tx: Transaction, job: sqlite3.Row) -> sqlite3.Row | None:
        rows = tx.rows(*run_statement(tx, 'SELECT c.* FROM completions c JOIN runs r ON r.id=c.run_id WHERE c.subject=? AND c.generation=? AND c.revision=? AND c.graph_revision=r.graph_revision AND c.run_id=?',
                   (job["id"], job["generation"], job["revision"]), run_last=True))
        return rows[0] if rows and job["phase"] == "complete" else None

    def _final_ancestry_anchors(self, tx: Transaction) -> tuple[str, ...]:
        # The same current Completion demand owns both query and read fence.
        return tuple(sorted({str(row[0]) for row in tx.all(*run_statement(tx,
            "SELECT c.merge_sha FROM completions c JOIN subjects s ON s.run_id=c.run_id AND s.id=c.subject "
            "AND s.generation=c.generation AND s.revision=c.revision JOIN runs r ON r.id=c.run_id "
            "WHERE s.kind='ticket' AND s.phase='complete' AND c.graph_revision=r.graph_revision AND c.run_id=?",
            (), run_last=True))}))

    def _eligibility(self, tx: Transaction, job: sqlite3.Row) -> str | None:
        if job["kind"] != DeliveryKind.TICKET or tx.one(*run_statement(tx, 'SELECT source_sequence FROM runs WHERE id=?',
                                                     (), run_last=True))[0] is None:
            return None
        facts = tx.rows(*run_statement(tx, 'SELECT * FROM issue_facts WHERE issue=? AND run_id=?',
                    (job["issue"],), run_last=True))
        if not facts:
            return "missing_ticket"
        if job["phase"] == "complete" and self._completion(tx, job) is not None:
            return "completed"
        # Local dependency authority is Completion, never the live CLOSED flag.
        for predecessor in tx.all(*run_statement(tx, 'SELECT s.id,s.generation,s.revision,s.phase FROM dependencies d JOIN subjects s ON s.run_id=d.run_id AND s.id=d.predecessor WHERE d.subject=? AND d.run_id=?',
                               (job["id"],), run_last=True)):
            if self._completion(tx, predecessor) is None:
                return "blocked_by_incomplete_tickets"
        if facts[0]["state"] != "OPEN":
            return "ticket_closed"
        if tx.rows(*run_statement(tx, "SELECT blocker FROM dependency_facts d WHERE d.issue=? AND d.state!='CLOSED' AND d.blocker NOT IN (SELECT issue FROM subjects WHERE kind='ticket' AND run_id=d.run_id) AND d.run_id=? LIMIT 1",
               (job["issue"],), run_last=True)):
            return "blocked_by_open_issues"
        if not tx.rows(*run_statement(tx, 'SELECT id FROM attempts WHERE subject=? AND generation=? AND run_id=? LIMIT 1',
                   (job["id"], job["generation"]), run_last=True)):
            labels: set[str] = set(json.loads(facts[0]["labels"]))
            disqualifying = sorted(labels & {"needs-triage", "needs-info", "ready-for-human"})
            if disqualifying:
                return "disqualifying_label:" + disqualifying[0]
            if "ready-for-agent" not in labels:
                return "missing_ready_for_agent"
        return None

    def source_status(self) -> dict[str, Any]:
        """Return observed revisions and exact current Completion identities.

        External readers reuse these witnesses; they do not hash requirements
        or substitute an accepted revision for a later observed revision.
        """
        with self.storage.transaction(write=False) as tx:
            return self._source_status(tx)

    def _source_status(self, tx: Transaction) -> dict[str, Any]:
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                  (), run_last=True))
        jobs = tx.all(*run_statement(tx, 'SELECT s.* FROM subjects s LEFT JOIN issue_facts i ON i.run_id=s.run_id AND i.issue=s.issue WHERE s.run_id=? ORDER BY i.position,s.issue',
                   (), run_last=True))
        frontier, remaining = [], {}
        for job in jobs:
            if job["kind"] != DeliveryKind.TICKET:
                continue
            reason = self._eligibility(tx, job)
            if reason == "completed":
                continue
            if reason is not None:
                remaining[job["id"]] = reason
            else:
                gates = self._gates(tx, job["id"])
                if gates:
                    remaining[job["id"]] = gates[0]["code"]
                elif not run["blocked"] and not run["stopped"]:
                    frontier.append(job["id"])
        final = next((job["id"] for job in jobs if job["kind"] == DeliveryKind.FINAL_RUN), None)
        summary: dict[str, Any] = {}
        if run["source_sequence"] is not None:
            accepted = json.loads(tx.one(*run_statement(tx, 'SELECT body FROM source_observations WHERE run_id=? ORDER BY sequence LIMIT 1',
                                      (), run_last=True))[0])
            observed = json.loads(tx.one(*run_statement(tx, 'SELECT body FROM source_observations WHERE sequence=? AND run_id=?',
                                      (run["source_sequence"],), run_last=True))[0])
            old_members, new_members = set(accepted["parent"]["sub_issue_numbers"]), set(observed["parent"]["sub_issue_numbers"])
            old_edges = {(issue["number"], blocker["number"]) for issue in accepted["issues"] for blocker in issue["blocked_by"] if issue["number"] in old_members}
            new_edges = {(issue["number"], blocker["number"]) for issue in observed["issues"] for blocker in issue["blocked_by"] if issue["number"] in new_members}
            summary = {"added_tickets": sorted(new_members - old_members), "removed_tickets": sorted(old_members - new_members),
                       "added_dependencies": [{"ticket_number": number, "blocked_by": blocker} for number, blocker in sorted(new_edges - old_edges)],
                       "removed_dependencies": [{"ticket_number": number, "blocked_by": blocker} for number, blocker in sorted(old_edges - new_edges)]}
        abandoned = self._abandon_action(tx)
        return {"frontier": frontier if abandoned is None else [], "remaining": remaining, "blocked": run["blocked"], "stopped": bool(run["stopped"]),
                "abandonment": None if abandoned is None else {"action": abandoned["id"], "status": abandoned["status"]},
                "parent_revision": run["parent_revision"], "observed_parent_revision": run["observed_parent_revision"],
                "accepted_parent_revision": run["accepted_parent_revision"], "graph_change_summary": summary,
                "graph_revision": run["graph_revision"], "observed_graph_revision": run["observed_graph_revision"],
                "observation": run["source_sequence"], "final_subject": final,
                "observed_issues": [dict(row) for row in tx.all(*run_statement(tx, 'SELECT issue,revision FROM issue_facts WHERE run_id=? ORDER BY issue',
                                                             (), run_last=True))],
                "completion_revisions": [dict(row) for row in tx.all(*run_statement(tx, 'SELECT id AS subject,generation,completion_revision FROM subjects WHERE run_id=? ORDER BY id',
                                                                  (), run_last=True))],
                "completions": [dict(row) for row in tx.all(*run_statement(tx, 'SELECT * FROM completions WHERE run_id=? ORDER BY subject,generation',
                                                         (), run_last=True))]}

    def delivery(self, subject: str) -> Delivery:
        """Current resource identity for an external reader; no state machine."""
        with self.storage.transaction(write=False) as tx:
            job = self._job(tx, subject)
            dependencies = tuple(row[0] for row in tx.all(*run_statement(tx, 'SELECT predecessor FROM dependencies WHERE subject=? AND run_id=? ORDER BY predecessor',
                                                       (subject,), run_last=True)))
            return Delivery(subject, DeliveryKind(job["kind"]), job["issue"], job["revision"], Commit(job["base_sha"], job["base_tree"]),
                            job["source_ref"], job["target_ref"], dependencies, job["completion_revision"], job["checkout"], job["owned_root"], job["common_git_dir"])

    def generation_resources(self) -> tuple[ResourceWitness, ...]:
        """Read original current and retired worktree identities for inspection."""
        with self.storage.transaction(write=False) as tx:
            return self._generation_resources(tx)

    def _generation_resources(self, tx: Transaction) -> tuple[ResourceWitness, ...]:
        resources: list[ResourceWitness] = []
        for job in tx.all(*run_statement(tx, 'SELECT * FROM subjects WHERE run_id=? ORDER BY id',
                       (), run_last=True)):
            generations = [*tx.all(*run_statement(tx, 'SELECT * FROM retired_generations WHERE subject=? AND run_id=? ORDER BY generation',
                                (job["id"],), run_last=True)), job]
            for generation in generations:
                subject = job["id"]
                if generation["checkout"]:
                    resources.append(ResourceWitness(subject, generation["generation"], "development", generation["checkout"],
                                                     generation["owned_root"], generation["common_git_dir"], generation["source_ref"],
                                                     generation["workspace_sha"]))
                for checkout in sorted({generation["validation_checkout"], generation["validation_cleanup"]} - {None}):
                    witnesses = [json.loads(tx.get(row[0], subject)) for row in tx.all(*run_statement(tx, "SELECT payload_id FROM effects WHERE subject=? AND generation=? AND operation='validation_checkout' AND status IN ('confirmed','observed') AND run_id=?",
                                                                                    (subject, generation["generation"]), run_last=True))]
                    matches = [value for value in witnesses if value["checkout"] == checkout]
                    if len(matches) != 1:
                        raise KernelError("validation_resource_unproven")
                    saved = matches[0]
                    resources.append(ResourceWitness(subject, generation["generation"], "validation", checkout,
                                                     saved["owned_root"], saved["common_git_dir"], generation["source_ref"], saved["reviewed_commit"]))
        for row in tx.all(*run_statement(tx, "SELECT * FROM effects WHERE operation='validation_checkout' AND status IN ('prepared','sent','observed') AND run_id=? ORDER BY rowid",
                       (), run_last=True)):
            saved = json.loads(tx.get(row["payload_id"], row["subject"]))
            if not any(item.checkout == saved["checkout"] for item in resources):
                resources.append(ResourceWitness(row["subject"], row["generation"], "validation", saved["checkout"],
                                                 saved["owned_root"], saved["common_git_dir"], row["source_ref"], saved["reviewed_commit"]))
        return tuple(resources)

    @staticmethod
    def _check_resource_inspections(resources: tuple[ResourceWitness, ...], inspections: tuple[ResourceInspection, ...], discard: bool) -> None:
        if tuple(item.resource for item in inspections) != resources:
            raise KernelError("abandon_resource_boundary")
        for item in inspections:
            if (item.disposition not in {"clean", "dirty", "absent"}
                    or (item.disposition != "absent" and item.head_sha != item.resource.head_sha)):
                raise KernelError("abandon_resource_unproven", item.resource.checkout)
            if item.disposition == "dirty" and not discard:
                raise KernelError("dirty_worktree", item.resource.checkout)

    def _abandon_action(self, tx: Transaction) -> sqlite3.Row | None:
        rows = tx.rows(*run_statement(tx, "SELECT * FROM actions WHERE kind='abandon' AND run_id=?",
                   (), run_last=True), limit=1)
        return None if not rows else rows[0]

    def _abandon_scope(self, tx: Transaction, action: sqlite3.Row) -> dict[str, Any]:
        selected = transaction_scope(tx)
        if action['run_id'] != selected.run_id:
            raise KernelError("effect_binding")
        owner = action["subject"] or tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1',
                                         (), run_last=True))[0]
        return dict(json.loads(tx.get(action["receipt_id"], owner))["abandonment"])

    def _abandon_discard(self, tx: Transaction, action: sqlite3.Row) -> bool:
        if self._abandon_scope(tx, action)["discard_worktree"]:
            return True
        for row in tx.all(*run_statement(tx, "SELECT subject,receipt_id FROM actions WHERE kind='abandon_continue' AND status='applied' AND run_id=?",
                       (), run_last=True)):
            receipt = json.loads(tx.get(row["receipt_id"], row["subject"]))
            if receipt.get("continuation_of") == action["id"] and receipt.get("discard_worktree") is True:
                return True
        return False

    def _abandon_inspection(self, tx: Transaction, action: sqlite3.Row) -> sqlite3.Row | None:
        rows = tx.rows(*run_statement(tx, "SELECT e.* FROM effects e JOIN history h ON h.run_id=e.run_id AND h.identity=e.id AND h.kind='effect_prepared' WHERE e.control_action=? AND e.operation='inspect_resources' AND h.position>COALESCE((SELECT MAX(position) FROM history WHERE run_id=e.run_id AND kind='abandon_continued' AND identity=?),0) AND e.run_id=? ORDER BY h.position DESC LIMIT 1",
                   (action["id"], action["id"]), run_last=True))
        return rows[0] if rows else None

    def _check_abandon_inspection(self, tx: Transaction, action: sqlite3.Row, row: sqlite3.Row) -> None:
        result = json.loads(tx.get(row["receipt_id"], row["subject"]))
        inspections = tuple(ResourceInspection(**{**value, "resource": ResourceWitness(**value["resource"])})
                            for value in result["raw"].get("resources", []))
        self._check_resource_inspections(self._abandon_resources(tx, action), inspections, self._abandon_discard(tx, action))

    def _abandon_resources(self, tx: Transaction, action: sqlite3.Row) -> tuple[ResourceWitness, ...]:
        scope = self._abandon_scope(tx, action)
        resources = [ResourceWitness(**value) for value in scope["resources"]]
        # A write already released before the fence may settle afterward. Its
        # exact original receipt can advance that resource's known HEAD; it
        # cannot add another checkout, owner or generation to this Action.
        for effect_id in scope["unsettled"]:
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                      (effect_id,), run_last=True))
            if row["status"] != "confirmed" or row["receipt_id"] is None or row["operation"] not in {"workspace", "candidate", "integrate", "publication_commit", "validation_checkout"}:
                continue
            result = json.loads(tx.get(row["receipt_id"], row["subject"]))
            if result["disposition"] != "target" or result["commit"] is None:
                continue
            payload = json.loads(tx.get(row["payload_id"], row["subject"]))
            resources = [replace(item, head_sha=result["commit"]["sha"])
                         if (item.subject, item.generation, item.checkout) == (row["subject"], row["generation"], payload.get("checkout"))
                         else item for item in resources]
        return tuple(resources)

    def _abandon_owned(self, tx: Transaction, action: sqlite3.Row, execution: Execution) -> None:
        selected = transaction_scope(tx)
        if execution.run_id != selected.run_id:
            raise KernelError("executor_fenced")
        scope = self._abandon_scope(tx, action)
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                  (), run_last=True))
        hosts = tx.rows("SELECT * FROM hosts WHERE run_id=? AND owner=? AND epoch=?", (execution.run_id, execution.owner, execution.epoch))
        original = bool(scope["execution"]["owner"] and scope["execution"] == asdict(execution)
                        and run["executor"] == execution.owner and run["epoch"] == execution.epoch + 1)
        admitted = False
        if (hosts and action["run_id"] == execution.run_id and run["executor"] == execution.owner
                and run["epoch"] == execution.epoch):
            request = json.loads(tx.get(hosts[0]["request_id"], hosts[0]["subject"]))
            admitted = (hosts[0]["action_id"] == action["id"] or request["intent"].get("continuation_of") == action["id"])
        if (not (original or admitted) or (not hosts and tx.rows(*run_statement(tx, 'SELECT owner FROM hosts WHERE run_id=? LIMIT 1',
                                                             (), run_last=True)))
                or (hosts and (hosts[0]["status"] != "running" or not hosts[0]["usage_ready"]))):
            raise KernelError("executor_fenced")

    def _abandon_effects(self, tx: Transaction, action: sqlite3.Row) -> list[sqlite3.Row]:
        selected = transaction_scope(tx)
        if action['run_id'] != selected.run_id:
            raise KernelError("effect_binding")
        return tx.all(*run_statement(tx, 'SELECT * FROM effects WHERE control_action=? AND run_id=? ORDER BY rowid',
                   (action["id"],), run_last=True))

    def _abandon_prs(self, tx: Transaction, action: sqlite3.Row) -> list[dict[str, Any]]:
        scope = self._abandon_scope(tx, action)
        generations = {tuple(value) for value in scope["generations"]}
        generations.update((row["subject"], row["generation"]) for row in tx.all(*run_statement(tx, 'SELECT subject,generation FROM retired_generations WHERE run_id=?',
                                                                              (), run_last=True)))
        latest: dict[tuple[str, int], dict[str, Any]] = {}
        for row in tx.all(*run_statement(tx, "SELECT * FROM effects WHERE operation='pr_write' AND status='confirmed' AND receipt_id IS NOT NULL AND run_id=? ORDER BY rowid",
                       (), run_last=True)):
            key = (row["subject"], row["generation"])
            if key not in generations:
                continue
            result = json.loads(tx.get(row["receipt_id"], row["subject"]))
            if result["disposition"] == "target" and result["pr"] is not None:
                latest[key] = {"subject": row["subject"], "generation": row["generation"], "prior_pr": result["pr"]}
        settled = {(row["subject"], row["generation"], row["pr_number"]) for row in tx.all(*run_statement(tx, "SELECT subject,generation,pr_number FROM effects WHERE operation IN ('merge','retire_pr') AND status='confirmed' AND run_id=?",
                                                                                        (), run_last=True))}
        return [value for key, value in latest.items() if (*key, value["prior_pr"]["number"]) not in settled]

    def _abandon_control_scope(self, tx: Transaction, action: sqlite3.Row, row: sqlite3.Row) -> bool:
        selected = transaction_scope(tx)
        if action['run_id'] != selected.run_id or row['run_id'] != selected.run_id:
            raise KernelError("effect_binding")
        if action["status"] != "pending" or row["control_action"] != action["id"] or row["operation"] not in self.ABANDON_EFFECTS:
            return False
        payload = json.loads(tx.get(row["payload_id"], row["subject"]))
        if payload.get("control_action") != action["id"]:
            return False
        discard = self._abandon_discard(tx, action)
        if row["operation"] == "inspect_resources":
            return bool(payload.get("resources") == [resource_wire(item) for item in self._abandon_resources(tx, action)]
                        and type(payload.get("discard_worktree")) is bool and (not payload["discard_worktree"] or discard))
        if row["operation"] == "abandon_worktree":
            resource = payload.get("resource")
            return bool(resource in [resource_wire(item) for item in self._abandon_resources(tx, action)]
                        and type(payload.get("discard_worktree")) is bool and (not payload["discard_worktree"] or discard)
                        and resource["subject"] == row["subject"] and resource["generation"] == row["generation"]
                        and resource["source_ref"] == row["source_ref"] and resource["head_sha"] == row["before_sha"])
        if row["operation"] == "retire_pr":
            return bool(payload.get("control_kind") == "abandon" and any(
                item["subject"] == row["subject"] and item["generation"] == row["generation"] and item["prior_pr"] == payload.get("prior_pr")
                for item in self._abandon_prs(tx, action)))
        completion = payload.get("completion")
        if row["operation"] not in {"reopen_ticket", "recovery_comment"} or not isinstance(completion, dict):
            return False
        records = tx.rows(*run_statement(tx, 'SELECT * FROM completions WHERE subject=? AND generation=? AND close_effect=? AND run_id=?',
                      (row["subject"], row["generation"], completion.get("close_effect")), run_last=True))
        if not records or dict(records[0]) != completion:
            return False
        closed = tx.one(*run_statement(tx, "SELECT * FROM effects WHERE id=? AND subject=? AND generation=? AND operation='close' AND status='confirmed' AND run_id=?",
                     (completion["close_effect"], row["subject"], row["generation"]), run_last=True))
        close_payload = json.loads(tx.get(closed["payload_id"], row["subject"]))
        if (payload.get("issue") != self._job(tx, row["subject"])["issue"]
                or payload.get("close_receipt") != json.loads(tx.get(closed["receipt_id"], row["subject"]))
                or payload.get("close_intent") != close_payload["close_intent"] or row["actor"] != closed["actor"]):
            return False
        if row["operation"] == "reopen_ticket":
            return True
        reopened = tx.rows(*run_statement(tx, "SELECT * FROM effects WHERE id=? AND subject=? AND generation=? AND control_action=? AND operation='reopen_ticket' AND status='confirmed' AND run_id=?",
                       (payload.get("reopen_effect"), row["subject"], row["generation"], action["id"]), run_last=True))
        if not reopened:
            return False
        prior = reopened[0]
        receipt = json.loads(tx.get(prior["receipt_id"], row["subject"]))
        return bool(prior["actor"] == row["actor"] and receipt["disposition"] == "target"
                    and payload.get("reopen_receipt") == receipt
                    and json.loads(tx.get(prior["payload_id"], row["subject"]))["completion"] == completion)

    def _abandon_control_authority(self, tx: Transaction, action: sqlite3.Row, row: sqlite3.Row) -> bool:
        if not self._abandon_control_scope(tx, action, row):
            return False
        if (tx.one(*run_statement(tx, 'SELECT blocked FROM runs WHERE id=?',
                (), run_last=True))[0] == "supervision_timeout"
                or any(gate["code"] == "supervision_timeout" for gate in self._gates(tx, row["subject"]))):
            return False
        if row["operation"] != "inspect_resources":
            inspection = self._abandon_inspection(tx, action)
            if inspection is None or inspection["status"] != "confirmed":
                return False
            self._check_abandon_inspection(tx, action, inspection)
        return True

    def _abandon_reconciliation_blocked(self, tx: Transaction, action: sqlite3.Row) -> bool:
        continued = tx.one(*run_statement(tx, "SELECT COALESCE(MAX(position),0) FROM history WHERE kind='abandon_continued' AND identity=? AND run_id=?",
                        (action["id"],), run_last=True))[0]
        effects = {row["id"]: row for row in self._abandon_effects(tx, action)}
        for effect_id in self._abandon_scope(tx, action)["unsettled"]:
            effects[effect_id] = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                                     (effect_id,), run_last=True))
        for row in effects.values():
            if row["status"] != "sent":
                continue
            pending = tx.one(*run_statement(tx, "SELECT COALESCE(MAX(position),0) FROM history WHERE kind='abandon_reconciliation_pending' AND identity=? AND run_id=?",
                          (row["id"],), run_last=True))[0]
            dispatched = tx.one(*run_statement(tx, "SELECT COALESCE(MAX(position),0) FROM history WHERE kind='effect_dispatched' AND identity=? AND run_id=?",
                             (row["id"],), run_last=True))[0]
            if pending > max(continued, dispatched):
                return True
        return False

    def _effect_owned(self, tx: Transaction, effect: Effect, execution: Execution) -> tuple[sqlite3.Row, sqlite3.Row]:
        selected = transaction_scope(tx)
        if effect.execution.run_id != selected.run_id or execution.run_id != selected.run_id:
            raise KernelError("effect_binding")
        action = self._abandon_action(tx)
        if action is None:
            return self._owned(tx, effect.subject, execution)
        self._abandon_owned(tx, action, execution)
        row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                  (effect.effect_id, effect.subject), run_last=True))
        if not self._abandon_control_authority(tx, action, row):
            raise KernelError("executor_fenced")
        return tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                   (), run_last=True)), self._job(tx, effect.subject)

    def _abandon_write_current(self, tx: Transaction, action: sqlite3.Row, row: sqlite3.Row,
                               inspections: tuple[ResourceInspection, ...] | None) -> None:
        if row["operation"] not in self.REMOTE:
            return
        if inspections is None:
            raise KernelError("abandon_resource_observation_required")
        resources = self._abandon_resources(tx, action)
        self._check_resource_inspections(resources, inspections, self._abandon_discard(tx, action))

    def effect_resources(self, effect: Effect) -> tuple[ResourceWitness, ...] | None:
        """Read the original Action's local witnesses for a fresh write guard."""
        with self.storage.transaction(write=False) as tx:
            selected = transaction_scope(tx)
            if effect.execution.run_id != selected.run_id:
                raise KernelError("effect_binding")
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                      (effect.effect_id, effect.subject), run_last=True))
            if self._effect(tx, row, effect.execution) != effect:
                raise KernelError("effect_binding")
            action = self._abandon_action(tx)
            if action is None or row["control_action"] != action["id"]:
                return None
            return self._abandon_resources(tx, action)

    @staticmethod
    def _event_actor(event: Mapping[str, Any]) -> str | None:
        actor = event.get("actor")
        return actor.get("login") if isinstance(actor, dict) else actor if isinstance(actor, str) else None

    def _reopen_not_owned(self, payload: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
        original = payload["close_receipt"].get("raw", {}).get("event")
        raw = result.get("raw", {})
        latest = raw.get("latest_close")
        closed = bool(isinstance(original, dict) and isinstance(latest, dict)
                    and raw.get("issue") == payload["issue"] and raw.get("state") in {"OPEN", "CLOSED"}
                    and type(latest.get("id")) is int and latest["id"] > original["id"]
                    and latest.get("event") == "closed" and self._event_actor(latest))
        reopened = raw.get("latest_reopen")
        return closed or bool(isinstance(original, dict) and isinstance(reopened, dict)
                    and raw.get("issue") == payload["issue"] and raw.get("state") == "OPEN"
                    and raw.get("prior_close") == original and type(reopened.get("id")) is int
                    and reopened["id"] > original["id"] and reopened.get("event") == "reopened"
                    and self._event_actor(reopened) not in {None, payload["close_intent"]["actor"]})

    def _prepare_abandon_effect(self, tx: Transaction, run: sqlite3.Row, action: sqlite3.Row, subject: str,
                                generation: int, work: Work, world: World, operation: str, *, before: str,
                                payload: dict[str, Any]) -> PrepareIO | EffectIO:
        selected = transaction_scope(tx)
        if action['run_id'] != selected.run_id:
            raise KernelError("effect_binding")
        execution = work.execution
        job = self._job(tx, subject)
        step = self._prepare_effect(tx, run, job, work, world, operation, before=before,
                                    payload={**payload, "control_action": action["id"]})
        if operation == "retire_pr":
            prior = payload["prior_pr"]
            tx.execute(*run_statement(tx, 'UPDATE effects SET generation=?,source_ref=?,target_ref=?,pr_number=?,base_sha=?,base_tree=? WHERE id=? AND run_id=?',
                (generation, prior["source_ref"], prior["target_ref"], prior["number"], prior["base"]["sha"], prior["base"]["tree"], step.effect.effect_id), run_last=True))
        else:
            resource = payload.get("resource")
            tx.execute(*run_statement(tx, 'UPDATE effects SET generation=?,source_ref=? WHERE id=? AND run_id=?',
                (generation, job["source_ref"] if resource is None else resource["source_ref"], step.effect.effect_id), run_last=True))
        return replace(step, effect=self._effect(tx, tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                                                         (step.effect.effect_id,), run_last=True)), execution))

    def _advance_abandon(self, tx: Transaction, run: sqlite3.Row, action: sqlite3.Row, subject: str,
                         work: Work, world: World | None) -> Step | IORequest:
        execution = work.execution
        self._abandon_owned(tx, action, execution)
        waiting = self._abandon_wait(tx, action)
        if waiting is not None:
            return waiting
        scope = self._abandon_scope(tx, action)
        for effect_id in scope["unsettled"]:
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                      (effect_id,), run_last=True))
            if row["operation"] == "issue_link":
                # Optional display keeps its original indeterminate audit;
                # it cannot block the Action or acquire readback acknowledgements.
                continue
            if row["status"] == "sent":
                return self._effect_work(tx, work, row)
            if row["status"] == "observed":
                if world is None or subject != row["subject"]:
                    return self._observe_work(tx, work, row["subject"])
                job = self._job(tx, row["subject"])
                if row["operation"] == "close":
                    return self._apply_effect(tx, run, job, row, world)
                return self._settle_stale_effect(tx, job, row)
            if row["status"] == "prepared":
                tx.execute(*run_statement(tx, "UPDATE effects SET status='cancelled' WHERE id=? AND run_id=?",
                    (row["id"],), run_last=True))
                return Step("progress", row["subject"], "business_intent_cancelled")
        effects = self._abandon_effects(tx, action)
        for row in effects:
            if row["status"] == "sent":
                return self._effect_work(tx, work, row)
            if row["status"] == "observed":
                result = json.loads(tx.get(row["receipt_id"], row["subject"]))
                self._validate_target(tx, run, self._job(tx, row["subject"]), row, result)
                payload = json.loads(tx.get(row["payload_id"], row["subject"]))
                inspection = self._abandon_inspection(tx, action)
                if row["operation"] == "inspect_resources" and inspection is not None and inspection["id"] == row["id"]:
                    try:
                        self._check_abandon_inspection(tx, action, row)
                    except KernelError as error:
                        return Step("paused", row["subject"], error.code)
                elif result["disposition"] != "target" and not (row["operation"] in {"reopen_ticket", "recovery_comment"}
                        and result["disposition"] == "third" and self._reopen_not_owned(payload, result)):
                    return Step("paused", row["subject"], "abandon_outcome_unproven")
                tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
                    (row["id"],), run_last=True))
                self._event(tx, row["subject"], "effect_consumed", row["id"], row["receipt_id"], generation=row["generation"])
                return Step("progress", row["subject"])
        def completed(operation: str, key: str, value: Any) -> bool:
            return any(row["operation"] == operation and row["status"] == "confirmed"
                       and json.loads(tx.get(row["payload_id"], row["subject"])).get(key) == value for row in effects)
        owner = tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1',
                    (), run_last=True))[0]
        inspection = self._abandon_inspection(tx, action)
        if inspection is None or inspection["status"] != "confirmed":
            if inspection is not None and inspection["status"] == "prepared":
                return self._effect_work(tx, work, inspection)
            if world is None or subject != owner:
                return self._observe_work(tx, work, owner)
            return self._prepare_abandon_effect(tx, run, action, owner, self._job(tx, owner)["generation"], work, world,
                "inspect_resources", before=self._job(tx, owner)["workspace_sha"],
                payload={"resources": [resource_wire(item) for item in self._abandon_resources(tx, action)], "discard_worktree": self._abandon_discard(tx, action)})
        self._check_abandon_inspection(tx, action, inspection)
        for row in effects:
            if row["status"] == "retired" and self._abandon_control_scope(tx, action, row):
                prior = tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE kind='abandon_prior_confirmed' AND identity=? AND run_id=? ORDER BY position DESC LIMIT 1",
                            (row["id"],), run_last=True))
                if prior:
                    proof = json.loads(tx.get(prior[0][0], row["subject"]))
                    if proof["dispatch_serial"] == row["attempts"] and proof["result"]["disposition"] == "prior":
                        tx.execute(*run_statement(tx, "UPDATE effects SET status='prepared' WHERE id=? AND run_id=?",
                            (row["id"],), run_last=True))
                        refreshed = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                                        (row["id"],), run_last=True))
                        return self._effect_work(tx, work, refreshed)
            if row["status"] == "prepared":
                return self._effect_work(tx, work, row)
        prs = self._abandon_prs(tx, action)
        for final in (False, True):
            for item in prs:
                if (self._job(tx, item["subject"])["kind"] == DeliveryKind.FINAL_RUN) != final:
                    continue
                if completed("retire_pr", "prior_pr", item["prior_pr"]):
                    continue
                if world is None or subject != item["subject"]:
                    return self._observe_work(tx, work, item["subject"])
                return self._prepare_abandon_effect(tx, run, action, item["subject"], item["generation"], work, world,
                    "retire_pr", before=item["prior_pr"]["head"]["sha"], payload={"prior_pr": item["prior_pr"], "control_kind": "abandon"})
            if final:
                continue
            for completion in tx.all(*run_statement(tx, 'SELECT * FROM completions WHERE run_id=? ORDER BY subject,generation',
                                  (), run_last=True)):
                value = dict(completion)
                reopened = next((row for row in effects if row["operation"] == "reopen_ticket" and row["status"] == "confirmed"
                                 and json.loads(tx.get(row["payload_id"], row["subject"]))["completion"] == value), None)
                if reopened is not None:
                    receipt = json.loads(tx.get(reopened["receipt_id"], completion["subject"]))
                    if receipt["disposition"] != "target" or completed("recovery_comment", "completion", value):
                        continue
                    if world is None or subject != completion["subject"]:
                        return self._observe_work(tx, work, completion["subject"])
                    payload = json.loads(tx.get(reopened["payload_id"], completion["subject"]))
                    issue = payload["issue"]
                    body = (f"Delivery Run `{run['id']}` was abandoned before entering the default branch. "
                            f"Reopened Ticket #{issue}; its prior completion was recorded by PR #{completion['pr_number']} "
                            f"at Run Branch commit `{completion['merge_sha']}`.")
                    return self._prepare_abandon_effect(tx, run, action, completion["subject"], completion["generation"], work, world,
                        "recovery_comment", before=completion["merge_sha"], payload={"issue": issue, "completion": value,
                         "close_receipt": payload["close_receipt"], "close_intent": payload["close_intent"],
                         "reopen_effect": reopened["id"], "reopen_receipt": receipt, "comment_body": body})
                if world is None or subject != completion["subject"]:
                    return self._observe_work(tx, work, completion["subject"])
                closed = tx.one(*run_statement(tx, "SELECT * FROM effects WHERE id=? AND operation='close' AND status='confirmed' AND run_id=?",
                             (completion["close_effect"],), run_last=True))
                payload = json.loads(tx.get(closed["payload_id"], completion["subject"]))
                return self._prepare_abandon_effect(tx, run, action, completion["subject"], completion["generation"], work, world,
                    "reopen_ticket", before=completion["merge_sha"], payload={"issue": self._job(tx, completion["subject"])["issue"],
                     "completion": value, "close_receipt": json.loads(tx.get(closed["receipt_id"], completion["subject"])), "close_intent": payload["close_intent"]})
        for witness in self._abandon_resources(tx, action):
            resource = resource_wire(witness)
            if completed("abandon_worktree", "resource", resource):
                continue
            if world is None or subject != resource["subject"]:
                return self._observe_work(tx, work, resource["subject"])
            return self._prepare_abandon_effect(tx, run, action, resource["subject"], resource["generation"], work, world,
                "abandon_worktree", before=resource["head_sha"], payload={"resource": resource, "discard_worktree": self._abandon_discard(tx, action)})
        tx.execute(*run_statement(tx, "UPDATE actions SET status='complete' WHERE id=? AND run_id=?",
            (action["id"],), run_last=True))
        self._event(tx, owner, "abandoned", action["id"], action["receipt_id"])
        return Step("complete", subject, "abandoned")

    def _activate_final(self, execution: Execution, run_head: Commit, default_head: Commit, *, ancestry: tuple[tuple[str, str], ...], token: str | ReadFence) -> str:
        """Create Final only from exact Completion and observed Git ancestry."""
        with self.storage.transaction() as tx:
            self._check_operation(tx, token, execution)
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            self._executor_owned(tx, run, execution)
            if run["stopped"] or run["blocked"] or run["source_sequence"] is None:
                raise KernelError("final_admission_closed")
            existing = tx.rows(*run_statement(tx, "SELECT id FROM subjects WHERE kind='final_run' AND run_id=?",
                           (), run_last=True))
            if existing:
                return str(existing[0][0])
            jobs = tx.all(*run_statement(tx, "SELECT * FROM subjects WHERE kind='ticket' AND run_id=? ORDER BY issue",
                       (), run_last=True))
            completed = [self._completion(tx, job) for job in jobs]
            if not jobs or any(completion is None for completion in completed):
                raise KernelError("ticket_completion_required")
            records = [dict(completion) for completion in completed if completion is not None]
            if any(record["merge_sha"] != run_head.sha and (record["merge_sha"], run_head.sha) not in ancestry for record in records):
                raise KernelError("completion_not_in_run")
            target = tx.one(*run_statement(tx, 'SELECT * FROM final_target WHERE run_id=?',
                         (), run_last=True))
            completion_revision = self._fingerprint(records)
            delivery = Delivery(target["id"], DeliveryKind.FINAL_RUN, run["parent_issue"], run["parent_revision"], run_head,
                                target["source_ref"], target["target_ref"], completion_revision=completion_revision, checkout=target["checkout"],
                                owned_root=target["owned_root"], common_git_dir=target["common_git_dir"])
            self._add_delivery(tx, run["id"], self._policy(run), delivery)
            tx.execute(*run_statement(tx, "UPDATE subjects SET phase='workspace' WHERE id=? AND run_id=?",
                (delivery.key,), run_last=True))
            review_evidence = []
            for record in records:
                item = dict(record)
                for key, role in (("review_attempt", "review"), ("development_attempt", "development")):
                    if record[key] is not None:
                        attempt = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND subject=? AND run_id=?',
                                      (record[key], record["subject"]), run_last=True))
                        item[role] = {"attempt": attempt["id"], "base_sha": attempt["base_sha"], "candidate_sha": attempt["candidate_sha"],
                                      "candidate_tree": attempt["candidate_tree"], "outcome": attempt["outcome"],
                                      "artifact": json.loads(tx.get(attempt["result_id"], record["subject"]))}
                completed_job = self._job(tx, record["subject"])
                item["delivered_candidate"] = {"sha": completed_job["candidate_sha"], "tree": completed_job["candidate_tree"],
                                                "development_base": completed_job["base_sha"]}
                review_evidence.append(item)
            evidence = tx.put(delivery.key, "completion_barrier", wire_json(review_evidence))
            tx.execute(*run_statement(tx, 'INSERT INTO final_barriers(subject,generation,completion_revision,run_sha,default_sha,evidence,run_id) VALUES (?,1,?,?,?,?,?)',
                (delivery.key, completion_revision, run_head.sha, default_head.sha, evidence), run_last=True))
            self._event(tx, delivery.key, "final_activated", completion_revision, evidence)
            return delivery.key

    def requeue(self, subject: str, action_id: str, replacement: Delivery, world: World) -> str:
        """Explicitly retire one stale generation, retaining its work and siblings."""
        with self.storage.transaction(ordered=False) as tx:
            return self._control(tx, "requeue", action_id, subject=subject, world=world, replacement=replacement)

    def _validate_requeue(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                          replacement: Delivery, world: World) -> str:
        self._control_binding(tx, run, job)
        subject = job["id"]
        if job["kind"] == DeliveryKind.FINAL_RUN:
            raise KernelError("final_reacceptance_required")
        if run["stopped"] or run["blocked"] != "requeue_required" or run["final_action"]:
            raise KernelError("requeue_not_authorized")
        if not any(gate["code"] == "requeue_required" for gate in self._gates(tx, subject)):
            raise KernelError("requeue_not_authorized")
        if self._agent_seats(tx, subject) or self._pending_effect(tx, subject) is not None:
            raise KernelError("generation_unsettled")
        if (replacement.key != subject or replacement.kind != job["kind"] or replacement.issue != job["issue"]
                or replacement.source_ref == job["source_ref"] or replacement.checkout == job["checkout"]
                or replacement.target_ref != job["target_ref"] or replacement.base != world.target):
            raise KernelError("replacement_identity")
        revision = (run["observed_parent_revision"] if job["kind"] != DeliveryKind.TICKET else
                    tx.one(*run_statement(tx, 'SELECT revision FROM issue_facts WHERE issue=? AND run_id=?',
                        (job["issue"],), run_last=True))[0])
        if world.revision != revision or world.parent_revision != run["observed_parent_revision"] or world.graph_revision != run["graph_revision"]:
            raise KernelError("replacement_requirements")
        return str(revision)

    def _requeue_final(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World) -> None:
        """Fresh whole-Run Acceptance shares the existing invalidation transition."""
        if (run["stopped"] or run["blocked"] != "final_refresh" or job["refresh_revision"] is None
                or any(gate["code"] in {"operator_stopped", "execution_failed"} for gate in self._gates(tx, job["id"]))):
            raise KernelError("requeue_not_authorized")
        if (self._agent_seats(tx, job["id"])
                or self._pending_effect(tx, job["id"]) is not None):
            raise KernelError("generation_unsettled")
        if job["merge_sha"] or self._final_merge_intent(tx, run, job) is not None:
            raise KernelError("final_merge_unsettled")
        if (world.source is None or world.target is None or world.revision != job["refresh_revision"]
                or world.parent_revision != run["observed_parent_revision"] or world.graph_revision != run["graph_revision"]
                or world.completion_revision != job["completion_revision"]):
            raise KernelError("replacement_requirements")
        refreshed = self._refresh_final(tx, run, job, world)
        if refreshed.kind != "progress":
            raise KernelError("requeue_not_authorized", refreshed.reason)

    def _replace_generation(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, replacement: Delivery,
                            revision: str, action_id: str, payload: str) -> None:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or job['run_id'] != selected.run_id:
            raise KernelError("allocation_binding")
        subject = job["id"]
        self._retire_generation(tx, job, "requirements_changed")
        tx.execute(*run_statement(tx, "UPDATE subjects SET generation=generation+1,revision=?,phase='workspace',purpose='requirements',source_ref=?,checkout=?,base_sha=?,base_tree=?,source_sha=?,workspace_sha=?,owned_root=?,common_git_dir=?,candidate_sha=NULL,candidate_tree=NULL,integrated_sha=NULL,integrated_tree=NULL,expected_tree=NULL,integration_target=NULL,current_attempt=NULL,review_attempt=NULL,publication_attempt=NULL,publication_sha=NULL,publication_tree=NULL,pr_number=NULL,checks_id=NULL,grant_id=NULL,merge_sha=NULL,gate_mode=NULL,development_thread=NULL,ci_failure=NULL,last_failure=NULL,resume_purpose=NULL,repair_source=NULL,validation_checkout=NULL,validation_cleanup=NULL,version=version+1 WHERE id=? AND run_id=?",
            (revision, replacement.source_ref, replacement.checkout, replacement.base.sha, replacement.base.tree, replacement.base.sha, replacement.base.sha, replacement.owned_root, replacement.common_git_dir, subject), run_last=True))
        tx.execute(*run_statement(tx, 'DELETE FROM issue_links WHERE subject=? AND run_id=?',
            (subject,), run_last=True))
        tx.execute(*run_statement(tx, 'UPDATE runs SET parent_revision=? WHERE id=?',
            (run["observed_parent_revision"],), run_last=True))
        for budget in tx.rows(*run_statement(tx, 'SELECT * FROM budgets WHERE subject=? AND current=1 AND run_id=?',
                          (subject,), run_last=True)):
            limits = self._budget_limits(self._subject_policy(tx, job), DeliveryKind(job["kind"]))[Purpose(budget["purpose"])]
            tx.execute(*run_statement(tx, 'UPDATE budgets SET current=0 WHERE subject=? AND purpose=? AND current=1 AND run_id=?',
                (subject, budget["purpose"]), run_last=True))
            tx.execute(*run_statement(tx, 'INSERT INTO budgets(subject,purpose,window,development_limit,review_limit,review_required,run_id) VALUES (?,?,?,?,?,?,?)',
                (subject, budget["purpose"], budget["window"] + 1, *limits, budget["review_required"]), run_last=True))
        self._renew_resync(tx, job, require_exhaustion=False)
        tx.execute(*run_statement(tx, 'DELETE FROM gates WHERE subject=? AND run_id=?',
            (subject,), run_last=True))
        self._release_integration(tx, subject)
        tx.execute(*run_statement(tx, 'DELETE FROM work_queue WHERE subject=? AND run_id=?',
            (subject,), run_last=True))
        if not tx.rows(*run_statement(tx, "SELECT subject FROM gates WHERE code='requeue_required' AND run_id=? LIMIT 1",
                   (), run_last=True)):
            tx.execute(*run_statement(tx, "UPDATE runs SET blocked=NULL WHERE blocked='requeue_required' AND id=?",
                (), run_last=True))
        self._event(tx, subject, "generation_requeued", action_id, payload)

    def _pending_requeue(self, tx: Transaction, subject: str) -> sqlite3.Row | None:
        rows = tx.rows(*run_statement(tx, "SELECT * FROM actions WHERE kind='requeue' AND subject=? AND status='pending' AND run_id=?",
                   (subject,), run_last=True), limit=1)
        return None if not rows else rows[0]

    def _requeue_scope(self, tx: Transaction, action: sqlite3.Row) -> dict[str, Any]:
        selected = transaction_scope(tx)
        if action['run_id'] != selected.run_id:
            raise KernelError("effect_binding")
        return dict(json.loads(tx.get(action["receipt_id"], action["subject"]))["requeue"])

    def _recorded_retirement(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World) -> sqlite3.Row:
        self._control_binding(tx, run, job)
        rows = tx.rows(*run_statement(tx, "SELECT * FROM effects WHERE subject=? AND generation=? AND pr_number=? AND operation='retire_pr' AND status='confirmed' AND run_id=? ORDER BY rowid DESC LIMIT 1",
                   (job["id"], job["generation"], job["pr_number"]), run_last=True))
        if not rows or not self._retirement_current(tx, run, job, rows[0], world):
            raise KernelError("retirement_pr_changed")
        return rows[0]

    def _retirement_current(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                            row: sqlite3.Row, world: World) -> bool:
        """CLOSED is usable only with this generation's exact owned retirement."""
        if (row["operation"] != "retire_pr" or row["status"] not in {"observed", "confirmed"}
                or row["subject"] != job["id"] or row["generation"] != job["generation"]
                or row["pr_number"] != job["pr_number"] or row["receipt_id"] is None
                or world.pr is None or self._pr_target(run, job, world, state="CLOSED") is None):
            return False
        result = json.loads(tx.get(row["receipt_id"], job["id"]))
        if result["disposition"] != "target":
            return False
        self._validate_target(tx, run, job, row, result)
        pr = result.get("pr")
        return bool(isinstance(pr, dict) and world.pr.number == pr["number"]
                    and asdict(world.pr.head) == pr["head"] and world.pr.actor == pr["actor"]
                    and (asdict(world.pr.base) == pr["base"] or world.pr.base.sha != pr["base"]["sha"]
                         and (pr["base"]["sha"], world.pr.base.sha) in world.ancestry))

    def _recheck_retirement(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, action: sqlite3.Row,
                            row: sqlite3.Row, result: EffectResult) -> Step:
        original = json.loads(tx.get(row["receipt_id"], job["id"]))
        # Anchor later intervention to the confirmed receipt, never to a new write grant.
        events = result.raw.get("state_events")
        pr = None if result.pr is None else asdict(result.pr)
        if (result.disposition == "unknown" and isinstance(events, list) and len(events) > 1
                and len(events) % 2 == 1 and all(isinstance(event, dict) for event in events)
                and pr is not None and pr["state"] == "CLOSED"
                and all(pr[key] == original["pr"][key] for key in
                        ("number", "repository", "head_repository", "source_ref", "target_ref", "head", "actor"))
                and result.raw.get("close_intent") == original["raw"].get("close_intent")
                and events[0] == original["raw"].get("event")):
            try:
                times = [datetime.fromisoformat(event["created_at"]) for event in events]
                intervened = (all(type(event.get("id")) is int and event["id"] > 0
                                  and event.get("event") == ("closed" if index % 2 == 0 else "reopened")
                                  and isinstance(event.get("actor"), str) and bool(event["actor"])
                                  for index, event in enumerate(events))
                    and all(event["id"] < later["id"] for event, later in zip(events, events[1:]))
                    and all(instant.utcoffset() is not None for instant in times)
                    and all(instant <= later for instant, later in zip(times, times[1:]))
                    and any(event["actor"] != row["actor"] for event in events[1:]))
            except (KeyError, TypeError, ValueError):
                intervened = False
            if intervened:
                result = replace(result, disposition="third")
        observed = asdict(result)
        if result.disposition == "target":
            self._validate_target(tx, run, job, row, observed)
            if (result.raw.get("event") != original["raw"].get("event")
                    or result.raw.get("close_intent") != original["raw"].get("close_intent")):
                result = replace(result, disposition="third")
                observed = asdict(result)
        material = tx.put(job["id"], "retirement_readback", wire_json({"effect": row["id"], "receipt": row["receipt_id"], "result": observed}))
        if result.disposition == "target":
            previous = tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE kind='retirement_rechecked' AND identity=? AND run_id=? ORDER BY position DESC LIMIT 1",
                           (action["id"],), run_last=True))
            if not previous or previous[0][0] != material:
                self._event(tx, job["id"], "retirement_rechecked", action["id"], material)
            return Step("progress", job["id"])
        if result.disposition == "unknown":
            self._gate(tx, job["id"], "write_result_unknown", evidence=material)
            self._event(tx, job["id"], "retirement_recheck_pending", action["id"], material)
            return Step("paused", job["id"], "write_result_unknown")
        tx.execute(*run_statement(tx, "UPDATE actions SET status='superseded' WHERE id=? AND run_id=?",
            (action["id"],), run_last=True))
        self._gate(tx, job["id"], "requeue_retirement_changed", evidence=material)
        self._event(tx, job["id"], "requeue_superseded", action["id"], material)
        return Step("progress", job["id"], "requeue_retirement_changed")

    def _requeue_versions_current(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                                  action: sqlite3.Row, world: World | None) -> bool:
        scope = self._requeue_scope(tx, action)
        facts = tx.rows(*run_statement(tx, 'SELECT revision FROM issue_facts WHERE issue=? AND run_id=?',
                    (job["issue"],), run_last=True))
        revision = run["observed_parent_revision"] if job["kind"] != DeliveryKind.TICKET else None if not facts else facts[0][0]
        return bool(scope["generation"] == job["generation"] and scope["revision"] == revision
                    and scope["parent_revision"] == run["observed_parent_revision"]
                    and scope["graph_revision"] == run["graph_revision"] == run["observed_graph_revision"]
                    and (world is None or (world.revision == scope["revision"] and world.parent_revision == scope["parent_revision"]
                         and world.graph_revision == scope["graph_revision"])))

    def _end_requeue(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, action: sqlite3.Row,
                     pending: sqlite3.Row | None, reason: str) -> Step:
        if pending is not None:
            if pending["operation"] != "retire_pr":
                raise KernelError("generation_unsettled")
            if pending["status"] == "observed":
                result = json.loads(tx.get(pending["receipt_id"], job["id"]))
                self._validate_target(tx, run, job, pending, result)
                if result["disposition"] != "target":
                    return Step("paused", job["id"], "retirement_unconfirmed")
                tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
                    (pending["id"],), run_last=True))
                self._event(tx, job["id"], "effect_consumed", pending["id"], pending["receipt_id"])
            elif pending["status"] == "prepared":
                tx.execute(*run_statement(tx, "UPDATE effects SET status='cancelled' WHERE id=? AND run_id=?",
                    (pending["id"],), run_last=True))
        tx.execute(*run_statement(tx, "UPDATE actions SET status='superseded' WHERE id=? AND run_id=?",
            (action["id"],), run_last=True))
        self._gate(tx, job["id"], reason)
        self._event(tx, job["id"], "requeue_superseded", action["id"], None if pending is None else pending["receipt_id"])
        return Step("paused", job["id"], reason)

    def _requeue_current(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, action: sqlite3.Row,
                         world: World, *, retirement: sqlite3.Row | None = None) -> bool:
        scope = self._requeue_scope(tx, action)
        original, pr = scope["prior_pr"], world.pr
        if retirement is not None:
            payload = json.loads(tx.get(retirement["payload_id"], job["id"]))
            if (payload.get("control_kind") != "requeue" or retirement["id"] != scope.get("retired_effect")
                    and payload.get("control_action") != action["id"]):
                return False
        return bool(not run["stopped"] and run["blocked"] == "requeue_required" and not run["final_action"]
                    and all(gate["code"] == "requeue_required" for gate in self._gates(tx, job["id"]))
                    and self._requeue_versions_current(tx, run, job, action, world)
                    and world.target is not None and (scope["target"] == asdict(world.target)
                        or (scope["target"]["sha"] != world.target.sha
                            and (scope["target"]["sha"], world.target.sha) in world.ancestry))
                    and original is not None and pr is not None and self._pr_identity(run, job, pr)
                    and pr.number == original["number"] and asdict(pr.head) == original["head"]
                    and (self._pr_target(run, job, world) is not None if retirement is None
                         else self._retirement_current(tx, run, job, retirement, world))
                    and pr.actor == original["actor"]
                    and pr.state == ("OPEN" if retirement is None else "CLOSED"))

    def _advance_requeue(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, action: sqlite3.Row,
                         work: Work, world: World | None) -> Step | IORequest:
        execution = work.execution
        subject = job["id"]
        scope = self._requeue_scope(tx, action)
        pending = self._pending_effect(tx, subject)
        if pending is not None and pending["status"] == "sent":
            return self._effect_work(tx, work, pending)
        if not self._requeue_versions_current(tx, run, job, action, world):
            return self._end_requeue(tx, run, job, action, pending, "requeue_scope_changed")
        if run["stopped"] or any(gate["code"] != "requeue_required" for gate in self._gates(tx, subject)):
            return Step("paused", subject, self._pause_reason(tx, subject, run))
        readback = None
        retirement = None
        if scope.get("retired_effect") is not None:
            original = tx.one(*run_statement(tx, "SELECT * FROM effects WHERE id=? AND subject=? AND generation=? AND status='confirmed' AND run_id=?",
                           (scope["retired_effect"], subject, job["generation"]), run_last=True))
            previous = tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE kind='retirement_rechecked' AND identity=? AND run_id=? ORDER BY position DESC LIMIT 1",
                           (action["id"],), run_last=True))
            if not previous:
                return self._effect_work(tx, work, original)
            readback = previous[0][0]
            retirement = original
        if pending is not None and pending["status"] == "observed":
            retirement = pending
        if world is None:
            return self._observe_work(tx, work, subject)
        if not self._requeue_current(tx, run, job, action, world, retirement=retirement):
            return self._end_requeue(tx, run, job, action, pending, "requeue_retirement_changed")
        if pending is None and readback is None:
            scope = self._requeue_scope(tx, action)
            return self._prepare_effect(tx, run, job, work, world, "retire_pr", before=scope["prior_pr"]["head"]["sha"],
                                        payload={"control_action": action["id"], "control_kind": "requeue", "prior_pr": scope["prior_pr"]})
        if readback is None:
            assert pending is not None
            if pending["operation"] != "retire_pr":
                raise KernelError("generation_unsettled")
            if pending["status"] == "prepared":
                return self._effect_work(tx, work, pending)
            tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
                (pending["id"],), run_last=True))
            self._event(tx, subject, "effect_consumed", pending["id"], pending["receipt_id"])
            retirement_effect, retirement_receipt = pending["id"], pending["receipt_id"]
        else:
            retirement_effect, retirement_receipt = scope["retired_effect"], readback
            self._event(tx, subject, "retirement_recheck_consumed", action["id"], readback)
        command = json.loads(tx.get(action["payload_id"], subject))
        replacement = command["replacement"]
        delivery = Delivery(**{**replacement, "kind": DeliveryKind(replacement["kind"]),
                               "base": self._require_target(world), "dependencies": tuple(replacement["dependencies"])})
        material = tx.put(subject, "generation_replacement", wire_json({"action": action["id"], "replacement": asdict(delivery),
            "retirement_effect": retirement_effect, "retirement_receipt": retirement_receipt, "observed_target": asdict(self._require_target(world))}))
        self._replace_generation(tx, run, job, delivery, self._requeue_scope(tx, action)["revision"], action["id"], material)
        tx.execute(*run_statement(tx, "UPDATE actions SET status='applied' WHERE id=? AND run_id=?",
            (action["id"],), run_last=True))
        return Step("progress", subject, "generation_requeued")

    def _charge_development(self, tx: Transaction, attempt_id: str) -> None:
        attempt = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                      (attempt_id,), run_last=True))
        if attempt["role"] == Role.DEVELOPMENT and not attempt["accounted"]:
            tx.execute(*run_statement(tx, 'UPDATE budgets SET developments_used=developments_used+1 WHERE subject=? AND purpose=? AND window=? AND run_id=?',
                (attempt["subject"], attempt["purpose"], attempt["window"]), run_last=True))
            tx.execute(*run_statement(tx, 'UPDATE attempts SET accounted=1 WHERE id=? AND run_id=?',
                (attempt_id,), run_last=True))

    def _charge_review(self, tx: Transaction, attempt: sqlite3.Row) -> None:
        selected = transaction_scope(tx)
        if attempt['run_id'] != selected.run_id:
            raise KernelError("attempt_binding")
        if attempt["role"] == Role.REVIEW and not attempt["accounted"]:
            tx.execute(*run_statement(tx, 'UPDATE budgets SET reviews_used=reviews_used+1 WHERE subject=? AND purpose=? AND window=? AND run_id=?',
                (attempt["subject"], attempt["purpose"], attempt["window"]), run_last=True))
            tx.execute(*run_statement(tx, 'UPDATE attempts SET accounted=1 WHERE id=? AND run_id=?',
                (attempt["id"],), run_last=True))

    def _retire_generation(self, tx: Transaction, job: sqlite3.Row, reason: str) -> None:
        selected = transaction_scope(tx)
        if job['run_id'] != selected.run_id:
            raise KernelError("allocation_binding")
        tx.execute(*run_statement(tx, 'INSERT INTO retired_generations(subject,generation,revision,source_ref,checkout,source_sha,candidate_sha,publication_sha,pr_number,reason,validation_checkout,validation_cleanup,workspace_sha,owned_root,common_git_dir,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (job["id"], job["generation"], job["revision"], job["source_ref"], job["checkout"], job["source_sha"],
                    job["candidate_sha"], job["publication_sha"], job["pr_number"], reason, job["validation_checkout"], job["validation_cleanup"],
                    job["workspace_sha"], job["owned_root"], job["common_git_dir"]), run_last=True))
        if job["current_attempt"]:
            attempt = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                          (job["current_attempt"],), run_last=True))
            if attempt["role"] == Role.REVIEW and attempt["result_id"]:
                try:
                    artifact(Role.REVIEW, tx.get(attempt["result_id"], job["id"]))
                except KernelError:
                    pass
                else:
                    self._charge_review(tx, attempt)
            tx.execute(*run_statement(tx, "UPDATE attempts SET status='stale' WHERE id=? AND run_id=?",
                (attempt["id"],), run_last=True))

    @staticmethod
    def _task(run: sqlite3.Row) -> TaskKey:
        if run["repository_root"] is None:
            raise KernelError("task_workspace_unknown")
        return TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])

    @staticmethod
    @contextmanager
    def _task_admission(task: TaskKey, *, wait: bool = True) -> Iterator[None]:
        """Retain the existing Task mutex and unrecorded-revocation FD barrier."""
        with task_lock(task, wait=wait):
            yield None

    def _require_current_task(self, tx: Transaction, task: TaskKey, run_id: str) -> None:
        """Canonical publication check inside the exact mutation snapshot."""
        scope = transaction_scope(tx)
        if (scope.run_id != run_id or TaskKey(Path(scope.repository_root), scope.repository, scope.parent_issue) != task or (not run_is_current(tx))):
            raise KernelError("task_location_identity")

    def _replaceable(self, task: TaskKey, run_id: str) -> bool:
        with self.storage.transaction(write=False) as tx:
            return self._replaceable_in(tx, task, run_id)

    def _replaceable_in(self, tx: Transaction, task: TaskKey, run_id: str) -> bool:
        """The original predicate, also usable inside an atomic Task writer."""
        selected = transaction_scope(tx)
        if selected.run_id != run_id:
            raise KernelError("task_location_identity")
        run = tx.one(*run_statement(tx, "SELECT * FROM runs WHERE id=?", (), run_last=True))
        if run["id"] != run_id or self._task(run) != task:
            raise KernelError("task_location_identity")
        abandoned = self._abandon_action(tx)
        completed_abandonment = bool(abandoned is not None and abandoned["status"] == "complete")
        return bool(run['executor'] is None and (completed_abandonment or not tx.rows(*run_statement(tx, "SELECT id FROM subjects WHERE phase!='complete' AND run_id=? LIMIT 1", (), run_last=True))) and (completed_abandonment or not tx.rows(*run_statement(tx, "SELECT id FROM final_target WHERE run_id=? AND id NOT IN (SELECT id FROM subjects WHERE run_id=? AND kind='final_run' AND phase='complete') LIMIT 1", (run_id,)))) and (not self._agent_seats(tx)) and (not tx.rows(*run_statement(tx, "SELECT owner FROM retired_executors WHERE status!='exited' AND run_id=? LIMIT 1", (), run_last=True))) and (not tx.rows(*run_statement(tx, "SELECT owner FROM hosts WHERE (status!='exited' OR usage_released=0) AND run_id=? LIMIT 1", (), run_last=True))) and (not tx.rows(*run_statement(tx, "SELECT id FROM effects WHERE status IN ('prepared','sent','observed') AND operation!='issue_link' AND run_id=? LIMIT 1", (), run_last=True))) and (completed_abandonment or not tx.rows(*run_statement(tx, "SELECT subject FROM resource_cleanup WHERE state!='complete' AND run_id=? LIMIT 1", (), run_last=True))))

    @classmethod
    def open_task(cls, task: TaskKey) -> Kernel | None:
        """Read the managed repository's canonical Task pointer, never JSON."""
        from agent_run.adapters.workspace import ManagedWorkspace
        from agent_run.storage.routing import RepositoryRoute
        workspace = ManagedWorkspace.for_repository(task.repository)
        if workspace.repository_root.resolve() != task.workspace:
            raise KernelError("task_location_identity")
        route = RepositoryRoute.for_workspace(workspace)
        if not route.database.exists() and not route.database.is_symlink():
            route._require_vacant_root(route.database)
            return None
        with task_lock(task):
            return route.open_task(task)

    def active_host_action(self) -> str | None:
        """Read the original Action until its Host and usage are both retired."""
        with self.storage.transaction(write=False) as tx:
            rows = tx.rows(*run_statement(tx, "SELECT action_id FROM hosts WHERE (status!='exited' OR usage_released=0) AND run_id=?",
                       (), run_last=True), limit=1)
            return None if not rows else str(rows[0][0])

    def host_dispatched(self, action_id: str) -> bool:
        """Read the durable launch claim, never infer it from process absence."""
        with self.storage.transaction(write=False) as tx:
            return tx.one(*run_statement(tx, 'SELECT dispatched_at FROM hosts WHERE action_id=? AND run_id=?',
                       (action_id,), run_last=True))[0] is not None

    def admit_host(self, action_id: str, *, kind: str, command: tuple[str, ...], environment_carrier: Path,
                   preflight: Callable[[], None], subject: str | None = None, world: World | None = None,
                   message: str | None = None, new_thread: bool = False,
                   policy_overrides: Mapping[str, Any] | None = None, replacement: Delivery | None = None,
                   resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False,
                   continuation_of: str | None = None, backend: Literal["systemd", "foreground"] = "systemd") -> HostLaunch:
        """Bind one Task and apply its explicit control with the same Host intent."""
        if (backend not in {"systemd", "foreground"} or kind not in {"run", "resume", "approve", "revise", "requeue", "abandon"} or not action_id or not command
                or any(not part or "\0" in part for part in command) or not environment_carrier.is_absolute()):
            raise KernelError("host_request_invalid")
        preflight()  # External I/O precedes both Task binding and durable Action.
        intent: dict[str, Any] = {"kind": kind, "subject": subject, "message": message, "new_thread": new_thread,
                  "command": list(command), "environment_carrier": str(environment_carrier), "backend": backend,
                  "policy_overrides": dict(policy_overrides or {})}
        if replacement is not None:
            intent["replacement"] = asdict(replacement)
        if resource_inspections is not None:
            intent["resource_inspections"] = [inspection_wire(item) for item in resource_inspections]
        if discard_worktree:
            intent["discard_worktree"] = True
        if continuation_of is not None:
            intent["continuation_of"] = continuation_of
        if kind == "run" and policy_overrides:
            raise KernelError("policy_override_boundary")
        with self.storage.transaction(write=False) as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                if (kind != "abandon" or continuation_of != abandoned["id"] or abandoned["status"] != "pending"
                        or subject is not None or message is not None or new_thread or policy_overrides or replacement is not None
                        or type(discard_worktree) is not bool):
                    raise KernelError("run_abandoned")
            elif continuation_of is not None:
                raise KernelError("abandon_continuation_boundary")
            run_id, task = run["id"], self._task(run)
        with self._task_admission(task):
            # A crash here preserves the bound, fully created Run even without a PID.
            with self.storage.transaction(ordered=False) as tx:
                self._require_current_task(tx, task, run_id)
                old = tx.rows(*run_statement(tx, 'SELECT * FROM hosts WHERE action_id=? AND run_id=?',
                          (action_id,), run_last=True))
                if old:
                    value = json.loads(tx.get(old[0]["request_id"], old[0]["subject"]))
                    if value["intent"] != intent:
                        raise KernelError("action_identity_changed")
                    return self._host_launch(tx, old[0])
                run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                          (), run_last=True))
                if (run["executor"] is not None or tx.rows(*run_statement(tx, "SELECT owner FROM hosts WHERE (status!='exited' OR usage_released=0) AND run_id=? LIMIT 1",
                                                       (), run_last=True))
                        or tx.rows(*run_statement(tx, "SELECT owner FROM retired_executors WHERE status!='exited' AND run_id=? LIMIT 1",
                               (), run_last=True))
                        or tx.rows(*run_statement(tx, 'SELECT id FROM leases WHERE run_id=? LIMIT 1',
                               (), run_last=True))):
                    raise KernelError("executor_exit_unproven")
                if tx.rows(*run_statement(tx, 'SELECT id FROM actions WHERE id=? AND run_id=?',
                       (action_id,), run_last=True)):
                    raise KernelError("action_already_applied_without_host")
                material_owner = subject or tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1',
                                                (), run_last=True))[0]
                if continuation_of is not None:
                    abandoned = self._abandon_action(tx)
                    if abandoned is None or abandoned["id"] != continuation_of or abandoned["status"] != "pending":
                        raise KernelError("abandon_continuation_boundary")
                    if discard_worktree and resource_inspections is None:
                        raise KernelError("abandon_resource_observation_required")
                    if resource_inspections is not None:
                        self._check_resource_inspections(self._abandon_resources(tx, abandoned), resource_inspections,
                                                         discard_worktree or self._abandon_discard(tx, abandoned))
                    payload = tx.put(material_owner, "abandon_continuation_intent", wire_json(intent))
                    receipt = tx.put(material_owner, "control_receipt", wire_json({"action": action_id, "continuation_of": continuation_of,
                                                                                 "discard_worktree": discard_worktree}))
                    tx.execute("INSERT INTO actions(id,run_id,kind,subject,payload_id,status,receipt_id) VALUES (?,?,'abandon_continue',?,?,'applied',?)",
                               (action_id, run_id, material_owner, payload, receipt))
                    # Pure observations remain facts even when they blocked the
                    # prior cleanup. A new explicit continuation gets a fresh
                    # inspection; its permission never rewrites the old Action.
                    for effect in self._abandon_effects(tx, abandoned):
                        if effect["status"] == "prepared":
                            tx.execute(*run_statement(tx, "UPDATE effects SET status='cancelled' WHERE id=? AND run_id=?",
                                (effect["id"],), run_last=True))
                            self._event(tx, effect["subject"], "abandon_preparation_cancelled", effect["id"], effect["payload_id"], generation=effect["generation"])
                        elif effect["operation"] == "inspect_resources" and effect["status"] == "observed":
                            tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
                                (effect["id"],), run_last=True))
                            self._event(tx, effect["subject"], "effect_consumed", effect["id"], effect["receipt_id"], generation=effect["generation"])
                    for wait in tx.all(*run_statement(tx, 'SELECT w.*,m.body FROM waits w JOIN material m ON m.run_id=w.run_id AND m.id=w.scope_id WHERE w.run_id=?',
                                    (), run_last=True)):
                        scope = json.loads(wait["body"])
                        if (scope.get("abandon_action") != continuation_of
                                or scope != self._automatic_scope(tx, run, wait["subject"], scope["cleanup"])):
                            continue
                        if wait["subject"] is not None:
                            for gate in self._gates(tx, wait["subject"]):
                                if (gate["code"] == "supervision_timeout" and gate["evidence"] is not None
                                        and json.loads(tx.get(gate["evidence"], wait["subject"])).get("scope_id") == wait["scope_id"]):
                                    tx.execute(*run_statement(tx, "DELETE FROM gates WHERE subject=? AND code='supervision_timeout' AND evidence=? AND run_id=?",
                                        (wait["subject"], gate["evidence"]), run_last=True))
                        elif run["blocked"] == "supervision_timeout":
                            latest = tx.rows(*run_statement(tx, "SELECT m.body FROM history h JOIN material m ON m.run_id=h.run_id AND m.id=h.material_id WHERE h.kind='paused' AND h.identity='supervision_timeout' AND h.run_id=? ORDER BY h.position DESC LIMIT 1",
                                         (), run_last=True))
                            if latest and json.loads(latest[0][0]).get("scope_id") == wait["scope_id"]:
                                tx.execute(*run_statement(tx, 'UPDATE runs SET blocked=NULL WHERE id=?',
                                    (), run_last=True))
                        tx.execute(*run_statement(tx, 'DELETE FROM waits WHERE scope_id=? AND kind=? AND run_id=?',
                            (wait["scope_id"], wait["kind"]), run_last=True))
                    self._event(tx, material_owner, "abandon_continued", continuation_of, receipt)
                elif kind == "run":
                    if run["stopped"]:
                        raise KernelError("operator_stopped")
                    payload = tx.put(material_owner, "control_intent", wire_json(intent))
                    receipt = tx.put(material_owner, "control_receipt", wire_json({"action": action_id, "kind": kind, "applied": True}))
                    tx.execute("INSERT INTO actions(id,run_id,kind,subject,payload_id,status,receipt_id) VALUES (?,?,?,?,?,'applied',?)",
                               (action_id, run_id, kind, subject, payload, receipt))
                    self._event(tx, material_owner, "control_applied", action_id, receipt)
                else:
                    self._control(tx, kind, action_id, subject=subject, world=world, message=message,
                                  new_thread=new_thread, policy_overrides=policy_overrides, replacement=replacement,
                                  resource_inspections=resource_inspections, discard_worktree=discard_worktree)
                epoch = tx.one(*run_statement(tx, 'SELECT epoch FROM runs WHERE id=?',
                            (), run_last=True))[0] + 1
                owner, nonce = identity(), identity()
                request = HostLaunch(run_id, epoch, owner, nonce, "agent-run-kernel-" + nonce + ".service" if backend == "systemd" else None,
                                     command, environment_carrier, backend)
                material = tx.put(material_owner, "host_request", wire_json({**asdict(request), "environment_carrier": str(environment_carrier), "intent": intent}))
                tx.execute("INSERT INTO hosts(run_id,epoch,owner,nonce,action_id,subject,request_id,unit_name,backend,allocated_at,status) VALUES (?,?,?,?,?,?,?,?,?,?,'allocated')",
                           (run_id, epoch, owner, nonce, action_id, material_owner, material, request.unit_name, backend, self.clock()))
                tx.execute(*run_statement(tx, 'UPDATE runs SET executor=?,epoch=? WHERE id=?',
                    (owner, epoch), run_last=True))
                self._event(tx, material_owner, "host_allocated", nonce, material)
                return request

    def control_host(self, request: HostLaunch, kind: str, action_id: str, *, subject: str | None = None,
                     world: World | None = None, message: str | None = None, new_thread: bool = False,
                     policy_overrides: Mapping[str, Any] | None = None, replacement: Delivery | None = None,
                     resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False) -> str:
        """Apply an explicit action through the exact current Task and Host."""
        with self.storage.transaction(write=False) as tx:
            self._host_run(tx, request.run_id)
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            run_id, task = run["id"], self._task(run)
        with self._task_admission(task, wait=False):
            with self.storage.transaction(ordered=False) as tx:
                self._require_current_task(tx, task, run_id)
                self._host(tx, request)
                run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                          (), run_last=True))
                abandoned = self._abandon_action(tx)
                if abandoned is not None:
                    if kind != "abandon" or action_id != abandoned["id"]:
                        raise KernelError("run_abandoned")
                    self._abandon_owned(tx, abandoned, Execution(request.run_id, request.owner, request.epoch))
                else:
                    self._executor_owned(tx, run, Execution(request.run_id, request.owner, request.epoch))
                return self._control(tx, kind, action_id, subject=subject, world=world,
                                     message=message, new_thread=new_thread, policy_overrides=policy_overrides, replacement=replacement,
                                  resource_inspections=resource_inspections, discard_worktree=discard_worktree)

    def stop_task(self, action_id: str, *, subject: str | None = None) -> str:
        """Revoke business work under the exact Task lock without a Host launch.

        Unknown Host and Worker lifetimes remain owned and unretired. This
        Action establishes neither their exit nor permission to replace them.
        """
        with self.storage.transaction(write=False) as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            run_id, task = run["id"], self._task(run)
        with self._task_admission(task):
            with self.storage.transaction(write=False) as tx:
                self._require_current_task(tx, task, run_id)
                run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?', (), run_last=True))
                unrecorded = (run["final_action"] is None
                            and self._abandon_action(tx) is None
                            and not tx.rows(*run_statement(tx, 'SELECT id FROM actions WHERE id=? AND run_id=?',
                                        (action_id,), run_last=True)))
                if unrecorded and self._replaceable_in(tx, task, run_id):
                    return "complete"
                # Business may remain unfinished after all execution has ended.
                # This Task-locked snapshot proves no execution to revoke; it
                # does not consume returns, reconcile Effects or complete work.
                if (subject is None and unrecorded and run["executor"] is None
                        and not self._agent_seats(tx)
                        and not tx.rows(*run_statement(tx,
                            "SELECT owner FROM retired_executors WHERE run_id=? AND status!='exited' LIMIT 1", ()))
                        and not tx.rows(*run_statement(tx,
                            "SELECT owner FROM hosts WHERE run_id=? AND (status!='exited' OR usage_released!=1) LIMIT 1", ()))):
                    return "not_running"
            with self.storage.transaction(ordered=False) as tx:
                self._require_current_task(tx, task, run_id)
                return self._control(tx, "stop", action_id, subject=subject)

    def abandon_task(self, action_id: str, *, resource_inspections: tuple[ResourceInspection, ...],
                     discard_worktree: bool = False) -> str:
        """Record the permanent fence without reclaiming execution or launching cleanup."""
        with self.storage.transaction(write=False) as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            run_id, task = run["id"], self._task(run)
        with self._task_admission(task):
            with self.storage.transaction(ordered=False) as tx:
                self._require_current_task(tx, task, run_id)
                return self._control(tx, "abandon", action_id, resource_inspections=resource_inspections,
                                     discard_worktree=discard_worktree)

    @staticmethod
    def _host_launch(tx: Transaction, row: sqlite3.Row) -> HostLaunch:
        value = json.loads(tx.get(row["request_id"], row["subject"]))
        return HostLaunch(value["run_id"], value["epoch"], value["owner"], value["nonce"], value["unit_name"],
                          tuple(value["command"]), Path(value["environment_carrier"]), value["backend"])

    def host_request(self, action_id: str) -> HostLaunch:
        """Read the original request; reading never allocates or relaunches it."""
        with self.storage.transaction(write=False) as tx:
            return self._host_launch(tx, tx.one(*run_statement(tx, 'SELECT * FROM hosts WHERE action_id=? AND run_id=?',
                                             (action_id,), run_last=True)))

    def host_usage(self, action_id: str) -> HostObservation | None:
        """Read the last accepted lease receipt, preserved across unknown reads."""
        with self.storage.transaction(write=False) as tx:
            row = tx.one(*run_statement(tx, 'SELECT * FROM hosts WHERE action_id=? AND run_id=?',
                      (action_id,), run_last=True))
            receipts = tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE subject=? AND identity=? AND kind IN ('host_usage_pending','host_usage_ready','host_usage_released') AND run_id=? ORDER BY rowid DESC LIMIT 1",
                           (row["subject"], row["nonce"]), run_last=True))
            return None if not receipts else HostObservation(**json.loads(tx.get(receipts[0][0], row["subject"])))

    @staticmethod
    def _host_run(tx: Transaction, run_id: str) -> None:
        """Bind callback ownership without inventing exit or live authority."""
        selected = transaction_scope(tx)
        if selected.run_id != run_id:
            raise KernelError("host_binding_changed")

    def _host(self, tx: Transaction, request: HostLaunch) -> sqlite3.Row:
        self._host_run(tx, request.run_id)
        row = tx.one("SELECT * FROM hosts WHERE run_id=? AND epoch=? AND owner=?", (request.run_id, request.epoch, request.owner))
        if self._host_launch(tx, row) != request:
            raise KernelError("host_binding_changed")
        return row

    def dispatch_host(self, request: HostLaunch) -> bool:
        with self.storage.transaction() as tx:
            row = self._host(tx, request)
            if row["dispatched_at"] is not None or row["status"] != "allocated":
                return False
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            abandoned = self._abandon_action(tx)
            continuation = False
            if abandoned is not None:
                intent = json.loads(tx.get(row["request_id"], row["subject"]))["intent"]
                continuation = (row["action_id"] == abandoned["id"] or intent.get("continuation_of") == abandoned["id"])
            if (run["stopped"] and not continuation) or (run["executor"], run["epoch"]) != (request.owner, request.epoch):
                return False
            tx.execute("UPDATE hosts SET status='dispatched',dispatched_at=? WHERE run_id=? AND epoch=?", (self.clock(), request.run_id, request.epoch))
            self._event(tx, row["subject"], "host_dispatched", request.nonce)
            return True

    def host_backend(self, execution: Execution) -> str:
        with self.storage.transaction(write=False) as tx:
            self._host_run(tx, execution.run_id)
            rows = tx.rows("SELECT backend FROM hosts WHERE run_id=? AND owner=? AND epoch=?",
                           (execution.run_id, execution.owner, execution.epoch))
            return "systemd" if not rows else str(rows[0][0])

    def host_action(self, request: HostLaunch) -> str:
        with self.storage.transaction(write=False) as tx:
            return str(self._host(tx, request)["action_id"])

    def authorize_host(self, request: HostLaunch) -> None:
        """Final bootstrap check against the original admitted Host and lease."""
        with self.storage.transaction(write=False) as tx:
            row = self._host(tx, request)
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            execution = Execution(request.run_id, request.owner, request.epoch)
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                self._abandon_owned(tx, abandoned, execution)
            else:
                self._executor_owned(tx, run, execution)
                if run["stopped"]:
                    raise KernelError("operator_stopped")
            if row["status"] != "running" or not row["usage_ready"] or row["usage_released"]:
                raise KernelError("host_not_ready")

    def observe_host(self, request: HostLaunch, observation: HostObservation) -> str:
        """Settle exact manager evidence without rearming an unknown launch."""
        with self.storage.transaction() as tx:
            row = self._host(tx, request)
            if row["dispatched_at"] is None:
                raise KernelError("host_not_dispatched")
            if observation.backend != request.backend or row["backend"] != request.backend:
                raise KernelError("host_backend_changed")
            if observation.usage_pending:
                if (observation.disposition != "unknown" or observation.unit_name != request.unit_name
                        or not observation.evidence or observation.usage_ready or observation.usage_released):
                    raise KernelError("host_handoff_invalid")
                material = tx.put(row["subject"], "host_observation", wire_json(asdict(observation)))
                prior = tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE subject=? AND identity=? AND kind='host_usage_pending' AND run_id=? LIMIT 1",
                            (row["subject"], request.nonce), run_last=True))
                if prior:
                    if prior[0][0] != material:
                        raise KernelError("host_handoff_changed")
                    return str(row["status"])
                if row["invocation_id"] is not None or row["status"] == "exited":
                    raise KernelError("host_handoff_late")
                self._event(tx, row["subject"], "host_usage_pending", request.nonce, material)
            domain_id = None
            if request.backend == "foreground" and observation.domain is not None:
                from agent_run.contracts.host import valid_foreground_domain
                if not valid_foreground_domain(observation.domain, request.nonce):
                    raise KernelError("host_domain_invalid")
                domain_id = tx.put(row["subject"], "foreground_domain", wire_json(observation.domain))
                if row["domain_id"] is not None and domain_id != row["domain_id"]:
                    raise KernelError("host_domain_changed")
            if row["status"] == "exited":
                if request.backend == "foreground" and domain_id != row["domain_id"]:
                    return "unknown"
                if (observation.unit_name, observation.invocation_id, observation.cgroup, observation.boot_id) != (
                        request.unit_name, row["invocation_id"], row["cgroup"], row["boot_id"]):
                    return "unknown"
                if (observation.disposition == "exited" and observation.usage_released and not row["usage_released"]
                        and (observation.exit_status, observation.ended_at) == (row["exit_status"], row["ended_at"])):
                    material = tx.put(row["subject"], "host_usage_released", wire_json(asdict(observation)))
                    tx.execute("UPDATE hosts SET usage_released=1 WHERE run_id=? AND epoch=?", (request.run_id, request.epoch))
                    self._event(tx, row["subject"], "host_usage_released", request.nonce, material)
                return "exited"
            material = tx.put(row["subject"], "host_observation", wire_json(asdict(observation)))
            bound = (row["invocation_id"], row["cgroup"], row["boot_id"])
            observed = (observation.invocation_id, observation.cgroup, observation.boot_id)
            known = (observation.unit_name == request.unit_name and all(isinstance(value, str) and value for value in observed)
                     and (bound[0] is None or bound == observed))
            if request.backend == "foreground":
                domain = observation.domain
                executor = None if domain is None else domain["executor"]
                startup_exit = (executor is None and observation.disposition == "exited"
                                and row["main_pid"] is None and not row["usage_ready"] and not observation.usage_ready
                                and observation.main_pid is None and observation.start_token is None
                                and observation.exit_status is None)
                known = bool(domain_id is not None and domain is not None and observation.unit_name is None
                             and observation.invocation_id is None and observation.cgroup is None
                             and observation.boot_id == domain["observer"]["boot_id"]
                             and (startup_exit or executor is not None and (observation.main_pid, observation.start_token)
                                  == (executor["pid"], executor["start_token"])))
            running = (known and observation.disposition == "running" and type(observation.main_pid) is int
                       and observation.main_pid > 1 and bool(observation.start_token)
                       and (row["main_pid"] is None or (row["main_pid"], row["start_token"]) == (observation.main_pid, observation.start_token)))
            exited = (known and observation.disposition == "exited" and (type(observation.exit_status) is int or request.backend == "foreground" and observation.exit_status is None)
                      and (request.backend == "systemd" or observation.exit_evidence in {"retained_namespace_pidfd", "same_observer_init_exit"})
                      and isinstance(observation.ended_at, (float, int)) and not isinstance(observation.ended_at, bool)
                      and math.isfinite(observation.ended_at) and observation.ended_at > 0)
            disposition = "running" if running else "exited" if exited else "unknown"
            host_interrupted = False
            if exited and request.backend == "foreground" and observation.exit_status != 0:
                try:
                    stopped = tx.one(*run_statement(tx, 'SELECT stopped FROM runs WHERE id=?',
                                                  (), run_last=True))[0]
                    if not stopped and self._abandon_action(tx) is None:
                        # Capture before retirement removes leases, preparations
                        # and integration seats or adds failure gates.
                        # Seats already prove ready/waiting. Decode preparation
                        # contexts only during retirement, within the read quota.
                        host_interrupted = bool(self._agent_seats(tx))
                        if not host_interrupted:
                            state = self._operation_state_in(tx)
                            host_interrupted = bool(state.active or state.disposition not in {"complete", "quiescent"})
                except KernelError:
                    if tx.failed:
                        raise
                    # Unknown applicability grants no interruption notice. It
                    # does not invalidate the independently proved physical exit.
            if known and (running or exited):
                if domain_id is not None:
                    tx.execute("UPDATE hosts SET domain_id=coalesce(domain_id,?) WHERE run_id=? AND epoch=?",
                               (domain_id, request.run_id, request.epoch))
                tx.execute("UPDATE hosts SET invocation_id=?,cgroup=?,boot_id=?,main_pid=coalesce(main_pid,?),start_token=coalesce(start_token,?) WHERE run_id=? AND epoch=?",
                           (*observed, observation.main_pid, observation.start_token, request.run_id, request.epoch))
                tx.execute("UPDATE hosts SET usage_ready=max(usage_ready,?),usage_released=max(usage_released,?) WHERE run_id=? AND epoch=?",
                           (bool(running and observation.usage_ready), bool(exited and observation.usage_released), request.run_id, request.epoch))
                if running and observation.usage_ready and not row["usage_ready"]:
                    self._event(tx, row["subject"], "host_usage_ready", request.nonce, material)
                if exited and observation.usage_released and not row["usage_released"]:
                    self._event(tx, row["subject"], "host_usage_released", request.nonce, material)
            tx.execute("UPDATE hosts SET status=?,observation_id=?,exit_status=?,ended_at=? WHERE run_id=? AND epoch=?",
                       (disposition, material, observation.exit_status if exited else None, observation.ended_at if exited else None, request.run_id, request.epoch))
            if exited:
                business = tx.one(*run_statement(tx, 'SELECT stopped,final_action FROM runs WHERE id=?',
                               (), run_last=True))
                interrupted: set[str] = set()
                self._host_preparations_exited(tx, Execution(request.run_id, request.owner, request.epoch), {"host_observation": material})
                for output in tx.all(*run_statement(tx, 'SELECT o.*,l.subject FROM leases l JOIN invocation_outputs o ON o.run_id=l.run_id AND o.lease=l.id WHERE l.executor=? AND l.epoch=? AND l.run_id=?',
                                  (request.owner, request.epoch), run_last=True)):
                    process = None if output["process_identity"] is None else json.loads(tx.get(output["process_identity"], output["subject"]))
                    unstarted = output["dispatch_state"] == "allocated"
                    if not unstarted and (process is None or not self._host_contains_process(tx, row, process)):
                        continue
                    # Host-domain exit never supplies the individual Worker end time.
                    proof = tx.put(output["subject"], "worker_host_exit", wire_json({"host_nonce": request.nonce, "observation": asdict(observation)}))
                    tx.execute(*run_statement(tx, "UPDATE invocation_outputs SET dispatch_state=?,failure_kind='fatal',start_evidence=? WHERE invocation=? AND ordinal=? AND exit_code IS NULL AND run_id=?",
                        ("unavailable" if unstarted else "exited", proof, output["invocation"], output["ordinal"]), run_last=True))
                    if output["raw_id"] is None:
                        tx.execute(*run_statement(tx, "UPDATE invocations SET status='interrupted' WHERE id=? AND run_id=?",
                            (output["invocation"],), run_last=True))
                    tx.execute(*run_statement(tx, 'DELETE FROM leases WHERE id=? AND run_id=?',
                        (output["lease"],), run_last=True))
                    self._event(tx, output["subject"], "worker_host_exited", output["invocation"], proof)
                    if tx.rows(*run_statement(tx, 'SELECT s.id FROM subjects s JOIN attempts a ON a.run_id=s.run_id AND a.id=s.current_attempt WHERE s.id=? AND a.current_invocation=? AND a.generation=s.generation AND s.run_id=?',
                           (output["subject"], output["invocation"]), run_last=True)):
                        interrupted.add(output["subject"])
                if not business["stopped"] and business["final_action"] is None:
                    interrupted.update(item["id"] for item in tx.rows(*run_statement(tx, "SELECT s.id FROM subjects s JOIN attempts a ON a.run_id=s.run_id AND a.id=s.current_attempt JOIN invocations i ON i.run_id=a.run_id AND i.id=a.current_invocation WHERE a.generation=s.generation AND a.status IN ('running','returned','continuing') AND i.executor=? AND i.epoch=? AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.run_id=s.run_id AND g.subject=s.id) AND s.run_id=?",
                                                                  (request.owner, request.epoch), run_last=True)))
                    interrupted.update(item["subject"] for item in tx.rows(*run_statement(tx, "SELECT DISTINCT e.subject FROM effects e JOIN subjects s ON s.run_id=e.run_id AND s.id=e.subject WHERE e.executor=? AND e.epoch=? AND e.generation=s.generation AND e.status IN ('prepared','sent','observed') AND e.operation!='issue_link' AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.run_id=s.run_id AND g.subject=s.id) AND e.run_id=?",
                                                                       (request.owner, request.epoch), run_last=True)))
                for subject in sorted(interrupted):
                    if (business["final_action"] is None
                            and not tx.rows(*run_statement(tx, "SELECT code FROM gates WHERE subject=? AND code='execution_failed' AND run_id=?",
                                        (subject,), run_last=True))):
                        proof = tx.put(subject, "worker_host_exit", wire_json({"host_nonce": request.nonce, "observation": asdict(observation)}))
                        self._gate(tx, subject, "execution_failed", evidence=proof)
                tx.execute("INSERT OR IGNORE INTO retired_executors VALUES (?,?,?,'exited')", (request.run_id, request.owner, request.epoch))
                tx.execute("UPDATE retired_executors SET status='exited' WHERE run_id=? AND owner=? AND epoch=?", (request.run_id, request.owner, request.epoch))
                tx.execute("UPDATE runs SET executor=NULL,epoch=epoch+1 WHERE id=? AND executor=? AND epoch=?",
                           (request.run_id, request.owner, request.epoch))
                tx.execute("UPDATE runs SET executor=NULL WHERE id=? AND executor=? AND epoch>?", (request.run_id, request.owner, request.epoch))
            if host_interrupted:
                self._event(tx, row["subject"], "host_interrupted", request.nonce, material)
            self._event(tx, row["subject"], "host_observed", request.nonce, material)
            return disposition

    @staticmethod
    def _host_contains_process(tx: Transaction, host: sqlite3.Row, process: Mapping[str, Any]) -> bool:
        Kernel._host_run(tx, host["run_id"])
        if host["backend"] == "foreground":
            if host["domain_id"] is None:
                return False
            domain = json.loads(tx.get(host["domain_id"], host["subject"]))
            executor = domain["executor"]
            if executor is None:
                return False
            return bool(process.get("schema") == "linux-namespace-process-v1"
                        and (process.get("host_pid"), process.get("host_start_token"), process.get("boot_id"),
                             process.get("host_namespace"), process.get("pid_namespace"))
                        == (executor["namespace_pids"][-1], executor["start_token"], executor["boot_id"],
                            executor["pid_namespace"], executor["pid_namespace"]))
        group = process.get("cgroup")
        return bool(host["main_pid"] is not None
                    and (process.get("host_pid"), process.get("host_start_token"), process.get("boot_id"), process.get("host_cgroup"))
                    == (host["main_pid"], host["start_token"], host["boot_id"], host["cgroup"])
                    and isinstance(group, str) and ".." not in group.split("/")
                    and (group == host["cgroup"] or group.startswith(host["cgroup"].rstrip("/") + "/")))

    @contextmanager
    def executor_scope(self, execution: Execution) -> Iterator[Callable[[BaseException], None]]:
        """Fence a failing Host before drain, retaining exact settlement rights.

        The existing Task lock excludes lifecycle admission if storage cannot
        record revocation. No SQL transaction spans physical cleanup. Retry the
        revocation once after drain; never claim a failed commit closed it.
        Unrecorded revocation retains that lock until this Host process exits.
        """
        with self.storage.transaction(write=False) as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            self._executor_owned(tx, run, execution)
            task = self._task(run)
        failure: BaseException | None = None
        retain_lock: Callable[[], None] | None = None
        revoked = False
        with ExitStack() as held:
            def close_admission(error: BaseException) -> None:
                nonlocal failure, retain_lock, revoked
                failure = error
                try:
                    if retain_lock is None:
                        retain_lock = held.enter_context(task_lock(task))
                    with self.storage.transaction(write=False) as tx:
                        current = run_is_current(tx)
                    if not current:
                        revoked = True  # The original Task binding no longer admits this Run.
                        held.close()
                        retain_lock = None
                        return
                    self._retire_failed_executor(execution, error)
                except Exception as unavailable:
                    error.add_note("Executor admission not durably revoked: " + bounded_error(str(unavailable)))
                else:
                    revoked = True
                    held.close()
                    retain_lock = None
            try:
                yield close_admission
            finally:
                if failure is not None and not revoked:
                    close_admission(failure)
                    if not revoked and retain_lock is not None:
                        # Transfer the already-held descriptor without allocating
                        # another FD. Only actual process exit releases this lock.
                        retain_lock()

    def _retire_failed_executor(self, execution: Execution, error: BaseException) -> None:
        with self.storage.transaction(ordered=False) as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            if run["id"] != execution.run_id:
                raise KernelError("executor_fenced")
            if (run["executor"], run["epoch"]) != (execution.owner, execution.epoch):
                return  # A prior Stop, exit or committed reply loss already fenced it.
            tx.execute("INSERT OR IGNORE INTO retired_executors VALUES (?,?,?,'pending')",
                       (execution.run_id, execution.owner, execution.epoch))
            tx.execute("UPDATE runs SET epoch=epoch+1 WHERE id=?", (execution.run_id,))
            owner = tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY rowid LIMIT 1',
                        (), run_last=True))[0]
            material = tx.put(owner, "executor_failure", wire_json({
                "owner": execution.owner, "epoch": execution.epoch,
                "type": type(error).__name__, "message": bounded_error(str(error)),
            }))
            self._event(tx, owner, "executor_failed", execution.owner, material)
            for waiting in tx.rows(*run_statement(tx, "SELECT * FROM waits WHERE kind='agent_preparation' AND run_id=?",
                               (), run_last=True)):
                preparation, _ = self._preparation(tx, waiting)
                if preparation.execution == execution and not self._gates(tx, preparation.subject):
                    evidence = tx.put(preparation.subject, "executor_failure", tx.get(material, owner))
                    self._gate(tx, preparation.subject, "worker_unavailable", evidence=evidence)
            # Raw/exit can settle before a failed revocation is retried. The
            # original unconsumed Attempt remains authoritative after its lease.
            for job in tx.rows(*run_statement(tx, "SELECT s.id FROM subjects s JOIN attempts a ON a.run_id=s.run_id AND a.id=s.current_attempt JOIN invocations i ON i.run_id=a.run_id AND i.id=a.current_invocation WHERE a.generation=s.generation AND a.status IN ('running','returned','continuing') AND i.executor=? AND i.epoch=? AND s.run_id=?",
                           (execution.owner, execution.epoch), run_last=True)):
                if not self._gates(tx, job["id"]):
                    evidence = tx.put(job["id"], "executor_failure", tx.get(material, owner))
                    self._gate(tx, job["id"], "execution_failed", evidence=evidence)

    def claim_executor(self, owner: str) -> Execution:
        with self.storage.transaction() as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            if self._abandon_action(tx) is not None:
                raise KernelError("run_abandoned")
            if tx.rows(*run_statement(tx, 'SELECT owner FROM hosts WHERE run_id=? LIMIT 1',
                   (), run_last=True)):
                raise KernelError("managed_host_required")
            if run["stopped"]:
                raise KernelError("operator_stopped")
            if tx.rows("SELECT owner FROM retired_executors WHERE run_id=? AND status='pending' LIMIT 1", (run["id"],)):
                raise KernelError("executor_exit_unproven")
            if run["executor"] is not None and run["executor"] != owner:
                raise KernelError("executor_exit_unproven")
            epoch = run["epoch"] if run["executor"] == owner else run["epoch"] + 1
            tx.execute(*run_statement(tx, 'UPDATE runs SET executor=?,epoch=? WHERE id=?',
                (owner, epoch), run_last=True))
            return Execution(run["id"], owner, epoch)

    def confirm_executor_exit(self, execution: Execution, exited_invocations: tuple[str, ...] = ()) -> None:
        """Consume exact Host exit evidence; no PID guessing or replacement claim.

        The caller must have observed the old Executor and all listed Workers
        exit. The isolated driver supplies synthetic Host evidence; a real Host
        adapter is deliberately outside this exploration's implemented surface.
        """
        with self.storage.transaction() as tx:
            self._host_run(tx, execution.run_id)
            if tx.rows("SELECT owner FROM hosts WHERE run_id=? AND owner=? AND epoch=?", (execution.run_id, execution.owner, execution.epoch)):
                raise KernelError("managed_host_exit_required")
            retired = tx.one("SELECT * FROM retired_executors WHERE run_id=? AND owner=? AND epoch=?",
                             (execution.run_id, execution.owner, execution.epoch))
            if retired["status"] == "exited":
                return
            leases = tx.all(*run_statement(tx, 'SELECT * FROM leases WHERE executor=? AND epoch=? AND run_id=?',
                         (execution.owner, execution.epoch), run_last=True))
            if {row["invocation"] for row in leases} != set(exited_invocations):
                raise KernelError("worker_exit_unproven")
            for lease in leases:
                tx.execute(*run_statement(tx, "UPDATE invocations SET status='interrupted' WHERE id=? AND status='running' AND run_id=?",
                    (lease["invocation"],), run_last=True))
                tx.execute(*run_statement(tx, 'DELETE FROM leases WHERE id=? AND invocation=? AND run_id=?',
                    (lease["id"], lease["invocation"]), run_last=True))
            self._host_preparations_exited(tx, execution, {"executor_exit_confirmed": True})
            tx.execute("UPDATE retired_executors SET status='exited' WHERE run_id=? AND owner=? AND epoch=?",
                       (execution.run_id, execution.owner, execution.epoch))
            tx.execute("UPDATE runs SET executor=NULL WHERE id=? AND executor=? AND epoch>?",
                       (execution.run_id, execution.owner, execution.epoch))

    def _advance(self, tx: Transaction, work: Work, world: World | None) -> Step | IORequest:
        subject, execution, allow_agent = work.subject, work.execution, work.allow_agent
        assert subject is not None
        abandoned = self._abandon_action(tx)
        if abandoned is not None:
            return self._advance_abandon(tx, tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                                 (), run_last=True)), abandoned, subject, work, world)
        run, job = self._owned(tx, subject, execution)
        read = _OperationSnapshot(self, tx, run).subject(subject, job)
        waiting = self._subject_wait(tx, run, job, read=read)
        if waiting is not None:
            return waiting
        requeue = read.requeue
        if requeue is not None:
            return self._advance_requeue(tx, run, job, requeue, work, world)
        pending = read.pending
        # An unknown write is read back before scope drift or any new work.
        if pending is not None:
            if pending["status"] == "sent":
                return self._effect_work(tx, work, pending)
            if pending["status"] == "observed" and self._effect_stale(tx, run, job, pending, read=read):
                return self._settle_stale_effect(tx, job, pending)
            if pending["status"] == "prepared" and self._effect_stale(tx, run, job, pending, read=read):
                tx.execute(*run_statement(tx, "UPDATE effects SET status='retired' WHERE id=? AND run_id=?",
                    (pending["id"],), run_last=True))
                return Step("progress", subject, "stale_intent_retired")
            if world is None:
                return self._observe_work(tx, work, subject)
            if not self._current(tx, job, run, world):
                return Step("paused", subject, "currentness")
            if world.target is None and not (pending["operation"] == "run_branch" and job["kind"] == DeliveryKind.TICKET):
                self._gate(tx, subject, "target_branch_missing")
                return Step("paused", subject, "target_branch_missing")
            if pending["status"] == "observed":
                return self._apply_effect(tx, run, job, pending, world)
            return self._effect_work(tx, work, pending)
        if job["refresh_revision"] is not None and self._automatic_control_read(tx, run, subject, False, read=read):
            if world is None:
                return self._observe_work(tx, work, subject)
            return self._refresh_final(tx, run, job, world)
        optional = tx.rows(*run_statement(tx, "SELECT e.* FROM effects e JOIN issue_links l ON l.run_id=e.run_id AND l.effect_id=e.id WHERE l.subject=? AND l.state='pending' AND e.status='prepared' AND e.run_id=?",
                       (subject,), run_last=True))
        if optional:
            if world is None:
                return self._observe_work(tx, work, subject)
            if not self._current(tx, job, run, world):
                return Step("paused", subject, "currentness")
            return self._effect_work(tx, work, optional[0])
        if job["validation_cleanup"]:
            if world is None:
                return self._observe_work(tx, work, subject)
            if not self._current(tx, job, run, world):
                return Step("paused", subject, "currentness")
            originals = [json.loads(tx.get(row[0], subject)) for row in tx.all(*run_statement(tx, "SELECT payload_id FROM effects WHERE subject=? AND operation='validation_checkout' AND status='confirmed' AND run_id=?",
                                                                            (subject,), run_last=True))]
            originals = [value for value in originals if value["checkout"] == job["validation_cleanup"]]
            if len(originals) != 1:
                raise KernelError("validation_origin_unproven")
            original = originals[0]
            return self._prepare_effect(tx, run, job, work, world, "validation_cleanup", before=original["reviewed_commit"],
                                        payload={key: original[key] for key in ("checkout", "owned_root", "common_git_dir", "reviewed_commit")})
        attempt = None
        facts = None
        if job["current_attempt"]:
            attempt = read.attempt
            assert attempt is not None
            if attempt["thread_id"] is not None or attempt["opening_id"] is not None:
                facts = read.continuation
                assert facts is not None
                prior = facts.prior
                if prior is not None and prior.status == "running" and prior.dispatch_state == "allocated" and self.clock() >= prior.deadline_at:
                    evidence = tx.put(subject, "allocation_expired", wire_json({"invocation": prior.invocation_id,
                        "ordinal": prior.ordinal, "deadline_at": prior.deadline_at}))
                    tx.execute(*run_statement(tx, "UPDATE invocation_outputs SET dispatch_state='unavailable',start_evidence=? WHERE invocation=? AND ordinal=? AND run_id=?",
                        (evidence, prior.invocation_id, prior.ordinal), run_last=True))
                    tx.execute(*run_statement(tx, "UPDATE invocations SET status='interrupted' WHERE id=? AND run_id=?",
                        (prior.invocation_id,), run_last=True))
                    tx.execute(*run_statement(tx, 'DELETE FROM leases WHERE id=? AND run_id=?',
                        (prior.lease_id,), run_last=True))
                    self._gate(tx, subject, "execution_failed", evidence=evidence)
                    return Step("paused", subject, "invocation_deadline_exceeded")
        if world is None:
            return self._observe_work(tx, work, subject)
        initialization = self._initial_run_branch(tx, run, job, work, world)
        if initialization is not None:
            return initialization
        if attempt is not None:
            if attempt["result_id"]:
                return self._consume(tx, run, job, attempt, world, work)
            if not self._current(tx, job, run, world):
                return Step("paused", subject, "currentness")
            try:
                facts = facts or load_continuation(tx, attempt["id"])
            except KernelError as error:
                if error.code != "continuation_basis_missing":
                    raise
                return Step("paused", subject, error.code)
            prior = facts.prior
            if prior is not None and prior.failure_kind is not None and attempt["resume_action"] is None:
                return self._recover_agent(tx, run, job, attempt, world, work, facts)
            return self._allocate(tx, run, job, work, world, attempt, continuation=facts)
        if not self._current(tx, job, run, world):
            return Step("paused", subject, "currentness")
        if job["phase"] in {"development", "review", "publish"}:
            return self._allocate(tx, run, job, work, world)
        return self._mechanical(tx, run, job, work, world)

    def _automatic_owned(self, tx: Transaction, run: sqlite3.Row, execution: Execution) -> None:
        abandoned = self._abandon_action(tx)
        if abandoned is None:
            self._executor_owned(tx, run, execution)
        else:
            self._abandon_owned(tx, abandoned, execution)

    def _automatic_scope(self, tx: Transaction, run: sqlite3.Row, subject: str | None, cleanup: bool, *,
                         read: _SubjectSnapshot | None = None) -> dict[str, Any]:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id:
            raise KernelError("effect_binding")
        scope: dict[str, Any] = {"run": run["id"], "parent_revision": run["parent_revision"],
                                 "graph_revision": run["graph_revision"], "subject": subject, "cleanup": cleanup}
        abandoned = self._abandon_action(tx)
        if abandoned is not None:
            scope["abandon_action"] = abandoned["id"]
        if subject is None:
            scope["final_target"] = [dict(row) for row in tx.rows(*run_statement(tx, 'SELECT * FROM final_target WHERE run_id=?',
                                                              (), run_last=True))]
            if abandoned is None:
                scope["final_ancestry"] = self._fingerprint(self._final_ancestry_anchors(tx))
        else:
            if read is not None:
                read.require(tx, read.job)
                if read.job['id'] != subject:
                    raise KernelError('operation_snapshot_binding')
            job = self._job(tx, subject) if read is None else read.job
            scope["delivery"] = {key: job[key] for key in (
                "generation", "revision", "phase", "purpose", "source_ref", "target_ref", "base_sha",
                "source_sha", "candidate_sha", "integrated_sha", "publication_sha", "pr_number",
                "current_attempt", "completion_revision")}
            requeue = self._pending_requeue(tx, subject) if read is None else read.requeue
            if requeue is not None:
                scope["requeue_action"] = requeue["id"]
        return scope

    def _automatic_control_read(self, tx: Transaction, run: sqlite3.Row, subject: str | None, cleanup: bool, *,
                                read: _SubjectSnapshot | None = None) -> bool:
        # Observation grants no write. The original approved Final Action owns
        # its proved-unapplied merge continuation; a new requeue cannot take it.
        if subject is None or cleanup or run["stopped"]:
            return False
        if read is not None:
            read.require(tx, read.job)
            if read.job['id'] != subject:
                raise KernelError('operation_snapshot_binding')
        if run["blocked"] == "final_refresh" and run["final_action"]:
            job = self._job(tx, subject) if read is None else read.job
            return bool(job["kind"] == DeliveryKind.FINAL_RUN and job["phase"] == "refresh_final"
                        and job["refresh_revision"] == run["observed_parent_revision"] and not (self._gates(tx, subject) if read is None else read.gates)
                        and tx.rows(*run_statement(tx, "SELECT id FROM actions WHERE id=? AND subject=? AND status='pending' AND kind IN ('approve','resume') AND run_id=?",
                                (run["final_action"], subject), run_last=True)))
        # Retirement still checks accepted versions, PR identity and publisher.
        return bool(not run["final_action"] and run["blocked"] == "requeue_required"
                    and (self._pending_requeue(tx, subject) if read is None else read.requeue) is not None
                    and all(gate["code"] == "requeue_required" for gate in (self._gates(tx, subject) if read is None else read.gates)))

    def _automatic_progress(self, tx: Transaction, run: sqlite3.Row, subject: str | None, cleanup: bool, *,
                            read: _SubjectSnapshot | None = None) -> str:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id:
            raise KernelError("effect_binding")
        if read is not None:
            read.require(tx, read.job)
            if read.job['id'] != subject:
                raise KernelError('operation_snapshot_binding')
        # No sequence counters, diagnostics, history, clocks, polling attempts
        # or sibling records can make a stalled operation appear to progress.
        abandoned = self._abandon_action(tx)
        if abandoned is not None:
            unsettled = self._abandon_scope(tx, abandoned)["unsettled"]
            facts: Any = {"action": abandoned["id"], "status": abandoned["status"],
                          "effects": [dict(row) for row in tx.all(*run_statement(tx, 'SELECT id,status,receipt_id FROM effects WHERE control_action=? AND run_id=? ORDER BY id',
                                                               (abandoned["id"],), run_last=True))],
                          "originals": [dict(tx.one(*run_statement(tx, 'SELECT id,status,receipt_id FROM effects WHERE id=? AND run_id=?',
                                                 (effect_id,), run_last=True)))
                                        for effect_id in unsettled]}
        elif subject is None:
            facts = [dict(row) for row in tx.rows(*run_statement(tx, "SELECT id,generation,phase FROM subjects WHERE kind='final_run' AND run_id=?",
                                              (), run_last=True))]
        else:
            job = self._job(tx, subject) if read is None else read.job
            attempt_keys = ('id', 'status', 'result_id', 'accounted', 'outcome', 'current_invocation', 'resume_action', 'new_thread')
            facts = {"delivery": {key: job[key] for key in job.keys() if key not in {"version", "last_failure", "checks_id"}},
                     "attempt": [dict(row) for row in tx.rows(*run_statement(tx, 'SELECT id,status,result_id,accounted,outcome,current_invocation,resume_action,new_thread FROM attempts WHERE id=? AND run_id=?',
                                                          (job["current_attempt"],), run_last=True))] if read is None else
                                ([] if read.attempt is None else [{key: read.attempt[key] for key in attempt_keys}]),
                     "effects": [dict(row) for row in tx.all(*run_statement(tx, "SELECT id,operation,status,receipt_id FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') AND run_id=? ORDER BY id",
                                                          (subject,), run_last=True))],
                     "gates": [dict(row) for row in tx.rows(*run_statement(tx, 'SELECT code,purpose FROM gates WHERE subject=? AND run_id=? ORDER BY code',
                                                        (subject,), run_last=True))] if read is None else
                              [{key: row[key] for key in ('code', 'purpose')} for row in read.gates],
                     "work_queue": bool(tx.rows(*run_statement(tx, 'SELECT subject FROM work_queue WHERE subject=? AND run_id=?',
                                            (subject,), run_last=True))),
                     "integration_queue": bool(tx.rows(*run_statement(tx, 'SELECT subject FROM integration_queue WHERE subject=? AND run_id=?',
                                                   (subject,), run_last=True))),
                     "integration_seat": run["integration_holder"] == subject,
                     "cleanup": [dict(row) for row in tx.rows(*run_statement(tx, 'SELECT generation,stage,state,effect_id,outcome FROM resource_cleanup WHERE subject=? AND generation=? AND run_id=?',
                                                          (subject, job["generation"]), run_last=True))],
                     "link": [dict(row) for row in tx.rows(*run_statement(tx, 'SELECT generation,state,effect_id FROM issue_links WHERE subject=? AND run_id=?',
                                                       (subject,), run_last=True))]}
            requeue = self._pending_requeue(tx, subject) if read is None else read.requeue
            # This exact current-Action witness is consumed by _advance_requeue
            # before generation replacement; unrelated audit events never count.
            facts["requeue_readback"] = None if requeue is None else [dict(row) for row in tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE kind='retirement_rechecked' AND identity=? AND run_id=? ORDER BY position DESC LIMIT 1",
                                                                                               (requeue["id"],), run_last=True))]
        return self._fingerprint(facts)

    @staticmethod
    def _automatic_token(token: str | ReadFence) -> dict[str, Any]:
        if isinstance(token, ReadFence):
            return {"scope": json.loads(token.scope), "source_sequence": token.source_sequence,
                    "control_version": list(token.control_version), "progress": None}
        try:
            if len(token.encode()) > CURRENT_LIMIT:
                raise ValueError("oversize token")
            value = json.loads(token)
            if not isinstance(value, dict) or set(value) != {"scope", "progress", "source_sequence", "control_version"} or not isinstance(value["scope"], dict):
                raise ValueError("invalid token")
            return value
        except (TypeError, ValueError, UnicodeError) as error:
            raise KernelError("automatic_step_identity") from error

    def _guard_automatic_step(self, token: str | ReadFence, execution: Execution, *, after_read: bool = False,
                             readback: Effect | None = None) -> Step | None:
        """Recheck exact work and Stop after I/O, without clearing a read wait."""
        value = self._automatic_token(token)
        scope = value["scope"]
        for writable in (False, True):
            with self.storage.transaction(write=writable) as tx:
                run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                          (), run_last=True))
                self._automatic_owned(tx, run, execution)
                if scope != self._automatic_scope(tx, run, scope["subject"], scope["cleanup"]):
                    raise ObservationSuperseded("automatic_step_superseded")
                if run["blocked"] == "supervision_timeout" or (scope["subject"] is not None and any(
                        gate["code"] == "supervision_timeout" for gate in self._gates(tx, scope["subject"]))):
                    return Step("paused", scope["subject"] or "", "supervision_timeout")
                if readback is not None:
                    row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                              (readback.effect_id, scope["subject"]), run_last=True))
                    if row["status"] != "sent" or self._effect(tx, row, execution) != readback:
                        raise KernelError("effect_binding")
                if (readback is None and self._abandon_action(tx) is None
                        and not self._automatic_control_read(tx, run, scope["subject"], scope["cleanup"]) and
                        (run["stopped"] or run["blocked"] or (scope["subject"] is not None and self._gates(tx, scope["subject"])))):
                    raise KernelError("automatic_step_paused")
                try:
                    return self._operation_read_wait(tx, run, scope, after_read=after_read, write_timeout=writable)
                except _SupervisionWriteRequired:
                    continue  # Close the reader before repeating every check under the ordered writer.
        raise AssertionError("Read supervision did not settle under its writer")

    def _finish_operation_read(self, token: str | ReadFence, execution: Execution) -> Step | None:
        """Check healthy reads without saving; expiration rechecks under its writer."""
        with self.storage.transaction(write=False) as tx:
            self._check_operation(tx, token, execution)
            scope = self._automatic_token(token)["scope"]
            try:
                return self._operation_read_wait(tx, tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                                         (), run_last=True)), scope, after_read=True,
                                                 write_timeout=False)
            except _SupervisionWriteRequired:
                pass  # Exit the reader normally so cleanup failures propagate.
        # No read-snapshot row or deadline authorizes this save. A concurrent
        # Stop, scope change or retired wait must be checked by the original
        # ordered transaction before it can record any timeout.
        with self.storage.transaction() as tx:
            self._check_operation(tx, token, execution)
            scope = self._automatic_token(token)["scope"]
            return self._operation_read_wait(tx, tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                                     (), run_last=True)), scope, after_read=True)

    def _operation_read_wait(self, tx: Transaction, run: sqlite3.Row, scope: dict[str, Any], *,
                             after_read: bool, write_timeout: bool = True) -> Step | None:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or scope.get('run') != selected.run_id:
            raise KernelError("effect_binding")
        owner = scope["subject"] or tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1',
                                        (), run_last=True))[0]
        rows = tx.rows(*run_statement(tx, "SELECT w.* FROM waits w JOIN material m ON m.run_id=w.run_id AND m.id=w.scope_id WHERE m.subject=? AND m.purpose='supervision_scope' AND m.body=? AND w.run_id=? ORDER BY w.deadline LIMIT 1",
                   (owner, wire_json(scope)), run_last=True))
        if rows:
            row = rows[0]
            if self.clock() >= row["deadline"]:
                if not write_timeout:
                    raise _SupervisionWriteRequired
                seconds = row["deadline"] - row["started_at"]
                return self._supervised_wait(tx, run, scope["subject"], scope, row["kind"], seconds)
            if not after_read and row["next_retry_at"] is not None and self.clock() < row["next_retry_at"]:
                return Step("waiting", scope["subject"] or "", row["kind"], retry_at=row["next_retry_at"])
        return None

    def _finish_automatic_step(self, token: str, execution: Execution, step: Step) -> Step:
        """Validate healthy Progress read-only; recheck required changes under the writer."""
        value = self._automatic_token(token)
        scope, subject = value["scope"], value["scope"]["subject"]
        for writable in (False, True):
            with self.storage.transaction(write=writable) as tx:
                selected = transaction_scope(tx)
                if scope.get('run') != selected.run_id:
                    raise ObservationSuperseded("automatic_step_superseded")
                run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                          (), run_last=True))
                self._automatic_owned(tx, run, execution)
                if subject is not None and step.subject != subject and self._abandon_action(tx) is None:
                    raise KernelError("automatic_step_identity")
                if step.kind == "progress" and value["progress"] == self._automatic_progress(tx, run, subject, scope["cleanup"]):
                    if not writable:
                        continue  # Close normally, then repeat every check under the ordered writer.
                    owner = subject or tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1',
                                           (), run_last=True))[0]
                    evidence = tx.put(owner, "execution_failure", wire_json({"code": "controller_no_progress",
                        "message": "Controller returned Progress without changing its durable business identity", "scope": scope}))
                    if subject is None:
                        tx.execute(*run_statement(tx, "UPDATE runs SET blocked='execution_failed' WHERE id=?",
                            (), run_last=True))
                        self._event(tx, owner, "paused", "controller_no_progress", evidence)
                    else:
                        self._gate(tx, subject, "execution_failed", evidence=evidence)
                    self._finish_final_failure(tx, run)
                    return Step("paused", subject or "", "controller_no_progress")
                checks_unchanged = (scope.get("delivery", {}).get("phase") in {"checks", "merge"}
                                    and scope == self._automatic_scope(tx, run, subject, scope["cleanup"]))
                if (step.kind not in {"waiting", "observe", "reconcile", "agent", "effect"}
                        and step.reason != "supervision_timeout" and not checks_unchanged):
                    if not writable:
                        if tx.rows(*run_statement(tx, "SELECT scope_id FROM waits WHERE subject IS ? AND kind='github_convergence' AND run_id=? LIMIT 1",
                                  (subject,), run_last=True)):
                            continue
                    else:
                        tx.execute(*run_statement(tx, "DELETE FROM waits WHERE subject IS ? AND kind='github_convergence' AND run_id=?",
                            (subject,), run_last=True))
                return step
        raise AssertionError("Automatic step did not settle under its writer")

    def _wait_for_read(self, token: str | ReadFence, execution: Execution, code: str, message: str, *, readback: Effect | None = None) -> Step:
        """Persist one original read outage; successful reads alone never renew it."""
        from agent_run.errors import bounded_error

        value = self._automatic_token(token)
        scope, subject = value["scope"], value["scope"]["subject"]
        with self.storage.transaction() as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            self._automatic_owned(tx, run, execution)
            if scope != self._automatic_scope(tx, run, subject, scope["cleanup"]):
                return Step("waiting", subject or "", "observation_superseded")
            gates = [] if subject is None else self._gates(tx, subject)
            abandoned = self._abandon_action(tx)
            requeue = self._automatic_control_read(tx, run, subject, scope["cleanup"])
            if readback is not None:
                original = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                               (readback.effect_id, subject), run_last=True))
                if original["status"] != "sent" or self._effect(tx, original, execution) != readback:
                    raise KernelError("effect_binding")
            timed_out = run["blocked"] == "supervision_timeout" or any(gate["code"] == "supervision_timeout" for gate in gates)
            if (timed_out
                    or ((abandoned is None or abandoned["status"] != "pending") and not requeue and readback is None
                        and (run["stopped"] or run["blocked"] or gates))):
                return Step("paused", subject or "", "supervision_timeout" if timed_out else run["blocked"] or "operator_gate")
            owner = subject or tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1',
                                   (), run_last=True))[0]
            evidence = tx.put(owner, "github_read_observation", wire_json({"code": code, "message": bounded_error(message)}))
            scope_id = tx.put(owner, "supervision_scope", wire_json(scope))
            pending = tx.rows(*run_statement(tx, "SELECT id,attempts,generation FROM effects WHERE subject=? AND status IN ('prepared','sent') AND operation!='issue_link' AND run_id=? ORDER BY rowid LIMIT 1",
                          (owner,), run_last=True))
            identity = scope_id if not pending else f"{scope_id}:{pending[0]['id']}:{pending[0]['attempts']}"
            if not tx.rows(*run_statement(tx, "SELECT position FROM history WHERE subject=? AND kind='github_read_observation' AND identity=? AND material_id=? AND run_id=? LIMIT 1",
                       (owner, identity, evidence), run_last=True)):
                self._event(tx, owner, "github_read_observation", identity, evidence,
                            generation=None if not pending else pending[0]["generation"])
            if code in {"ambiguous_run_pr", "invalid_fixture", "invalid_parent", "missing_parent", "stale_run_pr",
                        "foreign_run_pr", "github_invalid_response", "change_pr_identity_mismatch", "change_pr_head_drift", "parent_close_external_conflict"}:
                if subject is None:
                    tx.execute(*run_statement(tx, "UPDATE runs SET blocked='deterministic_contradiction' WHERE id=?",
                        (), run_last=True))
                    self._event(tx, owner, "paused", code, evidence)
                else:
                    self._gate(tx, subject, "deterministic_contradiction", evidence=evidence)
                return Step("paused", subject or "", code)
            return self._supervised_wait(tx, run, subject, scope, "github_convergence", 600, evidence)

    def _supervised_wait(self, tx: Transaction, run: sqlite3.Row, subject: str | None, scope: Mapping[str, Any],
                         kind: str, seconds: float, evidence: str | None = None) -> Step:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or scope.get('run') != selected.run_id:
            raise KernelError("effect_binding")
        owner = subject or tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1',
                               (), run_last=True))[0]
        scope_id = tx.put(owner, "supervision_scope", wire_json(scope))
        now = self.clock()
        # Actual pending checks establish their own frozen Policy window. Later
        # uncertainty reuses it; an earlier convergence wait cannot shorten it.
        checks_scope = scope.get("delivery", {}).get("phase") in {"checks", "merge"}
        rows = tx.rows(*run_statement(tx, 'SELECT * FROM waits WHERE scope_id=? AND (kind=? OR ?) AND run_id=? ORDER BY deadline LIMIT 1',
                   (scope_id, kind, checks_scope), run_last=True))
        if checks_scope and kind == "required_checks" and rows and rows[0]["kind"] == "github_convergence":
            tx.execute(*run_statement(tx, 'DELETE FROM waits WHERE scope_id=? AND run_id=?',
                (scope_id,), run_last=True))
            rows = []
        if rows:
            kind = rows[0]["kind"]
            seconds = rows[0]["deadline"] - rows[0]["started_at"]
        if not rows:
            # One current identity per subject and wait kind. Retain immutable
            # evidence, but never let an obsolete object govern a new candidate.
            tx.execute(*run_statement(tx, 'DELETE FROM waits WHERE subject IS ? AND kind=? AND run_id=?',
                (subject, kind), run_last=True))
            tx.execute(*run_statement(tx, 'INSERT INTO waits(subject,kind,scope_id,started_at,deadline,evidence,run_id) VALUES (?,?,?,?,?,?,?)',
                (subject, kind, scope_id, now, now + seconds, evidence), run_last=True))
        row = tx.one(*run_statement(tx, 'SELECT * FROM waits WHERE scope_id=? AND kind=? AND run_id=?',
                  (scope_id, kind), run_last=True))
        if now >= row["deadline"]:
            detail = tx.put(owner, "supervision_timeout", wire_json({"kind": kind, "scope_id": scope_id,
                "started_at": row["started_at"], "deadline": row["deadline"], "budget_seconds": seconds,
                "retry_count": row["retry_count"], "latest_observation": evidence or row["evidence"]}))
            if evidence is not None:
                tx.execute(*run_statement(tx, 'UPDATE waits SET evidence=? WHERE scope_id=? AND kind=? AND run_id=?',
                    (evidence, scope_id, kind), run_last=True))
            if subject is None:
                tx.execute(*run_statement(tx, "UPDATE runs SET blocked='supervision_timeout' WHERE id=?",
                    (), run_last=True))
                self._event(tx, owner, "paused", "supervision_timeout", detail)
            else:
                self._gate(tx, subject, "supervision_timeout", evidence=detail)
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                for pending in tx.rows(*run_statement(tx, "SELECT id FROM effects WHERE subject IS ? AND status='sent' AND run_id=?",
                                   (subject,), run_last=True)):
                    self._event(tx, owner, "abandon_reconciliation_pending", pending["id"], detail)
            self._finish_final_failure(tx, run)
            return Step("paused", subject or "", "supervision_timeout")
        retries = row["retry_count"]
        retry_at = row["next_retry_at"]
        if retry_at is None or now >= retry_at:
            retries += 1
            retry_at = min(now + min(60, 5 * 2 ** min(retries - 1, 4)), row["deadline"])
        tx.execute(*run_statement(tx, 'UPDATE waits SET retry_count=?,next_retry_at=?,evidence=COALESCE(?,evidence) WHERE scope_id=? AND kind=? AND run_id=?',
            (retries, retry_at, evidence, scope_id, kind), run_last=True))
        return Step("waiting", subject or "", kind, retry_at=retry_at)

    def agent_source_ready(self, call: AgentCall) -> None:
        """The original Worker is authorized to release, beyond a successful read."""
        with self.storage.transaction() as tx:
            invocation = self._agent_authority(tx, call)
            if invocation["dispatch_state"] != "started":
                raise KernelError("agent_not_started")
            self._charge_development(tx, call.attempt_id)
            tx.execute(*run_statement(tx, "DELETE FROM waits WHERE subject=? AND kind='github_convergence' AND run_id=?",
                (call.subject,), run_last=True))

    def _advance_cleanup(self, tx: Transaction, work: Work, world: World | None) -> Step | IORequest:
        """Bounded program work; cleanup never owns an Agent or integration seat."""
        subject, execution = work.subject, work.execution
        assert subject is not None
        abandoned = self._abandon_action(tx)
        if abandoned is not None:
            self._abandon_owned(tx, abandoned, execution)
            return Step("complete" if abandoned["status"] == "complete" else "waiting", subject, "abandoned")
        run, job = self._owned(tx, subject, execution)
        waiting = self._cleanup_wait(tx, run, job)
        if waiting is not None:
            return waiting
        records = tx.rows(*run_statement(tx, 'SELECT * FROM resource_cleanup WHERE subject=? AND generation=? AND run_id=?',
                      (subject, job["generation"]), run_last=True))
        if not records or records[0]["state"] == "complete":
            return Step("complete", subject, "resources_retired")
        cleanup = records[0]
        if cleanup["effect_id"]:
            pending = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                          (cleanup["effect_id"],), run_last=True))
            if pending["status"] == "sent":
                return self._effect_work(tx, work, pending)
            if pending["status"] == "observed":
                result = json.loads(tx.get(pending["receipt_id"], subject))
                if result["disposition"] != "target":
                    raise KernelError("invalid_cleanup_receipt")
                tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
                    (pending["id"],), run_last=True))
                stage = cleanup["stage"] + 1
                tx.execute(*run_statement(tx, 'UPDATE resource_cleanup SET stage=?,state=?,effect_id=NULL,outcome=NULL WHERE subject=? AND generation=? AND run_id=?',
                    (stage, "complete" if stage == len(self.CLEANUP) else "pending", subject, job["generation"]), run_last=True))
                self._event(tx, subject, "resource_retired", pending["operation"], pending["receipt_id"])
                return Step("progress", subject)
            if pending["status"] == "cancelled":
                tx.execute(*run_statement(tx, 'UPDATE resource_cleanup SET effect_id=NULL WHERE subject=? AND generation=? AND run_id=?',
                    (subject, job["generation"]), run_last=True))
                return Step("progress", subject)
            if run["stopped"] or run["blocked"] or self._gates(tx, subject):
                return Step("paused", subject, "cleanup_authority")
            if world is None:
                return self._observe_work(tx, work, subject)
            return self._effect_work(tx, work, pending)
        if run["stopped"] or run["blocked"] or self._gates(tx, subject):
            return Step("paused", subject, "cleanup_authority")
        operation = self.CLEANUP[cleanup["stage"]]
        # A first local intent uses the confirmed close, not fresh Delivery
        # facts. Fresh Source and all write guards still precede deletion.
        if world is None and operation not in {"cleanup_worktree", "cleanup_local_ref"}:
            return self._observe_work(tx, work, subject)
        head = cleanup["workspace_sha"] if operation == "cleanup_worktree" else cleanup["source_sha"]
        if operation == "cleanup_local_ref":
            workspaces = tx.rows(*run_statement(tx, "SELECT * FROM effects WHERE subject=? AND generation=? AND source_ref=? AND operation='workspace' AND status='confirmed' AND run_id=?",
                             (subject, cleanup["generation"], cleanup["source_ref"]), run_last=True))
            if len(workspaces) != 1:
                return Step("paused", subject, "cleanup_marker_unproven")
            if workspaces:
                workspace = workspaces[0]
                intent = json.loads(tx.get(workspace["payload_id"], subject))
                receipt = json.loads(tx.get(workspace["receipt_id"], subject))
                if (any(intent.get(key) != cleanup[key] for key in ("checkout", "owned_root", "common_git_dir"))
                        or receipt.get("disposition") != "target" or not isinstance(receipt.get("commit"), dict)
                        or receipt["commit"].get("sha") != workspace["before_sha"]):
                    return Step("paused", subject, "cleanup_marker_unproven")
                if intent.get("create_ref") is False:
                    # An ordinary detached Final workspace creates no
                    # local branch. Remote publication grants no ownership.
                    tx.execute(*run_statement(tx, 'UPDATE resource_cleanup SET stage=stage+1 WHERE subject=? AND generation=? AND run_id=?',
                        (subject, cleanup["generation"]), run_last=True))
                    self._event(tx, subject, "resource_not_owned", operation, workspace["payload_id"])
                    return Step("progress", subject)
                elif intent.get("create_ref") is True:
                    head = workspace["before_sha"]
                else:
                    return Step("paused", subject, "cleanup_marker_unproven")
        payload = {"checkout": cleanup["checkout"], "owned_root": cleanup["owned_root"],
                   "common_git_dir": cleanup["common_git_dir"], "branch_ref": cleanup["source_ref"], "expected_head": head,
                   "remote": self._inputs(tx, run)["repository_remote"]}
        step = self._prepare_effect(tx, run, job, work, world, operation, before=head, payload=payload)
        tx.execute(*run_statement(tx, 'UPDATE resource_cleanup SET effect_id=? WHERE subject=? AND generation=? AND run_id=?',
            (step.effect.effect_id, subject, job["generation"]), run_last=True))
        return step

    def _cleanup_current(self, tx: Transaction, job: sqlite3.Row, row: sqlite3.Row) -> bool:
        records = tx.rows(*run_statement(tx, 'SELECT * FROM resource_cleanup WHERE subject=? AND generation=? AND run_id=?',
                      (job["id"], row["generation"]), run_last=True))
        return bool(records and job["phase"] == "complete" and job["generation"] == row["generation"]
                    and records[0]["effect_id"] == row["id"] and records[0]["stage"] < len(self.CLEANUP)
                    and self.CLEANUP[records[0]["stage"]] == row["operation"] and records[0]["source_ref"] == row["source_ref"])

    def acknowledge_link(self, effect: Effect, result: EffectResult) -> None:
        """Accept the original call's acknowledgement; never promote a readback."""
        with self.storage.transaction() as tx:
            row = tx.one(*run_statement(tx, "SELECT * FROM effects WHERE id=? AND operation='issue_link' AND run_id=?",
                      (effect.effect_id,), run_last=True))
            if self._effect(tx, row, effect.execution) != effect or row["status"] != "sent":
                raise KernelError("effect_binding")
            status = result.raw.get("acknowledged_status")
            if status not in {"linked", "unavailable"}:
                return
            receipt = tx.put(effect.subject, "issue_link_ack", wire_json(asdict(result)))
            tx.execute(*run_statement(tx, 'UPDATE issue_links SET state=? WHERE effect_id=? AND run_id=?',
                (status, effect.effect_id), run_last=True))
            tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed',receipt_id=? WHERE id=? AND run_id=?",
                (receipt, effect.effect_id), run_last=True))
            self._event(tx, effect.subject, "issue_link_acknowledged", effect.effect_id, receipt)

    def receive(self, call: AgentCall, raw: bytes) -> None:
        """Durable raw/exit settlement is permitted after Stop or owner retirement.

        It cannot advance phase, clear a gate, or allocate work. A raw result
        proves work and settles any not-yet-accounted Development charge.
        Both the attempt and the exact invocation/thread/lease must still match.
        """
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            material = tx.put(call.subject, "agent_raw", raw)
            if invocation["raw_id"] is not None:
                if invocation["raw_id"] != material:
                    raise KernelError("conflicting_return")
                return
            if invocation["status"] not in {"running", "interrupted"}:
                raise KernelError("invocation_already_settled")
            self._charge_development(tx, call.attempt_id)
            tx.execute(*run_statement(tx, 'UPDATE invocation_outputs SET raw_id=?,returned_at=? WHERE invocation=? AND ordinal=? AND run_id=?',
                (material, self.clock(), call.invocation_id, call.output_ordinal), run_last=True))
            # A late output or superseded Invocation is history only.
            if self._latest_output(tx, invocation):
                tx.execute(*run_statement(tx, "UPDATE invocations SET status='returned' WHERE id=? AND run_id=?",
                    (call.invocation_id,), run_last=True))
                tx.execute(*run_statement(tx, "UPDATE attempts SET result_id=?,status='returned' WHERE id=? AND current_invocation=? AND run_id=?",
                    (material, call.attempt_id, call.invocation_id), run_last=True))
            current = self._job(tx, call.subject)
            if invocation["generation"] != current["generation"]:
                attempt = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                              (call.attempt_id,), run_last=True))
                if attempt["role"] == Role.REVIEW:
                    try:
                        artifact(Role.REVIEW, raw)
                    except KernelError:
                        pass
                    else:
                        self._charge_review(tx, attempt)
            if invocation["dispatch_state"] not in {"sent", "started", "unknown"}:
                tx.execute(*run_statement(tx, 'DELETE FROM leases WHERE id=? AND invocation=? AND run_id=?',
                    (call.lease_id, call.invocation_id), run_last=True))
            self._event(tx, call.subject, "raw_returned", call.invocation_id, material, generation=invocation["generation"])

    def bind_thread(self, call: AgentCall, reported_thread: str) -> None:
        """Persist a real backend thread.started event, separate from logical ID."""
        if not reported_thread:
            raise KernelError("missing_thread")
        with self.storage.transaction() as tx:
            self._invocation(tx, call)
            thread = tx.one(*run_statement(tx, 'SELECT backend_id FROM threads WHERE id=? AND run_id=?',
                         (call.thread_id,), run_last=True))
            if thread[0] is not None and thread[0] != reported_thread:
                raise KernelError("thread_identity_changed")
            tx.execute(*run_statement(tx, 'UPDATE threads SET backend_id=? WHERE id=? AND run_id=?',
                (reported_thread, call.thread_id), run_last=True))

    @staticmethod
    def _invocation_output(tx: Transaction, invocation_id: str, ordinal: int) -> sqlite3.Row:
        return tx.one(*run_statement(tx, 'SELECT i.*,o.*,a.subject,a.role,a.purpose,a.generation,a.parent_revision,a.graph_revision,a.revision,a.current_invocation FROM invocations i JOIN invocation_outputs o ON o.run_id=i.run_id AND o.invocation=i.id JOIN attempts a ON a.run_id=i.run_id AND a.id=i.attempt WHERE i.id=? AND o.ordinal=? AND i.run_id=?',
                  (invocation_id, ordinal), run_last=True))

    def inspection_bindings(self, tx: Transaction) -> InspectionBindings:
        """Read current Call inputs and reuse formal binding checks; no authority."""
        if transaction_scope(tx).run_id != self.storage.run_id:
            raise KernelError("run_scope_changed")
        run = tx.one(*run_statement(tx, "SELECT id,executor,epoch FROM runs WHERE id=?", (), run_last=True))
        current = (run['id'], run['executor'], run['epoch'])
        hosts = tx.rows('SELECT * FROM hosts WHERE run_id=? AND owner=? AND epoch=?', current)
        if not hosts:return InspectionBindings(current, None, None, ())
        request = self._host_launch(tx, hosts[0])
        if (request.run_id, request.owner, request.epoch) != current:
            raise KernelError('host_binding_changed')
        self._host(tx, request)
        workers = []
        for item in tx.rows('SELECT i.id,max(o.ordinal) AS ordinal FROM subjects s '
                'JOIN attempts a ON a.run_id=s.run_id AND a.id=s.current_attempt AND a.generation=s.generation '
                'JOIN invocations i ON i.run_id=a.run_id AND i.id=a.current_invocation '
                'JOIN invocation_outputs o ON o.run_id=i.run_id AND o.invocation=i.id '
                'WHERE s.run_id=? AND i.executor=? AND i.epoch=? GROUP BY i.id ORDER BY s.id', current):
            row = self._invocation_output(tx, item['id'], item['ordinal'])
            job = self._job(tx, row['subject'])
            thread = tx.one(*run_statement(tx, 'SELECT * FROM threads WHERE id=? AND run_id=?', (row['thread'],), run_last=True))
            profile, reference = self._thread_profile(tx, thread)
            context = json.loads(tx.get(row['context_id'], row['subject']))
            if not isinstance(context, dict):raise KernelError('invocation_binding')
            call = AgentCall(row['attempt'], item['id'], row['subject'], Role(row['role']), Purpose(row['purpose']),
                row['thread'], row['lease'], Execution(job['run_id'], row['executor'], row['epoch']), context,
                profile, row['deadline_seconds'], row['deadline_at'], row['mode'], row['requested_thread'],
                item['ordinal'], thread['profile_revision'], reference)
            if call.execution != Execution(request.run_id, request.owner, request.epoch):
                raise KernelError('invocation_binding')
            workers.append((call, dict(self._invocation(tx, call))))
        return InspectionBindings(current, request, dict(hosts[0]), tuple(workers))

    def _invocation(self, tx: Transaction, call: AgentCall) -> sqlite3.Row:
        # Opaque IDs and a reused owner/epoch do not imply the Call's Run.
        # Exact late receipts retain their original Run binding.
        row = self._invocation_output(tx, call.invocation_id, call.output_ordinal)
        if (row["attempt"] != call.attempt_id or row["subject"] != call.subject or row["thread"] != call.thread_id
                or row["lease"] != call.lease_id or row["executor"] != call.execution.owner or row["epoch"] != call.execution.epoch
                or row["role"] != call.role or row["purpose"] != call.purpose
                or row["mode"] != call.mode or row["requested_thread"] != call.reported_thread
                or row["deadline_seconds"] != call.deadline_seconds or row["deadline_at"] != call.deadline_at
                or json.loads(tx.get(row["context_id"], call.subject)) != call.context):
            raise KernelError("invocation_binding")
        job = self._job(tx, call.subject)
        if job["run_id"] != call.execution.run_id:
            raise KernelError("invocation_binding")
        binding = tx.one(*run_statement(tx, 'SELECT * FROM threads WHERE id=? AND run_id=?',
                      (call.thread_id,), run_last=True))
        if not self._thread_role_matches(job, call.role, binding):
            raise KernelError("thread_role_binding")
        profile, reference = self._thread_profile(tx, binding)
        if call.profile != profile or call.profile_revision != binding["profile_revision"] or call.profile_reference != reference:
            raise KernelError("invocation_binding")
        return row

    def _latest_output(self, tx: Transaction, output: sqlite3.Row) -> bool:
        return bool(output["ordinal"] == tx.one(*run_statement(tx, 'SELECT max(ordinal) FROM invocation_outputs WHERE invocation=? AND run_id=?',
                                             (output["id"],), run_last=True))[0])

    def _agent_subject_authority(self, tx: Transaction, subject: str, attempt_id: str, execution: Execution) -> None:
        run, job = self._owned(tx, subject, execution)
        attempt = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                      (attempt_id,), run_last=True))
        if (run["stopped"] or run["blocked"] or self._gates(tx, subject)
                or attempt["generation"] != job["generation"] or job["current_attempt"] != attempt_id
                or attempt["parent_revision"] != run["parent_revision"] or attempt["graph_revision"] != run["graph_revision"]
                or attempt["revision"] != job["revision"] or self._eligibility(tx, job) is not None):
            raise KernelError("agent_authority_changed")

    def _agent_authority(self, tx: Transaction, call: AgentCall) -> sqlite3.Row:
        self._agent_subject_authority(tx, call.subject, call.attempt_id, call.execution)
        invocation = self._invocation(tx, call)
        if self.clock() >= invocation["deadline_at"]:
            raise KernelError("invocation_deadline_exceeded")
        if (invocation["current_invocation"] != call.invocation_id or not self._latest_output(tx, invocation)
                or invocation["raw_id"] is not None or invocation["dispatch_state"] not in {"allocated", "sent", "started"}
                or not tx.rows(*run_statement(tx, 'SELECT id FROM leases WHERE id=? AND invocation=? AND run_id=?',
                           (call.lease_id, call.invocation_id), run_last=True))):
            raise KernelError("agent_authority_changed")
        return invocation

    def worker_settled(self, call: AgentCall | AgentPreparation) -> bool:
        with self.storage.transaction(write=False) as tx:
            if isinstance(call, AgentPreparation):
                selected = transaction_scope(tx)
                if selected.run_id != call.execution.run_id:
                    raise KernelError("preparation_binding")
                for waiting in tx.rows(*run_statement(tx, "SELECT * FROM waits WHERE subject=? AND kind='agent_preparation' AND run_id=?",
                                   (call.subject,), run_last=True)):
                    if self._preparation(tx, waiting)[0].reservation_id == call.reservation_id:
                        return False
                rows = tx.rows(*run_statement(tx, 'SELECT i.*,o.* FROM invocation_outputs o JOIN invocations i ON i.run_id=o.run_id AND i.id=o.invocation WHERE o.lease=? AND o.run_id=?',
                           (call.reservation_id,), run_last=True))
                if not rows:
                    for event in tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE kind='agent_preparation_settled' AND identity=? AND subject=? AND run_id=?",
                                     (call.reservation_id, call.subject), run_last=True)):
                        proof = json.loads(tx.get(event["material_id"], call.subject))
                        stored, _ = self._preparation(tx, {"scope_id": proof["preparation_scope"],
                            "subject": call.subject, "deadline": proof["deadline"]})
                        if stored == call:
                            return True
                    return False
                row = rows[0]
                if (row["attempt"] != call.attempt_id or row["executor"] != call.execution.owner
                        or row["epoch"] != call.execution.epoch or row["thread"] != call.thread_id
                        or wire_json(call.context) != tx.get(row["context_id"], call.subject)):
                    raise KernelError("preparation_binding")
                lease_id = call.reservation_id
            else:
                row = self._invocation(tx, call)
                lease_id = call.lease_id
            leased = tx.rows(*run_statement(tx, 'SELECT id FROM leases WHERE id=? AND invocation=? AND run_id=?',
                         (lease_id, row["id"]), run_last=True))
            return not leased and (row["dispatch_state"] == "exited" and row["exit_code"] is not None
                                   or row["dispatch_state"] == "unavailable" and row["process_identity"] is None and row["start_evidence"] is not None)

    def dispatch_agent(self, call: AgentCall) -> bool:
        """One durable launch claim. A repeated call never authorizes a restart."""
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["dispatch_state"] != "allocated":
                return False
            self._agent_authority(tx, call)
            tx.execute(*run_statement(tx, "UPDATE invocation_outputs SET dispatch_state='sent',dispatched_at=? WHERE invocation=? AND ordinal=? AND run_id=?",
                (self.clock(), call.invocation_id, call.output_ordinal), run_last=True))
            self._event(tx, call.subject, "agent_dispatched", call.invocation_id)
            return True

    def guard_agent(self, call: AgentCall) -> None:
        """Check after the driver has refreshed real source facts, before release."""
        with self.storage.transaction(write=False) as tx:
            invocation = self._agent_authority(tx, call)
            if invocation["dispatch_state"] not in {"sent", "started"}:
                raise KernelError("agent_not_dispatched")

    def agent_started(self, call: AgentCall, process_identity: dict[str, Any]) -> None:
        """Finite process binding may settle after revocation; it is no grant."""
        required = {"pid", "start_token", "pgid", "host_pid", "host_start_token"}
        if (not required <= process_identity.keys() or any(not process_identity[key] for key in required)
                or type(process_identity["pid"]) is not int or process_identity["pid"] <= 1):
            raise KernelError("invalid_process_identity")
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            hosts = tx.rows("SELECT * FROM hosts WHERE run_id=? AND owner=? AND epoch=?", (call.execution.run_id, call.execution.owner, call.execution.epoch))
            if hosts and not self._host_contains_process(tx, hosts[0], process_identity):
                raise KernelError("worker_host_binding")
            witness = tx.put(call.subject, "worker_process_identity", wire_json(process_identity))
            if invocation["process_identity"] is not None:
                if invocation["process_identity"] != witness:
                    raise KernelError("process_identity_changed")
                return
            if invocation["dispatch_state"] not in {"sent", "unknown"}:
                raise KernelError("agent_not_dispatched")
            tx.execute(*run_statement(tx, "UPDATE invocation_outputs SET process_identity=?,dispatch_state='started',started_at=? WHERE invocation=? AND ordinal=? AND run_id=?",
                (witness, self.clock(), call.invocation_id, call.output_ordinal), run_last=True))
            self._event(tx, call.subject, "worker_started", call.invocation_id, witness, generation=invocation["generation"])

    def agent_start_unknown(self, call: AgentCall, evidence: dict[str, Any]) -> None:
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["dispatch_state"] in {"exited", "unavailable"}:
                return
            known = evidence.get("process_identity")
            observed_exit = evidence.get("observed_exit")
            if (invocation["process_identity"] is not None and isinstance(known, dict) and type(observed_exit) is int
                    and wire_json(known) == tx.get(invocation["process_identity"], call.subject)):
                self._process_exit(tx, call, invocation, observed_exit, "start_acknowledgement_unknown", "fatal")
                return
            material = tx.put(call.subject, "worker_start_unknown", wire_json(evidence))
            tx.execute(*run_statement(tx, "UPDATE invocation_outputs SET dispatch_state='unknown',start_evidence=? WHERE invocation=? AND ordinal=? AND run_id=?",
                (material, call.invocation_id, call.output_ordinal), run_last=True))
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            job = self._job(tx, call.subject)
            if (not run["stopped"] and not run["blocked"] and run["executor"] == call.execution.owner and run["epoch"] == call.execution.epoch
                    and job["generation"] == invocation["generation"] and job["current_attempt"] == call.attempt_id
                    and invocation["current_invocation"] == call.invocation_id and self._latest_output(tx, invocation)
                    and not self._gates(tx, call.subject)):
                self._gate(tx, call.subject, "worker_start_unknown", evidence=material)

    def agent_unavailable(self, call: AgentCall, code: str, evidence: dict[str, Any] | str, *,
                          before_dispatch: bool = False) -> None:
        """Settle owned unspawned work; an unacknowledged claim is allocated-only."""
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["process_identity"] is not None:
                raise KernelError("worker_exit_unproven")
            if invocation["dispatch_state"] == "unavailable":
                return  # Preserve the original exact cleanup receipt on repeated settlement.
            if invocation["dispatch_state"] != ("allocated" if before_dispatch else "sent"):
                raise KernelError("worker_exit_unproven")
            material = tx.put(call.subject, "worker_unavailable", wire_json({"code": code, "evidence": evidence}))
            tx.execute(*run_statement(tx, "UPDATE invocation_outputs SET dispatch_state='unavailable',start_evidence=? WHERE invocation=? AND ordinal=? AND run_id=?",
                (material, call.invocation_id, call.output_ordinal), run_last=True))
            if self._latest_output(tx, invocation):
                tx.execute(*run_statement(tx, "UPDATE invocations SET status='interrupted' WHERE id=? AND run_id=?",
                    (call.invocation_id,), run_last=True))
            tx.execute(*run_statement(tx, 'DELETE FROM leases WHERE id=? AND invocation=? AND run_id=?',
                (call.lease_id, call.invocation_id), run_last=True))
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            job = self._job(tx, call.subject)
            if (not run["stopped"] and not run["blocked"] and run["executor"] == call.execution.owner and run["epoch"] == call.execution.epoch
                    and not self._gates(tx, call.subject) and job["current_attempt"] == call.attempt_id
                    and invocation["current_invocation"] == call.invocation_id and self._latest_output(tx, invocation)
                    and job["generation"] == invocation["generation"]):
                self._gate(tx, call.subject, "worker_unavailable", evidence=material)

    def agent_exited(self, call: AgentCall, process_identity: dict[str, Any], returncode: int, *, failure: str | None = None,
                     failure_kind: str = "ordinary", diagnostic: Mapping[str, Any] | None = None) -> None:
        if failure_kind not in {"ordinary", "capacity", "fatal"}:
            raise KernelError("invalid_failure_kind")
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["process_identity"] is None or json.loads(tx.get(invocation["process_identity"], call.subject)) != process_identity:
                raise KernelError("process_identity_changed")
            self._process_exit(tx, call, invocation, returncode, failure, failure_kind, diagnostic)

    def observe_worker_activity(self, call: AgentCall, observation_id: str, *, active: bool, observed_at: float,
                                evidence: Mapping[str, Any]) -> None:
        """Record exact Worker events; process lifetime does not imply active work."""
        if not observation_id or type(active) is not bool or not math.isfinite(observed_at) or observed_at < 0 or not evidence:
            raise KernelError("invalid_activity_observation")
        with self.storage.transaction() as tx:
            output = self._invocation(tx, call)
            if output["process_identity"] is None:
                raise KernelError("worker_start_unproven")
            material = tx.put(call.subject, "worker_activity", wire_json(dict(evidence)))
            old = tx.rows(*run_statement(tx, 'SELECT * FROM worker_activity WHERE invocation=? AND ordinal=? AND identity=? AND run_id=?',
                      (call.invocation_id, call.output_ordinal, observation_id), run_last=True))
            if old:
                if (old[0]["active"], old[0]["observed_at"], old[0]["evidence"]) != (active, observed_at, material):
                    raise KernelError("conflicting_activity_observation")
                return
            first_active = active and not tx.rows(*run_statement(tx, 'SELECT identity FROM worker_activity WHERE invocation=? AND ordinal=? AND active=1 AND run_id=? LIMIT 1',
                                              (call.invocation_id, call.output_ordinal), run_last=True))
            tx.execute(*run_statement(tx, 'INSERT INTO worker_activity(invocation,ordinal,identity,active,observed_at,evidence,run_id) VALUES (?,?,?,?,?,?,?)',
                (call.invocation_id, call.output_ordinal, observation_id, active, observed_at, material), run_last=True))
            if first_active:
                try:
                    self._agent_authority(tx, call)
                except KernelError as error:
                    if error.code not in {"executor_fenced", "host_usage_unproven", "agent_authority_changed",
                                          "invocation_deadline_exceeded"}:
                        raise
                    # Late activity is still evidence, never a current start.
                else:
                    self._event(tx, call.subject, "worker_active", f"{call.invocation_id}:{call.output_ordinal}",
                                material, generation=output["generation"])

    def _process_exit(self, tx: Transaction, call: AgentCall, invocation: sqlite3.Row, returncode: int, failure: str | None,
                      failure_kind: str, diagnostic: Mapping[str, Any] | None = None) -> None:
        if invocation["exit_code"] is not None:
            if invocation["exit_code"] != returncode:
                raise KernelError("conflicting_exit")
            return
        failed = returncode != 0 or failure is not None or invocation["raw_id"] is None
        exited_at = self.clock()
        tx.execute(*run_statement(tx, "UPDATE invocation_outputs SET dispatch_state='exited',exit_code=?,exited_at=?,failure_kind=?,recovery_ready_at=? WHERE invocation=? AND ordinal=? AND run_id=?",
            (returncode, exited_at, failure_kind if failed else None,
                    exited_at + 30 if failed and failure_kind == "capacity" else None, call.invocation_id, call.output_ordinal), run_last=True))
        tx.execute(*run_statement(tx, 'DELETE FROM leases WHERE id=? AND invocation=? AND run_id=?',
            (call.lease_id, call.invocation_id), run_last=True))
        self._event(tx, call.subject, "worker_exited", call.invocation_id, invocation["process_identity"], generation=invocation["generation"])
        if failed:
            material = tx.put(call.subject, "worker_failure", wire_json({"returncode": returncode, "failure": failure, "kind": failure_kind,
                                                                       "diagnostic": diagnostic}))
            tx.execute(*run_statement(tx, 'UPDATE invocation_outputs SET start_evidence=? WHERE invocation=? AND ordinal=? AND run_id=?',
                (material, call.invocation_id, call.output_ordinal), run_last=True))
            if invocation["raw_id"] is None and self._latest_output(tx, invocation):
                tx.execute(*run_statement(tx, "UPDATE invocations SET status='interrupted' WHERE id=? AND run_id=?",
                    (call.invocation_id,), run_last=True))
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            job = self._job(tx, call.subject)
            if (not run["stopped"] and not run["blocked"] and run["executor"] == call.execution.owner and run["epoch"] == call.execution.epoch
                    and not self._gates(tx, call.subject) and job["current_attempt"] == call.attempt_id
                    and invocation["current_invocation"] == call.invocation_id and self._latest_output(tx, invocation)
                    and job["generation"] == invocation["generation"] and failure_kind == "fatal"
                    and invocation["raw_id"] is None):
                self._gate(tx, call.subject, "execution_failed", evidence=material)

    def _guard_effect(self, effect: Effect, execution: Execution, world: World | LocalCleanupObservation, *,
                     resource_inspections: tuple[ResourceInspection, ...] | None = None, token: str | ReadFence) -> None:
        """Read-only authority guard for each real successor mutation in an adapter."""
        with self.storage.transaction(write=False) as tx:
            self._check_operation(tx, token, execution)
            run, job = self._effect_owned(tx, effect, execution)
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                      (effect.effect_id, effect.subject), run_last=True))
            self._guard_publisher(tx, run, row, world)
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                if row["status"] != "sent" or self._effect(tx, row, execution) != effect:
                    raise KernelError("operation_cancelled")
                self._abandon_write_current(tx, abandoned, row, resource_inspections)
                return
            if effect.operation == "retire_pr":
                if not isinstance(world, World):
                    raise KernelError("local_cleanup_observation_binding")
                if row["status"] != "sent" or self._effect(tx, row, execution) != effect or not self._retirement_authority(tx, run, job, row, world):
                    raise KernelError("operation_cancelled")
                return
            if effect.operation in self.CLEANUP:
                if (row["status"] != "sent" or run["stopped"] or run["blocked"] or self._gates(tx, effect.subject)
                        or not self._cleanup_current(tx, job, row) or self._effect(tx, row, execution) != effect):
                    raise KernelError("operation_cancelled")
                return
            if not isinstance(world, World):
                raise KernelError("local_cleanup_observation_binding")
            if effect.operation in {"completion_comment", "close"} and self._final_closing(tx, run, job):
                if (row["status"] != "sent" or run["stopped"] or run["blocked"] or self._gates(tx, effect.subject)
                        or row["generation"] != job["generation"] or world.graph_revision != run["graph_revision"]
                        or job["completion_revision"] != world.completion_revision or self._effect(tx, row, execution) != effect):
                    raise KernelError("operation_cancelled")
                return
            if (row["status"] != "sent" or run["stopped"] or run["blocked"] or self._gates(tx, effect.subject)
                    or row["generation"] != job["generation"] or row["revision"] != world.revision
                    or row["parent_revision"] != world.parent_revision or row["graph_revision"] != world.graph_revision
                    or job["completion_revision"] != world.completion_revision
                    or self._effect(tx, row, execution) != effect):
                raise KernelError("operation_cancelled")
            if (world.target is None and not (effect.operation == "run_branch" and job["kind"] == DeliveryKind.TICKET)
                    or effect.operation in {"integrate", "run_branch", "publication_commit", "push", "pr_write", "merge"}
                    and not self._effect_base_current(job, row, world)):
                raise KernelError("operation_boundary_changed")
            if effect.operation == "pr_write" and (not self._pr_write_current(run, job, world)
                    or effect.pr_number is None and (world.pr is not None or world.source is None
                                                     or world.source.sha != effect.after_sha)):
                raise KernelError("pr_identity_changed")
            if effect.operation == "merge" and not self._merge_authority(tx, run, job, world):
                raise KernelError("merge_authority_changed")

    @staticmethod
    def _control_binding(tx: Transaction, run: sqlite3.Row | None = None,
                         job: sqlite3.Row | None = None) -> None:
        """Reject foreign passed facts before control reads, audit or mutation."""
        selected = transaction_scope(tx)
        if run is not None and run['id'] != selected.run_id or (job is not None and job['run_id'] != selected.run_id):
            raise KernelError("control_binding")

    def _host_resume_gates(self, tx: Transaction, run: sqlite3.Row) -> list[sqlite3.Row]:
        """Only exact, physically retired Host failures form a shared boundary."""
        self._control_binding(tx, run)
        if (run["executor"] is not None or self._agent_seats(tx)
                or tx.rows(*run_statement(tx, "SELECT owner FROM hosts WHERE (status!='exited' OR usage_released=0) AND run_id=? LIMIT 1",
                       (), run_last=True))
                or tx.rows(*run_statement(tx, "SELECT owner FROM retired_executors WHERE status!='exited' AND run_id=? LIMIT 1",
                       (), run_last=True))):
            return []
        hosts = tx.rows(*run_statement(tx, "SELECT nonce,owner,epoch,observation_id FROM hosts WHERE status='exited' AND usage_released=1 AND run_id=?",
                    (), run_last=True))
        nonces = {host["nonce"] for host in hosts}
        owners = {(host["owner"], host["epoch"]) for host in hosts}
        observations = {host["observation_id"] for host in hosts if host["observation_id"] is not None}
        result = []
        for gate in tx.rows(*run_statement(tx, "SELECT g.*,m.purpose AS evidence_purpose FROM gates g JOIN material m ON m.run_id=g.run_id AND m.id=g.evidence AND m.subject=g.subject WHERE g.code IN ('execution_failed','worker_unavailable') AND g.run_id=?",
                        (), run_last=True)):
            if gate["evidence_purpose"] not in {"worker_host_exit", "agent_preparation_exit", "executor_failure"}:
                continue
            proof = json.loads(tx.get(gate["evidence"], gate["subject"]))
            if (gate["evidence_purpose"] == "worker_host_exit" and proof.get("host_nonce") in nonces
                    or gate["evidence_purpose"] == "agent_preparation_exit" and proof.get("evidence", {}).get("host_observation") in observations
                    or gate["evidence_purpose"] == "executor_failure" and (proof.get("owner"), proof.get("epoch")) in owners):
                result.append(gate)
        return result

    def resume_subject(self, *, ticket: int | None = None) -> str | None:
        """Select a recorded Resume scope; control rechecks it under its write lock."""
        with self.storage.transaction(write=False) as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            if ticket is not None:
                targets = tx.rows(*run_statement(tx, "SELECT id FROM subjects WHERE kind='ticket' AND issue=? AND run_id=?",
                              (ticket,), run_last=True))
            elif run["stopped"] or self._host_resume_gates(tx, run):
                return None
            else:
                targets = tx.rows(*run_statement(tx, 'SELECT DISTINCT s.id FROM subjects s JOIN gates g ON g.run_id=s.run_id AND g.subject=s.id WHERE s.run_id=? ORDER BY s.id',
                              (), run_last=True))
            if len(targets) != 1:
                raise KernelError("control_subject_ambiguous", "Select an exact Ticket or current Parent boundary")
            return str(targets[0]["id"])

    def control(self, kind: str, action_id: str, *, subject: str | None = None,
                world: World | None = None, message: str | None = None, new_thread: bool = False,
                policy_overrides: Mapping[str, Any] | None = None, replacement: Delivery | None = None,
                     resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False) -> str:
        """Apply one explicit operator intent atomically, with its application receipt.

        The offline prototype calls this trusted interface directly. Host launch,
        public capability isolation and process-exit proofs are separate adapters.
        """
        with self.storage.transaction(ordered=False) as tx:
            return self._control(tx, kind, action_id, subject=subject, world=world, message=message,
                                 new_thread=new_thread, policy_overrides=policy_overrides, replacement=replacement,
                                  resource_inspections=resource_inspections, discard_worktree=discard_worktree)

    def _checks_timeout_matches(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, evidence: str) -> bool:
        """Only the original expired checks scope permits a new wait policy."""
        self._control_binding(tx, run, job)
        timeout = json.loads(tx.get(evidence, job["id"]))
        if not isinstance(timeout, dict) or timeout.get("kind") != "required_checks":
            return False
        rows = tx.rows(*run_statement(tx, "SELECT * FROM waits WHERE subject=? AND kind='required_checks' AND scope_id=? AND run_id=?",
                   (job["id"], timeout.get("scope_id")), run_last=True))
        if len(rows) != 1:
            return False
        wait = rows[0]
        return bool(self.clock() >= wait["deadline"]
                    and (timeout.get("started_at"), timeout.get("deadline")) == (wait["started_at"], wait["deadline"])
                    and json.loads(tx.get(wait["scope_id"], job["id"])) == self._automatic_scope(tx, run, job["id"], False))

    def _control(self, tx: Transaction, kind: str, action_id: str, *, subject: str | None = None,
                 world: World | None = None, message: str | None = None, new_thread: bool = False,
                 policy_overrides: Mapping[str, Any] | None = None, replacement: Delivery | None = None,
                     resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False) -> str:
        if kind not in {"stop", "resume", "approve", "revise", "requeue", "abandon"}:
            raise KernelError("prototype_control_unimplemented", kind)
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                  (), run_last=True))
        material_owner = subject or tx.one(*run_statement(tx, 'SELECT id FROM subjects WHERE run_id=? ORDER BY id LIMIT 1',
                                        (), run_last=True))[0]
        command: dict[str, Any] = {"kind": kind, "subject": subject, "message": message, "new_thread": new_thread,
                                   "policy_overrides": dict(policy_overrides or {})}
        if replacement is not None:
            command["replacement"] = asdict(replacement)
        if resource_inspections is not None:
            command["resource_inspections"] = [inspection_wire(item) for item in resource_inspections]
        if discard_worktree:
            command["discard_worktree"] = True
        payload = wire_json(command)
        prior = tx.rows(*run_statement(tx, 'SELECT * FROM actions WHERE id=? AND run_id=?',
                    (action_id,), run_last=True))
        if prior:
            original_owner = prior[0]["subject"] or tx.one(*run_statement(tx, 'SELECT subject FROM material WHERE id=? AND run_id=?',
                                                        (prior[0]["payload_id"],), run_last=True))[0]
            if tx.get(prior[0]["payload_id"], original_owner) != payload:
                raise KernelError("action_identity_changed")
            return str(prior[0]["status"])
        if self._abandon_action(tx) is not None:
            raise KernelError("run_abandoned")
        if run["final_action"]:
            raise KernelError("final_approval_noncancellable")
        if replacement is not None and kind != "requeue":
            raise KernelError("replacement_identity")
        if kind != "abandon" and (resource_inspections is not None or discard_worktree):
            raise KernelError("abandon_resource_boundary")
        job = None if subject is None else self._job(tx, subject)
        if message is not None and (not message.strip() or len(message.encode()) > 8192):
            raise KernelError("human_response_boundary")
        if policy_overrides:
            if (kind != "resume" or job is None or "ticket_development_workers" in policy_overrides
                    or "development_thread_policy" in policy_overrides):
                raise KernelError("policy_override_boundary")
            gates = self._gates(tx, job["id"])
            timeout = next((gate for gate in gates if gate["code"] == "supervision_timeout"), None)
            if timeout is not None:
                allowed = (set(policy_overrides) == {"required_checks_wait_seconds"}
                           and policy_overrides["required_checks_wait_seconds"] is not None
                           and self._checks_timeout_matches(tx, run, job, timeout["evidence"]))
            else:
                allowed = any(gate["code"] in {"review_budget_exhausted", "modification_budget_exhausted", "ci_development_budget_exhausted"}
                              for gate in gates)
            if not allowed:
                raise KernelError("policy_override_boundary")
            policy = checkpoint_policy(self._subject_policy(tx, job), policy_overrides)
            policy_id = tx.put(job["id"], "delivery_policy", wire_json(self._policy_values(policy)))
            tx.execute(*run_statement(tx, 'UPDATE subjects SET policy_id=? WHERE id=? AND run_id=?',
                (policy_id, job["id"]), run_last=True))
            job = self._job(tx, job["id"])
        payload_id = tx.put(material_owner, "control_intent", payload)
        tx.execute("INSERT INTO actions(id,run_id,kind,subject,payload_id,status) VALUES (?,?,?,?,?,'applied')",
                   (action_id, run["id"], kind, subject, payload_id))
        feedback_scope: dict[str, dict[str, Any]] = {}
        requeue_scope: dict[str, Any] | None = None
        abandonment: dict[str, Any] | None = None
        if kind == "abandon":
            if subject is not None or type(discard_worktree) is not bool or resource_inspections is None:
                raise KernelError("abandon_resource_boundary")
            resources = self._generation_resources(tx)
            self._check_resource_inspections(resources, resource_inspections, discard_worktree)
            if tx.rows(*run_statement(tx, "SELECT id FROM subjects WHERE kind!='ticket' AND merge_sha IS NOT NULL AND run_id=? LIMIT 1",
                   (), run_last=True)):
                raise KernelError("merged_run_noncancellable")
            if tx.rows(*run_statement(tx, "SELECT e.id FROM effects e JOIN subjects s ON s.run_id=e.run_id AND s.id=e.subject WHERE s.kind!='ticket' AND e.operation='merge' AND e.status IN ('sent','observed') AND e.run_id=? LIMIT 1",
                   (), run_last=True)):
                raise KernelError("final_merge_unsettled")
            abandonment = {"resources": [resource_wire(item) for item in resources], "discard_worktree": discard_worktree,
                           "execution": {"run_id": run["id"], "owner": run["executor"], "epoch": run["epoch"]},
                           "generations": [[row["id"], row["generation"]] for row in tx.all(*run_statement(tx, 'SELECT id,generation FROM subjects WHERE run_id=? ORDER BY id',
                                                                                         (), run_last=True))],
                           "unsettled": [row[0] for row in tx.all(*run_statement(tx, "SELECT id FROM effects WHERE status IN ('prepared','sent','observed') AND run_id=?",
                                                               (), run_last=True)) ]}
            tx.execute(*run_statement(tx, "UPDATE actions SET status='pending' WHERE id=? AND run_id=?",
                (action_id,), run_last=True))
            if run["executor"] is not None:
                tx.execute("INSERT OR IGNORE INTO retired_executors VALUES (?,?,?,'pending')",
                           (run["id"], run["executor"], run["epoch"]))
            tx.execute("UPDATE runs SET epoch=epoch+1,integration_holder=NULL WHERE id=?", (run["id"],))
            tx.execute(*run_statement(tx, 'DELETE FROM work_queue WHERE run_id=?',
                (), run_last=True))
            tx.execute(*run_statement(tx, 'DELETE FROM integration_queue WHERE run_id=?',
                (), run_last=True))
        elif kind == "stop":
            if job is None:
                if not run["stopped"] and run["executor"] is not None:
                    tx.execute("INSERT INTO retired_executors VALUES (?,?,?,'pending')",
                               (run["id"], run["executor"], run["epoch"]))
                    tx.execute("UPDATE runs SET stopped=1,epoch=epoch+1 WHERE id=?", (run["id"],))
                else:
                    tx.execute("UPDATE runs SET stopped=1 WHERE id=?", (run["id"],))
                targets = tx.all("SELECT id FROM subjects WHERE run_id=?", (run["id"],))
            else:
                assert subject is not None  # job exists only for an explicit subject.
                self._gate(tx, subject, "operator_stopped")
                tx.execute(*run_statement(tx, 'DELETE FROM work_queue WHERE subject=? AND run_id=?',
                    (subject,), run_last=True))
                targets = [job]
            for target in targets:
                target_id = target["id"]
                tx.execute(*run_statement(tx, "UPDATE effects SET status='cancelled' WHERE subject=? AND status='prepared' AND run_id=?",
                    (target_id,), run_last=True))
                self._release_integration(tx, target_id)
        elif kind == "resume":
            if job is None:
                if run["executor"] is not None:
                    raise KernelError("executor_exit_unproven")
                if self._agent_seats(tx):
                    raise KernelError("worker_exit_unproven")
                host_gates = self._host_resume_gates(tx, run)
                if not run["stopped"] and run["blocked"] != "supervision_timeout" and not host_gates:
                    raise KernelError("no_resumable_boundary")
                for gate in host_gates:
                    tx.execute(*run_statement(tx, 'DELETE FROM gates WHERE subject=? AND code=? AND evidence=? AND run_id=?',
                        (gate["subject"], gate["code"], gate["evidence"]), run_last=True))
                tx.execute("UPDATE runs SET stopped=0 WHERE id=?", (run["id"],))
                if run["blocked"] == "supervision_timeout" and not host_gates:
                    tx.execute("UPDATE runs SET blocked=NULL WHERE id=?", (run["id"],))
                    tx.execute(*run_statement(tx, 'DELETE FROM waits WHERE subject IS NULL AND run_id=?',
                        (), run_last=True))
                tx.execute(*run_statement(tx, 'UPDATE attempts SET resume_action=? WHERE id IN (SELECT s.current_attempt FROM subjects s WHERE s.run_id=? AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.run_id=s.run_id AND g.subject=s.id)) AND run_id=?',
                    (action_id, run["id"]), run_last=True))
                if message is not None or new_thread:
                    feedback_scope = {row["id"]: {"generation": row["generation"], "attempt": row["current_attempt"],
                                                   "role": row["phase"] if row["phase"] in {"development", "review", "publish"} else None}
                                      for row in tx.all(*run_statement(tx, "WITH selected_run(id) AS (VALUES(?)) SELECT s.* FROM subjects s WHERE s.run_id=? AND s.run_id=(SELECT id FROM selected_run) AND s.phase!='complete' AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.run_id=s.run_id AND g.subject=s.id) AND NOT EXISTS (SELECT 1 FROM dependencies d JOIN subjects p ON p.run_id=d.run_id AND p.id=d.predecessor WHERE d.run_id=s.run_id AND d.subject=s.id AND p.phase!='complete')",
                                                     (run["id"],), run_last=False))
                                      if self._eligibility(tx, row) is None}
            else:
                assert subject is not None
                if self._agent_seats(tx, subject):
                    raise KernelError("worker_exit_unproven")
                gates = self._gates(tx, subject)
                if not gates:
                    raise KernelError("no_resumable_boundary")
                if message is not None:
                    response = tx.put(subject, "human_response", message.encode())
                    self._event(tx, subject, "human_response", action_id, response)
                for gate in gates:
                    code = gate["code"]
                    if code in {"review_budget_exhausted", "modification_budget_exhausted", "ci_development_budget_exhausted"}:
                        self._renew(tx, job, gate["purpose"])
                    elif code == "integration_resync_exhausted":
                        self._renew_resync(tx, job)
                    elif code == "human_blocker":
                        if job["current_attempt"]:
                            tx.execute(*run_statement(tx, "UPDATE attempts SET result_id=NULL,status='continuing' WHERE id=? AND run_id=?",
                                (job["current_attempt"],), run_last=True))
                    elif code in {"worker_unavailable", "execution_failed"}:
                        pass  # Explicitly continue the same charged Attempt.
                    elif code == "worker_start_unknown":
                        if not job["current_attempt"]:
                            raise KernelError("worker_exit_unproven")
                        invocation = tx.one(*run_statement(tx, 'SELECT i.*,o.* FROM invocations i JOIN attempts a ON a.run_id=i.run_id AND a.current_invocation=i.id JOIN invocation_outputs o ON o.run_id=i.run_id AND o.invocation=i.id WHERE a.id=? AND a.run_id=? ORDER BY o.ordinal DESC LIMIT 1',
                                         (job["current_attempt"],), run_last=True))
                        if invocation["dispatch_state"] != "exited" or invocation["process_identity"] is None:
                            raise KernelError("worker_exit_unproven")
                        if invocation["raw_id"] is None:
                            tx.execute(*run_statement(tx, "UPDATE invocations SET status='interrupted' WHERE id=? AND run_id=?",
                                (invocation["id"],), run_last=True))
                    elif code == "publication_retry_exhausted":
                        tx.execute(*run_statement(tx, "UPDATE effects SET retry_baseline=attempts WHERE subject=? AND status='sent' AND run_id=?",
                            (subject,), run_last=True))
                    elif code == "write_conflict":
                        pending_write = self._pending_effect(tx, subject)
                        if pending_write is None or pending_write["status"] != "observed":
                            raise KernelError("unresolved_write_conflict")
                    elif code not in {"operator_stopped", "execution_failed", "write_result_unknown", "ci_investigation_paused", "supervision_timeout"}:
                        continue
                    tx.execute(*run_statement(tx, 'DELETE FROM gates WHERE subject=? AND code=? AND run_id=?',
                        (subject, code), run_last=True))
                if new_thread and job["current_attempt"]:
                    tx.execute(*run_statement(tx, 'UPDATE attempts SET new_thread=1 WHERE id=? AND run_id=?',
                        (job["current_attempt"],), run_last=True))
                elif new_thread and job["phase"] == "development":
                    tx.execute(*run_statement(tx, 'UPDATE subjects SET development_thread=NULL WHERE id=? AND run_id=?',
                        (subject,), run_last=True))
                elif new_thread and job["phase"] == "publish":
                    feedback_scope = {subject: {"generation": job["generation"], "attempt": None, "role": "publish"}}
                if job["current_attempt"] and not self._gates(tx, subject):
                    tx.execute(*run_statement(tx, 'UPDATE attempts SET resume_action=? WHERE id=? AND run_id=?',
                        (action_id, job["current_attempt"]), run_last=True))
                if job["phase"] in {"development", "review", "publish"}:
                    self._ready(tx, subject, "work_queue")
                tx.execute(*run_statement(tx, "DELETE FROM waits WHERE subject=? AND kind!='agent_preparation' AND run_id=?",
                    (subject,), run_last=True))
                if job["grant_id"] and job["phase"] in {"merge", "completion_comment", "close"}:
                    tx.execute("UPDATE runs SET final_action=? WHERE id=?", (action_id, run["id"]))
                    tx.execute(*run_statement(tx, "UPDATE actions SET status='pending' WHERE id=? AND run_id=?",
                        (action_id,), run_last=True))
        elif kind == "requeue" and job is not None and job["kind"] == DeliveryKind.FINAL_RUN:
            if world is None or replacement is not None:
                raise KernelError("requeue_not_authorized")
            self._requeue_final(tx, run, job, world)
        elif kind == "requeue":
            if job is None or subject is None or world is None or replacement is None:
                raise KernelError("requeue_not_authorized")
            revision = self._validate_requeue(tx, run, job, replacement, world)
            if self._pending_requeue(tx, subject) is not None:
                raise KernelError("requeue_already_pending")
            prior_pr = None
            retired_effect = None
            if job["pr_number"] is not None:
                prior_pr = world.pr
                written = tx.rows(*run_statement(tx, "SELECT * FROM effects WHERE subject=? AND generation=? AND operation='pr_write' AND status='confirmed' AND run_id=? ORDER BY rowid DESC LIMIT 1",
                              (subject, job["generation"]), run_last=True))
                original_pr = (None if not written or written[0]["receipt_id"] is None else
                               json.loads(tx.get(written[0]["receipt_id"], subject)).get("pr"))
                if (prior_pr is None or prior_pr.number != job["pr_number"] or prior_pr.state not in {"OPEN", "CLOSED"}
                        or not self._pr_identity(run, job, prior_pr) or prior_pr.head.sha != job["publication_sha"]
                        or prior_pr.state == "OPEN" and self._pr_target(run, job, world) is None
                        or not isinstance(original_pr, dict)
                        or not self._pr_identity(run, job, original_pr) or prior_pr.actor != original_pr["actor"]
                        or prior_pr.actor != written[0]["actor"]):
                    raise KernelError("retirement_pr_changed")
                if prior_pr.state == "CLOSED":
                    retired = self._recorded_retirement(tx, run, job, world)
                    retired_effect = retired["id"]
                    # Reuse the original preparation, including its exact base;
                    # only a new readback may establish that closure still owns
                    # the current lifecycle. It grants no second close call.
                    original_payload = json.loads(tx.get(retired["payload_id"], subject))
                    prior_pr = PullRequest(**{**original_payload["prior_pr"],
                        "head": Commit(**original_payload["prior_pr"]["head"]),
                        "base": Commit(**original_payload["prior_pr"]["base"]), "merged": None})
                    tx.execute(*run_statement(tx, "DELETE FROM gates WHERE subject=? AND code IN ('requeue_scope_changed','requeue_retirement_changed') AND run_id=?",
                        (subject,), run_last=True))
            requeue_scope = {"generation": job["generation"], "revision": revision,
                             "parent_revision": world.parent_revision, "graph_revision": world.graph_revision,
                             "target": asdict(self._require_target(world)), "prior_pr": None if prior_pr is None else asdict(prior_pr),
                             "retired_effect": retired_effect}
            if prior_pr is None:
                self._replace_generation(tx, run, job, replacement, revision, action_id, payload_id)
            else:
                tx.execute(*run_statement(tx, "UPDATE actions SET status='pending' WHERE id=? AND run_id=?",
                    (action_id,), run_last=True))
        elif kind == "revise":
            if (job is None or subject is None or world is None or message is None
                    or job["kind"] != DeliveryKind.FINAL_RUN or job["phase"] != "approval"):
                raise KernelError("no_revision_boundary")
            if (run["stopped"] or run["blocked"] or self._gates(tx, subject)
                    or self._agent_seats(tx, subject)
                    or self._pending_effect(tx, subject) is not None
                    or not self._current(tx, job, run, world) or not self._published(tx, run, job, world)
                    or not self._review_current(tx, job, world)):
                raise KernelError("revision_currentness")
            feedback = tx.put(subject, "maintainer_revision", message.encode())
            self._renew(tx, job, Purpose.INTEGRATION.value, new_cycle=True)
            tx.execute(*run_statement(tx, "UPDATE subjects SET phase='development',purpose='integration',repair_source='human_revision',last_failure=?,current_attempt=NULL,integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL,gate_mode=NULL,resume_purpose=NULL,version=version+1 WHERE id=? AND run_id=?",
                (feedback, subject), run_last=True))
            self._event(tx, subject, "revision_requested", action_id, feedback)
            self._ready(tx, subject, "work_queue")
        else:
            if job is None or world is None or job["phase"] != "approval" or job["kind"] == DeliveryKind.TICKET:
                raise KernelError("no_approval_boundary")
            assert subject is not None
            if (run["stopped"] or run["blocked"] or self._gates(tx, subject)
                    or self._pending_effect(tx, subject) is not None
                    or tx.rows(*run_statement(tx, 'SELECT id FROM leases WHERE subject=? AND run_id=?',
                           (subject,), run_last=True))
                    or not self._current(tx, job, run, world)):
                raise KernelError("approval_currentness")
            updating = bool(world.target is not None and job["integration_target"] and (
                world.target.sha != job["integration_target"]
                or world.source is not None and world.source.sha != job["source_sha"]))
            if updating:
                if (self._pr_target(run, job, world) is None
                        or not self._observe_integration_snapshot(tx, job, world)):
                    raise KernelError("approval_currentness")
                self._update_boundary(tx, run, job)
            else:
                if not self._published(tx, run, job, world) or not self._review_current(tx, job, world):
                    raise KernelError("approval_currentness")
                checks = world.checks
                if (checks is None or checks.result not in {"none", "pass", "fail"}
                        or checks.head_sha != job["publication_sha"] or checks.pr_number != job["pr_number"]
                        or not job["checks_id"]):
                    raise KernelError("approval_checks")
                if checks.result == "fail":
                    # The ordinary checks transition owns evidence, waits and
                    # CI budgets. This control grants no merge or new window.
                    tx.execute(*run_statement(tx, "UPDATE subjects SET phase='checks',grant_id=NULL,version=version+1 WHERE id=? AND run_id=?",
                        (subject,), run_last=True))
                    updating = True
                else:
                    check = tx.one(*run_statement(tx, 'SELECT material_id FROM checks WHERE id=? AND run_id=?',
                                (job["checks_id"],), run_last=True))
                    if not same_required_checks(json.loads(tx.get(check[0], subject)), checks):
                        raise KernelError("approval_checks_changed")
                    grant_id = identity()
                    tx.execute(*run_statement(tx, 'INSERT INTO grants(id,subject,action,pr_number,head_sha,base_sha,review_attempt,checks_id,revision,parent_revision,graph_revision,completion_revision,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (grant_id, subject, action_id, job["pr_number"], job["publication_sha"], self._require_target(world).sha,
                                job["review_attempt"], job["checks_id"], job["revision"], run["parent_revision"],
                                run["graph_revision"], job["completion_revision"]), run_last=True))
                    tx.execute(*run_statement(tx, "UPDATE subjects SET phase='merge',grant_id=?,version=version+1 WHERE id=? AND run_id=?",
                        (grant_id, subject), run_last=True))
                    tx.execute("UPDATE runs SET final_action=? WHERE id=?", (action_id, run["id"]))
            tx.execute(*run_statement(tx, 'UPDATE actions SET status=? WHERE id=? AND run_id=?',
                ("updating" if updating else "pending", action_id), run_last=True))
        receipt = tx.put(material_owner, "control_receipt", wire_json({"action": action_id, "kind": kind, "applied": True,
                                                                     "feedback_scope": feedback_scope, "requeue": requeue_scope, "abandonment": abandonment}))
        tx.execute(*run_statement(tx, 'UPDATE actions SET receipt_id=? WHERE id=? AND run_id=?',
            (receipt, action_id), run_last=True))
        self._event(tx, material_owner, "control_applied", action_id, receipt)
        if kind == "resume" and (subject is None or feedback_scope) and (message is not None or new_thread):
            self._event(tx, material_owner, "run_feedback_pending", action_id, receipt)
        return str(tx.one(*run_statement(tx, 'SELECT status FROM actions WHERE id=? AND run_id=?',
                       (action_id,), run_last=True))[0])

    def _renew(self, tx: Transaction, job: sqlite3.Row, purpose: str, *, new_cycle: bool = False) -> None:
        self._control_binding(tx, job=job)
        old = tx.one(*run_statement(tx, 'SELECT * FROM budgets WHERE subject=? AND purpose=? AND current=1 AND run_id=?',
                  (job["id"], purpose), run_last=True))
        tx.execute(*run_statement(tx, 'UPDATE budgets SET current=0 WHERE subject=? AND purpose=? AND window=? AND run_id=?',
            (job["id"], purpose, old["window"]), run_last=True))
        policy = self._subject_policy(tx, job)
        limit, review_limit = self._budget_limits(policy, DeliveryKind(job["kind"]))[Purpose(purpose)]
        developments, reviews = 0, 0
        if purpose == Purpose.CI:
            reviews = old["reviews_used"]  # CI Reviewer count is cumulative, not a renewable quota.
        if purpose == Purpose.INTEGRATION and job["kind"] == DeliveryKind.FINAL_RUN and not new_cycle:
            developments = 0 if old["developments_used"] >= old["development_limit"] else old["developments_used"]
            reviews = 0 if old["review_limit"] is not None and old["reviews_used"] >= old["review_limit"] else old["reviews_used"]
        if purpose == Purpose.INTEGRATION and job["kind"] != DeliveryKind.FINAL_RUN:
            # This policy extends an accumulated allowance instead of erasing use.
            developments, reviews = old["developments_used"], old["reviews_used"]
            limit += old["development_limit"]
        review_required = job["kind"] != DeliveryKind.TICKET or purpose == Purpose.REQUIREMENTS or (purpose == Purpose.CI and policy.ticket_ci_review)
        tx.execute(*run_statement(tx, 'INSERT INTO budgets(subject,purpose,window,developments_used,reviews_used,development_limit,review_limit,review_required,current,run_id) VALUES (?,?,?,?,?,?,?,?,1,?)',
            (job["id"], purpose, old["window"] + 1, developments, reviews, limit, review_limit, review_required), run_last=True))
        self._event(tx, job["id"], "budget_renewed", f"{purpose}:{old['window'] + 1}", job["policy_id"])

    def status(self, subject: str) -> dict[str, Any]:
        with self.storage.transaction(write=False) as tx:
            job = self._job(tx, subject)
            run = tx.one("SELECT * FROM runs WHERE id=?", (job["run_id"],))
            budgets = tx.rows(*run_statement(tx, 'SELECT * FROM budgets WHERE subject=? AND current=1 AND run_id=?',
                          (subject,), run_last=True))
            pending = None if not job["current_attempt"] else tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                                                                  (job["current_attempt"],), run_last=True))
            observed = tx.rows(*run_statement(tx, 'SELECT revision FROM issue_facts WHERE issue=? AND run_id=?',
                           (job["issue"],), run_last=True))
            reviewed_checkout = None
            if job["review_attempt"]:
                original = tx.one(*run_statement(tx, 'SELECT context_id FROM attempts WHERE id=? AND run_id=?',
                               (job["review_attempt"],), run_last=True))[0]
                reviewed_checkout = json.loads(tx.get(original, subject))["checkout"]
            return {
                "subject": subject, "kind": job["kind"], "phase": job["phase"], "purpose": job["purpose"],
                "generation": job["generation"], "version": job["version"],
                "policy": self._policy_values(self._subject_policy(tx, job)), "policy_id": job["policy_id"],
                "revision": job["revision"], "parent_revision": run["parent_revision"], "graph_revision": run["graph_revision"],
                "completion_revision": job["completion_revision"],
                "source_sha": job["source_sha"], "integration_target": job["integration_target"],
                "resync": self._resync_state(tx, subject),
                "reviewed_checkout": reviewed_checkout,
                "source_revision": (job["revision"] if job["phase"] == "complete" or not observed else observed[0][0]),
                "source_parent_revision": run["observed_parent_revision"] or run["parent_revision"],
                "pending_attempt": job["current_attempt"], "pending_return": bool(pending and pending["result_id"]),
                "candidate": job["candidate_sha"], "pr_number": job["pr_number"], "gate_mode": job["gate_mode"],
                "gates": [row["code"] for row in self._gates(tx, subject)], "stopped": bool(run["stopped"]),
                "holds_integration": run["integration_holder"] == subject,
                "pending_write": None if (effect := self._pending_effect(tx, subject)) is None
                else {"operation": effect["operation"], "status": effect["status"], "id": effect["id"]},
                "budgets": {row["purpose"]: {**{key: row[key] for key in ("window", "developments_used", "reviews_used", "development_limit", "review_limit")},
                                           "review_required": bool(row["review_required"])} for row in budgets},
                "resource_cleanup": next((dict(row) for row in tx.rows(*run_statement(tx, 'SELECT * FROM resource_cleanup WHERE subject=? AND generation=? AND run_id=?',
                                                                   (subject, job["generation"]), run_last=True))), None),
                "issue_link": next((dict(row) for row in tx.rows(*run_statement(tx, 'SELECT * FROM issue_links WHERE subject=? AND run_id=?',
                                                             (subject,), run_last=True))), None),
                "retired_generations": [dict(row) for row in tx.all(*run_statement(tx, 'SELECT * FROM retired_generations WHERE subject=? AND run_id=? ORDER BY generation',
                                                                 (subject,), run_last=True))],
            }

    def history(self, subject: str, *, after: int = 0, limit: int = 128) -> list[dict[str, Any]]:
        if not 1 <= limit <= 128:
            raise KernelError("query_limit")
        with self.storage.transaction(write=False) as tx:
            self._job(tx, subject)
            rows = tx.rows(*run_statement(tx, 'SELECT * FROM history WHERE run_id=? AND subject=? AND position>? ORDER BY position LIMIT ?',
                       (subject, after, limit), run_last=False))
            return [dict(row) for row in rows]

    def _owned(self, tx: Transaction, subject: str, execution: Execution) -> tuple[sqlite3.Row, sqlite3.Row]:
        job = self._job(tx, subject)
        run = tx.one("SELECT * FROM runs WHERE id=?", (job["run_id"],))
        self._executor_owned(tx, run, execution)
        return run, job

    @staticmethod
    def _executor_owned(tx: Transaction, run: sqlite3.Row, execution: Execution) -> None:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id:
            raise KernelError("executor_fenced")
        if tx.rows(*run_statement(tx, "SELECT id FROM actions WHERE kind='abandon' AND run_id=? LIMIT 1",
               (), run_last=True)):
            raise KernelError("executor_fenced")
        if (run["id"] != execution.run_id or run["executor"] != execution.owner or run["epoch"] != execution.epoch):
            raise KernelError("executor_fenced")
        hosts = tx.rows("SELECT status,usage_ready FROM hosts WHERE run_id=? AND owner=? AND epoch=?", (execution.run_id, execution.owner, execution.epoch))
        if hosts and (hosts[0]["status"] != "running" or not hosts[0]["usage_ready"]):
            raise KernelError("host_usage_unproven")

    def _job(self, tx: Transaction, subject: str) -> sqlite3.Row:
        row = tx.one(*run_statement(tx, 'SELECT * FROM subjects WHERE id=? AND run_id=?',
                  (subject,), run_last=True))
        if sum(len(value.encode()) if isinstance(value, str) else 16 for value in row) > CURRENT_LIMIT:
            raise KernelError("current_record_limit")
        return row

    def _gates(self, tx: Transaction, subject: str) -> list[sqlite3.Row]:
        return tx.rows(*run_statement(tx, 'SELECT * FROM gates WHERE subject=? AND run_id=? ORDER BY code',
                   (subject,), run_last=True))

    def _pause_reason(self, tx: Transaction, subject: str, run: sqlite3.Row, *, read: _SubjectSnapshot | None = None) -> str:
        if read is not None:
            read.require(tx, read.job)
        return "operator_stopped" if run["stopped"] else (run["blocked"] or ",".join(row["code"] for row in (self._gates(tx, subject) if read is None else read.gates)))

    def _gate(self, tx: Transaction, subject: str, code: str, *, purpose: str | None = None, evidence: str | None = None) -> None:
        tx.execute(*run_statement(tx, 'INSERT OR IGNORE INTO gates(subject,code,purpose,evidence,run_id) VALUES (?,?,?,?,?)',
            (subject, code, purpose, evidence), run_last=True))
        tx.execute(*run_statement(tx, 'DELETE FROM work_queue WHERE subject=? AND run_id=?',
            (subject,), run_last=True))
        # The existing release guard still fences any unsettled formal write.
        self._release_integration(tx, subject)
        self._event(tx, subject, "paused", code, evidence)

    def _current(self, tx: Transaction, job: sqlite3.Row, run: sqlite3.Row, world: World) -> bool:
        if world.graph_revision != run["graph_revision"]:
            tx.execute("UPDATE runs SET blocked='unsupported_scope_change' WHERE id=?", (run["id"],))
            self._gate(tx, job["id"], "unsupported_scope_change")
            return False
        if world.parent_revision != run["parent_revision"] or world.revision != job["revision"]:
            if not self._final_closing(tx, run, job):
                self._gate(tx, job["id"], "requeue_required")
                return False
        if world.completion_revision != job["completion_revision"]:
            self._gate(tx, job["id"], "completion_changed")
            return False
        return True

    @staticmethod
    def _require_target(world: World) -> Commit:
        if world.target is None:
            raise KernelError("target_branch_missing")
        return world.target

    def _initial_run_branch(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                            work: Work, world: World) -> Step | IORequest | None:
        """One original empty-CAS precedes every Ticket workspace and Worker."""
        if world.target is not None and job["phase"] != "workspace":
            return None
        originals = tx.rows(*run_statement(tx, "SELECT e.* FROM effects e JOIN subjects s ON s.run_id=e.run_id AND s.id=e.subject WHERE e.operation='run_branch' AND s.kind='ticket' AND (e.status NOT IN ('retired','cancelled') OR e.attempts>0) AND e.run_id=? ORDER BY e.rowid DESC LIMIT 1",
                        (), run_last=True))
        if originals and originals[0]["status"] in {"prepared", "sent", "observed"}:
            return Step("waiting", job["id"], "run_branch_initializing")
        if world.target is not None:
            return None
        targets = tx.rows(*run_statement(tx, 'SELECT * FROM final_target WHERE run_id=?',
                      (), run_last=True))
        if (job["kind"] != DeliveryKind.TICKET or not targets or job["target_ref"] != targets[0]["source_ref"]
                or world.source is not None or world.pr is not None or originals
                or tx.rows(*run_statement(tx, "SELECT id FROM subjects WHERE (phase!='workspace' OR generation!=1) AND run_id=? LIMIT 1",
                       (), run_last=True))
                or tx.rows(*run_statement(tx, 'SELECT id FROM attempts WHERE run_id=? LIMIT 1',
                       (), run_last=True))
                or tx.rows(*run_statement(tx, "SELECT id FROM effects WHERE operation!='run_branch' AND run_id=? LIMIT 1",
                       (), run_last=True))):
            self._gate(tx, job["id"], "target_branch_missing")
            return Step("paused", job["id"], "target_branch_missing")
        if not self._current(tx, job, run, world):
            return Step("paused", job["id"], "currentness")
        return self._prepare_effect(tx, run, job, work, world, "run_branch",
                                    before=job["base_sha"], after=job["base_sha"])

    @staticmethod
    def _effect_base_current(job: sqlite3.Row, row: sqlite3.Row, world: World) -> bool:
        if row["operation"] == "run_branch" and job["kind"] == DeliveryKind.TICKET:
            return world.target is None and world.source is None and world.pr is None
        return world.target is not None and row["base_sha"] == world.target.sha

    @staticmethod
    def _policy_values(policy: Policy) -> dict[str, Any]:
        values = asdict(policy)
        for role in Role:
            del values[role.value + "_profile"]
        return values

    def _policy(self, run: sqlite3.Row) -> Policy:
        return self._decode_policy(run["policy"])

    def _decode_policy(self, raw: bytes) -> Policy:
        values = json.loads(raw)
        if isinstance(values, dict):
            # Existing current-protocol Runs froze the former 45-minute default
            # implicitly. Do not read personal defaults or change their waits.
            values.setdefault("required_checks_wait_seconds", Policy().required_checks_wait_seconds)
        if not isinstance(values, dict) or values.keys() != self._policy_values(Policy()).keys():
            raise KernelError("policy_snapshot_invalid")
        return Policy(**values)

    def _subject_policy(self, tx: Transaction, job: sqlite3.Row) -> Policy:
        if job["policy_id"] is None:
            return self._policy(tx.one("SELECT * FROM runs WHERE id=?", (job["run_id"],)))
        return self._decode_policy(tx.get(job["policy_id"], job["id"]))

    def _store_profiles(self, tx: Transaction, subject: str, profiles: Mapping[str, Any], source: Mapping[str, Any]) -> int:
        revision = tx.one(*run_statement(tx, 'SELECT coalesce(max(id),0)+1 FROM profile_revisions WHERE run_id=?',
                       (), run_last=True))[0]
        material = tx.put(subject, "profile_revision", wire_json({**profiles, "source": dict(source)}))
        tx.execute(*run_statement(tx, 'INSERT INTO profile_revisions(id,subject,material_id,created_at,run_id) VALUES (?,?,?,?,?)',
            (revision, subject, material, self.clock()), run_last=True))
        return int(revision)

    def _profile_document(self, tx: Transaction, revision: int) -> dict[str, Any]:
        row = tx.one(*run_statement(tx, 'SELECT * FROM profile_revisions WHERE id=? AND run_id=?',
                  (revision,), run_last=True))
        return dict(json.loads(tx.get(row["material_id"], row["subject"])))

    def _thread_profile(self, tx: Transaction, thread: sqlite3.Row) -> tuple[Profile, str | None]:
        value = self._profile_document(tx, thread["profile_revision"])["profiles"][thread["role"]]
        return Profile(value["model"], value["reasoning_effort"]), value["reference"]

    @staticmethod
    def _thread_role_matches(job: sqlite3.Row, role: Role, thread: sqlite3.Row) -> bool:
        return bool(thread["subject"] == job["id"] and (thread["role"] == role.value
                    or role == Role.PUBLICATION and job["kind"] != DeliveryKind.FINAL_RUN
                    and thread["role"] == Role.DEVELOPMENT))

    def _publication_thread(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> str | None:
        """Use an existing Publication context, then bounded Development context.

        A shared backend has one immutable binding. Its semantic Publication
        Attempt changes role, never the Development binding's profile.
        """
        if job["kind"] == DeliveryKind.FINAL_RUN:
            return None
        previous = tx.rows(*run_statement(tx, "SELECT a.thread_id AS thread FROM attempts a JOIN threads t ON t.run_id=a.run_id AND t.id=a.thread_id WHERE a.subject=? AND a.generation=? AND a.role='publication' AND t.role='publication' AND t.backend_id IS NOT NULL AND a.run_id=? ORDER BY a.ordinal DESC LIMIT 1",
                       (job["id"], job["generation"]), run_last=True))
        if previous:
            return str(previous[0][0])
        publication = self._profile_document(tx, run["profile_revision"])["profiles"]["publication"]
        attempts = tx.one(*run_statement(tx, "SELECT count(*) FROM attempts WHERE subject=? AND generation=? AND role='publication' AND run_id=?",
                       (job["id"], job["generation"]), run_last=True))[0]
        # The original selection observes the newly allocated role ordinal.
        if publication["reference"] == "development" and attempts + 1 < 4:
            development_thread = job["development_thread"]
            return str(development_thread) if development_thread is not None else None
        return None

    def configure_task_profiles(self, action_id: str, *, preset: str | None = None,
                                overrides: ProfileOverrides | None = None) -> int:
        """Configure future Threads only under this exact published Task's lock."""
        with self.storage.transaction(write=False) as tx:
            run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                      (), run_last=True))
            run_id, task = run["id"], self._task(run)
        with self._task_admission(task):
            with self.storage.transaction(ordered=False) as tx:
                self._require_current_task(tx, task, run_id)
                return self._configure_profiles_in(tx, action_id, preset=preset, overrides=overrides)

    def configure_profiles(self, action_id: str, *, preset: str | None = None, overrides: ProfileOverrides | None = None) -> int:
        """An explicit immutable revision applies only when a new Thread is allocated."""
        with self.storage.transaction(ordered=False) as tx:
            return self._configure_profiles_in(tx, action_id, preset=preset, overrides=overrides)

    def _configure_profiles_in(self, tx: Transaction, action_id: str, *, preset: str | None = None,
                               overrides: ProfileOverrides | None = None) -> int:
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                  (), run_last=True))
        owner = tx.one(*run_statement(tx, 'SELECT subject FROM profile_revisions WHERE id=? AND run_id=?',
                    (run["profile_revision"],), run_last=True))[0]
        payload = wire_json({"kind": "configure_profiles", "preset": preset, "overrides": dict(overrides or {})})
        existing = tx.rows(*run_statement(tx, 'SELECT * FROM actions WHERE id=? AND run_id=?',
                       (action_id,), run_last=True))
        if existing:
            if tx.get(existing[0]["payload_id"], owner) != payload:
                raise KernelError("action_identity_changed")
            return int(json.loads(tx.get(existing[0]["receipt_id"], owner))["profile_revision"])
        profiles = resolve_profiles(preset=preset, current=self._profile_document(tx, run["profile_revision"]), overrides=overrides)
        revision = self._store_profiles(tx, owner, profiles, {"operation": "configure", "action": action_id})
        material = tx.put(owner, "control_intent", payload)
        receipt = tx.put(owner, "control_receipt", wire_json({"profile_revision": revision}))
        tx.execute("INSERT INTO actions(id,run_id,kind,payload_id,status,receipt_id) VALUES (?,?,'configure_profiles',?,'applied',?)",
                   (action_id, run["id"], material, receipt))
        tx.execute(*run_statement(tx, 'UPDATE runs SET profile_revision=? WHERE id=?',
            (revision,), run_last=True))
        self._event(tx, owner, "profiles_configured", action_id, receipt)
        return revision

    def _inputs(self, tx: Transaction, run: sqlite3.Row) -> dict[str, Any]:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id:
            raise KernelError("creation_input_binding")
        rows = tx.rows("SELECT * FROM run_inputs WHERE run_id=?", (run["id"],))
        return {} if not rows else dict(json.loads(tx.get(rows[0]["material_id"], rows[0]["subject"])))

    def _budget(self, tx: Transaction, job: sqlite3.Row) -> sqlite3.Row:
        return tx.one(*run_statement(tx, 'SELECT * FROM budgets WHERE subject=? AND purpose=? AND current=1 AND run_id=?',
                   (job["id"], job["purpose"]), run_last=True))

    def _ready(self, tx: Transaction, subject: str, table: str) -> None:
        if table not in {"work_queue", "integration_queue"}:
            raise AssertionError(table)
        if not tx.rows(*run_statement(tx, f'SELECT ready FROM {table} WHERE subject=? AND run_id=?',
                   (subject,), run_last=True)):
            tx.execute(*run_statement(tx, 'UPDATE runs SET next_ready=next_ready+1 WHERE id=?',
                (), run_last=True))
            ready = tx.one(*run_statement(tx, 'SELECT next_ready FROM runs WHERE id=?',
                        (), run_last=True))[0]
            tx.execute(*run_statement(tx, f'INSERT INTO {table}(subject,ready,run_id) VALUES (?,?,?)',
                (subject, ready), run_last=True))

    def _allocate(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, work: Work,
                  world: World, pending: sqlite3.Row | None = None,
                  *, continuation: ContinuationFacts | None = None, recovery_kind: str | None = None,
                  output_error: str | None = None, retain_period: bool = False,
                  retain_deadline: bool = False) -> Step | IORequest:
        execution, allow_agent = work.execution, work.allow_agent
        selected = transaction_scope(tx)
        if (run["id"] != selected.run_id or job["run_id"] != selected.run_id
                or pending is not None and (pending["run_id"] != selected.run_id
                                           or pending["subject"] != job["id"])):
            raise KernelError("allocation_binding")
        self._executor_owned(tx, run, execution)
        subject = job["id"]
        if self._agent_seats(tx, subject):
            return Step("waiting", subject, "previous_invocation_exit")
        if not allow_agent:
            self._ready(tx, subject, "work_queue")
            return Step("needs_agent", subject)
        role = (Role(pending["role"]) if pending is not None else
                {"development": Role.DEVELOPMENT, "review": Role.REVIEW, "publish": Role.PUBLICATION}[job["phase"]])
        budget = self._budget(tx, job)
        if pending is None and role == Role.DEVELOPMENT and budget["developments_used"] >= budget["development_limit"]:
            reason = ("ci_development_budget_exhausted" if job["purpose"] == Purpose.CI
                      else "review_budget_exhausted" if job["kind"] != DeliveryKind.TICKET
                      and budget["review_limit"] is not None and budget["reviews_used"] >= budget["review_limit"]
                      else "modification_budget_exhausted")
            self._gate(tx, subject, reason, purpose=job["purpose"])
            return Step("paused", subject, "development_budget")
        if pending is None and role == Role.REVIEW and budget["review_limit"] is not None and budget["reviews_used"] >= budget["review_limit"]:
            self._gate(tx, subject, "review_budget_exhausted", purpose=job["purpose"])
            return Step("paused", subject, "review_budget")
        self._ready(tx, subject, "work_queue")
        first = tx.one(*run_statement(tx, 'SELECT subject FROM work_queue WHERE run_id=? ORDER BY ready LIMIT 1',
                    (), run_last=True))[0]
        count = self._agent_seats(tx)
        if first != subject or count >= run["concurrency"]:
            return Step("waiting", subject, "agent_capacity")
        if role == Role.REVIEW and not job["validation_checkout"]:
            return self._prepare_effect(tx, run, job, work, world, "validation_checkout", before=job["workspace_sha"],
                                        payload={"reviewed_commit": job["integrated_sha"] or job["candidate_sha"]})
        tx.execute(*run_statement(tx, 'DELETE FROM work_queue WHERE subject=? AND run_id=?',
            (subject,), run_last=True))
        policy = self._subject_policy(tx, job)
        force_new_thread = False
        bound_feedback: list[str] = []
        feedback_actions = [] if retain_period else tx.all(*run_statement(tx, "SELECT a.* FROM history p JOIN actions a ON a.run_id=p.run_id AND a.id=p.identity WHERE p.kind='run_feedback_pending' AND a.run_id=? AND NOT EXISTS (SELECT 1 FROM history b WHERE b.run_id=a.run_id AND b.kind IN ('run_feedback_bound','run_feedback_retired') AND b.identity=a.id) AND p.run_id=? ORDER BY p.position", (run["id"],), run_last=True))
        live_generations = {(row["id"], row["generation"]) for row in tx.all(*run_statement(tx, 'SELECT id,generation FROM subjects WHERE run_id=?',
                                                                          (), run_last=True))} if feedback_actions else set()
        for action in feedback_actions:
            owner = tx.one(*run_statement(tx, 'SELECT subject FROM material WHERE id=? AND run_id=?',
                        (action["payload_id"],), run_last=True))[0]
            feedback = json.loads(tx.get(action["payload_id"], owner))
            receipt = json.loads(tx.get(action["receipt_id"], owner))
            scope = receipt["feedback_scope"].get(subject)
            if scope is not None and scope["generation"] == job["generation"]:
                if feedback["message"] is not None and action["subject"] is None:
                    response = tx.put(subject, "human_response", feedback["message"].encode())
                    self._event(tx, subject, "human_response", action["id"], response)
                force_new_thread = force_new_thread or bool(feedback["new_thread"] and (
                    (pending is not None and pending["id"] == scope["attempt"])
                    or (pending is None and scope["attempt"] is None and scope["role"] == job["phase"])))
                if pending is not None and force_new_thread:
                    tx.execute(*run_statement(tx, 'UPDATE attempts SET new_thread=1 WHERE id=? AND run_id=?',
                        (pending["id"],), run_last=True))
                    pending = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                                  (pending["id"],), run_last=True))
                bound_feedback.append(action["id"])
            elif not any((key, value["generation"]) in live_generations for key, value in receipt["feedback_scope"].items()):
                self._event(tx, owner, "run_feedback_retired", action["id"], action["receipt_id"])
        attempt_id = pending["id"] if pending is not None else identity()
        facts = None if pending is None else continuation or load_continuation(tx, pending["id"])
        previous = None if facts is None else facts.prior
        # A semantic successor and its automatic recoveries share one absolute
        # clock, but only format repair reuses the previous Invocation/output.
        inherited = (retain_period or retain_deadline or facts is not None
                     and facts.semantic_deadline is not None and pending is not None
                     and pending["resume_action"] is None)
        if inherited and previous is None:
            raise KernelError("continuation_deadline_missing")
        if inherited and previous is not None and self.clock() >= previous.deadline_at:
            self._gate(tx, subject, "execution_failed", evidence=previous.start_evidence or previous.raw_id)
            return Step("paused", subject, "invocation_deadline_exceeded")
        thread_id = None
        if pending is not None and not pending["new_thread"]:
            assert facts is not None
            thread_id = facts.thread_id
        elif pending is None and role == Role.DEVELOPMENT and not policy.new_thread_per_development and not force_new_thread:
            thread_id = job["development_thread"]
        elif pending is None and role == Role.PUBLICATION and not force_new_thread:
            thread_id = self._publication_thread(tx, run, job)
        if thread_id is None:
            thread_id = identity()
            tx.execute(*run_statement(tx, 'INSERT INTO threads(id,subject,role,profile_revision,run_id) VALUES (?,?,?,?,?)',
                (thread_id, subject, role.value, run["profile_revision"]), run_last=True))
        binding = tx.one(*run_statement(tx, 'SELECT * FROM threads WHERE id=? AND run_id=?',
                      (thread_id,), run_last=True))
        if not self._thread_role_matches(job, role, binding):
            raise KernelError("thread_role_binding")
        profile, reference = self._thread_profile(tx, binding)
        if role == Role.DEVELOPMENT:
            tx.execute(*run_statement(tx, 'UPDATE subjects SET development_thread=? WHERE id=? AND run_id=?',
                (thread_id, subject), run_last=True))
        context = self._context(tx, run, job, world, role) if pending is None else {}
        if pending is None:
            ordinal = tx.one(*run_statement(tx, 'SELECT coalesce(max(ordinal),0)+1 FROM attempts WHERE subject=? AND run_id=?',
                          (subject,), run_last=True))[0]
            context_id = tx.put(subject, "role_context", wire_json(context))
            tx.execute(*run_statement(tx, "INSERT INTO attempts(id,subject,generation,ordinal,role,purpose,window,parent_revision,graph_revision,revision,base_sha,candidate_sha,candidate_tree,expected_tree,completion_revision,context_id,status,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'allocated',?)",
                (attempt_id, subject, job["generation"], ordinal, role.value, job["purpose"], budget["window"],
                        run["parent_revision"], run["graph_revision"], job["revision"], context["base_sha"],
                        job["candidate_sha"], job["candidate_tree"], job["expected_tree"],
                        job["completion_revision"], context_id), run_last=True))
        else:
            # The Attempt owns its complete input; physical outputs are history.
            assert facts is not None
            context = dict(facts.context)
            if role == Role.REVIEW:
                context["checkout"] = job["validation_checkout"]
            if pending["status"] == "continuing" and pending["outcome"] in {"human_blocker", "blocked"}:
                if previous is None or previous.raw_id is None:
                    raise KernelError("continuation_return_missing")
                blocked = artifact(role, tx.get(previous.raw_id, subject))
                context["prior_human_blockers"] = (blocked["human_blockers"] if role != Role.REVIEW else
                    [lane["evidence"] for lane in blocked["checks"].values() if lane["status"] == "blocked"])
        if output_error is not None:
            context.update(readonly=True, contract_error=output_error[:2000])
        context["semantic_continuation"] = pending is not None
        if not retain_period:
            if pending is None:
                responses = tx.all(*run_statement(tx, "SELECT material_id FROM history WHERE subject=? AND generation=? AND kind='human_response' AND run_id=? ORDER BY position",
                                (subject, job["generation"]), run_last=True))
                if responses:
                    context["human_responses"] = [tx.get(row[0], subject).decode() for row in responses]
            else:
                actions = [*bound_feedback, *([] if pending["resume_action"] is None else [pending["resume_action"]])]
                responses = [row for row in tx.all(*run_statement(tx, "SELECT identity,material_id FROM history WHERE subject=? AND generation=? AND kind='human_response' AND run_id=? ORDER BY position",
                                                (subject, job["generation"]), run_last=True)) if row["identity"] in actions]
                if responses:
                    context["human_responses"] = [*context.get("human_responses", []), *[tx.get(row["material_id"], subject).decode() for row in responses]]
        context_id = tx.put(subject, "role_context", wire_json(context))
        backend_thread = binding["backend_id"]
        mode = "fresh" if backend_thread is None else "resume"
        ordinal = 0 if facts is None else facts.output_ordinal
        if output_error is not None:
            ordinal += 1
        if ordinal and backend_thread is not None:
            mode = "output-repair"
        duration = (previous.deadline_seconds if previous is not None else
                    {Role.DEVELOPMENT: policy.development_deadline_seconds, Role.REVIEW: policy.review_deadline_seconds,
                     Role.PUBLICATION: policy.publication_deadline_seconds}[role])
        if pending is None:
            bind_attempt_continuation(tx, attempt_id, AttemptContinuationBasis(thread_id, context_id))
        owned_period = bool(retain_period and previous is not None and previous.invocation_id is not None
                            and tx.rows(*run_statement(tx, 'SELECT id FROM invocations WHERE id=? AND executor=? AND epoch=? AND thread=? AND run_id=?',
                                    (previous.invocation_id, execution.owner, execution.epoch, thread_id), run_last=True)))
        started = self.clock()
        deadline = min(started + AGENT_PREPARATION_SECONDS, previous.deadline_at) if inherited and previous is not None else started + AGENT_PREPARATION_SECONDS
        preparation = AgentPreparation(identity(), attempt_id, subject, role, Purpose(job["purpose"]), thread_id,
            execution, context, profile, duration, deadline, mode, backend_thread, ordinal,
            binding["profile_revision"], reference)
        data = asdict(preparation)
        for key in ("context", "profile", "preparation_deadline"):
            del data[key]
        data.update(context_id=context_id, previous_invocation=None if previous is None else previous.invocation_id,
                    retained_deadline=previous.deadline_at if inherited and previous is not None else None,
                    owned_period=owned_period, recovery_kind=recovery_kind,
                    resume_action=None if pending is None else pending["resume_action"],
                    new_thread=False if pending is None else bool(pending["new_thread"]))
        scope = tx.put(subject, "agent_preparation", wire_json(data))
        tx.execute(*run_statement(tx, "INSERT INTO waits(subject,kind,scope_id,started_at,deadline,run_id) VALUES (?,'agent_preparation',?,?,?,?)",
            (subject, scope, started, deadline), run_last=True))
        tx.execute(*run_statement(tx, 'UPDATE subjects SET current_attempt=?,version=version+1 WHERE id=? AND run_id=?',
            (attempt_id, subject), run_last=True))
        for action_id in bound_feedback:
            self._event(tx, subject, "run_feedback_bound", action_id, context_id)
        return Step("preparing", subject, preparation=preparation)

    @staticmethod
    def _agent_seats(tx: Transaction, subject: str | None = None) -> int:
        return int(tx.one(*run_statement(tx, "WITH selected_run(id) AS (VALUES(?)) SELECT (SELECT count(*) FROM leases WHERE (? IS NULL OR subject=?) AND run_id=(SELECT id FROM selected_run))+(SELECT count(*) FROM waits WHERE kind='agent_preparation' AND (? IS NULL OR subject=?) AND run_id=(SELECT id FROM selected_run))",
                       (subject, subject, subject, subject)))[0])

    def _preparation(self, tx: Transaction, row: sqlite3.Row | Mapping[str, Any]) -> tuple[AgentPreparation, dict[str, Any]]:
        data = json.loads(tx.get(row["scope_id"], row["subject"]))
        selected = transaction_scope(tx)
        if not isinstance(data.get('execution'), dict) or data['execution'].get('run_id') != selected.run_id:
            raise KernelError("preparation_binding")
        thread = tx.one(*run_statement(tx, 'SELECT * FROM threads WHERE id=? AND run_id=?',
                     (data["thread_id"],), run_last=True))
        profile, reference = self._thread_profile(tx, thread)
        if (thread["profile_revision"], reference) != (data["profile_revision"], data["profile_reference"]):
            raise KernelError("preparation_binding")
        request = {key: value for key, value in data.items() if key in AgentPreparation.__dataclass_fields__}
        request.update(role=Role(data["role"]), purpose=Purpose(data["purpose"]), execution=Execution(**data["execution"]),
                       context=json.loads(tx.get(data["context_id"], row["subject"])), profile=profile,
                       preparation_deadline=row["deadline"])
        return AgentPreparation(**request), data

    def _preparation_record(self, tx: Transaction, preparation: AgentPreparation) -> tuple[sqlite3.Row, dict[str, Any]]:
        for row in tx.rows(*run_statement(tx, "SELECT * FROM waits WHERE subject=? AND kind='agent_preparation' AND run_id=?",
                       (preparation.subject,), run_last=True)):
            stored, data = self._preparation(tx, row)
            if stored.reservation_id == preparation.reservation_id:
                if stored != preparation:
                    raise KernelError("preparation_binding")
                return row, data
        raise KernelError("preparation_binding")

    def guard_preparation(self, preparation: AgentPreparation) -> None:
        with self.storage.transaction(write=False) as tx:
            self._preparation_authority(tx, preparation)

    def _preparation_authority(self, tx: Transaction, preparation: AgentPreparation) -> tuple[sqlite3.Row, dict[str, Any]]:
        row, data = self._preparation_record(tx, preparation)
        self._agent_subject_authority(tx, preparation.subject, preparation.attempt_id, preparation.execution)
        attempt = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                      (preparation.attempt_id,), run_last=True))
        if (attempt["current_invocation"] != data["previous_invocation"]
                or attempt["resume_action"] != data["resume_action"] or bool(attempt["new_thread"]) != data["new_thread"]):
            raise KernelError("preparation_binding")
        if self.clock() >= row["deadline"]:
            raise KernelError("invocation_deadline_exceeded" if data["retained_deadline"] is not None
                              and self.clock() >= data["retained_deadline"] else "preparation_deadline_exceeded")
        return row, data

    def admit_agent(self, preparation: AgentPreparation) -> Step:
        """Credentials are ready; transfer the same work seat, never select work."""
        with self.storage.transaction() as tx:
            row, data = self._preparation_authority(tx, preparation)
            attempt_id, subject, execution = preparation.attempt_id, preparation.subject, preparation.execution
            now = self.clock()
            invocation_id = data["previous_invocation"] if data["owned_period"] else identity()
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
                (row["scope_id"],), run_last=True))
            tx.execute(*run_statement(tx, "UPDATE attempts SET current_invocation=?,thread_id=?,context_id=?,result_id=NULL,status='running',new_thread=0,resume_action=NULL WHERE id=? AND run_id=?",
                (invocation_id, preparation.thread_id, data["context_id"], attempt_id), run_last=True))
            self._event(tx, subject, "output_repair_allocated" if data["owned_period"] else "agent_allocated",
                        f"{invocation_id}:{preparation.output_ordinal}" if data["owned_period"] else invocation_id,
                        data["context_id"] if data["owned_period"] else None)
            call = AgentCall(attempt_id, invocation_id, subject, preparation.role, preparation.purpose,
                preparation.thread_id, preparation.reservation_id, execution, preparation.context, preparation.profile,
                preparation.deadline_seconds, deadline, preparation.mode, preparation.reported_thread,
                preparation.output_ordinal, preparation.profile_revision, preparation.profile_reference)
            return Step("agent", subject, call=call)

    def preparation_failed(self, preparation: AgentPreparation, code: str, evidence: Mapping[str, Any]) -> None:
        """Caller has closed owned preparation resources; never settle a real lease."""
        with self.storage.transaction() as tx:
            rows = tx.rows(*run_statement(tx, "SELECT * FROM waits WHERE subject=? AND kind='agent_preparation' AND run_id=?",
                       (preparation.subject,), run_last=True))
            if not any(self._preparation(tx, row)[0].reservation_id == preparation.reservation_id for row in rows):
                return
            row, data = self._preparation_record(tx, preparation)
            if data["retained_deadline"] is not None and self.clock() >= data["retained_deadline"]:
                code = "invocation_deadline_exceeded"
            self._finish_preparation(tx, preparation, row, code, evidence)

    def _finish_preparation(self, tx: Transaction, preparation: AgentPreparation, row: sqlite3.Row,
                            code: str, evidence: Mapping[str, Any]) -> None:
        material = tx.put(preparation.subject, "agent_preparation_exit", wire_json({"preparation_scope": row["scope_id"],
            "code": code, "evidence": dict(evidence), "started_at": row["started_at"], "deadline": row["deadline"]}))
        tx.execute(*run_statement(tx, "DELETE FROM waits WHERE scope_id=? AND kind='agent_preparation' AND run_id=?",
            (row["scope_id"],), run_last=True))
        self._event(tx, preparation.subject, "agent_preparation_settled", preparation.reservation_id, material)
        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                  (), run_last=True))
        job = self._job(tx, preparation.subject)
        if (not run["stopped"] and not run["blocked"] and run["executor"] == preparation.execution.owner
                and run["epoch"] == preparation.execution.epoch and job["current_attempt"] == preparation.attempt_id
                and not self._gates(tx, preparation.subject)):
            self._gate(tx, preparation.subject, "execution_failed" if code == "invocation_deadline_exceeded" else "worker_unavailable", evidence=material)

    def _host_preparations_exited(self, tx: Transaction, execution: Execution, evidence: Mapping[str, Any]) -> None:
        for row in tx.all(*run_statement(tx, "SELECT * FROM waits WHERE kind='agent_preparation' AND run_id=?",
                       (), run_last=True)):
            preparation, _ = self._preparation(tx, row)
            if preparation.execution == execution:
                self._finish_preparation(tx, preparation, row, "executor_exited", evidence)


    def _recover_agent(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, attempt: sqlite3.Row,
                       world: World, work: Work, facts: ContinuationFacts) -> Step | IORequest:
        previous = facts.prior
        assert previous is not None
        subject, kind = job["id"], previous.failure_kind
        if tx.rows(*run_statement(tx, 'SELECT id FROM leases WHERE subject=? AND run_id=?',
               (subject,), run_last=True)):
            return Step("waiting", subject, "previous_invocation_exit")
        backend = tx.one(*run_statement(tx, 'SELECT backend_id FROM threads WHERE id=? AND run_id=?',
                      (facts.thread_id,), run_last=True))[0]
        if (previous.dispatch_state != "exited" or previous.process_identity is None or backend is None
                or kind not in {"ordinary", "capacity"} or (kind == "ordinary" and facts.ordinary_used)):
            self._gate(tx, subject, "execution_failed", evidence=previous.start_evidence)
            return Step("paused", subject, "execution_recovery_unavailable")
        if facts.semantic_deadline is not None and self.clock() >= facts.semantic_deadline:
            self._gate(tx, subject, "execution_failed", evidence=previous.start_evidence)
            return Step("paused", subject, "invocation_deadline_exceeded")
        if kind == "capacity" and (previous.recovery_ready_at is None or self.clock() < previous.recovery_ready_at):
            return Step("waiting", subject, "capacity_recovery_wait")
        # Every recovery crosses the same current owner/requirements/eligibility
        # decision as a new dispatch, after the exact former process exited.
        if (not self._current(tx, job, run, world) or attempt["generation"] != job["generation"]
                or attempt["candidate_sha"] != job["candidate_sha"] or self._eligibility(tx, job) is not None):
            return Step("paused", subject, "recovery_currentness")
        return self._allocate(tx, run, job, work, world, attempt, continuation=facts, recovery_kind=kind)

    def _integration_failure(self, tx: Transaction, job: sqlite3.Row) -> tuple[sqlite3.Row, dict[str, Any]] | None:
        """The last actual integration failure, never a later Review/Git check."""
        rows = tx.rows(*run_statement(tx, "SELECT e.* FROM effects e JOIN material m ON m.run_id=e.run_id AND m.id=e.receipt_id WHERE e.subject=? AND e.generation=? AND e.operation='integrate' AND e.status='confirmed' AND json_extract(CAST(m.body AS TEXT),'$.disposition')='failure' AND e.run_id=? ORDER BY e.rowid DESC LIMIT 1",
                   (job["id"], job["generation"]), run_last=True))
        return None if not rows else (rows[0], json.loads(tx.get(rows[0]["receipt_id"], job["id"])))

    def _context(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World, role: Role) -> dict[str, Any]:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or job['run_id'] != selected.run_id:
            raise KernelError("allocation_binding")
        context: dict[str, Any] = {
            "repository": run["repository"], "parent_issue": run["parent_issue"], "issue": job["issue"],
            "language": run["language"], "repository_root": run["repository_root"],
            "scope": job["kind"], "generation": job["generation"], "role": role.value,
            "semantic_continuation": False,
            "acceptance_scope": "run" if job["kind"] == DeliveryKind.FINAL_RUN else job["kind"],
            "parent_issue_url": f"https://github.com/{run['repository']}/issues/{run['parent_issue']}",
            "purpose": job["purpose"], "revision": job["revision"], "parent_revision": run["parent_revision"],
            "repair_source": job["repair_source"],
            "graph_revision": run["graph_revision"], "completion_revision": job["completion_revision"],
            "base_sha": job["base_sha"] if job["purpose"] == Purpose.REQUIREMENTS else self._require_target(world).sha,
            "candidate_sha": job["candidate_sha"], "candidate_tree": job["candidate_tree"],
            "checkout": job["validation_checkout"] if role == Role.REVIEW else job["checkout"],
            "readonly": role != Role.DEVELOPMENT,
            "reviewed_commit": job["integrated_sha"] or job["candidate_sha"],
            "reviewed_tree": job["integrated_tree"] or job["candidate_tree"],
            "expected_tree": job["expected_tree"], "full_scope": job["kind"],
        }
        if job["kind"] == DeliveryKind.TICKET:
            context["task_issue_url"] = f"https://github.com/{run['repository']}/issues/{job['issue']}"
        inputs = self._inputs(tx, run)
        if inputs:
            context.update(_prompt_resources=inputs["prompt_resources"], github_identity=inputs["worker_identity"])
        if job["purpose"] == Purpose.CI:
            context["work_source"] = "ci"
            if job["ci_failure"]:
                context["ci_evidence"] = json.loads(tx.get(job["ci_failure"], job["id"]))
        revisions = tx.rows(*run_statement(tx, "SELECT material_id FROM history WHERE subject=? AND generation=? AND kind='revision_requested' AND run_id=? ORDER BY position DESC LIMIT 1",
                        (job["id"], job["generation"]), run_last=True))
        if revisions:
            context["human_feedback"] = tx.get(revisions[0][0], job["id"]).decode()
        if role == Role.DEVELOPMENT and job["repair_source"] not in {None, "human_revision"}:
            field = {"acceptance": "acceptance_artifact", "integration": "integration_evidence",
                     "git_integrity": "git_integrity_evidence", "required_checks": "ci_evidence"}[job["repair_source"]]
            material = job["ci_failure"] if job["repair_source"] == "required_checks" else job["last_failure"]
            context[field] = json.loads(tx.get(material, job["id"]))
        integration_failure = (self._integration_failure(tx, job) if job["purpose"] == Purpose.INTEGRATION
                               and (role == Role.REVIEW or role == Role.DEVELOPMENT and job["repair_source"] == "git_integrity") else None)
        if (role == Role.DEVELOPMENT and integration_failure is not None
                and integration_failure[1]["raw"].get("head") == job["workspace_sha"]):
            # A content-check repair still owns the original unfinished merge.
            # _mechanical checks its exact HEAD and original MERGE_HEAD again.
            context["integration_evidence"] = integration_failure[1]
        budget = self._budget(tx, job)
        if role in {Role.DEVELOPMENT, Role.REVIEW} and budget["review_limit"] is not None:
            context["review_budget_context"] = {
                "completed_review_attempts": budget["reviews_used"],
                "current_review_attempt": budget["reviews_used"] + 1,
                "remaining_review_attempts": max(0, budget["review_limit"] - budget["reviews_used"] - int(role == Role.REVIEW)),
            }
        seed = tx.rows(*run_statement(tx, "SELECT id FROM material WHERE subject=? AND purpose='fixture_evidence' AND run_id=?",
                   (job["id"],), run_last=True))
        if seed and role != Role.DEVELOPMENT:
            context["frozen_fixture_evidence"] = json.loads(tx.get(seed[0][0], job["id"]))
        if run["source_sequence"] is not None:
            numbers = [(run["parent_issue"], run["parent_revision"], "background" if job["kind"] == DeliveryKind.TICKET else "requirements")]
            if job["kind"] == DeliveryKind.TICKET:
                numbers.append((job["issue"], job["revision"], "requirements"))
            if job["kind"] == DeliveryKind.FINAL_RUN:
                numbers.extend((row["issue"], row["revision"], "completed_ticket") for row in tx.all(*run_statement(tx, "SELECT issue,revision FROM subjects WHERE kind='ticket' AND run_id=? ORDER BY issue",
                                                                                                  (), run_last=True)))
            context["requirements"] = []
            for number, revision, responsibility in numbers:
                requirement: dict[str, Any] = {"number": number, "revision": revision, "responsibility": responsibility,
                                               "url": f"https://github.com/{run['repository']}/issues/{number}"}
                if role != Role.DEVELOPMENT:
                    value = tx.one(*run_statement(tx, 'SELECT title,body FROM requirement_versions WHERE issue=? AND revision=? AND run_id=?',
                                (number, revision), run_last=True))
                    requirement.update({"title": value["title"], "body": value["body"]})
                context["requirements"].append(requirement)
            if job["kind"] == DeliveryKind.FINAL_RUN:
                barrier = tx.one(*run_statement(tx, 'SELECT evidence FROM final_barriers WHERE subject=? AND generation=? AND run_id=?',
                              (job["id"], job["generation"]), run_last=True))
                context["ticket_completions"] = json.loads(tx.get(barrier[0], job["id"]))
        previous_context = previous_artifact = None
        if job["review_attempt"]:
            previous = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                           (job["review_attempt"],), run_last=True))
            previous_context = json.loads(tx.get(previous["context_id"], job["id"]))
            previous_artifact = artifact(Role.REVIEW, tx.get(previous["result_id"], job["id"]))
        if role == Role.REVIEW:
            facts = {key: context[key] for key in (
                "acceptance_scope", "revision", "parent_revision", "graph_revision", "completion_revision",
                "requirements", "ticket_completions", "work_source", "ci_evidence", "human_feedback",
            ) if key in context}
            facts["current_review_identity"] = self._review_identity(context)
            review_base = (context["ci_evidence"]["head_sha"] if job["purpose"] == Purpose.CI
                           else context["base_sha"])
            if previous_context is not None and previous_artifact is not None:
                facts.update(previous_review_identity=self._review_identity(previous_context),
                             previous_acceptance_artifact=previous_artifact)
                # Findings define the next comparison, not a complete-baseline
                # heuristic. A new failed CI head starts a new repair scope.
                if (review_outcome(previous_artifact) == "fail"
                        and previous_context["purpose"] == job["purpose"]
                        and previous_context["generation"] == job["generation"]
                        and (job["purpose"] != Purpose.CI
                             or previous_context.get("ci_evidence", {}).get("head_sha") == review_base)):
                    review_base = previous_context["reviewed_commit"]
            developments = tx.rows(*run_statement(tx, "SELECT id,result_id,purpose FROM attempts WHERE subject=? AND generation=? AND role='development' AND status='consumed' AND run_id=? ORDER BY ordinal DESC LIMIT 1",
                               (job["id"], job["generation"]), run_last=True))
            if developments:
                latest = developments[0]
                facts.update(development_summary=artifact(Role.DEVELOPMENT, tx.get(latest["result_id"], job["id"]))["summary"],
                             development_identity={"attempt_id": latest["id"], "result_id": latest["result_id"],
                                                   "purpose": latest["purpose"]})
            if integration_failure is not None:
                failure, original = integration_failure
                development = (developments[0] if developments and developments[0]["purpose"] == Purpose.INTEGRATION
                               else tx.one(*run_statement(tx, "SELECT id,result_id FROM attempts WHERE subject=? AND generation=? AND purpose='integration' AND role='development' AND status='consumed' AND run_id=? ORDER BY ordinal DESC LIMIT 1",
                                        (job["id"], job["generation"]), run_last=True)))
                same_development = facts.get("development_identity", {}).get("attempt_id") == development["id"]
                summary = (facts.pop("development_summary") if same_development else
                           artifact(Role.DEVELOPMENT, tx.get(development["result_id"], job["id"]))["summary"])
                if same_development:
                    facts.pop("development_identity")
                facts["integration_record"] = {
                    "effect_id": failure["id"], "receipt_id": failure["receipt_id"], "subject": job["id"],
                    "generation": job["generation"], "source_ref": failure["source_ref"],
                    "target_ref": failure["target_ref"], "candidate_sha": failure["candidate_sha"],
                    "target_sha": failure["base_sha"], "receipt": original,
                    "development_attempt": development["id"], "development_result": development["result_id"],
                    "development_summary": summary,
                }
            if job["purpose"] == Purpose.CI and job["kind"] != DeliveryKind.TICKET:
                accepted = tx.rows(*run_statement(tx, "SELECT context_id,result_id FROM attempts WHERE subject=? AND generation=? AND role='review' AND outcome='pass' AND purpose!='ci' AND revision=? AND parent_revision=? AND graph_revision=? AND completion_revision=? AND run_id=? ORDER BY ordinal DESC LIMIT 1",
                               (job["id"], job["generation"], job["revision"], run["parent_revision"], run["graph_revision"], job["completion_revision"]), run_last=True))
                if accepted:
                    accepted_context = json.loads(tx.get(accepted[0]["context_id"], job["id"]))
                    facts["parent_acceptance_baseline"] = {
                        **self._review_identity(accepted_context),
                        "artifact": artifact(Role.REVIEW, tx.get(accepted[0]["result_id"], job["id"])),
                    }
            context["review_context"] = {"facts": facts, "review_range": {
                "base_sha": review_base, "candidate_sha": context["reviewed_commit"]}}
        elif role == Role.PUBLICATION:
            context["publication_context"] = {
                "commit_range": None if job["kind"] == DeliveryKind.FINAL_RUN else {
                    "base_sha": job["source_sha"], "head_sha": context["reviewed_commit"]},
                "pr_range": {"base_sha": self._require_target(world).sha, "head_sha": context["reviewed_commit"]},
                "existing_pr": None if world.pr is None else {
                    **asdict(world.pr), "url": f"https://github.com/{world.pr.repository}/pull/{world.pr.number}"},
            }
            if job["gate_mode"] == "review" and previous_context is not None and previous_artifact is not None:
                if review_outcome(previous_artifact) != "pass":
                    raise KernelError("publication_authority_missing")
                context.update(acceptance_artifact=previous_artifact,
                               acceptance_identity=self._review_identity(previous_context))
            elif job["kind"] == DeliveryKind.TICKET and job["gate_mode"] in {"deterministic_fallback", "ci_waiver"}:
                development = tx.one(*run_statement(tx, "SELECT result_id FROM attempts WHERE subject=? AND generation=? AND role='development' AND status='consumed' AND run_id=? ORDER BY ordinal DESC LIMIT 1",
                                  (job["id"], job["generation"]), run_last=True))
                publication_facts = {
                    "current_candidate_identity": {"base_sha": context["base_sha"], "candidate_sha": job["candidate_sha"],
                                                   "candidate_tree": job["candidate_tree"], "integrated_sha": job["integrated_sha"],
                                                   "integrated_tree": job["integrated_tree"]},
                    "current_candidate_has_additional_review": False,
                    "development_summary": artifact(Role.DEVELOPMENT, tx.get(development["result_id"], job["id"]))["summary"],
                }
                if job["gate_mode"] == "deterministic_fallback":
                    if previous_context is None or previous_artifact is None or review_outcome(previous_artifact) != "fail":
                        raise KernelError("publication_authority_missing")
                    publication_facts.update(last_review_identity=self._review_identity(previous_context),
                                             last_review_artifact=previous_artifact)
                    context["fallback_publication_context"] = publication_facts
                else:
                    publication_facts.update(failed_required_checks=json.loads(tx.get(job["ci_failure"], job["id"])),
                                             previous_published_head=job["publication_sha"])
                    context["ci_repair_publication_context"] = publication_facts
            else:
                raise KernelError("publication_authority_missing")
        return context

    @staticmethod
    def _review_identity(context: Mapping[str, Any]) -> dict[str, Any]:
        return {"reviewed_base_sha": context["base_sha"], "reviewed_candidate_sha": context["reviewed_commit"],
                "reviewed_candidate_tree": context["reviewed_tree"],
                **{key: context[key] for key in ("revision", "parent_revision", "graph_revision", "completion_revision")}}

    def _consume(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, attempt: sqlite3.Row,
                 world: World, work: Work) -> Step | IORequest:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or job['run_id'] != selected.run_id or attempt['run_id'] != selected.run_id or (attempt['subject'] != job['id']) or (work.execution.run_id != selected.run_id):
            raise KernelError("invocation_binding")
        subject = job["id"]
        if self._agent_seats(tx, subject):
            return Step("waiting", subject, "previous_invocation_exit")
        role = Role(attempt["role"])
        try:
            result = artifact(role, tx.get(attempt["result_id"], subject))
        except KernelError as error:
            return self._repair_output(tx, run, job, attempt, world, work, str(error))
        if role == Role.REVIEW and not attempt["accounted"]:
            self._charge_review(tx, attempt)
        if (not self._current(tx, job, run, world)
                or attempt["generation"] != job["generation"]
                or attempt["candidate_sha"] != job["candidate_sha"]):
            tx.execute(*run_statement(tx, "UPDATE attempts SET status='stale' WHERE id=? AND run_id=?",
                (attempt["id"],), run_last=True))
            self._gate(tx, subject, "stale_return", evidence=attempt["result_id"])
            return Step("paused", subject, "stale_return")
        outcome = review_outcome(result) if role == Role.REVIEW else result["result_kind"]
        if outcome in {"human_blocker", "blocked"}:
            if role == Role.REVIEW:
                self._retire_validation(tx, job)
            tx.execute(*run_statement(tx, "UPDATE attempts SET status='human',outcome=? WHERE id=? AND run_id=?",
                (outcome, attempt["id"]), run_last=True))
            self._gate(tx, subject, "human_blocker", evidence=attempt["result_id"])
            return Step("paused", subject, "human_blocker")
        if role == Role.DEVELOPMENT and outcome == "incomplete":
            return self._continue_incomplete(tx, run, job, attempt, world, work)
        if role == Role.DEVELOPMENT:
            phase = "candidate"
        elif role == Role.REVIEW:
            self._retire_validation(tx, job)
            tx.execute(*run_statement(tx, 'UPDATE subjects SET review_attempt=? WHERE id=? AND run_id=?',
                (attempt["id"], subject), run_last=True))
            if outcome == "fail":
                tx.execute(*run_statement(tx, 'UPDATE subjects SET last_failure=? WHERE id=? AND run_id=?',
                    (attempt["result_id"], subject), run_last=True))
                # Review Findings drive repair input; purpose retains the active budget.
                tx.execute(*run_statement(tx, "UPDATE subjects SET repair_source='acceptance' WHERE id=? AND run_id=?",
                    (subject,), run_last=True))
                phase = "development"
                self._release_integration(tx, subject)
            else:
                phase = "integration" if not job["integrated_sha"] else "ready_publication"
                tx.execute(*run_statement(tx, "UPDATE subjects SET gate_mode='review' WHERE id=? AND run_id=?",
                    (subject,), run_last=True))
        else:
            phase = "publication_commit"
            tx.execute(*run_statement(tx, 'UPDATE subjects SET publication_attempt=? WHERE id=? AND run_id=?',
                (attempt["id"], subject), run_last=True))
        tx.execute(*run_statement(tx, "UPDATE attempts SET status='consumed',outcome=? WHERE id=? AND run_id=?",
            (outcome, attempt["id"]), run_last=True))
        tx.execute(*run_statement(tx, 'UPDATE subjects SET current_attempt=NULL,phase=?,version=version+1 WHERE id=? AND run_id=?',
            (phase, subject), run_last=True))
        self._event(tx, subject, "role_consumed", attempt["id"], attempt["result_id"])
        return Step("progress", subject)

    def _continue_incomplete(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                             attempt: sqlite3.Row, world: World, work: Work) -> Step | IORequest:
        facts = load_continuation(tx, attempt["id"])
        prior = facts.prior
        backend = tx.one(*run_statement(tx, 'SELECT backend_id FROM threads WHERE id=? AND run_id=?',
                      (facts.thread_id,), run_last=True))[0]
        reason = None
        if (prior is None or prior.raw_id != attempt["result_id"] or prior.dispatch_state != "exited"
                or prior.exit_code != 0 or prior.process_identity is None or not backend):
            reason = "semantic_continuation_unavailable"
        elif attempt["resume_action"] is not None:
            # Explicit continuation uses existing control authority. An old
            # read-only formatting context stays read-only, never promoted.
            return self._allocate(tx, run, job, work, world, attempt, continuation=facts)
        elif facts.context.get("readonly") or facts.output_ordinal:
            reason = "semantic_incomplete_readonly"
        elif facts.semantic_used:
            reason = "semantic_incomplete_limit"
        elif self.clock() >= prior.deadline_at:
            reason = "invocation_deadline_exceeded"
        if reason is not None:
            evidence = tx.put(job["id"], "semantic_incomplete", wire_json({"reason": reason,
                "attempt": attempt["id"], "result_id": attempt["result_id"]}))
            self._gate(tx, job["id"], "execution_failed", evidence=evidence)
            return Step("paused", job["id"], reason)
        return self._allocate(tx, run, job, work, world, attempt, continuation=facts,
                              recovery_kind="semantic", retain_deadline=True)

    def _repair_output(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, attempt: sqlite3.Row,
                       world: World, work: Work, error: str) -> Step | IORequest:
        subject = job["id"]
        if (not self._current(tx, job, run, world) or attempt["generation"] != job["generation"]
                or attempt["candidate_sha"] != job["candidate_sha"]):
            self._gate(tx, subject, "stale_return", evidence=attempt["result_id"])
            return Step("paused", subject, "stale_return")
        if self._agent_seats(tx, subject):
            return Step("waiting", subject, "previous_invocation_exit")
        try:
            facts = load_continuation(tx, attempt["id"])
        except KernelError as missing:
            if missing.code != "continuation_basis_missing":
                raise
            return Step("paused", subject, missing.code)
        previous = facts.prior
        if previous is None:
            return Step("paused", subject, "continuation_exit_missing")
        backend = tx.one(*run_statement(tx, 'SELECT backend_id FROM threads WHERE id=? AND run_id=?',
                      (facts.thread_id,), run_last=True))[0]
        if (facts.semantic_deadline is not None and self.clock() >= facts.semantic_deadline
                and attempt["resume_action"] is None):
            self._gate(tx, subject, "execution_failed", evidence=previous.start_evidence)
            return Step("paused", subject, "invocation_deadline_exceeded")
        if previous.failure_kind == "capacity" and previous.recovery_ready_at is not None and self.clock() < previous.recovery_ready_at:
            return Step("waiting", subject, "capacity_recovery_wait")
        if attempt["resume_action"] is not None and previous.failure_kind is not None:
            return self._allocate(tx, run, job, work, world, attempt, continuation=facts)
        if previous.exit_code != 0 and previous.failure_kind is not None:
            return self._recover_agent(tx, run, job, attempt, world, work, facts)
        if (previous.dispatch_state != "exited" or previous.exit_code != 0
                or previous.process_identity is None or backend is None or facts.output_ordinal >= 2):
            self._gate(tx, subject, "invalid_artifact", evidence=attempt["result_id"])
            return Step("paused", subject, "invalid_artifact")
        if self.clock() >= previous.deadline_at:
            if attempt["resume_action"] is not None:
                return self._allocate(tx, run, job, work, world, attempt, continuation=facts, output_error=error)
            self._gate(tx, subject, "execution_failed", evidence=attempt["result_id"])
            return Step("paused", subject, "invocation_deadline_exceeded")
        return self._allocate(tx, run, job, work, world, attempt,
                              continuation=facts, output_error=error, retain_period=True)

    def _pending_effect(self, tx: Transaction, subject: str) -> sqlite3.Row | None:
        rows = tx.rows(*run_statement(tx, "SELECT * FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') AND operation NOT IN ('cleanup_worktree','cleanup_local_ref','cleanup_remote_ref','issue_link') AND run_id=?",
                   (subject,), run_last=True))
        return rows[0] if rows else None

    def _effect_stale(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row, *, read: _SubjectSnapshot | None = None) -> bool:
        if read is not None:
            read.require(tx, job)
        if run["source_sequence"] is None:
            return False
        if self._final_closing(tx, run, job) and row["operation"] in {"merge", "completion_comment", "close"}:
            return False
        facts = tx.rows(*run_statement(tx, 'SELECT revision FROM issue_facts WHERE issue=? AND run_id=?',
                    (job["issue"],), run_last=True))
        return bool(row["generation"] != job["generation"] or row["parent_revision"] != run["observed_parent_revision"]
                    or row["graph_revision"] != run["observed_graph_revision"] or job["refresh_revision"] is not None
                    or (job["kind"] == DeliveryKind.TICKET and facts and row["revision"] != facts[0][0])
                    or any(gate["code"] == "requeue_required" for gate in (self._gates(tx, job["id"]) if read is None else read.gates)))

    def _merge_receipt_matches(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row, result: dict[str, Any]) -> bool:
        pr, commit = result["pr"], result["commit"]
        payload = json.loads(tx.get(row["payload_id"], job["id"]))
        expected, actual = payload["commit_message"], result["raw"].get("commit_message")
        # Git/API may add one terminating LF to an unterminated message.
        # Body content, explicit newlines and ordinary merge policy stay exact.
        message_matches = isinstance(actual, str) and (actual == expected
                           or not expected.endswith("\n") and actual == expected + "\n")
        return bool(result["disposition"] == "target" and pr and commit and pr["number"] == row["pr_number"]
                    and self._pr_identity(run, row, pr)
                    and pr["state"] == "MERGED" and pr["actor"] == row["actor"] and pr["head"]["sha"] == row["after_sha"]
                    and pr["base"]["sha"] == row["before_sha"] and pr["merged"] == commit and commit["tree"] == job["publication_tree"]
                    and result["raw"].get("parents") == payload["parents"]
                    and (payload["method"] != "squash" or message_matches))

    def _final_merge_intent(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> sqlite3.Row | None:
        """The frozen grant survives the bounded lifetime of an Action observer."""
        if (job["kind"] != DeliveryKind.FINAL_RUN or not job["grant_id"]
                or run["observed_graph_revision"] not in {None, run["graph_revision"]}):
            return None
        grant = tx.one(*run_statement(tx, 'SELECT * FROM grants WHERE id=? AND subject=? AND run_id=?',
                    (job["grant_id"], job["id"]), run_last=True))
        intents = tx.rows(*run_statement(tx, "SELECT * FROM effects WHERE subject=? AND generation=? AND operation='merge' AND status IN ('sent','observed','confirmed') AND run_id=? ORDER BY rowid DESC LIMIT 1",
                      (job["id"], job["generation"]), run_last=True))
        if not intents:
            return None
        intent = intents[0]
        if not (grant["pr_number"] == intent["pr_number"] == job["pr_number"]
                and grant["head_sha"] == intent["after_sha"] == job["publication_sha"]
                and grant["base_sha"] == intent["before_sha"]
                and grant["review_attempt"] == job["review_attempt"] and grant["checks_id"] == job["checks_id"]
                and grant["revision"] == intent["revision"] == job["revision"]
                and grant["parent_revision"] == intent["parent_revision"] == run["parent_revision"]
                and grant["graph_revision"] == intent["graph_revision"] == run["graph_revision"]
                and grant["completion_revision"] == job["completion_revision"]
                and intent["source_ref"] == job["source_ref"] and intent["target_ref"] == job["target_ref"]):
            return None
        return intent

    def _final_closing(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> bool:
        intent = self._final_merge_intent(tx, run, job)
        return bool(intent is not None and intent["status"] in {"observed", "confirmed"} and intent["receipt_id"]
                    and self._merge_receipt_matches(tx, run, job, intent, json.loads(tx.get(intent["receipt_id"], job["id"]))))

    def _validate_target(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row, result: dict[str, Any]) -> None:
        if result["disposition"] != "target":
            return
        operation = row["operation"]
        payload = json.loads(tx.get(row["payload_id"], job["id"]))
        commit, pr = result["commit"], result["pr"]
        if operation == "abandon_worktree" and (result["raw"].get("resource") != payload["resource"]
                or result["raw"].get("reason") != "worktree_absent"):
            raise KernelError("foreign_resource_receipt")
        if operation == "reopen_ticket":
            raw = result["raw"]
            event = raw.get("reopened_event")
            original = payload["close_receipt"].get("raw", {}).get("event")
            if (raw.get("issue") != payload["issue"] or raw.get("state") != "OPEN"
                    or not isinstance(original, dict) or raw.get("prior_close") != original):
                raise KernelError("foreign_ticket_recovery_receipt")
            if (not isinstance(event, dict) or type(event.get("id")) is not int or event["id"] <= original["id"]
                    or event.get("event") != "reopened" or self._event_actor(event) != row["actor"]):
                raise KernelError("foreign_ticket_recovery_receipt")
        if operation == "recovery_comment":
            raw = result["raw"]
            reopened = payload["reopen_receipt"]["raw"]
            comment = raw.get("comment")
            if (raw.get("issue") != payload["issue"] or raw.get("reopen_effect") != payload["reopen_effect"]
                    or raw.get("prior_close") != reopened["prior_close"] or raw.get("reopened_event") != reopened["reopened_event"]
                    or not isinstance(comment, dict) or type(comment.get("id")) is not int or comment["id"] <= 0
                    or comment.get("actor") != row["actor"] or not isinstance(comment.get("body"), str)
                    or not comment["body"].endswith("\n" + payload["comment_body"])
                    or not isinstance(comment.get("created_at"), str) or not comment["created_at"]):
                raise KernelError("foreign_ticket_recovery_comment")
        if operation == "retire_pr":
            intent = payload.get("retirement_intent")
            receipt = result["raw"].get("close_intent")
            event = result["raw"].get("event")
            if (not isinstance(intent, dict) or not isinstance(receipt, dict) or not isinstance(event, dict)
                    or any(receipt.get(key) != value for key, value in intent.items())
                    or type(receipt.get("comment_id")) is not int or receipt["comment_id"] <= 0
                    or type(event.get("id")) is not int or event["id"] <= intent["baseline_event_id"]
                    or event.get("event") != "closed" or event.get("actor") != row["actor"]
                    or not pr or not self._pr_identity(run, row, pr) or pr["number"] != row["pr_number"]
                    or pr["state"] != "CLOSED" or pr["head"] != payload["prior_pr"]["head"]
                    or (pr["base"] != payload["prior_pr"]["base"] and (pr["base"]["sha"] == payload["prior_pr"]["base"]["sha"]
                        or result["raw"].get("base_ancestry") != [payload["prior_pr"]["base"]["sha"], pr["base"]["sha"]]))
                    or pr["actor"] != payload["prior_pr"]["actor"]):
                raise KernelError("foreign_retirement_receipt")
            try:
                commented = datetime.fromisoformat(receipt["comment_created_at"])
                closed = datetime.fromisoformat(event["created_at"])
                if commented.utcoffset() is None or closed.utcoffset() is None or closed < commented:
                    raise ValueError("retirement_event_precedes_intent")
            except (KeyError, TypeError, ValueError) as error:
                raise KernelError("foreign_retirement_receipt") from error
        if operation == "merge" and not self._merge_receipt_matches(tx, run, job, row, result):
            raise KernelError("foreign_merge_receipt")
        if operation == "pr_write" and (not pr or not self._pr_identity(run, row, pr)
                or pr["head"]["sha"] != row["after_sha"] or pr["state"] != "OPEN"
                or pr["title"] != payload["title"] or pr["body"] != payload["body"] or pr["actor"] != row["actor"]):
            raise KernelError("foreign_pr_receipt")
        if operation in {"workspace", "validation_checkout", "candidate", "integrate", "publication_commit", "merge", "push", "run_branch"} and not commit:
            raise KernelError("effect_receipt_incomplete", operation)
        if operation in {"push", "run_branch"} and commit["sha"] != row["after_sha"]:
            raise KernelError("write_target_changed")
        if operation == "run_branch" and job["kind"] == DeliveryKind.TICKET and commit["tree"] != row["base_tree"]:
            raise KernelError("write_target_changed")
        if operation == "publication_commit":
            expected = payload.get("reuse_sha") or payload.get("publication_commit_intent", {}).get("expected_sha")
            if not expected or commit["sha"] != expected or commit["tree"] != payload["integrated_tree"]:
                raise KernelError("publication_object_changed")
        if operation == "validation_checkout" and commit["sha"] != payload["reviewed_commit"]:
            raise KernelError("validation_commit_changed")
        if operation == "completion_comment":
            self._comment_close_intent(run, job, row, payload, result)

    def _comment_close_intent(self, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row,
                              payload: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        raw = result.get("raw")
        intent = raw.get("close_intent") if isinstance(raw, dict) else None
        binding = {"run_id": run["id"], "subject": job["id"], "issue": payload["issue"],
                   "merge_sha": row["before_sha"], "actor": row["actor"], "effect_id": row["id"],
                   "pr_number": row["pr_number"], "kind": payload["kind"]}
        if (not isinstance(intent, dict) or any(intent.get(key) != value for key, value in binding.items())
                or type(intent.get("baseline_event_id")) is not int or intent["baseline_event_id"] < 0
                or type(intent.get("comment_id")) is not int or intent["comment_id"] <= 0
                or not isinstance(intent.get("comment_created_at"), str) or not intent["comment_created_at"]
                or not isinstance(intent.get("comment_body"), str) or not intent["comment_body"]):
            raise KernelError("foreign_close_intent")
        return dict(intent)

    def _settle_stale_effect(self, tx: Transaction, job: sqlite3.Row, row: sqlite3.Row) -> Step:
        """Save the original outcome without granting any successor operation."""
        selected = transaction_scope(tx)
        if job['run_id'] != selected.run_id or row['run_id'] != selected.run_id or row['subject'] != job['id']:
            raise KernelError("effect_binding")
        result = json.loads(tx.get(row["receipt_id"], job["id"]))
        self._validate_target(tx, tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                      (), run_last=True)), job, row, result)
        commit = result["commit"]
        operation = row["operation"]
        if result["disposition"] == "target":
            if operation in {"candidate", "integrate"} and commit:
                tx.execute(*run_statement(tx, 'UPDATE subjects SET workspace_sha=? WHERE id=? AND run_id=?',
                    (commit["sha"], job["id"]), run_last=True))
            elif operation == "push" or operation == "run_branch" and job["kind"] == DeliveryKind.FINAL_RUN:
                tx.execute(*run_statement(tx, 'UPDATE subjects SET source_sha=? WHERE id=? AND run_id=?',
                    (row["after_sha"], job["id"]), run_last=True))
                self._record_source_write(tx, job["id"], row["source_ref"], row["after_sha"])
            elif operation == "publication_commit" and commit:
                tx.execute(*run_statement(tx, 'UPDATE subjects SET publication_sha=?,publication_tree=?,workspace_sha=? WHERE id=? AND run_id=?',
                    (commit["sha"], commit["tree"], commit["sha"], job["id"]), run_last=True))
            elif operation == "pr_write" and result["pr"]:
                tx.execute(*run_statement(tx, 'UPDATE subjects SET pr_number=? WHERE id=? AND run_id=?',
                    (result["pr"]["number"], job["id"]), run_last=True))
            elif operation == "merge" and commit:
                tx.execute(*run_statement(tx, 'UPDATE subjects SET merge_sha=? WHERE id=? AND run_id=?',
                    (commit["sha"], job["id"]), run_last=True))
            elif operation == "validation_checkout":
                payload = json.loads(tx.get(row["payload_id"], job["id"]))
                tx.execute(*run_statement(tx, 'UPDATE subjects SET validation_cleanup=? WHERE id=? AND run_id=?',
                    (payload["checkout"], job["id"]), run_last=True))
        tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
            (row["id"],), run_last=True))
        self._event(tx, job["id"], "stale_intent_settled", row["id"], row["receipt_id"])
        return Step("progress", job["id"], "original_outcome_retained")

    def _refresh_final(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World) -> Step:
        if job["kind"] != DeliveryKind.FINAL_RUN or not job["owned_root"]:
            raise KernelError("final_resource_identity")
        if world.source is None or world.parent_revision != job["refresh_revision"] or world.graph_revision != run["graph_revision"]:
            return Step("waiting", job["id"], "final_source_snapshot")
        if job["merge_sha"]:
            self._gate(tx, job["id"], "merged_requirements_changed")
            self._finish_final_failure(tx, run)
            return Step("paused", job["id"], "merged_requirements_changed")
        self._retire_generation(tx, job, "parent_requirements_changed")
        generation = job["generation"] + 1
        checkout = str(Path(job["owned_root"]) / f"final-{job['issue']}-g{generation}")
        tx.execute(*run_statement(tx, "UPDATE subjects SET generation=?,revision=?,phase='workspace',purpose='integration',checkout=?,base_sha=?,base_tree=?,source_sha=?,workspace_sha=?,candidate_sha=?,candidate_tree=?,integrated_sha=NULL,integrated_tree=NULL,expected_tree=NULL,integration_target=NULL,current_attempt=NULL,review_attempt=NULL,publication_attempt=NULL,checks_id=NULL,grant_id=NULL,gate_mode=NULL,development_thread=NULL,ci_failure=NULL,last_failure=NULL,resume_purpose=NULL,repair_source=NULL,validation_checkout=NULL,validation_cleanup=NULL,refresh_revision=NULL,version=version+1 WHERE id=? AND run_id=?",
            (generation, job["refresh_revision"], checkout, world.source.sha, world.source.tree, world.source.sha, world.source.sha,
                    world.source.sha, world.source.tree, job["id"]), run_last=True))
        # Requirement changes do not renew any allowance or refund prior work.
        prior = tx.one(*run_statement(tx, 'SELECT * FROM final_barriers WHERE subject=? AND generation=? AND run_id=?',
                    (job["id"], job["generation"]), run_last=True))
        tx.execute(*run_statement(tx, 'INSERT INTO final_barriers(subject,generation,completion_revision,run_sha,default_sha,evidence,run_id) VALUES (?,?,?,?,?,?,?)',
            (job["id"], generation, prior["completion_revision"], world.source.sha, self._require_target(world).sha, prior["evidence"]), run_last=True))
        tx.execute(*run_statement(tx, "DELETE FROM gates WHERE subject=? AND code!='operator_stopped' AND run_id=?",
            (job["id"],), run_last=True))
        tx.execute(*run_statement(tx, 'DELETE FROM work_queue WHERE subject=? AND run_id=?',
            (job["id"],), run_last=True))
        self._release_integration(tx, job["id"])
        self._finish_final_failure(tx, run)
        tx.execute(*run_statement(tx, "UPDATE runs SET parent_revision=?,blocked=NULL WHERE blocked='final_refresh' AND id=?",
            (job["refresh_revision"],), run_last=True))
        self._event(tx, job["id"], "final_requirements_invalidated", str(generation))
        return Step("progress", job["id"], "fresh_whole_run_review")

    def _retire_validation(self, tx: Transaction, job: sqlite3.Row) -> None:
        if job["validation_checkout"]:
            if job["validation_cleanup"]:
                raise KernelError("validation_cleanup_unsettled")
            tx.execute(*run_statement(tx, 'UPDATE subjects SET validation_cleanup=validation_checkout,validation_checkout=NULL WHERE id=? AND run_id=?',
                (job["id"],), run_last=True))

    def _effect(self, tx: Transaction, row: sqlite3.Row, execution: Execution) -> Effect:
        selected = transaction_scope(tx)
        if row['run_id'] != selected.run_id or execution.run_id != selected.run_id:
            raise KernelError("effect_binding")
        return Effect(row["id"], row["subject"], row["operation"],
                      tx.one(*run_statement(tx, 'SELECT repository FROM runs WHERE id=?',
                          (), run_last=True))[0], row["source_ref"], row["target_ref"],
                      row["before_sha"], row["after_sha"],
                      None if row["candidate_sha"] is None else Commit(row["candidate_sha"], row["candidate_tree"]),
                      Commit(row["base_sha"], row["base_tree"]), row["pr_number"], row["actor"],
                      json.loads(tx.get(row["payload_id"], row["subject"])), execution,
                      row["attempts"] + (1 if row["status"] == "prepared" else 0))

    def _mechanical(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, work: Work, world: World) -> Step | IORequest:
        subject, phase = job["id"], job["phase"]
        target = self._require_target(world)
        if phase == "workspace":
            payload: dict[str, Any] = {"checkout": job["checkout"], "create_ref": job["kind"] != DeliveryKind.FINAL_RUN}
            originals = tx.rows(*run_statement(tx, "SELECT payload_id FROM effects WHERE subject=? AND generation=? AND operation='workspace' AND run_id=? ORDER BY rowid LIMIT 1",
                            (subject, job["generation"]), run_last=True))
            if (job["kind"] == DeliveryKind.TICKET and world.source is None and world.pr is None
                    and all(job[key] is None for key in ("current_attempt", "development_thread", "candidate_sha", "integrated_sha", "publication_sha", "pr_number"))
                    and job["base_sha"] == job["source_sha"] == job["workspace_sha"]
                    and not tx.rows(*run_statement(tx, 'SELECT id FROM attempts WHERE subject=? AND generation=? AND run_id=? LIMIT 1',
                                (subject, job["generation"]), run_last=True))
                    and not originals):
                # Job allocation fixes the dependency-visible Run head once.
                # A durable workspace intent, including a failed one, keeps it.
                tx.execute(*run_statement(tx, 'UPDATE subjects SET base_sha=?,base_tree=?,source_sha=?,workspace_sha=? WHERE id=? AND run_id=?',
                    (target.sha, target.tree, target.sha, target.sha, subject), run_last=True))
                job = self._job(tx, subject)
            return self._prepare_effect(tx, run, job, work, world, "workspace", before=job["source_sha"], payload=payload)
        integrated_phases = {"ready_publication", "publication_commit", "push", "pr", "checks", "approval", "merge"}
        changed_source = world.source is not None and world.source.sha != job["source_sha"]
        if phase in integrated_phases | {"integration"} and not self._observe_integration_snapshot(tx, job, world):
            self._finish_final_failure(tx, run)
            return Step("paused", subject, "integration_snapshot")
        if (phase in integrated_phases and job["integration_target"]
                and (job["integration_target"] != target.sha or changed_source)):
            self._update_boundary(tx, run, job)
            return Step("progress", subject, "updating")
        if phase == "candidate":
            development = self._last_attempt(tx, subject, Role.DEVELOPMENT)
            context_id = tx.one(*run_statement(tx, 'SELECT context_id FROM attempts WHERE id=? AND run_id=?',
                             (development,), run_last=True))[0]
            context = json.loads(tx.get(context_id, subject))
            parents = [job["workspace_sha"]]
            failure = context.get("integration_evidence")
            raw = failure.get("raw") if isinstance(failure, dict) else None
            if isinstance(raw, dict) and raw.get("merge_head"):
                if raw.get("head") != job["workspace_sha"] or not isinstance(raw["merge_head"], str):
                    raise KernelError("candidate_parent_unproven")
                if raw["merge_head"] not in parents:
                    parents.append(raw["merge_head"])
            return self._prepare_effect(tx, run, job, work, world, "candidate", before=job["workspace_sha"],
                                        payload={"checkout": job["checkout"], "summary_attempt": development, "parents": parents})
        if phase == "integration":
            job = self._job(tx, subject)  # An admitted external source append is the next CAS parent.
            if not self._integration_seat(tx, run, job):
                return Step("waiting", subject, "integration_seat")
            return self._prepare_effect(tx, run, job, work, world, "integrate",
                                        before=target.sha,
                                        payload={"checkout": job["checkout"], "source_sha": None if world.source is None else world.source.sha})
        if phase == "ready_publication":
            if job["kind"] == DeliveryKind.FINAL_RUN and job["integrated_sha"] != job["source_sha"]:
                return self._prepare_effect(tx, run, job, work, world, "run_branch", before=job["source_sha"],
                                            after=job["integrated_sha"])
            tx.execute(*run_statement(tx, "UPDATE subjects SET phase='publish',version=version+1 WHERE id=? AND run_id=?",
                (subject,), run_last=True))
            self._acceptance_promoted(tx, self._job(tx, subject), world)
            return Step("progress", subject)
        if phase == "publication_commit":
            attempt = tx.one(*run_statement(tx, 'SELECT result_id FROM attempts WHERE id=? AND run_id=?',
                          (job["publication_attempt"],), run_last=True))
            narrative = artifact(Role.PUBLICATION, tx.get(attempt["result_id"], subject))
            parents = [job["source_sha"]]
            # Rebuilding the reviewed tree must not discard its target ancestry;
            # otherwise a resolved conflict becomes a conflicting PR again.
            if (job["kind"] in {DeliveryKind.TICKET, DeliveryKind.PARENT_ONLY}
                    and target.sha != job["source_sha"]
                    and (target.sha, job["source_sha"]) not in world.ancestry):
                parents.append(target.sha)
            return self._prepare_effect(tx, run, job, work, world, "publication_commit", before=job["source_sha"],
                                        payload={"commit_message": narrative["commit_message"], "title": narrative["pr_title"],
                                                 "body": narrative["pr_body_markdown"], "parents": parents,
                                                 "reuse_sha": job["integrated_sha"] if job["kind"] == DeliveryKind.FINAL_RUN else None})
        if phase == "push":
            if job["source_sha"] == job["publication_sha"]:
                tx.execute(*run_statement(tx, "UPDATE subjects SET phase='pr',version=version+1 WHERE id=? AND run_id=?",
                    (subject,), run_last=True))
                return Step("progress", subject)
            return self._prepare_effect(tx, run, job, work, world, "push", before=job["source_sha"], after=job["publication_sha"])
        if phase == "pr":
            if not self._pr_write_current(run, job, world):
                self._gate(tx, subject, "published_head_changed")
                return Step("paused", subject, "pr_identity_changed")
            if world.pr is not None and job["pr_number"] is None:
                if world.pr.actor != self._publisher_actor(tx, run, world):
                    self._gate(tx, subject, "published_head_changed")
                    return Step("paused", subject, "pr_identity_changed")
                # Bind only the new edit intent to a late owned create. The
                # retired request retains its original number and payload.
                tx.execute(*run_statement(tx, 'UPDATE subjects SET pr_number=? WHERE id=? AND run_id=?',
                    (world.pr.number, subject), run_last=True))
                job = self._job(tx, subject)
            attempt = tx.one(*run_statement(tx, 'SELECT result_id FROM attempts WHERE id=? AND run_id=?',
                          (job["publication_attempt"],), run_last=True))
            narrative = artifact(Role.PUBLICATION, tx.get(attempt["result_id"], subject))
            old = None if world.pr is None else {"title": world.pr.title, "body": world.pr.body}
            return self._prepare_effect(tx, run, job, work, world, "pr_write", before=job["publication_sha"],
                                        after=job["publication_sha"], payload={"title": narrative["pr_title"],
                                        "body": self._narrative_body(tx, run, job, narrative["pr_body_markdown"]), "prior": old})
        if phase == "checks":
            if not self._published(tx, run, job, world):
                return Step("paused", subject, "published_head")
            if job["owned_root"] and not tx.rows(*run_statement(tx, 'SELECT effect_id FROM issue_links WHERE subject=? AND run_id=?',
                                             (subject,), run_last=True)):
                step = self._prepare_effect(tx, run, job, work, world, "issue_link", before=job["publication_sha"],
                                            payload={"issue": job["issue"], "ref": job["source_ref"], "source_head": job["publication_sha"]})
                tx.execute(*run_statement(tx, 'INSERT INTO issue_links(subject,generation,source_ref,source_sha,issue,effect_id,run_id) VALUES (?,?,?,?,?,?,?)',
                    (subject, job["generation"], job["source_ref"], job["publication_sha"], job["issue"], step.effect.effect_id), run_last=True))
                return step
            checks = world.checks
            if checks is None or checks.pr_number != job["pr_number"] or checks.head_sha != job["publication_sha"]:
                return self._wait(tx, run, job, "github_convergence", 600)
            material = tx.put(subject, "required_checks", wire_json(asdict(checks)))
            check_id = material
            tx.execute(*run_statement(tx, 'INSERT OR IGNORE INTO checks(id,subject,pr_number,head_sha,result,material_id,run_id) VALUES (?,?,?,?,?,?,?)',
                (check_id, subject, checks.pr_number,
                       checks.head_sha, checks.result, material), run_last=True))
            tx.execute(*run_statement(tx, 'UPDATE subjects SET checks_id=? WHERE id=? AND run_id=?',
                (check_id, subject), run_last=True))
            waiting = self._checks_wait(tx, run, job, checks)
            if waiting is not None:
                return waiting
            tx.execute(*run_statement(tx, "DELETE FROM waits WHERE subject=? AND kind='required_checks' AND run_id=?",
                (subject,), run_last=True))
            if checks.result == "fail":
                tx.execute(*run_statement(tx, "UPDATE subjects SET phase='development',purpose='ci',repair_source='required_checks',ci_failure=?,last_failure=?,grant_id=NULL,integrated_sha=NULL,integrated_tree=NULL,version=version+1 WHERE id=? AND run_id=?",
                    (material, material, subject), run_last=True))
                self._release_integration(tx, subject)
                self._event(tx, subject, "ci_failed", check_id, material)
            else:
                tx.execute(*run_statement(tx, 'UPDATE subjects SET phase=?,version=version+1 WHERE id=? AND run_id=?',
                    ("merge" if job["kind"] == DeliveryKind.TICKET else "approval", subject), run_last=True))
                self._event(tx, subject, "checks_observed", check_id, material)
            return Step("progress", subject)
        if phase == "approval":
            if not self._published(tx, run, job, world):
                return Step("paused", subject, "published_head")
            return Step("approval", subject, "explicit_approval_required")
        if phase == "merge":
            if not self._published(tx, run, job, world):
                self._finish_final_failure(tx, run)
                return Step("paused", subject, "merge_authority")
            waiting = self._checks_wait(tx, run, job, world.checks)
            if waiting is not None:
                return waiting
            assert world.checks is not None
            if world.checks.result == "fail":
                self._finish_final_failure(tx, run, "updating")
                tx.execute(*run_statement(tx, "UPDATE subjects SET phase='checks',grant_id=NULL WHERE id=? AND run_id=?",
                    (subject,), run_last=True))
                return Step("progress", subject)
            if not self._merge_authority(tx, run, job, world):
                self._finish_final_failure(tx, run)
                self._gate(tx, subject, "approval_revalidation_failed")
                return Step("paused", subject, "merge_authority")
            publication = tx.one(*run_statement(tx, 'SELECT result_id FROM attempts WHERE id=? AND run_id=?',
                              (job["publication_attempt"],), run_last=True))
            narrative = artifact(Role.PUBLICATION, tx.get(publication[0], subject))
            return self._prepare_effect(tx, run, job, work, world, "merge", before=target.sha,
                                        after=job["publication_sha"], payload={"method": "squash" if job["kind"] == DeliveryKind.TICKET else "merge",
                                        "commit_message": (narrative["pr_title"] + "\n\n" + self._narrative_body(tx, run, job, narrative["pr_body_markdown"]))
                                        if job["kind"] == DeliveryKind.TICKET else f"merge: Delivery Run {job['source_ref'].removeprefix('refs/heads/')}",
                                        "parents": [target.sha] + ([] if job["kind"] == DeliveryKind.TICKET else [job["publication_sha"]])})
        if phase == "completion_comment":
            return self._prepare_effect(tx, run, job, work, world, "completion_comment", before=job["merge_sha"],
                                        payload={"issue": job["issue"], "merge_sha": job["merge_sha"], "gate_mode": job["gate_mode"]})
        if phase == "close":
            comment = tx.one(*run_statement(tx, "SELECT * FROM effects WHERE subject=? AND generation=? AND operation='completion_comment' AND status='confirmed' AND before_sha=? AND run_id=?",
                          (subject, job["generation"], job["merge_sha"]), run_last=True))
            intent = self._comment_close_intent(run, job, comment, json.loads(tx.get(comment["payload_id"], subject)),
                                                json.loads(tx.get(comment["receipt_id"], subject)))
            return self._prepare_effect(tx, run, job, work, world, "close", before=job["merge_sha"],
                                        payload={"issue": job["issue"], "merge_sha": job["merge_sha"], "close_intent": intent})
        raise KernelError("prototype_step_unimplemented", phase)

    def _narrative_body(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, narrative: str) -> str:
        header = f"Parent Issue: #{run['parent_issue']}\n"
        if job["kind"] == DeliveryKind.TICKET:
            header += f"Primary Ticket: #{job['issue']}\nDelivery Type: Ticket\n\n"
        else:
            header += "Delivery Type: " + ("Parent-only" if job["kind"] == DeliveryKind.PARENT_ONLY else "Final Run") + "\n\n"
            if job["kind"] == DeliveryKind.FINAL_RUN:
                tickets = [(row["issue"], row["title"]) for row in tx.all(*run_statement(tx, "SELECT s.issue,v.title FROM subjects s JOIN requirement_versions v ON v.run_id=s.run_id AND v.issue=s.issue AND v.revision=s.revision WHERE s.kind='ticket' AND s.run_id=? ORDER BY s.issue",
                                                                       (), run_last=True))]
                seed = tx.rows(*run_statement(tx, "SELECT id FROM material WHERE subject=? AND purpose='fixture_evidence' AND run_id=?",
                           (job["id"],), run_last=True))
                if not tickets and seed:
                    historical = json.loads(tx.get(seed[0][0], job["id"])).get("ticket")
                    if historical:
                        tickets = [(historical["number"], historical["title"])]
                if tickets:
                    header += "## Completed Tickets\n\n" + "\n".join(f"- #{number}: {title}" for number, title in tickets) + "\n\n"
        return header + narrative.strip()

    def _last_attempt(self, tx: Transaction, subject: str, role: Role) -> str:
        return str(tx.one(*run_statement(tx, 'SELECT id FROM attempts WHERE subject=? AND role=? AND run_id=? ORDER BY ordinal DESC LIMIT 1',
                       (subject, role.value), run_last=True))[0])

    def _checks_wait(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, checks: Checks | None) -> Step | None:
        if checks is None or checks.pr_number != job["pr_number"] or checks.head_sha != job["publication_sha"]:
            return self._wait(tx, run, job, "github_convergence", 600)
        if checks.result == "pending":
            return self._wait(tx, run, job, "required_checks", self._subject_policy(tx, job).required_checks_wait_seconds)
        unfinished = checks.result == "fail" and any(state not in {"pass", "fail"} for _, state in checks.details)
        if checks.result == "unknown" or unfinished:
            if checks.result == "unknown":
                code, message = "github_checks_observation_unknown", "GitHub Required Checks returned an unknown state"
            elif job["kind"] == DeliveryKind.TICKET:
                code, message = "github_checks_observation_pending", "GitHub Required Checks still contain pending checks"
            else:
                code = "github_check_evidence_observation_pending"
                message = "GitHub Required Check failure evidence has not converged"
            evidence = tx.put(job["id"], "github_read_observation", wire_json({"code": code, "message": message}))
            return self._supervised_wait(tx, run, job["id"], self._automatic_scope(tx, run, job["id"], False),
                                         "github_convergence", 600, evidence)
        return None

    def _wait(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, kind: str, seconds: float) -> Step:
        return self._supervised_wait(tx, run, job["id"], self._automatic_scope(tx, run, job["id"], False), kind, seconds)

    def _finish_final_failure(self, tx: Transaction, run: sqlite3.Row, result: str = "failed") -> None:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id:
            raise KernelError("effect_binding")
        if run["final_action"]:
            tx.execute(*run_statement(tx, 'UPDATE actions SET status=? WHERE id=? AND run_id=?',
                (result, run["final_action"]), run_last=True))
            tx.execute("UPDATE runs SET final_action=NULL WHERE id=?", (run["id"],))

    def _integration_seat(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> bool:
        if job["kind"] != DeliveryKind.TICKET:
            return True
        subject = job["id"]
        if run["integration_holder"] == subject:
            return True
        self._ready(tx, subject, "integration_queue")
        if run["integration_holder"] is not None:
            return False
        first = tx.one(*run_statement(tx, 'SELECT subject FROM integration_queue WHERE run_id=? ORDER BY ready LIMIT 1',
                    (), run_last=True))[0]
        if first != subject:
            return False
        tx.execute("UPDATE runs SET integration_holder=? WHERE id=?", (subject, run["id"]))
        tx.execute(*run_statement(tx, 'DELETE FROM integration_queue WHERE subject=? AND run_id=?',
            (subject,), run_last=True))
        return True

    def _release_integration(self, tx: Transaction, subject: str) -> None:
        if self._pending_effect(tx, subject) is None:
            tx.execute(*run_statement(tx, 'UPDATE runs SET integration_holder=NULL WHERE integration_holder=? AND id=?',
                (subject,), run_last=True))
            tx.execute(*run_statement(tx, 'DELETE FROM integration_queue WHERE subject=? AND run_id=?',
                (subject,), run_last=True))

    def _integration_origin(self, tx: Transaction, job: sqlite3.Row) -> Commit:
        if job["workspace_sha"] == job["candidate_sha"] and job["candidate_tree"]:
            return Commit(job["candidate_sha"], job["candidate_tree"])
        receipts = tx.rows(*run_statement(tx, "SELECT receipt_id FROM effects WHERE subject=? AND generation=? AND status='confirmed' AND operation IN ('candidate','integrate','publication_commit') AND run_id=? ORDER BY rowid DESC LIMIT 1",
                       (job["id"], job["generation"]), run_last=True))
        if receipts:
            result = json.loads(tx.get(receipts[0][0], job["id"]))
            commit = result.get("commit")
            if result["disposition"] == "target" and commit is not None and commit["sha"] == job["workspace_sha"]:
                return Commit(commit["sha"], commit["tree"])
        raise KernelError("integration_workspace_unproven")

    def _prepare_effect(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, work: Work,
                        world: World | None, operation: str, *, before: str, after: str | None = None,
                        payload: dict[str, Any] | None = None) -> PrepareIO | EffectIO:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or job['run_id'] != selected.run_id or work.execution.run_id != selected.run_id:
            raise KernelError("effect_binding")
        if world is None and (not work.cleanup or operation not in {"cleanup_worktree", "cleanup_local_ref"}):
            raise KernelError("effect_binding")
        execution = work.execution
        subject, effect_id = job["id"], identity()
        actor = None
        if operation in self.REMOTE:
            assert world is not None
            actor = self._publisher_actor(tx, run, world)
        initial = operation == "run_branch" and job["kind"] == DeliveryKind.TICKET
        if initial:
            target = tx.one(*run_statement(tx, 'SELECT * FROM final_target WHERE run_id=?',
                         (), run_last=True))
            source_ref, target_ref = target["source_ref"], target["target_ref"]
            base = Commit(job["base_sha"], job["base_tree"])
        elif operation in self.CLEANUP:
            closed = tx.one(*run_statement(tx, "SELECT source_ref,target_ref,base_sha,base_tree FROM effects WHERE subject=? AND generation=? AND operation='close' AND status='confirmed' AND run_id=?",
                         (subject, job["generation"]), run_last=True))
            source_ref, target_ref = closed["source_ref"], closed["target_ref"]
            base = Commit(closed["base_sha"], closed["base_tree"])
        elif operation in {"inspect_resources", "abandon_worktree"}:
            source_ref, target_ref = job["source_ref"], job["target_ref"]
            base = Commit(job["base_sha"], job["base_tree"])
        else:
            assert world is not None
            source_ref, target_ref, base = job["source_ref"], job["target_ref"], self._require_target(world)
        body = dict(payload or {})
        body["intent_token"] = effect_id
        body["integrated_sha"] = job["integrated_sha"]
        body["integrated_tree"] = job["integrated_tree"]
        body["kind"] = job["kind"]
        if operation in {"workspace", "validation_checkout", "validation_cleanup", "candidate", "integrate", "publication_commit"}:
            for key in ("checkout", "owned_root", "common_git_dir"):
                body.setdefault(key, job[key])
        if operation in {"push", "run_branch"}:
            assert world is not None
            body["expected_remote_sha"] = None if initial or world.source is None else world.source.sha
        inputs = self._inputs(tx, run)
        if inputs:
            body["github_identity"] = inputs["runner_identity"]
        if operation == "validation_checkout":
            body["checkout"] = str(Path(job["checkout"]).parent / ("review-" + effect_id))
        candidate_sha, candidate_tree = job["candidate_sha"], job["candidate_tree"]
        if operation == "integrate":
            origin = self._integration_origin(tx, job)
            candidate_sha, candidate_tree = origin.sha, origin.tree
        payload_id = tx.put(subject, "effect_intent", wire_json(body))
        tx.execute(*run_statement(tx, "INSERT INTO effects(id,subject,operation,status,executor,epoch,parent_revision,graph_revision,revision,generation,source_ref,target_ref,before_sha,after_sha,candidate_sha,candidate_tree,base_sha,base_tree,pr_number,actor,payload_id,control_action,run_id) VALUES (?,?,?,'prepared',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (effect_id, subject, operation, execution.owner, execution.epoch, run["parent_revision"],
                    run["graph_revision"], job["revision"], job["generation"], source_ref, target_ref,
                    before, after, candidate_sha, candidate_tree, base.sha, base.tree,
                    job["pr_number"], actor, payload_id, body.get("control_action")), run_last=True))
        self._event(tx, subject, "effect_prepared", effect_id, payload_id)
        row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                  (effect_id,), run_last=True))
        return self._effect_work(tx, work, row)

    def _publisher_actor(self, tx: Transaction, run: sqlite3.Row, world: World) -> str:
        selected = self._inputs(tx, run).get("runner_identity")
        actor = world.publisher_actor
        if (selected is None or not isinstance(actor, str) or not actor.strip() or actor != actor.strip()
                or world.publisher_identity != selected):
            raise KernelError("publisher_identity_unproven")
        placeholders = ",".join("?" for _ in self.REMOTE)
        previous = tx.rows(*run_statement(tx, f'SELECT actor FROM effects WHERE operation IN ({placeholders}) AND actor IS NOT NULL AND actor!=? AND run_id=? LIMIT 1',
                       (*self.REMOTE, actor), run_last=True))
        if previous:
            raise KernelError("publisher_identity_changed")
        return actor

    def _guard_publisher(self, tx: Transaction, run: sqlite3.Row, effect: sqlite3.Row, world: World | LocalCleanupObservation) -> None:
        if isinstance(world, LocalCleanupObservation):
            self._local_cleanup_effect(tx, world.effect)
            if effect["id"] != world.effect.effect_id:
                raise KernelError("local_cleanup_observation_binding")
            return
        if effect["operation"] in self.REMOTE and self._publisher_actor(tx, run, world) != effect["actor"]:
            raise KernelError("publisher_identity_changed")

    def attach_publication_commit(self, effect: Effect, witness: Mapping[str, Any],
                                  execution: Execution, world: World) -> Effect:
        """Persist the original Git object bytes before the first physical write."""
        with self.storage.transaction() as tx:
            run, job = self._owned(tx, effect.subject, execution)
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                      (effect.effect_id, effect.subject), run_last=True))
            stored = self._effect(tx, row, execution)
            payload = dict(stored.payload)
            prior = payload.pop("publication_commit_intent", None)
            original = replace(stored, payload=payload)
            if (row["operation"] != "publication_commit" or row["status"] != "prepared" or row["attempts"] != 0
                    or effect not in (stored, original) or payload.get("reuse_sha")):
                raise KernelError("effect_binding")
            if prior is not None:
                if prior != witness:
                    raise KernelError("publication_commit_intent_changed")
                return stored
            if (run["stopped"] or run["blocked"] or self._gates(tx, effect.subject)
                    or row["generation"] != job["generation"] or not self._current(tx, job, run, world)
                    or row["base_sha"] != self._require_target(world).sha):
                raise KernelError("operation_cancelled")
            if (set(witness) != {"tree_sha", "parents", "message", "raw_commit", "expected_sha"}
                    or witness["tree_sha"] != payload["integrated_tree"] or witness["parents"] != payload["parents"]
                    or witness["message"] != payload["commit_message"]
                    or not isinstance(witness["raw_commit"], str) or not witness["raw_commit"]
                    or not isinstance(witness["expected_sha"], str) or not witness["expected_sha"]):
                raise KernelError("publication_commit_intent_changed")
            payload["publication_commit_intent"] = dict(witness)
            material = tx.put(effect.subject, "effect_intent", wire_json(payload))
            tx.execute(*run_statement(tx, 'UPDATE effects SET payload_id=? WHERE id=? AND run_id=?',
                (material, effect.effect_id), run_last=True))
            self._event(tx, effect.subject, "publication_commit_prepared", effect.effect_id, material)
            return self._effect(tx, tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                                        (effect.effect_id,), run_last=True)), execution)

    def _retirement_authority(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                              row: sqlite3.Row, world: World) -> bool:
        abandoned = self._abandon_action(tx)
        if abandoned is not None:
            return self._abandon_control_authority(tx, abandoned, row)
        payload = json.loads(tx.get(row["payload_id"], job["id"]))
        action = self._pending_requeue(tx, job["id"])
        return bool(action is not None and payload.get("control_action") == action["id"]
                    and payload.get("control_kind") == "requeue" and row["generation"] == job["generation"]
                    and self._requeue_current(tx, run, job, action, world))

    def attach_retirement(self, effect: Effect, witness: Mapping[str, Any], execution: Execution, world: World) -> Effect:
        """Bind a read-only PR/event watermark before any retirement write."""
        with self.storage.transaction() as tx:
            run, job = self._effect_owned(tx, effect, execution)
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                      (effect.effect_id, effect.subject), run_last=True))
            if (effect.operation != "retire_pr" or row["status"] != "prepared" or row["attempts"]
                    or self._effect(tx, row, execution) != effect or not self._retirement_authority(tx, run, job, row, world)):
                raise KernelError("retirement_intent_boundary")
            payload = json.loads(tx.get(row["payload_id"], effect.subject))
            prior = payload["prior_pr"]
            expected = {"repository": effect.repository, "pr_number": prior["number"], "source_ref": effect.source_ref,
                        "target_ref": effect.target_ref, "head_sha": prior["head"]["sha"], "base_sha": prior["base"]["sha"],
                        "author": prior["actor"]}
            if (set(witness) != {*expected, "baseline_event_id"} or any(witness[key] != value for key, value in expected.items())
                    or type(witness["baseline_event_id"]) is not int or witness["baseline_event_id"] < 0):
                raise KernelError("retirement_pr_changed")
            intent = {**dict(witness), "effect_id": effect.effect_id, "control_action": payload["control_action"],
                      "run_id": run["id"], "subject": effect.subject, "generation": row["generation"],
                      "control_kind": payload["control_kind"], "actor": effect.actor,
                      "comment_body": (("Abandoning the original delivery Run." if run["language"] == "en" else "放弃原交付 Run。")
                                       if payload["control_kind"] == "abandon" else
                                       ("Retiring the superseded generation." if run["language"] == "en" else "封存已被替代的工作代次。"))
                                      + f"\n\n<!-- agent-run-retirement:{effect.effect_id} -->"}
            if "retirement_intent" in payload:
                if payload["retirement_intent"] != intent:
                    raise KernelError("retirement_intent_changed")
                return effect
            payload["retirement_intent"] = intent
            material = tx.put(effect.subject, "effect_intent", wire_json(payload))
            tx.execute(*run_statement(tx, 'UPDATE effects SET payload_id=? WHERE id=? AND run_id=?',
                (material, effect.effect_id), run_last=True))
            self._event(tx, effect.subject, "retirement_intent_prepared", effect.effect_id, material)
            return self._effect(tx, tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND run_id=?',
                                        (effect.effect_id,), run_last=True)), execution)

    def _dispatch(self, effect: Effect, execution: Execution, world: World | LocalCleanupObservation, *,
                 resource_inspections: tuple[ResourceInspection, ...] | None = None, token: str | ReadFence) -> bool:
        """Recheck authority immediately before the single external operation."""
        with self.storage.transaction() as tx:
            self._check_operation(tx, token, execution)
            run, job = self._effect_owned(tx, effect, execution)
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                      (effect.effect_id, effect.subject), run_last=True))
            if row["status"] != "prepared":
                return False
            if self._effect(tx, row, execution) != effect:
                raise KernelError("effect_binding")
            if (effect.operation == "publication_commit" and not effect.payload.get("reuse_sha")
                    and not effect.payload.get("publication_commit_intent")):
                raise KernelError("publication_commit_intent_required")
            self._guard_publisher(tx, run, row, world)
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                if effect.operation == "retire_pr" and not effect.payload.get("retirement_intent"):
                    raise KernelError("retirement_intent_required")
                self._abandon_write_current(tx, abandoned, row, resource_inspections)
                tx.execute(*run_statement(tx, "UPDATE effects SET status='sent',attempts=attempts+1 WHERE id=? AND run_id=?",
                    (effect.effect_id,), run_last=True))
                self._event(tx, effect.subject, "effect_dispatched", effect.effect_id, generation=row["generation"])
                return True
            if effect.operation == "retire_pr":
                if not isinstance(world, World):
                    raise KernelError("local_cleanup_observation_binding")
                if not effect.payload.get("retirement_intent"):
                    raise KernelError("retirement_intent_required")
                if not self._retirement_authority(tx, run, job, row, world):
                    return False
                tx.execute(*run_statement(tx, "UPDATE effects SET status='sent',attempts=attempts+1 WHERE id=? AND run_id=?",
                    (effect.effect_id,), run_last=True))
                self._event(tx, effect.subject, "effect_dispatched", effect.effect_id)
                return True
            if run["stopped"] or run["blocked"] or self._gates(tx, effect.subject):
                return False
            if effect.operation in self.CLEANUP:
                if not self._cleanup_current(tx, job, row):
                    return False
                tx.execute(*run_statement(tx, "UPDATE effects SET status='sent',attempts=attempts+1 WHERE id=? AND run_id=?",
                    (effect.effect_id,), run_last=True))
                self._event(tx, effect.subject, "effect_dispatched", effect.effect_id)
                return True
            if not isinstance(world, World):
                raise KernelError("local_cleanup_observation_binding")
            if not self._current(tx, job, run, world):
                return False
            if world.target is None and not (effect.operation == "run_branch" and job["kind"] == DeliveryKind.TICKET):
                self._gate(tx, effect.subject, "target_branch_missing")
                return False
            if (effect.operation in {"integrate", "run_branch", "publication_commit", "push", "pr_write", "merge"}
                    and not self._effect_base_current(job, row, world)):
                if effect.operation == "run_branch" and job["kind"] == DeliveryKind.TICKET:
                    self._gate(tx, effect.subject, "target_branch_changed")
                    return False
                tx.execute(*run_statement(tx, "UPDATE effects SET status='retired' WHERE id=? AND run_id=?",
                    (effect.effect_id,), run_last=True))
                if effect.operation != "integrate":
                    self._update_boundary(tx, run, job)
                return False
            if (row["generation"] != job["generation"] or row["parent_revision"] != run["parent_revision"]
                    or row["graph_revision"] != run["graph_revision"] or row["revision"] != job["revision"]):
                return False
            if effect.operation == "pr_write" and not self._pr_write_current(run, job, world):
                self._gate(tx, effect.subject, "published_head_changed")
                return False
            if effect.operation == "merge" and (not self._published(tx, run, job, world)
                                                   or not self._merge_authority(tx, run, job, world)):
                tx.execute(*run_statement(tx, "UPDATE effects SET status='retired' WHERE id=? AND run_id=?",
                    (effect.effect_id,), run_last=True))
                return False
            tx.execute(*run_statement(tx, "UPDATE effects SET status='sent',attempts=attempts+1 WHERE id=? AND run_id=?",
                (effect.effect_id,), run_last=True))
            if effect.operation == "issue_link":
                tx.execute(*run_statement(tx, "UPDATE issue_links SET state='indeterminate' WHERE effect_id=? AND run_id=?",
                    (effect.effect_id,), run_last=True))
            self._event(tx, effect.subject, "effect_dispatched", effect.effect_id)
            return True

    def _reconcile(self, request: EffectIO, result: EffectResult, world: World | None = None, *, token: str | ReadFence | None = None) -> Step | ObserveIO:
        """Record target/prior/third/unknown without treating absence as read failure.

        Exact target settlement does not need live mutation authority. A prior
        observation may only rearm the original intent under current authority.
        """
        effect, work = request.effect, request.work
        execution = work.execution
        with self.storage.transaction() as tx:
            selected = transaction_scope(tx)
            if effect.execution.run_id != selected.run_id or execution.run_id != selected.run_id:
                raise KernelError("effect_binding")
            if world is not None:
                if token is None:
                    raise KernelError("operation_scope_required")
                self._check_operation(tx, token, execution)
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                      (effect.effect_id, effect.subject), run_last=True))
            if row["status"] in {"confirmed", "observed"}:
                requeue = self._pending_requeue(tx, effect.subject)
                if (row["status"] == "confirmed" and requeue is not None
                        and self._requeue_scope(tx, requeue).get("retired_effect") == row["id"]):
                    run, job = self._owned(tx, effect.subject, execution)
                    if self._effect(tx, row, execution) != effect:
                        raise KernelError("effect_binding")
                    return self._recheck_retirement(tx, run, job, requeue, row, result)
                return Step("progress", effect.subject)
            # An already absent owned cleanup resource needs no deletion.
            # Record observation only: never invent a dispatch or successful delete.
            if row["status"] == "prepared" and effect.operation in self.CLEANUP and result.disposition == "target":
                _, job = self._owned(tx, effect.subject, execution)
                if self._effect(tx, row, execution) != effect or not self._cleanup_current(tx, job, row):
                    raise KernelError("effect_binding")
                receipt = tx.put(effect.subject, "effect_receipt", wire_json(asdict(result)))
                tx.execute(*run_statement(tx, "UPDATE effects SET status='observed',receipt_id=? WHERE id=? AND run_id=?",
                    (receipt, effect.effect_id), run_last=True))
                self._event(tx, effect.subject, "effect_observed", effect.effect_id, receipt)
                return Step("progress", effect.subject)
            # A foreign cleanup resource is a known observation, not a write.
            # Persist the refusal without inventing first-dispatch authority.
            if row["status"] == "prepared" and effect.operation in self.CLEANUP and result.disposition == "third":
                self._owned(tx, effect.subject, execution)
                if self._effect(tx, row, execution) != effect:
                    raise KernelError("effect_binding")
                evidence = tx.put(effect.subject, "cleanup_observation", wire_json(asdict(result)))
                tx.execute(*run_statement(tx, "UPDATE resource_cleanup SET outcome='third' WHERE effect_id=? AND run_id=?",
                    (effect.effect_id,), run_last=True))
                self._event(tx, effect.subject, "cleanup_pending", effect.effect_id, evidence)
                return Step("paused", effect.subject, "cleanup_pending")
            original_target = row["status"] == "prepared" and row["attempts"] > 0 and result.disposition == "target"
            if row["status"] == "prepared" and not original_target:
                run, _ = self._owned(tx, effect.subject, execution)
                if self._effect(tx, row, execution) != effect or result.disposition == "prior":
                    raise KernelError("effect_not_dispatched")
                evidence = tx.put(effect.subject, "effect_pre_dispatch_observation", wire_json(asdict(result)))
                self._gate(tx, effect.subject, "write_conflict" if result.disposition in {"target", "third"} else "write_result_unknown",
                           evidence=evidence)
                self._event(tx, effect.subject, "effect_pre_dispatch_observed", effect.effect_id, evidence)
                self._finish_final_failure(tx, run)
                return Step("paused", effect.subject, "write_not_dispatched")
            if row["status"] != "sent" and not original_target:
                raise KernelError("effect_not_dispatched")
            if self._effect(tx, row, effect.execution) != effect:
                raise KernelError("effect_binding")
            if effect.operation == "issue_link":
                # Readback cannot establish that the original optional call's
                # acknowledgement was durably received. Never replay it.
                return Step("progress", effect.subject, "optional_display_indeterminate")
            if effect.operation in self.CLEANUP and result.disposition != "target":
                if result.disposition == "prior" and world is None:
                    return self._observe_work(tx, work, effect.subject, after=request, result=result)
                evidence = tx.put(effect.subject, "cleanup_observation", wire_json(asdict(result)))
                tx.execute(*run_statement(tx, 'UPDATE resource_cleanup SET outcome=? WHERE effect_id=? AND run_id=?',
                    (result.disposition, effect.effect_id), run_last=True))
                self._event(tx, effect.subject, "cleanup_pending", effect.effect_id, evidence)
                if result.disposition == "prior":
                    run, job = self._owned(tx, effect.subject, execution)
                    if (not run["stopped"] and not run["blocked"] and not self._gates(tx, effect.subject)
                            and self._cleanup_current(tx, job, row) and row["attempts"] < 3):
                        tx.execute(*run_statement(tx, "UPDATE effects SET status='prepared' WHERE id=? AND run_id=?",
                            (effect.effect_id,), run_last=True))
                        tx.execute(*run_statement(tx, 'UPDATE resource_cleanup SET outcome=NULL WHERE effect_id=? AND run_id=?',
                            (effect.effect_id,), run_last=True))
                        return Step("progress", effect.subject)
                return Step("waiting", effect.subject, "cleanup_pending")
            if result.disposition in {"target", "failure"}:
                receipt = tx.put(effect.subject, "effect_receipt", wire_json(asdict(result)))
                tx.execute(*run_statement(tx, "UPDATE effects SET status='observed',receipt_id=? WHERE id=? AND run_id=?",
                    (receipt, effect.effect_id), run_last=True))
                self._event(tx, effect.subject, "effect_observed", effect.effect_id, receipt)
                return Step("progress", effect.subject)
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                self._abandon_owned(tx, abandoned, execution)
                if row["control_action"] == abandoned["id"]:
                    payload = json.loads(tx.get(row["payload_id"], effect.subject))
                    if result.disposition == "third" and row["operation"] in {"reopen_ticket", "recovery_comment"} and self._reopen_not_owned(payload, asdict(result)):
                        receipt = tx.put(effect.subject, "effect_receipt", wire_json(asdict(result)))
                        tx.execute(*run_statement(tx, "UPDATE effects SET status='observed',receipt_id=? WHERE id=? AND run_id=?",
                            (receipt, effect.effect_id), run_last=True))
                        self._event(tx, effect.subject, "ticket_recovery_not_owned", effect.effect_id, receipt, generation=row["generation"])
                        return Step("progress", effect.subject)
                    if row["operation"] in {"reopen_ticket", "recovery_comment"} and result.disposition in {"prior", "unknown"}:
                        evidence = tx.put(effect.subject, "github_read_observation", wire_json({
                            "code": "github_recovery_observation_pending", "effect": effect.effect_id, "result": asdict(result)}))
                        run = tx.one(*run_statement(tx, 'SELECT * FROM runs WHERE id=?',
                                  (), run_last=True))
                        return self._supervised_wait(tx, run, effect.subject,
                            self._automatic_scope(tx, run, effect.subject, False), "github_convergence", 600, evidence)
                    if (result.disposition == "prior" and self._abandon_control_scope(tx, abandoned, row)
                            and row["attempts"] - row["retry_baseline"] < 3):
                        evidence = tx.put(effect.subject, "abandon_prior_observation", wire_json({"dispatch_serial": row["attempts"], "result": asdict(result)}))
                        self._event(tx, effect.subject, "abandon_prior_confirmed", effect.effect_id, evidence, generation=row["generation"])
                        tx.execute(*run_statement(tx, "UPDATE effects SET status='retired' WHERE id=? AND run_id=?",
                            (effect.effect_id,), run_last=True))
                        return Step("progress", effect.subject)
                elif effect.effect_id in self._abandon_scope(tx, abandoned)["unsettled"] and result.disposition == "prior":
                    tx.execute(*run_statement(tx, "UPDATE effects SET status='retired' WHERE id=? AND run_id=?",
                        (effect.effect_id,), run_last=True))
                    self._event(tx, effect.subject, "abandoned_business_intent_not_applied", effect.effect_id, generation=row["generation"])
                    return Step("progress", effect.subject)
                evidence = tx.put(effect.subject, "abandon_observation", wire_json(asdict(result)))
                self._event(tx, effect.subject, "abandon_reconciliation_pending", effect.effect_id, evidence, generation=row["generation"])
                return Step("paused", effect.subject, result.disposition)
            run, job = self._owned(tx, effect.subject, execution)
            if result.disposition == "prior":
                if world is None:
                    return self._observe_work(tx, work, effect.subject, after=request, result=result)
                if row["operation"] == "retire_pr":
                    if not self._retirement_authority(tx, run, job, row, world):
                        return Step("paused", effect.subject, "retirement_authority")
                    if row["attempts"] - row["retry_baseline"] >= 3:
                        self._gate(tx, effect.subject, "publication_retry_exhausted")
                        return Step("paused", effect.subject, "publication_retry_exhausted")
                    tx.execute(*run_statement(tx, "UPDATE effects SET status='prepared' WHERE id=? AND run_id=?",
                        (effect.effect_id,), run_last=True))
                    return Step("progress", effect.subject)
                if self._effect_stale(tx, run, job, row):
                    tx.execute(*run_statement(tx, "UPDATE effects SET status='retired' WHERE id=? AND run_id=?",
                        (row["id"],), run_last=True))
                    if job["kind"] == DeliveryKind.FINAL_RUN and run["observed_graph_revision"] == run["graph_revision"]:
                        tx.execute(*run_statement(tx, "UPDATE subjects SET refresh_revision=?,phase='refresh_final',grant_id=NULL WHERE id=? AND run_id=?",
                            (run["observed_parent_revision"], job["id"]), run_last=True))
                        tx.execute(*run_statement(tx, "UPDATE runs SET blocked='final_refresh' WHERE (blocked IS NULL OR blocked='final_refresh') AND id=?",
                            (), run_last=True))
                    self._event(tx, effect.subject, "stale_intent_not_applied", effect.effect_id)
                    return Step("progress", effect.subject)
                if run["stopped"] or run["blocked"] or self._gates(tx, effect.subject) or not self._current(tx, job, run, world):
                    return Step("paused", effect.subject, "original_write_not_authorized")
                if (row["operation"] in {"run_branch", "publication_commit", "push", "pr_write", "merge"}
                        and not self._effect_base_current(job, row, world)):
                    if row["operation"] == "run_branch" and job["kind"] == DeliveryKind.TICKET:
                        self._gate(tx, effect.subject, "target_branch_changed")
                        return Step("paused", effect.subject, "target_branch_changed")
                    tx.execute(*run_statement(tx, "UPDATE effects SET status='retired' WHERE id=? AND run_id=?",
                        (effect.effect_id,), run_last=True))
                    self._update_boundary(tx, run, job)
                    return Step("progress", effect.subject, "updating")
                if row["attempts"] - row["retry_baseline"] >= 3:
                    self._gate(tx, effect.subject, "publication_retry_exhausted")
                    return Step("paused", effect.subject, "publication_retry_exhausted")
                tx.execute(*run_statement(tx, "UPDATE effects SET status='prepared' WHERE id=? AND run_id=?",
                    (effect.effect_id,), run_last=True))
                return Step("progress", effect.subject)
            evidence = tx.put(effect.subject, "effect_observation", wire_json(asdict(result)))
            self._gate(tx, effect.subject, "write_conflict" if result.disposition == "third" else "write_result_unknown", evidence=evidence)
            self._finish_final_failure(tx, run)
            return Step("paused", effect.subject, result.disposition)

    def observe_effect_failure(self, effect: Effect, diagnostic: Mapping[str, Any]) -> None:
        """A call failure is an audit fact, not proof of a remote write's outcome."""
        with self.storage.transaction() as tx:
            row = tx.one(*run_statement(tx, 'SELECT * FROM effects WHERE id=? AND subject=? AND run_id=?',
                      (effect.effect_id, effect.subject), run_last=True))
            actual = self._effect(tx, row, effect.execution)
            if (replace(effect, dispatch_serial=actual.dispatch_serial) != actual
                    or not 1 <= effect.dispatch_serial <= row["attempts"]):
                raise KernelError("effect_binding")
            material = tx.put(effect.subject, "effect_failure", wire_json(dict(diagnostic)))
            old = tx.rows(*run_statement(tx, 'SELECT evidence FROM effect_failures WHERE effect=? AND dispatch_serial=? AND run_id=?',
                      (effect.effect_id, effect.dispatch_serial), run_last=True))
            if old:
                if old[0][0] != material:
                    raise KernelError("conflicting_effect_failure")
                return
            tx.execute(*run_statement(tx, 'INSERT INTO effect_failures(effect,dispatch_serial,evidence,observed_at,run_id) VALUES (?,?,?,?,?)',
                (effect.effect_id, effect.dispatch_serial, material, self.clock()), run_last=True))
            self._event(tx, effect.subject, "effect_failed", effect.effect_id, material, generation=row["generation"])

    def _apply_effect(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row, world: World) -> Step:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or job['run_id'] != selected.run_id or row['run_id'] != selected.run_id or (row['subject'] != job['id']):
            raise KernelError("effect_binding")
        subject, operation = job["id"], row["operation"]
        result = json.loads(tx.get(row["receipt_id"], subject))
        self._validate_target(tx, run, job, row, result)
        commit = result["commit"]
        if result["disposition"] == "failure":
            tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
                (row["id"],), run_last=True))
            purpose = Purpose.INTEGRATION.value if operation == "integrate" else job["purpose"]
            tx.execute(*run_statement(tx, "UPDATE subjects SET phase='development',purpose=?,repair_source=?,resume_purpose=?,last_failure=?,integrated_sha=NULL,integrated_tree=NULL,workspace_sha=?,version=version+1 WHERE id=? AND run_id=?",
                (purpose, "integration" if operation == "integrate" else "git_integrity", job["purpose"], row["receipt_id"],
                        job["workspace_sha"] if commit is None else commit["sha"], subject), run_last=True))
            # Keep the exact CI trigger for a later CI-waiver Publication. The
            # current integration repair receives only its actual Git failure;
            # _context selects that responsibility from purpose/repair_source.
            if operation != "integrate" or result["raw"].get("merge_conflict") is not True:
                self._release_integration(tx, subject)
            return Step("progress", subject)
        if operation in {"workspace", "validation_checkout", "candidate", "integrate", "publication_commit", "merge"} and not commit:
            raise KernelError("effect_receipt_incomplete", operation)
        if operation == "workspace":
            if commit["sha"] != job["source_sha"]:
                raise KernelError("workspace_commit_changed")
            phase = "integration" if job["kind"] == DeliveryKind.FINAL_RUN else "development"
        elif operation == "validation_checkout":
            payload = json.loads(tx.get(row["payload_id"], subject))
            if commit["sha"] != payload["reviewed_commit"]:
                raise KernelError("validation_commit_changed")
            tx.execute(*run_statement(tx, 'UPDATE subjects SET validation_checkout=? WHERE id=? AND run_id=?',
                (payload["checkout"], subject), run_last=True))
            phase = job["phase"]
        elif operation == "validation_cleanup":
            tx.execute(*run_statement(tx, 'UPDATE subjects SET validation_cleanup=NULL WHERE id=? AND run_id=?',
                (subject,), run_last=True))
            phase = job["phase"]
        elif operation == "candidate":
            if job["purpose"] == Purpose.CI and result["raw"].get("no_change"):
                tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
                    (row["id"],), run_last=True))
                tx.execute(*run_statement(tx, "UPDATE subjects SET phase='development' WHERE id=? AND run_id=?",
                    (subject,), run_last=True))
                budget = self._budget(tx, job)
                self._gate(tx, subject, "ci_development_budget_exhausted" if budget["developments_used"] >= budget["development_limit"]
                           else "ci_investigation_paused", purpose=Purpose.CI.value, evidence=row["receipt_id"])
                return Step("paused", subject, "ci_investigation")
            tx.execute(*run_statement(tx, 'UPDATE subjects SET candidate_sha=?,candidate_tree=?,workspace_sha=?,expected_tree=?,integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL WHERE id=? AND run_id=?',
                (commit["sha"], commit["tree"], commit["sha"], commit["tree"], subject), run_last=True))
            budget = self._budget(tx, job)
            if job["purpose"] == Purpose.INTEGRATION:
                phase = "integration"
            elif job["purpose"] == Purpose.CI and not budget["review_required"]:
                phase = "integration"
                tx.execute(*run_statement(tx, "UPDATE subjects SET gate_mode='ci_waiver' WHERE id=? AND run_id=?",
                    (subject,), run_last=True))
            elif (job["kind"] == DeliveryKind.TICKET and job["purpose"] == Purpose.REQUIREMENTS
                    and budget["reviews_used"] >= budget["review_limit"] and not result["raw"].get("no_change")):
                if not job["review_attempt"]:
                    raise KernelError("fallback_without_prior_review")
                previous = tx.one(*run_statement(tx, 'SELECT outcome FROM attempts WHERE id=? AND run_id=?',
                               (job["review_attempt"],), run_last=True))
                if previous["outcome"] != "fail":
                    raise KernelError("fallback_without_findings")
                phase = "integration"
                tx.execute(*run_statement(tx, "UPDATE subjects SET gate_mode='deterministic_fallback' WHERE id=? AND run_id=?",
                    (subject,), run_last=True))
            else:
                phase = "review"
        elif operation == "integrate":
            if not result["expected_tree"]:
                raise KernelError("integration_receipt_incomplete")
            tx.execute(*run_statement(tx, 'UPDATE subjects SET integrated_sha=?,integrated_tree=?,workspace_sha=?,expected_tree=?,integration_target=? WHERE id=? AND run_id=?',
                (commit["sha"], commit["tree"], commit["sha"], result["expected_tree"], row["base_sha"], subject), run_last=True))
            payload = json.loads(tx.get(row["payload_id"], subject))
            live_source = None if world.source is None else world.source.sha
            if row["base_sha"] != self._require_target(world).sha or payload["source_sha"] != live_source:
                self._observe_integration_snapshot(tx, job, world)
                tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
                    (row["id"],), run_last=True))
                tx.execute(*run_statement(tx, "UPDATE subjects SET phase='integration',purpose='integration',repair_source='integration',version=version+1 WHERE id=? AND run_id=?",
                    (subject,), run_last=True))
                return Step("progress", subject, "integration_snapshot_changed")
            tx.execute(*run_statement(tx, 'UPDATE integration_snapshots SET open=0 WHERE subject=? AND window=(SELECT max(i.window) FROM integration_snapshots i WHERE i.subject=? AND i.run_id=integration_snapshots.run_id) AND run_id=?',
                (subject, subject), run_last=True))
            latest = self._job(tx, subject)
            if job["kind"] == DeliveryKind.TICKET:
                phase = "ready_publication"
            elif self._review_current(tx, latest, world):
                phase = "ready_publication"
            else:
                phase = "review"
                tx.execute(*run_statement(tx, 'UPDATE subjects SET purpose=? WHERE id=? AND run_id=?',
                    (Purpose.CI.value if job["purpose"] == Purpose.CI else Purpose.INTEGRATION.value, subject), run_last=True))
        elif operation == "run_branch":
            if job["kind"] == DeliveryKind.TICKET:
                phase = "workspace"
            else:
                tx.execute(*run_statement(tx, 'UPDATE subjects SET source_sha=? WHERE id=? AND run_id=?',
                    (row["after_sha"], subject), run_last=True))
                self._record_source_write(tx, subject, row["source_ref"], row["after_sha"])
                phase = "publish"
        elif operation == "publication_commit":
            if commit["tree"] != job["integrated_tree"]:
                raise KernelError("publication_tree_changed")
            tx.execute(*run_statement(tx, 'UPDATE subjects SET publication_sha=?,publication_tree=?,workspace_sha=? WHERE id=? AND run_id=?',
                (commit["sha"], commit["tree"], commit["sha"], subject), run_last=True))
            phase = "push"
        elif operation == "push":
            tx.execute(*run_statement(tx, 'UPDATE subjects SET source_sha=? WHERE id=? AND run_id=?',
                (row["after_sha"], subject), run_last=True))
            self._record_source_write(tx, subject, row["source_ref"], row["after_sha"])
            phase = "pr"
        elif operation == "pr_write":
            pr = result["pr"]
            tx.execute(*run_statement(tx, 'UPDATE subjects SET pr_number=? WHERE id=? AND run_id=?',
                (pr["number"], subject), run_last=True))
            phase = "checks" if row["base_sha"] == self._require_target(world).sha else "integration"
        elif operation == "merge":
            tx.execute(*run_statement(tx, 'UPDATE subjects SET merge_sha=? WHERE id=? AND run_id=?',
                (commit["sha"], subject), run_last=True))
            phase = "completion_comment"
        elif operation == "completion_comment":
            phase = "close"
        elif operation == "close":
            phase = "complete"
            if run["source_sequence"] is not None and job["kind"] == DeliveryKind.TICKET:
                payload = json.loads(tx.get(row["payload_id"], subject))
                comment = tx.one(*run_statement(tx, "SELECT id FROM effects WHERE id=? AND subject=? AND generation=? AND operation='completion_comment' AND status='confirmed' AND run_id=?",
                              (payload["close_intent"]["effect_id"], subject, job["generation"]), run_last=True))
                if not job["merge_sha"] or not job["publication_sha"] or not job["pr_number"] or not job["gate_mode"]:
                    raise KernelError("completion_evidence_missing")
                tx.execute(*run_statement(tx, 'INSERT INTO completions(subject,generation,revision,parent_revision,graph_revision,source_sha,merge_sha,tree,pr_number,gate_mode,review_attempt,development_attempt,comment_effect,close_effect,run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (subject, job["generation"], job["revision"], run["parent_revision"], run["graph_revision"], job["publication_sha"],
                            job["merge_sha"], job["publication_tree"], job["pr_number"], job["gate_mode"], job["review_attempt"],
                            self._last_attempt(tx, subject, Role.DEVELOPMENT), comment["id"], row["id"]), run_last=True))
            tx.execute("UPDATE runs SET final_action=NULL WHERE id=?", (run["id"],))
            if run["final_action"]:
                tx.execute(*run_statement(tx, "UPDATE actions SET status='complete' WHERE id=? AND run_id=?",
                    (run["final_action"],), run_last=True))
            if job["owned_root"] and job["common_git_dir"]:
                tx.execute(*run_statement(tx, 'INSERT OR IGNORE INTO resource_cleanup(subject,generation,source_ref,source_sha,checkout,workspace_sha,owned_root,common_git_dir,run_id) VALUES (?,?,?,?,?,?,?,?,?)',
                    (subject, job["generation"], job["source_ref"], job["publication_sha"], job["checkout"], job["workspace_sha"], job["owned_root"], job["common_git_dir"]), run_last=True))
        else:
            raise KernelError("unknown_effect", operation)
        tx.execute(*run_statement(tx, "UPDATE effects SET status='confirmed' WHERE id=? AND run_id=?",
            (row["id"],), run_last=True))
        tx.execute(*run_statement(tx, 'UPDATE subjects SET phase=?,version=version+1 WHERE id=? AND run_id=?',
            (phase, subject), run_last=True))
        if phase not in {"development", "review", "publish"}:
            tx.execute(*run_statement(tx, 'DELETE FROM work_queue WHERE subject=? AND run_id=?',
                (subject,), run_last=True))
        if phase == "complete":
            self._release_integration(tx, subject)
        self._event(tx, subject, "effect_consumed", row["id"], row["receipt_id"])
        if operation == "run_branch" and phase == "publish":
            self._acceptance_promoted(tx, self._job(tx, subject), world)
        return Step("progress", subject)

    def _update_boundary(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> None:
        selected = transaction_scope(tx)
        if run['id'] != selected.run_id or job['run_id'] != selected.run_id:
            raise KernelError("effect_binding")
        tx.execute(*run_statement(tx, "UPDATE subjects SET phase='integration',purpose='integration',repair_source='integration',integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL,version=version+1 WHERE id=? AND run_id=?",
            (job["id"],), run_last=True))
        if run["final_action"]:
            tx.execute(*run_statement(tx, "UPDATE actions SET status='updating' WHERE id=? AND run_id=?",
                (run["final_action"],), run_last=True))
            tx.execute("UPDATE runs SET final_action=NULL WHERE id=?", (run["id"],))
        self._event(tx, job["id"], "boundary_update", job["candidate_sha"] or job["id"])

    def _resync_state(self, tx: Transaction, subject: str) -> dict[str, Any]:
        allowance = tx.one(*run_statement(tx, 'SELECT * FROM resync_allowances WHERE subject=? AND current=1 AND run_id=?',
                        (subject,), run_last=True))
        snapshots = tx.rows(*run_statement(tx, 'SELECT * FROM integration_snapshots WHERE subject=? AND run_id=? ORDER BY window DESC LIMIT 1',
                        (subject,), run_last=True))
        snapshot = snapshots[0] if snapshots else None
        return {"generation": allowance["generation"], "used": allowance["used"], "limit": allowance["allowance"],
                "window": None if snapshot is None else snapshot["window"], "open": bool(snapshot and snapshot["open"]),
                "source_sha": None if snapshot is None else snapshot["source_sha"],
                "target_sha": None if snapshot is None else snapshot["target_sha"]}

    def _record_source_write(self, tx: Transaction, subject: str, ref: str, head: str) -> None:
        tx.execute(*run_statement(tx, 'UPDATE integration_snapshots SET source_sha=? WHERE subject=? AND source_ref=? AND window=(SELECT max(i.window) FROM integration_snapshots i WHERE i.subject=? AND i.run_id=integration_snapshots.run_id) AND run_id=?',
            (head, subject, ref, subject), run_last=True))

    def _renew_resync(self, tx: Transaction, job: sqlite3.Row, *, require_exhaustion: bool = True) -> None:
        previous = self._resync_state(tx, job["id"])
        if require_exhaustion and (previous["used"] < previous["limit"] or previous["open"]):
            raise KernelError("resync_allowance_not_exhausted")
        tx.execute(*run_statement(tx, 'UPDATE resync_allowances SET current=0 WHERE subject=? AND current=1 AND run_id=?',
            (job["id"],), run_last=True))
        tx.execute(*run_statement(tx, 'INSERT INTO resync_allowances(subject,generation,allowance,run_id) VALUES (?,?,?,?)',
            (job["id"], previous["generation"] + 1, self._subject_policy(tx, job).resync_limit), run_last=True))
        self._event(tx, job["id"], "resync_renewed", str(previous["generation"] + 1))

    def _observe_integration_snapshot(self, tx: Transaction, job: sqlite3.Row, world: World) -> bool:
        """Only an external change after a completed window spends allowance."""
        subject = job["id"]
        target = self._require_target(world)
        if world.source is None and job["pr_number"] is not None:
            self._gate(tx, subject, "published_head_changed")
            return False
        source_sha = job["source_sha"] if world.source is None else world.source.sha
        previous = tx.rows(*run_statement(tx, 'SELECT * FROM integration_snapshots WHERE subject=? AND run_id=? ORDER BY window DESC LIMIT 1',
                       (subject,), run_last=True))
        allowance = tx.one(*run_statement(tx, 'SELECT * FROM resync_allowances WHERE subject=? AND current=1 AND run_id=?',
                        (subject,), run_last=True))
        snapshot = previous[0] if previous else None
        first = snapshot is None or snapshot["source_ref"] != job["source_ref"] or snapshot["target_ref"] != job["target_ref"]
        source_changed = snapshot is not None and not first and snapshot["source_sha"] != source_sha
        target_changed = snapshot is not None and not first and snapshot["target_sha"] != target.sha
        if snapshot is not None and not first:
            for changed, before, after in ((source_changed, snapshot["source_sha"], source_sha),
                                           (target_changed, snapshot["target_sha"], target.sha)):
                if changed and (before, after) not in world.ancestry:
                    self._gate(tx, subject, "non_append_integration_history")
                    return False
        charged = bool(snapshot is not None and not first and (source_changed or target_changed) and not snapshot["open"])
        if charged and allowance["used"] >= allowance["allowance"]:
            self._gate(tx, subject, "integration_resync_exhausted")
            return False
        if first or charged:
            window = 1 if snapshot is None else snapshot["window"] + 1
            tx.execute(*run_statement(tx, 'INSERT INTO integration_snapshots(subject,window,allowance_generation,source_ref,target_ref,source_sha,target_sha,open,run_id) VALUES (?,?,?,?,?,?,?,1,?)',
                (subject, window, allowance["generation"], job["source_ref"], job["target_ref"], source_sha, target.sha), run_last=True))
            if charged:
                tx.execute(*run_statement(tx, 'UPDATE resync_allowances SET used=used+1 WHERE subject=? AND current=1 AND run_id=?',
                    (subject,), run_last=True))
        elif snapshot is not None and (source_changed or target_changed):
            tx.execute(*run_statement(tx, 'UPDATE integration_snapshots SET source_sha=?,target_sha=? WHERE subject=? AND window=? AND run_id=?',
                (source_sha, target.sha, subject, snapshot["window"]), run_last=True))
        if world.source is not None and source_sha != job["source_sha"]:
            tx.execute(*run_statement(tx, 'UPDATE subjects SET source_sha=? WHERE id=? AND run_id=?',
                (source_sha, subject), run_last=True))
        if first or source_changed or target_changed:
            evidence = tx.put(subject, "integration_snapshot", wire_json({"source_sha": source_sha, "target_sha": target.sha, "charged": charged}))
            self._event(tx, subject, "integration_snapshot", str(allowance["generation"]), evidence)
        return True

    def _acceptance_promoted(self, tx: Transaction, job: sqlite3.Row, world: World) -> None:
        """Record the actual full acceptance installation, beside its transition."""
        if (job["kind"] == DeliveryKind.TICKET or not job["integrated_sha"] or not job["integrated_tree"]
                or not self._review_current(tx, job, world)):
            return
        if job["kind"] == DeliveryKind.FINAL_RUN and (world.source is None or world.source.sha != job["source_sha"]):
            return
        material = tx.put(job["id"], "acceptance_promotion", wire_json({
            "review_attempt": job["review_attempt"], "promoted_sha": job["integrated_sha"],
            "promoted_tree": job["integrated_tree"], "source_sha": job["source_sha"],
        }))
        if not tx.rows(*run_statement(tx, "SELECT position FROM history WHERE subject=? AND generation=? AND kind='acceptance_promoted' AND identity=? AND material_id=? AND run_id=? LIMIT 1",
                   (job["id"], job["generation"], job["review_attempt"], material), run_last=True)):
            self._event(tx, job["id"], "acceptance_promoted", job["review_attempt"], material)

    def _review_current(self, tx: Transaction, job: sqlite3.Row, world: World) -> bool:
        if not job["review_attempt"] or world.target is None:
            return False
        review = tx.one(*run_statement(tx, 'SELECT * FROM attempts WHERE id=? AND run_id=?',
                     (job["review_attempt"],), run_last=True))
        return bool(review["outcome"] == "pass" and review["revision"] == job["revision"]
                and review["candidate_sha"] == job["candidate_sha"] and review["candidate_tree"] == job["candidate_tree"]
                and review["base_sha"] == self._require_target(world).sha and review["expected_tree"] == job["expected_tree"]
                and review["completion_revision"] == job["completion_revision"])

    @staticmethod
    def _pr_identity(run: sqlite3.Row, resource: sqlite3.Row, pr: PullRequest | Mapping[str, Any]) -> bool:
        value = asdict(pr) if isinstance(pr, PullRequest) else pr
        return bool(same_repository(value["repository"], run["repository"])
                    and same_repository(value.get("head_repository"), run["repository"])
                    and value["source_ref"] == resource["source_ref"] and value["target_ref"] == resource["target_ref"]
                    and (resource["pr_number"] is None or value["number"] == resource["pr_number"]))

    def _pr_write_current(self, run: sqlite3.Row, job: sqlite3.Row, world: World) -> bool:
        pr = world.pr
        if pr is None:
            return job["pr_number"] is None
        return bool(pr.head.sha == job["publication_sha"] and self._pr_target(run, job, world) is not None)

    @staticmethod
    def _pr_target(run: sqlite3.Row, resource: sqlite3.Row, world: World, *,
                   state: Literal["OPEN", "CLOSED"] = "OPEN") -> Commit | None:
        return None if world.pr is None else pr_target(world.pr,
            repository=run["repository"], pr_number=resource["pr_number"],
            source_ref=resource["source_ref"], target_ref=resource["target_ref"],
            source=world.source, target=world.target, ancestry=world.ancestry, state=state)

    def _published_matches(self, run: sqlite3.Row, job: sqlite3.Row, world: World) -> bool:
        pr = world.pr
        return (world.target is not None and pr is not None and pr.number == job["pr_number"]
                and pr.head.sha == job["publication_sha"] and pr.head.tree == job["publication_tree"]
                and world.target.sha == job["integration_target"] and self._pr_target(run, job, world) is not None)

    def _published(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World) -> bool:
        if not self._published_matches(run, job, world):
            self._gate(tx, job["id"], "published_head_changed")
            return False
        return True

    def _merge_authority(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World) -> bool:
        checks = world.checks
        if (not self._published_matches(run, job, world) or checks is None or checks.pr_number != job["pr_number"] or checks.head_sha != job["publication_sha"]
                or checks.result not in {"pass", "none"} or not job["checks_id"]):
            return False
        recorded = tx.one(*run_statement(tx, 'SELECT material_id FROM checks WHERE id=? AND run_id=?',
                       (job["checks_id"],), run_last=True))
        if not same_required_checks(json.loads(tx.get(recorded[0], job["id"])), checks):
            return False
        if job["kind"] == DeliveryKind.TICKET:
            return job["gate_mode"] in {"review", "deterministic_fallback", "ci_waiver"}
        if not job["grant_id"] or not self._review_current(tx, job, world):
            return False
        grant = tx.one(*run_statement(tx, 'SELECT * FROM grants WHERE id=? AND run_id=?',
                    (job["grant_id"],), run_last=True))
        return bool(grant["pr_number"] == job["pr_number"] and grant["head_sha"] == job["publication_sha"]
                and grant["base_sha"] == self._require_target(world).sha and grant["review_attempt"] == job["review_attempt"]
                and grant["checks_id"] == job["checks_id"] and grant["revision"] == job["revision"]
                and grant["parent_revision"] == run["parent_revision"] and grant["graph_revision"] == run["graph_revision"]
                and grant["completion_revision"] == job["completion_revision"])

    def _event(self, tx: Transaction, subject: str, kind: str, event_id: str, material: str | None = None, *, generation: int | None = None) -> None:
        job = tx.one(*run_statement(tx, 'SELECT generation,phase,purpose,current_attempt FROM subjects WHERE id=? AND run_id=?',
                  (subject,), run_last=True))
        if generation is None:
            generation = job["generation"]
        status = None
        if generation == job["generation"]:
            run = tx.one(*run_statement(tx, 'SELECT stopped,blocked FROM runs WHERE id=?',
                      (), run_last=True))
            status = wire_json({"stopped": bool(run["stopped"]), "blocked": run["blocked"],
                                "gates": [row["code"] for row in self._gates(tx, subject)],
                                "attempt_status": None if job["current_attempt"] is None else
                                tx.one(*run_statement(tx, 'SELECT status FROM attempts WHERE id=? AND run_id=?',
                                    (job["current_attempt"],), run_last=True))[0]}).decode()
        tx.execute(*run_statement(tx, 'INSERT INTO history(subject,kind,identity,material_id,generation,created_at,phase,purpose,status,run_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (subject, kind, event_id, material, generation, self.clock(),
                    job["phase"] if status is not None else None, job["purpose"] if status is not None else None, status), run_last=True))
