"""One authoritative transaction interpreter for delivery work.

All transition, currentness, budget, FIFO and mutation-authority choices are
made here from current SQL facts. The external driver only executes the one
returned request. A raw return and its business consumption commit separately.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
from uuid import uuid4

from .model import (
    AgentCall, Commit, DeliveryKind, Effect, EffectResult, Execution, KernelError,
    Delivery, Policy, Profile, Purpose, Requirements, Role, RunSpec, Step, World, artifact, review_outcome, wire_json,
)
from .storage import CURRENT_LIMIT, Storage, Transaction


def identity() -> str:
    return uuid4().hex


class Kernel:
    CLEANUP = ("cleanup_worktree", "cleanup_local_ref", "cleanup_remote_ref")
    def __init__(self, path: Path) -> None:
        self.storage = Storage(path)
        self.clock: Callable[[], float] = time.time

    @classmethod
    def create(cls, path: Path, spec: RunSpec, *, requirements: Requirements | None = None) -> Kernel:
        if not spec.deliveries or len({delivery.key for delivery in spec.deliveries}) != len(spec.deliveries):
            raise ValueError("one or more uniquely identified deliveries are required")
        Storage.create(path)
        kernel = cls(path)
        with kernel.storage.transaction() as tx:
            if requirements is not None:
                if requirements.parent.number != spec.parent_issue:
                    raise KernelError("parent_identity")
                parent_revision, graph_revision = kernel._source_revisions(requirements)
                spec = replace(spec, parent_revision=parent_revision, graph_revision=graph_revision)
                tickets = {delivery.issue for delivery in spec.deliveries if delivery.kind == DeliveryKind.TICKET}
                if tickets != set(requirements.parent.sub_issue_numbers):
                    raise KernelError("delivery_scope")
                if len([delivery for delivery in spec.deliveries if delivery.kind == DeliveryKind.FINAL_RUN]) != bool(tickets):
                    raise KernelError("final_target_required")
            tx.execute("INSERT INTO runs(id,repository,parent_issue,parent_revision,graph_revision,concurrency,policy) VALUES (?,?,?,?,?,?,?)",
                       (spec.run_id, spec.repository, spec.parent_issue, spec.parent_revision,
                        spec.graph_revision, spec.policy.concurrency, wire_json(asdict(spec.policy))))
            for delivery in spec.deliveries:
                if requirements is not None and delivery.kind == DeliveryKind.FINAL_RUN:
                    tx.execute("INSERT INTO final_target VALUES (?,?,?,?,?,?)", (delivery.key, delivery.source_ref, delivery.target_ref, delivery.checkout, delivery.owned_root, delivery.common_git_dir))
                    continue
                if requirements is not None:
                    issue = next((issue for issue in requirements.issues if issue.number == delivery.issue), None)
                    revision = spec.parent_revision if delivery.kind == DeliveryKind.PARENT_ONLY else (
                        "unread" if issue is None else kernel._fingerprint({"title": issue.title, "body": issue.body}))
                    delivery = replace(delivery, revision=revision, dependencies=())
                kernel._add_delivery(tx, spec.run_id, spec.policy, delivery)
            if requirements is None:
                for delivery in spec.deliveries:
                    for predecessor in delivery.dependencies:
                        tx.execute("INSERT INTO dependencies VALUES (?,?)", (delivery.key, predecessor))
            if requirements is not None:
                kernel._refresh(tx, requirements, initial=True)
        return kernel

    def _add_delivery(self, tx: Transaction, run_id: str, policy: Policy, delivery: Delivery) -> None:
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
        limits = {
            Purpose.REQUIREMENTS: (policy.ticket_rounds + 1, policy.ticket_rounds)
            if delivery.kind == DeliveryKind.TICKET else (policy.parent_rounds, policy.parent_rounds),
            Purpose.INTEGRATION: (policy.run_repairs, policy.run_repairs + 1)
            if final else (policy.run_repairs if delivery.kind == DeliveryKind.PARENT_ONLY else policy.integration_developments,
                           0 if delivery.kind == DeliveryKind.TICKET else None),
            Purpose.CI: (policy.ci_developments, None),
        }
        for purpose, (development_limit, review_limit) in limits.items():
            tx.execute("INSERT INTO budgets(subject,purpose,window,development_limit,review_limit) VALUES (?,?,1,?,?)",
                       (delivery.key, purpose.value, development_limit, review_limit))

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

    def refresh(self, requirements: Requirements) -> dict[str, Any]:
        """Observe real source facts without reopening any operator control."""
        with self.storage.transaction() as tx:
            if tx.one("SELECT source_sequence FROM runs")[0] is None:
                raise KernelError("source_not_configured")
            self._refresh(tx, requirements)
        return self.source_status()

    def _refresh(self, tx: Transaction, source: Requirements, *, initial: bool = False) -> None:
        run = tx.one("SELECT * FROM runs")
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
        previous = tx.rows("SELECT sequence,body FROM source_observations ORDER BY sequence DESC LIMIT 1")
        if previous and previous[0]["body"] == raw:
            return
        tx.execute("INSERT INTO source_observations(parent_revision,graph_revision,body) VALUES (?,?,?)", (parent_revision, graph_revision, raw))
        sequence = tx.one("SELECT last_insert_rowid()")[0]
        tx.execute("INSERT OR IGNORE INTO requirement_versions VALUES (?,?,?,?)",
                   (source.parent.number, parent_revision, source.parent.title, source.parent.body))
        tx.execute("DELETE FROM issue_facts")
        tx.execute("DELETE FROM dependency_facts")
        by_number = {issue.number: issue for issue in source.issues}
        for issue in source.issues:
            revision = self._fingerprint({"title": issue.title, "body": issue.body})
            tx.execute("INSERT OR IGNORE INTO requirement_versions VALUES (?,?,?,?)", (issue.number, revision, issue.title, issue.body))
            tx.execute("INSERT INTO issue_facts VALUES (?,?,?,?,?)", (issue.number, revision, issue.state.upper(), wire_json(sorted(issue.labels)), order.index(issue.number) if issue.number in order else -1))
            for blocker in issue.blocked_by:
                tx.execute("INSERT INTO dependency_facts VALUES (?,?,?)", (issue.number, blocker.number, blocker.state.upper()))
        tx.execute("UPDATE runs SET source_sequence=?,observed_parent_revision=?,observed_graph_revision=?",
                   (sequence, parent_revision, graph_revision))
        if initial:
            initial_jobs = {row["issue"]: row["id"] for row in tx.rows("SELECT id,issue FROM subjects WHERE kind='ticket'")}
            for issue in source.issues:
                for blocker in issue.blocked_by:
                    if issue.number in initial_jobs and blocker.number in initial_jobs:
                        tx.execute("INSERT INTO dependencies VALUES (?,?)", (initial_jobs[issue.number], initial_jobs[blocker.number]))
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
        jobs = tx.rows("SELECT * FROM subjects")
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
                facts = tx.rows("SELECT revision FROM issue_facts WHERE issue=?", (job["issue"],))
                revision = parent_revision if job["kind"] != DeliveryKind.TICKET else (facts[0][0] if facts else job["revision"])
                has_work = bool(tx.rows("SELECT id FROM attempts WHERE subject=? AND generation=? LIMIT 1", (job["id"], job["generation"])))
                if has_work and (revision != job["revision"] or changed_parent):
                    self._gate(tx, job["id"], "final_reacceptance_unimplemented" if job["kind"] == DeliveryKind.FINAL_RUN else "requeue_required")
                    tx.execute("UPDATE subjects SET grant_id=NULL WHERE id=?", (job["id"],))
                    parent_in_use = parent_in_use or changed_parent
                elif not has_work:
                    tx.execute("UPDATE subjects SET revision=? WHERE id=?", (revision, job["id"]))
            if not parent_in_use:
                tx.execute("UPDATE runs SET parent_revision=?", (parent_revision,))
            if tx.rows("SELECT subject FROM gates WHERE code='requeue_required' LIMIT 1"):
                blocked = "requeue_required"
            elif tx.rows("SELECT subject FROM gates WHERE code='final_reacceptance_unimplemented' LIMIT 1"):
                blocked = "final_reacceptance_unimplemented"
        managed = {None, "missing_ticket", "dependency_cycle", "unsupported_scope_change", "requeue_required", "final_reacceptance_unimplemented"}
        if run["blocked"] in managed:
            tx.execute("UPDATE runs SET blocked=?", (blocked,))
        owner = jobs[0]["id"]
        self._event(tx, owner, "source_refreshed", str(sequence))

    def _completion(self, tx: Transaction, job: sqlite3.Row) -> sqlite3.Row | None:
        rows = tx.rows("SELECT * FROM completions WHERE subject=? AND generation=? AND revision=? AND graph_revision=(SELECT graph_revision FROM runs)",
                       (job["id"], job["generation"], job["revision"]))
        return rows[0] if rows and job["phase"] == "complete" else None

    def _eligibility(self, tx: Transaction, job: sqlite3.Row) -> str | None:
        if job["kind"] != DeliveryKind.TICKET or tx.one("SELECT source_sequence FROM runs")[0] is None:
            return None
        facts = tx.rows("SELECT * FROM issue_facts WHERE issue=?", (job["issue"],))
        if not facts:
            return "missing_ticket"
        if job["phase"] == "complete" and self._completion(tx, job) is not None:
            return "completed"
        # Local dependency authority is Completion, never the live CLOSED flag.
        for predecessor in tx.rows("SELECT s.* FROM dependencies d JOIN subjects s ON s.id=d.predecessor WHERE d.subject=?", (job["id"],)):
            if self._completion(tx, predecessor) is None:
                return "blocked_by_incomplete_tickets"
        if facts[0]["state"] != "OPEN":
            return "ticket_closed"
        if tx.rows("SELECT blocker FROM dependency_facts WHERE issue=? AND state!='CLOSED' AND blocker NOT IN (SELECT issue FROM subjects WHERE kind='ticket')", (job["issue"],)):
            return "blocked_by_open_issues"
        if not tx.rows("SELECT id FROM attempts WHERE subject=? AND generation=? LIMIT 1", (job["id"], job["generation"])):
            labels: set[str] = set(json.loads(facts[0]["labels"]))
            disqualifying = sorted(labels & {"needs-triage", "needs-info", "ready-for-human"})
            if disqualifying:
                return "disqualifying_label:" + disqualifying[0]
            if "ready-for-agent" not in labels:
                return "missing_ready_for_agent"
        return None

    def source_status(self) -> dict[str, Any]:
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            jobs = tx.rows("SELECT s.* FROM subjects s LEFT JOIN issue_facts i ON i.issue=s.issue ORDER BY i.position,s.issue")
            frontier, remaining = [], {}
            for job in jobs:
                if job["kind"] != DeliveryKind.TICKET:
                    continue
                reason = self._eligibility(tx, job)
                if reason == "completed":
                    continue
                if reason is not None:
                    remaining[job["id"]] = reason
                elif not run["blocked"] and not run["stopped"] and not self._gates(tx, job["id"]):
                    frontier.append(job["id"])
            final = next((job["id"] for job in jobs if job["kind"] == DeliveryKind.FINAL_RUN), None)
            return {"frontier": frontier, "remaining": remaining, "blocked": run["blocked"], "stopped": bool(run["stopped"]),
                    "parent_revision": run["parent_revision"], "observed_parent_revision": run["observed_parent_revision"],
                    "graph_revision": run["graph_revision"], "observed_graph_revision": run["observed_graph_revision"],
                    "observation": run["source_sequence"], "final_subject": final,
                    "completions": [dict(row) for row in tx.rows("SELECT * FROM completions ORDER BY subject,generation")]}

    def delivery(self, subject: str) -> Delivery:
        """Current resource identity for an external reader; no state machine."""
        with self.storage.transaction(write=False) as tx:
            job = self._job(tx, subject)
            dependencies = tuple(row[0] for row in tx.rows("SELECT predecessor FROM dependencies WHERE subject=? ORDER BY predecessor", (subject,)))
            return Delivery(subject, DeliveryKind(job["kind"]), job["issue"], job["revision"], Commit(job["base_sha"], job["base_tree"]),
                            job["source_ref"], job["target_ref"], dependencies, job["completion_revision"], job["checkout"], job["owned_root"], job["common_git_dir"])

    def activate_final(self, execution: Execution, run_head: Commit, default_head: Commit, *, ancestry: tuple[tuple[str, str], ...]) -> str:
        """Create Final only from exact Completion and observed Git ancestry."""
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            if run["id"] != execution.run_id or run["executor"] != execution.owner or run["epoch"] != execution.epoch:
                raise KernelError("executor_fenced")
            if run["stopped"] or run["blocked"] or run["source_sequence"] is None:
                raise KernelError("final_admission_closed")
            existing = tx.rows("SELECT id FROM subjects WHERE kind='final_run'")
            if existing:
                return str(existing[0][0])
            jobs = tx.rows("SELECT * FROM subjects WHERE kind='ticket' ORDER BY issue")
            completed = [self._completion(tx, job) for job in jobs]
            if not jobs or any(completion is None for completion in completed):
                raise KernelError("ticket_completion_required")
            records = [dict(completion) for completion in completed if completion is not None]
            if any(record["merge_sha"] != run_head.sha and (record["merge_sha"], run_head.sha) not in ancestry for record in records):
                raise KernelError("completion_not_in_run")
            target = tx.one("SELECT * FROM final_target")
            completion_revision = self._fingerprint(records)
            delivery = Delivery(target["id"], DeliveryKind.FINAL_RUN, run["parent_issue"], run["parent_revision"], run_head,
                                target["source_ref"], target["target_ref"], completion_revision=completion_revision, checkout=target["checkout"],
                                owned_root=target["owned_root"], common_git_dir=target["common_git_dir"])
            self._add_delivery(tx, run["id"], self._policy(run), delivery)
            tx.execute("UPDATE subjects SET phase='workspace' WHERE id=?", (delivery.key,))
            review_evidence = []
            for record in records:
                item = dict(record)
                for key, role in (("review_attempt", "review"), ("development_attempt", "development")):
                    if record[key] is not None:
                        attempt = tx.one("SELECT * FROM attempts WHERE id=? AND subject=?", (record[key], record["subject"]))
                        item[role] = {"attempt": attempt["id"], "base_sha": attempt["base_sha"], "candidate_sha": attempt["candidate_sha"],
                                      "candidate_tree": attempt["candidate_tree"], "outcome": attempt["outcome"],
                                      "artifact": json.loads(tx.get(attempt["result_id"], record["subject"]))}
                completed_job = self._job(tx, record["subject"])
                item["delivered_candidate"] = {"sha": completed_job["candidate_sha"], "tree": completed_job["candidate_tree"],
                                                "development_base": completed_job["base_sha"]}
                review_evidence.append(item)
            evidence = tx.put(delivery.key, "completion_barrier", wire_json(review_evidence))
            tx.execute("INSERT INTO final_barriers VALUES (?,1,?,?,?,?)", (delivery.key, completion_revision, run_head.sha, default_head.sha, evidence))
            self._event(tx, delivery.key, "final_activated", completion_revision, evidence)
            return delivery.key

    def requeue(self, subject: str, action_id: str, replacement: Delivery, world: World) -> str:
        """Explicitly retire one stale generation, retaining its work and siblings."""
        with self.storage.transaction() as tx:
            job = self._job(tx, subject)
            run = tx.one("SELECT * FROM runs")
            if job["kind"] == DeliveryKind.FINAL_RUN:
                raise KernelError("final_reacceptance_required")
            prior = tx.rows("SELECT * FROM actions WHERE id=?", (action_id,))
            if prior:
                command = json.loads(tx.get(prior[0]["payload_id"], subject))
                if (prior[0]["kind"] != "requeue" or prior[0]["subject"] != subject
                        or wire_json(command["replacement"]) != wire_json(asdict(replacement))):
                    raise KernelError("action_identity_changed")
                return str(prior[0]["status"])
            if run["stopped"] or run["blocked"] != "requeue_required" or run["final_action"]:
                raise KernelError("requeue_not_authorized")
            if not any(gate["code"] == "requeue_required" for gate in self._gates(tx, subject)):
                raise KernelError("requeue_not_authorized")
            if tx.rows("SELECT id FROM leases WHERE subject=?", (subject,)) or self._pending_effect(tx, subject) is not None:
                raise KernelError("generation_unsettled")
            if (replacement.key != subject or replacement.kind != job["kind"] or replacement.issue != job["issue"]
                    or replacement.source_ref == job["source_ref"] or replacement.checkout == job["checkout"]
                    or replacement.target_ref != job["target_ref"] or replacement.base != world.target):
                raise KernelError("replacement_identity")
            revision = (run["observed_parent_revision"] if job["kind"] != DeliveryKind.TICKET else
                        tx.one("SELECT revision FROM issue_facts WHERE issue=?", (job["issue"],))[0])
            if world.revision != revision or world.parent_revision != run["observed_parent_revision"] or world.graph_revision != run["graph_revision"]:
                raise KernelError("replacement_requirements")
            payload = tx.put(subject, "control_intent", wire_json({"kind": "requeue", "generation": job["generation"], "replacement": asdict(replacement)}))
            receipt = tx.put(subject, "control_receipt", wire_json({"action": action_id, "generation": job["generation"] + 1}))
            tx.execute("INSERT INTO actions(id,run_id,kind,subject,payload_id,status,receipt_id) VALUES (?,?,?,?,?,'applied',?)", (action_id, run["id"], "requeue", subject, payload, receipt))
            tx.execute("INSERT INTO retired_generations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (subject, job["generation"], job["revision"], job["source_ref"], job["checkout"], job["source_sha"],
                        job["candidate_sha"], job["publication_sha"], job["pr_number"], "requirements_changed", job["validation_checkout"], job["validation_cleanup"]))
            if job["current_attempt"]:
                attempt = tx.one("SELECT * FROM attempts WHERE id=?", (job["current_attempt"],))
                if attempt["role"] == Role.REVIEW and attempt["result_id"] and not attempt["accounted"]:
                    try:
                        artifact(Role.REVIEW, tx.get(attempt["result_id"], subject))
                    except KernelError:
                        pass
                    else:
                        tx.execute("UPDATE budgets SET reviews_used=reviews_used+1 WHERE subject=? AND purpose=? AND window=?", (subject, attempt["purpose"], attempt["window"]))
                        tx.execute("UPDATE attempts SET accounted=1,status='stale' WHERE id=?", (attempt["id"],))
            tx.execute("UPDATE subjects SET generation=generation+1,revision=?,phase='workspace',purpose='requirements',source_ref=?,checkout=?,base_sha=?,base_tree=?,source_sha=?,workspace_sha=?,owned_root=?,common_git_dir=?,candidate_sha=NULL,candidate_tree=NULL,integrated_sha=NULL,integrated_tree=NULL,expected_tree=NULL,integration_target=NULL,current_attempt=NULL,review_attempt=NULL,publication_attempt=NULL,publication_sha=NULL,publication_tree=NULL,pr_number=NULL,checks_id=NULL,grant_id=NULL,merge_sha=NULL,gate_mode=NULL,development_thread=NULL,ci_failure=NULL,last_failure=NULL,resume_purpose=NULL,repair_source=NULL,validation_checkout=NULL,validation_cleanup=NULL,version=version+1 WHERE id=?",
                       (revision, replacement.source_ref, replacement.checkout, replacement.base.sha, replacement.base.tree, replacement.base.sha, replacement.base.sha, replacement.owned_root, replacement.common_git_dir, subject))
            tx.execute("DELETE FROM issue_links WHERE subject=?", (subject,))
            tx.execute("UPDATE runs SET parent_revision=?", (run["observed_parent_revision"],))
            for budget in tx.rows("SELECT * FROM budgets WHERE subject=? AND current=1", (subject,)):
                tx.execute("UPDATE budgets SET current=0 WHERE subject=? AND purpose=? AND current=1", (subject, budget["purpose"]))
                tx.execute("INSERT INTO budgets(subject,purpose,window,development_limit,review_limit) VALUES (?,?,?,?,?)",
                           (subject, budget["purpose"], budget["window"] + 1, budget["development_limit"], budget["review_limit"]))
            tx.execute("DELETE FROM gates WHERE subject=?", (subject,))
            self._release_integration(tx, subject)
            tx.execute("DELETE FROM work_queue WHERE subject=?", (subject,))
            if not tx.rows("SELECT subject FROM gates WHERE code='requeue_required'"):
                tx.execute("UPDATE runs SET blocked=NULL WHERE blocked='requeue_required'")
            self._event(tx, subject, "generation_requeued", action_id, payload)
            return "applied"

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
            optional = tx.rows("SELECT e.* FROM effects e JOIN issue_links l ON l.effect_id=e.id WHERE l.subject=? AND l.state='pending' AND e.status='prepared'", (subject,))
            if optional:
                if not self._current(tx, job, run, world):
                    return Step("paused", subject, "currentness")
                return Step("effect", subject, effect=self._effect(tx, optional[0], execution))
            if job["validation_cleanup"]:
                if not self._current(tx, job, run, world):
                    return Step("paused", subject, "currentness")
                return self._prepare_effect(tx, run, job, execution, world, "validation_cleanup", before=job["workspace_sha"],
                                            payload={"checkout": job["validation_cleanup"]})
            if job["phase"] == "complete":
                return Step("complete", subject)
            eligibility = self._eligibility(tx, job)
            if eligibility is not None:
                return Step("waiting", subject, eligibility)
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

    def advance_cleanup(self, subject: str, execution: Execution, world: World) -> Step:
        """Bounded program work; cleanup never owns an Agent or integration seat."""
        with self.storage.transaction() as tx:
            run, job = self._owned(tx, subject, execution)
            records = tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND generation=?", (subject, job["generation"]))
            if not records or records[0]["state"] == "complete":
                return Step("complete", subject, "resources_retired")
            cleanup = records[0]
            if cleanup["effect_id"]:
                pending = tx.one("SELECT * FROM effects WHERE id=?", (cleanup["effect_id"],))
                if pending["status"] == "sent":
                    return Step("reconcile", subject, effect=self._effect(tx, pending, execution))
                if pending["status"] == "observed":
                    result = json.loads(tx.get(pending["receipt_id"], subject))
                    if result["disposition"] != "target":
                        raise KernelError("invalid_cleanup_receipt")
                    tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (pending["id"],))
                    stage = cleanup["stage"] + 1
                    tx.execute("UPDATE resource_cleanup SET stage=?,state=?,effect_id=NULL,outcome=NULL WHERE subject=? AND generation=?",
                               (stage, "complete" if stage == len(self.CLEANUP) else "pending", subject, job["generation"]))
                    self._event(tx, subject, "resource_retired", pending["operation"], pending["receipt_id"])
                    return Step("progress", subject)
                if pending["status"] == "cancelled":
                    tx.execute("UPDATE resource_cleanup SET effect_id=NULL WHERE subject=? AND generation=?", (subject, job["generation"]))
                    return Step("progress", subject)
                if run["stopped"] or run["blocked"] or self._gates(tx, subject):
                    return Step("paused", subject, "cleanup_authority")
                return Step("effect", subject, effect=self._effect(tx, pending, execution))
            if run["stopped"] or run["blocked"] or self._gates(tx, subject):
                return Step("paused", subject, "cleanup_authority")
            operation = self.CLEANUP[cleanup["stage"]]
            head = cleanup["workspace_sha"] if operation == "cleanup_worktree" else cleanup["source_sha"]
            payload = {"checkout": cleanup["checkout"], "owned_root": cleanup["owned_root"],
                       "common_git_dir": cleanup["common_git_dir"], "branch_ref": cleanup["source_ref"], "expected_head": head,
                       "remote": "offline-remote"}
            step = self._prepare_effect(tx, run, job, execution, world, operation, before=head, payload=payload)
            assert step.effect is not None
            tx.execute("UPDATE resource_cleanup SET effect_id=? WHERE subject=? AND generation=?", (step.effect.effect_id, subject, job["generation"]))
            return step

    def _cleanup_current(self, tx: Transaction, job: sqlite3.Row, row: sqlite3.Row) -> bool:
        records = tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND generation=?", (job["id"], row["generation"]))
        return bool(records and job["phase"] == "complete" and job["generation"] == row["generation"]
                    and records[0]["effect_id"] == row["id"] and records[0]["stage"] < len(self.CLEANUP)
                    and self.CLEANUP[records[0]["stage"]] == row["operation"] and records[0]["source_ref"] == row["source_ref"])

    def acknowledge_link(self, effect: Effect, result: EffectResult) -> None:
        """Accept the original call's acknowledgement; never promote a readback."""
        with self.storage.transaction() as tx:
            row = tx.one("SELECT * FROM effects WHERE id=? AND operation='issue_link'", (effect.effect_id,))
            if self._effect(tx, row, effect.execution) != effect or row["status"] != "sent":
                raise KernelError("effect_binding")
            status = result.raw.get("acknowledged_status")
            if status not in {"linked", "unavailable"}:
                return
            receipt = tx.put(effect.subject, "issue_link_ack", wire_json(asdict(result)))
            tx.execute("UPDATE issue_links SET state=? WHERE effect_id=?", (status, effect.effect_id))
            tx.execute("UPDATE effects SET status='confirmed',receipt_id=? WHERE id=?", (receipt, effect.effect_id))
            self._event(tx, effect.subject, "issue_link_acknowledged", effect.effect_id, receipt)

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

    def _invocation(self, tx: Transaction, call: AgentCall) -> sqlite3.Row:
        row = tx.one("SELECT i.*,a.subject,a.role,a.purpose,a.generation,a.parent_revision,a.graph_revision,a.revision,a.current_invocation FROM invocations i JOIN attempts a ON a.id=i.attempt WHERE i.id=?", (call.invocation_id,))
        if (row["attempt"] != call.attempt_id or row["subject"] != call.subject or row["thread"] != call.thread_id
                or row["lease"] != call.lease_id or row["executor"] != call.execution.owner or row["epoch"] != call.execution.epoch
                or row["role"] != call.role or row["purpose"] != call.purpose
                or json.loads(tx.get(row["context_id"], call.subject)) != call.context):
            raise KernelError("invocation_binding")
        return row

    def _agent_authority(self, tx: Transaction, call: AgentCall) -> sqlite3.Row:
        run, job = self._owned(tx, call.subject, call.execution)
        invocation = self._invocation(tx, call)
        if (run["stopped"] or run["blocked"] or self._gates(tx, call.subject)
                or invocation["generation"] != job["generation"] or job["current_attempt"] != call.attempt_id
                or invocation["parent_revision"] != run["parent_revision"]
                or invocation["graph_revision"] != run["graph_revision"] or invocation["revision"] != job["revision"]
                or invocation["raw_id"] is not None or invocation["dispatch_state"] not in {"allocated", "sent", "started"}
                or self._eligibility(tx, job) is not None
                or not tx.rows("SELECT id FROM leases WHERE id=? AND invocation=?", (call.lease_id, call.invocation_id))):
            raise KernelError("agent_authority_changed")
        return invocation

    def dispatch_agent(self, call: AgentCall) -> bool:
        """One durable launch claim. A repeated call never authorizes a restart."""
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["dispatch_state"] != "allocated":
                return False
            self._agent_authority(tx, call)
            tx.execute("UPDATE invocations SET dispatch_state='sent' WHERE id=?", (call.invocation_id,))
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
            witness = tx.put(call.subject, "worker_process_identity", wire_json(process_identity))
            if invocation["process_identity"] is not None:
                if invocation["process_identity"] != witness:
                    raise KernelError("process_identity_changed")
                return
            if invocation["dispatch_state"] not in {"sent", "unknown"}:
                raise KernelError("agent_not_dispatched")
            tx.execute("UPDATE invocations SET process_identity=?,dispatch_state='started' WHERE id=?", (witness, call.invocation_id))
            self._event(tx, call.subject, "worker_started", call.invocation_id, witness)

    def agent_start_unknown(self, call: AgentCall, evidence: dict[str, Any]) -> None:
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["dispatch_state"] in {"exited", "unavailable"}:
                return
            known = evidence.get("process_identity")
            observed_exit = evidence.get("observed_exit")
            if (invocation["process_identity"] is not None and isinstance(known, dict) and type(observed_exit) is int
                    and wire_json(known) == tx.get(invocation["process_identity"], call.subject)):
                self._process_exit(tx, call, invocation, observed_exit, "start_acknowledgement_unknown")
                return
            material = tx.put(call.subject, "worker_start_unknown", wire_json(evidence))
            tx.execute("UPDATE invocations SET dispatch_state='unknown',start_evidence=? WHERE id=?", (material, call.invocation_id))
            run = tx.one("SELECT * FROM runs")
            job = self._job(tx, call.subject)
            if (not run["stopped"] and not run["blocked"] and run["executor"] == call.execution.owner and run["epoch"] == call.execution.epoch
                    and job["generation"] == invocation["generation"] and job["current_attempt"] == call.attempt_id
                    and invocation["current_invocation"] == call.invocation_id and not self._gates(tx, call.subject)):
                self._gate(tx, call.subject, "worker_start_unknown", evidence=material)

    def agent_unavailable(self, call: AgentCall, code: str, evidence: dict[str, Any] | str) -> None:
        """A proven pre-launch failure pauses the allocated semantic Attempt."""
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["process_identity"] is not None or invocation["dispatch_state"] not in {"sent", "unavailable"}:
                raise KernelError("worker_exit_unproven")
            material = tx.put(call.subject, "worker_unavailable", wire_json({"code": code, "evidence": evidence}))
            tx.execute("UPDATE invocations SET dispatch_state='unavailable',status='interrupted',start_evidence=? WHERE id=?", (material, call.invocation_id))
            tx.execute("DELETE FROM leases WHERE id=? AND invocation=?", (call.lease_id, call.invocation_id))
            run = tx.one("SELECT * FROM runs")
            job = self._job(tx, call.subject)
            if (not run["stopped"] and not run["blocked"] and run["executor"] == call.execution.owner and run["epoch"] == call.execution.epoch
                    and not self._gates(tx, call.subject) and job["current_attempt"] == call.attempt_id
                    and invocation["current_invocation"] == call.invocation_id
                    and job["generation"] == invocation["generation"]):
                self._gate(tx, call.subject, "worker_unavailable", evidence=material)

    def agent_exited(self, call: AgentCall, process_identity: dict[str, Any], returncode: int, *, failure: str | None = None) -> None:
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["process_identity"] is None or json.loads(tx.get(invocation["process_identity"], call.subject)) != process_identity:
                raise KernelError("process_identity_changed")
            self._process_exit(tx, call, invocation, returncode, failure)

    def _process_exit(self, tx: Transaction, call: AgentCall, invocation: sqlite3.Row, returncode: int, failure: str | None) -> None:
        if invocation["exit_code"] is not None and invocation["exit_code"] != returncode:
            raise KernelError("conflicting_exit")
        tx.execute("UPDATE invocations SET dispatch_state='exited',exit_code=? WHERE id=?", (returncode, call.invocation_id))
        tx.execute("DELETE FROM leases WHERE id=? AND invocation=?", (call.lease_id, call.invocation_id))
        self._event(tx, call.subject, "worker_exited", call.invocation_id, invocation["process_identity"])
        if invocation["raw_id"] is None and (returncode != 0 or failure is not None):
            material = tx.put(call.subject, "worker_failure", wire_json({"returncode": returncode, "failure": failure}))
            tx.execute("UPDATE invocations SET status='interrupted',start_evidence=? WHERE id=?", (material, call.invocation_id))
            run = tx.one("SELECT * FROM runs")
            job = self._job(tx, call.subject)
            if (not run["stopped"] and not run["blocked"] and run["executor"] == call.execution.owner and run["epoch"] == call.execution.epoch
                    and not self._gates(tx, call.subject) and job["current_attempt"] == call.attempt_id
                    and invocation["current_invocation"] == call.invocation_id and job["generation"] == invocation["generation"]):
                self._gate(tx, call.subject, "execution_failed", evidence=material)

    def guard_effect(self, effect: Effect, execution: Execution, world: World) -> None:
        """Read-only authority guard for each real successor mutation in an adapter."""
        with self.storage.transaction(write=False) as tx:
            run, job = self._owned(tx, effect.subject, execution)
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            if effect.operation in self.CLEANUP:
                if (row["status"] != "sent" or run["stopped"] or run["blocked"] or self._gates(tx, effect.subject)
                        or not self._cleanup_current(tx, job, row) or self._effect(tx, row, execution) != effect):
                    raise KernelError("operation_cancelled")
                return
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
                        elif code in {"worker_unavailable", "execution_failed"}:
                            pass  # Explicitly continue the same charged Attempt.
                        elif code == "worker_start_unknown":
                            if not job["current_attempt"]:
                                raise KernelError("worker_exit_unproven")
                            invocation = tx.one("SELECT i.* FROM invocations i JOIN attempts a ON a.current_invocation=i.id WHERE a.id=?", (job["current_attempt"],))
                            if invocation["dispatch_state"] != "exited" or invocation["process_identity"] is None:
                                raise KernelError("worker_exit_unproven")
                            tx.execute("UPDATE invocations SET status='interrupted' WHERE id=? AND raw_id IS NULL", (invocation["id"],))
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
            observed = tx.rows("SELECT revision FROM issue_facts WHERE issue=?", (job["issue"],))
            reviewed_checkout = None
            if job["review_attempt"]:
                original = tx.one("SELECT context_id FROM attempts WHERE id=?", (job["review_attempt"],))[0]
                reviewed_checkout = json.loads(tx.get(original, subject))["checkout"]
            return {
                "subject": subject, "kind": job["kind"], "phase": job["phase"], "purpose": job["purpose"],
                "generation": job["generation"], "version": job["version"],
                "revision": job["revision"], "parent_revision": run["parent_revision"], "graph_revision": run["graph_revision"],
                "completion_revision": job["completion_revision"],
                "reviewed_checkout": reviewed_checkout,
                "source_revision": (job["revision"] if job["phase"] == "complete" or not observed else observed[0][0]),
                "source_parent_revision": run["observed_parent_revision"] or run["parent_revision"],
                "pending_attempt": job["current_attempt"], "pending_return": bool(pending and pending["result_id"]),
                "candidate": job["candidate_sha"], "pr_number": job["pr_number"], "gate_mode": job["gate_mode"],
                "gates": [row["code"] for row in self._gates(tx, subject)], "stopped": bool(run["stopped"]),
                "holds_integration": run["integration_holder"] == subject,
                "pending_write": None if (effect := self._pending_effect(tx, subject)) is None
                else {"operation": effect["operation"], "status": effect["status"], "id": effect["id"]},
                "budgets": {row["purpose"]: {key: row[key] for key in ("window", "developments_used", "reviews_used", "development_limit", "review_limit")} for row in budgets},
                "resource_cleanup": next((dict(row) for row in tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND generation=?", (subject, job["generation"]))), None),
                "issue_link": next((dict(row) for row in tx.rows("SELECT * FROM issue_links WHERE subject=?", (subject,))), None),
                "retired_generations": [dict(row) for row in tx.rows("SELECT * FROM retired_generations WHERE subject=? ORDER BY generation", (subject,))],
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
        if role == Role.REVIEW and not job["validation_checkout"]:
            return self._prepare_effect(tx, run, job, execution, world, "validation_checkout", before=job["workspace_sha"],
                                        payload={"reviewed_commit": job["integrated_sha"] or job["candidate_sha"]})
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
            if role == Role.REVIEW:
                context["checkout"] = job["validation_checkout"]
            responses = tx.rows("SELECT material_id FROM history WHERE subject=? AND kind='human_response' ORDER BY position", (subject,))
            context["human_responses"] = [tx.get(row[0], subject).decode() for row in responses]
        invocation_id, lease_id = identity(), identity()
        context_id = tx.put(subject, "role_context", wire_json(context))
        tx.execute("INSERT INTO invocations(id,attempt,thread,context_id,executor,epoch,lease,status) VALUES (?,?,?,?,?,?,?,'running')",
                   (invocation_id, attempt_id, thread_id, context_id, execution.owner, execution.epoch, lease_id))
        tx.execute("UPDATE attempts SET current_invocation=?,status='running',new_thread=0 WHERE id=?", (invocation_id, attempt_id))
        tx.execute("INSERT INTO leases VALUES (?,?,?,?,?)", (lease_id, subject, invocation_id, execution.owner, execution.epoch))
        tx.execute("UPDATE subjects SET current_attempt=?,version=version+1 WHERE id=?", (attempt_id, subject))
        self._event(tx, subject, "agent_allocated", invocation_id)
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
            "candidate_sha": job["candidate_sha"], "candidate_tree": job["candidate_tree"],
            "checkout": job["validation_checkout"] if role == Role.REVIEW else job["checkout"],
            "readonly": role != Role.DEVELOPMENT,
            "reviewed_commit": job["integrated_sha"] or job["candidate_sha"],
            "expected_tree": job["expected_tree"], "full_scope": job["kind"],
        }
        for key in ("last_failure", "ci_failure"):
            if job[key]:
                context[key] = json.loads(tx.get(job[key], job["id"]))
        if run["source_sequence"] is not None:
            numbers = [(run["parent_issue"], run["parent_revision"], "background" if job["kind"] == DeliveryKind.TICKET else "requirements")]
            if job["kind"] == DeliveryKind.TICKET:
                numbers.append((job["issue"], job["revision"], "requirements"))
            if job["kind"] == DeliveryKind.FINAL_RUN:
                numbers.extend((row["issue"], row["revision"], "completed_ticket") for row in tx.rows("SELECT issue,revision FROM subjects WHERE kind='ticket' ORDER BY issue"))
            context["requirements"] = []
            for number, revision, responsibility in numbers:
                requirement: dict[str, Any] = {"number": number, "revision": revision, "responsibility": responsibility,
                                               "url": f"https://github.com/{run['repository']}/issues/{number}"}
                if role != Role.DEVELOPMENT:
                    value = tx.one("SELECT title,body FROM requirement_versions WHERE issue=? AND revision=?", (number, revision))
                    requirement.update({"title": value["title"], "body": value["body"]})
                context["requirements"].append(requirement)
            if job["kind"] == DeliveryKind.FINAL_RUN:
                barrier = tx.one("SELECT evidence FROM final_barriers WHERE subject=? AND generation=?", (job["id"], job["generation"]))
                context["ticket_completions"] = json.loads(tx.get(barrier[0], job["id"]))
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
            if role == Role.REVIEW:
                self._retire_validation(tx, job)
            tx.execute("UPDATE attempts SET status='human',outcome=? WHERE id=?", (outcome, attempt["id"]))
            self._gate(tx, subject, "human_blocker", evidence=attempt["result_id"])
            return Step("paused", subject, "human_blocker")
        if role == Role.DEVELOPMENT:
            phase = "candidate"
        elif role == Role.REVIEW:
            self._retire_validation(tx, job)
            tx.execute("UPDATE subjects SET review_attempt=? WHERE id=?", (attempt["id"], subject))
            if outcome == "fail":
                tx.execute("UPDATE subjects SET last_failure=? WHERE id=?", (attempt["result_id"], subject))
                source = ("required_checks" if job["purpose"] == Purpose.CI else "integration"
                          if job["purpose"] == Purpose.INTEGRATION and job["repair_source"] == "integration" else "acceptance")
                tx.execute("UPDATE subjects SET repair_source=? WHERE id=?", (source, subject))
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
        rows = tx.rows("SELECT * FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') AND operation NOT IN ('cleanup_worktree','cleanup_local_ref','cleanup_remote_ref','issue_link')", (subject,))
        return rows[0] if rows else None

    def _retire_validation(self, tx: Transaction, job: sqlite3.Row) -> None:
        if job["validation_checkout"]:
            if job["validation_cleanup"]:
                raise KernelError("validation_cleanup_unsettled")
            tx.execute("UPDATE subjects SET validation_cleanup=validation_checkout,validation_checkout=NULL WHERE id=?", (job["id"],))

    def _effect(self, tx: Transaction, row: sqlite3.Row, execution: Execution) -> Effect:
        return Effect(row["id"], row["subject"], row["operation"],
                      tx.one("SELECT repository FROM runs")[0], row["source_ref"], row["target_ref"],
                      row["before_sha"], row["after_sha"],
                      None if row["candidate_sha"] is None else Commit(row["candidate_sha"], row["candidate_tree"]),
                      Commit(row["base_sha"], row["base_tree"]), row["pr_number"], row["actor"],
                      json.loads(tx.get(row["payload_id"], row["subject"])), execution)

    def _mechanical(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, execution: Execution, world: World) -> Step:
        subject, phase = job["id"], job["phase"]
        if phase == "workspace":
            return self._prepare_effect(tx, run, job, execution, world, "workspace", before=job["source_sha"],
                                        payload={"checkout": job["checkout"], "create_ref": job["kind"] != DeliveryKind.FINAL_RUN})
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
                                        "body": self._narrative_body(run, job, narrative["pr_body_markdown"]), "prior": old})
        if phase == "checks":
            if not self._published(tx, run, job, world):
                return Step("paused", subject, "published_head")
            if job["owned_root"] and not tx.rows("SELECT effect_id FROM issue_links WHERE subject=?", (subject,)):
                step = self._prepare_effect(tx, run, job, execution, world, "issue_link", before=job["publication_sha"],
                                            payload={"issue": job["issue"], "ref": job["source_ref"], "source_head": job["publication_sha"]})
                assert step.effect is not None
                tx.execute("INSERT INTO issue_links(subject,generation,source_ref,source_sha,issue,effect_id) VALUES (?,?,?,?,?,?)",
                           (subject, job["generation"], job["source_ref"], job["publication_sha"], job["issue"], step.effect.effect_id))
                return step
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

    def _narrative_body(self, run: sqlite3.Row, job: sqlite3.Row, narrative: str) -> str:
        header = f"Parent Issue: #{run['parent_issue']}\n"
        if job["kind"] == DeliveryKind.TICKET:
            header += f"Primary Ticket: #{job['issue']}\nDelivery Type: Ticket\n\n"
        else:
            header += "Delivery Type: " + ("Parent-only" if job["kind"] == DeliveryKind.PARENT_ONLY else "Final Run") + "\n\n"
        return header + narrative.strip()

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
        if operation == "validation_checkout":
            body["checkout"] = str(Path(job["checkout"]).parent / ("review-" + effect_id))
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
            if effect.operation in self.CLEANUP:
                if not self._cleanup_current(tx, job, row):
                    return False
                tx.execute("UPDATE effects SET status='sent',attempts=attempts+1 WHERE id=?", (effect.effect_id,))
                self._event(tx, effect.subject, "effect_dispatched", effect.effect_id)
                return True
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
            if effect.operation == "issue_link":
                tx.execute("UPDATE issue_links SET state='indeterminate' WHERE effect_id=?", (effect.effect_id,))
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
            if effect.operation == "issue_link":
                # Readback cannot establish that the original optional call's
                # acknowledgement was durably received. Never replay it.
                return Step("progress", effect.subject, "optional_display_indeterminate")
            if effect.operation in self.CLEANUP and result.disposition != "target":
                evidence = tx.put(effect.subject, "cleanup_observation", wire_json(asdict(result)))
                tx.execute("UPDATE resource_cleanup SET outcome=? WHERE effect_id=?", (result.disposition, effect.effect_id))
                self._event(tx, effect.subject, "cleanup_pending", effect.effect_id, evidence)
                if result.disposition == "prior":
                    run, job = self._owned(tx, effect.subject, execution)
                    if (not run["stopped"] and not run["blocked"] and not self._gates(tx, effect.subject)
                            and self._cleanup_current(tx, job, row) and row["attempts"] < 3):
                        tx.execute("UPDATE effects SET status='prepared' WHERE id=?", (effect.effect_id,))
                        return Step("progress", effect.subject)
                return Step("waiting", effect.subject, "cleanup_pending")
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
            if operation == "integrate":
                # The original CI observation remains immutable in checks/history;
                # current integration work receives the actual Git failure only.
                tx.execute("UPDATE subjects SET ci_failure=NULL WHERE id=?", (subject,))
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
            tx.execute("UPDATE subjects SET validation_checkout=? WHERE id=?", (payload["checkout"], subject))
            phase = job["phase"]
        elif operation == "validation_cleanup":
            tx.execute("UPDATE subjects SET validation_cleanup=NULL WHERE id=?", (subject,))
            phase = job["phase"]
        elif operation == "candidate":
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
                tx.execute("UPDATE subjects SET phase='integration',purpose='integration',repair_source='integration',ci_failure=NULL,version=version+1 WHERE id=?", (subject,))
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
            if run["source_sequence"] is not None and job["kind"] == DeliveryKind.TICKET:
                comment = tx.one("SELECT id FROM effects WHERE subject=? AND generation=? AND operation='completion_comment' AND status='confirmed' ORDER BY rowid DESC LIMIT 1", (subject, job["generation"]))
                if not job["merge_sha"] or not job["publication_sha"] or not job["pr_number"] or not job["gate_mode"]:
                    raise KernelError("completion_evidence_missing")
                tx.execute("INSERT INTO completions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (subject, job["generation"], job["revision"], run["parent_revision"], run["graph_revision"], job["publication_sha"],
                            job["merge_sha"], job["publication_tree"], job["pr_number"], job["gate_mode"], job["review_attempt"],
                            self._last_attempt(tx, subject, Role.DEVELOPMENT), comment["id"], row["id"]))
            tx.execute("UPDATE runs SET final_action=NULL WHERE id=?", (run["id"],))
            if run["final_action"]:
                tx.execute("UPDATE actions SET status='complete' WHERE id=?", (run["final_action"],))
            if job["owned_root"] and job["common_git_dir"]:
                tx.execute("INSERT OR IGNORE INTO resource_cleanup(subject,generation,source_ref,source_sha,checkout,workspace_sha,owned_root,common_git_dir) VALUES (?,?,?,?,?,?,?,?)",
                           (subject, job["generation"], job["source_ref"], job["publication_sha"], job["checkout"], job["workspace_sha"], job["owned_root"], job["common_git_dir"]))
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
        tx.execute("UPDATE subjects SET phase='integration',purpose='integration',repair_source='integration',ci_failure=NULL,integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL,version=version+1 WHERE id=?", (job["id"],))
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
