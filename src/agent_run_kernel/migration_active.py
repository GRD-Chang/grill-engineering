"""Bounded v6 returned-role conversion for copied or canonical offline sources.

Old calls remain historical evidence. Only the normalized current result enters
native consumption; no old executor, lease or Invocation is fabricated.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import tarfile
from typing import Any, Callable, Mapping, TypeVar

from agent_run.managed_workspace import ManagedWorkspace

from .effects import LocalGit
from .kernel import Kernel
from .migration import (MigrationRefused, SourceSnapshot, _created_at_epoch,
                        _require_no_v6_notification_journal, capture_v6, validate_snapshot)
from .migration_conversion import _git, _one, _policy
from .migration_checkout import _verify_checkout_archive, seal_checkout
from .migration_quiescence import CONTROL_TABLES, read_v6_host_exit
from .migration_records import _preserve_copy
from .migration_facts import (ImportSource, ImportedAttempt, ImportedBudget, ImportedHistoryEvent,
    ImportedResource, ImportedRunFacts, ImportedSubject, HistoricalExecution, SourceRecord)
from .model import (Blocker, Commit, CreationInputs, Delivery, DeliveryKind, Issue,
                    KernelError, ParentIssue, Policy, Profile, Purpose, Requirements, Role,
                    RunSpec, artifact, wire_json)


_ACTIVE_TABLES = frozenset({
    "schema_meta", "runs", "work_evidence_materials", "work_evidence", "work_jobs",
    "work_job_layout", "work_job_slots", "work_job_archives", "work_job_review_artifacts",
    "work_job_acceptance_record", "work_job_review_budget", "work_job_ci_recovery",
    "work_job_integration_resync", "work_job_archive_roots", "work_attempts",
    "work_job_candidate_commit_intent",
    "work_attempt_versions", "work_attempt_current", "work_attempt_returns",
    "work_attempt_result_members", "work_job_attempt_roots", "work_job_attempt_history",
    "work_invocations", "work_invocation_roots", "work_invocation_current",
    "work_invocation_history", "run_root", "run_parent_versions", "run_graph_versions",
    "run_graph_members", "run_graph_snapshots", "run_graph_scope_order",
    "run_graph_ticket_slots", "run_frontier", "run_condition_base", "run_query_roots",
    "run_timeline_events", "run_timeline_slots", "run_resume_roots",
}) | CONTROL_TABLES

# These are durable publication intents, write outcomes or remote identities,
# not proof that the current role return is the only unconsumed work.  Their
# reconciliation is outside this returned-only converter.  Linked Branch
# display is also unsupported here even when its outcome is already known.
_RETURNED_WRITE_FIELDS = (
    "value_publication", "value_publication_sha", "value_publication_parent_sha",
    "value_publication_prior_published_sha", "value_published_sha",
    "value_published_head_sha", "value_published_integration_head_sha",
    "value_integrated_publication_sha", "value_pr_number", "value_pull_number",
    "value_linked_branch_display", "value_branch_publication_observation",
)


@dataclass(frozen=True)
class _Call:
    attempt_id: str
    role: Role
    old_ordinal: int
    thread_id: str
    invocation: dict[str, Any]
    bound_version: dict[str, Any]
    current_version: dict[str, Any]
    returned: dict[str, Any]
    raw_evidence_id: str
    native_artifact: dict[str, Any]


@dataclass(frozen=True)
class _Active:
    run_id: str
    repository: str
    parent_number: int
    parent_revision: str
    graph_revision: str
    created_at: float
    language: str
    requirements: Requirements
    policy: Policy
    inputs: CreationInputs
    run_branch: str
    base_branch: str
    base_sha: str
    ticket: int
    source_ref: str
    candidate_sha: str | None
    candidate_tree: str | None
    integration_target: str | None
    resync_window: int
    pending_role: Role
    candidate_intent: dict[str, Any] | None
    calls: tuple[_Call, ...]
    bindings: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    timeline: tuple[dict[str, Any], ...]


def _wire_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key + "_hex" if isinstance(value, bytes) else key:
            value.hex() if isinstance(value, bytes) else value
            for key, value in row.items()}


def _evidence(db: sqlite3.Connection, run_id: str, evidence_id: str,
              *, owner: str, kind: str) -> Any:
    row = _one(db, "SELECT * FROM work_evidence WHERE run_id=? AND evidence_id=?", (run_id, evidence_id))
    if (row["owner_id"] != owner or row["kind"] != kind
            or type(row["value_bytes"]) is not int or not 0 <= row["value_bytes"] <= 4 * 1024 * 1024):
        raise MigrationRefused("source_evidence")
    if row["source_evidence_id"] is not None:
        source = _one(db, "SELECT * FROM work_evidence WHERE run_id=? AND evidence_id=?",
                      (run_id, row["source_evidence_id"]))
        if (source["source_evidence_id"] is not None or source["kind"] != "returned_result"
                or row["material_id"] is not None or row["source_member"] not in {"summary", "artifact"}):
            raise MigrationRefused("source_evidence")
        envelope = _evidence(db, run_id, source["evidence_id"],
                             owner=source["owner_id"], kind="returned_result")
        if not isinstance(envelope, dict) or row["source_member"] not in envelope:
            raise MigrationRefused("source_evidence")
        value = envelope[row["source_member"]]
        body = wire_json(value)
        if len(body) != row["value_bytes"] or hashlib.sha256(body).hexdigest() != row["value_digest"]:
            raise MigrationRefused("source_evidence")
        return value
    if row["source_member"] is not None or row["material_id"] != row["value_digest"]:
        raise MigrationRefused("source_evidence")
    material = _one(db, "SELECT body,byte_length FROM work_evidence_materials WHERE material_id=?",
                    (row["material_id"],))
    body = material["body"]
    if (not isinstance(body, str) or material["byte_length"] != row["value_bytes"]
            or len(body.encode()) != row["value_bytes"]
            or hashlib.sha256(body.encode()).hexdigest() != row["value_digest"]):
        raise MigrationRefused("source_evidence")
    try:
        return json.loads(body)
    except ValueError as error:
        raise MigrationRefused("source_evidence") from error


def _read_profile(path: Path) -> tuple[dict[str, Any], str]:
    metadata = path.lstat()
    if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1 or metadata.st_size > 4 * 1024 * 1024:
        raise MigrationRefused("source_profile_file")
    original = path.read_bytes()
    if path.read_bytes() != original:
        raise MigrationRefused("source_profile_changed")
    try:
        document = json.loads(original)
    except (UnicodeError, ValueError) as error:
        raise MigrationRefused("source_profile") from error
    if not isinstance(document, dict):
        raise MigrationRefused("source_profile")
    return document, hashlib.sha256(original).hexdigest()




_ReturnedFacts = TypeVar('_ReturnedFacts')


def _read_returned_source(path: Path, profile: Mapping[str, Any], profile_sha: str,
                 source_sha: str, emit: Callable[..., _ReturnedFacts], *, original: bool) -> _ReturnedFacts:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        counts = {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not original and any(count and name not in _ACTIVE_TABLES for name, count in counts.items()):
            raise MigrationRefused("unsupported_source_relations")
        run = _one(db, "SELECT * FROM runs")
        run_id = run["run_id"]
        read_v6_host_exit(db, run_id, required=original)
        root = _one(db, "SELECT * FROM run_root WHERE run_id=?", (run_id,))
        layout = _one(db, "SELECT * FROM work_job_layout WHERE run_id=?", (run_id,))
        job = _one(db, "SELECT * FROM work_jobs WHERE run_id=? AND job_id=?",
                   (run_id, layout["active_job_id"]))
        ticket = job["value_ticket_number"]
        if original:
            # Current obligations need their own mapping; unrelated historical
            # rows, timing segments and diagnostics remain read-only records.
            for table in ('work_job_publication_commit_intent', 'work_job_ticket_write_intent',
                          'work_job_write_intent', 'work_job_run_branch_write_intent',
                          'work_job_integration_publication_intent', 'work_job_merge_intent',
                          'work_job_parent_close_intent', 'work_job_ticket_close_intent'):
                if counts.get(table, 0) and db.execute(
                        f'SELECT 1 FROM "{table}" WHERE run_id=? AND job_id=? LIMIT 1',
                        (run_id, job['job_id'])).fetchone():
                    raise MigrationRefused('unsupported_source_write_authority')
        pending_role = {"developing": Role.DEVELOPMENT, "reviewing": Role.REVIEW,
                        "accepted": Role.PUBLICATION}.get(job["stage"])
        if pending_role is None:
            raise MigrationRefused("unsupported_source_stage")
        count = {Role.DEVELOPMENT: 1, Role.REVIEW: 2, Role.PUBLICATION: 3}[pending_role]
        exact = {"runs": 1, "run_root": 1, "run_condition_base": 1, "work_jobs": 2,
                 "work_job_layout": 1, "work_attempts": count, "work_attempt_current": count,
                 "work_attempt_returns": count, "work_invocations": count,
                 "work_invocation_history": count, "work_job_attempt_roots": 1,
                 "work_job_attempt_history": count - 1, "work_job_review_budget": 1,
                 "work_invocation_current": 2, "work_job_review_artifacts": int(count == 3),
                 "work_job_acceptance_record": int(count == 3),
                 "work_job_integration_resync": int(count == 3),
                 "work_job_candidate_commit_intent": int(count == 1),
                 "work_job_ci_recovery": 1}
        if any(counts.get(table) != expected for table, expected in exact.items()):
            raise MigrationRefused("unsupported_source_stage")
        if (run["status"] != "active" or root["value_language"] not in {"zh", "en"}
                or job["job_id"] != f"ticket:{ticket}:generation:1"
                or job["owner_kind"] != "ticket" or job["owner_key"] != str(ticket)
                or job["value_ticket_branch_initialized"] != 1
                or job["value_ticket_branch"] != f"agent-run/{run_id}/ticket-{ticket}"
                or job["value_base_sha"] is None
                or job["value_modification_attempts"] != int(count > 1)
                or job["value_validation_attempts"] != int(count > 1)
                or job["value_publication_attempts"] != (1 if count == 3 else None)
                or (job["value_candidate_sha"] is None) != (count == 1)):
            raise MigrationRefused("unsupported_source_stage")
        selected = _one(db, "SELECT * FROM work_jobs WHERE run_id=? AND job_id!=?",
                        (run_id, job["job_id"]))
        if selected["job_id"] != f"ticket:{ticket}:selection" or selected["stage"] is not None:
            raise MigrationRefused("unsupported_source_stage")
        if (any(row[field] is not None for row in (job, selected) for field in _RETURNED_WRITE_FIELDS)
                or db.execute("SELECT 1 FROM work_evidence WHERE run_id=? AND kind IN "
                              "('linked_branch_display','branch_publication_observation') LIMIT 1",
                              (run_id,)).fetchone() is not None):
            raise MigrationRefused("unsupported_source_job_facts")
        parent = _one(db, "SELECT * FROM run_parent_versions WHERE run_id=? AND version_id=?",
                      (run_id, root["value_parent_version_id"]))
        title = _evidence(db, run_id, parent["title_id"], owner=f"run:{run_id}", kind="parent.title")
        body = _evidence(db, run_id, parent["body_id"], owner=f"run:{run_id}", kind="parent.body")
        graph = _one(db, "SELECT g.revision,s.evidence_id FROM run_graph_snapshots s "
                     "JOIN run_graph_versions g ON g.run_id=s.run_id AND g.version_id=s.version_id "
                     "WHERE s.run_id=? AND s.snapshot_id=?",
                     (run_id, root["value_graph_snapshot_id"]))
        observation = _evidence(db, run_id, graph["evidence_id"], owner=f"run:{run_id}", kind="graph.observation")
        order = [row[0] for row in db.execute("SELECT ticket_number FROM run_graph_scope_order "
                                               "WHERE run_id=? AND snapshot_id=? ORDER BY position",
                                               (run_id, root["value_graph_snapshot_id"]))]
        if (not isinstance(title, str) or not isinstance(body, str) or not isinstance(observation, dict)
                or order != [ticket] or len(observation.get("tickets", [])) != 1):
            raise MigrationRefused("source_requirements")
        item = observation["tickets"][0]
        try:
            issue = Issue(ticket, item["title"], item["body"], item["state"],
                          frozenset(item["labels"]),
                          tuple(Blocker(edge["number"], edge["state"]) for edge in item["blocked_by"]))
            requirements = Requirements(ParentIssue(run["parent_number"], title, body,
                                                    tuple(order), True), (issue,))
        except (KeyError, TypeError, ValueError) as error:
            raise MigrationRefused("source_requirements") from error
        parent_revision = Kernel._fingerprint({"title": title, "body": body})
        graph_revision = Kernel._fingerprint({
            "tickets": order,
            "dependencies": {str(ticket): sorted(edge.number for edge in issue.blocked_by)},
        })
        if (parent["revision"] != parent_revision or graph["revision"] != graph_revision
                or root["value_accepted_parent_spec_revision"] != parent_revision
                or root["value_accepted_ticket_graph_revision"] != graph_revision
                or item["content_revision"] != Kernel._fingerprint({"title": issue.title, "body": issue.body})):
            raise MigrationRefused("source_revision")
        base = _one(db, "SELECT * FROM run_condition_base WHERE run_id=?", (run_id,))
        frozen_ids = {kind: root[field] for field, kind in (
            ("value_policy_snapshot", "policy_snapshot"),
            ("value_prompt_resources", "prompt_resources"),
            ("value_github_identity", "github_identity"),
            ("value_runner_github_identity", "runner_github_identity"))}
        frozen = {kind: _evidence(db, run_id, evidence_id, owner=f"run:{run_id}", kind=kind)
                  for kind, evidence_id in frozen_ids.items()}
        query = _one(db, "SELECT presence FROM run_query_roots WHERE run_id=?", (run_id,))
        if (query["presence"] != b"\x1a" or counts.get("run_notification_config", 0)):
            raise MigrationRefused("unsupported_source_notifications")
        if profile.get("run_id") != run_id or profile.get("profile_revision") != 1:
            raise MigrationRefused("source_profile")
        profiles = {"preset": profile.get("preset"), "profiles": profile.get("profiles")}
        roles = profiles["profiles"]
        if not isinstance(roles, dict) or set(roles) != {"development", "review", "publication"}:
            raise MigrationRefused("source_profile")
        try:
            policy = replace(_policy(frozen["policy_snapshot"]),
                             development_profile=Profile(roles["development"]["model"], roles["development"]["reasoning_effort"]),
                             review_profile=Profile(roles["review"]["model"], roles["review"]["reasoning_effort"]),
                             publication_profile=Profile(roles["publication"]["model"], roles["publication"]["reasoning_effort"]))
        except (KeyError, TypeError, ValueError) as error:
            raise MigrationRefused("source_profile") from error
        inputs = CreationInputs(profiles, frozen["prompt_resources"], frozen["github_identity"],
                                frozen["runner_github_identity"], {"enabled": False},
                                {"source": "v6-offline-conversion", "source_sha256": source_sha,
                                 "frozen_evidence_ids": frozen_ids, "profile_sha256": profile_sha,
                                 "profiles": "old-profile-store", "notifications": "old-not-configured"})
        bindings = profile.get("bindings")
        if not isinstance(bindings, list) or len(bindings) != min(count, 2):
            raise MigrationRefused("source_profile_bindings")
        by_thread = {row.get("thread_id"): row for row in bindings if isinstance(row, dict)}
        expected_bindings = {"development"} if count == 1 else {"development", "review"}
        if len(by_thread) != len(bindings) or {row.get("role") for row in bindings} != expected_bindings:
            raise MigrationRefused("source_profile_bindings")
        roots = _one(db, "SELECT * FROM work_job_attempt_roots WHERE run_id=? AND job_id=?",
                     (run_id, job["job_id"]))
        history = db.execute("SELECT * FROM work_job_attempt_history WHERE run_id=? AND job_id=? ORDER BY position",
                             (run_id, job["job_id"])).fetchall()
        inv_history = db.execute("SELECT * FROM work_invocation_history WHERE run_id=? ORDER BY position",
                                 (run_id,)).fetchall()
        active_invocations = db.execute("SELECT * FROM work_invocation_current WHERE run_id=?",
                                        (run_id,)).fetchall()
        if ([row["position"] for row in history] != list(range(count - 1))
                or [row["position"] for row in inv_history] != list(range(count))
                or roots["pending_present"] != 1 or roots["history_count"] != count - 1
                or {row["started_at"] for row in active_invocations} != {inv_history[-1]["started_at"]}
                or {row["reference_kind"] for row in active_invocations} != {"active", "ticket"}):
            raise MigrationRefused("unsupported_source_attempt_history")
        ordered_ids = [row["attempt_id"] for row in history] + [roots["pending_attempt_id"]]
        if len(set(ordered_ids)) != count:
            raise MigrationRefused("source_attempt_identity")
        calls: list[_Call] = []
        previous_end = 0.0
        for index, (attempt_id, witness) in enumerate(zip(ordered_ids, inv_history, strict=True)):
            role = (Role.DEVELOPMENT, Role.REVIEW, Role.PUBLICATION)[index]
            attempt = _one(db, "SELECT * FROM work_attempts WHERE run_id=? AND attempt_id=?",
                           (run_id, attempt_id))
            current = _one(db, "SELECT * FROM work_attempt_current WHERE run_id=? AND attempt_id=?",
                           (run_id, attempt_id))
            version = _one(db, "SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?",
                           (run_id, attempt_id, current["version_id"]))
            returned = _one(db, "SELECT * FROM work_attempt_returns WHERE run_id=? AND attempt_id=?",
                            (run_id, attempt_id))
            invocation = _one(db, "SELECT * FROM work_invocations WHERE run_id=? AND work_subject=? AND started_at=?",
                              (run_id, f"ticket:{ticket}", witness["started_at"]))
            bound = _one(db, "SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?",
                         (run_id, attempt_id, invocation["attempt_version_id"]))
            if (attempt["job_id"] != job["job_id"] or attempt["work_subject"] != f"ticket:{ticket}"
                    or attempt["generation"] != 1 or attempt["ordinal"] != 1
                    or attempt["role"] != ("development", "reviewer", "publication")[index]
                    or version["status"] != ("pending" if index == count - 1 else "completed")
                    or (index == 0 and count > 1 and version["outcome"] != "candidate")
                    or (index == 1 and count == 3 and (version["outcome"] != "acceptance_artifact"
                                                         or version["budget_consumed"] != 1))
                    or version["returned_result_evidence_id"] != (returned["evidence_id"] if index == count - 1 else None)
                    or (index < count - 1 and history[index]["version_id"] != current["version_id"])
                    or invocation["attempt_id"] != attempt_id
                    or invocation["raw_result_evidence_id"] != returned["evidence_id"]
                    or invocation["status"] != "completed" or invocation["ended_at"] is None
                    or bound["status"] != "pending" or bound["returned_result_evidence_id"] is not None
                    or invocation["attempt_version_id"] == current["version_id"]
                    or (role == Role.REVIEW
                        and (bound["review_context_evidence_id"] is None
                             or bound["review_context_evidence_id"] != version["review_context_evidence_id"]))
                    or invocation["error"] is not None or invocation["execution_interrupted"] != 0
                    or invocation["recovery_waiting"] != 0 or invocation["return_code"] is not None
                    or invocation["execution_segments_null"] != 0
                    or invocation["profile_revision"] != 1):
                raise MigrationRefused("unsupported_source_invocation")
            started = _created_at_epoch(invocation["started_at"])
            ended = _created_at_epoch(invocation["ended_at"])
            if not previous_end <= started <= ended:
                raise MigrationRefused("source_invocation_chronology")
            previous_end = ended
            thread_id = invocation["reported_thread_id"]
            binding = by_thread.get(thread_id)
            expected_role = "review" if role == Role.REVIEW else "development"
            if (binding is None or binding.get("role") != expected_role
                    or binding.get("profile_revision") != 1
                    or binding.get("binding_id") != invocation["binding_id"]
                    or binding.get("model") != invocation["model"]
                    or binding.get("reasoning_effort") != invocation["reasoning_effort"]
                    or invocation["binding_role"] != expected_role
                    or invocation["profile_role"] != expected_role
                    or invocation["model"] != roles[expected_role]["model"]
                    or invocation["reasoning_effort"] != roles[expected_role]["reasoning_effort"]):
                raise MigrationRefused("source_profile_bindings")
            raw = _evidence(db, run_id, returned["evidence_id"],
                            owner=f"attempt:{attempt_id}", kind="returned_result")
            wire_publication = (original and role == Role.PUBLICATION and isinstance(raw, dict)
                                and raw.get("kind") == "publication_wire" and set(raw) == {"kind", "artifact"})
            # Original publication_wire deliberately has no top-level Thread.
            # The completed Invocation and its requested/reported binding above
            # remain the authority; preserve the envelope without adding fields.
            if not isinstance(raw, dict) or not wire_publication and raw.get("thread_id") != thread_id:
                raise MigrationRefused("source_returned_result")
            if role == Role.DEVELOPMENT:
                if raw.get("kind") != "development" or not isinstance(raw.get("summary"), str):
                    raise MigrationRefused("source_returned_result")
                value: Any = {"result_kind": "development", "summary": raw["summary"], "human_blockers": None}
            else:
                if not wire_publication and raw.get("kind") != ("review" if role == Role.REVIEW else "publication"):
                    raise MigrationRefused("source_returned_result")
                value = raw.get("artifact")
            try:
                normalized = artifact(role, wire_json(value))
                if wire_publication and normalized.get("result_kind") != "publication":
                    raise MigrationRefused("unsupported_source_artifact")
            except (KernelError, TypeError, ValueError) as error:
                raise MigrationRefused("unsupported_source_artifact") from error
            calls.append(_Call(attempt_id, role, attempt["ordinal"], thread_id,
                               dict(invocation), dict(bound), dict(version), raw,
                               returned["evidence_id"], normalized))
        if (count == 3 and (calls[2].invocation["requested_thread_id"] != calls[0].thread_id
                or calls[2].thread_id != calls[0].thread_id)):
            raise MigrationRefused("source_publication_thread")
        if not original and (counts.get("work_invocation_segments", 0) or counts.get("work_invocation_wait_intervals", 0)):
            raise MigrationRefused("unsupported_source_process")
        budget = _one(db, "SELECT * FROM work_job_review_budget WHERE run_id=? AND job_id=?",
                      (run_id, job["job_id"]))
        if (budget["value_window"] != 1 or budget["value_development_attempts"] != 1
                or budget["value_reviewer_invocations"] != int(count == 3)
                or budget["value_final_ci_fix_used"] != 0):
            raise MigrationRefused("unsupported_source_budget")
        ci = _one(db, "SELECT * FROM work_job_ci_recovery WHERE run_id=? AND job_id=?", (run_id, job['job_id']))
        if (ci['value_window'] != 1 or any(ci[field] != 0 for field in (
                'value_developments_used','value_repair_pending','value_review_required','value_reviewer_invocations'))
                or ci['value_failure_head'] is not None or ci['value_previous_publication_authorization'] is not None):
            raise MigrationRefused('unsupported_source_ci_history')
        candidate_intent: dict[str, Any] | None = None
        if count == 1:
            intent = _one(db, "SELECT * FROM work_job_candidate_commit_intent WHERE run_id=? AND job_id=?",
                          (run_id, job["job_id"]))
            if (intent["value_kind"] != "candidate_commit" or intent["value_attempt"] != 1
                    or intent["value_expected_head"] != base["value_sha"]
                    or not intent["value_token"]):
                raise MigrationRefused("unsupported_source_candidate_intent")
            candidate_intent = _wire_row(dict(intent))
        integration_target: str | None = None
        candidate_tree: str | None = None
        resync_window = 1
        if count == 3:
            review = _one(db, "SELECT * FROM work_job_review_artifacts WHERE run_id=? AND job_id=?",
                          (run_id, job["job_id"]))
            accepted = _one(db, "SELECT * FROM work_job_acceptance_record WHERE run_id=? AND job_id=?",
                            (run_id, job["job_id"]))
            accepted_artifact = _one(db, "SELECT evidence_id FROM work_evidence WHERE run_id=? AND "
                                     "kind='acceptance_record.artifact'", (run_id,))[0]
            if (review["candidate_sha"] != job["value_candidate_sha"]
                    or review["reviewed_base_sha"] != base["value_sha"]
                    or review["reviewer_thread_id"] != calls[1].thread_id
                    or review["artifact_id"] != job["value_acceptance_artifact"]
                    or accepted["value_acceptance_scope"] != "change_job"
                    or accepted["value_artifact"] != accepted_artifact
                    or accepted["value_reviewed_base_sha"] != base["value_sha"]
                    or accepted["value_reviewed_candidate_sha"] != job["value_candidate_sha"]
                    or accepted["value_reviewed_candidate_tree"] is None
                    or accepted["value_reviewer_thread_id"] != calls[1].thread_id):
                raise MigrationRefused("source_acceptance")
            integration = _evidence(db, run_id, job["value_ticket_integration"],
                                    owner=f"job:{job['job_id']}", kind="ticket_integration")
            if (not isinstance(integration, dict)
                    or integration.get("base_sha") != base["value_sha"]
                    or integration.get("source_sha") != base["value_sha"]
                    or integration.get("candidate_sha") != job["value_candidate_sha"]
                    or integration.get("head_sha") != job["value_candidate_sha"]
                    or integration.get("candidate_tree") != integration.get("head_tree")
                    or accepted["value_reviewed_candidate_tree"] != integration.get("candidate_tree")
                    or integration.get("target_sha") != base["value_sha"]
                    or integration.get("conflict_paths") != []):
                raise MigrationRefused("source_integration")
            resync = _one(db, "SELECT * FROM work_job_integration_resync WHERE run_id=? AND job_id=?",
                          (run_id, job["job_id"]))
            if (resync["value_generation"] != 1 or resync["value_window"] != 1
                    or resync["value_used"] != 0 or resync["value_limit"] != policy.resync_limit
                    or resync["value_open"] != 0 or resync["value_source_sha"] != base["value_sha"]
                    or resync["value_target_sha"] != integration["target_sha"]):
                raise MigrationRefused("source_resync")
            integration_target = integration["target_sha"]
            candidate_tree = integration["candidate_tree"]
            resync_window = resync["value_window"]
        elif count == 2:
            review_context = _evidence(db, run_id, calls[-1].current_version["review_context_evidence_id"],
                                       owner=f"attempt:{calls[-1].attempt_id}", kind="review_context")
            identity = (review_context.get("facts", {}).get("current_review_identity")
                        if isinstance(review_context, dict) else None)
            if (not isinstance(identity, dict) or identity.get("reviewed_base_sha") != base["value_sha"]
                    or identity.get("reviewed_candidate_sha") != job["value_candidate_sha"]
                    or not isinstance(identity.get("reviewed_candidate_tree"), str)):
                raise MigrationRefused("source_review_identity")
            candidate_tree = identity["reviewed_candidate_tree"]
        evidence = () if original else tuple({"evidence_id": row["evidence_id"], "owner_id": row["owner_id"],
                          "kind": row["kind"], "version": row["version"],
                          "source_evidence_id": row["source_evidence_id"],
                          "source_member": row["source_member"],
                          "value": _evidence(db, run_id, row["evidence_id"],
                                             owner=row["owner_id"], kind=row["kind"])}
                         for row in db.execute("SELECT * FROM work_evidence WHERE run_id=? ORDER BY evidence_id", (run_id,)))
        if not original and len(evidence) != counts["work_evidence"]:
            raise MigrationRefused("source_evidence")
        timeline = () if original else tuple(dict(row) for row in db.execute(
            "SELECT e.* FROM run_timeline_slots s JOIN run_timeline_events e "
            "ON e.run_id=s.run_id AND e.event_id=s.event_id "
            "WHERE s.run_id=? AND s.stream='timeline' ORDER BY s.position", (run_id,)))
        if not original and len(timeline) != counts["run_timeline_events"]:
            raise MigrationRefused("source_timeline")
        return emit(db, run_id, run["repository"], run["parent_number"], parent_revision,
                       graph_revision, _created_at_epoch(run["created_at"]), root["value_language"],
                       requirements, policy, inputs, root["value_run_branch"], base["value_branch"],
                       base["value_sha"], ticket, f"refs/heads/{job['value_ticket_branch']}",
                       job["value_candidate_sha"], candidate_tree,
                       integration_target, resync_window, pending_role, candidate_intent,
                       tuple(calls), tuple(bindings), evidence, timeline)
    except sqlite3.Error as error:
        raise MigrationRefused("source_corrupt") from error
    finally:
        db.close()


def _read_active(path: Path, profile: Mapping[str, Any], profile_sha: str,
                 source_sha: str, *, original: bool = False) -> _Active:
    # Retained only for the old writer until unique installation is connected.
    def legacy(db: sqlite3.Connection, *values: Any) -> _Active:
        return _Active(*values)
    return _read_returned_source(path, profile, profile_sha, source_sha, legacy, original=original)


def _build_returned_candidate(
    snapshot: SourceSnapshot, facts: _Active, candidate: Path, *, repository_root: Path,
    checkout: Path, final_checkout: Path, profile_sha: str, remote_source_sha: str,
    remote_target_sha: str, archive_ref: str | None, original_mode: bool = False,
) -> Kernel:
    """Map proved role facts; only Kernel later consumes the returned artifact."""
    subject = f"ticket:{facts.ticket}"
    common = repository_root / ".git"
    spec = RunSpec(facts.run_id, facts.repository, facts.parent_number, facts.parent_revision,
                   facts.graph_revision,
                   (Delivery(subject, DeliveryKind.TICKET, facts.ticket, "source-verified",
                             Commit(facts.base_sha, _git(repository_root, "rev-parse", f"{facts.base_sha}^{{tree}}")),
                             facts.source_ref, f"refs/heads/{facts.run_branch}",
                             checkout=str(checkout), owned_root=str(checkout.parent),
                             common_git_dir=str(common)),
                    Delivery(f"final:{facts.run_id}", DeliveryKind.FINAL_RUN, facts.parent_number,
                             facts.parent_revision, Commit(facts.base_sha,
                             _git(repository_root, "rev-parse", f"{facts.base_sha}^{{tree}}")),
                             f"refs/heads/{facts.run_branch}", f"refs/heads/{facts.base_branch}",
                             checkout=str(final_checkout), owned_root=str(final_checkout.parent),
                             common_git_dir=str(common))), facts.policy, facts.language,
                   str(repository_root), facts.inputs)
    kernel = Kernel.create(candidate, spec, requirements=facts.requirements)
    phase = {Role.DEVELOPMENT: "development", Role.REVIEW: "review",
             Role.PUBLICATION: "publish"}[facts.pending_role]
    with kernel.storage.transaction() as tx:
        native = tx.one("SELECT revision,completion_revision FROM subjects WHERE id=?", (subject,))
        tx.execute("UPDATE runs SET created_at=?", (facts.created_at,))
        tx.execute("UPDATE subjects SET phase=?,current_attempt=?,review_attempt=?,"
                   "development_thread=?,candidate_sha=?,candidate_tree=?,workspace_sha=?,"
                   "expected_tree=?,integrated_sha=?,integrated_tree=?,integration_target=?,gate_mode=? "
                   "WHERE id=?", (phase, facts.calls[-1].attempt_id,
                                  facts.calls[1].attempt_id if facts.pending_role == Role.PUBLICATION else None,
                                  facts.calls[0].thread_id, facts.candidate_sha, facts.candidate_tree,
                                  facts.candidate_sha or facts.base_sha, facts.candidate_tree,
                                  facts.candidate_sha if facts.pending_role == Role.PUBLICATION else None,
                                  facts.candidate_tree if facts.pending_role == Role.PUBLICATION else None,
                                  facts.integration_target,
                                  "review" if facts.pending_role == Role.PUBLICATION else None, subject))
        tx.execute("UPDATE budgets SET developments_used=1,reviews_used=? "
                   "WHERE subject=? AND purpose='requirements' AND window=1",
                   (int(facts.pending_role == Role.PUBLICATION), subject))
        if facts.pending_role == Role.PUBLICATION:
            tx.execute("INSERT INTO integration_snapshots VALUES (?,?,?,?,?,?,?,0)",
                       (subject, facts.resync_window, 1, facts.source_ref,
                        f"refs/heads/{facts.run_branch}", facts.base_sha, facts.integration_target))
        tx.execute("DELETE FROM history")
        for event in facts.timeline:
            at = _created_at_epoch(event["value_at"])
            payload = {"event_id": event["event_id"], "presence_hex": event["presence"].hex(),
                       "values": {key: value for key, value in event.items()
                                  if key.startswith("value_") and value is not None}}
            material = tx.put(subject, "v6_timeline_event", wire_json(payload))
            tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) "
                       "VALUES (?,?,?,?,1,?)", (subject, f"v6_{event['value_kind']}",
                                               event["event_id"], material, at))
        for entry in facts.evidence:
            tx.put(subject, "v6_evidence", wire_json(entry))
        if facts.candidate_intent is not None:
            tx.put(subject, "v6_candidate_commit_intent", wire_json(facts.candidate_intent))
        for binding in facts.bindings:
            tx.execute("INSERT INTO threads(id,subject,role,profile_revision,backend_id) VALUES (?,?,?,1,?)",
                       (binding["thread_id"], subject, binding["role"], binding["thread_id"]))
            tx.put(subject, "v6_thread_binding", wire_json(binding))
        for index, call in enumerate(facts.calls, start=1):
            context_witness: dict[str, Any] = {
                "old_attempt": call.attempt_id,
                "old_role_ordinal": call.old_ordinal,
                "input_fingerprint": call.invocation["input_fingerprint"],
                "bound_version": _wire_row(call.bound_version),
                "native_authority": {
                    "kind": "continuation-boundary",
                    "parent_revision": facts.parent_revision,
                    "graph_revision": facts.graph_revision,
                    "revision": native["revision"],
                    "completion_revision": native["completion_revision"],
                    "base_sha": facts.base_sha,
                    "candidate_sha": facts.candidate_sha,
                    "candidate_tree": facts.candidate_tree,
                },
            }
            if call.role == Role.REVIEW:
                # The old review_context evidence binds the exact candidate
                # but contains no reviewer checkout path.  Preserve that
                # absence instead of claiming the Ticket checkout was it.
                # Successor Publication consumes the native review identity;
                # these revisions identify the verified continuation boundary,
                # while the original bound input remains above unchanged.
                context_witness.update(
                    checkout=None, base_sha=facts.base_sha,
                    reviewed_commit=facts.candidate_sha, reviewed_tree=facts.candidate_tree,
                    revision=native["revision"], parent_revision=facts.parent_revision,
                    graph_revision=facts.graph_revision,
                    completion_revision=native["completion_revision"],
                )
            context = tx.put(subject, "v6_attempt_input_witness", wire_json(context_witness))
            result = tx.put(subject, "v6_normalized_artifact", wire_json(call.native_artifact))
            tx.execute("""INSERT INTO attempts(
                id,subject,generation,ordinal,role,purpose,window,parent_revision,graph_revision,
                revision,base_sha,candidate_sha,candidate_tree,expected_tree,completion_revision,
                context_id,current_invocation,result_id,status,accounted,outcome)
                VALUES (?,?,1,?,?,'requirements',1,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",
                       (call.attempt_id, subject, index, call.role.value, facts.parent_revision,
                        facts.graph_revision, native["revision"],
                        facts.base_sha, facts.candidate_sha, facts.candidate_tree, facts.candidate_tree,
                        native["completion_revision"], context, result,
                        "returned" if index == len(facts.calls) else "consumed",
                        1 if index == 2 and facts.pending_role == Role.PUBLICATION else 0,
                        None if index == len(facts.calls) else "pass" if index == 2 else "development"))
            original = {"attempt_id": call.attempt_id, "old_role_ordinal": call.old_ordinal,
                        "thread_id": call.thread_id, "raw_evidence_id": call.raw_evidence_id,
                        "returned": call.returned, "invocation": _wire_row(call.invocation),
                        "bound_version": _wire_row(call.bound_version),
                        "current_version": _wire_row(call.current_version)}
            material = tx.put(subject, "v6_imported_invocation", wire_json(original))
            original_identity = ":".join((facts.run_id, subject, call.invocation["started_at"]))
            tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) "
                       "VALUES (?,?,?,?,1,?)", (subject, "imported_invocation", original_identity,
                                               material, _created_at_epoch(call.invocation["ended_at"])))
        tx.put(subject, "v6_import_identity", wire_json({
            "source_sha256": snapshot.sha256, "profile_sha256": profile_sha,
            "source_ref": facts.source_ref, "candidate_sha": facts.candidate_sha,
            "candidate_tree": facts.candidate_tree, "remote_source_sha": remote_source_sha,
            "remote_target_sha": remote_target_sha,
            "archive_ref": archive_ref if facts.candidate_sha is not None else None,
            "checkout": str(checkout),
            "mirror_conversion": "candidate-to-remote-base" if facts.candidate_sha is not None and not original_mode else "none",
            "source_backend": "original-reaped-local" if original_mode else "completed-synchronous-oracle",
        }))
    return kernel


