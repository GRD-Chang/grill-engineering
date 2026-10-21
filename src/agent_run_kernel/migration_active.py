"""A narrow v6 returned-Publication import from a copied, stopped source.

This imports completed synchronous old calls as historical evidence.  It never
invents a native executor, process lease, or Invocation for old calls.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Mapping

from .kernel import Kernel
from .migration import MigrationRefused, SourceSnapshot, _created_at_epoch, capture_v6
from .migration_conversion import _git, _one, _policy
from .model import (Blocker, Commit, CreationInputs, Delivery, DeliveryKind, Issue,
                    KernelError, ParentIssue, Policy, Profile, Requirements, Role,
                    RunSpec, artifact, wire_json)


_ACTIVE_TABLES = frozenset({
    "schema_meta", "runs", "work_evidence_materials", "work_evidence", "work_jobs",
    "work_job_layout", "work_job_slots", "work_job_archives", "work_job_review_artifacts",
    "work_job_acceptance_record", "work_job_review_budget", "work_job_ci_recovery",
    "work_job_integration_resync", "work_job_archive_roots", "work_attempts",
    "work_attempt_versions", "work_attempt_current", "work_attempt_returns",
    "work_attempt_result_members", "work_job_attempt_roots", "work_job_attempt_history",
    "work_invocations", "work_invocation_roots", "work_invocation_current",
    "work_invocation_history", "run_root", "run_parent_versions", "run_graph_versions",
    "run_graph_members", "run_graph_snapshots", "run_graph_scope_order",
    "run_graph_ticket_slots", "run_frontier", "run_condition_base", "run_query_roots",
    "run_timeline_events", "run_timeline_slots", "run_resume_roots",
})


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
    candidate_sha: str
    candidate_tree: str
    integration_target: str
    resync_window: int
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


def _read_active(path: Path, profile: Mapping[str, Any], profile_sha: str,
                 source_sha: str) -> _Active:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        counts = {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if any(count and name not in _ACTIVE_TABLES for name, count in counts.items()):
            raise MigrationRefused("unsupported_source_relations")
        exact = {"runs": 1, "run_root": 1, "run_condition_base": 1, "work_jobs": 2,
                 "work_job_layout": 1, "work_attempts": 3, "work_attempt_current": 3,
                 "work_attempt_returns": 3, "work_invocations": 3,
                 "work_invocation_history": 3, "work_job_attempt_roots": 1,
                 "work_job_attempt_history": 2, "work_job_review_budget": 1,
                 "work_invocation_current": 2, "work_job_review_artifacts": 1,
                 "work_job_acceptance_record": 1, "work_job_integration_resync": 1,
                 "work_job_ci_recovery": 1}
        if any(counts.get(table) != expected for table, expected in exact.items()):
            raise MigrationRefused("unsupported_source_stage")
        run = _one(db, "SELECT * FROM runs")
        run_id = run["run_id"]
        root = _one(db, "SELECT * FROM run_root WHERE run_id=?", (run_id,))
        layout = _one(db, "SELECT * FROM work_job_layout WHERE run_id=?", (run_id,))
        job = _one(db, "SELECT * FROM work_jobs WHERE run_id=? AND job_id=?",
                   (run_id, layout["active_job_id"]))
        ticket = job["value_ticket_number"]
        if (run["status"] != "active" or root["value_language"] not in {"zh", "en"}
                or job["job_id"] != f"ticket:{ticket}:generation:1" or job["stage"] != "accepted"
                or job["owner_kind"] != "ticket" or job["owner_key"] != str(ticket)
                or job["value_ticket_branch_initialized"] != 1
                or job["value_ticket_branch"] != f"agent-run/{run_id}/ticket-{ticket}"
                or job["value_base_sha"] is None or job["value_candidate_sha"] is None
                or job["value_publication_attempts"] != 1
                or job["value_modification_attempts"] != 1
                or job["value_validation_attempts"] != 1):
            raise MigrationRefused("unsupported_source_stage")
        selected = _one(db, "SELECT * FROM work_jobs WHERE run_id=? AND job_id!=?",
                        (run_id, job["job_id"]))
        if selected["job_id"] != f"ticket:{ticket}:selection" or selected["stage"] is not None:
            raise MigrationRefused("unsupported_source_stage")
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
        if not isinstance(bindings, list) or len(bindings) != 2:
            raise MigrationRefused("source_profile_bindings")
        by_thread = {row.get("thread_id"): row for row in bindings if isinstance(row, dict)}
        if len(by_thread) != 2 or {row.get("role") for row in bindings} != {"development", "review"}:
            raise MigrationRefused("source_profile_bindings")
        roots = _one(db, "SELECT * FROM work_job_attempt_roots WHERE run_id=? AND job_id=?",
                     (run_id, job["job_id"]))
        history = db.execute("SELECT * FROM work_job_attempt_history WHERE run_id=? AND job_id=? ORDER BY position",
                             (run_id, job["job_id"])).fetchall()
        inv_history = db.execute("SELECT * FROM work_invocation_history WHERE run_id=? ORDER BY position",
                                 (run_id,)).fetchall()
        active_invocations = db.execute("SELECT * FROM work_invocation_current WHERE run_id=?",
                                        (run_id,)).fetchall()
        if ([row["position"] for row in history] != [0, 1]
                or [row["position"] for row in inv_history] != [0, 1, 2]
                or roots["pending_present"] != 1 or roots["history_count"] != 2
                or {row["started_at"] for row in active_invocations} != {inv_history[2]["started_at"]}
                or {row["reference_kind"] for row in active_invocations} != {"active", "ticket"}):
            raise MigrationRefused("unsupported_source_attempt_history")
        ordered_ids = [row["attempt_id"] for row in history] + [roots["pending_attempt_id"]]
        if len(set(ordered_ids)) != 3:
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
                    or version["status"] != ("pending" if index == 2 else "completed")
                    or (index == 0 and version["outcome"] != "candidate")
                    or (index == 1 and (version["outcome"] != "acceptance_artifact"
                                        or version["budget_consumed"] != 1))
                    or version["returned_result_evidence_id"] != (returned["evidence_id"] if index == 2 else None)
                    or (index < 2 and history[index]["version_id"] != current["version_id"])
                    or invocation["attempt_id"] != attempt_id
                    or invocation["raw_result_evidence_id"] != returned["evidence_id"]
                    or invocation["status"] != "completed" or invocation["ended_at"] is None
                    or bound["status"] != "pending" or bound["returned_result_evidence_id"] is not None
                    or invocation["attempt_version_id"] == current["version_id"]
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
            if not isinstance(raw, dict) or raw.get("thread_id") != thread_id:
                raise MigrationRefused("source_returned_result")
            if role == Role.DEVELOPMENT:
                if raw.get("kind") != "development" or not isinstance(raw.get("summary"), str):
                    raise MigrationRefused("source_returned_result")
                value: Any = {"result_kind": "development", "summary": raw["summary"], "human_blockers": None}
            else:
                if raw.get("kind") != ("review" if role == Role.REVIEW else "publication"):
                    raise MigrationRefused("source_returned_result")
                value = raw.get("artifact")
            try:
                normalized = artifact(role, wire_json(value))
            except (KernelError, TypeError, ValueError) as error:
                raise MigrationRefused("unsupported_source_artifact") from error
            calls.append(_Call(attempt_id, role, attempt["ordinal"], thread_id,
                               dict(invocation), dict(bound), dict(version), raw,
                               returned["evidence_id"], normalized))
        if (calls[2].invocation["requested_thread_id"] != calls[0].thread_id
                or calls[2].thread_id != calls[0].thread_id):
            raise MigrationRefused("source_publication_thread")
        if counts.get("work_invocation_segments", 0) or counts.get("work_invocation_wait_intervals", 0):
            raise MigrationRefused("unsupported_source_process")
        budget = _one(db, "SELECT * FROM work_job_review_budget WHERE run_id=? AND job_id=?",
                      (run_id, job["job_id"]))
        if (budget["value_window"] != 1 or budget["value_development_attempts"] != 1
                or budget["value_reviewer_invocations"] != 1 or budget["value_final_ci_fix_used"] != 0):
            raise MigrationRefused("unsupported_source_budget")
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
        evidence = tuple({"evidence_id": row["evidence_id"], "owner_id": row["owner_id"],
                          "kind": row["kind"], "version": row["version"],
                          "source_evidence_id": row["source_evidence_id"],
                          "source_member": row["source_member"],
                          "value": _evidence(db, run_id, row["evidence_id"],
                                             owner=row["owner_id"], kind=row["kind"])}
                         for row in db.execute("SELECT * FROM work_evidence WHERE run_id=? ORDER BY evidence_id", (run_id,)))
        if len(evidence) != counts["work_evidence"]:
            raise MigrationRefused("source_evidence")
        timeline = tuple(dict(row) for row in db.execute(
            "SELECT e.* FROM run_timeline_slots s JOIN run_timeline_events e "
            "ON e.run_id=s.run_id AND e.event_id=s.event_id "
            "WHERE s.run_id=? AND s.stream='timeline' ORDER BY s.position", (run_id,)))
        if len(timeline) != counts["run_timeline_events"]:
            raise MigrationRefused("source_timeline")
        return _Active(run_id, run["repository"], run["parent_number"], parent_revision,
                       graph_revision, _created_at_epoch(run["created_at"]), root["value_language"],
                       requirements, policy, inputs, root["value_run_branch"], base["value_branch"],
                       base["value_sha"], ticket, f"refs/heads/{job['value_ticket_branch']}",
                       job["value_candidate_sha"], integration["candidate_tree"],
                       integration["target_sha"], resync["value_window"],
                       tuple(calls), tuple(bindings), evidence, timeline)
    except sqlite3.Error as error:
        raise MigrationRefused("source_corrupt") from error
    finally:
        db.close()


def convert_returned_publication_v6(
    snapshot: SourceSnapshot, destination: Path, *, repository_root: Path,
    checkout: Path, final_checkout: Path, profile_snapshot: Path,
    observation_snapshot: Path, remote_source_sha: str, remote_target_sha: str,
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
        completed = observed.get("completed_invocation")
        pending = observed.get("pending_attempt")
        if (observed.get("run_id") != facts.run_id or not isinstance(completed, dict)
                or not isinstance(pending, dict)
                or not isinstance(completed.get("semantic_attempt"), dict)
                or pending.get("attempt_id") != facts.calls[2].attempt_id
                or completed["semantic_attempt"].get("attempt_id") != facts.calls[2].attempt_id
                or completed.get("started_at") != facts.calls[2].invocation["started_at"]
                or completed.get("ended_at") != facts.calls[2].invocation["ended_at"]
                or completed.get("status") != "completed"
                or completed.get("execution_segments") != []):
            raise MigrationRefused("source_quiescence_unproved")
        if _git(repository_root, "rev-parse", "--show-toplevel") != str(repository_root):
            raise MigrationRefused("source_git")
        for ref in (facts.source_ref, f"refs/heads/{facts.run_branch}", f"refs/heads/{facts.base_branch}"):
            _git(repository_root, "check-ref-format", ref)
        if (remote_source_sha != facts.base_sha or remote_target_sha != facts.integration_target
                or _git(repository_root, "rev-parse", f"refs/heads/{facts.run_branch}^{{commit}}") != facts.base_sha
                or _git(repository_root, "rev-parse", f"refs/heads/{facts.base_branch}^{{commit}}") != facts.base_sha
                or _git(repository_root, "rev-parse", f"{facts.source_ref}^{{commit}}") != facts.candidate_sha
                or _git(repository_root, "rev-parse", f"{facts.candidate_sha}^{{tree}}") != facts.candidate_tree):
            raise MigrationRefused("source_git")
        if (not checkout.is_dir() or _git(checkout, "rev-parse", "--show-toplevel") != str(checkout)
                or _git(checkout, "rev-parse", "HEAD") != facts.candidate_sha
                or _git(checkout, "status", "--porcelain") or checkout == repository_root):
            raise MigrationRefused("source_checkout")
        common_value = Path(_git(checkout, "rev-parse", "--git-common-dir") or "")
        common = (common_value if common_value.is_absolute() else checkout / common_value).resolve()
        if common != (repository_root / ".git").resolve():
            raise MigrationRefused("source_checkout")
        if final_checkout.exists() or final_checkout == checkout or final_checkout == repository_root:
            raise MigrationRefused("new_final_checkout")
        archive_ref = f"refs/agent-run-kernel-migration/{facts.run_id}/ticket-{facts.ticket}"
        _git(repository_root, "check-ref-format", archive_ref)
        if _git(repository_root, "for-each-ref", "--format=%(refname)", archive_ref):
            raise MigrationRefused("migration_archive_exists")
        subject = f"ticket:{facts.ticket}"
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
        candidate = Path(directory) / "kernel.sqlite3"
        kernel = Kernel.create(candidate, spec, requirements=facts.requirements)
        with kernel.storage.transaction() as tx:
            native = tx.one("SELECT revision,completion_revision FROM subjects WHERE id=?", (subject,))
            tx.execute("UPDATE runs SET created_at=?", (facts.created_at,))
            tx.execute("UPDATE subjects SET phase='publish',current_attempt=?,review_attempt=?,"
                       "development_thread=?,candidate_sha=?,candidate_tree=?,workspace_sha=?,"
                       "expected_tree=?,integrated_sha=?,integrated_tree=?,integration_target=?,gate_mode='review' "
                       "WHERE id=?", (facts.calls[2].attempt_id, facts.calls[1].attempt_id,
                                      facts.calls[0].thread_id, facts.candidate_sha, facts.candidate_tree,
                                      facts.candidate_sha, facts.candidate_tree, facts.candidate_sha,
                                      facts.candidate_tree, facts.integration_target, subject))
            tx.execute("UPDATE budgets SET developments_used=1,reviews_used=1 "
                       "WHERE subject=? AND purpose='requirements' AND window=1", (subject,))
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
                    context_witness["checkout"] = None
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
                            native["completion_revision"], context, result, "returned" if index == 3 else "consumed",
                            1 if index == 2 else 0,
                            None if index == 3 else "pass" if index == 2 else "development"))
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
                "remote_target_sha": remote_target_sha, "archive_ref": archive_ref,
                "checkout": str(checkout), "mirror_conversion": "candidate-to-remote-base",
                "source_backend": "completed-synchronous-oracle",
            }))
        if (kernel.status(subject)["pending_attempt"] != facts.calls[2].attempt_id
                or kernel.status(subject)["phase"] != "publish"):
            raise MigrationRefused("converted_state")
        if _read_profile(profile_snapshot)[1] != profile_sha:
            raise MigrationRefused("source_profile_changed")
        if hashlib.sha256(observation_snapshot.read_bytes()).hexdigest() != observation_sha:
            raise MigrationRefused("source_observation_changed")
        # This is only the caller's isolated copied repository.  Keep the old
        # candidate reachable before normalizing the local mirror to the
        # observed remote parent.  The old source and its ref are untouched.
        zeros = "0" * len(facts.candidate_sha)
        archived = normalized = linked = False
        try:
            _git(repository_root, "update-ref", archive_ref, facts.candidate_sha, zeros)
            archived = True
            _git(repository_root, "update-ref", facts.source_ref, remote_source_sha, facts.candidate_sha)
            normalized = True
            if (_git(repository_root, "rev-parse", archive_ref) != facts.candidate_sha
                    or _git(repository_root, "rev-parse", facts.source_ref) != remote_source_sha
                    or _git(checkout, "rev-parse", "HEAD") != facts.candidate_sha
                    or _git(checkout, "status", "--porcelain")):
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
                if normalized:
                    _git(repository_root, "update-ref", facts.source_ref,
                         facts.candidate_sha, remote_source_sha)
                if archived:
                    _git(repository_root, "update-ref", "-d", archive_ref, facts.candidate_sha)
            except MigrationRefused as error:
                # No target database was activated.  The candidate remains
                # reachable in this disposable copy; reconstruct from the
                # immutable bundle instead of retrying this changed copy.
                raise MigrationRefused("copied_mirror_recovery_required") from error
            raise
    return Kernel(destination)
