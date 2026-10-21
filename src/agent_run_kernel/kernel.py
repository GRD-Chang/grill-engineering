"""One authoritative transaction interpreter for delivery work.

All transition, currentness, budget, FIFO and mutation-authority choices are
made here from current SQL facts. The external driver only executes the one
returned request. A raw return and its business consumption commit separately.
"""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from agent_run.agent_profiles import ProfileOverrides, resolve_profiles, validate_profile_options
from agent_run.github_identity import validate_identity_snapshot
from agent_run.prompt_resources import validate_resources
from agent_run.task_identity import TaskKey

from .model import (
    AgentCall, Commit, DeliveryKind, Effect, EffectResult, Execution, HostLaunch, HostObservation, KernelError,
    Delivery, Policy, Profile, PullRequest, Purpose, Requirements, Role, RunSpec, Step, World, artifact, review_outcome, wire_json,
)
from .storage import CURRENT_LIMIT, Storage, Transaction
from .task_location import bind_task, locked_task, read_task


def identity() -> str:
    return uuid4().hex


class Kernel:
    CLEANUP = ("cleanup_worktree", "cleanup_local_ref", "cleanup_remote_ref")
    REMOTE = ("push", "run_branch", "pr_write", "merge", "completion_comment", "close", "issue_link", "cleanup_remote_ref")
    def __init__(self, path: Path) -> None:
        self.storage = Storage(path)
        self.clock: Callable[[], float] = time.time

    @classmethod
    def create(cls, path: Path, spec: RunSpec, *, requirements: Requirements | None = None,
               fixture_evidence: Mapping[str, Any] | None = None) -> Kernel:
        if not spec.deliveries or len({delivery.key for delivery in spec.deliveries}) != len(spec.deliveries):
            raise ValueError("one or more uniquely identified deliveries are required")
        profiles = ({"preset": None, "profiles": {
            role.value: {"model": getattr(spec.policy, role.value + "_profile").model,
                         "reasoning_effort": getattr(spec.policy, role.value + "_profile").effort,
                         "reference": None, "provenance": {"source": "explicit_policy"}} for role in Role}}
                    if spec.inputs is None else dict(spec.inputs.profiles))
        if spec.inputs is not None:
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
        policy = asdict(spec.policy)
        for role in Role:
            del policy[role.value + "_profile"]
        Storage.create(path)
        kernel = cls(path)
        with kernel.storage.transaction() as tx:
            if requirements is not None and fixture_evidence is not None:
                raise KernelError("mixed_source_authority")
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
            tx.execute("INSERT INTO runs(id,repository,parent_issue,parent_revision,graph_revision,accepted_parent_revision,concurrency,policy,language,repository_root,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (spec.run_id, spec.repository, spec.parent_issue, spec.parent_revision,
                        spec.graph_revision, spec.parent_revision, spec.policy.concurrency, wire_json(policy),
                        spec.language, None if spec.repository_root is None else str(Path(spec.repository_root).resolve()), kernel.clock()))
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
            owner = tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
            kernel._store_profiles(tx, owner, profiles, {"operation": "create"})
            if spec.inputs is not None:
                values: dict[str, Any] = {name: dict(getattr(spec.inputs, name)) for name in (
                    "prompt_resources", "worker_identity", "runner_identity", "notifications", "provenance")}
                values["repository_remote"] = spec.inputs.repository_remote
                material = tx.put(owner, "creation_inputs", wire_json(values))
                tx.execute("INSERT INTO run_inputs VALUES (?,?,?)", (spec.run_id, owner, material))
            if requirements is None:
                for delivery in spec.deliveries:
                    for predecessor in delivery.dependencies:
                        tx.execute("INSERT INTO dependencies VALUES (?,?)", (delivery.key, predecessor))
            if requirements is not None:
                kernel._refresh(tx, requirements, initial=True)
            if fixture_evidence is not None:
                for delivery in spec.deliveries:
                    tx.put(delivery.key, "fixture_evidence", wire_json(fixture_evidence))
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
        limits = self._budget_limits(policy, delivery.kind)
        for purpose, (development_limit, review_limit) in limits.items():
            review_required = delivery.kind != DeliveryKind.TICKET or purpose == Purpose.REQUIREMENTS or (purpose == Purpose.CI and policy.ticket_ci_review)
            tx.execute("INSERT INTO budgets(subject,purpose,window,development_limit,review_limit,review_required) VALUES (?,?,1,?,?,?)",
                       (delivery.key, purpose.value, development_limit, review_limit, review_required))
        tx.execute("INSERT INTO resync_allowances(subject,generation,allowance) VALUES (?,1,?)", (delivery.key, policy.resync_limit))

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
            sequence = tx.one("SELECT source_sequence FROM runs")[0]
            if sequence is None:
                raise KernelError("source_not_configured")
        requirements = read()
        with self.storage.transaction() as tx:
            if tx.one("SELECT source_sequence FROM runs")[0] != sequence:
                raise KernelError("source_observation_changed")
            self._refresh(tx, requirements)
            return self._source_status(tx)

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
            initial_jobs = {row["issue"]: row["id"] for row in tx.all("SELECT id,issue FROM subjects WHERE kind='ticket'")}
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
        jobs = tx.all("SELECT * FROM subjects")
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
                    if job["kind"] == DeliveryKind.FINAL_RUN:
                        if self._final_merge_intent(tx, run, job) is not None:
                            # The original approval owns exact unknown-merge
                            # readback and successful Parent closeout first.
                            parent_in_use = True
                            continue
                        tx.execute("UPDATE subjects SET refresh_revision=?,phase='refresh_final',grant_id=NULL WHERE id=?", (parent_revision, job["id"]))
                    else:
                        self._gate(tx, job["id"], "requeue_required")
                    tx.execute("UPDATE subjects SET grant_id=NULL WHERE id=?", (job["id"],))
                    parent_in_use = parent_in_use or changed_parent
                elif not has_work:
                    tx.execute("UPDATE subjects SET revision=? WHERE id=?", (revision, job["id"]))
            if not parent_in_use:
                tx.execute("UPDATE runs SET parent_revision=?", (parent_revision,))
            if tx.rows("SELECT subject FROM gates WHERE code='requeue_required' LIMIT 1"):
                blocked = "requeue_required"
            elif tx.rows("SELECT id FROM subjects WHERE refresh_revision IS NOT NULL LIMIT 1"):
                blocked = "final_refresh"
        managed = {None, "missing_ticket", "dependency_cycle", "unsupported_scope_change", "requeue_required", "final_refresh"}
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
        for predecessor in tx.all("SELECT s.* FROM dependencies d JOIN subjects s ON s.id=d.predecessor WHERE d.subject=?", (job["id"],)):
            if self._completion(tx, predecessor) is None:
                return "blocked_by_incomplete_tickets"
        if facts[0]["state"] != "OPEN":
            return "ticket_closed"
        if tx.rows("SELECT blocker FROM dependency_facts WHERE issue=? AND state!='CLOSED' AND blocker NOT IN (SELECT issue FROM subjects WHERE kind='ticket') LIMIT 1", (job["issue"],)):
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
        """Return observed revisions and exact current Completion identities.

        External readers reuse these witnesses; they do not hash requirements
        or substitute an accepted revision for a later observed revision.
        """
        with self.storage.transaction(write=False) as tx:
            return self._source_status(tx)

    def _source_status(self, tx: Transaction) -> dict[str, Any]:
        run = tx.one("SELECT * FROM runs")
        jobs = tx.all("SELECT s.* FROM subjects s LEFT JOIN issue_facts i ON i.issue=s.issue ORDER BY i.position,s.issue")
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
            accepted = json.loads(tx.one("SELECT body FROM source_observations ORDER BY sequence LIMIT 1")[0])
            observed = json.loads(tx.one("SELECT body FROM source_observations WHERE sequence=?", (run["source_sequence"],))[0])
            old_members, new_members = set(accepted["parent"]["sub_issue_numbers"]), set(observed["parent"]["sub_issue_numbers"])
            old_edges = {(issue["number"], blocker["number"]) for issue in accepted["issues"] for blocker in issue["blocked_by"] if issue["number"] in old_members}
            new_edges = {(issue["number"], blocker["number"]) for issue in observed["issues"] for blocker in issue["blocked_by"] if issue["number"] in new_members}
            summary = {"added_tickets": sorted(new_members - old_members), "removed_tickets": sorted(old_members - new_members),
                       "added_dependencies": [{"ticket_number": number, "blocked_by": blocker} for number, blocker in sorted(new_edges - old_edges)],
                       "removed_dependencies": [{"ticket_number": number, "blocked_by": blocker} for number, blocker in sorted(old_edges - new_edges)]}
        return {"frontier": frontier, "remaining": remaining, "blocked": run["blocked"], "stopped": bool(run["stopped"]),
                "parent_revision": run["parent_revision"], "observed_parent_revision": run["observed_parent_revision"],
                "accepted_parent_revision": run["accepted_parent_revision"], "graph_change_summary": summary,
                "graph_revision": run["graph_revision"], "observed_graph_revision": run["observed_graph_revision"],
                "observation": run["source_sequence"], "final_subject": final,
                "observed_issues": [dict(row) for row in tx.all("SELECT issue,revision FROM issue_facts ORDER BY issue")],
                "completion_revisions": [dict(row) for row in tx.all("SELECT id AS subject,generation,completion_revision FROM subjects ORDER BY id")],
                "completions": [dict(row) for row in tx.all("SELECT * FROM completions ORDER BY subject,generation")]}

    def delivery(self, subject: str) -> Delivery:
        """Current resource identity for an external reader; no state machine."""
        with self.storage.transaction(write=False) as tx:
            job = self._job(tx, subject)
            dependencies = tuple(row[0] for row in tx.all("SELECT predecessor FROM dependencies WHERE subject=? ORDER BY predecessor", (subject,)))
            return Delivery(subject, DeliveryKind(job["kind"]), job["issue"], job["revision"], Commit(job["base_sha"], job["base_tree"]),
                            job["source_ref"], job["target_ref"], dependencies, job["completion_revision"], job["checkout"], job["owned_root"], job["common_git_dir"])

    def activate_final(self, execution: Execution, run_head: Commit, default_head: Commit, *, ancestry: tuple[tuple[str, str], ...]) -> str:
        """Create Final only from exact Completion and observed Git ancestry."""
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            self._executor_owned(tx, run, execution)
            if run["stopped"] or run["blocked"] or run["source_sequence"] is None:
                raise KernelError("final_admission_closed")
            existing = tx.rows("SELECT id FROM subjects WHERE kind='final_run'")
            if existing:
                return str(existing[0][0])
            jobs = tx.all("SELECT * FROM subjects WHERE kind='ticket' ORDER BY issue")
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
            self._retire_generation(tx, job, "requirements_changed")
            tx.execute("UPDATE subjects SET generation=generation+1,revision=?,phase='workspace',purpose='requirements',source_ref=?,checkout=?,base_sha=?,base_tree=?,source_sha=?,workspace_sha=?,owned_root=?,common_git_dir=?,candidate_sha=NULL,candidate_tree=NULL,integrated_sha=NULL,integrated_tree=NULL,expected_tree=NULL,integration_target=NULL,current_attempt=NULL,review_attempt=NULL,publication_attempt=NULL,publication_sha=NULL,publication_tree=NULL,pr_number=NULL,checks_id=NULL,grant_id=NULL,merge_sha=NULL,gate_mode=NULL,development_thread=NULL,ci_failure=NULL,last_failure=NULL,resume_purpose=NULL,repair_source=NULL,validation_checkout=NULL,validation_cleanup=NULL,version=version+1 WHERE id=?",
                       (revision, replacement.source_ref, replacement.checkout, replacement.base.sha, replacement.base.tree, replacement.base.sha, replacement.base.sha, replacement.owned_root, replacement.common_git_dir, subject))
            tx.execute("DELETE FROM issue_links WHERE subject=?", (subject,))
            tx.execute("UPDATE runs SET parent_revision=?", (run["observed_parent_revision"],))
            for budget in tx.rows("SELECT * FROM budgets WHERE subject=? AND current=1", (subject,)):
                limits = self._budget_limits(self._policy(run), DeliveryKind(job["kind"]))[Purpose(budget["purpose"])]
                tx.execute("UPDATE budgets SET current=0 WHERE subject=? AND purpose=? AND current=1", (subject, budget["purpose"]))
                tx.execute("INSERT INTO budgets(subject,purpose,window,development_limit,review_limit,review_required) VALUES (?,?,?,?,?,?)",
                           (subject, budget["purpose"], budget["window"] + 1, *limits, budget["review_required"]))
            self._renew_resync(tx, job, require_exhaustion=False)
            tx.execute("DELETE FROM gates WHERE subject=?", (subject,))
            self._release_integration(tx, subject)
            tx.execute("DELETE FROM work_queue WHERE subject=?", (subject,))
            if not tx.rows("SELECT subject FROM gates WHERE code='requeue_required' LIMIT 1"):
                tx.execute("UPDATE runs SET blocked=NULL WHERE blocked='requeue_required'")
            self._event(tx, subject, "generation_requeued", action_id, payload)
            return "applied"

    def _charge_review(self, tx: Transaction, attempt: sqlite3.Row) -> None:
        if attempt["role"] == Role.REVIEW and not attempt["accounted"]:
            tx.execute("UPDATE budgets SET reviews_used=reviews_used+1 WHERE subject=? AND purpose=? AND window=?",
                       (attempt["subject"], attempt["purpose"], attempt["window"]))
            tx.execute("UPDATE attempts SET accounted=1 WHERE id=?", (attempt["id"],))

    def _retire_generation(self, tx: Transaction, job: sqlite3.Row, reason: str) -> None:
        tx.execute("INSERT INTO retired_generations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (job["id"], job["generation"], job["revision"], job["source_ref"], job["checkout"], job["source_sha"],
                    job["candidate_sha"], job["publication_sha"], job["pr_number"], reason, job["validation_checkout"], job["validation_cleanup"]))
        if job["current_attempt"]:
            attempt = tx.one("SELECT * FROM attempts WHERE id=?", (job["current_attempt"],))
            if attempt["role"] == Role.REVIEW and attempt["result_id"]:
                try:
                    artifact(Role.REVIEW, tx.get(attempt["result_id"], job["id"]))
                except KernelError:
                    pass
                else:
                    self._charge_review(tx, attempt)
            tx.execute("UPDATE attempts SET status='stale' WHERE id=?", (attempt["id"],))

    @staticmethod
    def _task(run: sqlite3.Row) -> TaskKey:
        if run["repository_root"] is None:
            raise KernelError("task_workspace_unknown")
        return TaskKey(Path(run["repository_root"]), run["repository"], run["parent_issue"])

    def _replaceable(self, task: TaskKey, run_id: str) -> bool:
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            if run["id"] != run_id or self._task(run) != task:
                raise KernelError("task_location_identity")
            return bool(run["executor"] is None
                and not tx.rows("SELECT id FROM subjects WHERE phase!='complete' LIMIT 1")
                and not tx.rows("SELECT id FROM final_target WHERE id NOT IN (SELECT id FROM subjects WHERE kind='final_run' AND phase='complete') LIMIT 1")
                and not tx.rows("SELECT id FROM leases LIMIT 1")
                and not tx.rows("SELECT owner FROM retired_executors WHERE status!='exited' LIMIT 1")
                and not tx.rows("SELECT owner FROM hosts WHERE status!='exited' OR usage_released=0 LIMIT 1")
                and not tx.rows("SELECT id FROM effects WHERE status IN ('prepared','sent','observed') AND operation!='issue_link' LIMIT 1")
                and not tx.rows("SELECT subject FROM resource_cleanup WHERE state!='complete' LIMIT 1"))

    @classmethod
    def open_task(cls, task: TaskKey) -> Kernel | None:
        """Find unfinished work under the original Task admission lock."""
        with locked_task(task) as location:
            prior = read_task(location)
            if prior is None:
                return None
            kernel = cls(prior[1])
            return None if kernel._replaceable(task, prior[0]) else kernel

    def active_host_action(self) -> str | None:
        """Read the original Action until its Host and usage are both retired."""
        with self.storage.transaction(write=False) as tx:
            rows = tx.rows("SELECT action_id FROM hosts WHERE status!='exited' OR usage_released=0", limit=1)
            return None if not rows else str(rows[0][0])

    def host_dispatched(self, action_id: str) -> bool:
        """Read the durable launch claim, never infer it from process absence."""
        with self.storage.transaction(write=False) as tx:
            return tx.one("SELECT dispatched_at FROM hosts WHERE action_id=?", (action_id,))[0] is not None

    def admit_host(self, action_id: str, *, kind: str, command: tuple[str, ...], environment_carrier: Path,
                   preflight: Callable[[], None], subject: str | None = None, world: World | None = None,
                   message: str | None = None, new_thread: bool = False) -> HostLaunch:
        """Bind one Task and apply its explicit control with the same Host intent."""
        if (kind not in {"run", "resume", "approve"} or not action_id or not command
                or any(not part or "\0" in part for part in command) or not environment_carrier.is_absolute()):
            raise KernelError("host_request_invalid")
        preflight()  # External I/O precedes both Task binding and durable Action.
        intent = {"kind": kind, "subject": subject, "message": message, "new_thread": new_thread,
                  "command": list(command), "environment_carrier": str(environment_carrier)}
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            run_id, task = run["id"], self._task(run)
        with locked_task(task) as location:
            prior = read_task(location)
            if prior is not None and prior != (run_id, self.storage.path.resolve()):
                if not Kernel(prior[1])._replaceable(task, prior[0]):
                    raise KernelError("task_run_busy")
            if prior != (run_id, self.storage.path.resolve()):
                bind_task(location, run_id, self.storage.path)
            # A crash here preserves the bound, fully created Run even without a PID.
            with self.storage.transaction() as tx:
                old = tx.rows("SELECT * FROM hosts WHERE action_id=?", (action_id,))
                if old:
                    value = json.loads(tx.get(old[0]["request_id"], old[0]["subject"]))
                    if value["intent"] != intent:
                        raise KernelError("action_identity_changed")
                    return self._host_launch(tx, old[0])
                run = tx.one("SELECT * FROM runs")
                if (run["executor"] is not None or tx.rows("SELECT owner FROM hosts WHERE status!='exited' OR usage_released=0 LIMIT 1")
                        or tx.rows("SELECT owner FROM retired_executors WHERE status!='exited' LIMIT 1")
                        or tx.rows("SELECT id FROM leases LIMIT 1")):
                    raise KernelError("executor_exit_unproven")
                if tx.rows("SELECT id FROM actions WHERE id=?", (action_id,)):
                    raise KernelError("action_already_applied_without_host")
                material_owner = subject or tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
                if kind == "run":
                    if run["stopped"]:
                        raise KernelError("operator_stopped")
                    payload = tx.put(material_owner, "control_intent", wire_json(intent))
                    receipt = tx.put(material_owner, "control_receipt", wire_json({"action": action_id, "kind": kind, "applied": True}))
                    tx.execute("INSERT INTO actions(id,run_id,kind,subject,payload_id,status,receipt_id) VALUES (?,?,?,?,?,'applied',?)",
                               (action_id, run_id, kind, subject, payload, receipt))
                    self._event(tx, material_owner, "control_applied", action_id, receipt)
                else:
                    self._control(tx, kind, action_id, subject=subject, world=world, message=message, new_thread=new_thread)
                epoch = tx.one("SELECT epoch FROM runs")[0] + 1
                owner, nonce = identity(), identity()
                request = HostLaunch(run_id, epoch, owner, nonce, "agent-run-kernel-" + nonce + ".service", command, environment_carrier)
                material = tx.put(material_owner, "host_request", wire_json({**asdict(request), "environment_carrier": str(environment_carrier), "intent": intent}))
                tx.execute("INSERT INTO hosts(run_id,epoch,owner,nonce,action_id,subject,request_id,unit_name,allocated_at,status) VALUES (?,?,?,?,?,?,?,?,?,'allocated')",
                           (run_id, epoch, owner, nonce, action_id, material_owner, material, request.unit_name, self.clock()))
                tx.execute("UPDATE runs SET executor=?,epoch=?", (owner, epoch))
                self._event(tx, material_owner, "host_allocated", nonce, material)
                return request

    @staticmethod
    def _host_launch(tx: Transaction, row: sqlite3.Row) -> HostLaunch:
        value = json.loads(tx.get(row["request_id"], row["subject"]))
        return HostLaunch(value["run_id"], value["epoch"], value["owner"], value["nonce"], value["unit_name"],
                          tuple(value["command"]), Path(value["environment_carrier"]))

    def host_request(self, action_id: str) -> HostLaunch:
        """Read the original request; reading never allocates or relaunches it."""
        with self.storage.transaction(write=False) as tx:
            return self._host_launch(tx, tx.one("SELECT * FROM hosts WHERE action_id=?", (action_id,)))

    def host_usage(self, action_id: str) -> HostObservation | None:
        """Read the last accepted lease receipt, preserved across unknown reads."""
        with self.storage.transaction(write=False) as tx:
            row = tx.one("SELECT * FROM hosts WHERE action_id=?", (action_id,))
            receipts = tx.rows("SELECT material_id FROM history WHERE subject=? AND identity=? AND kind IN ('host_usage_pending','host_usage_ready','host_usage_released') ORDER BY rowid DESC LIMIT 1",
                               (row["subject"], row["nonce"]))
            return None if not receipts else HostObservation(**json.loads(tx.get(receipts[0][0], row["subject"])))

    def _host(self, tx: Transaction, request: HostLaunch) -> sqlite3.Row:
        row = tx.one("SELECT * FROM hosts WHERE run_id=? AND epoch=? AND owner=?", (request.run_id, request.epoch, request.owner))
        if self._host_launch(tx, row) != request:
            raise KernelError("host_binding_changed")
        return row

    def dispatch_host(self, request: HostLaunch) -> bool:
        with self.storage.transaction() as tx:
            row = self._host(tx, request)
            if row["dispatched_at"] is not None or row["status"] != "allocated":
                return False
            run = tx.one("SELECT * FROM runs")
            if run["stopped"] or (run["executor"], run["epoch"]) != (request.owner, request.epoch):
                return False
            tx.execute("UPDATE hosts SET status='dispatched',dispatched_at=? WHERE run_id=? AND epoch=?", (self.clock(), request.run_id, request.epoch))
            self._event(tx, row["subject"], "host_dispatched", request.nonce)
            return True

    def observe_host(self, request: HostLaunch, observation: HostObservation) -> str:
        """Settle exact manager evidence without rearming an unknown launch."""
        with self.storage.transaction() as tx:
            row = self._host(tx, request)
            if row["dispatched_at"] is None:
                raise KernelError("host_not_dispatched")
            if observation.usage_pending:
                if (observation.disposition != "unknown" or observation.unit_name != request.unit_name
                        or not observation.evidence or observation.usage_ready or observation.usage_released):
                    raise KernelError("host_handoff_invalid")
                material = tx.put(row["subject"], "host_observation", wire_json(asdict(observation)))
                prior = tx.rows("SELECT material_id FROM history WHERE subject=? AND identity=? AND kind='host_usage_pending' LIMIT 1",
                                (row["subject"], request.nonce))
                if prior:
                    if prior[0][0] != material:
                        raise KernelError("host_handoff_changed")
                    return str(row["status"])
                if row["invocation_id"] is not None or row["status"] == "exited":
                    raise KernelError("host_handoff_late")
                self._event(tx, row["subject"], "host_usage_pending", request.nonce, material)
            if row["status"] == "exited":
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
            running = (known and observation.disposition == "running" and type(observation.main_pid) is int
                       and observation.main_pid > 1 and bool(observation.start_token)
                       and (row["main_pid"] is None or (row["main_pid"], row["start_token"]) == (observation.main_pid, observation.start_token)))
            exited = (known and observation.disposition == "exited" and type(observation.exit_status) is int
                      and isinstance(observation.ended_at, (float, int)) and not isinstance(observation.ended_at, bool)
                      and math.isfinite(observation.ended_at) and observation.ended_at > 0)
            disposition = "running" if running else "exited" if exited else "unknown"
            if known and (running or exited):
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
                for output in tx.all("SELECT o.*,l.subject FROM leases l JOIN invocation_outputs o ON o.lease=l.id WHERE l.executor=? AND l.epoch=?", (request.owner, request.epoch)):
                    process = None if output["process_identity"] is None else json.loads(tx.get(output["process_identity"], output["subject"]))
                    unstarted = output["dispatch_state"] == "allocated"
                    if not unstarted and (process is None or not self._host_contains_process(row, process)):
                        continue
                    proof = tx.put(output["subject"], "worker_host_exit", wire_json({"host_nonce": request.nonce, "observation": asdict(observation)}))
                    tx.execute("UPDATE invocation_outputs SET dispatch_state=?,exited_at=?,failure_kind='fatal',start_evidence=? WHERE invocation=? AND ordinal=? AND exit_code IS NULL",
                               ("unavailable" if unstarted else "exited", observation.ended_at, proof, output["invocation"], output["ordinal"]))
                    if output["raw_id"] is None:
                        tx.execute("UPDATE invocations SET status='interrupted' WHERE id=?", (output["invocation"],))
                    tx.execute("DELETE FROM leases WHERE id=?", (output["lease"],))
                    self._event(tx, output["subject"], "worker_host_exited", output["invocation"], proof)
                tx.execute("INSERT OR IGNORE INTO retired_executors VALUES (?,?,?,'exited')", (request.run_id, request.owner, request.epoch))
                tx.execute("UPDATE retired_executors SET status='exited' WHERE run_id=? AND owner=? AND epoch=?", (request.run_id, request.owner, request.epoch))
                tx.execute("UPDATE runs SET executor=NULL,epoch=epoch+1 WHERE id=? AND executor=? AND epoch=?",
                           (request.run_id, request.owner, request.epoch))
                tx.execute("UPDATE runs SET executor=NULL WHERE id=? AND executor=? AND epoch>?", (request.run_id, request.owner, request.epoch))
            self._event(tx, row["subject"], "host_observed", request.nonce, material)
            return disposition

    @staticmethod
    def _host_contains_process(host: sqlite3.Row, process: Mapping[str, Any]) -> bool:
        group = process.get("cgroup")
        return bool(host["main_pid"] is not None
                    and (process.get("host_pid"), process.get("host_start_token"), process.get("boot_id"), process.get("host_cgroup"))
                    == (host["main_pid"], host["start_token"], host["boot_id"], host["cgroup"])
                    and isinstance(group, str) and ".." not in group.split("/")
                    and (group == host["cgroup"] or group.startswith(host["cgroup"].rstrip("/") + "/")))

    def claim_executor(self, owner: str) -> Execution:
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            if tx.rows("SELECT owner FROM hosts LIMIT 1"):
                raise KernelError("managed_host_required")
            if run["stopped"]:
                raise KernelError("operator_stopped")
            if tx.rows("SELECT owner FROM retired_executors WHERE run_id=? AND status='pending' LIMIT 1", (run["id"],)):
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
            if tx.rows("SELECT owner FROM hosts WHERE run_id=? AND owner=? AND epoch=?", (execution.run_id, execution.owner, execution.epoch)):
                raise KernelError("managed_host_exit_required")
            retired = tx.one("SELECT * FROM retired_executors WHERE run_id=? AND owner=? AND epoch=?",
                             (execution.run_id, execution.owner, execution.epoch))
            if retired["status"] == "exited":
                return
            leases = tx.all("SELECT * FROM leases WHERE executor=? AND epoch=?", (execution.owner, execution.epoch))
            if {row["invocation"] for row in leases} != set(exited_invocations):
                raise KernelError("worker_exit_unproven")
            for lease in leases:
                tx.execute("UPDATE invocations SET status='interrupted' WHERE id=? AND status='running'", (lease["invocation"],))
                tx.execute("DELETE FROM leases WHERE id=? AND invocation=?", (lease["id"], lease["invocation"]))
            tx.execute("UPDATE retired_executors SET status='exited' WHERE run_id=? AND owner=? AND epoch=?",
                       (execution.run_id, execution.owner, execution.epoch))
            tx.execute("UPDATE runs SET executor=NULL WHERE id=? AND executor=? AND epoch>?",
                       (execution.run_id, execution.owner, execution.epoch))

    def advance(self, subject: str, execution: Execution, world: World | None = None, *, allow_agent: bool = True) -> Step:
        with self.storage.transaction() as tx:
            run, job = self._owned(tx, subject, execution)
            pending = self._pending_effect(tx, subject)
            # An unknown write is read back before scope drift or any new work.
            if pending is not None:
                if pending["status"] == "sent":
                    return Step("reconcile", subject, effect=self._effect(tx, pending, execution))
                if pending["status"] == "observed" and self._effect_stale(tx, run, job, pending):
                    return self._settle_stale_effect(tx, job, pending)
                if pending["status"] == "prepared" and self._effect_stale(tx, run, job, pending):
                    tx.execute("UPDATE effects SET status='retired' WHERE id=?", (pending["id"],))
                    return Step("progress", subject, "stale_intent_retired")
                if run["stopped"] or run["blocked"] or self._gates(tx, subject):
                    return Step("paused", subject, self._pause_reason(tx, subject, run))
                if world is None:
                    return Step("observe", subject)
                if not self._current(tx, job, run, world):
                    return Step("paused", subject, "currentness")
                if pending["status"] == "observed":
                    return self._apply_effect(tx, run, job, pending, world)
                return Step("effect", subject, effect=self._effect(tx, pending, execution))
            if (job["refresh_revision"] is not None and not run["stopped"] and run["blocked"] == "final_refresh"
                    and not any(gate["code"] == "operator_stopped" for gate in self._gates(tx, subject))):
                if world is None:
                    return Step("observe", subject)
                return self._refresh_final(tx, run, job, world)
            if run["stopped"] or run["blocked"] or self._gates(tx, subject):
                return Step("paused", subject, self._pause_reason(tx, subject, run))
            optional = tx.rows("SELECT e.* FROM effects e JOIN issue_links l ON l.effect_id=e.id WHERE l.subject=? AND l.state='pending' AND e.status='prepared'", (subject,))
            if optional:
                if world is None:
                    return Step("observe", subject)
                if not self._current(tx, job, run, world):
                    return Step("paused", subject, "currentness")
                return Step("effect", subject, effect=self._effect(tx, optional[0], execution))
            if job["validation_cleanup"]:
                if world is None:
                    return Step("observe", subject)
                if not self._current(tx, job, run, world):
                    return Step("paused", subject, "currentness")
                originals = [json.loads(tx.get(row[0], subject)) for row in tx.all(
                    "SELECT payload_id FROM effects WHERE subject=? AND operation='validation_checkout' AND status='confirmed'",
                    (subject,))]
                originals = [value for value in originals if value["checkout"] == job["validation_cleanup"]]
                if len(originals) != 1:
                    raise KernelError("validation_origin_unproven")
                original = originals[0]
                return self._prepare_effect(tx, run, job, execution, world, "validation_cleanup", before=original["reviewed_commit"],
                                            payload={key: original[key] for key in ("checkout", "owned_root", "common_git_dir", "reviewed_commit")})
            if job["phase"] == "complete":
                return Step("complete", subject)
            eligibility = self._eligibility(tx, job)
            if eligibility is not None:
                return Step("waiting", subject, eligibility)
            dependencies = tx.all("SELECT s.phase FROM dependencies d JOIN subjects s ON s.id=d.predecessor WHERE d.subject=?", (subject,))
            if any(row[0] != "complete" for row in dependencies):
                return Step("waiting", subject, "dependencies")
            if world is None:
                return Step("observe", subject)
            if job["current_attempt"]:
                attempt = tx.one("SELECT * FROM attempts WHERE id=?", (job["current_attempt"],))
                if attempt["result_id"]:
                    return self._consume(tx, run, job, attempt, world, execution, allow_agent)
                if not self._current(tx, job, run, world):
                    return Step("paused", subject, "currentness")
                invocation = tx.one("SELECT * FROM invocations WHERE id=?", (attempt["current_invocation"],))
                if invocation["status"] == "running":
                    output = tx.one("SELECT * FROM invocation_outputs WHERE invocation=? ORDER BY ordinal DESC LIMIT 1", (invocation["id"],))
                    if output["dispatch_state"] == "allocated" and self.clock() >= invocation["deadline_at"]:
                        evidence = tx.put(subject, "allocation_expired", wire_json({"invocation": invocation["id"], "ordinal": output["ordinal"],
                                          "deadline_at": invocation["deadline_at"]}))
                        tx.execute("UPDATE invocation_outputs SET dispatch_state='unavailable',start_evidence=? WHERE invocation=? AND ordinal=?",
                                   (evidence, invocation["id"], output["ordinal"]))
                        tx.execute("UPDATE invocations SET status='interrupted' WHERE id=?", (invocation["id"],))
                        tx.execute("DELETE FROM leases WHERE id=?", (output["lease"],))
                        self._gate(tx, subject, "execution_failed", evidence=evidence)
                        return Step("paused", subject, "invocation_deadline_exceeded")
                    return Step("waiting", subject, "agent_running")
                output = tx.one("SELECT i.*,o.* FROM invocations i JOIN invocation_outputs o ON o.invocation=i.id WHERE i.id=? ORDER BY ordinal DESC LIMIT 1", (invocation["id"],))
                if output["failure_kind"] == "capacity" and self.clock() < output["recovery_ready_at"]:
                    return Step("waiting", subject, "capacity_recovery_wait")
                if output["failure_kind"] is not None and attempt["resume_action"] is None:
                    return self._recover_agent(tx, run, job, attempt, world, execution, allow_agent, output)
                return self._allocate(tx, run, job, execution, world, allow_agent, attempt)
            if not self._current(tx, job, run, world):
                return Step("paused", subject, "currentness")
            if job["phase"] in {"development", "review", "publish"}:
                return self._allocate(tx, run, job, execution, world, allow_agent)
            return self._mechanical(tx, run, job, execution, world)

    def advance_cleanup(self, subject: str, execution: Execution, world: World | None = None) -> Step:
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
                if world is None:
                    return Step("observe", subject)
                return Step("effect", subject, effect=self._effect(tx, pending, execution))
            if run["stopped"] or run["blocked"] or self._gates(tx, subject):
                return Step("paused", subject, "cleanup_authority")
            if world is None:
                return Step("observe", subject)
            operation = self.CLEANUP[cleanup["stage"]]
            head = cleanup["workspace_sha"] if operation == "cleanup_worktree" else cleanup["source_sha"]
            payload = {"checkout": cleanup["checkout"], "owned_root": cleanup["owned_root"],
                       "common_git_dir": cleanup["common_git_dir"], "branch_ref": cleanup["source_ref"], "expected_head": head,
                       "remote": self._inputs(tx, run)["repository_remote"]}
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
            invocation = self._invocation(tx, call)
            material = tx.put(call.subject, "agent_raw", raw)
            if invocation["raw_id"] is not None:
                if invocation["raw_id"] != material:
                    raise KernelError("conflicting_return")
                return
            if invocation["status"] not in {"running", "interrupted"}:
                raise KernelError("invocation_already_settled")
            tx.execute("UPDATE invocation_outputs SET raw_id=?,returned_at=? WHERE invocation=? AND ordinal=?",
                       (material, self.clock(), call.invocation_id, call.output_ordinal))
            # A late output or superseded Invocation is history only.
            if self._latest_output(tx, invocation):
                tx.execute("UPDATE invocations SET status='returned' WHERE id=?", (call.invocation_id,))
                tx.execute("UPDATE attempts SET result_id=?,status='returned' WHERE id=? AND current_invocation=?",
                           (material, call.attempt_id, call.invocation_id))
            current = self._job(tx, call.subject)
            if invocation["generation"] != current["generation"]:
                attempt = tx.one("SELECT * FROM attempts WHERE id=?", (call.attempt_id,))
                if attempt["role"] == Role.REVIEW:
                    try:
                        artifact(Role.REVIEW, raw)
                    except KernelError:
                        pass
                    else:
                        self._charge_review(tx, attempt)
            if invocation["dispatch_state"] not in {"sent", "started", "unknown"}:
                tx.execute("DELETE FROM leases WHERE id=? AND invocation=?", (call.lease_id, call.invocation_id))
            self._event(tx, call.subject, "raw_returned", call.invocation_id, material, generation=invocation["generation"])

    def bind_thread(self, call: AgentCall, reported_thread: str) -> None:
        """Persist a real backend thread.started event, separate from logical ID."""
        if not reported_thread:
            raise KernelError("missing_thread")
        with self.storage.transaction() as tx:
            self._invocation(tx, call)
            thread = tx.one("SELECT backend_id FROM threads WHERE id=?", (call.thread_id,))
            if thread[0] is not None and thread[0] != reported_thread:
                raise KernelError("thread_identity_changed")
            tx.execute("UPDATE threads SET backend_id=? WHERE id=?", (reported_thread, call.thread_id))

    def _invocation(self, tx: Transaction, call: AgentCall) -> sqlite3.Row:
        row = tx.one("SELECT i.*,o.*,a.subject,a.role,a.purpose,a.generation,a.parent_revision,a.graph_revision,a.revision,a.current_invocation FROM invocations i JOIN invocation_outputs o ON o.invocation=i.id JOIN attempts a ON a.id=i.attempt WHERE i.id=? AND o.ordinal=?", (call.invocation_id, call.output_ordinal))
        if (row["attempt"] != call.attempt_id or row["subject"] != call.subject or row["thread"] != call.thread_id
                or row["lease"] != call.lease_id or row["executor"] != call.execution.owner or row["epoch"] != call.execution.epoch
                or row["role"] != call.role or row["purpose"] != call.purpose
                or row["mode"] != call.mode or row["requested_thread"] != call.reported_thread
                or row["deadline_seconds"] != call.deadline_seconds or row["deadline_at"] != call.deadline_at
                or json.loads(tx.get(row["context_id"], call.subject)) != call.context):
            raise KernelError("invocation_binding")
        binding = tx.one("SELECT * FROM threads WHERE id=?", (call.thread_id,))
        profile, reference = self._thread_profile(tx, binding)
        if call.profile != profile or call.profile_revision != binding["profile_revision"] or call.profile_reference != reference:
            raise KernelError("invocation_binding")
        return row

    def _latest_output(self, tx: Transaction, output: sqlite3.Row) -> bool:
        return bool(output["ordinal"] == tx.one("SELECT max(ordinal) FROM invocation_outputs WHERE invocation=?", (output["id"],))[0])

    def _agent_authority(self, tx: Transaction, call: AgentCall) -> sqlite3.Row:
        run, job = self._owned(tx, call.subject, call.execution)
        invocation = self._invocation(tx, call)
        if self.clock() >= invocation["deadline_at"]:
            raise KernelError("invocation_deadline_exceeded")
        if (run["stopped"] or run["blocked"] or self._gates(tx, call.subject)
                or invocation["generation"] != job["generation"] or job["current_attempt"] != call.attempt_id
                or invocation["parent_revision"] != run["parent_revision"]
                or invocation["graph_revision"] != run["graph_revision"] or invocation["revision"] != job["revision"]
                or invocation["current_invocation"] != call.invocation_id or not self._latest_output(tx, invocation)
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
            tx.execute("UPDATE invocation_outputs SET dispatch_state='sent',dispatched_at=? WHERE invocation=? AND ordinal=?",
                       (self.clock(), call.invocation_id, call.output_ordinal))
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
            if hosts and not self._host_contains_process(hosts[0], process_identity):
                raise KernelError("worker_host_binding")
            witness = tx.put(call.subject, "worker_process_identity", wire_json(process_identity))
            if invocation["process_identity"] is not None:
                if invocation["process_identity"] != witness:
                    raise KernelError("process_identity_changed")
                return
            if invocation["dispatch_state"] not in {"sent", "unknown"}:
                raise KernelError("agent_not_dispatched")
            tx.execute("UPDATE invocation_outputs SET process_identity=?,dispatch_state='started',started_at=? WHERE invocation=? AND ordinal=?",
                       (witness, self.clock(), call.invocation_id, call.output_ordinal))
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
            tx.execute("UPDATE invocation_outputs SET dispatch_state='unknown',start_evidence=? WHERE invocation=? AND ordinal=?",
                       (material, call.invocation_id, call.output_ordinal))
            run = tx.one("SELECT * FROM runs")
            job = self._job(tx, call.subject)
            if (not run["stopped"] and not run["blocked"] and run["executor"] == call.execution.owner and run["epoch"] == call.execution.epoch
                    and job["generation"] == invocation["generation"] and job["current_attempt"] == call.attempt_id
                    and invocation["current_invocation"] == call.invocation_id and self._latest_output(tx, invocation)
                    and not self._gates(tx, call.subject)):
                self._gate(tx, call.subject, "worker_start_unknown", evidence=material)

    def agent_unavailable(self, call: AgentCall, code: str, evidence: dict[str, Any] | str) -> None:
        """A proven pre-launch failure pauses the allocated semantic Attempt."""
        with self.storage.transaction() as tx:
            invocation = self._invocation(tx, call)
            if invocation["process_identity"] is not None or invocation["dispatch_state"] not in {"sent", "unavailable"}:
                raise KernelError("worker_exit_unproven")
            material = tx.put(call.subject, "worker_unavailable", wire_json({"code": code, "evidence": evidence}))
            tx.execute("UPDATE invocation_outputs SET dispatch_state='unavailable',start_evidence=? WHERE invocation=? AND ordinal=?",
                       (material, call.invocation_id, call.output_ordinal))
            if self._latest_output(tx, invocation):
                tx.execute("UPDATE invocations SET status='interrupted' WHERE id=?", (call.invocation_id,))
            tx.execute("DELETE FROM leases WHERE id=? AND invocation=?", (call.lease_id, call.invocation_id))
            run = tx.one("SELECT * FROM runs")
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
            old = tx.rows("SELECT * FROM worker_activity WHERE invocation=? AND ordinal=? AND identity=?",
                          (call.invocation_id, call.output_ordinal, observation_id))
            if old:
                if (old[0]["active"], old[0]["observed_at"], old[0]["evidence"]) != (active, observed_at, material):
                    raise KernelError("conflicting_activity_observation")
                return
            tx.execute("INSERT INTO worker_activity VALUES (?,?,?,?,?,?)",
                       (call.invocation_id, call.output_ordinal, observation_id, active, observed_at, material))

    def _process_exit(self, tx: Transaction, call: AgentCall, invocation: sqlite3.Row, returncode: int, failure: str | None,
                      failure_kind: str, diagnostic: Mapping[str, Any] | None = None) -> None:
        if invocation["exit_code"] is not None:
            if invocation["exit_code"] != returncode:
                raise KernelError("conflicting_exit")
            return
        failed = returncode != 0 or failure is not None or invocation["raw_id"] is None
        exited_at = self.clock()
        tx.execute("UPDATE invocation_outputs SET dispatch_state='exited',exit_code=?,exited_at=?,failure_kind=?,recovery_ready_at=? WHERE invocation=? AND ordinal=?",
                   (returncode, exited_at, failure_kind if failed else None,
                    exited_at + 30 if failed and failure_kind == "capacity" else None, call.invocation_id, call.output_ordinal))
        tx.execute("DELETE FROM leases WHERE id=? AND invocation=?", (call.lease_id, call.invocation_id))
        self._event(tx, call.subject, "worker_exited", call.invocation_id, invocation["process_identity"], generation=invocation["generation"])
        if failed:
            material = tx.put(call.subject, "worker_failure", wire_json({"returncode": returncode, "failure": failure, "kind": failure_kind,
                                                                       "diagnostic": diagnostic}))
            tx.execute("UPDATE invocation_outputs SET start_evidence=? WHERE invocation=? AND ordinal=?",
                       (material, call.invocation_id, call.output_ordinal))
            if invocation["raw_id"] is None and self._latest_output(tx, invocation):
                tx.execute("UPDATE invocations SET status='interrupted' WHERE id=?", (call.invocation_id,))
            run = tx.one("SELECT * FROM runs")
            job = self._job(tx, call.subject)
            if (not run["stopped"] and not run["blocked"] and run["executor"] == call.execution.owner and run["epoch"] == call.execution.epoch
                    and not self._gates(tx, call.subject) and job["current_attempt"] == call.attempt_id
                    and invocation["current_invocation"] == call.invocation_id and self._latest_output(tx, invocation)
                    and job["generation"] == invocation["generation"] and failure_kind == "fatal"
                    and invocation["raw_id"] is None):
                self._gate(tx, call.subject, "execution_failed", evidence=material)

    def guard_effect(self, effect: Effect, execution: Execution, world: World) -> None:
        """Read-only authority guard for each real successor mutation in an adapter."""
        with self.storage.transaction(write=False) as tx:
            run, job = self._owned(tx, effect.subject, execution)
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            self._guard_publisher(tx, run, row, world)
            if effect.operation in self.CLEANUP:
                if (row["status"] != "sent" or run["stopped"] or run["blocked"] or self._gates(tx, effect.subject)
                        or not self._cleanup_current(tx, job, row) or self._effect(tx, row, execution) != effect):
                    raise KernelError("operation_cancelled")
                return
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
            if effect.operation in {"integrate", "run_branch", "publication_commit", "push", "pr_write", "merge"} and effect.base.sha != world.target.sha:
                raise KernelError("operation_boundary_changed")
            if effect.operation == "pr_write" and not self._pr_write_current(run, job, world):
                raise KernelError("pr_identity_changed")
            if effect.operation == "merge" and not self._merge_authority(tx, run, job, world):
                raise KernelError("merge_authority_changed")

    def control(self, kind: str, action_id: str, *, subject: str | None = None,
                world: World | None = None, message: str | None = None, new_thread: bool = False) -> str:
        """Apply one explicit operator intent atomically, with its application receipt.

        The offline prototype calls this trusted interface directly. Host launch,
        public capability isolation and process-exit proofs are separate adapters.
        """
        with self.storage.transaction() as tx:
            return self._control(tx, kind, action_id, subject=subject, world=world, message=message, new_thread=new_thread)

    def _control(self, tx: Transaction, kind: str, action_id: str, *, subject: str | None = None,
                 world: World | None = None, message: str | None = None, new_thread: bool = False) -> str:
        if kind not in {"stop", "resume", "approve"}:
            raise KernelError("prototype_control_unimplemented", kind)
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
                targets = tx.all("SELECT id FROM subjects WHERE run_id=?", (run["id"],))
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
                tx.execute("UPDATE attempts SET resume_action=? WHERE id IN (SELECT current_attempt FROM subjects WHERE run_id=?)",
                           (action_id, run["id"]))
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
                    elif code == "integration_resync_exhausted":
                        self._renew_resync(tx, job)
                    elif code == "human_blocker":
                        if job["current_attempt"]:
                            tx.execute("UPDATE attempts SET result_id=NULL,status='continuing' WHERE id=?", (job["current_attempt"],))
                    elif code in {"worker_unavailable", "execution_failed"}:
                        pass  # Explicitly continue the same charged Attempt.
                    elif code == "worker_start_unknown":
                        if not job["current_attempt"]:
                            raise KernelError("worker_exit_unproven")
                        invocation = tx.one("SELECT i.*,o.* FROM invocations i JOIN attempts a ON a.current_invocation=i.id JOIN invocation_outputs o ON o.invocation=i.id WHERE a.id=? ORDER BY o.ordinal DESC LIMIT 1", (job["current_attempt"],))
                        if invocation["dispatch_state"] != "exited" or invocation["process_identity"] is None:
                            raise KernelError("worker_exit_unproven")
                        if invocation["raw_id"] is None:
                            tx.execute("UPDATE invocations SET status='interrupted' WHERE id=?", (invocation["id"],))
                    elif code == "publication_retry_exhausted":
                        tx.execute("UPDATE effects SET retry_baseline=attempts WHERE subject=? AND status='sent'", (subject,))
                    elif code == "write_conflict":
                        pending_write = self._pending_effect(tx, subject)
                        if pending_write is None or pending_write["status"] != "observed":
                            raise KernelError("unresolved_write_conflict")
                    elif code not in {"operator_stopped", "execution_failed", "write_result_unknown", "ci_investigation_paused", "supervision_timeout"}:
                        continue
                    tx.execute("DELETE FROM gates WHERE subject=? AND code=?", (subject, code))
                if new_thread and job["current_attempt"]:
                    tx.execute("UPDATE attempts SET new_thread=1 WHERE id=?", (job["current_attempt"],))
                if job["current_attempt"] and not self._gates(tx, subject):
                    tx.execute("UPDATE attempts SET resume_action=? WHERE id=?", (action_id, job["current_attempt"]))
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
        tx.execute("INSERT INTO budgets VALUES (?,?,?,?,?,?,?,?,1)",
                   (job["id"], purpose, old["window"] + 1, developments, reviews, limit, old["review_limit"], old["review_required"]))
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
                "resource_cleanup": next((dict(row) for row in tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND generation=?", (subject, job["generation"]))), None),
                "issue_link": next((dict(row) for row in tx.rows("SELECT * FROM issue_links WHERE subject=?", (subject,))), None),
                "retired_generations": [dict(row) for row in tx.all("SELECT * FROM retired_generations WHERE subject=? ORDER BY generation", (subject,))],
            }

    def scheduling(self) -> dict[str, Any]:
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            jobs = tx.all("SELECT * FROM subjects ORDER BY id")
            active = tx.all("SELECT l.*,i.deadline_at,o.dispatch_state FROM leases l JOIN invocations i ON i.id=l.invocation JOIN invocation_outputs o ON o.invocation=i.id AND o.lease=l.id ORDER BY l.subject")
            now = self.clock()
            deadlines = [float(row["deadline_at"]) for row in active]
            progress = any(row["dispatch_state"] == "allocated" and row["deadline_at"] <= now for row in active)
            leased = {row["subject"] for row in active}
            for job in jobs:
                subject = job["id"]
                gates = {row["code"] for row in self._gates(tx, subject)}
                pending = self._pending_effect(tx, subject)
                if pending is not None:
                    if pending["status"] == "sent":
                        progress |= not bool(gates & {"write_result_unknown", "write_conflict", "publication_retry_exhausted"})
                    elif self._effect_stale(tx, run, job, pending):
                        progress = True
                refreshing = (job["refresh_revision"] is not None and run["blocked"] == "final_refresh"
                              and "operator_stopped" not in gates)
                if run["stopped"] or (run["blocked"] and not refreshing) or (gates and not refreshing):
                    continue
                if pending is not None:
                    progress |= pending["status"] != "sent"
                    continue
                if refreshing or job["validation_cleanup"] or tx.rows("SELECT subject FROM issue_links WHERE subject=? AND state='pending'", (subject,)):
                    progress = True
                    continue
                cleanup = tx.rows("SELECT c.*,e.status AS effect_status FROM resource_cleanup c LEFT JOIN effects e ON e.id=c.effect_id WHERE c.subject=? AND c.generation=? AND c.state!='complete'",
                                  (subject, job["generation"]))
                if cleanup:
                    progress |= (cleanup[0]["effect_status"] in {"prepared", "observed", "cancelled"}
                                 or cleanup[0]["outcome"] is None)
                if job["phase"] in {"complete", "approval"} or subject in leased or self._eligibility(tx, job) is not None:
                    continue
                if tx.rows("SELECT s.id FROM dependencies d JOIN subjects s ON s.id=d.predecessor WHERE d.subject=? AND s.phase!='complete' LIMIT 1", (subject,)):
                    continue
                waits = (tx.rows("SELECT deadline FROM waits WHERE subject=?", (subject,))
                         if job["phase"] in {"checks", "merge"} else [])
                if waits:
                    deadlines.extend(float(row[0]) for row in waits)
                    progress |= any(row[0] <= now for row in waits)
                    continue
                if job["current_attempt"]:
                    recovery = tx.rows("SELECT o.failure_kind,o.recovery_ready_at FROM attempts a JOIN invocation_outputs o ON o.invocation=a.current_invocation WHERE a.id=? ORDER BY o.ordinal DESC LIMIT 1",
                                       (job["current_attempt"],))
                    if recovery and recovery[0]["failure_kind"] == "capacity" and recovery[0]["recovery_ready_at"] > now:
                        deadlines.append(float(recovery[0]["recovery_ready_at"]))
                        continue
                if job["phase"] == "integration" and job["kind"] == DeliveryKind.TICKET and run["integration_holder"] not in {None, subject}:
                    continue
                progress = True
            final_pending = bool(tx.rows("SELECT id FROM final_target WHERE id NOT IN (SELECT id FROM subjects WHERE kind='final_run')"))
            if final_pending and not run["stopped"] and not run["blocked"]:
                tickets = [job for job in jobs if job["kind"] == DeliveryKind.TICKET]
                progress |= bool(tickets) and all(self._completion(tx, job) is not None for job in tickets)
            complete = (all(job["phase"] == "complete" for job in jobs) and not final_pending
                        and not active and not tx.rows("SELECT subject FROM resource_cleanup WHERE state!='complete' LIMIT 1")
                        and not tx.rows("SELECT id FROM effects WHERE status IN ('prepared','sent','observed') AND operation!='issue_link' LIMIT 1"))
            disposition = "progress" if progress else "waiting" if deadlines else "complete" if complete else "quiescent"
            return {"concurrency": run["concurrency"], "integration_holder": run["integration_holder"],
                    "active": [{key: row[key] for key in ("id", "subject", "invocation")} for row in active],
                    "work_fifo": [row[0] for row in tx.all("SELECT subject FROM work_queue ORDER BY ready")],
                    "integration_fifo": [row[0] for row in tx.all("SELECT subject FROM integration_queue ORDER BY ready")],
                    "disposition": disposition, "next_deadline_at": min(deadlines) if deadlines else None}

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
        self._executor_owned(tx, run, execution)
        return run, job

    @staticmethod
    def _executor_owned(tx: Transaction, run: sqlite3.Row, execution: Execution) -> None:
        if (run["id"] != execution.run_id or run["executor"] != execution.owner or run["epoch"] != execution.epoch):
            raise KernelError("executor_fenced")
        hosts = tx.rows("SELECT status,usage_ready FROM hosts WHERE run_id=? AND owner=? AND epoch=?", (execution.run_id, execution.owner, execution.epoch))
        if hosts and (hosts[0]["status"] != "running" or not hosts[0]["usage_ready"]):
            raise KernelError("host_usage_unproven")

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
            if not self._final_closing(tx, run, job):
                self._gate(tx, job["id"], "requeue_required")
                return False
        if world.completion_revision != job["completion_revision"]:
            self._gate(tx, job["id"], "completion_changed")
            return False
        return True

    def _policy(self, run: sqlite3.Row) -> Policy:
        return Policy(**json.loads(run["policy"]))

    def _store_profiles(self, tx: Transaction, subject: str, profiles: Mapping[str, Any], source: Mapping[str, Any]) -> int:
        revision = tx.one("SELECT coalesce(max(id),0)+1 FROM profile_revisions")[0]
        material = tx.put(subject, "profile_revision", wire_json({**profiles, "source": dict(source)}))
        tx.execute("INSERT INTO profile_revisions VALUES (?,?,?,?)", (revision, subject, material, self.clock()))
        return int(revision)

    def _profile_document(self, tx: Transaction, revision: int) -> dict[str, Any]:
        row = tx.one("SELECT * FROM profile_revisions WHERE id=?", (revision,))
        return dict(json.loads(tx.get(row["material_id"], row["subject"])))

    def _thread_profile(self, tx: Transaction, thread: sqlite3.Row) -> tuple[Profile, str | None]:
        value = self._profile_document(tx, thread["profile_revision"])["profiles"][thread["role"]]
        return Profile(value["model"], value["reasoning_effort"]), value["reference"]

    def configure_profiles(self, action_id: str, *, preset: str | None = None, overrides: ProfileOverrides | None = None) -> int:
        """An explicit immutable revision applies only when a new Thread is allocated."""
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            owner = tx.one("SELECT subject FROM profile_revisions WHERE id=?", (run["profile_revision"],))[0]
            payload = wire_json({"kind": "configure_profiles", "preset": preset, "overrides": dict(overrides or {})})
            existing = tx.rows("SELECT * FROM actions WHERE id=?", (action_id,))
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
            tx.execute("UPDATE runs SET profile_revision=?", (revision,))
            self._event(tx, owner, "profiles_configured", action_id, receipt)
            return revision

    def _inputs(self, tx: Transaction, run: sqlite3.Row) -> dict[str, Any]:
        rows = tx.rows("SELECT * FROM run_inputs WHERE run_id=?", (run["id"],))
        return {} if not rows else dict(json.loads(tx.get(rows[0]["material_id"], rows[0]["subject"])))

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
                  world: World, allow_agent: bool, pending: sqlite3.Row | None = None,
                  recovery: sqlite3.Row | None = None, *, output_error: str | None = None) -> Step:
        subject = job["id"]
        if tx.rows("SELECT id FROM leases WHERE subject=?", (subject,)):
            return Step("waiting", subject, "previous_invocation_exit")
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
        attempt_id = pending["id"] if pending is not None else identity()
        previous_output = None if pending is None else tx.one(
            "SELECT i.*,o.* FROM invocations i JOIN invocation_outputs o ON o.invocation=i.id WHERE i.id=? ORDER BY ordinal DESC LIMIT 1",
            (pending["current_invocation"],))
        thread_id = None
        if pending is not None and not pending["new_thread"]:
            previous = tx.one("SELECT thread FROM invocations WHERE id=?", (pending["current_invocation"],))
            thread_id = previous["thread"]
        elif pending is None and role == Role.DEVELOPMENT and not policy.new_thread_per_development:
            thread_id = job["development_thread"]
        if thread_id is None:
            thread_id = identity()
            tx.execute("INSERT INTO threads(id,subject,role,profile_revision) VALUES (?,?,?,?)", (thread_id, subject, role.value, run["profile_revision"]))
        binding = tx.one("SELECT * FROM threads WHERE id=?", (thread_id,))
        if binding["subject"] != subject or binding["role"] != role.value:
            raise KernelError("thread_role_binding")
        profile, reference = self._thread_profile(tx, binding)
        if role == Role.DEVELOPMENT:
            tx.execute("UPDATE subjects SET development_thread=? WHERE id=?", (thread_id, subject))
        context = self._context(tx, run, job, world, role) if pending is None else {}
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
            assert previous_output is not None
            context = json.loads(tx.get(previous_output["context_id"], subject))
            if role == Role.REVIEW:
                context["checkout"] = job["validation_checkout"]
            if pending["status"] == "continuing" and pending["outcome"] in {"human_blocker", "blocked"}:
                blocked = artifact(role, tx.get(previous_output["raw_id"], subject))
                context["prior_human_blockers"] = (blocked["human_blockers"] if role != Role.REVIEW else
                    [lane["evidence"] for lane in blocked["checks"].values() if lane["status"] == "blocked"])
        if output_error is not None:
            context.update(readonly=True, contract_error=output_error[:2000])
        context["semantic_continuation"] = pending is not None
        responses = tx.all("SELECT material_id FROM history WHERE subject=? AND generation=? AND kind='human_response' ORDER BY position", (subject, job["generation"]))
        if responses:
            context["human_responses"] = [tx.get(row[0], subject).decode() for row in responses]
        invocation_id, lease_id = identity(), identity()
        context_id = tx.put(subject, "role_context", wire_json(context))
        backend_thread = tx.one("SELECT backend_id FROM threads WHERE id=?", (thread_id,))[0]
        mode = "fresh" if backend_thread is None else "resume"
        ordinal = 0 if previous_output is None else previous_output["ordinal"]
        if output_error is not None:
            ordinal += 1
        if ordinal and backend_thread is not None:
            mode = "output-repair"
        allocated_at = self.clock()
        deadline_seconds = (previous_output["deadline_seconds"] if previous_output is not None else
                            {Role.DEVELOPMENT: policy.development_deadline_seconds, Role.REVIEW: policy.review_deadline_seconds,
                             Role.PUBLICATION: policy.publication_deadline_seconds}[role])
        deadline_at = allocated_at + deadline_seconds
        recovery_kind = None if previous_output is None else "explicit" if recovery is None else recovery["failure_kind"]
        tx.execute("INSERT INTO invocations(id,attempt,thread,executor,epoch,status,allocated_at,deadline_seconds,deadline_at,predecessor,recovery_kind,action_id) VALUES (?,?,?,?,?,'running',?,?,?,?,?,?)",
                   (invocation_id, attempt_id, thread_id, execution.owner, execution.epoch, allocated_at, deadline_seconds, deadline_at,
                    None if previous_output is None else previous_output["id"], recovery_kind, None if pending is None else pending["resume_action"]))
        tx.execute("INSERT INTO invocation_outputs(invocation,ordinal,context_id,lease,allocated_at,mode,requested_thread) VALUES (?,?,?,?,?,?,?)",
                   (invocation_id, ordinal, context_id, lease_id, allocated_at, mode, backend_thread))
        tx.execute("UPDATE attempts SET current_invocation=?,result_id=NULL,status='running',new_thread=0,resume_action=NULL WHERE id=?", (invocation_id, attempt_id))
        tx.execute("INSERT INTO leases VALUES (?,?,?,?,?)", (lease_id, subject, invocation_id, execution.owner, execution.epoch))
        tx.execute("UPDATE subjects SET current_attempt=?,version=version+1 WHERE id=?", (attempt_id, subject))
        self._event(tx, subject, "agent_allocated", invocation_id)
        call = AgentCall(attempt_id, invocation_id, subject, role, Purpose(job["purpose"]), thread_id,
                         lease_id, execution, context, profile, deadline_seconds, deadline_at, mode, backend_thread, ordinal,
                         binding["profile_revision"], reference)
        return Step("agent", subject, call=call)

    def _recover_agent(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, attempt: sqlite3.Row,
                       world: World, execution: Execution, allow_agent: bool, previous: sqlite3.Row) -> Step:
        subject, kind = job["id"], previous["failure_kind"]
        if tx.rows("SELECT id FROM leases WHERE subject=?", (subject,)):
            return Step("waiting", subject, "previous_invocation_exit")
        backend = tx.one("SELECT backend_id FROM threads WHERE id=?", (previous["thread"],))[0]
        used = tx.one("SELECT count(*) FROM invocations WHERE attempt=? AND recovery_kind='ordinary'", (attempt["id"],))[0]
        if (previous["dispatch_state"] != "exited" or previous["process_identity"] is None or backend is None
                or kind not in {"ordinary", "capacity"} or (kind == "ordinary" and used)):
            self._gate(tx, subject, "execution_failed", evidence=previous["start_evidence"])
            return Step("paused", subject, "execution_recovery_unavailable")
        if kind == "capacity" and (previous["recovery_ready_at"] is None or self.clock() < previous["recovery_ready_at"]):
            return Step("waiting", subject, "capacity_recovery_wait")
        # Every recovery crosses the same current owner/requirements/eligibility
        # decision as a new dispatch, after the exact former process exited.
        if (not self._current(tx, job, run, world) or attempt["generation"] != job["generation"]
                or attempt["candidate_sha"] != job["candidate_sha"] or self._eligibility(tx, job) is not None):
            return Step("paused", subject, "recovery_currentness")
        return self._allocate(tx, run, job, execution, world, allow_agent, attempt, previous)

    def _context(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World, role: Role) -> dict[str, Any]:
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
            "base_sha": job["base_sha"] if job["purpose"] == Purpose.REQUIREMENTS else world.target.sha,
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
        if role == Role.DEVELOPMENT and job["repair_source"] is not None:
            field = {"acceptance": "acceptance_artifact", "integration": "integration_evidence",
                     "git_integrity": "git_integrity_evidence", "required_checks": "ci_evidence"}[job["repair_source"]]
            material = job["ci_failure"] if job["repair_source"] == "required_checks" else job["last_failure"]
            context[field] = json.loads(tx.get(material, job["id"]))
        budget = self._budget(tx, job)
        if role in {Role.DEVELOPMENT, Role.REVIEW} and budget["review_limit"] is not None:
            context["review_budget_context"] = {
                "completed_review_attempts": budget["reviews_used"],
                "current_review_attempt": budget["reviews_used"] + 1,
                "remaining_review_attempts": max(0, budget["review_limit"] - budget["reviews_used"] - int(role == Role.REVIEW)),
            }
        seed = tx.rows("SELECT id FROM material WHERE subject=? AND purpose='fixture_evidence'", (job["id"],))
        if seed and role != Role.DEVELOPMENT:
            context["frozen_fixture_evidence"] = json.loads(tx.get(seed[0][0], job["id"]))
        if run["source_sequence"] is not None:
            numbers = [(run["parent_issue"], run["parent_revision"], "background" if job["kind"] == DeliveryKind.TICKET else "requirements")]
            if job["kind"] == DeliveryKind.TICKET:
                numbers.append((job["issue"], job["revision"], "requirements"))
            if job["kind"] == DeliveryKind.FINAL_RUN:
                numbers.extend((row["issue"], row["revision"], "completed_ticket") for row in tx.all("SELECT issue,revision FROM subjects WHERE kind='ticket' ORDER BY issue"))
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
        previous_context = previous_artifact = None
        if job["review_attempt"]:
            previous = tx.one("SELECT * FROM attempts WHERE id=?", (job["review_attempt"],))
            previous_context = json.loads(tx.get(previous["context_id"], job["id"]))
            previous_artifact = artifact(Role.REVIEW, tx.get(previous["result_id"], job["id"]))
        if role == Role.REVIEW:
            facts = {key: context[key] for key in (
                "acceptance_scope", "revision", "parent_revision", "graph_revision", "completion_revision",
                "requirements", "ticket_completions", "work_source", "ci_evidence",
            ) if key in context}
            facts["current_review_identity"] = self._review_identity(context)
            full = {"base_sha": context["base_sha"], "candidate_sha": context["reviewed_commit"]}
            focus, complete = full, False
            if previous_context is not None and previous_artifact is not None:
                facts.update(previous_review_identity=self._review_identity(previous_context),
                             previous_acceptance_artifact=previous_artifact)
                saved = previous_context.get("review_context")
                if isinstance(saved, dict):
                    prior_candidate = saved["full_range"]["candidate_sha"]
                    complete = (saved["full_range"]["base_sha"] == full["base_sha"]
                                and all(previous_context.get(key) == context[key] for key in (
                                    "scope", "revision", "parent_revision", "graph_revision", "completion_revision"))
                                and not any(lane["status"] == "blocked" for lane in previous_artifact["checks"].values())
                                and (prior_candidate == full["candidate_sha"] or (prior_candidate, full["candidate_sha"]) in world.ancestry))
                    if complete:
                        focus = {"base_sha": prior_candidate, "candidate_sha": full["candidate_sha"]}
            if job["purpose"] == Purpose.CI and job["kind"] != DeliveryKind.TICKET:
                accepted = tx.rows("SELECT context_id,result_id FROM attempts WHERE subject=? AND generation=? AND role='review' AND outcome='pass' AND purpose!='ci' AND revision=? AND parent_revision=? AND graph_revision=? AND completion_revision=? ORDER BY ordinal DESC LIMIT 1",
                                   (job["id"], job["generation"], job["revision"], run["parent_revision"], run["graph_revision"], job["completion_revision"]))
                if accepted:
                    accepted_context = json.loads(tx.get(accepted[0]["context_id"], job["id"]))
                    facts["parent_acceptance_baseline"] = {
                        **self._review_identity(accepted_context),
                        "artifact": artifact(Role.REVIEW, tx.get(accepted[0]["result_id"], job["id"])),
                    }
            context["review_context"] = {"facts": facts, "full_range": full, "focus_range": focus,
                                         "has_complete_baseline": complete}
        elif role == Role.PUBLICATION:
            if job["gate_mode"] == "review" and previous_context is not None and previous_artifact is not None:
                if review_outcome(previous_artifact) != "pass":
                    raise KernelError("publication_authority_missing")
                context.update(acceptance_artifact=previous_artifact,
                               acceptance_identity=self._review_identity(previous_context))
            elif job["kind"] == DeliveryKind.TICKET and job["gate_mode"] in {"deterministic_fallback", "ci_waiver"}:
                development = tx.one("SELECT result_id FROM attempts WHERE subject=? AND generation=? AND role='development' AND status='consumed' ORDER BY ordinal DESC LIMIT 1", (job["id"], job["generation"]))
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
                 world: World, execution: Execution, allow_agent: bool) -> Step:
        subject = job["id"]
        if tx.rows("SELECT id FROM leases WHERE subject=?", (subject,)):
            return Step("waiting", subject, "previous_invocation_exit")
        role = Role(attempt["role"])
        try:
            result = artifact(role, tx.get(attempt["result_id"], subject))
        except KernelError as error:
            return self._repair_output(tx, run, job, attempt, world, execution, allow_agent, str(error))
        if role == Role.REVIEW and not attempt["accounted"]:
            self._charge_review(tx, attempt)
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

    def _repair_output(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, attempt: sqlite3.Row,
                       world: World, execution: Execution, allow_agent: bool, error: str) -> Step:
        subject = job["id"]
        if (not self._current(tx, job, run, world) or attempt["generation"] != job["generation"]
                or attempt["candidate_sha"] != job["candidate_sha"]):
            self._gate(tx, subject, "stale_return", evidence=attempt["result_id"])
            return Step("paused", subject, "stale_return")
        previous = tx.one("SELECT i.*,o.* FROM invocations i JOIN invocation_outputs o ON o.invocation=i.id WHERE i.id=? ORDER BY ordinal DESC LIMIT 1",
                          (attempt["current_invocation"],))
        if tx.rows("SELECT id FROM leases WHERE subject=?", (subject,)):
            return Step("waiting", subject, "previous_invocation_exit")
        backend = tx.one("SELECT * FROM threads WHERE id=?", (previous["thread"],))
        if previous["failure_kind"] == "capacity" and self.clock() < previous["recovery_ready_at"]:
            return Step("waiting", subject, "capacity_recovery_wait")
        if attempt["resume_action"] is not None and previous["failure_kind"] is not None:
            return self._allocate(tx, run, job, execution, world, allow_agent, attempt)
        if previous["exit_code"] != 0 and previous["failure_kind"] is not None:
            return self._recover_agent(tx, run, job, attempt, world, execution, allow_agent, previous)
        ordinal = tx.one("SELECT max(o.ordinal) FROM invocation_outputs o JOIN invocations i ON i.id=o.invocation WHERE i.attempt=?", (attempt["id"],))[0] + 1
        if (previous["dispatch_state"] != "exited" or previous["exit_code"] != 0
                or previous["process_identity"] is None or backend["backend_id"] is None or ordinal > 2):
            self._gate(tx, subject, "invalid_artifact", evidence=attempt["result_id"])
            return Step("paused", subject, "invalid_artifact")
        if self.clock() >= previous["deadline_at"]:
            if attempt["resume_action"] is not None:
                return self._allocate(tx, run, job, execution, world, allow_agent, attempt, output_error=error)
            self._gate(tx, subject, "execution_failed", evidence=attempt["result_id"])
            return Step("paused", subject, "invocation_deadline_exceeded")
        if not allow_agent:
            return Step("needs_agent", subject)
        self._ready(tx, subject, "work_queue")
        if (tx.one("SELECT subject FROM work_queue ORDER BY ready LIMIT 1")[0] != subject
                or tx.one("SELECT count(*) FROM leases")[0] >= run["concurrency"]):
            return Step("waiting", subject, "agent_capacity")
        context = json.loads(tx.get(previous["context_id"], subject))
        context.update(readonly=True, contract_error=error[:2000])
        context_id = tx.put(subject, "output_repair_context", wire_json(context))
        lease_id = identity()
        tx.execute("INSERT INTO invocation_outputs(invocation,ordinal,context_id,lease,allocated_at,mode,requested_thread) VALUES (?,?,?,?,?,'output-repair',?)",
                   (previous["id"], ordinal, context_id, lease_id, self.clock(), backend["backend_id"]))
        tx.execute("INSERT INTO leases VALUES (?,?,?,?,?)", (lease_id, subject, previous["id"], execution.owner, execution.epoch))
        tx.execute("UPDATE invocations SET status='running' WHERE id=?", (previous["id"],))
        tx.execute("UPDATE attempts SET result_id=NULL,status='running' WHERE id=?", (attempt["id"],))
        tx.execute("DELETE FROM work_queue WHERE subject=?", (subject,))
        self._event(tx, subject, "output_repair_allocated", f"{previous['id']}:{ordinal}", context_id)
        profile, reference = self._thread_profile(tx, backend)
        return Step("agent", subject, call=AgentCall(attempt["id"], previous["id"], subject, Role(attempt["role"]),
                    Purpose(attempt["purpose"]), previous["thread"], lease_id, execution, context,
                    profile, previous["deadline_seconds"], previous["deadline_at"],
                    "output-repair", backend["backend_id"], ordinal, backend["profile_revision"], reference))

    def _pending_effect(self, tx: Transaction, subject: str) -> sqlite3.Row | None:
        rows = tx.rows("SELECT * FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') AND operation NOT IN ('cleanup_worktree','cleanup_local_ref','cleanup_remote_ref','issue_link')", (subject,))
        return rows[0] if rows else None

    def _effect_stale(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row) -> bool:
        if run["source_sequence"] is None:
            return False
        if self._final_closing(tx, run, job) and row["operation"] in {"merge", "completion_comment", "close"}:
            return False
        facts = tx.rows("SELECT revision FROM issue_facts WHERE issue=?", (job["issue"],))
        return bool(row["generation"] != job["generation"] or row["parent_revision"] != run["observed_parent_revision"]
                    or row["graph_revision"] != run["observed_graph_revision"] or job["refresh_revision"] is not None
                    or (job["kind"] == DeliveryKind.TICKET and facts and row["revision"] != facts[0][0])
                    or any(gate["code"] == "requeue_required" for gate in self._gates(tx, job["id"])))

    def _merge_receipt_matches(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row, result: dict[str, Any]) -> bool:
        pr, commit = result["pr"], result["commit"]
        payload = json.loads(tx.get(row["payload_id"], job["id"]))
        return bool(result["disposition"] == "target" and pr and commit and pr["number"] == row["pr_number"]
                    and self._pr_identity(run, row, pr)
                    and pr["state"] == "MERGED" and pr["actor"] == row["actor"] and pr["head"]["sha"] == row["after_sha"]
                    and pr["base"]["sha"] == row["before_sha"] and pr["merged"] == commit and commit["tree"] == job["publication_tree"]
                    and result["raw"].get("parents") == payload["parents"])

    def _final_merge_intent(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row) -> sqlite3.Row | None:
        """The frozen grant survives the bounded lifetime of an Action observer."""
        if (job["kind"] != DeliveryKind.FINAL_RUN or not job["grant_id"]
                or run["observed_graph_revision"] not in {None, run["graph_revision"]}):
            return None
        grant = tx.one("SELECT * FROM grants WHERE id=? AND subject=?", (job["grant_id"], job["id"]))
        intents = tx.rows("SELECT * FROM effects WHERE subject=? AND generation=? AND operation='merge' AND status IN ('sent','observed','confirmed') ORDER BY rowid DESC LIMIT 1",
                          (job["id"], job["generation"]))
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
        result = json.loads(tx.get(row["receipt_id"], job["id"]))
        self._validate_target(tx, tx.one("SELECT * FROM runs"), job, row, result)
        commit = result["commit"]
        operation = row["operation"]
        if result["disposition"] == "target":
            if operation in {"candidate", "integrate"} and commit:
                tx.execute("UPDATE subjects SET workspace_sha=? WHERE id=?", (commit["sha"], job["id"]))
            elif operation in {"push", "run_branch"}:
                tx.execute("UPDATE subjects SET source_sha=? WHERE id=?", (row["after_sha"], job["id"]))
                self._record_source_write(tx, job["id"], row["source_ref"], row["after_sha"])
            elif operation == "publication_commit" and commit:
                tx.execute("UPDATE subjects SET publication_sha=?,publication_tree=?,workspace_sha=? WHERE id=?",
                           (commit["sha"], commit["tree"], commit["sha"], job["id"]))
            elif operation == "pr_write" and result["pr"]:
                tx.execute("UPDATE subjects SET pr_number=? WHERE id=?", (result["pr"]["number"], job["id"]))
            elif operation == "merge" and commit:
                tx.execute("UPDATE subjects SET merge_sha=? WHERE id=?", (commit["sha"], job["id"]))
            elif operation == "validation_checkout":
                payload = json.loads(tx.get(row["payload_id"], job["id"]))
                tx.execute("UPDATE subjects SET validation_cleanup=? WHERE id=?", (payload["checkout"], job["id"]))
        tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (row["id"],))
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
        tx.execute("UPDATE subjects SET generation=?,revision=?,phase='workspace',purpose='integration',checkout=?,base_sha=?,base_tree=?,source_sha=?,workspace_sha=?,candidate_sha=?,candidate_tree=?,integrated_sha=NULL,integrated_tree=NULL,expected_tree=NULL,integration_target=NULL,current_attempt=NULL,review_attempt=NULL,publication_attempt=NULL,checks_id=NULL,grant_id=NULL,gate_mode=NULL,development_thread=NULL,ci_failure=NULL,last_failure=NULL,resume_purpose=NULL,repair_source=NULL,validation_checkout=NULL,validation_cleanup=NULL,refresh_revision=NULL,version=version+1 WHERE id=?",
                   (generation, job["refresh_revision"], checkout, world.source.sha, world.source.tree, world.source.sha, world.source.sha,
                    world.source.sha, world.source.tree, job["id"]))
        # Requirement changes do not renew any allowance or refund prior work.
        prior = tx.one("SELECT * FROM final_barriers WHERE subject=? AND generation=?", (job["id"], job["generation"]))
        tx.execute("INSERT INTO final_barriers VALUES (?,?,?,?,?,?)", (job["id"], generation, prior["completion_revision"], world.source.sha, world.target.sha, prior["evidence"]))
        tx.execute("DELETE FROM gates WHERE subject=? AND code!='operator_stopped'", (job["id"],))
        tx.execute("DELETE FROM work_queue WHERE subject=?", (job["id"],))
        self._release_integration(tx, job["id"])
        self._finish_final_failure(tx, run)
        tx.execute("UPDATE runs SET parent_revision=?,blocked=NULL WHERE blocked='final_refresh'", (job["refresh_revision"],))
        self._event(tx, job["id"], "final_requirements_invalidated", str(generation))
        return Step("progress", job["id"], "fresh_whole_run_review")

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
                      json.loads(tx.get(row["payload_id"], row["subject"])), execution,
                      row["attempts"] + (1 if row["status"] == "prepared" else 0))

    def _mechanical(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, execution: Execution, world: World) -> Step:
        subject, phase = job["id"], job["phase"]
        if phase == "workspace":
            return self._prepare_effect(tx, run, job, execution, world, "workspace", before=job["source_sha"],
                                        payload={"checkout": job["checkout"], "create_ref": job["kind"] != DeliveryKind.FINAL_RUN})
        integrated_phases = {"ready_publication", "publication_commit", "push", "pr", "checks", "approval", "merge"}
        changed_source = world.source is not None and world.source.sha != job["source_sha"]
        if phase in integrated_phases | {"integration"} and not self._observe_integration_snapshot(tx, job, world):
            self._finish_final_failure(tx, run)
            return Step("paused", subject, "integration_snapshot")
        if (phase in integrated_phases and job["integration_target"]
                and (job["integration_target"] != world.target.sha or changed_source)):
            self._update_boundary(tx, run, job)
            return Step("progress", subject, "updating")
        if phase == "candidate":
            development = self._last_attempt(tx, subject, Role.DEVELOPMENT)
            context_id = tx.one("SELECT context_id FROM attempts WHERE id=?", (development,))[0]
            context = json.loads(tx.get(context_id, subject))
            parents = [job["workspace_sha"]]
            failure = context.get("integration_evidence")
            raw = failure.get("raw") if isinstance(failure, dict) else None
            if isinstance(raw, dict) and raw.get("merge_head"):
                if raw.get("head") != job["workspace_sha"] or not isinstance(raw["merge_head"], str):
                    raise KernelError("candidate_parent_unproven")
                if raw["merge_head"] not in parents:
                    parents.append(raw["merge_head"])
            return self._prepare_effect(tx, run, job, execution, world, "candidate", before=job["workspace_sha"],
                                        payload={"checkout": job["checkout"], "summary_attempt": development, "parents": parents})
        if phase == "integration":
            job = self._job(tx, subject)  # An admitted external source append is the next CAS parent.
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
            if not self._pr_write_current(run, job, world):
                self._gate(tx, subject, "published_head_changed")
                return Step("paused", subject, "pr_identity_changed")
            attempt = tx.one("SELECT result_id FROM attempts WHERE id=?", (job["publication_attempt"],))
            narrative = json.loads(tx.get(attempt["result_id"], subject))
            old = None if world.pr is None else {"title": world.pr.title, "body": world.pr.body}
            return self._prepare_effect(tx, run, job, execution, world, "pr_write", before=job["publication_sha"],
                                        after=job["publication_sha"], payload={"title": narrative["pr_title"],
                                        "body": self._narrative_body(tx, run, job, narrative["pr_body_markdown"]), "prior": old})
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
                                        "commit_message": narrative["commit_message"] if job["kind"] == DeliveryKind.TICKET else f"merge: Delivery Run {job['source_ref'].removeprefix('refs/heads/')}",
                                        "parents": [world.target.sha] + ([] if job["kind"] == DeliveryKind.TICKET else [job["publication_sha"]])})
        if phase == "completion_comment":
            return self._prepare_effect(tx, run, job, execution, world, "completion_comment", before=job["merge_sha"],
                                        payload={"issue": job["issue"], "merge_sha": job["merge_sha"], "gate_mode": job["gate_mode"]})
        if phase == "close":
            comment = tx.one("SELECT * FROM effects WHERE subject=? AND generation=? AND operation='completion_comment' AND status='confirmed' AND before_sha=?",
                             (subject, job["generation"], job["merge_sha"]))
            intent = self._comment_close_intent(run, job, comment, json.loads(tx.get(comment["payload_id"], subject)),
                                                json.loads(tx.get(comment["receipt_id"], subject)))
            return self._prepare_effect(tx, run, job, execution, world, "close", before=job["merge_sha"],
                                        payload={"issue": job["issue"], "merge_sha": job["merge_sha"], "close_intent": intent})
        raise KernelError("prototype_step_unimplemented", phase)

    def _narrative_body(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, narrative: str) -> str:
        header = f"Parent Issue: #{run['parent_issue']}\n"
        if job["kind"] == DeliveryKind.TICKET:
            header += f"Primary Ticket: #{job['issue']}\nDelivery Type: Ticket\n\n"
        else:
            header += "Delivery Type: " + ("Parent-only" if job["kind"] == DeliveryKind.PARENT_ONLY else "Final Run") + "\n\n"
            if job["kind"] == DeliveryKind.FINAL_RUN:
                tickets = [(row["issue"], row["title"]) for row in tx.all("SELECT s.issue,v.title FROM subjects s JOIN requirement_versions v ON v.issue=s.issue AND v.revision=s.revision WHERE s.kind='ticket' ORDER BY s.issue")]
                seed = tx.rows("SELECT id FROM material WHERE subject=? AND purpose='fixture_evidence'", (job["id"],))
                if not tickets and seed:
                    historical = json.loads(tx.get(seed[0][0], job["id"])).get("ticket")
                    if historical:
                        tickets = [(historical["number"], historical["title"])]
                if tickets:
                    header += "## Completed Tickets\n\n" + "\n".join(f"- #{number}: {title}" for number, title in tickets) + "\n\n"
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
        actor = self._publisher_actor(tx, run, world) if operation in self.REMOTE else None
        body = dict(payload or {})
        body["intent_token"] = effect_id
        body["integrated_sha"] = job["integrated_sha"]
        body["integrated_tree"] = job["integrated_tree"]
        body["kind"] = job["kind"]
        if operation in {"workspace", "validation_checkout", "validation_cleanup", "candidate", "integrate", "publication_commit"}:
            for key in ("checkout", "owned_root", "common_git_dir"):
                body.setdefault(key, job[key])
        if operation in {"push", "run_branch"}:
            body["expected_remote_sha"] = None if world.source is None else world.source.sha
        inputs = self._inputs(tx, run)
        if inputs:
            body["github_identity"] = inputs["runner_identity"]
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
                    job["pr_number"], actor, payload_id))
        self._event(tx, subject, "effect_prepared", effect_id, payload_id)
        row = tx.one("SELECT * FROM effects WHERE id=?", (effect_id,))
        return Step("effect", subject, effect=self._effect(tx, row, execution))

    def _publisher_actor(self, tx: Transaction, run: sqlite3.Row, world: World) -> str:
        selected = self._inputs(tx, run).get("runner_identity")
        actor = world.publisher_actor
        if (selected is None or not isinstance(actor, str) or not actor.strip() or actor != actor.strip()
                or world.publisher_identity != selected):
            raise KernelError("publisher_identity_unproven")
        placeholders = ",".join("?" for _ in self.REMOTE)
        previous = tx.rows(f"SELECT actor FROM effects WHERE operation IN ({placeholders}) AND actor IS NOT NULL AND actor!=? LIMIT 1",
                           (*self.REMOTE, actor))
        if previous:
            raise KernelError("publisher_identity_changed")
        return actor

    def _guard_publisher(self, tx: Transaction, run: sqlite3.Row, effect: sqlite3.Row, world: World) -> None:
        if effect["operation"] in self.REMOTE and self._publisher_actor(tx, run, world) != effect["actor"]:
            raise KernelError("publisher_identity_changed")

    def attach_publication_commit(self, effect: Effect, witness: Mapping[str, Any],
                                  execution: Execution, world: World) -> Effect:
        """Persist the original Git object bytes before the first physical write."""
        with self.storage.transaction() as tx:
            run, job = self._owned(tx, effect.subject, execution)
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
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
                    or row["base_sha"] != world.target.sha):
                raise KernelError("operation_cancelled")
            if (set(witness) != {"tree_sha", "parents", "message", "raw_commit", "expected_sha"}
                    or witness["tree_sha"] != payload["integrated_tree"] or witness["parents"] != payload["parents"]
                    or witness["message"] != payload["commit_message"]
                    or not isinstance(witness["raw_commit"], str) or not witness["raw_commit"]
                    or not isinstance(witness["expected_sha"], str) or not witness["expected_sha"]):
                raise KernelError("publication_commit_intent_changed")
            payload["publication_commit_intent"] = dict(witness)
            material = tx.put(effect.subject, "effect_intent", wire_json(payload))
            tx.execute("UPDATE effects SET payload_id=? WHERE id=?", (material, effect.effect_id))
            self._event(tx, effect.subject, "publication_commit_prepared", effect.effect_id, material)
            return self._effect(tx, tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,)), execution)

    def dispatch(self, effect: Effect, execution: Execution, world: World) -> bool:
        """Recheck authority immediately before the single external operation."""
        with self.storage.transaction() as tx:
            run, job = self._owned(tx, effect.subject, execution)
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            if row["status"] != "prepared":
                return False
            if self._effect(tx, row, execution) != effect:
                raise KernelError("effect_binding")
            if (effect.operation == "publication_commit" and not effect.payload.get("reuse_sha")
                    and not effect.payload.get("publication_commit_intent")):
                raise KernelError("publication_commit_intent_required")
            self._guard_publisher(tx, run, row, world)
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
            if effect.operation == "pr_write" and not self._pr_write_current(run, job, world):
                self._gate(tx, effect.subject, "published_head_changed")
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

    def reconcile(self, effect: Effect, result: EffectResult, execution: Execution, world: World | None = None) -> Step:
        """Record target/prior/third/unknown without treating absence as read failure.

        Exact target settlement does not need live mutation authority. A prior
        observation may only rearm the original intent under current authority.
        """
        with self.storage.transaction() as tx:
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            if row["status"] in {"confirmed", "observed"}:
                return Step("progress", effect.subject)
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
                    return Step("observe", effect.subject)
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
                if world is None:
                    return Step("observe", effect.subject)
                if self._effect_stale(tx, run, job, row):
                    tx.execute("UPDATE effects SET status='retired' WHERE id=?", (row["id"],))
                    if job["kind"] == DeliveryKind.FINAL_RUN and run["observed_graph_revision"] == run["graph_revision"]:
                        tx.execute("UPDATE subjects SET refresh_revision=?,phase='refresh_final',grant_id=NULL WHERE id=?", (run["observed_parent_revision"], job["id"]))
                        tx.execute("UPDATE runs SET blocked='final_refresh' WHERE blocked IS NULL OR blocked='final_refresh'")
                    self._event(tx, effect.subject, "stale_intent_not_applied", effect.effect_id)
                    return Step("progress", effect.subject)
                if run["stopped"] or run["blocked"] or self._gates(tx, effect.subject) or not self._current(tx, job, run, world):
                    return Step("paused", effect.subject, "original_write_not_authorized")
                if (row["operation"] in {"run_branch", "publication_commit", "push", "pr_write", "merge"}
                        and row["base_sha"] != world.target.sha):
                    tx.execute("UPDATE effects SET status='retired' WHERE id=?", (effect.effect_id,))
                    self._update_boundary(tx, run, job)
                    return Step("progress", effect.subject, "updating")
                if row["attempts"] - row["retry_baseline"] >= 3:
                    self._gate(tx, effect.subject, "publication_retry_exhausted")
                    return Step("paused", effect.subject, "publication_retry_exhausted")
                tx.execute("UPDATE effects SET status='prepared' WHERE id=?", (effect.effect_id,))
                return Step("progress", effect.subject)
            evidence = tx.put(effect.subject, "effect_observation", wire_json(asdict(result)))
            self._gate(tx, effect.subject, "write_conflict" if result.disposition == "third" else "write_result_unknown", evidence=evidence)
            self._finish_final_failure(tx, run)
            return Step("paused", effect.subject, result.disposition)

    def observe_effect_failure(self, effect: Effect, diagnostic: Mapping[str, Any]) -> None:
        """A call failure is an audit fact, not proof of a remote write's outcome."""
        with self.storage.transaction() as tx:
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            actual = self._effect(tx, row, effect.execution)
            if (replace(effect, dispatch_serial=actual.dispatch_serial) != actual
                    or not 1 <= effect.dispatch_serial <= row["attempts"]):
                raise KernelError("effect_binding")
            material = tx.put(effect.subject, "effect_failure", wire_json(dict(diagnostic)))
            old = tx.rows("SELECT evidence FROM effect_failures WHERE effect=? AND dispatch_serial=?",
                          (effect.effect_id, effect.dispatch_serial))
            if old:
                if old[0][0] != material:
                    raise KernelError("conflicting_effect_failure")
                return
            tx.execute("INSERT INTO effect_failures VALUES (?,?,?,?)", (effect.effect_id, effect.dispatch_serial, material, self.clock()))
            self._event(tx, effect.subject, "effect_failed", effect.effect_id, material, generation=row["generation"])

    def _apply_effect(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, row: sqlite3.Row, world: World) -> Step:
        subject, operation = job["id"], row["operation"]
        result = json.loads(tx.get(row["receipt_id"], subject))
        self._validate_target(tx, run, job, row, result)
        commit = result["commit"]
        if result["disposition"] == "failure":
            tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (row["id"],))
            purpose = Purpose.INTEGRATION.value if operation == "integrate" else job["purpose"]
            tx.execute("UPDATE subjects SET phase='development',purpose=?,repair_source=?,resume_purpose=?,last_failure=?,integrated_sha=NULL,integrated_tree=NULL,workspace_sha=?,version=version+1 WHERE id=?",
                       (purpose, "integration" if operation == "integrate" else "git_integrity", job["purpose"], row["receipt_id"],
                        job["workspace_sha"] if commit is None else commit["sha"], subject))
            # Keep the exact CI trigger for a later CI-waiver Publication. The
            # current integration repair receives only its actual Git failure;
            # _context selects that responsibility from purpose/repair_source.
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
            if job["purpose"] == Purpose.INTEGRATION:
                phase = "integration"
            elif job["purpose"] == Purpose.CI and not budget["review_required"]:
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
            payload = json.loads(tx.get(row["payload_id"], subject))
            live_source = None if world.source is None else world.source.sha
            if row["base_sha"] != world.target.sha or payload["source_sha"] != live_source:
                self._observe_integration_snapshot(tx, job, world)
                tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (row["id"],))
                tx.execute("UPDATE subjects SET phase='integration',purpose='integration',repair_source='integration',version=version+1 WHERE id=?", (subject,))
                return Step("progress", subject, "integration_snapshot_changed")
            tx.execute("UPDATE integration_snapshots SET open=0 WHERE subject=? AND window=(SELECT max(window) FROM integration_snapshots WHERE subject=?)", (subject, subject))
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
            self._record_source_write(tx, subject, row["source_ref"], row["after_sha"])
            phase = "publish"
        elif operation == "publication_commit":
            if commit["tree"] != job["integrated_tree"]:
                raise KernelError("publication_tree_changed")
            tx.execute("UPDATE subjects SET publication_sha=?,publication_tree=?,workspace_sha=? WHERE id=?",
                       (commit["sha"], commit["tree"], commit["sha"], subject))
            phase = "push"
        elif operation == "push":
            tx.execute("UPDATE subjects SET source_sha=? WHERE id=?", (row["after_sha"], subject))
            self._record_source_write(tx, subject, row["source_ref"], row["after_sha"])
            phase = "pr"
        elif operation == "pr_write":
            pr = result["pr"]
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
                payload = json.loads(tx.get(row["payload_id"], subject))
                comment = tx.one("SELECT id FROM effects WHERE id=? AND subject=? AND generation=? AND operation='completion_comment' AND status='confirmed'",
                                 (payload["close_intent"]["effect_id"], subject, job["generation"]))
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
        tx.execute("UPDATE subjects SET phase='integration',purpose='integration',repair_source='integration',integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL,version=version+1 WHERE id=?", (job["id"],))
        if run["final_action"]:
            tx.execute("UPDATE actions SET status='updating' WHERE id=?", (run["final_action"],))
            tx.execute("UPDATE runs SET final_action=NULL WHERE id=?", (run["id"],))
        self._event(tx, job["id"], "boundary_update", job["candidate_sha"] or job["id"])

    def _resync_state(self, tx: Transaction, subject: str) -> dict[str, Any]:
        allowance = tx.one("SELECT * FROM resync_allowances WHERE subject=? AND current=1", (subject,))
        snapshots = tx.rows("SELECT * FROM integration_snapshots WHERE subject=? ORDER BY window DESC LIMIT 1", (subject,))
        snapshot = snapshots[0] if snapshots else None
        return {"generation": allowance["generation"], "used": allowance["used"], "limit": allowance["allowance"],
                "window": None if snapshot is None else snapshot["window"], "open": bool(snapshot and snapshot["open"]),
                "source_sha": None if snapshot is None else snapshot["source_sha"],
                "target_sha": None if snapshot is None else snapshot["target_sha"]}

    def _record_source_write(self, tx: Transaction, subject: str, ref: str, head: str) -> None:
        tx.execute("UPDATE integration_snapshots SET source_sha=? WHERE subject=? AND source_ref=? AND window=(SELECT max(window) FROM integration_snapshots WHERE subject=?)",
                   (head, subject, ref, subject))

    def _renew_resync(self, tx: Transaction, job: sqlite3.Row, *, require_exhaustion: bool = True) -> None:
        previous = self._resync_state(tx, job["id"])
        if require_exhaustion and (previous["used"] < previous["limit"] or previous["open"]):
            raise KernelError("resync_allowance_not_exhausted")
        tx.execute("UPDATE resync_allowances SET current=0 WHERE subject=? AND current=1", (job["id"],))
        tx.execute("INSERT INTO resync_allowances(subject,generation,allowance) VALUES (?,?,?)",
                   (job["id"], previous["generation"] + 1, self._policy(tx.one("SELECT * FROM runs")).resync_limit))
        self._event(tx, job["id"], "resync_renewed", str(previous["generation"] + 1))

    def _observe_integration_snapshot(self, tx: Transaction, job: sqlite3.Row, world: World) -> bool:
        """Only an external change after a completed window spends allowance."""
        subject = job["id"]
        if world.source is None and job["pr_number"] is not None:
            self._gate(tx, subject, "published_head_changed")
            return False
        source_sha = job["source_sha"] if world.source is None else world.source.sha
        previous = tx.rows("SELECT * FROM integration_snapshots WHERE subject=? ORDER BY window DESC LIMIT 1", (subject,))
        allowance = tx.one("SELECT * FROM resync_allowances WHERE subject=? AND current=1", (subject,))
        snapshot = previous[0] if previous else None
        first = snapshot is None or snapshot["source_ref"] != job["source_ref"] or snapshot["target_ref"] != job["target_ref"]
        source_changed = snapshot is not None and not first and snapshot["source_sha"] != source_sha
        target_changed = snapshot is not None and not first and snapshot["target_sha"] != world.target.sha
        if snapshot is not None and not first:
            for changed, before, after in ((source_changed, snapshot["source_sha"], source_sha),
                                           (target_changed, snapshot["target_sha"], world.target.sha)):
                if changed and (before, after) not in world.ancestry:
                    self._gate(tx, subject, "non_append_integration_history")
                    return False
        charged = bool(snapshot is not None and not first and (source_changed or target_changed) and not snapshot["open"])
        if charged and allowance["used"] >= allowance["allowance"]:
            self._gate(tx, subject, "integration_resync_exhausted")
            return False
        if first or charged:
            window = 1 if snapshot is None else snapshot["window"] + 1
            tx.execute("INSERT INTO integration_snapshots VALUES (?,?,?,?,?,?,?,1)",
                       (subject, window, allowance["generation"], job["source_ref"], job["target_ref"], source_sha, world.target.sha))
            if charged:
                tx.execute("UPDATE resync_allowances SET used=used+1 WHERE subject=? AND current=1", (subject,))
        elif snapshot is not None and (source_changed or target_changed):
            tx.execute("UPDATE integration_snapshots SET source_sha=?,target_sha=? WHERE subject=? AND window=?",
                       (source_sha, world.target.sha, subject, snapshot["window"]))
        if world.source is not None and source_sha != job["source_sha"]:
            tx.execute("UPDATE subjects SET source_sha=? WHERE id=?", (source_sha, subject))
        if first or source_changed or target_changed:
            evidence = tx.put(subject, "integration_snapshot", wire_json({"source_sha": source_sha, "target_sha": world.target.sha, "charged": charged}))
            self._event(tx, subject, "integration_snapshot", str(allowance["generation"]), evidence)
        return True

    def _review_current(self, tx: Transaction, job: sqlite3.Row, world: World) -> bool:
        if not job["review_attempt"]:
            return False
        review = tx.one("SELECT * FROM attempts WHERE id=?", (job["review_attempt"],))
        return bool(review["outcome"] == "pass" and review["revision"] == job["revision"]
                and review["candidate_sha"] == job["candidate_sha"] and review["candidate_tree"] == job["candidate_tree"]
                and review["base_sha"] == world.target.sha and review["expected_tree"] == job["expected_tree"]
                and review["completion_revision"] == job["completion_revision"])

    @staticmethod
    def _pr_identity(run: sqlite3.Row, resource: sqlite3.Row, pr: PullRequest | Mapping[str, Any]) -> bool:
        value = asdict(pr) if isinstance(pr, PullRequest) else pr
        return bool(value["repository"] == value.get("head_repository") == run["repository"]
                    and value["source_ref"] == resource["source_ref"] and value["target_ref"] == resource["target_ref"]
                    and (resource["pr_number"] is None or value["number"] == resource["pr_number"]))

    def _pr_write_current(self, run: sqlite3.Row, job: sqlite3.Row, world: World) -> bool:
        pr = world.pr
        if pr is None:
            return job["pr_number"] is None
        return bool(self._pr_identity(run, job, pr) and pr.state == "OPEN"
                    and pr.head == world.source and pr.head.sha == job["publication_sha"] and pr.base == world.target)

    def _published_matches(self, run: sqlite3.Row, job: sqlite3.Row, world: World) -> bool:
        pr = world.pr
        return (pr is not None and pr.number == job["pr_number"] and self._pr_identity(run, job, pr)
                and world.source == pr.head and pr.head.sha == job["publication_sha"] and pr.head.tree == job["publication_tree"]
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

    def _event(self, tx: Transaction, subject: str, kind: str, event_id: str, material: str | None = None, *, generation: int | None = None) -> None:
        job = tx.one("SELECT generation,phase,purpose,current_attempt FROM subjects WHERE id=?", (subject,))
        if generation is None:
            generation = job["generation"]
        status = None
        if generation == job["generation"]:
            run = tx.one("SELECT stopped,blocked FROM runs")
            status = wire_json({"stopped": bool(run["stopped"]), "blocked": run["blocked"],
                                "gates": [row["code"] for row in self._gates(tx, subject)],
                                "attempt_status": None if job["current_attempt"] is None else
                                tx.one("SELECT status FROM attempts WHERE id=?", (job["current_attempt"],))[0]}).decode()
        tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at,phase,purpose,status) VALUES (?,?,?,?,?,?,?,?,?)",
                   (subject, kind, event_id, material, generation, self.clock(),
                    job["phase"] if status is not None else None, job["purpose"] if status is not None else None, status))
