"""One authoritative transaction interpreter for delivery work.

All transition, currentness, budget, FIFO and mutation-authority choices are
made here from current SQL facts. The external driver only executes the one
returned request. A raw return and its business consumption commit separately.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
from uuid import uuid4

from .model import (
    AgentCall, Commit, DeliveryKind, Effect, EffectResult, Execution, KernelError,
    Policy, Profile, Purpose, Role, RunSpec, Step, World, artifact, review_outcome, wire_json,
)
from .storage import CURRENT_LIMIT, Storage, Transaction


def identity() -> str:
    return uuid4().hex


class Kernel:
    def __init__(self, path: Path) -> None:
        self.storage = Storage(path)
        self.clock: Callable[[], float] = time.time

    @classmethod
    def create(cls, path: Path, spec: RunSpec) -> Kernel:
        if not spec.deliveries or len({delivery.key for delivery in spec.deliveries}) != len(spec.deliveries):
            raise ValueError("one or more uniquely identified deliveries are required")
        storage = Storage.create(path)
        with storage.transaction() as tx:
            tx.execute("INSERT INTO runs(id,repository,parent_issue,parent_revision,graph_revision,concurrency,policy) VALUES (?,?,?,?,?,?,?)",
                       (spec.run_id, spec.repository, spec.parent_issue, spec.parent_revision,
                        spec.graph_revision, spec.policy.concurrency, wire_json(asdict(spec.policy))))
            for delivery in spec.deliveries:
                final = delivery.kind == DeliveryKind.FINAL_RUN
                tx.execute("""INSERT INTO subjects(
                    id,run_id,kind,issue,revision,phase,purpose,source_ref,target_ref,
                    base_sha,base_tree,source_sha,candidate_sha,candidate_tree,completion_revision,checkout,workspace_sha)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (delivery.key, spec.run_id, delivery.kind.value, delivery.issue, delivery.revision,
                            "integration" if final else "development",
                            Purpose.INTEGRATION.value if final else Purpose.REQUIREMENTS.value,
                            delivery.source_ref, delivery.target_ref, delivery.base.sha, delivery.base.tree,
                            delivery.base.sha, delivery.base.sha if final else None,
                            delivery.base.tree if final else None, delivery.completion_revision,
                            delivery.checkout, delivery.base.sha))
                limits = {
                    Purpose.REQUIREMENTS: (spec.policy.ticket_rounds + 1, spec.policy.ticket_rounds)
                    if delivery.kind == DeliveryKind.TICKET else (spec.policy.parent_rounds, spec.policy.parent_rounds),
                    Purpose.INTEGRATION: (spec.policy.run_repairs, spec.policy.run_repairs + 1)
                    if final else (spec.policy.run_repairs if delivery.kind == DeliveryKind.PARENT_ONLY else spec.policy.integration_developments,
                                   0 if delivery.kind == DeliveryKind.TICKET else None),
                    Purpose.CI: (spec.policy.ci_developments, None),
                }
                for purpose, (development_limit, review_limit) in limits.items():
                    tx.execute("INSERT INTO budgets(subject,purpose,window,development_limit,review_limit) VALUES (?,?,1,?,?)",
                               (delivery.key, purpose.value, development_limit, review_limit))
            for delivery in spec.deliveries:
                for predecessor in delivery.dependencies:
                    tx.execute("INSERT INTO dependencies VALUES (?,?)", (delivery.key, predecessor))
        return cls(path)

    def claim_executor(self, owner: str) -> Execution:
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            if run["stopped"]:
                raise KernelError("operator_stopped")
            if tx.rows("SELECT owner FROM retired_executors WHERE run_id=? AND status='pending'", (run["id"],)):
                raise KernelError("executor_exit_unproven")
            if run["executor"] is not None and run["executor"] != owner:
                raise KernelError("executor_exit_unproven")
            epoch = run["epoch"] if run["executor"] == owner else run["epoch"] + 1
            tx.execute("UPDATE runs SET executor=?,epoch=?", (owner, epoch))
            return Execution(run["id"], owner, epoch)

    def confirm_executor_exit(self, execution: Execution, exited_invocations: tuple[str, ...] = ()) -> None:
        """Consume exact Host exit evidence; no PID guessing or replacement claim.

        The caller must have observed the old Executor and all listed Workers
        exit. The isolated driver supplies synthetic Host evidence; a real Host
        adapter is deliberately outside this exploration's implemented surface.
        """
        with self.storage.transaction() as tx:
            retired = tx.one("SELECT * FROM retired_executors WHERE run_id=? AND owner=? AND epoch=?",
                             (execution.run_id, execution.owner, execution.epoch))
            if retired["status"] == "exited":
                return
            leases = tx.rows("SELECT * FROM leases WHERE executor=? AND epoch=?", (execution.owner, execution.epoch))
            if {row["invocation"] for row in leases} != set(exited_invocations):
                raise KernelError("worker_exit_unproven")
            for lease in leases:
                tx.execute("UPDATE invocations SET status='interrupted' WHERE id=? AND status='running'", (lease["invocation"],))
                tx.execute("DELETE FROM leases WHERE id=? AND invocation=?", (lease["id"], lease["invocation"]))
            tx.execute("UPDATE retired_executors SET status='exited' WHERE run_id=? AND owner=? AND epoch=?",
                       (execution.run_id, execution.owner, execution.epoch))
            tx.execute("UPDATE runs SET executor=NULL WHERE id=? AND executor=? AND epoch>?",
                       (execution.run_id, execution.owner, execution.epoch))

    def advance(self, subject: str, execution: Execution, world: World, *, allow_agent: bool = True) -> Step:
        with self.storage.transaction() as tx:
            run, job = self._owned(tx, subject, execution)
            pending = self._pending_effect(tx, subject)
            # An unknown write is read back before scope drift or any new work.
            if pending is not None:
                if pending["status"] == "sent":
                    return Step("reconcile", subject, effect=self._effect(tx, pending, execution))
                if run["stopped"] or run["blocked"] or self._gates(tx, subject):
                    return Step("paused", subject, self._pause_reason(tx, subject, run))
                if not self._current(tx, job, run, world):
                    return Step("paused", subject, "currentness")
                if pending["status"] == "observed":
                    return self._apply_effect(tx, run, job, pending, world)
                return Step("effect", subject, effect=self._effect(tx, pending, execution))
            if run["stopped"] or run["blocked"] or self._gates(tx, subject):
                return Step("paused", subject, self._pause_reason(tx, subject, run))
            if job["phase"] == "complete":
                return Step("complete", subject)
            dependencies = tx.rows("SELECT s.phase FROM dependencies d JOIN subjects s ON s.id=d.predecessor WHERE d.subject=?", (subject,))
            if any(row[0] != "complete" for row in dependencies):
                return Step("waiting", subject, "dependencies")
            if job["current_attempt"]:
                attempt = tx.one("SELECT * FROM attempts WHERE id=?", (job["current_attempt"],))
                if attempt["result_id"]:
                    return self._consume(tx, run, job, attempt, world)
                if not self._current(tx, job, run, world):
                    return Step("paused", subject, "currentness")
                invocation = tx.one("SELECT * FROM invocations WHERE id=?", (attempt["current_invocation"],))
                if invocation["status"] == "running":
                    return Step("waiting", subject, "agent_running")
                return self._allocate(tx, run, job, execution, world, allow_agent, attempt)
            if not self._current(tx, job, run, world):
                return Step("paused", subject, "currentness")
            if job["phase"] in {"development", "review", "publish"}:
                return self._allocate(tx, run, job, execution, world, allow_agent)
            return self._mechanical(tx, run, job, execution, world)

    def receive(self, call: AgentCall, raw: bytes) -> None:
        """Durable raw/exit settlement is permitted after Stop or owner retirement.

        It cannot advance phase, consume a budget, clear a gate, or allocate work.
        Both the attempt and the exact invocation/thread/lease must still match.
        """
        with self.storage.transaction() as tx:
            invocation = tx.one("SELECT i.*,a.subject,a.generation FROM invocations i JOIN attempts a ON a.id=i.attempt WHERE i.id=?", (call.invocation_id,))
            if (invocation["attempt"] != call.attempt_id or invocation["subject"] != call.subject
                    or invocation["thread"] != call.thread_id or invocation["lease"] != call.lease_id
                    or invocation["executor"] != call.execution.owner or invocation["epoch"] != call.execution.epoch):
                raise KernelError("invocation_binding")
            material = tx.put(call.subject, "agent_raw", raw)
            if invocation["raw_id"] is not None:
                if invocation["raw_id"] != material:
                    raise KernelError("conflicting_return")
                return
            if invocation["status"] not in {"running", "interrupted"}:
                raise KernelError("invocation_already_settled")
            tx.execute("UPDATE invocations SET raw_id=?,status='returned' WHERE id=?", (material, call.invocation_id))
            # A late, superseded invocation is history only, never current work.
            tx.execute("UPDATE attempts SET result_id=?,status='returned' WHERE id=? AND current_invocation=?",
                       (material, call.attempt_id, call.invocation_id))
            tx.execute("DELETE FROM leases WHERE id=? AND invocation=?", (call.lease_id, call.invocation_id))
            self._event(tx, call.subject, "raw_returned", call.invocation_id, material)

    def bind_thread(self, call: AgentCall, reported_thread: str) -> None:
        """Persist a real backend thread.started event, separate from logical ID."""
        if not reported_thread:
            raise KernelError("missing_thread")
        with self.storage.transaction() as tx:
            invocation = tx.one("SELECT attempt,thread,lease FROM invocations WHERE id=?", (call.invocation_id,))
            if tuple(invocation) != (call.attempt_id, call.thread_id, call.lease_id):
                raise KernelError("invocation_binding")
            thread = tx.one("SELECT backend_id FROM threads WHERE id=?", (call.thread_id,))
            if thread[0] is not None and thread[0] != reported_thread:
                raise KernelError("thread_identity_changed")
            tx.execute("UPDATE threads SET backend_id=? WHERE id=?", (reported_thread, call.thread_id))

    def guard_effect(self, effect: Effect, execution: Execution, world: World) -> None:
        """Read-only authority guard for each real successor mutation in an adapter."""
        with self.storage.transaction(write=False) as tx:
            run, job = self._owned(tx, effect.subject, execution)
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            if (row["status"] != "sent" or run["stopped"] or run["blocked"] or self._gates(tx, effect.subject)
                    or row["generation"] != job["generation"] or row["revision"] != world.revision
                    or row["parent_revision"] != world.parent_revision or row["graph_revision"] != world.graph_revision
                    or job["completion_revision"] != world.completion_revision
                    or self._effect(tx, row, execution) != effect):
                raise KernelError("operation_cancelled")
            if effect.operation in {"integrate", "run_branch", "publication_commit", "push", "pr_write", "merge"} and effect.base.sha != world.target.sha:
                raise KernelError("operation_boundary_changed")
            if effect.operation == "merge" and not self._merge_authority(tx, run, job, world):
                raise KernelError("merge_authority_changed")

    def control(self, kind: str, action_id: str, *, subject: str | None = None,
                world: World | None = None, message: str | None = None, new_thread: bool = False) -> str:
        """Apply one explicit operator intent atomically, with its application receipt.

        The offline prototype calls this trusted interface directly. Host launch,
        public capability isolation and process-exit proofs are separate adapters.
        """
        if kind not in {"stop", "resume", "approve"}:
            raise KernelError("prototype_control_unimplemented", kind)
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            material_owner = subject or tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
            payload = wire_json({"kind": kind, "subject": subject, "message": message, "new_thread": new_thread})
            prior = tx.rows("SELECT * FROM actions WHERE id=?", (action_id,))
            if prior:
                if tx.get(prior[0]["payload_id"], material_owner) != payload:
                    raise KernelError("action_identity_changed")
                return str(prior[0]["status"])
            if run["final_action"]:
                raise KernelError("final_approval_noncancellable")
            job = None if subject is None else self._job(tx, subject)
            payload_id = tx.put(material_owner, "control_intent", payload)
            tx.execute("INSERT INTO actions(id,run_id,kind,subject,payload_id,status) VALUES (?,?,?,?,?,'applied')",
                       (action_id, run["id"], kind, subject, payload_id))
            if kind == "stop":
                if job is None:
                    if not run["stopped"] and run["executor"] is not None:
                        tx.execute("INSERT INTO retired_executors VALUES (?,?,?,'pending')",
                                   (run["id"], run["executor"], run["epoch"]))
                        tx.execute("UPDATE runs SET stopped=1,epoch=epoch+1 WHERE id=?", (run["id"],))
                    else:
                        tx.execute("UPDATE runs SET stopped=1 WHERE id=?", (run["id"],))
                    targets = tx.rows("SELECT id FROM subjects WHERE run_id=?", (run["id"],))
                else:
                    assert subject is not None  # job exists only for an explicit subject.
                    self._gate(tx, subject, "operator_stopped")
                    tx.execute("DELETE FROM work_queue WHERE subject=?", (subject,))
                    targets = [job]
                for target in targets:
                    target_id = target["id"]
                    tx.execute("UPDATE effects SET status='cancelled' WHERE subject=? AND status='prepared'", (target_id,))
                    self._release_integration(tx, target_id)
            elif kind == "resume":
                if job is None:
                    if run["executor"] is not None:
                        raise KernelError("executor_exit_unproven")
                    if tx.one("SELECT count(*) FROM leases")[0]:
                        raise KernelError("worker_exit_unproven")
                    if not run["stopped"]:
                        raise KernelError("no_resumable_boundary")
                    tx.execute("UPDATE runs SET stopped=0 WHERE id=?", (run["id"],))
                else:
                    assert subject is not None
                    if tx.rows("SELECT id FROM leases WHERE subject=?", (subject,)):
                        raise KernelError("worker_exit_unproven")
                    gates = self._gates(tx, subject)
                    if not gates:
                        raise KernelError("no_resumable_boundary")
                    if message is not None:
                        if (not any(gate["code"] == "human_blocker" for gate in gates)
                                or not message.strip() or len(message.encode()) > 8192):
                            raise KernelError("human_response_boundary")
                        response = tx.put(subject, "human_response", message.encode())
                        self._event(tx, subject, "human_response", action_id, response)
                    for gate in gates:
                        code = gate["code"]
                        if code in {"review_budget_exhausted", "modification_budget_exhausted", "ci_development_budget_exhausted"}:
                            self._renew(tx, run, job, gate["purpose"])
                        elif code == "human_blocker":
                            if job["current_attempt"]:
                                tx.execute("UPDATE attempts SET result_id=NULL,status='continuing' WHERE id=?", (job["current_attempt"],))
                        elif code == "publication_retry_exhausted":
                            tx.execute("UPDATE effects SET attempts=0 WHERE subject=? AND status='sent'", (subject,))
                        elif code == "write_conflict":
                            pending_write = self._pending_effect(tx, subject)
                            if pending_write is None or pending_write["status"] != "observed":
                                raise KernelError("unresolved_write_conflict")
                        elif code not in {"operator_stopped", "execution_failed", "write_result_unknown", "ci_investigation_paused", "supervision_timeout"}:
                            continue
                        tx.execute("DELETE FROM gates WHERE subject=? AND code=?", (subject, code))
                    if new_thread and job["current_attempt"]:
                        tx.execute("UPDATE attempts SET new_thread=1 WHERE id=?", (job["current_attempt"],))
                    if job["phase"] in {"development", "review", "publish"}:
                        self._ready(tx, subject, "work_queue")
                    tx.execute("DELETE FROM waits WHERE subject=?", (subject,))
                    if job["grant_id"] and job["phase"] in {"merge", "completion_comment", "close"}:
                        tx.execute("UPDATE runs SET final_action=? WHERE id=?", (action_id, run["id"]))
                        tx.execute("UPDATE actions SET status='pending' WHERE id=?", (action_id,))
            else:
                if job is None or world is None or job["phase"] != "approval" or job["kind"] == DeliveryKind.TICKET:
                    raise KernelError("no_approval_boundary")
                assert subject is not None
                if (run["stopped"] or run["blocked"] or self._gates(tx, subject)
                        or not self._current(tx, job, run, world) or not self._published(tx, run, job, world)
                        or not self._review_current(tx, job, world)):
                    raise KernelError("approval_currentness")
                checks = world.checks
                if (checks is None or checks.result not in {"none", "pass"} or checks.head_sha != job["publication_sha"]
                        or checks.pr_number != job["pr_number"] or not job["checks_id"]):
                    raise KernelError("approval_checks")
                check = tx.one("SELECT material_id FROM checks WHERE id=?", (job["checks_id"],))
                if wire_json(json.loads(tx.get(check[0], subject))) != wire_json(asdict(checks)):
                    raise KernelError("approval_checks_changed")
                grant_id = identity()
                tx.execute("INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (grant_id, subject, action_id, job["pr_number"], job["publication_sha"], world.target.sha,
                            job["review_attempt"], job["checks_id"], job["revision"], run["parent_revision"],
                            run["graph_revision"], job["completion_revision"]))
                tx.execute("UPDATE subjects SET phase='merge',grant_id=?,version=version+1 WHERE id=?", (grant_id, subject))
                tx.execute("UPDATE runs SET final_action=? WHERE id=?", (action_id, run["id"]))
                tx.execute("UPDATE actions SET status='pending' WHERE id=?", (action_id,))
            receipt = tx.put(material_owner, "control_receipt", wire_json({"action": action_id, "kind": kind, "applied": True}))
            tx.execute("UPDATE actions SET receipt_id=? WHERE id=?", (receipt, action_id))
            self._event(tx, material_owner, "control_applied", action_id, receipt)
            return "pending" if kind == "approve" else "applied"

    def _renew(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, purpose: str) -> None:
        old = tx.one("SELECT * FROM budgets WHERE subject=? AND purpose=? AND current=1", (job["id"], purpose))
        tx.execute("UPDATE budgets SET current=0 WHERE subject=? AND purpose=? AND window=?", (job["id"], purpose, old["window"]))
        developments, reviews, limit = 0, 0, old["development_limit"]
        if purpose == Purpose.CI:
            reviews = old["reviews_used"]  # CI Reviewer count is cumulative, not a renewable quota.
        if purpose == Purpose.INTEGRATION and job["kind"] == DeliveryKind.FINAL_RUN:
            developments = 0 if old["developments_used"] >= old["development_limit"] else old["developments_used"]
            reviews = 0 if old["review_limit"] is not None and old["reviews_used"] >= old["review_limit"] else old["reviews_used"]
        if purpose == Purpose.INTEGRATION and job["kind"] != DeliveryKind.FINAL_RUN:
            # This policy extends an accumulated allowance instead of erasing use.
            developments, reviews = old["developments_used"], old["reviews_used"]
            policy = self._policy(run)
            limit += policy.run_repairs if job["kind"] == DeliveryKind.PARENT_ONLY else policy.integration_developments
        tx.execute("INSERT INTO budgets VALUES (?,?,?,?,?,?,?,1)",
                   (job["id"], purpose, old["window"] + 1, developments, reviews, limit, old["review_limit"]))
        self._event(tx, job["id"], "budget_renewed", f"{purpose}:{old['window'] + 1}")

    def status(self, subject: str) -> dict[str, Any]:
        with self.storage.transaction(write=False) as tx:
            job = self._job(tx, subject)
            run = tx.one("SELECT * FROM runs WHERE id=?", (job["run_id"],))
            budgets = tx.rows("SELECT * FROM budgets WHERE subject=? AND current=1", (subject,))
            pending = None if not job["current_attempt"] else tx.one("SELECT * FROM attempts WHERE id=?", (job["current_attempt"],))
            return {
                "subject": subject, "kind": job["kind"], "phase": job["phase"], "purpose": job["purpose"],
                "generation": job["generation"], "version": job["version"],
                "pending_attempt": job["current_attempt"], "pending_return": bool(pending and pending["result_id"]),
                "candidate": job["candidate_sha"], "pr_number": job["pr_number"], "gate_mode": job["gate_mode"],
                "gates": [row["code"] for row in self._gates(tx, subject)], "stopped": bool(run["stopped"]),
                "holds_integration": run["integration_holder"] == subject,
                "pending_write": None if (effect := self._pending_effect(tx, subject)) is None
                else {"operation": effect["operation"], "status": effect["status"], "id": effect["id"]},
                "budgets": {row["purpose"]: {key: row[key] for key in ("window", "developments_used", "reviews_used", "development_limit", "review_limit")} for row in budgets},
            }

    def scheduling(self) -> dict[str, Any]:
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT concurrency,integration_holder FROM runs")
            return {"concurrency": run["concurrency"], "integration_holder": run["integration_holder"],
                    "active": [dict(row) for row in tx.rows("SELECT id,subject,invocation FROM leases ORDER BY subject")],
                    "work_fifo": [row[0] for row in tx.rows("SELECT subject FROM work_queue ORDER BY ready")],
                    "integration_fifo": [row[0] for row in tx.rows("SELECT subject FROM integration_queue ORDER BY ready")]}

    def history(self, subject: str, *, after: int = 0, limit: int = 128) -> list[dict[str, Any]]:
        if not 1 <= limit <= 128:
            raise KernelError("query_limit")
        with self.storage.transaction(write=False) as tx:
            self._job(tx, subject)
            rows = tx.rows("SELECT * FROM history WHERE subject=? AND position>? ORDER BY position LIMIT ?", (subject, after, limit))
            return [dict(row) for row in rows]

    def _owned(self, tx: Transaction, subject: str, execution: Execution) -> tuple[sqlite3.Row, sqlite3.Row]:
        job = self._job(tx, subject)
        run = tx.one("SELECT * FROM runs WHERE id=?", (job["run_id"],))
        if (run["id"] != execution.run_id or run["executor"] != execution.owner or run["epoch"] != execution.epoch):
            raise KernelError("executor_fenced")
        return run, job

    def _job(self, tx: Transaction, subject: str) -> sqlite3.Row:
        row = tx.one("SELECT * FROM subjects WHERE id=?", (subject,))
        if sum(len(value.encode()) if isinstance(value, str) else 16 for value in row) > CURRENT_LIMIT:
            raise KernelError("current_record_limit")
        return row

    def _gates(self, tx: Transaction, subject: str) -> list[sqlite3.Row]:
        return tx.rows("SELECT * FROM gates WHERE subject=? ORDER BY code", (subject,))

    def _pause_reason(self, tx: Transaction, subject: str, run: sqlite3.Row) -> str:
        return "operator_stopped" if run["stopped"] else (run["blocked"] or ",".join(row["code"] for row in self._gates(tx, subject)))

    def _gate(self, tx: Transaction, subject: str, code: str, *, purpose: str | None = None, evidence: str | None = None) -> None:
        tx.execute("INSERT OR IGNORE INTO gates VALUES (?,?,?,?)", (subject, code, purpose, evidence))
        self._event(tx, subject, "paused", code, evidence)

    def _current(self, tx: Transaction, job: sqlite3.Row, run: sqlite3.Row, world: World) -> bool:
        if world.graph_revision != run["graph_revision"]:
            tx.execute("UPDATE runs SET blocked='unsupported_scope_change' WHERE id=?", (run["id"],))
            self._gate(tx, job["id"], "unsupported_scope_change")
            return False
        if world.parent_revision != run["parent_revision"] or world.revision != job["revision"]:
            self._gate(tx, job["id"], "requeue_required")
            return False
        if world.completion_revision != job["completion_revision"]:
            self._gate(tx, job["id"], "completion_changed")
            return False
        return True

    def _policy(self, run: sqlite3.Row) -> Policy:
        data = json.loads(run["policy"])
        for field in ("development_profile", "review_profile", "publication_profile"):
            data[field] = Profile(**data[field])
        return Policy(**data)

    def _budget(self, tx: Transaction, job: sqlite3.Row) -> sqlite3.Row:
        return tx.one("SELECT * FROM budgets WHERE subject=? AND purpose=? AND current=1", (job["id"], job["purpose"]))

    def _ready(self, tx: Transaction, subject: str, table: str) -> None:
        if table not in {"work_queue", "integration_queue"}:
            raise AssertionError(table)
        if not tx.rows(f"SELECT ready FROM {table} WHERE subject=?", (subject,)):
            tx.execute("UPDATE runs SET next_ready=next_ready+1")
            ready = tx.one("SELECT next_ready FROM runs")[0]
            tx.execute(f"INSERT INTO {table} VALUES (?,?)", (subject, ready))

    def _allocate(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, execution: Execution,
                  world: World, allow_agent: bool, pending: sqlite3.Row | None = None) -> Step:
        subject = job["id"]
        if not allow_agent:
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
        first = tx.one("SELECT subject FROM work_queue ORDER BY ready LIMIT 1")[0]
        count = tx.one("SELECT count(*) FROM leases")[0]
        if first != subject or count >= run["concurrency"]:
            return Step("waiting", subject, "agent_capacity")
        tx.execute("DELETE FROM work_queue WHERE subject=?", (subject,))
        policy = self._policy(run)
        profile = {Role.DEVELOPMENT: policy.development_profile, Role.REVIEW: policy.review_profile,
                   Role.PUBLICATION: policy.publication_profile}[role]
        attempt_id = pending["id"] if pending is not None else identity()
        thread_id = None
        if pending is not None and not pending["new_thread"]:
            previous = tx.one("SELECT thread FROM invocations WHERE id=?", (pending["current_invocation"],))
            thread_id = previous["thread"]
        elif pending is None and role == Role.DEVELOPMENT and not policy.new_thread_per_development:
            thread_id = job["development_thread"]
        if thread_id is None:
            thread_id = identity()
            tx.execute("INSERT INTO threads(id,subject,role,model,effort) VALUES (?,?,?,?,?)", (thread_id, subject, role.value, profile.model, profile.effort))
        else:
            binding = tx.one("SELECT * FROM threads WHERE id=?", (thread_id,))
            if binding["subject"] != subject or binding["role"] != role.value:
                raise KernelError("thread_role_binding")
            profile = Profile(binding["model"], binding["effort"])
        if role == Role.DEVELOPMENT:
            tx.execute("UPDATE subjects SET development_thread=? WHERE id=?", (thread_id, subject))
        context = self._context(tx, run, job, world, role)
        if pending is None:
            ordinal = tx.one("SELECT coalesce(max(ordinal),0)+1 FROM attempts WHERE subject=?", (subject,))[0]
            context_id = tx.put(subject, "role_context", wire_json(context))
            tx.execute("""INSERT INTO attempts(id,subject,generation,ordinal,role,purpose,window,parent_revision,
                graph_revision,revision,base_sha,candidate_sha,candidate_tree,expected_tree,completion_revision,
                context_id,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'allocated')""",
                       (attempt_id, subject, job["generation"], ordinal, role.value, job["purpose"], budget["window"],
                        run["parent_revision"], run["graph_revision"], job["revision"], context["base_sha"],
                        job["candidate_sha"], job["candidate_tree"], job["expected_tree"],
                        job["completion_revision"], context_id))
            if role == Role.DEVELOPMENT:
                tx.execute("UPDATE budgets SET developments_used=developments_used+1 WHERE subject=? AND purpose=? AND window=?",
                           (subject, job["purpose"], budget["window"]))
        else:
            # A continuation retains the semantic input, adding only immutable human responses.
            context = json.loads(tx.get(pending["context_id"], subject))
            responses = tx.rows("SELECT material_id FROM history WHERE subject=? AND kind='human_response' ORDER BY position", (subject,))
            context["human_responses"] = [tx.get(row[0], subject).decode() for row in responses]
        invocation_id, lease_id = identity(), identity()
        context_id = tx.put(subject, "role_context", wire_json(context))
        tx.execute("INSERT INTO invocations(id,attempt,thread,context_id,executor,epoch,lease,status) VALUES (?,?,?,?,?,?,?,'running')",
                   (invocation_id, attempt_id, thread_id, context_id, execution.owner, execution.epoch, lease_id))
        tx.execute("UPDATE attempts SET current_invocation=?,status='running',new_thread=0 WHERE id=?", (invocation_id, attempt_id))
        tx.execute("INSERT INTO leases VALUES (?,?,?,?,?)", (lease_id, subject, invocation_id, execution.owner, execution.epoch))
        tx.execute("UPDATE subjects SET current_attempt=?,version=version+1 WHERE id=?", (attempt_id, subject))
        self._event(tx, subject, "agent_started", invocation_id)
        backend_thread = tx.one("SELECT backend_id FROM threads WHERE id=?", (thread_id,))[0]
        call = AgentCall(attempt_id, invocation_id, subject, role, Purpose(job["purpose"]), thread_id,
                         lease_id, execution, context, profile, "fresh" if backend_thread is None else "resume", backend_thread)
        return Step("agent", subject, call=call)

    def _context(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World, role: Role) -> dict[str, Any]:
        context: dict[str, Any] = {
            "repository": run["repository"], "parent_issue": run["parent_issue"], "issue": job["issue"],
            "scope": job["kind"], "generation": job["generation"], "role": role.value,
            "purpose": job["purpose"], "revision": job["revision"], "parent_revision": run["parent_revision"],
            "repair_source": job["repair_source"],
            "graph_revision": run["graph_revision"], "completion_revision": job["completion_revision"],
            "base_sha": job["base_sha"] if job["purpose"] == Purpose.REQUIREMENTS else world.target.sha,
            "candidate_sha": job["candidate_sha"], "candidate_tree": job["candidate_tree"], "checkout": job["checkout"],
            "expected_tree": job["expected_tree"], "full_scope": job["kind"],
        }
        for key in ("last_failure", "ci_failure"):
            if job[key]:
                context[key] = json.loads(tx.get(job[key], job["id"]))
        if job["review_attempt"]:
            previous = tx.one("SELECT * FROM attempts WHERE id=?", (job["review_attempt"],))
            context["previous_review"] = {"attempt": previous["id"], "base": previous["base_sha"],
                                          "candidate": previous["candidate_sha"],
                                          "artifact": json.loads(tx.get(previous["result_id"], job["id"]))}
        return context

    def _consume(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, attempt: sqlite3.Row, world: World) -> Step:
        subject = job["id"]
        role = Role(attempt["role"])
        try:
            result = artifact(role, tx.get(attempt["result_id"], subject))
        except KernelError:
            self._gate(tx, subject, "invalid_artifact", evidence=attempt["result_id"])
            return Step("paused", subject, "invalid_artifact")
        if role == Role.REVIEW and not attempt["accounted"]:
            tx.execute("UPDATE budgets SET reviews_used=reviews_used+1 WHERE subject=? AND purpose=? AND window=?",
                       (subject, attempt["purpose"], attempt["window"]))
            tx.execute("UPDATE attempts SET accounted=1 WHERE id=?", (attempt["id"],))
        if (not self._current(tx, job, run, world)
                or attempt["generation"] != job["generation"]
                or attempt["candidate_sha"] != job["candidate_sha"]):
            tx.execute("UPDATE attempts SET status='stale' WHERE id=?", (attempt["id"],))
            self._gate(tx, subject, "stale_return", evidence=attempt["result_id"])
            return Step("paused", subject, "stale_return")
        outcome = review_outcome(result) if role == Role.REVIEW else result["result_kind"]
        if outcome in {"human_blocker", "blocked"}:
            tx.execute("UPDATE attempts SET status='human',outcome=? WHERE id=?", (outcome, attempt["id"]))
            self._gate(tx, subject, "human_blocker", evidence=attempt["result_id"])
            return Step("paused", subject, "human_blocker")
        if role == Role.DEVELOPMENT:
            phase = "candidate"
        elif role == Role.REVIEW:
            tx.execute("UPDATE subjects SET review_attempt=? WHERE id=?", (attempt["id"], subject))
            if outcome == "fail":
                tx.execute("UPDATE subjects SET last_failure=? WHERE id=?", (attempt["result_id"], subject))
                tx.execute("UPDATE subjects SET repair_source=? WHERE id=?", ("required_checks" if job["purpose"] == Purpose.CI else "acceptance", subject))
                phase = "development"
                self._release_integration(tx, subject)
            else:
                phase = "integration" if not job["integrated_sha"] else "ready_publication"
                tx.execute("UPDATE subjects SET gate_mode='review' WHERE id=?", (subject,))
        else:
            phase = "publication_commit"
            tx.execute("UPDATE subjects SET publication_attempt=? WHERE id=?", (attempt["id"], subject))
        tx.execute("UPDATE attempts SET status='consumed',outcome=? WHERE id=?", (outcome, attempt["id"]))
        tx.execute("UPDATE subjects SET current_attempt=NULL,phase=?,version=version+1 WHERE id=?", (phase, subject))
        self._event(tx, subject, "role_consumed", attempt["id"], attempt["result_id"])
        return Step("progress", subject)

    def _pending_effect(self, tx: Transaction, subject: str) -> sqlite3.Row | None:
        rows = tx.rows("SELECT * FROM effects WHERE subject=? AND status IN ('prepared','sent','observed')", (subject,))
        return rows[0] if rows else None

    def _effect(self, tx: Transaction, row: sqlite3.Row, execution: Execution) -> Effect:
        return Effect(row["id"], row["subject"], row["operation"],
                      tx.one("SELECT repository FROM runs")[0], row["source_ref"], row["target_ref"],
                      row["before_sha"], row["after_sha"],
                      None if row["candidate_sha"] is None else Commit(row["candidate_sha"], row["candidate_tree"]),
                      Commit(row["base_sha"], row["base_tree"]), row["pr_number"], row["actor"],
                      json.loads(tx.get(row["payload_id"], row["subject"])), execution)

    def _mechanical(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, execution: Execution, world: World) -> Step:
        subject, phase = job["id"], job["phase"]
        if (phase in {"ready_publication", "publication_commit", "push", "pr", "checks", "approval", "merge"}
                and job["integration_target"] and job["integration_target"] != world.target.sha):
            self._update_boundary(tx, run, job)
            return Step("progress", subject, "updating")
        if phase == "candidate":
            return self._prepare_effect(tx, run, job, execution, world, "candidate", before=job["workspace_sha"],
                                        payload={"checkout": job["checkout"], "summary_attempt": self._last_attempt(tx, subject, Role.DEVELOPMENT)})
        if phase == "integration":
            if not self._integration_seat(tx, run, job):
                return Step("waiting", subject, "integration_seat")
            return self._prepare_effect(tx, run, job, execution, world, "integrate",
                                        before=world.target.sha,
                                        payload={"checkout": job["checkout"], "source_sha": None if world.source is None else world.source.sha})
        if phase == "ready_publication":
            if job["kind"] == DeliveryKind.FINAL_RUN and job["integrated_sha"] != job["source_sha"]:
                return self._prepare_effect(tx, run, job, execution, world, "run_branch", before=job["source_sha"],
                                            after=job["integrated_sha"])
            tx.execute("UPDATE subjects SET phase='publish',version=version+1 WHERE id=?", (subject,))
            return Step("progress", subject)
        if phase == "publication_commit":
            attempt = tx.one("SELECT result_id FROM attempts WHERE id=?", (job["publication_attempt"],))
            narrative = json.loads(tx.get(attempt["result_id"], subject))
            parents = [job["source_sha"]]
            if (job["kind"] == DeliveryKind.PARENT_ONLY and world.target.sha != job["source_sha"]
                    and (world.target.sha, job["source_sha"]) not in world.ancestry):
                parents.append(world.target.sha)
            return self._prepare_effect(tx, run, job, execution, world, "publication_commit", before=job["source_sha"],
                                        payload={"commit_message": narrative["commit_message"], "title": narrative["pr_title"],
                                                 "body": narrative["pr_body_markdown"], "parents": parents,
                                                 "reuse_sha": job["integrated_sha"] if job["kind"] == DeliveryKind.FINAL_RUN else None})
        if phase == "push":
            if job["source_sha"] == job["publication_sha"]:
                tx.execute("UPDATE subjects SET phase='pr',version=version+1 WHERE id=?", (subject,))
                return Step("progress", subject)
            return self._prepare_effect(tx, run, job, execution, world, "push", before=job["source_sha"], after=job["publication_sha"])
        if phase == "pr":
            attempt = tx.one("SELECT result_id FROM attempts WHERE id=?", (job["publication_attempt"],))
            narrative = json.loads(tx.get(attempt["result_id"], subject))
            old = None if world.pr is None else {"title": world.pr.title, "body": world.pr.body}
            return self._prepare_effect(tx, run, job, execution, world, "pr_write", before=job["publication_sha"],
                                        after=job["publication_sha"], payload={"title": narrative["pr_title"],
                                        "body": narrative["pr_body_markdown"], "prior": old})
        if phase == "checks":
            if not self._published(tx, run, job, world):
                return Step("paused", subject, "published_head")
            checks = world.checks
            if checks is None or checks.pr_number != job["pr_number"] or checks.head_sha != job["publication_sha"]:
                return self._wait(tx, run, job, "required_checks", 2700)
            material = tx.put(subject, "required_checks", wire_json(asdict(checks)))
            check_id = material
            tx.execute("INSERT OR IGNORE INTO checks VALUES (?,?,?,?,?,?)", (check_id, subject, checks.pr_number,
                       checks.head_sha, checks.result, material))
            tx.execute("UPDATE subjects SET checks_id=? WHERE id=?", (check_id, subject))
            if checks.result in {"pending", "unknown"}:
                return self._wait(tx, run, job, "required_checks", 2700)
            tx.execute("DELETE FROM waits WHERE subject=? AND kind='required_checks'", (subject,))
            if checks.result == "fail":
                tx.execute("UPDATE subjects SET phase='development',purpose='ci',repair_source='required_checks',ci_failure=?,last_failure=?,grant_id=NULL,integrated_sha=NULL,integrated_tree=NULL,version=version+1 WHERE id=?",
                           (material, material, subject))
                self._release_integration(tx, subject)
                self._event(tx, subject, "ci_failed", check_id, material)
            else:
                tx.execute("UPDATE subjects SET phase=?,version=version+1 WHERE id=?",
                           ("merge" if job["kind"] == DeliveryKind.TICKET else "approval", subject))
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
            if world.checks is None or world.checks.result in {"pending", "unknown"}:
                return self._wait(tx, run, job, "required_checks", 2700)
            if world.checks.result == "fail":
                self._finish_final_failure(tx, run, "updating")
                tx.execute("UPDATE subjects SET phase='checks',grant_id=NULL WHERE id=?", (subject,))
                return Step("progress", subject)
            if not self._merge_authority(tx, run, job, world):
                self._finish_final_failure(tx, run)
                self._gate(tx, subject, "approval_revalidation_failed")
                return Step("paused", subject, "merge_authority")
            publication = tx.one("SELECT result_id FROM attempts WHERE id=?", (job["publication_attempt"],))
            narrative = json.loads(tx.get(publication[0], subject))
            return self._prepare_effect(tx, run, job, execution, world, "merge", before=world.target.sha,
                                        after=job["publication_sha"], payload={"method": "squash" if job["kind"] == DeliveryKind.TICKET else "merge",
                                        "commit_message": narrative["commit_message"],
                                        "parents": [world.target.sha] + ([] if job["kind"] == DeliveryKind.TICKET else [job["publication_sha"]])})
        if phase == "completion_comment":
            return self._prepare_effect(tx, run, job, execution, world, "completion_comment", before=job["merge_sha"],
                                        payload={"issue": job["issue"], "merge_sha": job["merge_sha"], "gate_mode": job["gate_mode"]})
        if phase == "close":
            return self._prepare_effect(tx, run, job, execution, world, "close", before=job["merge_sha"],
                                        payload={"issue": job["issue"], "merge_sha": job["merge_sha"]})
        raise KernelError("prototype_step_unimplemented", phase)

    def _last_attempt(self, tx: Transaction, subject: str, role: Role) -> str:
        return str(tx.one("SELECT id FROM attempts WHERE subject=? AND role=? ORDER BY ordinal DESC LIMIT 1", (subject, role.value))[0])

    def _wait(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, kind: str, seconds: int) -> Step:
        deadline = tx.rows("SELECT deadline FROM waits WHERE subject=? AND kind=?", (job["id"], kind))
        if not deadline:
            tx.execute("INSERT INTO waits VALUES (?,?,?)", (job["id"], kind, self.clock() + seconds))
        elif self.clock() >= deadline[0][0]:
            self._gate(tx, job["id"], "supervision_timeout")
            self._finish_final_failure(tx, run)
            return Step("paused", job["id"], "supervision_timeout")
        return Step("waiting", job["id"], kind)

    def _finish_final_failure(self, tx: Transaction, run: sqlite3.Row, result: str = "failed") -> None:
        if run["final_action"]:
            tx.execute("UPDATE actions SET status=? WHERE id=?", (result, run["final_action"]))
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
        first = tx.one("SELECT subject FROM integration_queue ORDER BY ready LIMIT 1")[0]
        if first != subject:
            return False
        tx.execute("UPDATE runs SET integration_holder=? WHERE id=?", (subject, run["id"]))
        tx.execute("DELETE FROM integration_queue WHERE subject=?", (subject,))
        return True

    def _release_integration(self, tx: Transaction, subject: str) -> None:
        if self._pending_effect(tx, subject) is None:
            tx.execute("UPDATE runs SET integration_holder=NULL WHERE integration_holder=?", (subject,))
            tx.execute("DELETE FROM integration_queue WHERE subject=?", (subject,))

    def _prepare_effect(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, execution: Execution,
                        world: World, operation: str, *, before: str, after: str | None = None,
                        payload: dict[str, Any] | None = None) -> Step:
        subject, effect_id = job["id"], identity()
        body = dict(payload or {})
        body["intent_token"] = effect_id
        body["integrated_sha"] = job["integrated_sha"]
        body["integrated_tree"] = job["integrated_tree"]
        body["kind"] = job["kind"]
        payload_id = tx.put(subject, "effect_intent", wire_json(body))
        tx.execute("""INSERT INTO effects(id,subject,operation,status,executor,epoch,parent_revision,
            graph_revision,revision,generation,source_ref,target_ref,before_sha,after_sha,candidate_sha,
            candidate_tree,base_sha,base_tree,pr_number,actor,payload_id)
            VALUES (?,?,?,'prepared',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (effect_id, subject, operation, execution.owner, execution.epoch, run["parent_revision"],
                    run["graph_revision"], job["revision"], job["generation"], job["source_ref"], job["target_ref"],
                    before, after, job["candidate_sha"], job["candidate_tree"], world.target.sha, world.target.tree,
                    job["pr_number"], "offline-publisher", payload_id))
        self._event(tx, subject, "effect_prepared", effect_id, payload_id)
        row = tx.one("SELECT * FROM effects WHERE id=?", (effect_id,))
        return Step("effect", subject, effect=self._effect(tx, row, execution))

    def dispatch(self, effect: Effect, execution: Execution, world: World) -> bool:
        """Recheck authority immediately before the single external operation."""
        with self.storage.transaction() as tx:
            run, job = self._owned(tx, effect.subject, execution)
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            if row["status"] != "prepared":
                return False
            if self._effect(tx, row, execution) != effect:
                raise KernelError("effect_binding")
            if run["stopped"] or run["blocked"] or self._gates(tx, effect.subject):
                return False
            if not self._current(tx, job, run, world):
                return False
            if (effect.operation in {"integrate", "run_branch", "publication_commit", "push", "pr_write", "merge"}
                    and row["base_sha"] != world.target.sha):
                tx.execute("UPDATE effects SET status='retired' WHERE id=?", (effect.effect_id,))
                if effect.operation != "integrate":
                    self._update_boundary(tx, run, job)
                return False
            if (row["generation"] != job["generation"] or row["parent_revision"] != run["parent_revision"]
                    or row["graph_revision"] != run["graph_revision"] or row["revision"] != job["revision"]):
                return False
            if effect.operation == "merge" and (not self._published(tx, run, job, world)
                                                   or not self._merge_authority(tx, run, job, world)):
                tx.execute("UPDATE effects SET status='retired' WHERE id=?", (effect.effect_id,))
                return False
            tx.execute("UPDATE effects SET status='sent',attempts=attempts+1 WHERE id=?", (effect.effect_id,))
            self._event(tx, effect.subject, "effect_dispatched", effect.effect_id)
            return True

    def reconcile(self, effect: Effect, result: EffectResult, execution: Execution, world: World) -> Step:
        """Record target/prior/third/unknown without treating absence as read failure.

        Exact target settlement does not need live mutation authority. A prior
        observation may only rearm the original intent under current authority.
        """
        with self.storage.transaction() as tx:
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            if row["status"] in {"confirmed", "observed"}:
                return Step("progress", effect.subject)
            if row["status"] != "sent":
                raise KernelError("effect_not_dispatched")
            if self._effect(tx, row, effect.execution) != effect:
                raise KernelError("effect_binding")
            if result.disposition in {"target", "failure"}:
                receipt = tx.put(effect.subject, "effect_receipt", wire_json(asdict(result)))
                tx.execute("UPDATE effects SET status='observed',receipt_id=? WHERE id=?", (receipt, effect.effect_id))
                self._event(tx, effect.subject, "effect_observed", effect.effect_id, receipt)
                return Step("progress", effect.subject)
            run, job = self._owned(tx, effect.subject, execution)
            if result.disposition == "prior":
                if run["stopped"] or run["blocked"] or self._gates(tx, effect.subject) or not self._current(tx, job, run, world):
                    return Step("paused", effect.subject, "original_write_not_authorized")
                if (row["operation"] in {"run_branch", "publication_commit", "push", "pr_write", "merge"}
                        and row["base_sha"] != world.target.sha):
                    tx.execute("UPDATE effects SET status='retired' WHERE id=?", (effect.effect_id,))
                    self._update_boundary(tx, run, job)
                    return Step("progress", effect.subject, "updating")
                if row["attempts"] >= 3:
                    self._gate(tx, effect.subject, "publication_retry_exhausted")
                    return Step("paused", effect.subject, "publication_retry_exhausted")
                tx.execute("UPDATE effects SET status='prepared' WHERE id=?", (effect.effect_id,))
                return Step("progress", effect.subject)
            evidence = tx.put(effect.subject, "effect_observation", wire_json(asdict(result)))
            self._gate(tx, effect.subject, "write_conflict" if result.disposition == "third" else "write_result_unknown", evidence=evidence)
            self._finish_final_failure(tx, run)
            return Step("paused", effect.subject, result.disposition)

    def _apply_effect(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row, world: World) -> Step:
        subject, operation = job["id"], row["operation"]
        result = json.loads(tx.get(row["receipt_id"], subject))
        commit = result["commit"]
        if result["disposition"] == "failure":
            tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (row["id"],))
            purpose = Purpose.INTEGRATION.value if operation == "integrate" else job["purpose"]
            tx.execute("UPDATE subjects SET phase='development',purpose=?,repair_source=?,resume_purpose=?,last_failure=?,integrated_sha=NULL,integrated_tree=NULL,workspace_sha=?,version=version+1 WHERE id=?",
                       (purpose, "integration" if operation == "integrate" else "git_integrity", job["purpose"], row["receipt_id"],
                        job["workspace_sha"] if commit is None else commit["sha"], subject))
            self._release_integration(tx, subject)
            return Step("progress", subject)
        if operation in {"candidate", "integrate", "publication_commit", "merge"} and not commit:
            raise KernelError("effect_receipt_incomplete", operation)
        if operation == "candidate":
            if job["purpose"] == Purpose.CI and result["raw"].get("no_change"):
                tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (row["id"],))
                tx.execute("UPDATE subjects SET phase='development' WHERE id=?", (subject,))
                budget = self._budget(tx, job)
                self._gate(tx, subject, "ci_development_budget_exhausted" if budget["developments_used"] >= budget["development_limit"]
                           else "ci_investigation_paused", purpose=Purpose.CI.value, evidence=row["receipt_id"])
                return Step("paused", subject, "ci_investigation")
            tx.execute("UPDATE subjects SET candidate_sha=?,candidate_tree=?,workspace_sha=?,expected_tree=?,integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL WHERE id=?",
                       (commit["sha"], commit["tree"], commit["sha"], commit["tree"], subject))
            budget = self._budget(tx, job)
            policy = self._policy(run)
            if job["purpose"] == Purpose.INTEGRATION:
                phase = "integration"
            elif job["purpose"] == Purpose.CI and job["kind"] == DeliveryKind.TICKET and not policy.ticket_ci_review:
                phase = "integration"
                tx.execute("UPDATE subjects SET gate_mode='ci_waiver' WHERE id=?", (subject,))
            elif (job["kind"] == DeliveryKind.TICKET and job["purpose"] == Purpose.REQUIREMENTS
                    and budget["reviews_used"] >= budget["review_limit"]):
                if not job["review_attempt"]:
                    raise KernelError("fallback_without_prior_review")
                previous = tx.one("SELECT outcome FROM attempts WHERE id=?", (job["review_attempt"],))
                if previous["outcome"] != "fail":
                    raise KernelError("fallback_without_findings")
                phase = "integration"
                tx.execute("UPDATE subjects SET gate_mode='deterministic_fallback' WHERE id=?", (subject,))
            else:
                phase = "review"
        elif operation == "integrate":
            if not result["expected_tree"]:
                raise KernelError("integration_receipt_incomplete")
            tx.execute("UPDATE subjects SET integrated_sha=?,integrated_tree=?,workspace_sha=?,expected_tree=?,integration_target=? WHERE id=?",
                       (commit["sha"], commit["tree"], commit["sha"], result["expected_tree"], row["base_sha"], subject))
            if row["base_sha"] != world.target.sha:
                tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (row["id"],))
                tx.execute("UPDATE subjects SET phase='integration',version=version+1 WHERE id=?", (subject,))
                return Step("progress", subject, "integration_snapshot_changed")
            latest = self._job(tx, subject)
            if job["kind"] == DeliveryKind.TICKET:
                phase = "ready_publication"
            elif self._review_current(tx, latest, world):
                phase = "ready_publication"
            else:
                phase = "review"
                tx.execute("UPDATE subjects SET purpose=? WHERE id=?", (Purpose.CI.value if job["purpose"] == Purpose.CI else Purpose.INTEGRATION.value, subject))
        elif operation == "run_branch":
            tx.execute("UPDATE subjects SET source_sha=? WHERE id=?", (row["after_sha"], subject))
            phase = "publish"
        elif operation == "publication_commit":
            if commit["tree"] != job["integrated_tree"]:
                raise KernelError("publication_tree_changed")
            tx.execute("UPDATE subjects SET publication_sha=?,publication_tree=? WHERE id=?", (commit["sha"], commit["tree"], subject))
            phase = "push"
        elif operation == "push":
            tx.execute("UPDATE subjects SET source_sha=? WHERE id=?", (row["after_sha"], subject))
            phase = "pr"
        elif operation == "pr_write":
            pr = result["pr"]
            payload = json.loads(tx.get(row["payload_id"], subject))
            if (not pr or pr["repository"] != run["repository"] or pr["source_ref"] != job["source_ref"]
                    or pr["target_ref"] != job["target_ref"] or pr["head"]["sha"] != row["after_sha"]
                    or pr["state"] != "OPEN"
                    or pr["title"] != payload["title"] or pr["body"] != payload["body"]
                    or pr["actor"] != row["actor"] or (row["pr_number"] is not None and row["pr_number"] != pr["number"])):
                raise KernelError("foreign_pr_receipt")
            tx.execute("UPDATE subjects SET pr_number=? WHERE id=?", (pr["number"], subject))
            phase = "checks" if row["base_sha"] == world.target.sha else "integration"
        elif operation == "merge":
            tx.execute("UPDATE subjects SET merge_sha=? WHERE id=?", (commit["sha"], subject))
            phase = "completion_comment"
        elif operation == "completion_comment":
            phase = "close"
        elif operation == "close":
            phase = "complete"
            tx.execute("UPDATE runs SET final_action=NULL WHERE id=?", (run["id"],))
            if run["final_action"]:
                tx.execute("UPDATE actions SET status='complete' WHERE id=?", (run["final_action"],))
        else:
            raise KernelError("unknown_effect", operation)
        tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (row["id"],))
        tx.execute("UPDATE subjects SET phase=?,version=version+1 WHERE id=?", (phase, subject))
        if phase not in {"development", "review", "publish"}:
            tx.execute("DELETE FROM work_queue WHERE subject=?", (subject,))
        if phase == "complete":
            self._release_integration(tx, subject)
        self._event(tx, subject, "effect_consumed", row["id"], row["receipt_id"])
        return Step("progress", subject)

    def _update_boundary(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> None:
        tx.execute("UPDATE subjects SET phase='integration',purpose='integration',integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL,version=version+1 WHERE id=?", (job["id"],))
        if run["final_action"]:
            tx.execute("UPDATE actions SET status='updating' WHERE id=?", (run["final_action"],))
            tx.execute("UPDATE runs SET final_action=NULL WHERE id=?", (run["id"],))
        self._event(tx, job["id"], "boundary_update", job["candidate_sha"] or job["id"])

    def _review_current(self, tx: Transaction, job: sqlite3.Row, world: World) -> bool:
        if not job["review_attempt"]:
            return False
        review = tx.one("SELECT * FROM attempts WHERE id=?", (job["review_attempt"],))
        return bool(review["outcome"] == "pass" and review["revision"] == job["revision"]
                and review["candidate_sha"] == job["candidate_sha"] and review["candidate_tree"] == job["candidate_tree"]
                and review["base_sha"] == world.target.sha and review["expected_tree"] == job["expected_tree"]
                and review["completion_revision"] == job["completion_revision"])

    def _published_matches(self, run: sqlite3.Row, job: sqlite3.Row, world: World) -> bool:
        pr = world.pr
        return (pr is not None and pr.number == job["pr_number"] and pr.repository == run["repository"]
                and pr.source_ref == job["source_ref"] and pr.target_ref == job["target_ref"]
                and pr.head.sha == job["publication_sha"] and pr.head.tree == job["publication_tree"]
                and pr.base.sha == job["integration_target"] and world.target.sha == pr.base.sha and pr.state == "OPEN")

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
        recorded = tx.one("SELECT material_id FROM checks WHERE id=?", (job["checks_id"],))
        if json.loads(tx.get(recorded[0], job["id"])) != asdict(checks):
            # JSON arrays and dataclass tuples are compared canonically.
            if wire_json(json.loads(tx.get(recorded[0], job["id"]))) != wire_json(asdict(checks)):
                return False
        if job["kind"] == DeliveryKind.TICKET:
            return job["gate_mode"] in {"review", "deterministic_fallback", "ci_waiver"}
        if not job["grant_id"] or not self._review_current(tx, job, world):
            return False
        grant = tx.one("SELECT * FROM grants WHERE id=?", (job["grant_id"],))
        return bool(grant["pr_number"] == job["pr_number"] and grant["head_sha"] == job["publication_sha"]
                and grant["base_sha"] == world.target.sha and grant["review_attempt"] == job["review_attempt"]
                and grant["checks_id"] == job["checks_id"] and grant["revision"] == job["revision"]
                and grant["parent_revision"] == run["parent_revision"] and grant["graph_revision"] == run["graph_revision"]
                and grant["completion_revision"] == job["completion_revision"])

    def _event(self, tx: Transaction, subject: str, kind: str, event_id: str, material: str | None = None) -> None:
        tx.execute("INSERT INTO history(subject,kind,identity,material_id) VALUES (?,?,?,?)", (subject, kind, event_id, material))