def convert_returned_role_v6(
    snapshot: SourceSnapshot, destination: Path, *, repository_root: Path,
    checkout: Path, final_checkout: Path, profile_snapshot: Path, github_snapshot: Path,
    observation_snapshot: Path, remote_source_sha: str, remote_target_sha: str,
    checkout_archive: Path | None = None,
) -> Kernel:
    """Import a sealed synchronous-oracle return, never a live Host invocation.

    The observation must come from a completed old-process oracle whose
    producer has exited.  This limited API is not active-Host admission.
    """
    destination = destination.absolute()
    repository_root, checkout, final_checkout = (repository_root.resolve(), checkout.resolve(),
                                                  final_checkout.resolve())
    if (repository_root == destination.parent.resolve()
            or not repository_root.is_relative_to(destination.parent.resolve())
            or snapshot.source.resolve().is_relative_to(repository_root)
            or snapshot.path.resolve().is_relative_to(repository_root)):
        raise MigrationRefused("copied_repository_required")
    if destination.exists() or destination.is_symlink():
        raise MigrationRefused("destination_exists")
    profile, profile_sha = _read_profile(profile_snapshot)
    try:
        observation_bytes = observation_snapshot.read_bytes()
        if len(observation_bytes) > 4 * 1024 * 1024:
            raise MigrationRefused("source_observation")
        observed = json.loads(observation_bytes)
    except (OSError, UnicodeError, ValueError) as error:
        raise MigrationRefused("source_observation") from error
    observation_sha = hashlib.sha256(observation_bytes).hexdigest()
    if (not isinstance(observed, dict)
            or observed.get("backend") != "synchronous scripted Agent; completed event and returned result, no OS process proof"
            or observed.get("source_sha256") != snapshot.sha256
            or observed.get("profile_sha256") != profile_sha):
        raise MigrationRefused("source_quiescence_unproved")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".v6-active-") as directory:
        copied = capture_v6(snapshot.path, Path(directory) / "source.sqlite3")
        if ((copied.sha256, copied.source_protocol, copied.schema_sha256, copied.table_rows)
                != (snapshot.sha256, snapshot.source_protocol, snapshot.schema_sha256, snapshot.table_rows)):
            raise MigrationRefused("snapshot_changed")
        facts = _read_active(copied.path, profile, profile_sha, snapshot.sha256)
        _require_no_v6_notification_journal(snapshot, facts.run_id)
        try:
            github_bytes = github_snapshot.read_bytes()
            github = json.loads(github_bytes)
        except (OSError, UnicodeError, ValueError) as error:
            raise MigrationRefused("source_github_observation") from error
        branches = github.get("delivery", {}).get("published_branches") if isinstance(github, dict) else None
        if (len(github_bytes) > 4 * 1024 * 1024 or not isinstance(branches, dict)
                or hashlib.sha256(github_bytes).hexdigest() != observed.get("fixture_sha256")
                or github.get("repository") != facts.repository
                or github.get("default_branch") != facts.base_branch
                or github.get("default_head_sha") != remote_target_sha
                or branches.get(facts.run_branch) != facts.base_sha
                or branches.get(facts.source_ref.removeprefix("refs/heads/")) != remote_source_sha):
            raise MigrationRefused("source_github_observation")
        completed = observed.get("completed_invocation")
        pending = observed.get("pending_attempt")
        old_phase = {Role.DEVELOPMENT: "developing", Role.REVIEW: "reviewing",
                     Role.PUBLICATION: "accepted"}[facts.pending_role]
        if (observed.get("run_id") != facts.run_id or not isinstance(completed, dict)
                or not isinstance(pending, dict)
                or not isinstance(completed.get("semantic_attempt"), dict)
                or (facts.pending_role != Role.PUBLICATION and observed.get("job_phase") != old_phase)
                or (facts.pending_role != Role.PUBLICATION
                    and observed.get("role") not in {facts.pending_role.value,
                                                      "reviewer" if facts.pending_role == Role.REVIEW else facts.pending_role.value})
                or pending.get("attempt_id") != facts.calls[-1].attempt_id
                or completed["semantic_attempt"].get("attempt_id") != facts.calls[-1].attempt_id
                or completed.get("started_at") != facts.calls[-1].invocation["started_at"]
                or completed.get("ended_at") != facts.calls[-1].invocation["ended_at"]
                or completed.get("status") != "completed"
                or completed.get("execution_segments") != []):
            raise MigrationRefused("source_quiescence_unproved")
        if _git(repository_root, "rev-parse", "--show-toplevel") != str(repository_root):
            raise MigrationRefused("source_git")
        for ref in (facts.source_ref, f"refs/heads/{facts.run_branch}", f"refs/heads/{facts.base_branch}"):
            _git(repository_root, "check-ref-format", ref)
        if (remote_source_sha != facts.base_sha
                or remote_target_sha != (facts.integration_target or facts.base_sha)
                or _git(repository_root, "rev-parse", f"refs/heads/{facts.run_branch}^{{commit}}") != facts.base_sha
                or _git(repository_root, "rev-parse", f"refs/heads/{facts.base_branch}^{{commit}}") != facts.base_sha
                or _git(repository_root, "rev-parse", f"{facts.source_ref}^{{commit}}") != (facts.candidate_sha or facts.base_sha)
                or (facts.candidate_sha is not None and
                    _git(repository_root, "rev-parse", f"{facts.candidate_sha}^{{tree}}") != facts.candidate_tree)):
            raise MigrationRefused("source_git")
        checkout_head = facts.candidate_sha or facts.base_sha
        if (not checkout.is_dir() or _git(checkout, "rev-parse", "--show-toplevel") != str(checkout)
                or _git(checkout, "rev-parse", "HEAD") != checkout_head
                or checkout == repository_root):
            raise MigrationRefused("source_checkout")
        status = _git(checkout, "status", "--porcelain").splitlines()
        if (status != observed.get("ticket_checkout_status", [] if facts.pending_role == Role.PUBLICATION else None)
                or observed.get("ticket_checkout_head") != checkout_head):
            raise MigrationRefused("source_checkout")
        if checkout_archive is not None:
            archive_sha = observed.get("checkout_archive_sha256")
            if not isinstance(archive_sha, str):
                raise MigrationRefused("source_checkout_archive")
            _verify_checkout_archive(checkout_archive, checkout, archive_sha)
        elif facts.pending_role == Role.DEVELOPMENT:
            raise MigrationRefused("source_checkout_archive_required")
        if facts.pending_role == Role.DEVELOPMENT:
            if (not status or _git(checkout, "diff", "--cached", "--name-only")
                    or facts.candidate_sha is not None):
                raise MigrationRefused("source_checkout")
        elif status:
            raise MigrationRefused("source_checkout")
        common_value = Path(_git(checkout, "rev-parse", "--git-common-dir") or "")
        common = (common_value if common_value.is_absolute() else checkout / common_value).resolve()
        if common != (repository_root / ".git").resolve():
            raise MigrationRefused("source_checkout")
        if final_checkout.exists() or final_checkout == checkout or final_checkout == repository_root:
            raise MigrationRefused("new_final_checkout")
        archive_ref = f"refs/agent-run-kernel-migration/{facts.run_id}/ticket-{facts.ticket}"
        _git(repository_root, "check-ref-format", archive_ref)
        if facts.candidate_sha is not None and _git(repository_root, "for-each-ref", "--format=%(refname)", archive_ref):
            raise MigrationRefused("migration_archive_exists")
        subject = f"ticket:{facts.ticket}"
        candidate = Path(directory) / "kernel.sqlite3"
        kernel = _build_returned_candidate(
            snapshot, facts, candidate, repository_root=repository_root, checkout=checkout,
            final_checkout=final_checkout, profile_sha=profile_sha, remote_source_sha=remote_source_sha,
            remote_target_sha=remote_target_sha, archive_ref=archive_ref)
        phase = {Role.DEVELOPMENT: "development", Role.REVIEW: "review", Role.PUBLICATION: "publish"}[facts.pending_role]
        if (kernel.status(subject)["pending_attempt"] != facts.calls[-1].attempt_id
                or kernel.status(subject)["phase"] != phase):
            raise MigrationRefused("converted_state")
        if _read_profile(profile_snapshot)[1] != profile_sha:
            raise MigrationRefused("source_profile_changed")
        if hashlib.sha256(observation_snapshot.read_bytes()).hexdigest() != observation_sha:
            raise MigrationRefused("source_observation_changed")
        if hashlib.sha256(github_snapshot.read_bytes()).hexdigest() != observed["fixture_sha256"]:
            raise MigrationRefused("source_github_observation_changed")
        if checkout_archive is not None:
            _verify_checkout_archive(checkout_archive, checkout, observed["checkout_archive_sha256"])
        # This is only the caller's isolated copied repository.  Keep the old
        # candidate reachable before normalizing the local mirror to the
        # observed remote parent.  The old source and its ref are untouched.
        zeros = "0" * len(facts.base_sha)
        archived = normalized = linked = False
        try:
            if facts.candidate_sha is not None:
                _git(repository_root, "update-ref", archive_ref, facts.candidate_sha, zeros)
                archived = True
                _git(repository_root, "update-ref", facts.source_ref, remote_source_sha, facts.candidate_sha)
                normalized = True
            if ((facts.candidate_sha is not None and
                 _git(repository_root, "rev-parse", archive_ref) != facts.candidate_sha)
                    or _git(repository_root, "rev-parse", facts.source_ref) != remote_source_sha
                    or _git(checkout, "rev-parse", "HEAD") != checkout_head
                    or _git(checkout, "status", "--porcelain").splitlines() != status):
                raise MigrationRefused("mirror_conversion")
            if any(Path(str(candidate) + suffix).exists() and Path(str(candidate) + suffix).stat().st_size
                   for suffix in ("-wal", "-journal")):
                raise MigrationRefused("converted_sidecar")
            try:
                os.link(candidate, destination)
            except FileExistsError as error:
                raise MigrationRefused("destination_exists") from error
            linked = True
            parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except BaseException:
            if linked and destination.samefile(candidate):
                destination.unlink()
            try:
                if normalized and facts.candidate_sha is not None:
                    _git(repository_root, "update-ref", facts.source_ref,
                         facts.candidate_sha, remote_source_sha)
                if archived and facts.candidate_sha is not None:
                    _git(repository_root, "update-ref", "-d", archive_ref, facts.candidate_sha)
            except MigrationRefused as error:
                # No target database was activated.  The candidate remains
                # reachable in this disposable copy; reconstruct from the
                # immutable bundle instead of retrying this changed copy.
                raise MigrationRefused("copied_mirror_recovery_required") from error
            raise
    return Kernel(destination)


