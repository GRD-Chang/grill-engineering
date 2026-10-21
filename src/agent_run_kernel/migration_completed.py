"""Offline import of one completed v6 Ticket with witnessed external effects.

The old synchronous backend has semantic Attempts, but no physical Invocation
or bound Thread.  Completion is imported as a native relation, with old effect
receipts and chronology retained as source material.  This does not authorize
resuming the old runtime after the new one has acted.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Mapping

from .kernel import Kernel
from .migration import (MigrationRefused, SourceSnapshot, _created_at_epoch,
                        _require_no_v6_notification_journal, capture_v6)
from .migration_active import _evidence, _read_profile, _wire_row
from .migration_conversion import _git, _one, _policy
from .model import (Blocker, Commit, CreationInputs, Delivery, DeliveryKind, Issue,
                    KernelError, ParentIssue, Policy, Profile, Requirements, Role,
                    RunSpec, artifact, wire_json)


_COMPLETED_TABLES = frozenset({
    "schema_meta", "runs", "run_root", "run_condition_base", "run_parent_versions",
    "run_graph_versions", "run_graph_snapshots", "run_graph_scope_order",
    "run_graph_ticket_slots", "run_graph_members", "run_frontier", "run_query_roots",
    "run_resume_roots", "run_timeline_events", "run_timeline_slots",
    "run_closeout_headers", "run_cleanup_items", "work_jobs", "work_job_slots",
    "work_job_layout", "work_job_archives", "work_job_archive_roots",
    "work_job_attempt_roots", "work_job_attempt_history", "work_job_acceptance_record",
    "work_job_review_artifacts", "work_job_review_budget", "work_job_ci_recovery",
    "work_job_integration_resync", "work_job_merge_intent", "work_job_record_strings",
    "work_job_ticket_close_intent", "work_job_ticket_close_ownership",
    "work_invocation_roots", "work_evidence", "work_evidence_materials",
    "work_attempts", "work_attempt_versions", "work_attempt_current",
    "work_attempt_returns", "work_attempt_result_members",
})


@dataclass(frozen=True)
class _Completed:
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
    ticket_branch: str
    candidate_sha: str
    candidate_tree: str
    integrated_sha: str
    integrated_tree: str
    pr_number: int
    old_job_revision: int
    old_job: dict[str, Any]
    old_cleanup: dict[str, Any]
    attempts: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    timeline: tuple[dict[str, Any], ...]
    close_intent: dict[str, Any]
    close_ownership: dict[str, Any]


def _completed_evidence(db: sqlite3.Connection, run_id: str, evidence_id: str,
                        *, owner: str, kind: str) -> Any:
    row = _one(db, "SELECT * FROM work_evidence WHERE run_id=? AND evidence_id=?",
               (run_id, evidence_id))
    if row["source_member"] != "publication_fields":
        return _evidence(db, run_id, evidence_id, owner=owner, kind=kind)
    source = _one(db, "SELECT * FROM work_evidence WHERE run_id=? AND evidence_id=?",
                  (run_id, row["source_evidence_id"]))
    if (row["owner_id"] != owner or row["kind"] != kind or row["material_id"] is not None
            or source["kind"] != "returned_result" or source["source_evidence_id"] is not None
            or not isinstance(row["value_bytes"], int) or row["value_bytes"] > 4 * 1024 * 1024):
        raise MigrationRefused("source_evidence")
    original = _evidence(db, run_id, source["evidence_id"],
                         owner=source["owner_id"], kind="returned_result")
    if not isinstance(original, dict) or not isinstance(original.get("artifact"), dict):
        raise MigrationRefused("source_evidence")
    fields = ("commit_message", "pr_title", "pr_body_markdown")
    artifact_value = original["artifact"]
    if any(key not in artifact_value for key in fields):
        raise MigrationRefused("source_evidence")
    selected = {key: artifact_value[key] for key in fields}
    body = wire_json(selected)
    if len(body) != row["value_bytes"] or hashlib.sha256(body).hexdigest() != row["value_digest"]:
        raise MigrationRefused("source_evidence")
    return selected


def _read_completed(path: Path, profile: Mapping[str, Any], profile_sha: str,
                    source_sha: str) -> _Completed:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        counts = {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if any(count and name not in _COMPLETED_TABLES for name, count in counts.items()):
            raise MigrationRefused("unsupported_source_relations")
        exact = {"runs": 1, "run_root": 1, "run_condition_base": 1,
                 "work_jobs": 2, "work_job_layout": 1, "work_attempts": 3,
                 "work_attempt_current": 3, "work_attempt_returns": 3,
                 "work_job_attempt_history": 3, "work_job_attempt_roots": 1,
                 "work_invocations": 0, "work_invocation_history": 0,
                 "work_invocation_current": 0, "run_timeline_events": 22,
                 "run_timeline_slots": 22, "run_closeout_headers": 1,
                 "run_cleanup_items": 1, "work_job_ticket_close_intent": 1,
                 "work_job_ticket_close_ownership": 1, "work_job_review_artifacts": 1,
                 "work_job_acceptance_record": 1, "work_job_review_budget": 1,
                 "work_job_integration_resync": 1, "work_job_ci_recovery": 1,
                 "work_job_merge_intent": 1}
        if any(counts.get(name, 0) != value for name, value in exact.items()):
            raise MigrationRefused("unsupported_source_stage")
        run = _one(db, "SELECT * FROM runs")
        root = _one(db, "SELECT * FROM run_root WHERE run_id=?", (run["run_id"],))
        layout = _one(db, "SELECT * FROM work_job_layout WHERE run_id=?", (run["run_id"],))
        job = _one(db, "SELECT * FROM work_jobs WHERE run_id=? AND job_id=?",
                   (run["run_id"], layout["active_job_id"]))
        ticket = job["value_ticket_number"]
        if (not isinstance(run["run_id"], str)
                or re.fullmatch(r"run-[1-9][0-9]*-[0-9a-f]{16}", run["run_id"]) is None
                or type(ticket) is not int or ticket < 1):
            raise MigrationRefused("source_identity")
        selected = _one(db, "SELECT * FROM work_jobs WHERE run_id=? AND job_id!=?",
                        (run["run_id"], job["job_id"]))
        if (run["status"] != "ticket_completed" or root["value_language"] not in {"zh", "en"}
                or job["job_id"] != f"ticket:{ticket}:generation:1" or job["stage"] != "completed"
                or job["owner_kind"] != "ticket" or job["owner_key"] != str(ticket)
                or selected["job_id"] != f"ticket:{ticket}:selection" or selected["stage"] is not None
                or job["value_ticket_closed_by_run"] != 1
                or job["value_ticket_branch_initialized"] != 1
                or job["value_ticket_branch"] != f"agent-run/{run['run_id']}/ticket-{ticket}"
                or job["value_publication_attempts"] != 1
                or job["value_modification_attempts"] != 1
                or job["value_validation_attempts"] != 1
                or job["value_candidate_sha"] is None or job["value_integrated_sha"] is None
                or job["value_publication_sha"] != job["value_integrated_sha"]
                or job["value_integrated_tree"] is None or job["value_pr_number"] is None):
            raise MigrationRefused("unsupported_source_stage")
        closeout = _one(db, "SELECT * FROM run_closeout_headers WHERE run_id=? AND kind='cleanup'",
                        (run["run_id"],))
        cleanup = _one(db, "SELECT * FROM run_cleanup_items WHERE run_id=?", (run["run_id"],))
        if (closeout["value_status"] != "completed" or cleanup["branch"] != job["value_ticket_branch"]
                or cleanup["value_status"] != "completed"
                or cleanup["value_expected_head_sha"] != job["value_integrated_sha"]):
            raise MigrationRefused("source_cleanup")
        parent = _one(db, "SELECT * FROM run_parent_versions WHERE run_id=? AND version_id=?",
                      (run["run_id"], root["value_parent_version_id"]))
        title = _completed_evidence(db, run["run_id"], parent["title_id"], owner=f"run:{run['run_id']}", kind="parent.title")
        body = _completed_evidence(db, run["run_id"], parent["body_id"], owner=f"run:{run['run_id']}", kind="parent.body")
        graph = _one(db, "SELECT g.revision,s.evidence_id FROM run_graph_snapshots s "
                     "JOIN run_graph_versions g ON g.run_id=s.run_id AND g.version_id=s.version_id "
                     "WHERE s.run_id=? AND s.snapshot_id=?",
                     (run["run_id"], root["value_graph_snapshot_id"]))
        observation = _completed_evidence(db, run["run_id"], graph["evidence_id"],
                                          owner=f"run:{run['run_id']}", kind="graph.observation")
        order = [row[0] for row in db.execute("SELECT ticket_number FROM run_graph_scope_order "
                                               "WHERE run_id=? AND snapshot_id=? ORDER BY position",
                                               (run["run_id"], root["value_graph_snapshot_id"]))]
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
        graph_revision = Kernel._fingerprint({"tickets": order,
                                              "dependencies": {str(ticket): sorted(
                                                  edge.number for edge in issue.blocked_by)}})
        if (parent["revision"] != parent_revision or graph["revision"] != graph_revision
                or root["value_accepted_parent_spec_revision"] != parent_revision
                or root["value_accepted_ticket_graph_revision"] != graph_revision
                or item["content_revision"] != Kernel._fingerprint({"title": issue.title, "body": issue.body})):
            raise MigrationRefused("source_revision")
        base = _one(db, "SELECT * FROM run_condition_base WHERE run_id=?", (run["run_id"],))
        if (job["value_base_sha"] != base["value_sha"] or base["value_branch"] != "main"
                or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None
                       for value in (base["value_sha"], job["value_candidate_sha"],
                                     job["value_integrated_sha"]))
                or root["value_run_branch"] != f"agent-run/{run['run_id']}/run"):
            raise MigrationRefused("source_base")
        frozen_ids = {kind: root[field] for field, kind in (
            ("value_policy_snapshot", "policy_snapshot"),
            ("value_prompt_resources", "prompt_resources"),
            ("value_github_identity", "github_identity"),
            ("value_runner_github_identity", "runner_github_identity"))}
        frozen = {kind: _completed_evidence(db, run["run_id"], evidence_id,
                                            owner=f"run:{run['run_id']}", kind=kind)
                  for kind, evidence_id in frozen_ids.items()}
        query = _one(db, "SELECT presence FROM run_query_roots WHERE run_id=?", (run["run_id"],))
        if query["presence"] != b"\x1a" or counts.get("run_notification_config", 0):
            raise MigrationRefused("unsupported_source_notifications")
        if (profile.get("run_id") != run["run_id"] or profile.get("profile_revision") != 1
                or profile.get("bindings") != []):
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
        budget = _one(db, "SELECT * FROM work_job_review_budget WHERE run_id=? AND job_id=?",
                      (run["run_id"], job["job_id"]))
        resync = _one(db, "SELECT * FROM work_job_integration_resync WHERE run_id=? AND job_id=?",
                      (run["run_id"], job["job_id"]))
        if (budget["value_window"] != 1 or budget["value_development_attempts"] != 1
                or budget["value_reviewer_invocations"] != 1 or budget["value_final_ci_fix_used"] != 0
                or resync["value_generation"] != 1 or resync["value_window"] != 1
                or resync["value_used"] != 0 or resync["value_limit"] != policy.resync_limit
                or resync["value_open"] != 0 or resync["value_source_sha"] != base["value_sha"]
                or resync["value_target_sha"] != base["value_sha"]):
            raise MigrationRefused("unsupported_source_budget")
        roots = _one(db, "SELECT * FROM work_job_attempt_roots WHERE run_id=? AND job_id=?",
                     (run["run_id"], job["job_id"]))
        history = db.execute("SELECT * FROM work_job_attempt_history WHERE run_id=? AND job_id=? ORDER BY position",
                             (run["run_id"], job["job_id"])).fetchall()
        if (roots["pending_present"] != 0 or roots["history_count"] != 3
                or [row["position"] for row in history] != [0, 1, 2]):
            raise MigrationRefused("source_attempt_history")
        calls: list[dict[str, Any]] = []
        version_total = member_total = 0
        for index, witness in enumerate(history):
            role = (Role.DEVELOPMENT, Role.REVIEW, Role.PUBLICATION)[index]
            attempt_id = witness["attempt_id"]
            attempt = _one(db, "SELECT * FROM work_attempts WHERE run_id=? AND attempt_id=?",
                           (run["run_id"], attempt_id))
            current = _one(db, "SELECT * FROM work_attempt_current WHERE run_id=? AND attempt_id=?",
                           (run["run_id"], attempt_id))
            version = _one(db, "SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?",
                           (run["run_id"], attempt_id, current["version_id"]))
            returned = _one(db, "SELECT * FROM work_attempt_returns WHERE run_id=? AND attempt_id=?",
                            (run["run_id"], attempt_id))
            if (attempt["job_id"] != job["job_id"] or attempt["work_subject"] != f"ticket:{ticket}"
                    or attempt["generation"] != 1 or attempt["ordinal"] != 1
                    or attempt["role"] != ("development", "reviewer", "publication")[index]
                    or version["status"] != "completed" or witness["version_id"] != current["version_id"]
                    or version["outcome"] != ("candidate", "acceptance_artifact", "publication_artifact")[index]):
                raise MigrationRefused("source_attempts")
            raw = _completed_evidence(db, run["run_id"], returned["evidence_id"],
                                      owner=f"attempt:{attempt_id}", kind="returned_result")
            if (not isinstance(raw, dict) or raw.get("kind") != ("development", "review", "publication")[index]
                    or not isinstance(raw.get("thread_id"), str)):
                raise MigrationRefused("source_returned_result")
            value: Any = ({"result_kind": "development", "summary": raw.get("summary"),
                           "human_blockers": None} if index == 0 else
                          {"checks": raw.get("artifact", {}).get("checks")}
                          if index == 1 and isinstance(raw.get("artifact"), dict) else
                          raw.get("artifact"))
            try:
                normalized = artifact(role, wire_json(value))
            except (KernelError, TypeError, ValueError) as error:
                raise MigrationRefused("unsupported_source_artifact") from error
            versions = tuple(_wire_row(dict(row)) for row in db.execute(
                "SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? ORDER BY rowid",
                (run["run_id"], attempt_id)))
            members = tuple(_wire_row(dict(row)) for row in db.execute(
                "SELECT * FROM work_attempt_result_members WHERE run_id=? AND attempt_id=? ORDER BY rowid",
                (run["run_id"], attempt_id)))
            version_total += len(versions)
            member_total += len(members)
            calls.append({"attempt_id": attempt_id, "role": role.value,
                          "old_role_ordinal": attempt["ordinal"], "returned": raw,
                          "current_version": _wire_row(dict(version)), "versions": versions,
                          "result_members": members, "artifact": normalized,
                          "raw_evidence_id": returned["evidence_id"]})
        if (version_total != counts["work_attempt_versions"]
                or member_total != counts["work_attempt_result_members"]):
            raise MigrationRefused("source_attempt_relations")
        review = _one(db, "SELECT * FROM work_job_review_artifacts WHERE run_id=? AND job_id=?",
                      (run["run_id"], job["job_id"]))
        accepted = _one(db, "SELECT * FROM work_job_acceptance_record WHERE run_id=? AND job_id=?",
                        (run["run_id"], job["job_id"]))
        if (review["candidate_sha"] != job["value_candidate_sha"]
                or review["reviewed_base_sha"] != base["value_sha"]
                or review["artifact_id"] != job["value_acceptance_artifact"]
                or accepted["value_reviewed_candidate_sha"] != job["value_candidate_sha"]
                or accepted["value_reviewed_candidate_tree"] is None
                or accepted["value_reviewed_base_sha"] != base["value_sha"]):
            raise MigrationRefused("source_acceptance")
        close_intent = _one(db, "SELECT * FROM work_job_ticket_close_intent WHERE run_id=? AND job_id=?",
                            (run["run_id"], job["job_id"]))
        close_owner = _one(db, "SELECT * FROM work_job_ticket_close_ownership WHERE run_id=? AND job_id=?",
                           (run["run_id"], job["job_id"]))
        binding = f"pr-{job['value_pr_number']}:sha-{job['value_integrated_sha']}"
        if (close_intent["value_intent_binding"] != binding
                or close_owner["value_intent_binding"] != binding
                or close_owner["value_actor"] != close_intent["value_actor"]
                or close_owner["value_event_id"] != f"{run['run_id']}:ticket-{ticket}:closed"
                or close_intent["value_dispatch_attempted"] != 1):
            raise MigrationRefused("source_close_ownership")
        evidence = tuple({"evidence_id": row["evidence_id"], "owner_id": row["owner_id"],
                          "kind": row["kind"], "value": _completed_evidence(db, run["run_id"], row["evidence_id"],
                                                     owner=row["owner_id"], kind=row["kind"])}
                         for row in db.execute("SELECT * FROM work_evidence WHERE run_id=? ORDER BY evidence_id", (run["run_id"],)))
        timeline = tuple(dict(row) for row in db.execute(
            "SELECT e.* FROM run_timeline_slots s JOIN run_timeline_events e "
            "ON e.run_id=s.run_id AND e.event_id=s.event_id "
            "WHERE s.run_id=? AND s.stream='timeline' ORDER BY s.position", (run["run_id"],)))
        if (len(timeline) != 22 or timeline[-1]["value_phase"] != "completed"):
            raise MigrationRefused("source_history")
        return _Completed(run["run_id"], run["repository"], run["parent_number"],
                          parent_revision, graph_revision, _created_at_epoch(run["created_at"]),
                          root["value_language"], requirements, policy, inputs,
                          root["value_run_branch"], base["value_branch"], base["value_sha"],
                          ticket, job["value_ticket_branch"], job["value_candidate_sha"],
                          accepted["value_reviewed_candidate_tree"], job["value_integrated_sha"],
                          job["value_integrated_tree"], job["value_pr_number"], job["revision"],
                          _wire_row(dict(job)), _wire_row(dict(cleanup)), tuple(calls),
                          evidence, timeline, _wire_row(dict(close_intent)),
                          _wire_row(dict(close_owner)))
    except sqlite3.Error as error:
        raise MigrationRefused("source_corrupt") from error
    finally:
        db.close()


def _read_document(path: Path, *, limit: int = 4 * 1024 * 1024) -> tuple[dict[str, Any], str]:
    if (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
            or not 0 < path.stat().st_size <= limit):
        raise MigrationRefused("source_document")
    body = path.read_bytes()
    try:
        document = json.loads(body)
    except (UnicodeError, ValueError) as error:
        raise MigrationRefused("source_document") from error
    if not isinstance(document, dict):
        raise MigrationRefused("source_document")
    if path.read_bytes() != body:
        raise MigrationRefused("source_document_changed")
    return document, hashlib.sha256(body).hexdigest()


def _copy_loose_git(source: Path, target: Path, *, run_ref: str, base: str,
                    candidate: str, integrated: str, candidate_tree: str,
                    integrated_tree: str) -> None:
    """Build a new owned repository using only sealed loose objects, never source config."""
    if (source.is_symlink() or not source.is_dir() or target.is_symlink()
            or not target.is_dir() or any(target.iterdir())):
        raise MigrationRefused("source_git_store")
    objects = source / "objects"
    if objects.is_symlink() or not objects.is_dir():
        raise MigrationRefused("source_git_store")
    entries = list(objects.rglob("*"))
    regular = []
    if len(entries) > 4096:
        raise MigrationRefused("source_git_store_size")
    for entry in entries:
        relative = entry.relative_to(objects)
        if entry.is_symlink() or (not entry.is_dir() and not entry.is_file()):
            raise MigrationRefused("source_git_store")
        if entry.is_file() and (len(relative.parts) != 2 or len(relative.parts[0]) != 2
                                or len(relative.parts[1]) != 38
                                or any(char not in "0123456789abcdef" for char in "".join(relative.parts))):
            raise MigrationRefused("unsupported_git_store")
        if entry.is_file():
            regular.append(entry)
    if sum(entry.stat().st_size for entry in regular) > 128 * 1024 * 1024:
        raise MigrationRefused("source_git_store_size")
    _git(target, "init", "--initial-branch=main")
    for entry in regular:
        relative = entry.relative_to(objects)
        destination = target / ".git" / "objects" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry, destination)
    for sha, tree in ((candidate, candidate_tree), (integrated, integrated_tree)):
        if _git(target, "cat-file", "-t", sha) != "commit" or _git(target, "rev-parse", f"{sha}^{{tree}}") != tree:
            raise MigrationRefused("source_git_object")
    if (_git(target, "cat-file", "-t", base) != "commit"
            or _git(target, "show", "-s", "--format=%P", integrated) != base):
        raise MigrationRefused("source_git_ancestry")
    _git(target, "update-ref", "refs/heads/main", base)
    _git(target, "update-ref", run_ref, integrated)
    archive = f"refs/agent-run-kernel-migration/{run_ref.split('/')[3]}/ticket-candidate"
    _git(target, "update-ref", archive, candidate)
    _git(target, "fsck", "--full", "--no-reflogs")
    _git(target, "reset", "--hard", base)
    if _git(target, "status", "--porcelain"):
        raise MigrationRefused("copied_git_dirty")


def convert_completed_ticket_v6(
    snapshot: SourceSnapshot, destination: Path, *, git_store: Path,
    repository_root: Path, final_checkout: Path, profile_snapshot: Path,
    github_snapshot: Path, observation_snapshot: Path, original_report: Path,
) -> Kernel:
    """Import one sealed old Ticket completion, including its native Completion.

    The supplied old artifacts are from a finished synchronous fixture.  This
    narrow API does not certify live Host quiescence or transfer ownership of
    a general old repository.  It creates its own Git repository and database.
    """
    destination = destination.absolute()
    repository_root, final_checkout = repository_root.absolute(), final_checkout.absolute()
    if (destination.exists() or destination.is_symlink() or repository_root.exists()
            or repository_root.is_symlink()
            or not repository_root.parent.resolve().is_relative_to(destination.parent.resolve())
            or not repository_root.is_relative_to(destination.parent)
            or snapshot.source.resolve().is_relative_to(destination.parent.resolve())
            or snapshot.path.resolve().is_relative_to(destination.parent.resolve())
            or git_store.is_symlink()
            or git_store.resolve().is_relative_to(destination.parent.resolve())
            or final_checkout in {repository_root, destination} or final_checkout.exists()
            or not final_checkout.is_relative_to(destination.parent)
            or not final_checkout.parent.resolve().is_relative_to(destination.parent.resolve())):
        raise MigrationRefused("private_destination_required")
    profile, profile_sha = _read_profile(profile_snapshot)
    observed, observation_sha = _read_document(observation_snapshot)
    report, report_sha = _read_document(original_report)
    github, github_sha = _read_document(github_snapshot)
    if (observed.get("backend") != "synchronous scripted roles, no native process evidence or ProfiledAgentBackend"
            or observed.get("copied_database_sha256") != snapshot.sha256
            or observed.get("source_database_sha256") != snapshot.sha256
            or observed.get("profile_present") is not True
            or observed.get("profile_sha256") != profile_sha
            or observed.get("github_sha256") != github_sha
            or observed.get("original_report_sha256") != report_sha
            or report.get("status") != "ticket_completed"
            or observed.get("external_effects") != report.get("mutations")
            or report.get("raw_backend_calls") != {"development": 1, "review": 1, "publication": 1}):
        raise MigrationRefused("source_observation")
    registrations = report.get("post_close_worktrees", {}).get("registrations")
    original_roots = ([Path(row["path"]) for row in registrations
                       if isinstance(row, dict) and row.get("repository") is True
                       and isinstance(row.get("path"), str)]
                      if isinstance(registrations, list) else [])
    if (len(original_roots) != 1 or not original_roots[0].is_absolute()
            or destination.parent.resolve().is_relative_to(original_roots[0].resolve())):
        raise MigrationRefused("old_repository_alias")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".v6-completed-") as directory:
        copied = capture_v6(snapshot.path, Path(directory) / "source.sqlite3")
        if ((copied.sha256, copied.source_protocol, copied.schema_sha256, copied.table_rows)
                != (snapshot.sha256, snapshot.source_protocol, snapshot.schema_sha256, snapshot.table_rows)):
            raise MigrationRefused("snapshot_changed")
        facts = _read_completed(copied.path, profile, profile_sha, snapshot.sha256)
        _require_no_v6_notification_journal(snapshot, facts.run_id)
        if (observed.get("run_id") != facts.run_id
                or observed.get("candidate_sha") != facts.candidate_sha
                or observed.get("integrated_sha") != facts.integrated_sha
                or report.get("source_identity", {}).get("run_id") != facts.run_id
                or report.get("source_identity", {}).get("pull_request") != observed.get("pull_request")):
            raise MigrationRefused("source_observation")
        delivery = github.get("delivery")
        issues = github.get("issues")
        if (github.get("repository") != facts.repository or not isinstance(delivery, dict)
                or not isinstance(issues, dict) or not isinstance(issues.get(str(facts.ticket)), dict)
                or github.get("parent") != {"number": facts.parent_number,
                                            "title": facts.requirements.parent.title,
                                            "body": facts.requirements.parent.body,
                                            "sub_issues": [facts.ticket],
                                            "sub_issue_order_reliable": True}
                or delivery.get("closed_issues") != [facts.ticket]
                or issues[str(facts.ticket)].get("state") != "CLOSED"
                or delivery.get("pull_requests") != [observed["pull_request"]]
                or delivery.get("published_branches", {}).get(facts.run_branch) != facts.integrated_sha):
            raise MigrationRefused("source_external")
        original = facts.requirements.issues[0]
        latest = issues[str(facts.ticket)]
        if (latest.get("number") != facts.ticket or latest.get("title") != original.title
                or latest.get("body") != original.body or set(latest.get("labels", [])) != original.labels
                or latest.get("blocked_by") != [] or original.state != "OPEN"):
            raise MigrationRefused("source_external_issue")
        pr = observed.get("pull_request")
        if (not isinstance(pr, dict)
                or pr.get("number") != facts.pr_number or pr.get("state") != "MERGED"
                or pr.get("branch") != facts.ticket_branch or pr.get("base_branch") != facts.run_branch
                or pr.get("head_sha") != facts.integrated_sha
                or pr.get("integrated_sha") != facts.integrated_sha
                or pr.get("base_sha") != facts.base_sha):
            raise MigrationRefused("source_external_pr")
        mutations = delivery.get("mutations")
        expected = {"run_id": facts.run_id, "ticket_number": facts.ticket,
                    "pr_number": facts.pr_number, "integrated_sha": facts.integrated_sha}
        if (not isinstance(mutations, list) or len(mutations) != 3
                or mutations[:2] != [{"action": action, **expected}
                                     for action in ("completion_comment", "close_issue")]
                or mutations[2] != {"action": "delete_managed_branch", "branch": facts.ticket_branch}
                or delivery.get("ticket_close_ownership", {}).get(str(facts.ticket), {}).get("event_id")
                   != facts.close_ownership["value_event_id"]
                or delivery["ticket_close_ownership"][str(facts.ticket)].get("intent_binding")
                   != facts.close_ownership["value_intent_binding"]
                or delivery["ticket_close_ownership"][str(facts.ticket)].get("actor")
                   != facts.close_ownership["value_actor"]):
            raise MigrationRefused("source_external_effects")
        observed_refs = observed.get("git_refs")
        if observed_refs != sorted([f"refs/heads/{facts.run_branch}\t{facts.integrated_sha}",
                                    f"refs/heads/{facts.base_branch}\t{facts.base_sha}"]):
            raise MigrationRefused("source_git_refs")
        source_run_ref = git_store / "refs" / "heads" / facts.run_branch
        source_base_ref = git_store / "refs" / "heads" / facts.base_branch
        try:
            source_refs_valid = (not any(path.is_symlink() or not path.is_file()
                                         or not path.resolve().is_relative_to(git_store.resolve())
                                         for path in (source_run_ref, source_base_ref))
                                 and source_run_ref.read_text(encoding="ascii").strip() == facts.integrated_sha
                                 and source_base_ref.read_text(encoding="ascii").strip() == facts.base_sha
                                 and not (git_store / "refs" / "heads" / facts.ticket_branch).exists()
                                 and not (git_store / "packed-refs").exists())
        except (OSError, UnicodeError):
            source_refs_valid = False
        if not source_refs_valid:
            raise MigrationRefused("source_git_refs")
        subject = f"ticket:{facts.ticket}"
        owned_repository = False
        try:
            try:
                repository_root.mkdir()
            except FileExistsError as error:
                raise MigrationRefused("private_destination_exists") from error
            owned_repository = True
            _copy_loose_git(git_store, repository_root,
                            run_ref=f"refs/heads/{facts.run_branch}", base=facts.base_sha,
                            candidate=facts.candidate_sha, integrated=facts.integrated_sha,
                            candidate_tree=facts.candidate_tree,
                            integrated_tree=facts.integrated_tree)
            base_tree = _git(repository_root, "rev-parse", f"{facts.base_sha}^{{tree}}")
            common = repository_root / ".git"
            spec = RunSpec(facts.run_id, facts.repository, facts.parent_number,
                           facts.parent_revision, facts.graph_revision,
                           (Delivery(subject, DeliveryKind.TICKET, facts.ticket, "source-verified",
                                     Commit(facts.base_sha, base_tree),
                                     f"refs/heads/{facts.ticket_branch}",
                                     f"refs/heads/{facts.run_branch}"),
                            Delivery(f"final:{facts.run_id}", DeliveryKind.FINAL_RUN,
                                     facts.parent_number, facts.parent_revision,
                                     Commit(facts.integrated_sha, facts.integrated_tree),
                                     f"refs/heads/{facts.run_branch}",
                                     f"refs/heads/{facts.base_branch}",
                                     checkout=str(final_checkout), owned_root=str(final_checkout.parent),
                                     common_git_dir=str(common))),
                           facts.policy, facts.language, str(repository_root), facts.inputs)
            candidate = Path(directory) / "kernel.sqlite3"
            kernel = Kernel.create(candidate, spec, requirements=facts.requirements)
            with kernel.storage.transaction() as tx:
                native = tx.one("SELECT revision,completion_revision FROM subjects WHERE id=?", (subject,))
                tx.execute("UPDATE runs SET created_at=?", (facts.created_at,))
                tx.execute("""UPDATE subjects SET phase='complete',purpose='requirements',
                    source_sha=?,candidate_sha=?,candidate_tree=?,integrated_sha=?,integrated_tree=?,
                    publication_sha=?,publication_tree=?,workspace_sha=?,expected_tree=?,
                    integration_target=?,pr_number=?,gate_mode='review',merge_sha=?,
                    review_attempt=?,current_attempt=NULL WHERE id=?""",
                           (facts.integrated_sha, facts.candidate_sha, facts.candidate_tree,
                            facts.integrated_sha, facts.integrated_tree, facts.integrated_sha,
                            facts.integrated_tree, facts.integrated_sha, facts.candidate_tree,
                            facts.base_sha, facts.pr_number, facts.integrated_sha,
                            facts.attempts[1]["attempt_id"], subject))
                tx.execute("UPDATE budgets SET developments_used=1,reviews_used=1 "
                           "WHERE subject=? AND purpose='requirements' AND window=1", (subject,))
                tx.execute("INSERT INTO integration_snapshots VALUES (?,?,?,?,?,?,?,0)",
                           (subject, 1, 1, f"refs/heads/{facts.ticket_branch}",
                            f"refs/heads/{facts.run_branch}", facts.base_sha, facts.base_sha))
                tx.execute("DELETE FROM work_queue WHERE subject=?", (subject,))
                tx.execute("DELETE FROM integration_queue WHERE subject=?", (subject,))
                tx.execute("DELETE FROM history")
                for event in facts.timeline:
                    payload = {"event_id": event["event_id"], "presence_hex": event["presence"].hex(),
                               "values": {key: value for key, value in event.items()
                                          if key.startswith("value_") and value is not None}}
                    material = tx.put(subject, "v6_timeline_event", wire_json(payload))
                    tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) "
                               "VALUES (?,?,?,?,1,?)", (subject, f"v6_{event['value_kind']}",
                                                       event["event_id"], material,
                                                       _created_at_epoch(event["value_at"])))
                for entry in facts.evidence:
                    tx.put(subject, "v6_evidence", wire_json(entry))
                for index, call in enumerate(facts.attempts, start=1):
                    context_value: dict[str, Any] = {"source": "v6-completed-semantic-attempt",
                                                     "old_role_ordinal": call["old_role_ordinal"],
                                                     "old_current_version": call["current_version"],
                                                     "native_authority": {
                                                         "parent_revision": facts.parent_revision,
                                                         "graph_revision": facts.graph_revision,
                                                         "revision": native["revision"],
                                                         "base_sha": facts.base_sha,
                                                         "candidate_sha": facts.candidate_sha,
                                                         "candidate_tree": facts.candidate_tree}}
                    if call["role"] == "review":
                        context_value["checkout"] = None
                    context = tx.put(subject, "v6_attempt_context", wire_json(context_value))
                    result = tx.put(subject, "v6_normalized_artifact", wire_json(call["artifact"]))
                    tx.execute("""INSERT INTO attempts(
                        id,subject,generation,ordinal,role,purpose,window,parent_revision,graph_revision,
                        revision,base_sha,candidate_sha,candidate_tree,expected_tree,completion_revision,
                        context_id,current_invocation,result_id,status,accounted,outcome)
                        VALUES (?,?,1,?,?,'requirements',1,?,?,?,?,?,?,?,?,?,NULL,?,'consumed',?,?)""",
                               (call["attempt_id"], subject, index, call["role"], facts.parent_revision,
                                facts.graph_revision, native["revision"], facts.base_sha,
                                facts.candidate_sha, facts.candidate_tree, facts.candidate_tree,
                                native["completion_revision"], context, result,
                                1 if call["role"] == "review" else 0,
                                "pass" if call["role"] == "review" else
                                "development" if call["role"] == "development" else "publication"))
                    tx.put(subject, "v6_semantic_attempt", wire_json(call))
                receipt_ids = []
                for operation, mutation in zip(("completion_comment", "close"), mutations[:2], strict=True):
                    receipt = tx.put(subject, "v6_confirmed_effect_receipt", wire_json({
                        "mutation": mutation, "close_intent": facts.close_intent,
                        "close_ownership": facts.close_ownership if operation == "close" else None,
                        "source": "old-github-fixture-and-v6-close-ledger"}))
                    effect_id = f"v6-import:{facts.run_id}:{facts.ticket}:{operation}"
                    payload_id = tx.put(subject, "v6_confirmed_effect_payload", wire_json(mutation))
                    tx.execute("""INSERT INTO effects(
                        id,subject,operation,status,executor,epoch,parent_revision,graph_revision,
                        revision,generation,source_ref,target_ref,before_sha,after_sha,candidate_sha,
                        candidate_tree,base_sha,base_tree,pr_number,actor,payload_id,receipt_id)
                        VALUES (?,?,?,'confirmed',NULL,NULL,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (effect_id, subject, operation, facts.parent_revision, facts.graph_revision,
                                native["revision"], f"refs/heads/{facts.ticket_branch}",
                                f"refs/heads/{facts.run_branch}", facts.integrated_sha,
                                facts.integrated_sha, facts.candidate_sha, facts.candidate_tree,
                                facts.base_sha, base_tree, facts.pr_number,
                                facts.close_ownership["value_actor"], payload_id, receipt))
                    receipt_ids.append(effect_id)
                tx.execute("INSERT INTO completions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (subject, 1, native["revision"], facts.parent_revision,
                            facts.graph_revision, facts.integrated_sha, facts.integrated_sha,
                            facts.integrated_tree, facts.pr_number, "review",
                            facts.attempts[1]["attempt_id"], facts.attempts[0]["attempt_id"],
                            *receipt_ids))
                identity = tx.put(subject, "v6_completed_import_identity", wire_json({
                    "source_sha256": snapshot.sha256, "profile_sha256": profile_sha,
                    "github_sha256": github_sha, "observation_sha256": observation_sha,
                    "original_report_sha256": report_sha,
                    "old_backend": observed["backend"],
                    "old_physical_invocations": 0,
                    "old_profile_bindings": 0,
                    "old_completed_job_revision": facts.old_job_revision,
                    "copied_repository_root": str(repository_root),
                    "candidate_sha": facts.candidate_sha,
                    "integrated_sha": facts.integrated_sha,
                    "old_job": facts.old_job,
                    "old_cleanup": facts.old_cleanup,
                }))
                tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) "
                           "VALUES (?,?,?,?,1,?)", (subject, "v6_completed_import", facts.run_id,
                                                   identity, kernel.clock()))
            latest_requirements = Requirements(facts.requirements.parent,
                                               (replace(facts.requirements.issues[0], state="CLOSED"),))
            # This is the sealed copy's verified GitHub readback, not a new
            # mutable source read. Requirements and all nested facts are frozen.
            kernel.refresh(lambda: latest_requirements)
            state = kernel.status(subject)
            summary = kernel.source_status()
            if (state["phase"] != "complete" or state["pending_write"] is not None
                    or state["pending_attempt"] is not None or len(summary["completions"]) != 1
                    or subject in summary["frontier"]):
                raise MigrationRefused("converted_state")
            for path, expected_sha in ((profile_snapshot, profile_sha), (github_snapshot, github_sha),
                                       (observation_snapshot, observation_sha), (original_report, report_sha)):
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
                    raise MigrationRefused("source_document_changed")
            if any(Path(str(candidate) + suffix).exists() and Path(str(candidate) + suffix).stat().st_size
                   for suffix in ("-wal", "-journal")):
                raise MigrationRefused("converted_sidecar")
            try:
                os.link(candidate, destination)
            except FileExistsError as error:
                raise MigrationRefused("destination_exists") from error
            parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except BaseException:
            if destination.exists() and 'candidate' in locals() and destination.samefile(candidate):
                destination.unlink()
            if owned_repository:
                shutil.rmtree(repository_root)
            raise
    return Kernel(destination)
