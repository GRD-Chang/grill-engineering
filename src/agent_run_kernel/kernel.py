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
from typing import Any
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
                    base_sha,base_tree,source_sha,candidate_sha,candidate_tree,completion_revision)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (delivery.key, spec.run_id, delivery.kind.value, delivery.issue, delivery.revision,
                            "integration" if final else "development",
                            Purpose.INTEGRATION.value if final else Purpose.REQUIREMENTS.value,
                            delivery.source_ref, delivery.target_ref, delivery.base.sha, delivery.base.tree,
                            delivery.base.sha, delivery.base.sha if final else None,
                            delivery.base.tree if final else None, delivery.completion_revision))
                limits = {
                    Purpose.REQUIREMENTS: (spec.policy.ticket_rounds + 1, spec.policy.ticket_rounds)
                    if delivery.kind == DeliveryKind.TICKET else (spec.policy.parent_rounds, spec.policy.parent_rounds),
                    Purpose.INTEGRATION: (spec.policy.run_repairs, spec.policy.run_repairs + 1)
                    if final else (spec.policy.integration_developments,
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
            if run["executor"] is not None and run["executor"] != owner:
                raise KernelError("executor_exit_unproven")
            epoch = run["epoch"] if run["executor"] == owner else run["epoch"] + 1
            tx.execute("UPDATE runs SET executor=?,epoch=?", (owner, epoch))
            return Execution(run["id"], owner, epoch)

    def advance(self, subject: str, execution: Execution, world: World, *, allow_agent: bool = True) -> Step:
        with self.storage.transaction() as tx:
            run, job = self._owned(tx, subject, execution)
            pending = self._pending_effect(tx, subject)
            # An unknown write is read back before scope drift or any new work.
            if pending is not None:
                return Step("reconcile" if pending["status"] == "sent" else "effect", subject,
                            effect=self._effect(tx, pending, execution))
            if run["stopped"] or self._gates(tx, subject):
                return Step("paused", subject, self._pause_reason(tx, subject, run))
            if not self._current(tx, job, run, world):
                return Step("paused", subject, "currentness")
            if job["phase"] == "complete":
                return Step("complete", subject)
            dependencies = tx.rows("SELECT s.phase FROM dependencies d JOIN subjects s ON s.id=d.predecessor WHERE d.subject=?", (subject,))
            if any(row[0] != "complete" for row in dependencies):
                return Step("waiting", subject, "dependencies")
            if job["current_attempt"]:
                attempt = tx.one("SELECT * FROM attempts WHERE id=?", (job["current_attempt"],))
                if attempt["result_id"]:
                    return self._consume(tx, run, job, attempt, world)
                invocation = tx.one("SELECT * FROM invocations WHERE id=?", (attempt["current_invocation"],))
                if invocation["status"] == "running":
                    return Step("waiting", subject, "agent_running")
                return self._allocate(tx, run, job, execution, world, allow_agent, attempt)
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
        return "operator_stopped" if run["stopped"] else ",".join(row["code"] for row in self._gates(tx, subject))

    def _gate(self, tx: Transaction, subject: str, code: str, *, purpose: str | None = None, evidence: str | None = None) -> None:
        tx.execute("INSERT OR IGNORE INTO gates VALUES (?,?,?,?)", (subject, code, purpose, evidence))
        self._event(tx, subject, "paused", code, evidence)

    def _current(self, tx: Transaction, job: sqlite3.Row, run: sqlite3.Row, world: World) -> bool:
        if world.graph_revision != run["graph_revision"]:
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
            self._gate(tx, subject, "ci_development_budget_exhausted" if job["purpose"] == Purpose.CI else "modification_budget_exhausted", purpose=job["purpose"])
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
        if pending is not None:
            previous = tx.one("SELECT thread FROM invocations WHERE id=?", (pending["current_invocation"],))
            thread_id = previous["thread"]
        elif role == Role.DEVELOPMENT and not policy.new_thread_per_development:
            thread_id = job["development_thread"]
        if thread_id is None:
            thread_id = identity()
            tx.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (thread_id, subject, role.value, profile.model, profile.effort))
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
                        run["parent_revision"], run["graph_revision"], job["revision"], world.target.sha,
                        job["candidate_sha"], job["candidate_tree"], job["expected_tree"],
                        job["completion_revision"], context_id))
            if role == Role.DEVELOPMENT:
                tx.execute("UPDATE budgets SET developments_used=developments_used+1 WHERE subject=? AND purpose=? AND window=?",
                           (subject, job["purpose"], budget["window"]))
        else:
            # A continuation retains the semantic input, adding only immutable human responses.
            context = json.loads(tx.get(pending["context_id"], subject))
        invocation_id, lease_id = identity(), identity()
        context_id = tx.put(subject, "role_context", wire_json(context))
        tx.execute("INSERT INTO invocations(id,attempt,thread,context_id,executor,epoch,lease,status) VALUES (?,?,?,?,?,?,?,'running')",
                   (invocation_id, attempt_id, thread_id, context_id, execution.owner, execution.epoch, lease_id))
        tx.execute("UPDATE attempts SET current_invocation=?,status='running' WHERE id=?", (invocation_id, attempt_id))
        tx.execute("INSERT INTO leases VALUES (?,?,?,?,?)", (lease_id, subject, invocation_id, execution.owner, execution.epoch))
        tx.execute("UPDATE subjects SET current_attempt=?,version=version+1 WHERE id=?", (attempt_id, subject))
        self._event(tx, subject, "agent_started", invocation_id)
        call = AgentCall(attempt_id, invocation_id, subject, role, Purpose(job["purpose"]), thread_id,
                         lease_id, execution, context, profile)
        return Step("agent", subject, call=call)

    def _context(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World, role: Role) -> dict[str, Any]:
        context: dict[str, Any] = {
            "repository": run["repository"], "parent_issue": run["parent_issue"], "issue": job["issue"],
            "scope": job["kind"], "generation": job["generation"], "role": role.value,
            "purpose": job["purpose"], "revision": job["revision"], "parent_revision": run["parent_revision"],
            "graph_revision": run["graph_revision"], "completion_revision": job["completion_revision"],
            "base_sha": world.target.sha, "candidate_sha": job["candidate_sha"], "candidate_tree": job["candidate_tree"],
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
                phase = "development"
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
        rows = tx.rows("SELECT * FROM effects WHERE subject=? AND status IN ('prepared','sent')", (subject,))
        return rows[0] if rows else None

    def _effect(self, tx: Transaction, row: sqlite3.Row, execution: Execution) -> Effect:
        return Effect(row["id"], row["subject"], row["operation"],
                      tx.one("SELECT repository FROM runs")[0], row["source_ref"], row["target_ref"],
                      row["before_sha"], row["after_sha"],
                      None if row["candidate_sha"] is None else Commit(row["candidate_sha"], row["candidate_tree"]),
                      Commit(row["base_sha"], row["base_tree"]), row["pr_number"], row["actor"],
                      json.loads(tx.get(row["payload_id"], row["subject"])), execution)

    def _mechanical(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, execution: Execution, world: World) -> Step:
        raise KernelError("prototype_step_unimplemented", job["phase"])

    def _event(self, tx: Transaction, subject: str, kind: str, event_id: str, material: str | None = None) -> None:
        tx.execute("INSERT INTO history(subject,kind,identity,material_id) VALUES (?,?,?,?)", (subject, kind, event_id, material))