def _check_returned_external(proof: Mapping[str, Any], source_sha: str, *,
        requirements: Requirements, runner_identity: Mapping[str, Any], source_ref: str,
        base_sha: str, run_branch: str, base_branch: str) -> None:
    """Match actual requirements and exact remote refs; no pending write is inferred."""
    expected = {
        'parent': {'number': requirements.parent.number, 'title': requirements.parent.title,
                   'body': requirements.parent.body,
                   'sub_issue_numbers': list(requirements.parent.sub_issue_numbers),
                   'sub_issue_order_reliable': requirements.parent.sub_issue_order_reliable},
        'issues': [{'number': issue.number, 'title': issue.title, 'body': issue.body,
                    'state': issue.state, 'labels': sorted(issue.labels),
                    'blocked_by': [{'number': edge.number, 'state': edge.state} for edge in issue.blocked_by]}
                   for issue in requirements.issues],
    }
    if (proof.get('source_kind') != 'returned_role' or proof.get('source_sha256') != source_sha
            or proof.get('runner_identity') != runner_identity
            or proof.get('requirements') != expected
            or proof.get('remote_heads') != {source_ref: base_sha,
                'refs/heads/' + run_branch: base_sha,
                'refs/heads/' + base_branch: base_sha}):
        raise MigrationRefused('source_returned_external')


