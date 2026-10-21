"""Offline completed-Ticket import with lossless history and proved authority.

Historical records grant no execution. Only the current Completion's supporting
semantic Attempts and current allowance become native operational relations.
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
from .migration_records import RECORD_TABLES, _preserve_copy
from .model import (Blocker, Commit, CreationInputs, Delivery, DeliveryKind, Issue,
                    KernelError, ParentIssue, Policy, Profile, Requirements, Role,
                    RunSpec, artifact, wire_json)


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
    timeline: tuple[dict[str, Any], ...]
    close_intent: dict[str, Any]
    close_ownership: dict[str, Any]
    developments_used: int
    reviews_used: int
    physical_invocations: int


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


def _settled_invocations(db: sqlite3.Connection, run_id: str, calls: list[dict[str, Any]],
                         profile: Mapping[str, Any]) -> int:
    """Verify old execution facts without constructing native execution rows."""
    history = db.execute("SELECT * FROM work_invocation_history WHERE run_id=? ORDER BY position",
                         (run_id,)).fetchall()
    count = db.execute("SELECT count(*) FROM work_invocations WHERE run_id=?", (run_id,)).fetchone()[0]
    if not count:
        if history or profile["bindings"] or db.execute(
                "SELECT 1 FROM work_invocation_current WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
            raise MigrationRefused("source_invocation_relations")
        return 0
    if count != len(history) or count != len(calls) or [row["position"] for row in history] != list(range(count)):
        raise MigrationRefused("unsupported_source_invocation_history")
    bindings = profile["bindings"]
    by_thread = {row.get("thread_id"): row for row in bindings if isinstance(row, dict)}
    if len(by_thread) != len(bindings):
        raise MigrationRefused("source_profile_bindings")
    previous_end = 0.0
    for call, witness in zip(calls, history, strict=True):
        invocation = _one(db, "SELECT * FROM work_invocations WHERE run_id=? AND work_subject=? AND started_at=?",
                          (run_id, witness["work_subject"], witness["started_at"]))
        bound = _one(db, "SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?",
                     (run_id, call["attempt_id"], invocation["attempt_version_id"]))
        if (invocation["attempt_id"] != call["attempt_id"]
                or invocation["work_subject"] != call["work_subject"]
                or invocation["generation"] != 1 or invocation["status"] != "completed"
                or invocation["ended_at"] is None or invocation["resume_pending"] != 0
                or invocation["error"] is not None or invocation["execution_interrupted"] != 0
                or invocation["recovery_waiting"] != 0 or invocation["return_code"] is not None
                or invocation["raw_result_evidence_id"] != call["raw_evidence_id"]
                or not isinstance(invocation["reported_thread_id"], str)
                or (call["thread_id"] is not None and invocation["reported_thread_id"] != call["thread_id"])
                or bound["status"] != "pending" or bound["returned_result_evidence_id"] is not None
                or (call["role"] == "review" and
                    (bound["review_context_evidence_id"] is None or bound["review_context_evidence_id"]
                     != call["current_version"]["review_context_evidence_id"]))):
            raise MigrationRefused("unsupported_source_invocation")
        started, ended = (_created_at_epoch(invocation[field]) for field in ("started_at", "ended_at"))
        if not previous_end <= started <= ended:
            raise MigrationRefused("source_invocation_chronology")
        previous_end = ended
        binding = by_thread.get(invocation["reported_thread_id"])
        if (binding is None or any(binding.get(field) != invocation[field] for field in (
                "binding_id", "model", "reasoning_effort", "profile_revision"))
                or binding.get("role") != invocation["binding_role"]
                or invocation["profile_role"] != binding.get("role")
                or invocation["invocation_role"] != call["role"]
                or invocation["profile_revision"] != profile["profile_revision"]):
            raise MigrationRefused("source_profile_bindings")
        owner = 'invocation:' + wire_json([invocation["work_subject"], invocation["started_at"]]).decode()
        bound_thread = _completed_evidence(db, run_id, invocation["thread_execution_binding_evidence_id"],
                                          owner=owner, kind="thread_execution_binding")
        boundary = _completed_evidence(db, run_id, invocation["currentness_boundary_evidence_id"],
                                      owner=owner, kind="currentness_boundary")
        if bound_thread != binding or not isinstance(boundary, dict):
            raise MigrationRefused("source_invocation_binding")
        call["allocation_boundary"] = boundary
        if call["thread_id"] is None:
            # v6 publication_wire has no Thread field. Its distinct physical
            # Invocation supplies the observed identity; raw stays unchanged.
            call["thread_id"] = invocation["reported_thread_id"]
            call["thread_identity_source"] = {key: invocation[key] for key in (
                "work_subject", "started_at", "thread_execution_binding_evidence_id")}
    if db.execute("SELECT 1 FROM work_invocation_current c LEFT JOIN work_invocations v "
                  "USING(run_id,work_subject,started_at) WHERE c.run_id=? AND "
                  "(v.started_at IS NULL OR v.status!='completed' OR v.ended_at IS NULL OR v.resume_pending!=0) LIMIT 1",
                  (run_id,)).fetchone():
        raise MigrationRefused("source_invocation_current")
    return int(count)


def _read_completed(path: Path, profile: Mapping[str, Any], profile_sha: str,
                    source_sha: str) -> _Completed:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        run_id = profile.get("run_id")
        if not isinstance(run_id, str):
            raise MigrationRefused("source_profile")
        counts: dict[str, int] = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted = '"' + name.replace('"', '""') + '"'
            scoped = any(row[1] == "run_id" for row in db.execute(f"PRAGMA table_info({quoted})"))
            counts[name] = db.execute(f"SELECT count(*) FROM {quoted}" + (" WHERE run_id=?" if scoped else ""),
                                      (run_id,) if scoped else ()).fetchone()[0]
        if any(count and name not in RECORD_TABLES for name, count in counts.items()):
            raise MigrationRefused("unsupported_source_relations")
        exact = {"runs": 1, "run_root": 1, "run_condition_base": 1,
                 "work_jobs": 2, "work_job_layout": 1, "work_job_attempt_roots": 1,
                 "run_closeout_headers": 1,
                 "run_cleanup_items": 1, "work_job_ticket_close_intent": 1,
                 "work_job_ticket_close_ownership": 1,
                 "work_job_acceptance_record": 1, "work_job_review_budget": 1,
                 "work_job_integration_resync": 1, "work_job_ci_recovery": 1,
                 "work_job_merge_intent": 1}
        if any(counts.get(name, 0) != value for name, value in exact.items()):
            raise MigrationRefused("unsupported_source_stage")
        run = _one(db, "SELECT * FROM runs WHERE run_id=?", (run_id,))
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
                or type(job["value_modification_attempts"]) is not int or job["value_modification_attempts"] < 1
                or type(job["value_validation_attempts"]) is not int or job["value_validation_attempts"] < 1
                or job["value_candidate_sha"] is None or job["value_integrated_sha"] is None
                or job["value_publication_sha"] != job["value_integrated_sha"]
                or job["value_integrated_tree"] is None or job["value_pr_number"] is None):
            raise MigrationRefused("unsupported_source_stage")
        if any(job[field] is not None for field in (
                "value_action_application_receipt", "value_ci_repair_publication_receipt",
                "value_fallback_publication_receipt", "value_pending_publication_result",
                "value_pending_review_result", "value_publication_authority")):
            raise MigrationRefused("unsupported_source_write_authority")
        if job["value_pending_attempt"] not in (None, 0) or job["value_pending_attempt_kind"] is not None:
            raise MigrationRefused("source_attempt_history")
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
                or not isinstance(profile.get("bindings"), list)):
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
        ci = _one(db, "SELECT * FROM work_job_ci_recovery WHERE run_id=? AND job_id=?",
                  (run["run_id"], job["job_id"]))
        if (budget["value_window"] != 1
                or budget["value_development_attempts"] != job["value_modification_attempts"]
                or budget["value_reviewer_invocations"] != job["value_validation_attempts"]
                or not 1 <= budget["value_development_attempts"] <= policy.ticket_rounds
                or not 1 <= budget["value_reviewer_invocations"] <= policy.ticket_rounds
                or budget["value_final_ci_fix_used"] != 0
                or resync["value_generation"] != 1 or resync["value_window"] != 1
                or resync["value_used"] != 0 or resync["value_limit"] != policy.resync_limit
                or resync["value_open"] != 0 or resync["value_source_sha"] != base["value_sha"]
                or resync["value_target_sha"] != base["value_sha"]):
            raise MigrationRefused("unsupported_source_budget")
        if (counts.get("work_job_budget_events", 0) or ci["value_window"] != 1
                or any(ci[field] != 0 for field in ("value_developments_used", "value_repair_pending",
                                                  "value_review_required", "value_reviewer_invocations"))
                or ci["value_failure_head"] is not None or ci["value_previous_publication_authorization"] is not None):
            raise MigrationRefused("unsupported_source_ci_history")
        roots = _one(db, "SELECT * FROM work_job_attempt_roots WHERE run_id=? AND job_id=?",
                     (run["run_id"], job["job_id"]))
        history = db.execute("SELECT * FROM work_job_attempt_history WHERE run_id=? AND job_id=? ORDER BY position",
                             (run["run_id"], job["job_id"])).fetchall()
        if (roots["pending_present"] != 0 or not history or roots["history_count"] != len(history)
                or [row["position"] for row in history] != list(range(len(history)))
                or len({row["attempt_id"] for row in history}) != len(history)
                or any(counts.get(name, 0) != len(history) for name in (
                    "work_attempts", "work_attempt_current", "work_attempt_returns", "work_job_attempt_history"))):
            raise MigrationRefused("source_attempt_history")
        calls: list[dict[str, Any]] = []
        version_total = member_total = 0
        role_ordinals = {"development": 0, "reviewer": 0, "publication": 0}
        for witness in history:
            attempt_id = witness["attempt_id"]
            attempt = _one(db, "SELECT * FROM work_attempts WHERE run_id=? AND attempt_id=?",
                           (run["run_id"], attempt_id))
            old_role = attempt["role"]
            if old_role not in role_ordinals:
                raise MigrationRefused("unsupported_source_attempt_role")
            role_ordinals[old_role] += 1
            role = {"development": Role.DEVELOPMENT, "reviewer": Role.REVIEW,
                    "publication": Role.PUBLICATION}[old_role]
            current = _one(db, "SELECT * FROM work_attempt_current WHERE run_id=? AND attempt_id=?",
                           (run["run_id"], attempt_id))
            version = _one(db, "SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?",
                           (run["run_id"], attempt_id, current["version_id"]))
            returned = _one(db, "SELECT * FROM work_attempt_returns WHERE run_id=? AND attempt_id=?",
                            (run["run_id"], attempt_id))
            if (attempt["job_id"] != job["job_id"] or attempt["work_subject"] != f"ticket:{ticket}"
                    or attempt["generation"] != 1 or attempt["ordinal"] != role_ordinals[old_role]
                    or attempt["budget_window"] != (None if role == Role.PUBLICATION else budget["value_window"])
                    or version["status"] != "completed" or witness["version_id"] != current["version_id"]
                    or version["outcome"] != {Role.DEVELOPMENT: "candidate", Role.REVIEW: "acceptance_artifact",
                                               Role.PUBLICATION: "publication_artifact"}[role]):
                raise MigrationRefused("source_attempts")
            raw = _completed_evidence(db, run["run_id"], returned["evidence_id"],
                                      owner=f"attempt:{attempt_id}", kind="returned_result")
            wire_publication = (role == Role.PUBLICATION and isinstance(raw, dict)
                                and raw.get("kind") == "publication_wire")
            if (not isinstance(raw, dict)
                    or not wire_publication and (raw.get("kind") != role.value
                                                  or not isinstance(raw.get("thread_id"), str))):
                raise MigrationRefused("source_returned_result")
            value: Any = ({"result_kind": "development", "summary": raw.get("summary"),
                           "human_blockers": None} if role == Role.DEVELOPMENT else
                          {"checks": raw.get("artifact", {}).get("checks")}
                          if role == Role.REVIEW and isinstance(raw.get("artifact"), dict) else
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
                          "work_subject": attempt["work_subject"],
                          "thread_id": raw.get("thread_id"),
                          "old_role_ordinal": attempt["ordinal"], "returned": raw,
                          "current_version": _wire_row(dict(version)), "versions": versions,
                          "result_members": members, "artifact": normalized,
                          "raw_evidence_id": returned["evidence_id"]})
        if (role_ordinals != {"development": budget["value_development_attempts"],
                             "reviewer": budget["value_reviewer_invocations"], "publication": 1}
                or calls[-1]["role"] != "publication"):
            raise MigrationRefused("source_budget_accounting")
        if (version_total != counts["work_attempt_versions"]
                or member_total != counts["work_attempt_result_members"]):
            raise MigrationRefused("source_attempt_relations")
        physical_invocations = _settled_invocations(db, run["run_id"], calls, profile)
        review = _one(db, "SELECT * FROM work_job_review_artifacts WHERE run_id=? AND job_id=? AND artifact_id=?",
                      (run["run_id"], job["job_id"], job["value_acceptance_artifact"]))
        accepted = _one(db, "SELECT * FROM work_job_acceptance_record WHERE run_id=? AND job_id=?",
                        (run["run_id"], job["job_id"]))
        if (review["candidate_sha"] != job["value_candidate_sha"]
                or review["reviewed_base_sha"] != base["value_sha"]
                or review["artifact_id"] != job["value_acceptance_artifact"]
                or accepted["value_reviewed_candidate_sha"] != job["value_candidate_sha"]
                or accepted["value_reviewed_candidate_tree"] is None
                or accepted["value_reviewed_base_sha"] != base["value_sha"]):
            raise MigrationRefused("source_acceptance")
        # The accepted Review is identified by its original Thread, raw artifact
        # and immutable Review context, not by position in a three-call fixture.
        accepted_value = _completed_evidence(db, run["run_id"], accepted["value_artifact"],
                                             owner=f"job:{job['job_id']}", kind="acceptance_record.artifact")
        support = [(position, call) for position, call in enumerate(calls)
                   if call["role"] == "review" and call["returned"]["thread_id"] == review["reviewer_thread_id"]
                   and call["returned"].get("artifact") == accepted_value]
        if (len(support) != 1 or review["reviewer_thread_id"] != accepted["value_reviewer_thread_id"]
                or any(check.get("status") != "pass" for check in support[0][1]["artifact"]["checks"].values())):
            raise MigrationRefused("source_acceptance_attempt")
        review_position, accepted_call = support[0]
        context = _completed_evidence(db, run["run_id"], accepted_call["current_version"]["review_context_evidence_id"],
                                     owner=f"attempt:{accepted_call['attempt_id']}", kind="review_context")
        expected_identity = {"reviewed_base_sha": base["value_sha"],
                             "reviewed_candidate_sha": job["value_candidate_sha"],
                             "reviewed_candidate_tree": accepted["value_reviewed_candidate_tree"]}
        if (not isinstance(context, dict) or context.get("facts", {}).get("current_review_identity") != expected_identity
                or context.get("binding", {}).get("effective_revision") != job["value_effective_revision"]
                or context.get("binding", {}).get("base_sha") != base["value_sha"]
                or context.get("binding", {}).get("candidate_sha") != job["value_candidate_sha"]
                or accepted["value_effective_revision"] != job["value_effective_revision"]):
            raise MigrationRefused("source_acceptance_identity")
        accepted_call["allocation_boundary"] = {
            "base_sha": base["value_sha"], "candidate_sha": job["value_candidate_sha"],
            "candidate_tree": accepted["value_reviewed_candidate_tree"]}
        development = [call for call in calls[:review_position] if call["role"] == "development"]
        summary = _completed_evidence(db, run["run_id"], job["value_development_summary"],
                                     owner=f"job:{job['job_id']}", kind="development_summary")
        if (not development or development[-1]["returned"].get("summary") != summary
                or development[-1]["returned"]["thread_id"] != job["value_development_thread_id"]
                or calls[-1]["thread_id"] != job["value_development_thread_id"]
                or any(call["role"] == "development" for call in calls[review_position + 1:])):
            raise MigrationRefused("source_development_support")
        supporting_calls = (development[-1], accepted_call, calls[-1])
        for field, kind, expected in (
                ("value_linked_branch_display", "linked_branch_display", {"status": "linked"}),
                ("value_branch_publication_observation", "branch_publication_observation",
                 {"disposition": "published", "branch": job["value_ticket_branch"],
                  "head_sha": job["value_integrated_sha"]})):
            observed_write = _completed_evidence(db, run["run_id"], job[field],
                                                owner=f"job:{job['job_id']}", kind=kind)
            if not isinstance(observed_write, dict) or any(observed_write.get(key) != value for key, value in expected.items()):
                raise MigrationRefused("unsupported_source_effect_outcome")
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
        # Validate the complete evidence graph; the uniform record carrier
        # preserves the original material once, including superseded versions.
        for row in db.execute("SELECT * FROM work_evidence WHERE run_id=? ORDER BY evidence_id", (run["run_id"],)):
            _completed_evidence(db, run["run_id"], row["evidence_id"], owner=row["owner_id"], kind=row["kind"])
        timeline = tuple(dict(row) for row in db.execute(
            "SELECT e.* FROM run_timeline_slots s JOIN run_timeline_events e "
            "ON e.run_id=s.run_id AND e.event_id=s.event_id "
            "WHERE s.run_id=? AND s.stream='timeline' ORDER BY s.position", (run["run_id"],)))
        slots = db.execute("SELECT * FROM run_timeline_slots WHERE run_id=? ORDER BY stream,position",
                           (run["run_id"],)).fetchall()
        for stream in {slot["stream"] for slot in slots}:
            if [slot["position"] for slot in slots if slot["stream"] == stream] != list(range(sum(slot["stream"] == stream for slot in slots))):
                raise MigrationRefused("source_history")
        if (not timeline or timeline[-1]["value_phase"] != "completed"
                or len({slot["event_id"] for slot in slots}) != counts["run_timeline_events"]):
            raise MigrationRefused("source_history")
        return _Completed(run["run_id"], run["repository"], run["parent_number"],
                          parent_revision, graph_revision, _created_at_epoch(run["created_at"]),
                          root["value_language"], requirements, policy, inputs,
                          root["value_run_branch"], base["value_branch"], base["value_sha"],
                          ticket, job["value_ticket_branch"], job["value_candidate_sha"],
                          accepted["value_reviewed_candidate_tree"], job["value_integrated_sha"],
                          job["value_integrated_tree"], job["value_pr_number"], job["revision"],
                          _wire_row(dict(job)), _wire_row(dict(cleanup)), supporting_calls,
                          timeline, _wire_row(dict(close_intent)),
                          _wire_row(dict(close_owner)), budget["value_development_attempts"],
                          budget["value_reviewer_invocations"], physical_invocations)
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
    """Import a copied, offline single-Ticket completion and its source records.

    The supplied old artifacts are from a finished, observed runtime. This
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
    if (not isinstance(observed.get("backend"), str)
            or observed.get("copied_database_sha256") != snapshot.sha256
            or observed.get("source_database_sha256") != snapshot.sha256
            or observed.get("profile_present") is not True
            or observed.get("profile_sha256") != profile_sha
            or observed.get("github_sha256") != github_sha
            or observed.get("original_report_sha256") != report_sha
            or report.get("status") != "ticket_completed"
            or observed.get("external_effects") != report.get("mutations")):
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
                or report.get("raw_backend_calls") != {"development": facts.developments_used,
                                                        "review": facts.reviews_used, "publication": 1}
                or (facts.physical_invocations and observed.get("old_runtime_exit") != {"returncode": 0, "reaped": True})
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
                                     Commit(facts.base_sha, base_tree),
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
                tx.execute("UPDATE budgets SET developments_used=?,reviews_used=? "
                           "WHERE subject=? AND purpose='requirements' AND window=1",
                           (facts.developments_used, facts.reviews_used, subject))
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
                for index, call in enumerate(facts.attempts, start=1):
                    allocation = call.get("allocation_boundary", {})
                    allocated_candidate = allocation.get("candidate_sha")
                    allocated_tree = allocation.get("candidate_tree")
                    context_value: dict[str, Any] = {"source": "v6-completed-semantic-attempt",
                                                     "old_role_ordinal": call["old_role_ordinal"],
                                                     "old_current_version": call["current_version"],
                                                     "original_allocation_boundary": allocation,
                                                     "completion_identity": {
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
                                allocated_candidate, allocated_tree, allocated_tree,
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
                    "old_physical_invocations": facts.physical_invocations,
                    "old_profile_bindings": len(profile["bindings"]),
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
            profile_body = profile_snapshot.read_bytes()
            if hashlib.sha256(profile_body).hexdigest() != profile_sha:
                raise MigrationRefused("source_document_changed")
            _preserve_copy(copied, kernel, run_id=facts.run_id, subject=subject,
                           profile_document=profile_body)
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
