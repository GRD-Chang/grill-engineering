"""One authoritative transaction interpreter for delivery work.

All transition, currentness, budget, FIFO and mutation-authority choices are
made here from current SQL facts. The external driver only executes the one
returned request. A raw return and its business consumption commit separately.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime
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
    Delivery, Policy, Profile, PullRequest, Purpose, Requirements, ResourceInspection, ResourceWitness, Role, RunSpec, Step, World, artifact, review_outcome, wire_json,
)
from .storage import CURRENT_LIMIT, Storage, Transaction
from .configuration import checkpoint_policy
from .task_location import bind_task, read_task
from .legacy_admission import locked_admission, origin_reference, read_source_fence, source_fence


def identity() -> str:
    return uuid4().hex


class Kernel:
    CLEANUP = ("cleanup_worktree", "cleanup_local_ref", "cleanup_remote_ref")
    ABANDON_EFFECTS = ("inspect_resources", "retire_pr", "reopen_ticket", "recovery_comment", "abandon_worktree")
    REMOTE = ("push", "run_branch", "pr_write", "merge", "completion_comment", "close", "issue_link", "cleanup_remote_ref", "retire_pr", "reopen_ticket", "recovery_comment")
    def __init__(self, path: Path) -> None:
        self.storage = Storage(path)
        self.clock: Callable[[], float] = time.time

    @classmethod
    def create_task(cls, path: Path, spec: RunSpec, *, requirements: Requirements) -> Kernel:
        """Create or reuse one Task after external creation reads have finished."""
        if spec.repository_root is None:
            raise KernelError("task_workspace_unknown")
        task = TaskKey(Path(spec.repository_root), spec.repository, spec.parent_issue)
        with locked_admission(task) as location:
            prior = read_task(location)
            origin: dict[str, str] | None = None
            if prior is not None:
                existing = cls(prior[1])
                if not existing._replaceable(task, prior[0]):
                    return existing
                origin = origin_reference(prior[1], task, prior[0])
            kernel = cls.create(path, spec, requirements=requirements)
            kernel._carry_source_origin(task, spec.run_id, origin)
            bind_task(location, spec.run_id, kernel.storage.path)
            return kernel

    @classmethod
    def create(cls, path: Path, spec: RunSpec, *, requirements: Requirements | None = None,
               fixture_evidence: Mapping[str, Any] | None = None,
               source_origin: Mapping[str, str] | None = None) -> Kernel:
        if not spec.deliveries or len({delivery.key for delivery in spec.deliveries}) != len(spec.deliveries):
            raise ValueError("one or more uniquely identified deliveries are required")
        origin = None if source_origin is None else read_source_fence(source_fence(source_origin))
        if origin is not None and (spec.repository_root is None or origin["run_id"] != spec.run_id
                or origin["task_fingerprint"] != TaskKey(Path(spec.repository_root), spec.repository, spec.parent_issue).fingerprint):
            raise KernelError("source_origin_identity")
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
        policy = cls._policy_values(spec.policy)
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
                if tickets:
                    final = next(delivery for delivery in spec.deliveries if delivery.kind == DeliveryKind.FINAL_RUN)
                    if any(delivery.target_ref != final.source_ref or delivery.base != final.base
                           for delivery in spec.deliveries if delivery.kind == DeliveryKind.TICKET):
                        raise KernelError("run_branch_identity")
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
                if requirements is not None and delivery.owned_root:
                    tx.execute("UPDATE subjects SET phase='workspace' WHERE id=?", (delivery.key,))
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
            if origin is not None:
                # This is the converter's final publication intent, even when
                # creation uses a temporary inode. It grants no execution.
                material = tx.put(owner, "v6_original_cutover", wire_json(origin))
                kernel._event(tx, owner, "v6_original_cutover", origin["nonce"], material)
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
        abandoned = self._abandon_action(tx)
        return {"frontier": frontier if abandoned is None else [], "remaining": remaining, "blocked": run["blocked"], "stopped": bool(run["stopped"]),
                "abandonment": None if abandoned is None else {"action": abandoned["id"], "status": abandoned["status"]},
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

    def generation_resources(self) -> tuple[ResourceWitness, ...]:
        """Read original current and retired worktree identities for inspection."""
        with self.storage.transaction(write=False) as tx:
            return self._generation_resources(tx)

    def _generation_resources(self, tx: Transaction) -> tuple[ResourceWitness, ...]:
        resources: list[ResourceWitness] = []
        for job in tx.all("SELECT * FROM subjects ORDER BY id"):
            generations = [*tx.all("SELECT * FROM retired_generations WHERE subject=? ORDER BY generation", (job["id"],)), job]
            for generation in generations:
                subject = job["id"]
                if generation["checkout"]:
                    resources.append(ResourceWitness(subject, generation["generation"], "development", generation["checkout"],
                                                     generation["owned_root"], generation["common_git_dir"], generation["source_ref"],
                                                     generation["workspace_sha"]))
                for checkout in sorted({generation["validation_checkout"], generation["validation_cleanup"]} - {None}):
                    witnesses = [json.loads(tx.get(row[0], subject)) for row in tx.all(
                        "SELECT payload_id FROM effects WHERE subject=? AND generation=? AND operation='validation_checkout' AND status IN ('confirmed','observed')",
                        (subject, generation["generation"]))]
                    matches = [value for value in witnesses if value["checkout"] == checkout]
                    if len(matches) != 1:
                        raise KernelError("validation_resource_unproven")
                    saved = matches[0]
                    resources.append(ResourceWitness(subject, generation["generation"], "validation", checkout,
                                                     saved["owned_root"], saved["common_git_dir"], generation["source_ref"], saved["reviewed_commit"]))
        for row in tx.all("SELECT * FROM effects WHERE operation='validation_checkout' AND status IN ('prepared','sent','observed') ORDER BY rowid"):
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
        rows = tx.rows("SELECT * FROM actions WHERE kind='abandon'", limit=1)
        return None if not rows else rows[0]

    def _abandon_scope(self, tx: Transaction, action: sqlite3.Row) -> dict[str, Any]:
        owner = action["subject"] or tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
        return dict(json.loads(tx.get(action["receipt_id"], owner))["abandonment"])

    def _abandon_discard(self, tx: Transaction, action: sqlite3.Row) -> bool:
        if self._abandon_scope(tx, action)["discard_worktree"]:
            return True
        for row in tx.all("SELECT subject,receipt_id FROM actions WHERE kind='abandon_continue' AND status='applied'"):
            receipt = json.loads(tx.get(row["receipt_id"], row["subject"]))
            if receipt.get("continuation_of") == action["id"] and receipt.get("discard_worktree") is True:
                return True
        return False

    def _abandon_inspection(self, tx: Transaction, action: sqlite3.Row) -> sqlite3.Row | None:
        rows = tx.rows("SELECT e.* FROM effects e JOIN history h ON h.identity=e.id AND h.kind='effect_prepared' "
                       "WHERE e.control_action=? AND e.operation='inspect_resources' AND h.position>"
                       "COALESCE((SELECT MAX(position) FROM history WHERE kind='abandon_continued' AND identity=?),0) "
                       "ORDER BY h.position DESC LIMIT 1", (action["id"], action["id"]))
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
            row = tx.one("SELECT * FROM effects WHERE id=?", (effect_id,))
            if row["status"] != "confirmed" or row["receipt_id"] is None or row["operation"] not in {"candidate", "integrate", "publication_commit", "validation_checkout"}:
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
        scope = self._abandon_scope(tx, action)
        run = tx.one("SELECT * FROM runs")
        hosts = tx.rows("SELECT * FROM hosts WHERE run_id=? AND owner=? AND epoch=?", (execution.run_id, execution.owner, execution.epoch))
        original = bool(scope["execution"]["owner"] and scope["execution"] == asdict(execution)
                        and run["executor"] == execution.owner and run["epoch"] == execution.epoch + 1)
        admitted = False
        if (hosts and action["run_id"] == execution.run_id and run["executor"] == execution.owner
                and run["epoch"] == execution.epoch):
            request = json.loads(tx.get(hosts[0]["request_id"], hosts[0]["subject"]))
            admitted = (hosts[0]["action_id"] == action["id"] or request["intent"].get("continuation_of") == action["id"])
        if (not (original or admitted) or (not hosts and tx.rows("SELECT owner FROM hosts LIMIT 1"))
                or (hosts and (hosts[0]["status"] != "running" or not hosts[0]["usage_ready"]))):
            raise KernelError("executor_fenced")

    def _abandon_effects(self, tx: Transaction, action: sqlite3.Row) -> list[sqlite3.Row]:
        return tx.all("SELECT * FROM effects WHERE control_action=? ORDER BY rowid", (action["id"],))

    def _abandon_prs(self, tx: Transaction, action: sqlite3.Row) -> list[dict[str, Any]]:
        scope = self._abandon_scope(tx, action)
        generations = {tuple(value) for value in scope["generations"]}
        generations.update((row["subject"], row["generation"]) for row in tx.all("SELECT subject,generation FROM retired_generations"))
        latest: dict[tuple[str, int], dict[str, Any]] = {}
        for row in tx.all("SELECT * FROM effects WHERE operation='pr_write' AND status='confirmed' AND receipt_id IS NOT NULL ORDER BY rowid"):
            key = (row["subject"], row["generation"])
            if key not in generations:
                continue
            result = json.loads(tx.get(row["receipt_id"], row["subject"]))
            if result["disposition"] == "target" and result["pr"] is not None:
                latest[key] = {"subject": row["subject"], "generation": row["generation"], "prior_pr": result["pr"]}
        settled = {(row["subject"], row["generation"], row["pr_number"]) for row in tx.all(
            "SELECT subject,generation,pr_number FROM effects WHERE operation IN ('merge','retire_pr') AND status='confirmed'")}
        return [value for key, value in latest.items() if (*key, value["prior_pr"]["number"]) not in settled]

    def _abandon_control_scope(self, tx: Transaction, action: sqlite3.Row, row: sqlite3.Row) -> bool:
        if action["status"] != "pending" or row["control_action"] != action["id"] or row["operation"] not in self.ABANDON_EFFECTS:
            return False
        payload = json.loads(tx.get(row["payload_id"], row["subject"]))
        if payload.get("control_action") != action["id"]:
            return False
        discard = self._abandon_discard(tx, action)
        if row["operation"] == "inspect_resources":
            return bool(payload.get("resources") == [asdict(item) for item in self._abandon_resources(tx, action)]
                        and type(payload.get("discard_worktree")) is bool and (not payload["discard_worktree"] or discard))
        if row["operation"] == "abandon_worktree":
            resource = payload.get("resource")
            return bool(resource in [asdict(item) for item in self._abandon_resources(tx, action)]
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
        records = tx.rows("SELECT * FROM completions WHERE subject=? AND generation=? AND close_effect=?",
                          (row["subject"], row["generation"], completion.get("close_effect")))
        if not records or dict(records[0]) != completion:
            return False
        closed = tx.one("SELECT * FROM effects WHERE id=? AND subject=? AND generation=? AND operation='close' AND status='confirmed'",
                        (completion["close_effect"], row["subject"], row["generation"]))
        close_payload = json.loads(tx.get(closed["payload_id"], row["subject"]))
        if (payload.get("issue") != self._job(tx, row["subject"])["issue"]
                or payload.get("close_receipt") != json.loads(tx.get(closed["receipt_id"], row["subject"]))
                or payload.get("close_intent") != close_payload["close_intent"] or row["actor"] != closed["actor"]):
            return False
        if row["operation"] == "reopen_ticket":
            return True
        reopened = tx.rows("SELECT * FROM effects WHERE id=? AND subject=? AND generation=? AND control_action=? "
                           "AND operation='reopen_ticket' AND status='confirmed'",
                           (payload.get("reopen_effect"), row["subject"], row["generation"], action["id"]))
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
        if (tx.one("SELECT blocked FROM runs")[0] == "supervision_timeout"
                or any(gate["code"] == "supervision_timeout" for gate in self._gates(tx, row["subject"]))):
            return False
        if row["operation"] != "inspect_resources":
            inspection = self._abandon_inspection(tx, action)
            if inspection is None or inspection["status"] != "confirmed":
                return False
            self._check_abandon_inspection(tx, action, inspection)
        return True

    def _abandon_reconciliation_blocked(self, tx: Transaction, action: sqlite3.Row) -> bool:
        continued = tx.one("SELECT COALESCE(MAX(position),0) FROM history WHERE kind='abandon_continued' AND identity=?", (action["id"],))[0]
        effects = {row["id"]: row for row in self._abandon_effects(tx, action)}
        for effect_id in self._abandon_scope(tx, action)["unsettled"]:
            effects[effect_id] = tx.one("SELECT * FROM effects WHERE id=?", (effect_id,))
        for row in effects.values():
            if row["status"] != "sent":
                continue
            pending = tx.one("SELECT COALESCE(MAX(position),0) FROM history WHERE kind='abandon_reconciliation_pending' AND identity=?", (row["id"],))[0]
            dispatched = tx.one("SELECT COALESCE(MAX(position),0) FROM history WHERE kind='effect_dispatched' AND identity=?", (row["id"],))[0]
            if pending > max(continued, dispatched):
                return True
        return False

    def _effect_owned(self, tx: Transaction, effect: Effect, execution: Execution) -> tuple[sqlite3.Row, sqlite3.Row]:
        action = self._abandon_action(tx)
        if action is None:
            return self._owned(tx, effect.subject, execution)
        self._abandon_owned(tx, action, execution)
        row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
        if not self._abandon_control_authority(tx, action, row):
            raise KernelError("executor_fenced")
        return tx.one("SELECT * FROM runs"), self._job(tx, effect.subject)

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
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
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
                                generation: int, execution: Execution, world: World, operation: str, *, before: str,
                                payload: dict[str, Any]) -> Step:
        job = self._job(tx, subject)
        step = self._prepare_effect(tx, run, job, execution, world, operation, before=before,
                                    payload={**payload, "control_action": action["id"]})
        assert step.effect is not None
        if operation == "retire_pr":
            prior = payload["prior_pr"]
            tx.execute("UPDATE effects SET generation=?,source_ref=?,target_ref=?,pr_number=?,base_sha=?,base_tree=? WHERE id=?",
                       (generation, prior["source_ref"], prior["target_ref"], prior["number"], prior["base"]["sha"], prior["base"]["tree"], step.effect.effect_id))
        else:
            resource = payload.get("resource")
            tx.execute("UPDATE effects SET generation=?,source_ref=? WHERE id=?",
                       (generation, job["source_ref"] if resource is None else resource["source_ref"], step.effect.effect_id))
        return Step("effect", subject, effect=self._effect(tx, tx.one("SELECT * FROM effects WHERE id=?", (step.effect.effect_id,)), execution))

    def _advance_abandon(self, tx: Transaction, run: sqlite3.Row, action: sqlite3.Row, subject: str,
                         execution: Execution, world: World | None) -> Step:
        self._abandon_owned(tx, action, execution)
        if action["status"] == "complete":
            return Step("complete", subject, "abandoned")
        if tx.rows("SELECT id FROM leases LIMIT 1"):
            return Step("waiting", subject, "worker_exit_unproven")
        scope = self._abandon_scope(tx, action)
        for effect_id in scope["unsettled"]:
            row = tx.one("SELECT * FROM effects WHERE id=?", (effect_id,))
            if row["operation"] == "issue_link":
                # Optional display keeps its original indeterminate audit;
                # it cannot block the Action or acquire readback acknowledgements.
                continue
            if row["status"] == "sent":
                return Step("reconcile", row["subject"], effect=self._effect(tx, row, execution))
            if row["status"] == "observed":
                if world is None or subject != row["subject"]:
                    return Step("observe", row["subject"])
                job = self._job(tx, row["subject"])
                if row["operation"] == "close":
                    return self._apply_effect(tx, run, job, row, world)
                return self._settle_stale_effect(tx, job, row)
            if row["status"] == "prepared":
                tx.execute("UPDATE effects SET status='cancelled' WHERE id=?", (row["id"],))
                return Step("progress", row["subject"], "business_intent_cancelled")
        effects = self._abandon_effects(tx, action)
        for row in effects:
            if row["status"] == "sent":
                return Step("reconcile", row["subject"], effect=self._effect(tx, row, execution))
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
                tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (row["id"],))
                self._event(tx, row["subject"], "effect_consumed", row["id"], row["receipt_id"], generation=row["generation"])
                return Step("progress", row["subject"])
        def completed(operation: str, key: str, value: Any) -> bool:
            return any(row["operation"] == operation and row["status"] == "confirmed"
                       and json.loads(tx.get(row["payload_id"], row["subject"])).get(key) == value for row in effects)
        owner = tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
        inspection = self._abandon_inspection(tx, action)
        if inspection is None or inspection["status"] != "confirmed":
            if inspection is not None and inspection["status"] == "prepared":
                return Step("effect", inspection["subject"], effect=self._effect(tx, inspection, execution))
            if world is None or subject != owner:
                return Step("observe", owner)
            return self._prepare_abandon_effect(tx, run, action, owner, self._job(tx, owner)["generation"], execution, world,
                "inspect_resources", before=self._job(tx, owner)["workspace_sha"],
                payload={"resources": [asdict(item) for item in self._abandon_resources(tx, action)], "discard_worktree": self._abandon_discard(tx, action)})
        self._check_abandon_inspection(tx, action, inspection)
        for row in effects:
            if row["status"] == "retired" and self._abandon_control_scope(tx, action, row):
                prior = tx.rows("SELECT material_id FROM history WHERE kind='abandon_prior_confirmed' AND identity=? ORDER BY position DESC LIMIT 1", (row["id"],))
                if prior:
                    proof = json.loads(tx.get(prior[0][0], row["subject"]))
                    if proof["dispatch_serial"] == row["attempts"] and proof["result"]["disposition"] == "prior":
                        tx.execute("UPDATE effects SET status='prepared' WHERE id=?", (row["id"],))
                        refreshed = tx.one("SELECT * FROM effects WHERE id=?", (row["id"],))
                        return Step("effect", row["subject"], effect=self._effect(tx, refreshed, execution))
            if row["status"] == "prepared":
                return Step("effect", row["subject"], effect=self._effect(tx, row, execution))
        prs = self._abandon_prs(tx, action)
        for final in (False, True):
            for item in prs:
                if (self._job(tx, item["subject"])["kind"] == DeliveryKind.FINAL_RUN) != final:
                    continue
                if completed("retire_pr", "prior_pr", item["prior_pr"]):
                    continue
                if world is None or subject != item["subject"]:
                    return Step("observe", item["subject"])
                return self._prepare_abandon_effect(tx, run, action, item["subject"], item["generation"], execution, world,
                    "retire_pr", before=item["prior_pr"]["head"]["sha"], payload={"prior_pr": item["prior_pr"], "control_kind": "abandon"})
            if final:
                continue
            for completion in tx.all("SELECT * FROM completions ORDER BY subject,generation"):
                value = dict(completion)
                reopened = next((row for row in effects if row["operation"] == "reopen_ticket" and row["status"] == "confirmed"
                                 and json.loads(tx.get(row["payload_id"], row["subject"]))["completion"] == value), None)
                if reopened is not None:
                    receipt = json.loads(tx.get(reopened["receipt_id"], completion["subject"]))
                    if receipt["disposition"] != "target" or completed("recovery_comment", "completion", value):
                        continue
                    if world is None or subject != completion["subject"]:
                        return Step("observe", completion["subject"])
                    payload = json.loads(tx.get(reopened["payload_id"], completion["subject"]))
                    issue = payload["issue"]
                    body = (f"Delivery Run `{run['id']}` was abandoned before entering the default branch. "
                            f"Reopened Ticket #{issue}; its prior completion was recorded by PR #{completion['pr_number']} "
                            f"at Run Branch commit `{completion['merge_sha']}`.")
                    return self._prepare_abandon_effect(tx, run, action, completion["subject"], completion["generation"], execution, world,
                        "recovery_comment", before=completion["merge_sha"], payload={"issue": issue, "completion": value,
                         "close_receipt": payload["close_receipt"], "close_intent": payload["close_intent"],
                         "reopen_effect": reopened["id"], "reopen_receipt": receipt, "comment_body": body})
                if world is None or subject != completion["subject"]:
                    return Step("observe", completion["subject"])
                closed = tx.one("SELECT * FROM effects WHERE id=? AND operation='close' AND status='confirmed'", (completion["close_effect"],))
                payload = json.loads(tx.get(closed["payload_id"], completion["subject"]))
                return self._prepare_abandon_effect(tx, run, action, completion["subject"], completion["generation"], execution, world,
                    "reopen_ticket", before=completion["merge_sha"], payload={"issue": self._job(tx, completion["subject"])["issue"],
                     "completion": value, "close_receipt": json.loads(tx.get(closed["receipt_id"], completion["subject"])), "close_intent": payload["close_intent"]})
        for witness in self._abandon_resources(tx, action):
            resource = asdict(witness)
            if completed("abandon_worktree", "resource", resource):
                continue
            if world is None or subject != resource["subject"]:
                return Step("observe", resource["subject"])
            return self._prepare_abandon_effect(tx, run, action, resource["subject"], resource["generation"], execution, world,
                "abandon_worktree", before=resource["head_sha"], payload={"resource": resource, "discard_worktree": self._abandon_discard(tx, action)})
        tx.execute("UPDATE actions SET status='complete' WHERE id=?", (action["id"],))
        self._event(tx, owner, "abandoned", action["id"], action["receipt_id"])
        return Step("complete", subject, "abandoned")

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
            return self._control(tx, "requeue", action_id, subject=subject, world=world, replacement=replacement)

    def _validate_requeue(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                          replacement: Delivery, world: World) -> str:
        subject = job["id"]
        if job["kind"] == DeliveryKind.FINAL_RUN:
            raise KernelError("final_reacceptance_required")
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
        return str(revision)

    def _requeue_final(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World) -> None:
        """Fresh whole-Run Acceptance shares the existing invalidation transition."""
        if (run["stopped"] or run["blocked"] != "final_refresh" or job["refresh_revision"] is None
                or any(gate["code"] in {"operator_stopped", "execution_failed"} for gate in self._gates(tx, job["id"]))):
            raise KernelError("requeue_not_authorized")
        if (tx.rows("SELECT id FROM leases WHERE subject=?", (job["id"],))
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
        subject = job["id"]
        self._retire_generation(tx, job, "requirements_changed")
        tx.execute("UPDATE subjects SET generation=generation+1,revision=?,phase='workspace',purpose='requirements',source_ref=?,checkout=?,base_sha=?,base_tree=?,source_sha=?,workspace_sha=?,owned_root=?,common_git_dir=?,candidate_sha=NULL,candidate_tree=NULL,integrated_sha=NULL,integrated_tree=NULL,expected_tree=NULL,integration_target=NULL,current_attempt=NULL,review_attempt=NULL,publication_attempt=NULL,publication_sha=NULL,publication_tree=NULL,pr_number=NULL,checks_id=NULL,grant_id=NULL,merge_sha=NULL,gate_mode=NULL,development_thread=NULL,ci_failure=NULL,last_failure=NULL,resume_purpose=NULL,repair_source=NULL,validation_checkout=NULL,validation_cleanup=NULL,version=version+1 WHERE id=?",
                   (revision, replacement.source_ref, replacement.checkout, replacement.base.sha, replacement.base.tree, replacement.base.sha, replacement.base.sha, replacement.owned_root, replacement.common_git_dir, subject))
        tx.execute("DELETE FROM issue_links WHERE subject=?", (subject,))
        tx.execute("UPDATE runs SET parent_revision=?", (run["observed_parent_revision"],))
        for budget in tx.rows("SELECT * FROM budgets WHERE subject=? AND current=1", (subject,)):
            limits = self._budget_limits(self._subject_policy(tx, job), DeliveryKind(job["kind"]))[Purpose(budget["purpose"])]
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

    def _pending_requeue(self, tx: Transaction, subject: str) -> sqlite3.Row | None:
        rows = tx.rows("SELECT * FROM actions WHERE kind='requeue' AND subject=? AND status='pending'", (subject,), limit=1)
        return None if not rows else rows[0]

    def _requeue_scope(self, tx: Transaction, action: sqlite3.Row) -> dict[str, Any]:
        return dict(json.loads(tx.get(action["receipt_id"], action["subject"]))["requeue"])

    def _recorded_retirement(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, world: World) -> sqlite3.Row:
        rows = tx.rows("SELECT * FROM effects WHERE subject=? AND generation=? AND pr_number=? AND operation='retire_pr' AND status='confirmed' ORDER BY rowid DESC LIMIT 1",
                       (job["id"], job["generation"], job["pr_number"]))
        if not rows or world.pr is None or world.pr.state != "CLOSED" or world.target is None:
            raise KernelError("retirement_pr_changed")
        row = rows[0]
        result = json.loads(tx.get(row["receipt_id"], job["id"]))
        self._validate_target(tx, run, job, row, result)
        pr = result.get("pr")
        if (result["disposition"] != "target" or not isinstance(pr, dict) or not self._pr_identity(run, job, world.pr)
                or world.pr.number != pr["number"] or asdict(world.pr.head) != pr["head"] or world.pr.actor != pr["actor"]
                or world.pr.base != world.target or (world.pr.base.sha != pr["base"]["sha"]
                    and (pr["base"]["sha"], world.pr.base.sha) not in world.ancestry)):
            raise KernelError("retirement_pr_changed")
        return row

    def _recheck_retirement(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, action: sqlite3.Row,
                            row: sqlite3.Row, result: EffectResult) -> Step:
        original = json.loads(tx.get(row["receipt_id"], job["id"]))
        observed = asdict(result)
        if result.disposition == "target":
            self._validate_target(tx, run, job, row, observed)
            if (result.raw.get("event") != original["raw"].get("event")
                    or result.raw.get("close_intent") != original["raw"].get("close_intent")):
                result = replace(result, disposition="third")
                observed = asdict(result)
        material = tx.put(job["id"], "retirement_readback", wire_json({"effect": row["id"], "receipt": row["receipt_id"], "result": observed}))
        if result.disposition == "target":
            previous = tx.rows("SELECT material_id FROM history WHERE kind='retirement_rechecked' AND identity=? ORDER BY position DESC LIMIT 1", (action["id"],))
            if not previous or previous[0][0] != material:
                self._event(tx, job["id"], "retirement_rechecked", action["id"], material)
            return Step("progress", job["id"])
        if result.disposition == "unknown":
            self._gate(tx, job["id"], "write_result_unknown", evidence=material)
            self._event(tx, job["id"], "retirement_recheck_pending", action["id"], material)
            return Step("paused", job["id"], "write_result_unknown")
        tx.execute("UPDATE actions SET status='superseded' WHERE id=?", (action["id"],))
        self._gate(tx, job["id"], "requeue_retirement_changed", evidence=material)
        self._event(tx, job["id"], "requeue_superseded", action["id"], material)
        return Step("paused", job["id"], "requeue_retirement_changed")

    def _requeue_versions_current(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                                  action: sqlite3.Row, world: World | None) -> bool:
        scope = self._requeue_scope(tx, action)
        facts = tx.rows("SELECT revision FROM issue_facts WHERE issue=?", (job["issue"],))
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
                tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (pending["id"],))
                self._event(tx, job["id"], "effect_consumed", pending["id"], pending["receipt_id"])
            elif pending["status"] == "prepared":
                tx.execute("UPDATE effects SET status='cancelled' WHERE id=?", (pending["id"],))
        tx.execute("UPDATE actions SET status='superseded' WHERE id=?", (action["id"],))
        self._gate(tx, job["id"], reason)
        self._event(tx, job["id"], "requeue_superseded", action["id"], None if pending is None else pending["receipt_id"])
        return Step("paused", job["id"], reason)

    def _requeue_current(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, action: sqlite3.Row,
                         world: World, *, open_pr: bool = True) -> bool:
        scope = self._requeue_scope(tx, action)
        original, pr = scope["prior_pr"], world.pr
        return bool(not run["stopped"] and run["blocked"] == "requeue_required" and not run["final_action"]
                    and all(gate["code"] == "requeue_required" for gate in self._gates(tx, job["id"]))
                    and self._requeue_versions_current(tx, run, job, action, world)
                    and world.target is not None and (scope["target"] == asdict(world.target)
                        or (scope["target"]["sha"] != world.target.sha
                            and (scope["target"]["sha"], world.target.sha) in world.ancestry))
                    and original is not None and pr is not None and self._pr_identity(run, job, pr)
                    and pr.number == original["number"] and asdict(pr.head) == original["head"]
                    and pr.base == world.target and pr.actor == original["actor"]
                    and pr.state == ("OPEN" if open_pr else "CLOSED"))

    def _advance_requeue(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, action: sqlite3.Row,
                         execution: Execution, world: World | None) -> Step:
        subject = job["id"]
        scope = self._requeue_scope(tx, action)
        pending = self._pending_effect(tx, subject)
        if pending is not None and pending["status"] == "sent":
            return Step("reconcile", subject, effect=self._effect(tx, pending, execution))
        if not self._requeue_versions_current(tx, run, job, action, world):
            return self._end_requeue(tx, run, job, action, pending, "requeue_scope_changed")
        if run["stopped"] or any(gate["code"] != "requeue_required" for gate in self._gates(tx, subject)):
            return Step("paused", subject, self._pause_reason(tx, subject, run))
        readback = None
        if scope.get("retired_effect") is not None:
            original = tx.one("SELECT * FROM effects WHERE id=? AND subject=? AND generation=? AND status='confirmed'",
                              (scope["retired_effect"], subject, job["generation"]))
            previous = tx.rows("SELECT material_id FROM history WHERE kind='retirement_rechecked' AND identity=? ORDER BY position DESC LIMIT 1", (action["id"],))
            if not previous:
                return Step("reconcile", subject, effect=self._effect(tx, original, execution))
            readback = previous[0][0]
        if world is None:
            return Step("observe", subject)
        if not self._requeue_current(tx, run, job, action, world, open_pr=readback is None and (pending is None or pending["status"] != "observed")):
            return self._end_requeue(tx, run, job, action, pending, "requeue_retirement_changed")
        if pending is None and readback is None:
            scope = self._requeue_scope(tx, action)
            return self._prepare_effect(tx, run, job, execution, world, "retire_pr", before=scope["prior_pr"]["head"]["sha"],
                                        payload={"control_action": action["id"], "control_kind": "requeue", "prior_pr": scope["prior_pr"]})
        if readback is None:
            assert pending is not None
            if pending["operation"] != "retire_pr":
                raise KernelError("generation_unsettled")
            if pending["status"] == "prepared":
                return Step("effect", subject, effect=self._effect(tx, pending, execution))
            result = json.loads(tx.get(pending["receipt_id"], subject))
            self._validate_target(tx, run, job, pending, result)
            if result["disposition"] != "target":
                return Step("paused", subject, "retirement_unconfirmed")
            tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (pending["id"],))
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
        tx.execute("UPDATE actions SET status='applied' WHERE id=?", (action["id"],))
        return Step("progress", subject, "generation_requeued")

    def _charge_review(self, tx: Transaction, attempt: sqlite3.Row) -> None:
        if attempt["role"] == Role.REVIEW and not attempt["accounted"]:
            tx.execute("UPDATE budgets SET reviews_used=reviews_used+1 WHERE subject=? AND purpose=? AND window=?",
                       (attempt["subject"], attempt["purpose"], attempt["window"]))
            tx.execute("UPDATE attempts SET accounted=1 WHERE id=?", (attempt["id"],))

    def _retire_generation(self, tx: Transaction, job: sqlite3.Row, reason: str) -> None:
        tx.execute("INSERT INTO retired_generations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (job["id"], job["generation"], job["revision"], job["source_ref"], job["checkout"], job["source_sha"],
                    job["candidate_sha"], job["publication_sha"], job["pr_number"], reason, job["validation_checkout"], job["validation_cleanup"],
                    job["workspace_sha"], job["owned_root"], job["common_git_dir"]))
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

    def _carry_source_origin(self, task: TaskKey, run_id: str, origin: Mapping[str, str] | None) -> None:
        """Commit the fixed origin before publishing a legally replaced Task binding."""
        if origin is None:
            return
        if self.storage.path != Path(origin["origin_db"]).parent.parent / run_id / "kernel.sqlite3":
            raise KernelError("legacy_requires_offline_migration")
        present = origin_reference(self.storage.path, task, run_id)
        if present is not None:
            if present != origin:
                raise KernelError("legacy_requires_offline_migration")
            return
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            if (run["id"] != run_id or self._task(run) != task
                    or tx.rows("SELECT identity FROM history WHERE kind IN ('v6_original_cutover','v6_source_origin') LIMIT 1")):
                raise KernelError("legacy_requires_offline_migration")
            owner = tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
            material = tx.put(owner, "v6_source_origin", wire_json(dict(origin)))
            self._event(tx, owner, "v6_source_origin", origin["nonce"], material)

    def _replaceable(self, task: TaskKey, run_id: str) -> bool:
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            if run["id"] != run_id or self._task(run) != task:
                raise KernelError("task_location_identity")
            abandoned = self._abandon_action(tx)
            completed_abandonment = bool(abandoned is not None and abandoned["status"] == "complete")
            return bool(run["executor"] is None
                and (completed_abandonment or not tx.rows("SELECT id FROM subjects WHERE phase!='complete' LIMIT 1"))
                and (completed_abandonment or not tx.rows("SELECT id FROM final_target WHERE id NOT IN (SELECT id FROM subjects WHERE kind='final_run' AND phase='complete') LIMIT 1"))
                and not tx.rows("SELECT id FROM leases LIMIT 1")
                and not tx.rows("SELECT owner FROM retired_executors WHERE status!='exited' LIMIT 1")
                and not tx.rows("SELECT owner FROM hosts WHERE status!='exited' OR usage_released=0 LIMIT 1")
                and not tx.rows("SELECT id FROM effects WHERE status IN ('prepared','sent','observed') AND operation!='issue_link' LIMIT 1")
                and (completed_abandonment or not tx.rows("SELECT subject FROM resource_cleanup WHERE state!='complete' LIMIT 1")))

    @classmethod
    def open_task(cls, task: TaskKey) -> Kernel | None:
        """Find unfinished work under the original Task admission lock."""
        with locked_admission(task) as location:
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
                   message: str | None = None, new_thread: bool = False,
                   policy_overrides: Mapping[str, Any] | None = None, replacement: Delivery | None = None,
                   resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False,
                   continuation_of: str | None = None) -> HostLaunch:
        """Bind one Task and apply its explicit control with the same Host intent."""
        if (kind not in {"run", "resume", "approve", "revise", "requeue", "abandon"} or not action_id or not command
                or any(not part or "\0" in part for part in command) or not environment_carrier.is_absolute()):
            raise KernelError("host_request_invalid")
        preflight()  # External I/O precedes both Task binding and durable Action.
        intent: dict[str, Any] = {"kind": kind, "subject": subject, "message": message, "new_thread": new_thread,
                  "command": list(command), "environment_carrier": str(environment_carrier),
                  "policy_overrides": dict(policy_overrides or {})}
        if replacement is not None:
            intent["replacement"] = asdict(replacement)
        if resource_inspections is not None:
            intent["resource_inspections"] = [asdict(item) for item in resource_inspections]
        if discard_worktree:
            intent["discard_worktree"] = True
        if continuation_of is not None:
            intent["continuation_of"] = continuation_of
        if kind == "run" and policy_overrides:
            raise KernelError("policy_override_boundary")
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            original_source = bool(tx.rows("SELECT identity FROM history WHERE kind IN ('v6_original_cutover','v6_source_origin') LIMIT 1"))
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                if (kind != "abandon" or continuation_of != abandoned["id"] or abandoned["status"] != "pending"
                        or subject is not None or message is not None or new_thread or policy_overrides or replacement is not None
                        or type(discard_worktree) is not bool):
                    raise KernelError("run_abandoned")
            elif continuation_of is not None:
                raise KernelError("abandon_continuation_boundary")
            run_id, task = run["id"], self._task(run)
        with locked_admission(task) as location:
            prior = read_task(location)
            if original_source and prior != (run_id, self.storage.path.resolve()):
                if (prior is None or origin_reference(self.storage.path, task, run_id)
                        != origin_reference(prior[1], task, prior[0])):
                    raise KernelError("legacy_requires_offline_migration")
            if prior is not None and prior != (run_id, self.storage.path.resolve()):
                if not Kernel(prior[1])._replaceable(task, prior[0]):
                    raise KernelError("task_run_busy")
                self._carry_source_origin(task, run_id, origin_reference(prior[1], task, prior[0]))
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
                            tx.execute("UPDATE effects SET status='cancelled' WHERE id=?", (effect["id"],))
                            self._event(tx, effect["subject"], "abandon_preparation_cancelled", effect["id"], effect["payload_id"], generation=effect["generation"])
                        elif effect["operation"] == "inspect_resources" and effect["status"] == "observed":
                            tx.execute("UPDATE effects SET status='confirmed' WHERE id=?", (effect["id"],))
                            self._event(tx, effect["subject"], "effect_consumed", effect["id"], effect["receipt_id"], generation=effect["generation"])
                    for wait in tx.all("SELECT w.*,m.body FROM waits w JOIN material m ON m.id=w.scope_id"):
                        scope = json.loads(wait["body"])
                        if (scope.get("abandon_action") != continuation_of
                                or scope != self._automatic_scope(tx, run, wait["subject"], scope["cleanup"])):
                            continue
                        if wait["subject"] is not None:
                            for gate in self._gates(tx, wait["subject"]):
                                if (gate["code"] == "supervision_timeout" and gate["evidence"] is not None
                                        and json.loads(tx.get(gate["evidence"], wait["subject"])).get("scope_id") == wait["scope_id"]):
                                    tx.execute("DELETE FROM gates WHERE subject=? AND code='supervision_timeout' AND evidence=?",
                                               (wait["subject"], gate["evidence"]))
                        elif run["blocked"] == "supervision_timeout":
                            latest = tx.rows("SELECT m.body FROM history h JOIN material m ON m.id=h.material_id "
                                             "WHERE h.kind='paused' AND h.identity='supervision_timeout' ORDER BY h.position DESC LIMIT 1")
                            if latest and json.loads(latest[0][0]).get("scope_id") == wait["scope_id"]:
                                tx.execute("UPDATE runs SET blocked=NULL")
                        tx.execute("DELETE FROM waits WHERE scope_id=? AND kind=?", (wait["scope_id"], wait["kind"]))
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
                epoch = tx.one("SELECT epoch FROM runs")[0] + 1
                owner, nonce = identity(), identity()
                request = HostLaunch(run_id, epoch, owner, nonce, "agent-run-kernel-" + nonce + ".service", command, environment_carrier)
                material = tx.put(material_owner, "host_request", wire_json({**asdict(request), "environment_carrier": str(environment_carrier), "intent": intent}))
                tx.execute("INSERT INTO hosts(run_id,epoch,owner,nonce,action_id,subject,request_id,unit_name,allocated_at,status) VALUES (?,?,?,?,?,?,?,?,?,'allocated')",
                           (run_id, epoch, owner, nonce, action_id, material_owner, material, request.unit_name, self.clock()))
                tx.execute("UPDATE runs SET executor=?,epoch=?", (owner, epoch))
                self._event(tx, material_owner, "host_allocated", nonce, material)
                return request

    def control_host(self, request: HostLaunch, kind: str, action_id: str, *, subject: str | None = None,
                     world: World | None = None, message: str | None = None, new_thread: bool = False,
                     policy_overrides: Mapping[str, Any] | None = None, replacement: Delivery | None = None,
                     resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False) -> str:
        """Apply an explicit action through the exact current Task and Host."""
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            run_id, task = run["id"], self._task(run)
        with locked_admission(task) as location:
            if read_task(location) != (run_id, self.storage.path.resolve()):
                raise KernelError("task_location_identity")
            with self.storage.transaction() as tx:
                self._host(tx, request)
                run = tx.one("SELECT * FROM runs")
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
            run = tx.one("SELECT * FROM runs")
            run_id, task = run["id"], self._task(run)
        with locked_admission(task) as location:
            if read_task(location) != (run_id, self.storage.path.resolve()):
                raise KernelError("task_location_identity")
            with self.storage.transaction() as tx:
                return self._control(tx, "stop", action_id, subject=subject)

    def abandon_task(self, action_id: str, *, resource_inspections: tuple[ResourceInspection, ...],
                     discard_worktree: bool = False) -> str:
        """Record the permanent fence without reclaiming execution or launching cleanup."""
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            run_id, task = run["id"], self._task(run)
        with locked_admission(task) as location:
            if read_task(location) != (run_id, self.storage.path.resolve()):
                raise KernelError("task_location_identity")
            with self.storage.transaction() as tx:
                return self._control(tx, "abandon", action_id, resource_inspections=resource_inspections,
                                     discard_worktree=discard_worktree)

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
        with self.storage.transaction(write=False) as tx:
            original_source = bool(tx.rows("SELECT identity FROM history WHERE kind IN ('v6_original_cutover','v6_source_origin') LIMIT 1"))
            run = tx.one("SELECT * FROM runs")
        with ExitStack() as admission:
            if original_source:
                task = self._task(run)
                location = admission.enter_context(locked_admission(task))
                if read_task(location) != (run["id"], self.storage.path.resolve()):
                    raise KernelError("legacy_requires_offline_migration")
            with self.storage.transaction() as tx:
                run = tx.one("SELECT * FROM runs")
                if self._abandon_action(tx) is not None:
                    raise KernelError("run_abandoned")
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
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                return self._advance_abandon(tx, tx.one("SELECT * FROM runs"), abandoned, subject, execution, world)
            run, job = self._owned(tx, subject, execution)
            requeue = self._pending_requeue(tx, subject)
            if requeue is not None:
                return self._advance_requeue(tx, run, job, requeue, execution, world)
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
                if world.target is None and not (pending["operation"] == "run_branch" and job["kind"] == DeliveryKind.TICKET):
                    self._gate(tx, subject, "target_branch_missing")
                    return Step("paused", subject, "target_branch_missing")
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
            initialization = self._initial_run_branch(tx, run, job, execution, world)
            if initialization is not None:
                return initialization
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

    def begin_automatic_step(self, execution: Execution, subject: str | None = None, *, cleanup: bool = False) -> str:
        """Capture only this operation's business identity, never a Run snapshot.

        The Executor brackets one complete observe/advance/effect operation.
        Individual idempotent reads and receipt writes are deliberately outside
        the no-progress decision. The token is opaque to the I/O adapters.
        """
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            self._automatic_owned(tx, run, execution)
            return wire_json({"scope": self._automatic_scope(tx, run, subject, cleanup),
                              "progress": self._automatic_progress(tx, run, subject, cleanup)}).decode()

    def _automatic_owned(self, tx: Transaction, run: sqlite3.Row, execution: Execution) -> None:
        abandoned = self._abandon_action(tx)
        if abandoned is None:
            self._executor_owned(tx, run, execution)
        else:
            self._abandon_owned(tx, abandoned, execution)

    def _automatic_scope(self, tx: Transaction, run: sqlite3.Row, subject: str | None, cleanup: bool) -> dict[str, Any]:
        scope: dict[str, Any] = {"run": run["id"], "parent_revision": run["parent_revision"],
                                 "graph_revision": run["graph_revision"], "subject": subject, "cleanup": cleanup}
        abandoned = self._abandon_action(tx)
        if abandoned is not None:
            scope["abandon_action"] = abandoned["id"]
        if subject is None:
            scope["final_target"] = [dict(row) for row in tx.rows("SELECT * FROM final_target")]
        else:
            job = self._job(tx, subject)
            scope["delivery"] = {key: job[key] for key in (
                "generation", "revision", "phase", "purpose", "source_ref", "target_ref", "base_sha",
                "source_sha", "candidate_sha", "integrated_sha", "publication_sha", "pr_number",
                "current_attempt", "completion_revision")}
            requeue = self._pending_requeue(tx, subject)
            if requeue is not None:
                scope["requeue_action"] = requeue["id"]
        return scope

    def _automatic_requeue_read(self, tx: Transaction, run: sqlite3.Row, subject: str | None, cleanup: bool) -> bool:
        # This permits an observation only. Retirement still checks the exact
        # accepted versions, PR identity and publisher before any external write.
        return bool(subject is not None and not cleanup and not run["stopped"] and not run["final_action"]
                    and run["blocked"] == "requeue_required" and self._pending_requeue(tx, subject) is not None
                    and all(gate["code"] == "requeue_required" for gate in self._gates(tx, subject)))

    def _automatic_progress(self, tx: Transaction, run: sqlite3.Row, subject: str | None, cleanup: bool) -> str:
        # No sequence counters, diagnostics, history, clocks, polling attempts
        # or sibling records can make a stalled operation appear to progress.
        abandoned = self._abandon_action(tx)
        if abandoned is not None:
            unsettled = self._abandon_scope(tx, abandoned)["unsettled"]
            facts: Any = {"action": abandoned["id"], "status": abandoned["status"],
                          "effects": [dict(row) for row in tx.all(
                              "SELECT id,status,receipt_id FROM effects WHERE control_action=? ORDER BY id", (abandoned["id"],))],
                          "originals": [dict(tx.one("SELECT id,status,receipt_id FROM effects WHERE id=?", (effect_id,)))
                                        for effect_id in unsettled]}
        elif subject is None:
            facts = [dict(row) for row in tx.rows("SELECT id,generation,phase FROM subjects WHERE kind='final_run'")]
        else:
            job = self._job(tx, subject)
            facts = {"delivery": {key: job[key] for key in job.keys() if key not in {"version", "last_failure", "checks_id"}},
                     "attempt": [dict(row) for row in tx.rows(
                         "SELECT id,status,result_id,accounted,outcome,current_invocation,resume_action,new_thread "
                         "FROM attempts WHERE id=?", (job["current_attempt"],))],
                     "effects": [dict(row) for row in tx.all(
                         "SELECT id,operation,status,receipt_id FROM effects WHERE subject=? AND status IN ('prepared','sent','observed') ORDER BY id", (subject,))],
                     "gates": [dict(row) for row in tx.rows("SELECT code,purpose FROM gates WHERE subject=? ORDER BY code", (subject,))],
                     "work_queue": bool(tx.rows("SELECT subject FROM work_queue WHERE subject=?", (subject,))),
                     "integration_queue": bool(tx.rows("SELECT subject FROM integration_queue WHERE subject=?", (subject,))),
                     "integration_seat": run["integration_holder"] == subject,
                     "cleanup": [dict(row) for row in tx.rows("SELECT generation,stage,state,effect_id,outcome FROM resource_cleanup WHERE subject=? AND generation=?", (subject, job["generation"]))],
                     "link": [dict(row) for row in tx.rows("SELECT generation,state,effect_id FROM issue_links WHERE subject=?", (subject,))]}
        return self._fingerprint(facts)

    @staticmethod
    def _automatic_token(token: str) -> dict[str, Any]:
        try:
            if len(token.encode()) > CURRENT_LIMIT:
                raise ValueError("oversize token")
            value = json.loads(token)
            if not isinstance(value, dict) or set(value) != {"scope", "progress"} or not isinstance(value["scope"], dict):
                raise ValueError("invalid token")
            return value
        except (TypeError, ValueError, UnicodeError) as error:
            raise KernelError("automatic_step_identity") from error

    def guard_automatic_step(self, token: str, execution: Execution, *, after_read: bool = False,
                             readback: Effect | None = None) -> Step | None:
        """Recheck exact work and Stop after I/O, without clearing a read wait."""
        value = self._automatic_token(token)
        scope = value["scope"]
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            self._automatic_owned(tx, run, execution)
            if scope != self._automatic_scope(tx, run, scope["subject"], scope["cleanup"]):
                raise KernelError("automatic_step_superseded")
            if run["blocked"] == "supervision_timeout" or (scope["subject"] is not None and any(
                    gate["code"] == "supervision_timeout" for gate in self._gates(tx, scope["subject"]))):
                return Step("paused", scope["subject"] or "", "supervision_timeout")
            if readback is not None:
                row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (readback.effect_id, scope["subject"]))
                if row["status"] != "sent" or self._effect(tx, row, execution) != readback:
                    raise KernelError("effect_binding")
            if (readback is None and self._abandon_action(tx) is None
                    and not self._automatic_requeue_read(tx, run, scope["subject"], scope["cleanup"]) and
                    (run["stopped"] or run["blocked"] or (scope["subject"] is not None and self._gates(tx, scope["subject"])))):
                raise KernelError("automatic_step_paused")
            if not after_read:
                owner = scope["subject"] or tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
                rows = tx.rows("SELECT w.* FROM waits w JOIN material m ON m.id=w.scope_id "
                               "WHERE m.subject=? AND m.purpose='supervision_scope' AND m.body=? ORDER BY w.deadline LIMIT 1",
                               (owner, wire_json(scope)))
                if rows:
                    row = rows[0]
                    if self.clock() >= row["deadline"]:
                        seconds = 2700 if row["kind"] == "required_checks" else 600
                        return self._supervised_wait(tx, run, scope["subject"], scope, row["kind"], seconds)
                    if row["next_retry_at"] is not None and self.clock() < row["next_retry_at"]:
                        return Step("waiting", scope["subject"] or "", row["kind"], retry_at=row["next_retry_at"])
            return None

    def finish_automatic_step(self, token: str, execution: Execution, step: Step) -> Step:
        """Validate a whole operation's Progress and retire a completed read wait."""
        value = self._automatic_token(token)
        scope, subject = value["scope"], value["scope"]["subject"]
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            self._automatic_owned(tx, run, execution)
            if subject is not None and step.subject != subject and self._abandon_action(tx) is None:
                raise KernelError("automatic_step_identity")
            if step.kind == "progress" and value["progress"] == self._automatic_progress(tx, run, subject, scope["cleanup"]):
                owner = subject or tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
                evidence = tx.put(owner, "execution_failure", wire_json({"code": "controller_no_progress",
                    "message": "Controller returned Progress without changing its durable business identity", "scope": scope}))
                if subject is None:
                    tx.execute("UPDATE runs SET blocked='execution_failed'")
                    self._event(tx, owner, "paused", "controller_no_progress", evidence)
                else:
                    self._gate(tx, subject, "execution_failed", evidence=evidence)
                self._finish_final_failure(tx, run)
                return Step("paused", subject or "", "controller_no_progress")
            if step.kind not in {"waiting", "observe", "reconcile", "agent", "effect"} and step.reason != "supervision_timeout":
                tx.execute("DELETE FROM waits WHERE subject IS ? AND kind='github_convergence'", (subject,))
            return step

    def wait_for_read(self, token: str, execution: Execution, code: str, message: str) -> Step:
        """Persist one original read outage; successful reads alone never renew it."""
        from agent_run.error_safety import bounded_error

        value = self._automatic_token(token)
        scope, subject = value["scope"], value["scope"]["subject"]
        with self.storage.transaction() as tx:
            run = tx.one("SELECT * FROM runs")
            self._automatic_owned(tx, run, execution)
            if scope != self._automatic_scope(tx, run, subject, scope["cleanup"]):
                return Step("waiting", subject or "", "observation_superseded")
            gates = [] if subject is None else self._gates(tx, subject)
            abandoned = self._abandon_action(tx)
            requeue = self._automatic_requeue_read(tx, run, subject, scope["cleanup"])
            timed_out = run["blocked"] == "supervision_timeout" or any(gate["code"] == "supervision_timeout" for gate in gates)
            if (timed_out
                    or ((abandoned is None or abandoned["status"] != "pending") and not requeue
                        and (run["stopped"] or run["blocked"] or gates))):
                return Step("paused", subject or "", "supervision_timeout" if timed_out else run["blocked"] or "operator_gate")
            owner = subject or tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
            evidence = tx.put(owner, "github_read_observation", wire_json({"code": code, "message": bounded_error(message)}))
            if code in {"ambiguous_run_pr", "invalid_fixture", "invalid_parent", "missing_parent", "stale_run_pr",
                        "foreign_run_pr", "github_invalid_response", "change_pr_identity_mismatch", "change_pr_head_drift", "parent_close_external_conflict"}:
                if subject is None:
                    tx.execute("UPDATE runs SET blocked='deterministic_contradiction'")
                    self._event(tx, owner, "paused", code, evidence)
                else:
                    self._gate(tx, subject, "deterministic_contradiction", evidence=evidence)
                return Step("paused", subject or "", code)
            kind = "required_checks" if not requeue and subject is not None and self._job(tx, subject)["phase"] == "checks" else "github_convergence"
            seconds = 2700 if kind == "required_checks" else 600
            return self._supervised_wait(tx, run, subject, scope, kind, seconds, evidence)

    def _supervised_wait(self, tx: Transaction, run: sqlite3.Row, subject: str | None, scope: Mapping[str, Any],
                         kind: str, seconds: int, evidence: str | None = None) -> Step:
        owner = subject or tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
        scope_id = tx.put(owner, "supervision_scope", wire_json(scope))
        now = self.clock()
        rows = tx.rows("SELECT * FROM waits WHERE scope_id=? AND kind=?", (scope_id, kind))
        if not rows:
            # One current identity per subject and wait kind. Retain immutable
            # evidence, but never let an obsolete object govern a new candidate.
            tx.execute("DELETE FROM waits WHERE subject IS ? AND kind=?", (subject, kind))
            tx.execute("INSERT INTO waits(subject,kind,scope_id,started_at,deadline,evidence) VALUES (?,?,?,?,?,?)",
                       (subject, kind, scope_id, now, now + seconds, evidence))
        row = tx.one("SELECT * FROM waits WHERE scope_id=? AND kind=?", (scope_id, kind))
        if now >= row["deadline"]:
            detail = tx.put(owner, "supervision_timeout", wire_json({"kind": kind, "scope_id": scope_id,
                "started_at": row["started_at"], "deadline": row["deadline"], "budget_seconds": seconds,
                "retry_count": row["retry_count"], "latest_observation": evidence or row["evidence"]}))
            if evidence is not None:
                tx.execute("UPDATE waits SET evidence=? WHERE scope_id=? AND kind=?", (evidence, scope_id, kind))
            if subject is None:
                tx.execute("UPDATE runs SET blocked='supervision_timeout'")
                self._event(tx, owner, "paused", "supervision_timeout", detail)
            else:
                self._gate(tx, subject, "supervision_timeout", evidence=detail)
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                for pending in tx.rows("SELECT id FROM effects WHERE subject IS ? AND status='sent'", (subject,)):
                    self._event(tx, owner, "abandon_reconciliation_pending", pending["id"], detail)
            self._finish_final_failure(tx, run)
            return Step("paused", subject or "", "supervision_timeout")
        retries = row["retry_count"]
        retry_at = row["next_retry_at"]
        if retry_at is None or now >= retry_at:
            retries += 1
            retry_at = min(now + min(60, 5 * 2 ** min(retries - 1, 4)), row["deadline"])
        tx.execute("UPDATE waits SET retry_count=?,next_retry_at=?,evidence=COALESCE(?,evidence) WHERE scope_id=? AND kind=?",
                   (retries, retry_at, evidence, scope_id, kind))
        return Step("waiting", subject or "", kind, retry_at=retry_at)

    def agent_source_ready(self, call: AgentCall) -> None:
        """The original Worker is authorized to release, beyond a successful read."""
        with self.storage.transaction() as tx:
            invocation = self._agent_authority(tx, call)
            if invocation["dispatch_state"] != "started":
                raise KernelError("agent_not_started")
            tx.execute("DELETE FROM waits WHERE subject=? AND kind='github_convergence'", (call.subject,))

    def _whole_imported_local_resources(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                                       cleanup: sqlite3.Row, origin: Mapping[str, Any], imported: sqlite3.Row) -> tuple[str, str]:
        proof = json.loads(tx.get(imported["material_id"], imported["subject"]))
        root_record = {"table": "run_root", "key": [run["id"]]}
        if (imported["material_purpose"] != "v6_run_import" or imported["identity"] != origin["nonce"]
                or imported["generation"] != 1 or not isinstance(proof, dict)
                or proof.get("origin_nonce") != origin["nonce"] or proof.get("source_sha256") != origin["source_sha256"]
                or proof.get("resource_mode") != "existing-verified" or proof.get("run_record") != root_record
                or not isinstance(proof.get("subjects"), list)):
            raise KernelError("cleanup_marker_unproven")
        native = {row["id"]: row for row in tx.all("SELECT id,issue FROM subjects WHERE kind='ticket'")}
        capsules: dict[str, dict[str, Any]] = {}
        refs: set[str] = set()
        checkouts: set[str] = set()
        for entry in proof["subjects"]:
            if (not isinstance(entry, dict) or set(entry) != {"subject", "generation", "proof_id"}
                    or not isinstance(entry["subject"], str) or not isinstance(entry["proof_id"], str)
                    or entry["subject"] not in native or entry["subject"] in capsules or entry["generation"] != 1):
                raise KernelError("cleanup_marker_unproven")
            facts = tx.rows("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id "
                            "WHERE h.kind='v6_subject_import' AND h.material_id=? LIMIT 2", (entry["proof_id"],))
            if (len(facts) != 1 or facts[0]["material_purpose"] != "v6_subject_import"
                    or facts[0]["subject"] != entry["subject"] or facts[0]["generation"] != entry["generation"]
                    or facts[0]["identity"] != origin["nonce"]):
                raise KernelError("cleanup_marker_unproven")
            capsule = json.loads(tx.get(entry["proof_id"], entry["subject"]))
            if (not isinstance(capsule, dict) or capsule.get("origin_nonce") != origin["nonce"]
                    or capsule.get("source_sha256") != origin["source_sha256"] or capsule.get("subject") != entry["subject"]
                    or capsule.get("generation") != entry["generation"] or capsule.get("kind") not in {"completed", "returned", "unallocated"}
                    or not isinstance(capsule.get("resources"), list)
                    or (capsule["kind"] == "unallocated" and capsule["resources"])):
                raise KernelError("cleanup_marker_unproven")
            capsules[entry["subject"]] = capsule
            for resource in capsule["resources"]:
                if (not isinstance(resource, dict) or set(resource) != {"layout", "checkout_index_sha256", "source_record"}
                        or resource["source_record"] != capsule.get("source_record") or not isinstance(resource["layout"], dict)):
                    raise KernelError("cleanup_marker_unproven")
                layout = resource["layout"]
                if (not isinstance(layout.get("source_ref"), str) or not isinstance(layout.get("checkout"), str)
                        or layout["source_ref"] in refs or layout["checkout"] in checkouts):
                    raise KernelError("cleanup_marker_unproven")
                refs.add(layout["source_ref"])
                checkouts.add(layout["checkout"])
        run_ref = proof.get("run_ref_ownership")
        if (set(capsules) != set(native) or not isinstance(run_ref, dict)
                or set(run_ref) != {"source_ref", "expected_oid", "source_record", "head_evidence"}
                or run_ref["source_record"] != root_record or not isinstance(run_ref["source_ref"], str) or run_ref["source_ref"] in refs
                or not isinstance(run_ref["expected_oid"], str) or len(run_ref["expected_oid"]) not in {40, 64}
                or any(c not in "0123456789abcdef" for c in run_ref["expected_oid"])
                or not isinstance(run_ref["head_evidence"], list) or not run_ref["head_evidence"]
                or cleanup["source_ref"] != job["source_ref"] or cleanup["generation"] != job["generation"]
                or any(cleanup[key] != job[key] for key in ("checkout", "owned_root", "common_git_dir"))):
            raise KernelError("cleanup_marker_unproven")
        if job["kind"] == DeliveryKind.FINAL_RUN and cleanup["stage"] == 1:
            if run_ref["source_ref"] != cleanup["source_ref"]:
                raise KernelError("cleanup_marker_unproven")
            from .migration_records import V6RecordReader
            from .migration_run_ownership import authenticate_run_ownership

            try:
                # Historical ownership authenticates H0, not the current ref.
                # Native workspace receipts remain the separate write authority.
                head = authenticate_run_ownership(tx, proof, origin, Path(run["repository_root"]), V6RecordReader(self.storage))
            except (KernelError, KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
                raise KernelError("cleanup_marker_unproven") from error
        elif job["kind"] == DeliveryKind.TICKET and cleanup["stage"] in {0, 1}:
            capsule = capsules.get(job["id"], {})
            record = {"table": "work_jobs", "key": [run["id"], f"ticket:{job['issue']}:generation:{job['generation']}"]}
            resources = [resource for resource in capsule.get("resources", [])
                         if resource["layout"].get("checkout") == cleanup["checkout"]
                         and resource["layout"].get("source_ref") == cleanup["source_ref"]]
            if (capsule.get("generation") != job["generation"] or capsule.get("kind") not in {"completed", "returned"}
                    or capsule.get("source_record") != record or capsule.get("historical_base") != job["base_sha"] or len(resources) != 1):
                raise KernelError("cleanup_marker_unproven")
            resource, layout = resources[0], resources[0]["layout"]
            checkout = Path(origin["target_db"]).parent.parent / "worktrees" / run["id"] / f"ticket-{job['issue']}"
            if (set(layout) != {"version", "nonce", "source_sha256", "source_uuid", "repository_root", "checkout", "owned_root",
                               "common_git_dir", "source_ref", "expected_oid", "before_head", "checkout_archive_sha256"}
                    or layout["version"] != 1 or layout["nonce"] != origin["nonce"]
                    or layout["source_sha256"] != origin["source_sha256"] or layout["source_uuid"] != origin["source_uuid"]
                    or layout["repository_root"] != run["repository_root"] or layout["source_ref"] != cleanup["source_ref"]
                    or not isinstance(layout["expected_oid"], str) or len(layout["expected_oid"]) not in {40, 64}
                    or any(c not in "0123456789abcdef" for c in layout["expected_oid"])
                    or layout["before_head"] != "ref: " + cleanup["source_ref"] + "\n"
                    or any(layout[key] != cleanup[key] for key in ("checkout", "owned_root", "common_git_dir"))
                    or cleanup["checkout"] != str(checkout) or cleanup["owned_root"] != str(checkout.parent)
                    or cleanup["common_git_dir"] != str(Path(run["repository_root"]) / ".git")
                    or any(not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                           for value in (resource["checkout_index_sha256"], layout["checkout_archive_sha256"]))):
                raise KernelError("cleanup_marker_unproven")
            head = cleanup["workspace_sha"] if cleanup["stage"] == 0 else layout["expected_oid"]
        else:
            raise KernelError("cleanup_marker_unproven")
        if (not isinstance(head, str) or len(head) not in {40, 64} or any(c not in "0123456789abcdef" for c in head)):
            raise KernelError("cleanup_marker_unproven")
        return head, str(origin["source_uuid"])

    def _imported_local_resources(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                            cleanup: sqlite3.Row) -> tuple[str, str] | None:
        origins = tx.rows("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id "
                          "WHERE h.kind='v6_original_cutover' LIMIT 2")
        if not origins:
            return None
        if len(origins) != 1:
            raise KernelError("cleanup_marker_unproven")
        anchor = origins[0]
        original = json.loads(tx.get(anchor["material_id"], anchor["subject"]))
        if not isinstance(original, dict):
            raise KernelError("cleanup_marker_unproven")
        try:
            origin = read_source_fence(source_fence(original))
        except KernelError as error:
            raise KernelError("cleanup_marker_unproven") from error
        if (anchor["material_purpose"] != "v6_original_cutover" or anchor["identity"] != origin["nonce"]
                or origin["run_id"] != run["id"] or origin["target_db"] != str(self.storage.path)
                or origin["task_fingerprint"] != self._task(run).fingerprint):
            raise KernelError("cleanup_marker_unproven")
        manifests = tx.rows("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id "
                            "WHERE h.kind='v6_run_import' LIMIT 2")
        if manifests:
            if len(manifests) != 1 or manifests[0]["subject"] != anchor["subject"]:
                raise KernelError("cleanup_marker_unproven")
            return self._whole_imported_local_resources(tx, run, job, cleanup, origin, manifests[0])
        returned = tx.rows("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id "
                           "WHERE h.kind='v6_returned_import' LIMIT 2")
        if returned:
            layouts = tx.rows("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id "
                              "WHERE h.kind='v6_original_layout' LIMIT 2")
            if len(returned) != 1 or len(layouts) != 1:
                raise KernelError("cleanup_marker_unproven")
            imported, layout_fact = returned[0], layouts[0]
            proof = json.loads(tx.get(imported["material_id"], imported["subject"]))
            layout = json.loads(tx.get(layout_fact["material_id"], layout_fact["subject"]))
            sources = tx.rows("SELECT * FROM subjects WHERE id=? AND kind='ticket' AND generation=1", (imported["subject"],))
            if len(sources) != 1:
                raise KernelError("cleanup_marker_unproven")
            source = sources[0]
            checkout = Path(origin["target_db"]).parent.parent / "worktrees" / run["id"] / f"ticket-{source['issue']}"
            record = {"table": "work_jobs", "key": [run["id"], f"ticket:{source['issue']}:generation:1"]}
            if (imported["material_purpose"] != "v6_returned_import" or imported["identity"] != origin["source_sha256"]
                    or imported["subject"] != anchor["subject"]
                    or imported["generation"] != 1 or layout_fact["generation"] != 1 or job["generation"] != 1
                    or layout_fact["subject"] != imported["subject"]
                    or layout_fact["material_purpose"] != "v6_original_layout" or layout_fact["identity"] != origin["nonce"]
                    or not isinstance(proof, dict) or not isinstance(layout, dict)
                    or proof.get("resource_mode") != "existing-verified" or proof.get("source_sha256") != origin["source_sha256"]
                    or proof.get("source_record") != record or proof.get("original_layout") != layout
                    or proof.get("run_ref") != source["target_ref"]
                    or layout.get("version") != 1 or layout.get("nonce") != origin["nonce"]
                    or layout.get("source_sha256") != origin["source_sha256"] or layout.get("source_uuid") != origin["source_uuid"]
                    or layout.get("repository_root") != run["repository_root"]
                    or layout.get("source_ref") != source["source_ref"]
                    or layout.get("before_head") != "ref: " + source["source_ref"] + "\n"
                    or not isinstance(layout.get("expected_oid"), str)
                    or len(layout["expected_oid"]) not in {40, 64}
                    or any(character not in "0123456789abcdef" for character in layout["expected_oid"])
                    or any(layout.get(key) != source[key] for key in ("checkout", "owned_root", "common_git_dir"))
                    or source["checkout"] != str(checkout) or source["owned_root"] != str(checkout.parent)
                    or source["common_git_dir"] != str(Path(run["repository_root"]) / ".git")
                    or cleanup["source_ref"] != job["source_ref"]
                    or any(cleanup[key] != job[key] for key in ("checkout", "owned_root", "common_git_dir"))):
                raise KernelError("cleanup_marker_unproven")
            if job["kind"] == DeliveryKind.TICKET and job["id"] == imported["subject"]:
                # Native work advances the detached checkout; the original
                # local branch remains at its independently authenticated OID.
                head = cleanup["workspace_sha"] if cleanup["stage"] == 0 else layout["expected_oid"]
            elif job["kind"] == DeliveryKind.FINAL_RUN and cleanup["stage"] == 1:
                run_ref = proof.get("run_ref_ownership")
                if (not isinstance(run_ref, dict) or set(run_ref) != {"source_ref", "expected_oid", "source_record"}
                        or run_ref["source_ref"] != proof["run_ref"] or run_ref["source_ref"] != cleanup["source_ref"]
                        or run_ref["source_record"] != {"table": "run_root", "key": [run["id"]]}
                        or not isinstance(run_ref["expected_oid"], str) or len(run_ref["expected_oid"]) not in {40, 64}
                        or any(character not in "0123456789abcdef" for character in run_ref["expected_oid"])):
                    raise KernelError("cleanup_marker_unproven")
                head = run_ref["expected_oid"]
            else:
                raise KernelError("cleanup_marker_unproven")
            return head, origin["source_uuid"]
        ownership = tx.rows("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id "
                            "WHERE h.kind='v6_local_ref_ownership' AND h.identity=? LIMIT 2", (cleanup["source_ref"],))
        imports = tx.rows("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id "
                         "WHERE h.kind='v6_completed_import' AND h.identity=? LIMIT 2", (run["id"],))
        if len(origins) != 1 or len(ownership) != 1 or len(imports) != 1:
            raise KernelError("cleanup_marker_unproven")
        fact, imported = ownership[0], imports[0]
        value = json.loads(tx.get(fact["material_id"], fact["subject"]))
        proof = json.loads(tx.get(imported["material_id"], imported["subject"]))
        if (fact["material_purpose"] != "v6_local_ref_ownership" or fact["subject"] != imported["subject"]
                or imported["material_purpose"] != "v6_completed_import_identity"
                or not isinstance(proof, dict)
                or proof.get("resource_mode") != "existing-verified"
                or proof.get("source_sha256") != origin["source_sha256"]
                or proof.get("copied_repository_root") != run["repository_root"]
                or not isinstance(value, dict) or set(value) != {"origin_nonce", "source_ref", "expected_oid", "source_record"}
                or value["origin_nonce"] != origin["nonce"] or value["source_ref"] != cleanup["source_ref"]
                or not isinstance(value["expected_oid"], str)):
            raise KernelError("cleanup_marker_unproven")
        if job["kind"] == DeliveryKind.FINAL_RUN:
            if (value["source_record"] != {"table": "run_root", "key": [run["id"]]}
                    or value["expected_oid"] != proof.get("integrated_sha")):
                raise KernelError("cleanup_marker_unproven")
        elif job["kind"] == DeliveryKind.TICKET:
            old = proof.get("old_job")
            trees = tx.rows("SELECT h.*,m.purpose AS material_purpose FROM history h JOIN material m ON m.id=h.material_id "
                            "WHERE h.kind='v6_local_worktree_ownership' AND h.identity=? LIMIT 2", (cleanup["checkout"],))
            if (job["generation"] != 1 or job["id"] != imported["subject"] or not isinstance(old, dict)
                    or old.get("run_id") != run["id"] or old.get("owner_kind") != "ticket"
                    or old.get("value_ticket_number") != job["issue"]
                    or old.get("job_id") != f"ticket:{job['issue']}:generation:1" or len(trees) != 1):
                raise KernelError("cleanup_marker_unproven")
            tree = trees[0]
            tree_value = json.loads(tx.get(tree["material_id"], tree["subject"]))
            record = {"table": "work_jobs", "key": [run["id"], old["job_id"]]}
            checkout = Path(origin["target_db"]).parent.parent / "worktrees" / run["id"] / f"ticket-{job['issue']}"
            if (fact["generation"] != 1 or tree["generation"] != 1 or tree["subject"] != imported["subject"]
                    or tree["material_purpose"] != "v6_local_worktree_ownership"
                    or value["source_record"] != record or value["expected_oid"] != proof.get("publication_sha")
                    or value["expected_oid"] != old.get("value_publication_sha")
                    or cleanup["source_ref"] != "refs/heads/" + str(old.get("value_ticket_branch"))
                    or any(cleanup[key] != job[key] for key in ("checkout", "owned_root", "common_git_dir"))
                    or cleanup["checkout"] != str(checkout) or cleanup["owned_root"] != str(checkout.parent)
                    or cleanup["common_git_dir"] != str(Path(run["repository_root"]) / ".git")
                    or cleanup["workspace_sha"] != value["expected_oid"] or cleanup["source_sha"] != value["expected_oid"]
                    or tree_value != {"origin_nonce": origin["nonce"], "checkout": cleanup["checkout"],
                                      "expected_oid": value["expected_oid"], "source_record": record}):
                raise KernelError("cleanup_marker_unproven")
        else:
            raise KernelError("cleanup_marker_unproven")
        return value["expected_oid"], origin["source_uuid"]

    def advance_cleanup(self, subject: str, execution: Execution, world: World | None = None) -> Step:
        """Bounded program work; cleanup never owns an Agent or integration seat."""
        with self.storage.transaction() as tx:
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                self._abandon_owned(tx, abandoned, execution)
                return Step("complete" if abandoned["status"] == "complete" else "waiting", subject, "abandoned")
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
            source_uuid = None
            if operation == "cleanup_worktree" and job["kind"] == DeliveryKind.TICKET:
                workspaces = tx.rows("SELECT id FROM effects WHERE subject=? AND generation=? AND operation='workspace' AND status='confirmed'",
                                     (subject, cleanup["generation"]))
                if not workspaces:
                    try:
                        original = self._imported_local_resources(tx, run, job, cleanup)
                    except KernelError as error:
                        if error.code != "cleanup_marker_unproven":
                            raise
                        return Step("paused", subject, error.code)
                    if original is not None:
                        head, source_uuid = original
            if operation == "cleanup_local_ref":
                workspaces = tx.rows("SELECT * FROM effects WHERE subject=? AND generation=? AND source_ref=? AND operation='workspace' AND status='confirmed'",
                                     (subject, cleanup["generation"], cleanup["source_ref"]))
                if not workspaces and job["kind"] == DeliveryKind.TICKET:
                    try:
                        original = self._imported_local_resources(tx, run, job, cleanup)
                    except KernelError as error:
                        if error.code != "cleanup_marker_unproven":
                            raise
                        return Step("paused", subject, error.code)
                    if original is None:
                        return Step("paused", subject, "cleanup_marker_unproven")
                    head, source_uuid = original
                elif len(workspaces) != 1:
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
                        try:
                            original = self._imported_local_resources(tx, run, job, cleanup)
                        except KernelError as error:
                            if error.code != "cleanup_marker_unproven":
                                raise
                            return Step("paused", subject, error.code)
                        if original is None:
                            # An ordinary detached Final workspace creates no
                            # local branch. Remote publication grants no ownership.
                            tx.execute("UPDATE resource_cleanup SET stage=stage+1 WHERE subject=? AND generation=?",
                                       (subject, cleanup["generation"]))
                            self._event(tx, subject, "resource_not_owned", operation, workspace["payload_id"])
                            return Step("progress", subject)
                        head, source_uuid = original
                    elif intent.get("create_ref") is True:
                        head = workspace["before_sha"]
                    else:
                        return Step("paused", subject, "cleanup_marker_unproven")
            payload = {"checkout": cleanup["checkout"], "owned_root": cleanup["owned_root"],
                       "common_git_dir": cleanup["common_git_dir"], "branch_ref": cleanup["source_ref"], "expected_head": head,
                       "remote": self._inputs(tx, run)["repository_remote"]}
            if source_uuid is not None:
                payload["source_uuid"] = source_uuid
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
        if not self._thread_role_matches(self._job(tx, call.subject), call.role, binding):
            raise KernelError("thread_role_binding")
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

    def guard_effect(self, effect: Effect, execution: Execution, world: World, *,
                     resource_inspections: tuple[ResourceInspection, ...] | None = None) -> None:
        """Read-only authority guard for each real successor mutation in an adapter."""
        with self.storage.transaction(write=False) as tx:
            run, job = self._effect_owned(tx, effect, execution)
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
            self._guard_publisher(tx, run, row, world)
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                if row["status"] != "sent" or self._effect(tx, row, execution) != effect:
                    raise KernelError("operation_cancelled")
                self._abandon_write_current(tx, abandoned, row, resource_inspections)
                return
            if effect.operation == "retire_pr":
                if row["status"] != "sent" or self._effect(tx, row, execution) != effect or not self._retirement_authority(tx, run, job, row, world):
                    raise KernelError("operation_cancelled")
                return
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
            if (world.target is None and not (effect.operation == "run_branch" and job["kind"] == DeliveryKind.TICKET)
                    or effect.operation in {"integrate", "run_branch", "publication_commit", "push", "pr_write", "merge"}
                    and not self._effect_base_current(job, row, world)):
                raise KernelError("operation_boundary_changed")
            if effect.operation == "pr_write" and not self._pr_write_current(run, job, world):
                raise KernelError("pr_identity_changed")
            if effect.operation == "merge" and not self._merge_authority(tx, run, job, world):
                raise KernelError("merge_authority_changed")

    def control(self, kind: str, action_id: str, *, subject: str | None = None,
                world: World | None = None, message: str | None = None, new_thread: bool = False,
                policy_overrides: Mapping[str, Any] | None = None, replacement: Delivery | None = None,
                     resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False) -> str:
        """Apply one explicit operator intent atomically, with its application receipt.

        The offline prototype calls this trusted interface directly. Host launch,
        public capability isolation and process-exit proofs are separate adapters.
        """
        with self.storage.transaction() as tx:
            return self._control(tx, kind, action_id, subject=subject, world=world, message=message,
                                 new_thread=new_thread, policy_overrides=policy_overrides, replacement=replacement,
                                  resource_inspections=resource_inspections, discard_worktree=discard_worktree)

    def _control(self, tx: Transaction, kind: str, action_id: str, *, subject: str | None = None,
                 world: World | None = None, message: str | None = None, new_thread: bool = False,
                 policy_overrides: Mapping[str, Any] | None = None, replacement: Delivery | None = None,
                     resource_inspections: tuple[ResourceInspection, ...] | None = None, discard_worktree: bool = False) -> str:
        if kind not in {"stop", "resume", "approve", "revise", "requeue", "abandon"}:
            raise KernelError("prototype_control_unimplemented", kind)
        run = tx.one("SELECT * FROM runs")
        material_owner = subject or tx.one("SELECT id FROM subjects ORDER BY id LIMIT 1")[0]
        command: dict[str, Any] = {"kind": kind, "subject": subject, "message": message, "new_thread": new_thread,
                                   "policy_overrides": dict(policy_overrides or {})}
        if replacement is not None:
            command["replacement"] = asdict(replacement)
        if resource_inspections is not None:
            command["resource_inspections"] = [asdict(item) for item in resource_inspections]
        if discard_worktree:
            command["discard_worktree"] = True
        payload = wire_json(command)
        prior = tx.rows("SELECT * FROM actions WHERE id=?", (action_id,))
        if prior:
            if tx.get(prior[0]["payload_id"], material_owner) != payload:
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
                    or not any(gate["code"] in {"review_budget_exhausted", "modification_budget_exhausted", "ci_development_budget_exhausted"}
                               for gate in self._gates(tx, job["id"]))):
                raise KernelError("policy_override_boundary")
            policy = checkpoint_policy(self._subject_policy(tx, job), policy_overrides)
            policy_id = tx.put(job["id"], "delivery_policy", wire_json(self._policy_values(policy)))
            tx.execute("UPDATE subjects SET policy_id=? WHERE id=?", (policy_id, job["id"]))
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
            if tx.rows("SELECT id FROM subjects WHERE kind!='ticket' AND merge_sha IS NOT NULL LIMIT 1"):
                raise KernelError("merged_run_noncancellable")
            if tx.rows("SELECT e.id FROM effects e JOIN subjects s ON s.id=e.subject WHERE s.kind!='ticket' AND e.operation='merge' AND e.status IN ('sent','observed') LIMIT 1"):
                raise KernelError("final_merge_unsettled")
            abandonment = {"resources": [asdict(item) for item in resources], "discard_worktree": discard_worktree,
                           "execution": {"run_id": run["id"], "owner": run["executor"], "epoch": run["epoch"]},
                           "generations": [[row["id"], row["generation"]] for row in tx.all("SELECT id,generation FROM subjects ORDER BY id")],
                           "unsettled": [row[0] for row in tx.all("SELECT id FROM effects WHERE status IN ('prepared','sent','observed')") ]}
            tx.execute("UPDATE actions SET status='pending' WHERE id=?", (action_id,))
            if run["executor"] is not None:
                tx.execute("INSERT OR IGNORE INTO retired_executors VALUES (?,?,?,'pending')",
                           (run["id"], run["executor"], run["epoch"]))
            tx.execute("UPDATE runs SET epoch=epoch+1,integration_holder=NULL WHERE id=?", (run["id"],))
            tx.execute("DELETE FROM work_queue")
            tx.execute("DELETE FROM integration_queue")
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
                if not run["stopped"] and run["blocked"] != "supervision_timeout":
                    raise KernelError("no_resumable_boundary")
                tx.execute("UPDATE runs SET stopped=0 WHERE id=?", (run["id"],))
                if run["blocked"] == "supervision_timeout":
                    tx.execute("UPDATE runs SET blocked=NULL WHERE id=?", (run["id"],))
                    tx.execute("DELETE FROM waits WHERE subject IS NULL")
                tx.execute("UPDATE attempts SET resume_action=? WHERE id IN (SELECT s.current_attempt FROM subjects s WHERE s.run_id=? AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.subject=s.id))",
                           (action_id, run["id"]))
                if message is not None or new_thread:
                    feedback_scope = {row["id"]: {"generation": row["generation"], "attempt": row["current_attempt"],
                                                   "role": row["phase"] if row["phase"] in {"development", "review", "publish"} else None}
                                      for row in tx.all("SELECT s.* FROM subjects s WHERE s.run_id=? AND s.phase!='complete' AND NOT EXISTS (SELECT 1 FROM gates g WHERE g.subject=s.id) AND NOT EXISTS (SELECT 1 FROM dependencies d JOIN subjects p ON p.id=d.predecessor WHERE d.subject=s.id AND p.phase!='complete')", (run["id"],))
                                      if self._eligibility(tx, row) is None}
            else:
                assert subject is not None
                if tx.rows("SELECT id FROM leases WHERE subject=?", (subject,)):
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
                elif new_thread and job["phase"] == "development":
                    tx.execute("UPDATE subjects SET development_thread=NULL WHERE id=?", (subject,))
                elif new_thread and job["phase"] == "publish":
                    feedback_scope = {subject: {"generation": job["generation"], "attempt": None, "role": "publish"}}
                if job["current_attempt"] and not self._gates(tx, subject):
                    tx.execute("UPDATE attempts SET resume_action=? WHERE id=?", (action_id, job["current_attempt"]))
                if job["phase"] in {"development", "review", "publish"}:
                    self._ready(tx, subject, "work_queue")
                tx.execute("DELETE FROM waits WHERE subject=?", (subject,))
                if job["grant_id"] and job["phase"] in {"merge", "completion_comment", "close"}:
                    tx.execute("UPDATE runs SET final_action=? WHERE id=?", (action_id, run["id"]))
                    tx.execute("UPDATE actions SET status='pending' WHERE id=?", (action_id,))
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
                written = tx.rows("SELECT * FROM effects WHERE subject=? AND generation=? AND operation='pr_write' AND status='confirmed' ORDER BY rowid DESC LIMIT 1",
                                  (subject, job["generation"]))
                original_pr = (None if not written or written[0]["receipt_id"] is None else
                               json.loads(tx.get(written[0]["receipt_id"], subject)).get("pr"))
                if (prior_pr is None or prior_pr.number != job["pr_number"] or prior_pr.state not in {"OPEN", "CLOSED"}
                        or not self._pr_identity(run, job, prior_pr) or prior_pr.head.sha != job["publication_sha"]
                        or prior_pr.base != world.target or not isinstance(original_pr, dict)
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
                    tx.execute("DELETE FROM gates WHERE subject=? AND code IN ('requeue_scope_changed','requeue_retirement_changed')", (subject,))
            requeue_scope = {"generation": job["generation"], "revision": revision,
                             "parent_revision": world.parent_revision, "graph_revision": world.graph_revision,
                             "target": asdict(self._require_target(world)), "prior_pr": None if prior_pr is None else asdict(prior_pr),
                             "retired_effect": retired_effect}
            if prior_pr is None:
                self._replace_generation(tx, run, job, replacement, revision, action_id, payload_id)
            else:
                tx.execute("UPDATE actions SET status='pending' WHERE id=?", (action_id,))
        elif kind == "revise":
            if (job is None or subject is None or world is None or message is None
                    or job["kind"] != DeliveryKind.FINAL_RUN or job["phase"] != "approval"):
                raise KernelError("no_revision_boundary")
            if (run["stopped"] or run["blocked"] or self._gates(tx, subject)
                    or tx.rows("SELECT id FROM leases WHERE subject=?", (subject,))
                    or self._pending_effect(tx, subject) is not None
                    or not self._current(tx, job, run, world) or not self._published(tx, run, job, world)
                    or not self._review_current(tx, job, world)):
                raise KernelError("revision_currentness")
            feedback = tx.put(subject, "maintainer_revision", message.encode())
            self._renew(tx, job, Purpose.INTEGRATION.value, new_cycle=True)
            tx.execute("UPDATE subjects SET phase='development',purpose='integration',repair_source='human_revision',last_failure=?,current_attempt=NULL,integrated_sha=NULL,integrated_tree=NULL,grant_id=NULL,checks_id=NULL,gate_mode=NULL,resume_purpose=NULL,version=version+1 WHERE id=?",
                       (feedback, subject))
            self._event(tx, subject, "revision_requested", action_id, feedback)
            self._ready(tx, subject, "work_queue")
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
                       (grant_id, subject, action_id, job["pr_number"], job["publication_sha"], self._require_target(world).sha,
                        job["review_attempt"], job["checks_id"], job["revision"], run["parent_revision"],
                        run["graph_revision"], job["completion_revision"]))
            tx.execute("UPDATE subjects SET phase='merge',grant_id=?,version=version+1 WHERE id=?", (grant_id, subject))
            tx.execute("UPDATE runs SET final_action=? WHERE id=?", (action_id, run["id"]))
            tx.execute("UPDATE actions SET status='pending' WHERE id=?", (action_id,))
        receipt = tx.put(material_owner, "control_receipt", wire_json({"action": action_id, "kind": kind, "applied": True,
                                                                     "feedback_scope": feedback_scope, "requeue": requeue_scope, "abandonment": abandonment}))
        tx.execute("UPDATE actions SET receipt_id=? WHERE id=?", (receipt, action_id))
        self._event(tx, material_owner, "control_applied", action_id, receipt)
        if kind == "resume" and (subject is None or feedback_scope) and (message is not None or new_thread):
            self._event(tx, material_owner, "run_feedback_pending", action_id, receipt)
        return str(tx.one("SELECT status FROM actions WHERE id=?", (action_id,))[0])

    def _renew(self, tx: Transaction, job: sqlite3.Row, purpose: str, *, new_cycle: bool = False) -> None:
        old = tx.one("SELECT * FROM budgets WHERE subject=? AND purpose=? AND current=1", (job["id"], purpose))
        tx.execute("UPDATE budgets SET current=0 WHERE subject=? AND purpose=? AND window=?", (job["id"], purpose, old["window"]))
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
        tx.execute("INSERT INTO budgets VALUES (?,?,?,?,?,?,?,?,1)",
                   (job["id"], purpose, old["window"] + 1, developments, reviews, limit, review_limit, review_required))
        self._event(tx, job["id"], "budget_renewed", f"{purpose}:{old['window'] + 1}", job["policy_id"])

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
                "resource_cleanup": next((dict(row) for row in tx.rows("SELECT * FROM resource_cleanup WHERE subject=? AND generation=?", (subject, job["generation"]))), None),
                "issue_link": next((dict(row) for row in tx.rows("SELECT * FROM issue_links WHERE subject=?", (subject,))), None),
                "retired_generations": [dict(row) for row in tx.all("SELECT * FROM retired_generations WHERE subject=? ORDER BY generation", (subject,))],
            }

    def scheduling(self) -> dict[str, Any]:
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            jobs = tx.all("SELECT * FROM subjects ORDER BY id")
            active = tx.all("SELECT l.*,i.deadline_at,o.dispatch_state FROM leases l JOIN invocations i ON i.id=l.invocation JOIN invocation_outputs o ON o.invocation=i.id AND o.lease=l.id ORDER BY l.subject")
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                disposition = "complete" if abandoned["status"] == "complete" else "waiting" if active else "progress"
                inspection = self._abandon_inspection(tx, abandoned)
                if disposition == "progress" and inspection is not None and inspection["status"] in {"observed", "confirmed"}:
                    try:
                        self._check_abandon_inspection(tx, abandoned, inspection)
                    except KernelError:
                        disposition = "quiescent"
                if disposition == "progress" and self._abandon_reconciliation_blocked(tx, abandoned):
                    disposition = "quiescent"
                waits = [row for row in tx.all("SELECT w.*,m.body FROM waits w JOIN material m ON m.id=w.scope_id")
                         if json.loads(row["body"]) == self._automatic_scope(tx, run, row["subject"], False)]
                if disposition == "progress" and waits:
                    if (run["blocked"] == "supervision_timeout" or any(gate["code"] == "supervision_timeout"
                            for row in waits if row["subject"] is not None for gate in self._gates(tx, row["subject"]))):
                        disposition = "quiescent"
                    elif all(row["next_retry_at"] is not None and row["next_retry_at"] > self.clock() for row in waits):
                        disposition = "waiting"
                return {"concurrency": run["concurrency"], "integration_holder": None,
                        "active": [{key: row[key] for key in ("id", "subject", "invocation")} for row in active],
                        "work_fifo": [], "integration_fifo": [], "cancel_workers": True,
                        "disposition": disposition,
                        "next_deadline_at": min((row["next_retry_at"] or row["deadline"] for row in waits), default=None)}
            now = self.clock()
            deadlines = [float(row["deadline_at"]) for row in active]
            progress = any(row["dispatch_state"] == "allocated" and row["deadline_at"] <= now for row in active)
            leased = {row["subject"] for row in active}
            for job in jobs:
                subject = job["id"]
                gates = {row["code"] for row in self._gates(tx, subject)}
                pending = self._pending_effect(tx, subject)
                if self._pending_requeue(tx, subject) is not None:
                    progress |= not run["stopped"] and gates <= {"requeue_required"}
                    continue
                if pending is not None:
                    if pending["status"] == "sent":
                        progress |= not bool(gates & {"write_result_unknown", "write_conflict", "publication_retry_exhausted", "supervision_timeout"})
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
                waits = tx.rows("SELECT deadline,next_retry_at FROM waits WHERE subject=?", (subject,))
                if waits:
                    deadlines.extend(float(row["next_retry_at"] or row["deadline"]) for row in waits)
                    progress |= any((row["next_retry_at"] or row["deadline"]) <= now for row in waits)
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
                final_wait = tx.rows("SELECT deadline,next_retry_at FROM waits WHERE subject IS NULL")
                if final_wait:
                    deadlines.extend(float(row["next_retry_at"] or row["deadline"]) for row in final_wait)
                progress |= (bool(tickets) and all(self._completion(tx, job) is not None for job in tickets)
                             and (not final_wait or any((row["next_retry_at"] or row["deadline"]) <= now for row in final_wait)))
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
        if tx.rows("SELECT id FROM actions WHERE kind='abandon' LIMIT 1"):
            raise KernelError("executor_fenced")
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

    @staticmethod
    def _require_target(world: World) -> Commit:
        if world.target is None:
            raise KernelError("target_branch_missing")
        return world.target

    def _initial_run_branch(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row,
                            execution: Execution, world: World) -> Step | None:
        """One original empty-CAS precedes every Ticket workspace and Worker."""
        if world.target is not None and job["phase"] != "workspace":
            return None
        originals = tx.rows("SELECT e.* FROM effects e JOIN subjects s ON s.id=e.subject WHERE e.operation='run_branch' AND s.kind='ticket' AND (e.status NOT IN ('retired','cancelled') OR e.attempts>0) ORDER BY e.rowid DESC LIMIT 1")
        if originals and originals[0]["status"] in {"prepared", "sent", "observed"}:
            return Step("waiting", job["id"], "run_branch_initializing")
        if world.target is not None:
            return None
        targets = tx.rows("SELECT * FROM final_target")
        if (job["kind"] != DeliveryKind.TICKET or not targets or job["target_ref"] != targets[0]["source_ref"]
                or world.source is not None or world.pr is not None or originals
                or tx.rows("SELECT id FROM subjects WHERE phase!='workspace' OR generation!=1 LIMIT 1")
                or tx.rows("SELECT id FROM attempts LIMIT 1")
                or tx.rows("SELECT id FROM effects WHERE operation!='run_branch' LIMIT 1")):
            self._gate(tx, job["id"], "target_branch_missing")
            return Step("paused", job["id"], "target_branch_missing")
        if not self._current(tx, job, run, world):
            return Step("paused", job["id"], "currentness")
        return self._prepare_effect(tx, run, job, execution, world, "run_branch",
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
        if not isinstance(values, dict) or values.keys() != self._policy_values(Policy()).keys():
            raise KernelError("policy_snapshot_invalid")
        return Policy(**values)

    def _subject_policy(self, tx: Transaction, job: sqlite3.Row) -> Policy:
        if job["policy_id"] is None:
            return self._policy(tx.one("SELECT * FROM runs WHERE id=?", (job["run_id"],)))
        return self._decode_policy(tx.get(job["policy_id"], job["id"]))

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
        previous = tx.rows("SELECT i.thread FROM attempts a JOIN invocations i ON i.attempt=a.id "
                           "JOIN threads t ON t.id=i.thread WHERE a.subject=? AND a.generation=? "
                           "AND a.role='publication' AND t.role='publication' AND t.backend_id IS NOT NULL "
                           "ORDER BY a.ordinal DESC,i.rowid DESC LIMIT 1", (job["id"], job["generation"]))
        if previous:
            return str(previous[0][0])
        publication = self._profile_document(tx, run["profile_revision"])["profiles"]["publication"]
        attempts = tx.one("SELECT count(*) FROM attempts WHERE subject=? AND generation=? AND role='publication'",
                          (job["id"], job["generation"]))[0]
        # The original selection observes the newly allocated role ordinal.
        if publication["reference"] == "development" and attempts + 1 < 4:
            development_thread = job["development_thread"]
            return str(development_thread) if development_thread is not None else None
        return None

    def configure_task_profiles(self, action_id: str, *, preset: str | None = None,
                                overrides: ProfileOverrides | None = None) -> int:
        """Configure future Threads only under this exact published Task's lock."""
        with self.storage.transaction(write=False) as tx:
            run = tx.one("SELECT * FROM runs")
            run_id, task = run["id"], self._task(run)
        with locked_admission(task) as location:
            if read_task(location) != (run_id, self.storage.path.resolve()):
                raise KernelError("task_location_identity")
            return self.configure_profiles(action_id, preset=preset, overrides=overrides)

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
        policy = self._subject_policy(tx, job)
        force_new_thread = False
        bound_feedback: list[str] = []
        feedback_actions = tx.all("SELECT a.* FROM history p JOIN actions a ON a.id=p.identity WHERE p.kind='run_feedback_pending' AND a.run_id=? AND NOT EXISTS (SELECT 1 FROM history b WHERE b.kind IN ('run_feedback_bound','run_feedback_retired') AND b.identity=a.id) ORDER BY p.position", (run["id"],))
        live_generations = {(row["id"], row["generation"]) for row in tx.all("SELECT id,generation FROM subjects")} if feedback_actions else set()
        for action in feedback_actions:
            owner = tx.one("SELECT subject FROM material WHERE id=?", (action["payload_id"],))[0]
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
                    tx.execute("UPDATE attempts SET new_thread=1 WHERE id=?", (pending["id"],))
                    pending = tx.one("SELECT * FROM attempts WHERE id=?", (pending["id"],))
                bound_feedback.append(action["id"])
            elif not any((key, value["generation"]) in live_generations for key, value in receipt["feedback_scope"].items()):
                self._event(tx, owner, "run_feedback_retired", action["id"], action["receipt_id"])
        attempt_id = pending["id"] if pending is not None else identity()
        previous_output = None if pending is None else tx.one(
            "SELECT i.*,o.* FROM invocations i JOIN invocation_outputs o ON o.invocation=i.id WHERE i.id=? ORDER BY ordinal DESC LIMIT 1",
            (pending["current_invocation"],))
        thread_id = None
        if pending is not None and not pending["new_thread"]:
            previous = tx.one("SELECT thread FROM invocations WHERE id=?", (pending["current_invocation"],))
            thread_id = previous["thread"]
        elif pending is None and role == Role.DEVELOPMENT and not policy.new_thread_per_development and not force_new_thread:
            thread_id = job["development_thread"]
        elif pending is None and role == Role.PUBLICATION and not force_new_thread:
            thread_id = self._publication_thread(tx, run, job)
        if thread_id is None:
            thread_id = identity()
            tx.execute("INSERT INTO threads(id,subject,role,profile_revision) VALUES (?,?,?,?)", (thread_id, subject, role.value, run["profile_revision"]))
        binding = tx.one("SELECT * FROM threads WHERE id=?", (thread_id,))
        if not self._thread_role_matches(job, role, binding):
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
        for action_id in bound_feedback:
            self._event(tx, subject, "run_feedback_bound", action_id, context_id)
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
        revisions = tx.rows("SELECT material_id FROM history WHERE subject=? AND generation=? AND kind='revision_requested' ORDER BY position DESC LIMIT 1",
                            (job["id"], job["generation"]))
        if revisions:
            context["human_feedback"] = tx.get(revisions[0][0], job["id"]).decode()
        if role == Role.DEVELOPMENT and job["repair_source"] not in {None, "human_revision"}:
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
                "requirements", "ticket_completions", "work_source", "ci_evidence", "human_feedback",
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
        result = json.loads(tx.get(row["receipt_id"], job["id"]))
        self._validate_target(tx, tx.one("SELECT * FROM runs"), job, row, result)
        commit = result["commit"]
        operation = row["operation"]
        if result["disposition"] == "target":
            if operation in {"candidate", "integrate"} and commit:
                tx.execute("UPDATE subjects SET workspace_sha=? WHERE id=?", (commit["sha"], job["id"]))
            elif operation == "push" or operation == "run_branch" and job["kind"] == DeliveryKind.FINAL_RUN:
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
        tx.execute("INSERT INTO final_barriers VALUES (?,?,?,?,?,?)", (job["id"], generation, prior["completion_revision"], world.source.sha, self._require_target(world).sha, prior["evidence"]))
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
        target = self._require_target(world)
        if phase == "workspace":
            if (job["kind"] == DeliveryKind.TICKET and world.source is None and world.pr is None
                    and all(job[key] is None for key in ("current_attempt", "development_thread", "candidate_sha", "integrated_sha", "publication_sha", "pr_number"))
                    and job["base_sha"] == job["source_sha"] == job["workspace_sha"]
                    and not tx.rows("SELECT id FROM attempts WHERE subject=? AND generation=? LIMIT 1", (subject, job["generation"]))
                    and not tx.rows("SELECT id FROM effects WHERE subject=? AND generation=? AND operation='workspace' LIMIT 1", (subject, job["generation"]))):
                # Job allocation fixes the dependency-visible Run head once.
                # A durable workspace intent, including a failed one, keeps it.
                tx.execute("UPDATE subjects SET base_sha=?,base_tree=?,source_sha=?,workspace_sha=? WHERE id=?",
                           (target.sha, target.tree, target.sha, target.sha, subject))
                job = self._job(tx, subject)
            return self._prepare_effect(tx, run, job, execution, world, "workspace", before=job["source_sha"],
                                        payload={"checkout": job["checkout"], "create_ref": job["kind"] != DeliveryKind.FINAL_RUN})
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
                                        before=target.sha,
                                        payload={"checkout": job["checkout"], "source_sha": None if world.source is None else world.source.sha})
        if phase == "ready_publication":
            if job["kind"] == DeliveryKind.FINAL_RUN and job["integrated_sha"] != job["source_sha"]:
                return self._prepare_effect(tx, run, job, execution, world, "run_branch", before=job["source_sha"],
                                            after=job["integrated_sha"])
            tx.execute("UPDATE subjects SET phase='publish',version=version+1 WHERE id=?", (subject,))
            self._acceptance_promoted(tx, self._job(tx, subject), world)
            return Step("progress", subject)
        if phase == "publication_commit":
            attempt = tx.one("SELECT result_id FROM attempts WHERE id=?", (job["publication_attempt"],))
            narrative = json.loads(tx.get(attempt["result_id"], subject))
            parents = [job["source_sha"]]
            if (job["kind"] == DeliveryKind.PARENT_ONLY and target.sha != job["source_sha"]
                    and (target.sha, job["source_sha"]) not in world.ancestry):
                parents.append(target.sha)
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
            return self._prepare_effect(tx, run, job, execution, world, "merge", before=target.sha,
                                        after=job["publication_sha"], payload={"method": "squash" if job["kind"] == DeliveryKind.TICKET else "merge",
                                        "commit_message": narrative["commit_message"] if job["kind"] == DeliveryKind.TICKET else f"merge: Delivery Run {job['source_ref'].removeprefix('refs/heads/')}",
                                        "parents": [target.sha] + ([] if job["kind"] == DeliveryKind.TICKET else [job["publication_sha"]])})
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
        return self._supervised_wait(tx, run, job["id"], self._automatic_scope(tx, run, job["id"], False), kind, seconds)

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

    def _integration_origin(self, tx: Transaction, job: sqlite3.Row) -> Commit:
        if job["workspace_sha"] == job["candidate_sha"] and job["candidate_tree"]:
            return Commit(job["candidate_sha"], job["candidate_tree"])
        receipts = tx.rows("SELECT receipt_id FROM effects WHERE subject=? AND generation=? AND status='confirmed' AND operation IN ('candidate','integrate','publication_commit') ORDER BY rowid DESC LIMIT 1",
                           (job["id"], job["generation"]))
        if receipts:
            result = json.loads(tx.get(receipts[0][0], job["id"]))
            commit = result.get("commit")
            if result["disposition"] == "target" and commit is not None and commit["sha"] == job["workspace_sha"]:
                return Commit(commit["sha"], commit["tree"])
        raise KernelError("integration_workspace_unproven")

    def _prepare_effect(self, tx: Transaction, run: sqlite3.Row, job: sqlite3.Row, execution: Execution,
                        world: World, operation: str, *, before: str, after: str | None = None,
                        payload: dict[str, Any] | None = None) -> Step:
        subject, effect_id = job["id"], identity()
        actor = self._publisher_actor(tx, run, world) if operation in self.REMOTE else None
        initial = operation == "run_branch" and job["kind"] == DeliveryKind.TICKET
        if initial:
            target = tx.one("SELECT * FROM final_target")
            source_ref, target_ref = target["source_ref"], target["target_ref"]
            base = Commit(job["base_sha"], job["base_tree"])
        elif operation in self.CLEANUP:
            closed = tx.one("SELECT source_ref,target_ref,base_sha,base_tree FROM effects WHERE subject=? AND generation=? AND operation='close' AND status='confirmed'",
                            (subject, job["generation"]))
            source_ref, target_ref = closed["source_ref"], closed["target_ref"]
            base = Commit(closed["base_sha"], closed["base_tree"])
        elif operation in {"inspect_resources", "abandon_worktree"}:
            source_ref, target_ref = job["source_ref"], job["target_ref"]
            base = Commit(job["base_sha"], job["base_tree"])
        else:
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
        tx.execute("""INSERT INTO effects(id,subject,operation,status,executor,epoch,parent_revision,
            graph_revision,revision,generation,source_ref,target_ref,before_sha,after_sha,candidate_sha,
            candidate_tree,base_sha,base_tree,pr_number,actor,payload_id,control_action)
            VALUES (?,?,?,'prepared',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (effect_id, subject, operation, execution.owner, execution.epoch, run["parent_revision"],
                    run["graph_revision"], job["revision"], job["generation"], source_ref, target_ref,
                    before, after, candidate_sha, candidate_tree, base.sha, base.tree,
                    job["pr_number"], actor, payload_id, body.get("control_action")))
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
            tx.execute("UPDATE effects SET payload_id=? WHERE id=?", (material, effect.effect_id))
            self._event(tx, effect.subject, "publication_commit_prepared", effect.effect_id, material)
            return self._effect(tx, tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,)), execution)

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
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
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
            tx.execute("UPDATE effects SET payload_id=? WHERE id=?", (material, effect.effect_id))
            self._event(tx, effect.subject, "retirement_intent_prepared", effect.effect_id, material)
            return self._effect(tx, tx.one("SELECT * FROM effects WHERE id=?", (effect.effect_id,)), execution)

    def dispatch(self, effect: Effect, execution: Execution, world: World, *,
                 resource_inspections: tuple[ResourceInspection, ...] | None = None) -> bool:
        """Recheck authority immediately before the single external operation."""
        with self.storage.transaction() as tx:
            run, job = self._effect_owned(tx, effect, execution)
            row = tx.one("SELECT * FROM effects WHERE id=? AND subject=?", (effect.effect_id, effect.subject))
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
                tx.execute("UPDATE effects SET status='sent',attempts=attempts+1 WHERE id=?", (effect.effect_id,))
                self._event(tx, effect.subject, "effect_dispatched", effect.effect_id, generation=row["generation"])
                return True
            if effect.operation == "retire_pr":
                if not effect.payload.get("retirement_intent"):
                    raise KernelError("retirement_intent_required")
                if not self._retirement_authority(tx, run, job, row, world):
                    return False
                tx.execute("UPDATE effects SET status='sent',attempts=attempts+1 WHERE id=?", (effect.effect_id,))
                self._event(tx, effect.subject, "effect_dispatched", effect.effect_id)
                return True
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
            if world.target is None and not (effect.operation == "run_branch" and job["kind"] == DeliveryKind.TICKET):
                self._gate(tx, effect.subject, "target_branch_missing")
                return False
            if (effect.operation in {"integrate", "run_branch", "publication_commit", "push", "pr_write", "merge"}
                    and not self._effect_base_current(job, row, world)):
                if effect.operation == "run_branch" and job["kind"] == DeliveryKind.TICKET:
                    self._gate(tx, effect.subject, "target_branch_changed")
                    return False
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
                requeue = self._pending_requeue(tx, effect.subject)
                if (row["status"] == "confirmed" and requeue is not None
                        and self._requeue_scope(tx, requeue).get("retired_effect") == row["id"]):
                    run, job = self._owned(tx, effect.subject, execution)
                    if self._effect(tx, row, execution) != effect:
                        raise KernelError("effect_binding")
                    return self._recheck_retirement(tx, run, job, requeue, row, result)
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
            abandoned = self._abandon_action(tx)
            if abandoned is not None:
                self._abandon_owned(tx, abandoned, execution)
                if row["control_action"] == abandoned["id"]:
                    payload = json.loads(tx.get(row["payload_id"], effect.subject))
                    if result.disposition == "third" and row["operation"] in {"reopen_ticket", "recovery_comment"} and self._reopen_not_owned(payload, asdict(result)):
                        receipt = tx.put(effect.subject, "effect_receipt", wire_json(asdict(result)))
                        tx.execute("UPDATE effects SET status='observed',receipt_id=? WHERE id=?", (receipt, effect.effect_id))
                        self._event(tx, effect.subject, "ticket_recovery_not_owned", effect.effect_id, receipt, generation=row["generation"])
                        return Step("progress", effect.subject)
                    if row["operation"] in {"reopen_ticket", "recovery_comment"} and result.disposition in {"prior", "unknown"}:
                        evidence = tx.put(effect.subject, "github_read_observation", wire_json({
                            "code": "github_recovery_observation_pending", "effect": effect.effect_id, "result": asdict(result)}))
                        run = tx.one("SELECT * FROM runs")
                        return self._supervised_wait(tx, run, effect.subject,
                            self._automatic_scope(tx, run, effect.subject, False), "github_convergence", 600, evidence)
                    if (result.disposition == "prior" and self._abandon_control_scope(tx, abandoned, row)
                            and row["attempts"] - row["retry_baseline"] < 3):
                        evidence = tx.put(effect.subject, "abandon_prior_observation", wire_json({"dispatch_serial": row["attempts"], "result": asdict(result)}))
                        self._event(tx, effect.subject, "abandon_prior_confirmed", effect.effect_id, evidence, generation=row["generation"])
                        tx.execute("UPDATE effects SET status='retired' WHERE id=?", (effect.effect_id,))
                        return Step("progress", effect.subject)
                elif effect.effect_id in self._abandon_scope(tx, abandoned)["unsettled"] and result.disposition == "prior":
                    tx.execute("UPDATE effects SET status='retired' WHERE id=?", (effect.effect_id,))
                    self._event(tx, effect.subject, "abandoned_business_intent_not_applied", effect.effect_id, generation=row["generation"])
                    return Step("progress", effect.subject)
                evidence = tx.put(effect.subject, "abandon_observation", wire_json(asdict(result)))
                self._event(tx, effect.subject, "abandon_reconciliation_pending", effect.effect_id, evidence, generation=row["generation"])
                return Step("paused", effect.subject, result.disposition)
            run, job = self._owned(tx, effect.subject, execution)
            if result.disposition == "prior":
                if world is None:
                    return Step("observe", effect.subject)
                if row["operation"] == "retire_pr":
                    if not self._retirement_authority(tx, run, job, row, world):
                        return Step("paused", effect.subject, "retirement_authority")
                    if row["attempts"] - row["retry_baseline"] >= 3:
                        self._gate(tx, effect.subject, "publication_retry_exhausted")
                        return Step("paused", effect.subject, "publication_retry_exhausted")
                    tx.execute("UPDATE effects SET status='prepared' WHERE id=?", (effect.effect_id,))
                    return Step("progress", effect.subject)
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
                        and not self._effect_base_current(job, row, world)):
                    if row["operation"] == "run_branch" and job["kind"] == DeliveryKind.TICKET:
                        self._gate(tx, effect.subject, "target_branch_changed")
                        return Step("paused", effect.subject, "target_branch_changed")
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
            if row["base_sha"] != self._require_target(world).sha or payload["source_sha"] != live_source:
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
            if job["kind"] == DeliveryKind.TICKET:
                phase = "workspace"
            else:
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
            phase = "checks" if row["base_sha"] == self._require_target(world).sha else "integration"
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
        if operation == "run_branch" and phase == "publish":
            self._acceptance_promoted(tx, self._job(tx, subject), world)
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
                   (job["id"], previous["generation"] + 1, self._subject_policy(tx, job).resync_limit))
        self._event(tx, job["id"], "resync_renewed", str(previous["generation"] + 1))

    def _observe_integration_snapshot(self, tx: Transaction, job: sqlite3.Row, world: World) -> bool:
        """Only an external change after a completed window spends allowance."""
        subject = job["id"]
        target = self._require_target(world)
        if world.source is None and job["pr_number"] is not None:
            self._gate(tx, subject, "published_head_changed")
            return False
        source_sha = job["source_sha"] if world.source is None else world.source.sha
        previous = tx.rows("SELECT * FROM integration_snapshots WHERE subject=? ORDER BY window DESC LIMIT 1", (subject,))
        allowance = tx.one("SELECT * FROM resync_allowances WHERE subject=? AND current=1", (subject,))
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
            tx.execute("INSERT INTO integration_snapshots VALUES (?,?,?,?,?,?,?,1)",
                       (subject, window, allowance["generation"], job["source_ref"], job["target_ref"], source_sha, target.sha))
            if charged:
                tx.execute("UPDATE resync_allowances SET used=used+1 WHERE subject=? AND current=1", (subject,))
        elif snapshot is not None and (source_changed or target_changed):
            tx.execute("UPDATE integration_snapshots SET source_sha=?,target_sha=? WHERE subject=? AND window=?",
                       (source_sha, target.sha, subject, snapshot["window"]))
        if world.source is not None and source_sha != job["source_sha"]:
            tx.execute("UPDATE subjects SET source_sha=? WHERE id=?", (source_sha, subject))
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
        if not tx.rows("SELECT position FROM history WHERE subject=? AND generation=? AND kind='acceptance_promoted' AND identity=? AND material_id=? LIMIT 1",
                       (job["id"], job["generation"], job["review_attempt"], material)):
            self._event(tx, job["id"], "acceptance_promoted", job["review_attempt"], material)

    def _review_current(self, tx: Transaction, job: sqlite3.Row, world: World) -> bool:
        if not job["review_attempt"] or world.target is None:
            return False
        review = tx.one("SELECT * FROM attempts WHERE id=?", (job["review_attempt"],))
        return bool(review["outcome"] == "pass" and review["revision"] == job["revision"]
                and review["candidate_sha"] == job["candidate_sha"] and review["candidate_tree"] == job["candidate_tree"]
                and review["base_sha"] == self._require_target(world).sha and review["expected_tree"] == job["expected_tree"]
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
        return (world.target is not None and pr is not None and pr.number == job["pr_number"] and self._pr_identity(run, job, pr)
                and world.source == pr.head and pr.head.sha == job["publication_sha"] and pr.head.tree == job["publication_tree"]
                and pr.base.sha == job["integration_target"] and self._require_target(world).sha == pr.base.sha and pr.state == "OPEN")

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
                and grant["base_sha"] == self._require_target(world).sha and grant["review_attempt"] == job["review_attempt"]
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