def _returned_external(facts: _Active, proof: Mapping[str, Any], source_sha: str) -> None:
    _check_returned_external(proof, source_sha, requirements=facts.requirements,
        runner_identity=facts.inputs.runner_identity, source_ref=facts.source_ref,
        base_sha=facts.base_sha, run_branch=facts.run_branch, base_branch=facts.base_branch)


def _returned_layout(facts: _Active, origin: Mapping[str, str], archive_sha: str) -> dict[str, Any]:
    managed = ManagedWorkspace.for_repository(facts.repository)
    checkout = managed.state_root / 'worktrees' / facts.run_id / f'ticket-{facts.ticket}'
    return {'version': 1, 'nonce': origin['nonce'], 'source_sha256': origin['source_sha256'],
            'source_uuid': origin['source_uuid'], 'repository_root': str(managed.repository_root),
            'checkout': str(checkout), 'owned_root': str(checkout.parent),
            'common_git_dir': str(managed.repository_root / '.git'), 'source_ref': facts.source_ref,
            'expected_oid': facts.candidate_sha or facts.base_sha,
            'before_head': 'ref: ' + facts.source_ref + '\n', 'checkout_archive_sha256': archive_sha}


def _verify_returned_git(snapshot: SourceSnapshot, destination: Path, archive: Path,
        archive_sha: str, index_sha: str, *, repository: str, run_id: str, ticket: int,
        source_ref: str, candidate_sha: str | None, candidate_tree: str | None,
        base_sha: str, run_branch: str, base_branch: str, detached: bool = False) -> None:
    """Keep the original clone, refs, dirty content and exact checkout identity."""
    managed = ManagedWorkspace.for_repository(repository)
    root = managed.repository_root
    checkout = managed.state_root / 'worktrees' / run_id / f'ticket-{ticket}'
    if (managed.open_path() != root or snapshot.source != managed.state_root / 'state.sqlite3'
            or destination != managed.state_root / run_id / 'kernel.sqlite3'
            or destination.parent.resolve() != destination.parent
            or root.is_symlink() or checkout.is_symlink() or checkout.resolve() != checkout):
        raise MigrationRefused('source_existing_resources')
    with closing(sqlite3.connect(snapshot.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        task = read_v6_host_exit(db, run_id, required=True)
        uuid = _one(db, 'SELECT value_checkout_identity FROM run_root WHERE run_id=?', (run_id,))[0]
        control = _one(db, 'SELECT run_state_dir FROM control_tasks')
        if task is None or task.workspace != root or control[0] != str(managed.state_root):
            raise MigrationRefused('source_existing_identity')
    marker = root / '.git' / 'agent-run-checkout-id'
    if (not isinstance(uuid, str) or marker.is_symlink() or not marker.is_file()
            or marker.stat().st_nlink != 1 or marker.read_bytes() != (uuid + '\n').encode('ascii')):
        raise MigrationRefused('source_existing_identity')
    git = LocalGit(root)
    for ref, oid in ((source_ref, candidate_sha or base_sha),
                     ('refs/heads/' + run_branch, base_sha),
                     ('refs/heads/' + base_branch, base_sha)):
        if (git.run('symbolic-ref', '-q', ref, check=False).returncode != 1
                or _git(root, 'rev-parse', '--verify', ref) != oid):
            raise MigrationRefused('source_existing_ref')
    expected_head = candidate_sha or base_sha
    shape = git.run('symbolic-ref', '-q', 'HEAD', cwd=checkout, check=False)
    if (not checkout.is_dir() or _git(root, 'rev-parse', 'HEAD') != base_sha
            or _git(root, 'status', '--porcelain')
            or _git(checkout, 'diff', '--cached', '--name-only')
            or _git(checkout, 'rev-parse', 'HEAD') != expected_head
            or _git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir') != str(root / '.git')
            or (shape.returncode == 0 and shape.stdout.strip() != source_ref)
            or shape.returncode not in ({0, 1} if detached else {0})):
        raise MigrationRefused('source_existing_checkout')
    worktrees = _git(root, 'worktree', 'list', '--porcelain').strip().split('\n\n')
    if (len(worktrees) != 2 or {block.splitlines()[0] for block in worktrees}
            != {'worktree ' + str(root), 'worktree ' + str(checkout)}):
        raise MigrationRefused('source_existing_worktrees')
    if candidate_sha is not None and (
            _git(root, 'rev-parse', candidate_sha + '^{tree}') != candidate_tree
            or _git(root, 'show', '-s', '--format=%P', candidate_sha) != base_sha):
        raise MigrationRefused('source_git_ancestry')
    _verify_checkout_archive(archive, checkout, archive_sha)
    sealed_index = archive.parent / 'checkout-index'
    index = Path(_git(checkout, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    if (index.is_symlink() or index.resolve() != index or not index.is_relative_to(root / '.git' / 'worktrees')
            or not index.is_file() or index.stat().st_nlink != 1 or index.stat().st_size > 32 * 1024 * 1024
            or sealed_index.is_symlink() or not sealed_index.is_file() or sealed_index.stat().st_nlink != 1
            or sealed_index.stat().st_size > 32 * 1024 * 1024
            or hashlib.sha256(sealed_index.read_bytes()).hexdigest() != index_sha
            or index.read_bytes() != sealed_index.read_bytes()):
        raise MigrationRefused('source_checkout_index')


def _existing_returned_git(snapshot: SourceSnapshot, facts: _Active, destination: Path,
                           archive: Path, archive_sha: str, index_sha: str, *, detached: bool = False) -> None:
    _verify_returned_git(snapshot, destination, archive, archive_sha, index_sha,
        repository=facts.repository, run_id=facts.run_id, ticket=facts.ticket, source_ref=facts.source_ref,
        candidate_sha=facts.candidate_sha, candidate_tree=facts.candidate_tree,
        base_sha=facts.base_sha, run_branch=facts.run_branch, base_branch=facts.base_branch, detached=detached)


def convert_original_returned_v6(snapshot: SourceSnapshot, destination: Path, *,
                                  profile_snapshot: Path, external_snapshot: Path,
                                  checkout_archive: Path, source_origin: Mapping[str, str]) -> Kernel:
    """Prepare original pending raw facts; no source ref, layout or authority is changed."""
    from .migration_completed import _read_document
    from .legacy_admission import read_source_fence, source_fence
    from .migration_layout import verify_layout
    origin = read_source_fence(source_fence(dict(source_origin)))
    profile, profile_sha = _read_profile(profile_snapshot)
    proof, proof_sha = _read_document(external_snapshot)
    facts = _read_active(snapshot.path, profile, profile_sha, snapshot.sha256, original=True)
    _require_no_v6_notification_journal(snapshot, facts.run_id)
    _returned_external(facts, proof, snapshot.sha256)
    archive_sha = proof.get('checkout_archive_sha256')
    index_sha = proof.get('checkout_index_sha256')
    if not isinstance(archive_sha, str) or not isinstance(index_sha, str):
        raise MigrationRefused('source_checkout_archive')
    managed = ManagedWorkspace.for_repository(facts.repository)
    layout = _returned_layout(facts, origin, archive_sha)
    _existing_returned_git(snapshot, facts, destination, checkout_archive, archive_sha, index_sha)
    verify_layout(layout, state='attached')
    if (destination.exists() or destination.is_symlink() or origin['source_db'] != str(snapshot.source)
            or origin['source_sha256'] != snapshot.sha256 or origin['run_id'] != facts.run_id
            or origin['target_db'] != str(destination)):
        raise MigrationRefused('source_original_identity')
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.v6-returned-original-') as directory:
        candidate = Path(directory) / 'kernel.sqlite3'
        kernel = _build_returned_candidate(snapshot, facts, candidate,
            repository_root=managed.repository_root, checkout=Path(layout['checkout']),
            final_checkout=managed.root / 'worktrees' / facts.run_id / 'run-publication',
            profile_sha=profile_sha, remote_source_sha=facts.base_sha,
            remote_target_sha=facts.base_sha, archive_ref=None, original_mode=True)
        subject = f'ticket:{facts.ticket}'
        _preserve_copy(snapshot, kernel, run_id=facts.run_id, subject=subject,
                       profile_document=profile_snapshot.read_bytes(), whole_source=True)
        with kernel.storage.transaction() as tx:
            body = {'source_sha256': snapshot.sha256, 'profile_sha256': profile_sha,
                    'external_readproof_sha256': proof_sha, 'resource_mode': 'existing-verified',
                    'original_layout': layout, 'checkout_index_sha256': index_sha,
                    'source_record': {'table': 'work_jobs', 'key': [facts.run_id, f'ticket:{facts.ticket}:generation:1']},
                    'run_ref': 'refs/heads/' + facts.run_branch,
                    'run_ref_ownership': {'source_ref': 'refs/heads/' + facts.run_branch, 'expected_oid': facts.base_sha,
                                          'source_record': {'table': 'run_root', 'key': [facts.run_id]}}}
            for kind, identity, payload in (
                    ('v6_returned_import', snapshot.sha256, body),
                    ('v6_original_cutover', origin['nonce'], origin),
                    ('v6_original_layout', origin['nonce'], layout),
                    ('v6_external_readproof', proof_sha, proof)):
                material = tx.put(subject, kind, wire_json(payload))
                tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) '
                           'VALUES (?,?,?,?,1,?)', (subject, kind, identity, material, kernel.clock()))
        _existing_returned_git(snapshot, facts, destination, checkout_archive, archive_sha, index_sha)
        if _read_profile(profile_snapshot)[1] != profile_sha or _read_document(external_snapshot)[1] != proof_sha:
            raise MigrationRefused('source_documents_changed')
        descriptor = os.open(candidate, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(candidate, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return Kernel(destination)


def _seal_returned_checkout(facts: _Active, archive: Path) -> tuple[str, str]:
    """Seal the canonical checkout using the common byte-preservation routine."""
    managed = ManagedWorkspace.for_repository(facts.repository)
    checkout = managed.state_root / 'worktrees' / facts.run_id / f'ticket-{facts.ticket}'
    return seal_checkout(checkout, managed.repository_root, archive)


def read_original_returned_import(snapshot: SourceSnapshot, *, profile_snapshot: Path,
        external_snapshot: Path, checkout_archive: Path) -> ImportedRunFacts:
    """Decode the existing original returned-role family; no writer DTO is built.

    This consumes sealed inspection evidence and reuses the exact existing
    SQL, external and physical resource validators. It neither installs Core
    nor grants an Invocation, continuation Opening, fence or Task authority.
    """
    from .migration_completed import _read_document
    validate_snapshot(snapshot)
    profile, profile_sha = _read_profile(profile_snapshot)
    profile_document = profile_snapshot.read_bytes()
    proof, proof_sha = _read_document(external_snapshot)
    external_document = external_snapshot.read_bytes()
    if (hashlib.sha256(profile_document).hexdigest() != profile_sha
            or hashlib.sha256(external_document).hexdigest() != proof_sha):
        raise MigrationRefused('source_documents_changed')

    def decode(db: sqlite3.Connection, run_id: str, repository: str, parent_number: int,
            parent_revision: str, graph_revision: str, created_at: float, language: str,
            requirements: Requirements, policy: Policy, inputs: CreationInputs,
            run_branch: str, base_branch: str, base_sha: str, ticket: int, source_ref: str,
            candidate_sha: str | None, candidate_tree: str | None, integration_target: str | None,
            resync_window: int, pending_role: Role, candidate_intent: dict[str, Any] | None,
            calls: tuple[_Call, ...], bindings: tuple[dict[str, Any], ...],
            evidence: tuple[dict[str, Any], ...], timeline: tuple[dict[str, Any], ...]) -> ImportedRunFacts:
        _require_no_v6_notification_journal(snapshot, run_id)
        _check_returned_external(proof, snapshot.sha256, requirements=requirements,
            runner_identity=inputs.runner_identity, source_ref=source_ref, base_sha=base_sha,
            run_branch=run_branch, base_branch=base_branch)
        archive_sha, index_sha = proof.get('checkout_archive_sha256'), proof.get('checkout_index_sha256')
        if not isinstance(archive_sha, str) or not isinstance(index_sha, str):
            raise MigrationRefused('source_checkout_archive')
        managed = ManagedWorkspace.for_repository(repository)
        root, common = managed.repository_root, managed.repository_root / '.git'
        checkout = managed.state_root / 'worktrees' / run_id / f'ticket-{ticket}'
        final_checkout = managed.root / 'worktrees' / run_id / 'run-publication'
        # This descriptor is the sealed source claim. The unique cutover
        # authenticates current resources after strict prepared validation;
        # advanced/cleaned native resources must not redefine this baseline.
        base = Commit(base_sha, _git(root, 'rev-parse', base_sha + '^{tree}'))
        subject = f'ticket:{ticket}'
        spec = RunSpec(run_id, repository, parent_number, parent_revision, graph_revision,
            (Delivery(subject, DeliveryKind.TICKET, ticket, 'source-verified', base, source_ref,
                'refs/heads/' + run_branch, checkout=str(checkout), owned_root=str(checkout.parent),
                common_git_dir=str(common)),
             Delivery(f'final:{run_id}', DeliveryKind.FINAL_RUN, parent_number, parent_revision, base,
                'refs/heads/' + run_branch, 'refs/heads/' + base_branch, checkout=str(final_checkout),
                owned_root=str(final_checkout.parent), common_git_dir=str(common))),
            policy, language, str(root), inputs)
        job = _one(db, 'SELECT j.* FROM work_jobs j JOIN work_job_layout l '
            'ON l.run_id=j.run_id AND l.active_job_id=j.job_id WHERE j.run_id=?', (run_id,))
        owner = SourceRecord('work_jobs', (run_id, job['job_id']))
        aliases = tuple(SourceRecord('work_job_slots', (run_id, row['container'], row['slot']))
            for row in db.execute('SELECT * FROM work_job_slots WHERE run_id=? AND job_id=?', owner.key))
        aliases += (SourceRecord('work_job_layout', (run_id,)),)
        budget = _one(db, 'SELECT * FROM work_job_review_budget WHERE run_id=? AND job_id=?', owner.key)
        budgets = (ImportedBudget(owner=owner, record=SourceRecord('work_job_review_budget', owner.key),
            purpose=Purpose.REQUIREMENTS, window=budget['value_window'],
            developments_used=budget['value_development_attempts'], reviews_used=budget['value_reviewer_invocations'],
            current=True),)
        attempts = []
        for index, call in enumerate(calls):
            attempt = _one(db, 'SELECT * FROM work_attempts WHERE run_id=? AND attempt_id=?', (run_id, call.attempt_id))
            binding = next(item for item in bindings if item['binding_id'] == call.invocation['binding_id'])
            raw = _one(db, 'SELECT m.body FROM work_evidence e JOIN work_evidence_materials m '
                'ON m.material_id=e.material_id WHERE e.run_id=? AND e.evidence_id=?', (run_id, call.raw_evidence_id))[0]
            boundary_id = call.invocation['currentness_boundary_evidence_id']
            boundary = None if boundary_id is None else _evidence(db, run_id, boundary_id,
                owner='invocation:' + json.dumps([subject, call.invocation['started_at']], separators=(',', ':')),
                kind='currentness_boundary')
            attempts.append(ImportedAttempt(record=SourceRecord('work_attempts', (run_id, call.attempt_id)),
                role=call.role, role_ordinal=call.old_ordinal, generation=attempt['generation'],
                purpose=Purpose.REQUIREMENTS, window=attempt['budget_window'], pending=index == len(calls) - 1,
                accounted=call.role == Role.REVIEW and call.current_version['budget_consumed'] == 1,
                outcome=call.current_version['outcome'], raw_record=SourceRecord('work_evidence', (run_id, call.raw_evidence_id)),
                raw_result=raw.encode('utf-8'), normalized_artifact=wire_json(call.native_artifact),
                bound_version=wire_json(_wire_row(call.bound_version)),
                selected_version=wire_json(_wire_row(call.current_version)),
                current_version=wire_json(_wire_row(call.current_version)),
                allocation_boundary=None if boundary is None else wire_json(boundary),
                execution=HistoricalExecution(record=SourceRecord('work_invocations',
                    (run_id, call.invocation['work_subject'], call.invocation['started_at'])),
                    binding_id=call.invocation['binding_id'], thread_id=call.thread_id,
                    binding=wire_json(binding), invocation=wire_json(_wire_row(call.invocation))), continuation_basis=None))
        workspace = ImportedResource(owner=owner, kind='ticket-workspace', disposition='existing',
            source_ref=source_ref, expected_oid=candidate_sha or base_sha, checkout=checkout,
            workspace_oid=candidate_sha or base_sha, owned_root=checkout.parent, common_git_dir=common,
            # The sealed source owner/branch is the original attached HEAD
            # witness. Current HEAD may already be detached by this transfer.
            before_head=('ref: ' + source_ref + '\n').encode('ascii'),
            checkout_archive_sha256=archive_sha, checkout_index_sha256=index_sha, head_evidence=(owner,), cleanup_pending=False)
        integration = None if job['value_ticket_integration'] is None else _one(db,
            'SELECT m.body FROM work_evidence e JOIN work_evidence_materials m ON m.material_id=e.material_id '
            'WHERE e.run_id=? AND e.evidence_id=?', (run_id, job['value_ticket_integration']))[0].encode('utf-8')
        candidate = None
        if candidate_sha is not None:
            assert candidate_tree is not None  # Already bound by the source Review/acceptance checks.
            candidate = Commit(candidate_sha, candidate_tree)
        imported = ImportedSubject(subject=subject, issue=ticket, generation=1, record=owner, aliases=aliases,
            kind='returned', historical_base=base, budgets=budgets, attempts=tuple(attempts),
            candidate=candidate, integration=integration,
            accepted_review=calls[1].attempt_id if pending_role == Role.PUBLICATION else None, completion=None,
            resources=(workspace,))
        task = read_v6_host_exit(db, run_id, required=True)
        documents = tuple((row[0], row[1].encode('utf-8')) for row in db.execute(
            'SELECT e.evidence_id,m.body FROM work_evidence e JOIN work_evidence_materials m '
            'ON m.material_id=e.material_id WHERE e.run_id=? ORDER BY e.evidence_id', (run_id,)))
        if candidate_intent is not None:
            # Preserve the existing v6_candidate_commit_intent history input,
            # a checked source row rather than a native action to dispatch.
            # This reserved document identity does not grant material purpose
            # or effect authority to arbitrary oracle document names.
            documents += (('source:work_job_candidate_commit_intent', wire_json(candidate_intent)),)
        source = ImportSource(snapshot=snapshot, mode='original', scope='single-ticket', task=task,
            checkout_uuid=_one(db, 'SELECT value_checkout_identity FROM run_root WHERE run_id=?', (run_id,))[0],
            profile_document=profile_document, external_readproof=external_document, oracle_documents=documents)
        run_resource = ImportedResource(owner=SourceRecord('runs', (run_id,)), kind='run-ref', disposition='existing',
            source_ref='refs/heads/' + run_branch, expected_oid=base_sha, checkout=None, workspace_oid=None,
            owned_root=None, common_git_dir=common, before_head=None, checkout_archive_sha256=None,
            checkout_index_sha256=None, head_evidence=(SourceRecord('run_condition_base', (run_id,)),), cleanup_pending=False)
        source_timeline = tuple(dict(row) for row in db.execute(
            "SELECT e.* FROM run_timeline_slots s JOIN run_timeline_events e "
            "ON e.run_id=s.run_id AND e.event_id=s.event_id "
            "WHERE s.run_id=? AND s.stream='timeline' ORDER BY s.position", (run_id,)))
        history = tuple(ImportedHistoryEvent(record=SourceRecord('run_timeline_events', (run_id, event['event_id'])),
            subject=subject, generation=1, kind=f"v6_{event['value_kind']}", identity=event['event_id'],
            created_at=_created_at_epoch(event['value_at']), body=wire_json({'event_id': event['event_id'],
                'presence_hex': event['presence'].hex(), 'values': {key: value for key, value in event.items()
                    if key.startswith('value_') and value is not None}})) for event in source_timeline)
        return ImportedRunFacts(source=source, spec=spec, requirements=requirements, observed_requirements=requirements,
            created_at=created_at, subjects=(imported,), run_resource=run_resource, history=history)

    result = _read_returned_source(snapshot.path, profile, profile_sha, snapshot.sha256, decode, original=True)
    validate_snapshot(snapshot)
    if (_read_profile(profile_snapshot)[1] != profile_sha or _read_document(external_snapshot)[1] != proof_sha):
        raise MigrationRefused('source_documents_changed')
    return result
