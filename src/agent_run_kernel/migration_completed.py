"""Offline completed-Ticket import with lossless history and proved authority.

Historical records grant no execution. Only the current Completion's supporting
semantic Attempts and current allowance become native operational relations.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Callable, Mapping, TypeVar

from agent_run.managed_workspace import ManagedWorkspace

from .effects import LocalGit
from .kernel import Kernel
from .migration import (MigrationRefused, SourceSnapshot, _created_at_epoch,
                        _require_no_v6_notification_journal, capture_v6, validate_snapshot)
from .migration_active import _evidence, _read_profile, _wire_row
from .migration_conversion import _git, _one, _policy
from .migration_records import RECORD_TABLES, V6RecordReader, _preserve_copy
from .migration_quiescence import read_v6_host_exit
from .migration_facts import (ImportSource, ImportedAttempt, ImportedBudget, ImportedCompletion,
    ImportedHistoryEvent, ImportedResource, ImportedRunFacts, ImportedSubject, HistoricalExecution, SourceRecord)
from .model import (Blocker, Commit, CreationInputs, Delivery, DeliveryKind, Issue,
                    KernelError, ParentIssue, Policy, Profile, Purpose, Requirements, Role,
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
    publication_sha: str
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
    close_readproof: dict[str, Any] | None = None
    pending_resources: bool = False
    observed_default_sha: str | None = None


def _external_close_proof(readproof: Mapping[str, Any], *, run_id: str, repository: str,
                          ticket: int, pr_number: int, publication_sha: str,
                          integrated_sha: str, base_sha: str, run_branch: str,
                          ticket_branch: str, base_branch: str, source_sha: str,
                          intent: Mapping[str, Any], requirements: Requirements,
                          runner_identity: Mapping[str, Any],
                          proved_run_head: str | None = None) -> dict[str, Any]:
    """Bind original intent/merge authority to observed external facts once.

    This is evidence validation for the shared importer, not a close retry or
    delivery phase interpreter. No original row is normalized into completion.
    """
    try:
        close = readproof['close']
        issue, actor = close['issue'], close['actor']
        binding = f'pr-{pr_number}:sha-{integrated_sha}'
        baseline = intent['value_baseline_event_id']
        login, intended = intent['value_actor'], intent['value_intent_created_at']
        if (readproof['source_sha256'] != source_sha or readproof['runner_identity'] != runner_identity
                or close['repository'] != repository or close['issue_number'] != ticket
                or issue['number'] != ticket or issue['state'] != 'closed'
                or actor['login'] != login or intent['value_dispatch_attempted'] != 1
                or intent['value_intent_binding'] != binding
                or type(baseline) is not int or baseline < 0
                or not isinstance(login, str) or not login):
            raise MigrationRefused('source_external_close')
        values = (close['actor'], close['issue'], close['comment_pages'], close['event_pages'])
        if (len(close['responses']) != 4
                or any(json.loads(response['stdout']) != value
                       for response, value in zip(close['responses'], values, strict=True))):
            raise MigrationRefused('source_external_close')
        comments = [comment for page in close['comment_pages'] for comment in page]
        events = [event for page in close['event_pages'] for event in page]
        transitions = [event for event in events if event.get('event') in {'closed', 'reopened'}]
        if any(type(event.get('id')) is not int or event['id'] < 1
               or not isinstance(event.get('created_at'), str) for event in transitions):
            raise MigrationRefused('source_external_close')
        transitions.sort(key=lambda event: (_created_at_epoch(event['created_at']), event['id']))
        after = [event for event in transitions if event['id'] > baseline]
        if (len(after) != 1 or not transitions or after[0] != transitions[-1]
                or after[0]['event'] != 'closed' or after[0].get('actor', {}).get('login') != login
                or _created_at_epoch(after[0]['created_at']) < _created_at_epoch(intended)):
            raise MigrationRefused('source_external_close')
        markers = (f'<!-- agent-run:{run_id}:ticket-{ticket}:completed -->',
                   f'<!-- agent-run:{run_id}:ticket-{ticket}:publisher-close-intent:{binding} -->',
                   f'<!-- agent-run:{run_id}:ticket-{ticket}:publisher-close-baseline:{binding}:{baseline} -->')
        matching = [comment for comment in comments
                    if comment.get('user', {}).get('login') == login
                    and comment.get('created_at') == intended
                    and isinstance(comment.get('body'), str)
                    and all(marker in comment['body'] for marker in markers)]
        if len(matching) != 1 or type(matching[0].get('id')) is not int or matching[0]['id'] < 1:
            raise MigrationRefused('source_external_close')
        for pr in (readproof['pr_before'], readproof['pr_after']):
            if (pr['number'] != pr_number or pr['state'] != 'closed' or pr['merged'] is not True
                    or pr['merge_commit_sha'] != integrated_sha or pr['user']['login'] != login
                    or pr['head']['sha'] != publication_sha or pr['head']['ref'] != ticket_branch
                    or pr['head']['repo']['full_name'] != repository
                    or pr['base']['sha'] not in ({base_sha, integrated_sha} if proved_run_head is None
                                                 else {base_sha, integrated_sha, proved_run_head})
                    or pr['base']['ref'] != run_branch
                    or pr['base']['repo']['full_name'] != repository):
                raise MigrationRefused('source_external_pr')
        expected_requirements = {
            'parent': {'number': requirements.parent.number, 'title': requirements.parent.title,
                       'body': requirements.parent.body,
                       'sub_issue_numbers': list(requirements.parent.sub_issue_numbers),
                       'sub_issue_order_reliable': requirements.parent.sub_issue_order_reliable},
            'issues': [{'number': item.number, 'title': item.title, 'body': item.body,
                        'state': 'CLOSED' if item.number == ticket else item.state, 'labels': sorted(item.labels),
                        'blocked_by': [{'number': edge.number, 'state': edge.state} for edge in item.blocked_by]}
                       for item in sorted(requirements.issues, key=lambda item: item.number)],
        }
        if readproof['requirements'] != expected_requirements:
            raise MigrationRefused('source_external_requirements')
        heads = readproof['remote_heads']
        if (set(heads) != {'refs/heads/' + branch for branch in (run_branch, ticket_branch, base_branch)}
                or heads['refs/heads/' + run_branch] != (integrated_sha if proved_run_head is None else proved_run_head)
                or not isinstance(heads['refs/heads/' + base_branch], str)
                or re.fullmatch(r'[0-9a-f]{40}', heads['refs/heads/' + base_branch]) is None
                or heads['refs/heads/' + ticket_branch] not in {publication_sha, None}):
            raise MigrationRefused('source_external_refs')
        return {'event': after[0], 'comment': matching[0],
                'close_intent': {'actor': login, 'intent_created_at': intended,
                                 'baseline_event_id': baseline, 'intent_binding': binding}}
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, MigrationRefused):
            raise
        raise MigrationRefused('source_external_close') from error


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


_CompletedFacts = TypeVar('_CompletedFacts')


def _read_completed_source(path: Path, profile: Mapping[str, Any], profile_sha: str,
                    source_sha: str, emit: Callable[..., _CompletedFacts],
                    readproof: Mapping[str, Any] | None) -> _CompletedFacts:
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
        run = _one(db, "SELECT * FROM runs WHERE run_id=?", (run_id,))
        layout = _one(db, "SELECT * FROM work_job_layout WHERE run_id=?", (run_id,))
        job = _one(db, "SELECT * FROM work_jobs WHERE run_id=? AND job_id=?", (run_id, layout['active_job_id']))
        pending_resources = run['status'] == 'waiting_external' and job['stage'] == 'merged'
        if pending_resources and readproof is None:
            raise MigrationRefused('source_external_readback_required')
        exact = {"runs": 1, "run_root": 1, "run_condition_base": 1,
                 "work_jobs": 2, "work_job_layout": 1, "work_job_attempt_roots": 1,
                 "run_closeout_headers": 0 if pending_resources else 1,
                 "run_cleanup_items": 0 if pending_resources else 1, "work_job_ticket_close_intent": 1,
                 "work_job_ticket_close_ownership": 0 if pending_resources else 1,
                 "work_job_acceptance_record": 1, "work_job_review_budget": 1,
                 "work_job_integration_resync": 1, "work_job_ci_recovery": 1,
                 "work_job_merge_intent": 1}
        if any(counts.get(name, 0) != value for name, value in exact.items()):
            raise MigrationRefused("unsupported_source_stage")
        run = _one(db, "SELECT * FROM runs WHERE run_id=?", (run_id,))
        read_v6_host_exit(db, run_id)
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
        if ((not pending_resources and (run["status"] != "ticket_completed" or job["stage"] != "completed"))
                or root["value_language"] not in {"zh", "en"}
                or job["job_id"] != f"ticket:{ticket}:generation:1"
                or job["owner_kind"] != "ticket" or job["owner_key"] != str(ticket)
                or selected["job_id"] != f"ticket:{ticket}:selection" or selected["stage"] is not None
                or (job["value_ticket_closed_by_run"] not in (None, 0) if pending_resources
                    else job["value_ticket_closed_by_run"] != 1)
                or job["value_ticket_branch_initialized"] != 1
                or job["value_ticket_branch"] != f"agent-run/{run['run_id']}/ticket-{ticket}"
                or job["value_publication_attempts"] != 1
                or type(job["value_modification_attempts"]) is not int or job["value_modification_attempts"] < 1
                or type(job["value_validation_attempts"]) is not int or job["value_validation_attempts"] < 1
                or job["value_candidate_sha"] is None or job["value_integrated_sha"] is None
                or job["value_publication_sha"] is None
                or job["value_integrated_tree"] is None or job["value_pr_number"] is None):
            raise MigrationRefused("unsupported_source_stage")
        if any(job[field] is not None for field in (
                "value_action_application_receipt", "value_ci_repair_publication_receipt",
                "value_fallback_publication_receipt", "value_pending_publication_result",
                "value_pending_review_result", "value_publication_authority")):
            raise MigrationRefused("unsupported_source_write_authority")
        if job["value_pending_attempt"] not in (None, 0) or job["value_pending_attempt_kind"] is not None:
            raise MigrationRefused("source_attempt_history")
        cleanup: dict[str, Any] = {}
        if not pending_resources:
            closeout = _one(db, "SELECT * FROM run_closeout_headers WHERE run_id=? AND kind='cleanup'", (run_id,))
            cleanup = dict(_one(db, "SELECT * FROM run_cleanup_items WHERE run_id=?", (run_id,)))
            if (closeout["value_status"] != "completed" or cleanup["branch"] != job["value_ticket_branch"]
                    or cleanup["value_status"] != "completed"
                    or cleanup["value_expected_head_sha"] != job["value_publication_sha"]):
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
                       for value in (base["value_sha"], job["value_candidate_sha"], job["value_publication_sha"],
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
        integrated = _completed_evidence(
            db, run_id, job['value_deterministic_integration_record'],
            owner=f"job:{job['job_id']}", kind='deterministic_integration_record')
        merge = _one(db, 'SELECT * FROM work_job_merge_intent WHERE run_id=? AND job_id=?',
                     (run_id, job['job_id']))
        integration_identity = {
            'base_sha': base['value_sha'], 'candidate_sha': job['value_candidate_sha'],
            'candidate_tree': accepted['value_reviewed_candidate_tree'],
            'publication_sha': job['value_publication_sha'],
            'integrated_publication_sha': job['value_publication_sha'],
            'integrated_sha': job['value_integrated_sha'], 'integrated_tree': job['value_integrated_tree'],
            'integrated_parents': [base['value_sha']], 'pr_number': job['value_pr_number'],
        }
        if (not isinstance(integrated, dict)
                or any(integrated.get(key) != value for key, value in integration_identity.items())
                or job['value_integrated_publication_sha'] != job['value_publication_sha']
                or job['value_publication_parent_sha'] != base['value_sha']
                or job['value_integrated_tree'] != accepted['value_reviewed_candidate_tree']
                or merge['value_head_sha'] != job['value_publication_sha']
                or merge['value_base_sha'] != base['value_sha']
                or merge['value_base_branch'] != root['value_run_branch']
                or merge['value_effective_revision'] != job['value_effective_revision']):
            raise MigrationRefused('source_integration_identity')
        integrated_pr = integrated.get('pr')
        if (not isinstance(integrated_pr, dict)
                or integrated_pr.get('head_sha') != job['value_publication_sha']
                or integrated_pr.get('merge_commit_sha') != job['value_integrated_sha']
                or integrated_pr.get('number') != job['value_pr_number']
                or integrated_pr.get('base_sha') != base['value_sha']):
            raise MigrationRefused('source_integration_identity')
        for field, kind, expected in (
                ("value_linked_branch_display", "linked_branch_display", {"status": "linked"}),
                ("value_branch_publication_observation", "branch_publication_observation",
                 {"disposition": "published", "branch": job["value_ticket_branch"],
                  "head_sha": job["value_publication_sha"]})):
            observed_write = _completed_evidence(db, run["run_id"], job[field],
                                                owner=f"job:{job['job_id']}", kind=kind)
            if not isinstance(observed_write, dict) or any(observed_write.get(key) != value for key, value in expected.items()):
                raise MigrationRefused("unsupported_source_effect_outcome")
        close_intent = _one(db, "SELECT * FROM work_job_ticket_close_intent WHERE run_id=? AND job_id=?",
                            (run["run_id"], job["job_id"]))
        close_owner: dict[str, Any] = ({} if pending_resources else dict(_one(
            db, "SELECT * FROM work_job_ticket_close_ownership WHERE run_id=? AND job_id=?", (run_id, job['job_id']))))
        binding = f"pr-{job['value_pr_number']}:sha-{job['value_integrated_sha']}"
        close_readproof = None
        if readproof is not None:
            close_readproof = _external_close_proof(
                readproof, run_id=run_id, repository=run['repository'], ticket=ticket,
                pr_number=job['value_pr_number'], publication_sha=job['value_publication_sha'],
                integrated_sha=job['value_integrated_sha'], base_sha=base['value_sha'],
                run_branch=root['value_run_branch'], ticket_branch=job['value_ticket_branch'],
                base_branch=base['value_branch'], source_sha=source_sha, intent=dict(close_intent),
                requirements=requirements, runner_identity=frozen['runner_github_identity'])
        if (close_intent["value_intent_binding"] != binding or close_intent['value_dispatch_attempted'] != 1):
            raise MigrationRefused('source_close_ownership')
        if not pending_resources and (
                close_intent["value_intent_binding"] != binding
                or close_owner["value_intent_binding"] != binding
                or close_owner["value_actor"] != close_intent["value_actor"]
                or close_owner["value_event_id"] != (close_readproof['event']['id'] if close_readproof is not None
                                                      else f"{run['run_id']}:ticket-{ticket}:closed")
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
        if (not timeline or timeline[-1]["value_phase"] != ('merged' if pending_resources else 'completed')
                or len({slot["event_id"] for slot in slots}) != counts["run_timeline_events"]):
            raise MigrationRefused("source_history")
        return emit(db, run["run_id"], run["repository"], run["parent_number"],
                          parent_revision, graph_revision, _created_at_epoch(run["created_at"]),
                          root["value_language"], requirements, policy, inputs,
                          root["value_run_branch"], base["value_branch"], base["value_sha"],
                          ticket, job["value_ticket_branch"], job["value_candidate_sha"],
                          accepted["value_reviewed_candidate_tree"], job["value_publication_sha"], job["value_integrated_sha"],
                          job["value_integrated_tree"], job["value_pr_number"], job["revision"],
                          _wire_row(dict(job)), _wire_row(dict(cleanup)), supporting_calls,
                          timeline, _wire_row(dict(close_intent)),
                          _wire_row(dict(close_owner)), budget["value_development_attempts"],
                          budget["value_reviewer_invocations"], physical_invocations,
                          close_readproof, pending_resources,
                          None if readproof is None else readproof['remote_heads']['refs/heads/' + base['value_branch']])
    except sqlite3.Error as error:
        raise MigrationRefused("source_corrupt") from error
    finally:
        db.close()


def _read_completed(path: Path, profile: Mapping[str, Any], profile_sha: str,
                    source_sha: str, readproof: Mapping[str, Any] | None = None) -> _Completed:
    # Temporary writer consumer; canonical source decoding never constructs it.
    def legacy(db: sqlite3.Connection, *values: Any) -> _Completed:
        return _Completed(*values)
    return _read_completed_source(path, profile, profile_sha, source_sha, legacy, readproof)


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
                    candidate: str, publication: str, integrated: str, candidate_tree: str,
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
    for sha, tree in ((candidate, candidate_tree), (publication, integrated_tree), (integrated, integrated_tree)):
        if _git(target, "cat-file", "-t", sha) != "commit" or _git(target, "rev-parse", f"{sha}^{{tree}}") != tree:
            raise MigrationRefused("source_git_object")
    if (_git(target, "cat-file", "-t", base) != "commit"
            or any(_git(target, "show", "-s", "--format=%P", sha) != base
                   for sha in (publication, integrated))):
        raise MigrationRefused("source_git_ancestry")
    _git(target, "update-ref", "refs/heads/main", base)
    _git(target, "update-ref", run_ref, integrated)
    archive = f"refs/agent-run-kernel-migration/{run_ref.split('/')[3]}/ticket-candidate"
    _git(target, "update-ref", archive, candidate)
    _git(target, "fsck", "--full", "--no-reflogs")
    _git(target, "reset", "--hard", base)
    if _git(target, "status", "--porcelain"):
        raise MigrationRefused("copied_git_dirty")


def _verify_completed_git(snapshot: SourceSnapshot, copied: Path, destination: Path,
        root: Path, final: Path, git_store: Path, *, repository: str, run_id: str,
        observed_default_sha: str | None, base_sha: str, run_branch: str, integrated_sha: str,
        base_branch: str, ticket_branch: str, ticket: int, pending_resources: bool,
        publication_sha: str, candidate_sha: str, candidate_tree: str, integrated_tree: str) -> None:
    """Verify original resources without changing a ref, worktree or clone."""
    managed = ManagedWorkspace.for_repository(repository)
    expected_final = managed.root / 'worktrees' / run_id / 'run-publication'
    if (root != managed.repository_root or managed.open_path() != root
            or snapshot.source.resolve() != managed.state_root / 'state.sqlite3'
            or destination != managed.state_root / run_id / 'kernel.sqlite3'
            or destination.parent.resolve() != destination.parent
            or final != expected_final or final.exists() or final.is_symlink()
            or git_store != root / '.git' or git_store.resolve() != git_store):
        raise MigrationRefused('source_existing_resources')
    with closing(sqlite3.connect(copied.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('SELECT count(*) FROM runs').fetchone()[0] != 1:
            raise MigrationRefused('source_task_scope')
        task = read_v6_host_exit(db, run_id, required=True)
        control = _one(db, 'SELECT run_state_dir FROM control_tasks')
        uuid = _one(db, 'SELECT value_checkout_identity FROM run_root WHERE run_id=?',
                    (run_id,))[0]
        if (task is None or task.workspace != root
                or control['run_state_dir'] != str(managed.state_root)
                or not isinstance(uuid, str) or re.fullmatch(r'[0-9a-f]{32}', uuid) is None):
            raise MigrationRefused('source_existing_identity')
    marker = git_store / 'agent-run-checkout-id'
    if (marker.is_symlink() or not marker.is_file() or marker.stat().st_nlink != 1
            or marker.stat().st_size != 33 or marker.read_bytes() != uuid.encode('ascii') + b'\n'):
        raise MigrationRefused('source_existing_identity')
    reader = LocalGit(root)
    # The live default observation grants no ownership of a changed local ref.
    # Prove append-only ancestry from existing immutable objects; never fetch or
    # move the historical checkout to make an observation acceptable.
    if observed_default_sha is not None and observed_default_sha != base_sha:
        observed = reader.run('cat-file', '-t', observed_default_sha, check=False)
        if observed.returncode != 0 or observed.stdout.strip() != 'commit':
            raise MigrationRefused('source_external_default_object')
        if reader.run('merge-base', '--is-ancestor', base_sha,
                      observed_default_sha, check=False).returncode != 0:
            raise MigrationRefused('source_external_default_ancestry')
    for ref, sha in ((f'refs/heads/{run_branch}', integrated_sha),
                     (f'refs/heads/{base_branch}', base_sha)):
        if (reader.run('symbolic-ref', '-q', ref, check=False).returncode != 1
                or _git(root, 'rev-parse', '--verify', ref) != sha):
            raise MigrationRefused('source_existing_ref')
    ticket_ref = f'refs/heads/{ticket_branch}'
    ticket_checkout = managed.state_root / 'worktrees' / run_id / f'ticket-{ticket}'
    if (_git(root, 'rev-parse', 'HEAD') != base_sha or _git(root, 'status', '--porcelain')
            or (not pending_resources and _git(root, 'for-each-ref', '--format=%(refname)', ticket_ref))):
        raise MigrationRefused('source_existing_checkout')
    worktrees = _git(root, 'worktree', 'list', '--porcelain').strip().split('\n\n')
    expected_paths = {str(root)}
    if pending_resources:
        expected_paths.add(str(ticket_checkout))
        if (ticket_checkout.is_symlink() or not ticket_checkout.is_dir()
                or ticket_checkout.resolve() != ticket_checkout
                or reader.run('symbolic-ref', '-q', ticket_ref, check=False).returncode != 1
                or _git(root, 'rev-parse', '--verify', ticket_ref) != publication_sha
                or _git(ticket_checkout, 'symbolic-ref', '-q', 'HEAD') != ticket_ref
                or _git(ticket_checkout, 'rev-parse', 'HEAD') != publication_sha
                or _git(ticket_checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir') != str(git_store)
                or _git(ticket_checkout, 'status', '--porcelain')):
            raise MigrationRefused('source_existing_ticket_resources')
    if (len(worktrees) != len(expected_paths)
            or {entry.splitlines()[0].removeprefix('worktree ') for entry in worktrees} != expected_paths):
        raise MigrationRefused('source_existing_worktrees')
    for sha, tree in ((candidate_sha, candidate_tree),
                      (publication_sha, integrated_tree),
                      (integrated_sha, integrated_tree)):
        if (_git(root, 'cat-file', '-t', sha) != 'commit'
                or _git(root, 'rev-parse', f'{sha}^{{tree}}') != tree):
            raise MigrationRefused('source_git_object')
    if (_git(root, 'cat-file', '-t', base_sha) != 'commit'
            or any(_git(root, 'show', '-s', '--format=%P', sha) != base_sha
                   for sha in (publication_sha, integrated_sha))):
        raise MigrationRefused('source_git_ancestry')


def _existing_completed_git(snapshot: SourceSnapshot, copied: Path, facts: _Completed,
                            destination: Path, root: Path, final: Path, git_store: Path) -> None:
    _verify_completed_git(snapshot, copied, destination, root, final, git_store,
        repository=facts.repository, run_id=facts.run_id, observed_default_sha=facts.observed_default_sha,
        base_sha=facts.base_sha, run_branch=facts.run_branch, integrated_sha=facts.integrated_sha,
        base_branch=facts.base_branch, ticket_branch=facts.ticket_branch, ticket=facts.ticket,
        pending_resources=facts.pending_resources, publication_sha=facts.publication_sha,
        candidate_sha=facts.candidate_sha, candidate_tree=facts.candidate_tree, integrated_tree=facts.integrated_tree)


def convert_completed_ticket_v6(
    snapshot: SourceSnapshot, destination: Path, *, git_store: Path,
    repository_root: Path, final_checkout: Path, profile_snapshot: Path,
    github_snapshot: Path | None = None, observation_snapshot: Path | None = None,
    original_report: Path | None = None, external_snapshot: Path | None = None,
    existing_verified: bool = False,
    source_origin: Mapping[str, str] | None = None,
) -> Kernel:
    """Import a copied, offline single-Ticket completion and its source records.

    The supplied old artifacts are from a finished, observed runtime. This
    ``existing_verified`` instead verifies the exact original managed clone
    read-only and creates only an unpublished database. Neither mode retires
    source authority, publishes a Task, activates Final or launches a Host.
    """
    destination = destination.absolute()
    origin = None if source_origin is None else dict(source_origin)
    if origin is not None and not existing_verified:
        raise MigrationRefused('source_origin_resources')
    repository_root, final_checkout = repository_root.absolute(), final_checkout.absolute()
    if not existing_verified and (destination.exists() or destination.is_symlink() or repository_root.exists()
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
    if existing_verified and (destination.exists() or destination.is_symlink()
                              or not destination.parent.is_dir() or repository_root.is_symlink()):
        raise MigrationRefused('source_existing_resources')
    profile, profile_sha = _read_profile(profile_snapshot)
    readproof, readproof_sha = (None, None) if external_snapshot is None else _read_document(external_snapshot)
    if readproof is not None and (not existing_verified or origin is None
                                  or any(path is not None for path in (github_snapshot, observation_snapshot, original_report))):
        raise MigrationRefused('source_readproof_resources')
    observed: dict[str, Any] = {}
    report: dict[str, Any] = {}
    github: dict[str, Any] = {}
    observation_sha: str | None = None
    report_sha: str | None = None
    github_sha: str | None = None
    if readproof is None:
        if github_snapshot is None or observation_snapshot is None or original_report is None:
            raise MigrationRefused('source_documents_required')
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
                or (not existing_verified and destination.parent.resolve().is_relative_to(original_roots[0].resolve()))
                or (existing_verified and original_roots[0] != repository_root)):
            raise MigrationRefused("old_repository_alias")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".v6-completed-") as directory:
        copied = capture_v6(snapshot.path, Path(directory) / "source.sqlite3")
        if ((copied.sha256, copied.source_protocol, copied.schema_sha256, copied.table_rows)
                != (snapshot.sha256, snapshot.source_protocol, snapshot.schema_sha256, snapshot.table_rows)):
            raise MigrationRefused("snapshot_changed")
        facts = _read_completed(copied.path, profile, profile_sha, snapshot.sha256, readproof)
        _require_no_v6_notification_journal(snapshot, facts.run_id)
        mutations: list[Any] = []
        if readproof is None:
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
                    or pr.get("head_sha") != facts.publication_sha
                    or pr.get("integrated_sha") != facts.integrated_sha
                    or pr.get("base_sha") != facts.base_sha):
                raise MigrationRefused("source_external_pr")
            source_mutations = delivery.get("mutations")
            expected = {"run_id": facts.run_id, "ticket_number": facts.ticket,
                        "pr_number": facts.pr_number, "integrated_sha": facts.integrated_sha}
            if (not isinstance(source_mutations, list) or len(source_mutations) != 3
                    or source_mutations[:2] != [{"action": action, **expected}
                                         for action in ("completion_comment", "close_issue")]
                    or source_mutations[2] != {"action": "delete_managed_branch", "branch": facts.ticket_branch}
                    or delivery.get("ticket_close_ownership", {}).get(str(facts.ticket), {}).get("event_id")
                       != facts.close_ownership["value_event_id"]
                    or delivery["ticket_close_ownership"][str(facts.ticket)].get("intent_binding")
                       != facts.close_ownership["value_intent_binding"]
                    or delivery["ticket_close_ownership"][str(facts.ticket)].get("actor")
                       != facts.close_ownership["value_actor"]):
                raise MigrationRefused("source_external_effects")
            mutations = source_mutations
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
            if not source_refs_valid and not existing_verified:
                raise MigrationRefused("source_git_refs")
        subject = f"ticket:{facts.ticket}"
        owned_repository = False
        try:
            if existing_verified:
                _existing_completed_git(snapshot, copied.path, facts, destination,
                                        repository_root, final_checkout, git_store)
            else:
                try:
                    repository_root.mkdir()
                except FileExistsError as error:
                    raise MigrationRefused("private_destination_exists") from error
                owned_repository = True
                _copy_loose_git(git_store, repository_root,
                                run_ref=f"refs/heads/{facts.run_branch}", base=facts.base_sha,
                                candidate=facts.candidate_sha, publication=facts.publication_sha,
                                integrated=facts.integrated_sha,
                                candidate_tree=facts.candidate_tree,
                                integrated_tree=facts.integrated_tree)
            base_tree = _git(repository_root, "rev-parse", f"{facts.base_sha}^{{tree}}")
            common = repository_root / ".git"
            ticket_checkout = ManagedWorkspace.for_repository(facts.repository).state_root / 'worktrees' / facts.run_id / f'ticket-{facts.ticket}'
            spec = RunSpec(facts.run_id, facts.repository, facts.parent_number,
                           facts.parent_revision, facts.graph_revision,
                           (Delivery(subject, DeliveryKind.TICKET, facts.ticket, "source-verified",
                                     Commit(facts.base_sha, base_tree),
                                     f"refs/heads/{facts.ticket_branch}",
                                     f"refs/heads/{facts.run_branch}",
                                     checkout=str(ticket_checkout) if facts.pending_resources else '',
                                     owned_root=str(ticket_checkout.parent) if facts.pending_resources else '',
                                     common_git_dir=str(common) if facts.pending_resources else ''),
                            Delivery(f"final:{facts.run_id}", DeliveryKind.FINAL_RUN,
                                     facts.parent_number, facts.parent_revision,
                                     Commit(facts.base_sha, base_tree),
                                     f"refs/heads/{facts.run_branch}",
                                     f"refs/heads/{facts.base_branch}",
                                     checkout=str(final_checkout), owned_root=str(final_checkout.parent),
                                     common_git_dir=str(common))),
                           facts.policy, facts.language, str(repository_root), facts.inputs)
            candidate = Path(directory) / "kernel.sqlite3"
            kernel = Kernel.create(candidate, spec, requirements=facts.requirements, source_origin=origin)
            with kernel.storage.transaction() as tx:
                native = tx.one("SELECT revision,completion_revision FROM subjects WHERE id=?", (subject,))
                tx.execute("UPDATE runs SET created_at=?", (facts.created_at,))
                tx.execute("""UPDATE subjects SET phase='complete',purpose='requirements',
                    source_sha=?,candidate_sha=?,candidate_tree=?,integrated_sha=?,integrated_tree=?,
                    publication_sha=?,publication_tree=?,workspace_sha=?,expected_tree=?,
                    integration_target=?,pr_number=?,gate_mode='review',merge_sha=?,
                    review_attempt=?,current_attempt=NULL WHERE id=?""",
                           (facts.publication_sha, facts.candidate_sha, facts.candidate_tree,
                            facts.integrated_sha, facts.integrated_tree, facts.publication_sha,
                            facts.integrated_tree, facts.publication_sha, facts.candidate_tree,
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
                if origin is None:
                    tx.execute("DELETE FROM history")
                else:
                    tx.execute("DELETE FROM history WHERE kind!='v6_original_cutover' OR identity!=?",
                               (origin['nonce'],))
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
                for index, operation in enumerate(('completion_comment', 'close')):
                    effect_id = f"v6-import:{facts.run_id}:{facts.ticket}:{operation}"
                    if facts.close_readproof is None:
                        payload = mutations[index]
                        receipt_body = {
                            'mutation': payload, 'close_intent': facts.close_intent,
                            'close_ownership': facts.close_ownership if operation == 'close' else None,
                            'source': 'old-github-fixture-and-v6-close-ledger'}
                    else:
                        close_proof = facts.close_readproof
                        close_intent = {
                            **close_proof['close_intent'], 'run_id': facts.run_id, 'subject': subject,
                            'issue': facts.ticket, 'merge_sha': facts.integrated_sha,
                            'effect_id': f'v6-import:{facts.run_id}:{facts.ticket}:completion_comment',
                            'pr_number': facts.pr_number, 'kind': 'ticket',
                            'comment_id': close_proof['comment']['id'],
                            'comment_created_at': close_proof['comment']['created_at'],
                            'comment_body': close_proof['comment']['body'],
                        }
                        payload = {'issue': facts.ticket, 'merge_sha': facts.integrated_sha,
                                   'kind': 'ticket', 'close_intent': close_intent}
                        receipt_body = {'disposition': 'target', 'commit': None, 'pr': None,
                                        'raw': {**close_proof, 'close_intent': close_intent,
                                                'source': 'v6-intent-and-external-readproof'}}
                    receipt = tx.put(subject, 'v6_confirmed_effect_receipt', wire_json(receipt_body))
                    payload_id = tx.put(subject, 'v6_confirmed_effect_payload', wire_json(payload))
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
                                facts.close_intent['value_actor'], payload_id, receipt))
                    receipt_ids.append(effect_id)
                tx.execute("INSERT INTO completions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (subject, 1, native["revision"], facts.parent_revision,
                            facts.graph_revision, facts.publication_sha, facts.integrated_sha,
                            facts.integrated_tree, facts.pr_number, "review",
                            facts.attempts[1]["attempt_id"], facts.attempts[0]["attempt_id"],
                            *receipt_ids))
                identity = tx.put(subject, "v6_completed_import_identity", wire_json({
                    "source_sha256": snapshot.sha256, "profile_sha256": profile_sha,
                    "github_sha256": github_sha, "observation_sha256": observation_sha,
                    "original_report_sha256": report_sha,
                    "old_backend": observed.get('backend'),
                    "old_physical_invocations": facts.physical_invocations,
                    "old_profile_bindings": len(profile["bindings"]),
                    "old_completed_job_revision": facts.old_job_revision,
                    "copied_repository_root": str(repository_root),
                    "resource_mode": "existing-verified" if existing_verified else "copy",
                    "candidate_sha": facts.candidate_sha,
                    "publication_sha": facts.publication_sha,
                    "integrated_sha": facts.integrated_sha,
                    "old_job": facts.old_job,
                    "old_cleanup": facts.old_cleanup,
                    "external_readproof_sha256": readproof_sha,
                }))
                tx.execute("INSERT INTO history(subject,kind,identity,material_id,generation,created_at) "
                           "VALUES (?,?,?,?,1,?)", (subject, "v6_completed_import", facts.run_id,
                                                   identity, kernel.clock()))
                if readproof is not None:
                    material = tx.put(subject, 'v6_external_readproof', wire_json(readproof))
                    tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) '
                               'VALUES (?,?,?,?,1,?)', (subject, 'v6_external_readproof', readproof_sha,
                                                       material, kernel.clock()))
                if facts.pending_resources:
                    tx.execute('INSERT INTO resource_cleanup(subject,generation,source_ref,source_sha,checkout,workspace_sha,owned_root,common_git_dir) '
                               'VALUES (?,1,?,?,?,?,?,?)',
                               (subject, f'refs/heads/{facts.ticket_branch}', facts.publication_sha,
                                str(ticket_checkout), facts.publication_sha,
                                str(ticket_checkout.parent), str(common)))
            profile_body = profile_snapshot.read_bytes()
            if hashlib.sha256(profile_body).hexdigest() != profile_sha:
                raise MigrationRefused("source_document_changed")
            _preserve_copy(copied, kernel, run_id=facts.run_id, subject=subject,
                           profile_document=profile_body, whole_source=origin is not None)
            if origin is not None:
                reader = V6RecordReader(kernel)
                original_root = reader.row('run_root', [facts.run_id])
                with kernel.storage.transaction() as tx:
                    anchor = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_original_cutover' AND identity=?",
                                    (origin['nonce'],))
                    imported = tx.one("SELECT subject,material_id FROM history WHERE kind='v6_completed_import'")
                    original_import = json.loads(tx.get(imported['material_id'], imported['subject']))
                    if (tx.get(anchor['material_id'], anchor['subject']) != wire_json(origin)
                            or origin['target_db'] != str(destination.resolve())
                            or origin['source_db'] != str(snapshot.source.resolve())
                            or origin['source_sha256'] != snapshot.sha256
                            or reader.source['sha256'] != snapshot.sha256
                            or original_import['source_sha256'] != snapshot.sha256
                            or original_root['value_checkout_identity'] != origin['source_uuid']
                            or original_root['value_run_branch'] != facts.run_branch):
                        raise MigrationRefused('source_origin_provenance')
                    # Transfer the authenticated old RunRoot responsibility,
                    # without claiming native workspace execution created it.
                    source_ref = 'refs/heads/' + facts.run_branch
                    ownership = tx.put(subject, 'v6_local_ref_ownership', wire_json({
                        'origin_nonce': origin['nonce'], 'source_ref': source_ref,
                        'expected_oid': facts.integrated_sha,
                        'source_record': {'table': 'run_root', 'key': [facts.run_id]},
                    }))
                    tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) '
                               'VALUES (?,?,?,?,1,?)',
                               (subject, 'v6_local_ref_ownership', source_ref, ownership, kernel.clock()))
                    if facts.pending_resources:
                        record = {'table': 'work_jobs', 'key': [facts.run_id, facts.old_job['job_id']]}
                        for kind, field, resource in (
                                ('v6_local_ref_ownership', 'source_ref', 'refs/heads/' + facts.ticket_branch),
                                ('v6_local_worktree_ownership', 'checkout', str(ticket_checkout))):
                            body = {'origin_nonce': origin['nonce'], field: resource,
                                    'expected_oid': facts.publication_sha, 'source_record': record}
                            material = tx.put(subject, kind, wire_json(body))
                            tx.execute('INSERT INTO history(subject,kind,identity,material_id,generation,created_at) '
                                       'VALUES (?,?,?,?,1,?)', (subject, kind, resource, material, kernel.clock()))
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
                                       (observation_snapshot, observation_sha), (original_report, report_sha),
                                       (external_snapshot, readproof_sha)):
                if path is None:
                    if expected_sha is not None:
                        raise MigrationRefused('source_document_changed')
                    continue
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


def read_original_completed_import(snapshot: SourceSnapshot, *, profile_snapshot: Path,
        external_snapshot: Path) -> ImportedRunFacts:
    """Decode existing original Completion/cleanup facts with external close proof.

    The old writer DTO is not an intermediate. Historical Invocation absence
    remains absence; neither the returned raw nor the reaped Host proves an
    unknown worker exited. No native Invocation, Thread or Opening is created.
    """
    validate_snapshot(snapshot)
    profile, profile_sha = _read_profile(profile_snapshot)
    proof, proof_sha = _read_document(external_snapshot)
    profile_document, external_document = profile_snapshot.read_bytes(), external_snapshot.read_bytes()
    if (hashlib.sha256(profile_document).hexdigest() != profile_sha
            or hashlib.sha256(external_document).hexdigest() != proof_sha):
        raise MigrationRefused('source_documents_changed')

    def decode(db: sqlite3.Connection, run_id: str, repository: str, parent_number: int,
            parent_revision: str, graph_revision: str, created_at: float, language: str,
            requirements: Requirements, policy: Policy, inputs: CreationInputs,
            run_branch: str, base_branch: str, base_sha: str, ticket: int, ticket_branch: str,
            candidate_sha: str, candidate_tree: str, publication_sha: str, integrated_sha: str,
            integrated_tree: str, pr_number: int, old_job_revision: int, old_job: dict[str, Any],
            old_cleanup: dict[str, Any], calls: tuple[dict[str, Any], ...], timeline: tuple[dict[str, Any], ...],
            close_intent: dict[str, Any], close_ownership: dict[str, Any], developments_used: int,
            reviews_used: int, physical_invocations: int, close_readproof: dict[str, Any] | None,
            pending_resources: bool, observed_default_sha: str | None) -> ImportedRunFacts:
        _require_no_v6_notification_journal(snapshot, run_id)
        managed = ManagedWorkspace.for_repository(repository)
        root, common = managed.repository_root, managed.repository_root / '.git'
        final = managed.root / 'worktrees' / run_id / 'run-publication'
        _verify_completed_git(snapshot, snapshot.path, managed.state_root / run_id / 'kernel.sqlite3', root, final, common,
            repository=repository, run_id=run_id, observed_default_sha=observed_default_sha,
            base_sha=base_sha, run_branch=run_branch, integrated_sha=integrated_sha,
            base_branch=base_branch, ticket_branch=ticket_branch, ticket=ticket, pending_resources=pending_resources,
            publication_sha=publication_sha, candidate_sha=candidate_sha, candidate_tree=candidate_tree,
            integrated_tree=integrated_tree)
        checkout = managed.state_root / 'worktrees' / run_id / f'ticket-{ticket}'
        base = Commit(base_sha, _git(root, 'rev-parse', base_sha + '^{tree}'))
        subject, source_ref = f'ticket:{ticket}', 'refs/heads/' + ticket_branch
        spec = RunSpec(run_id, repository, parent_number, parent_revision, graph_revision,
            (Delivery(subject, DeliveryKind.TICKET, ticket, 'source-verified', base, source_ref,
                'refs/heads/' + run_branch, checkout=str(checkout) if pending_resources else '',
                owned_root=str(checkout.parent) if pending_resources else '',
                common_git_dir=str(common) if pending_resources else ''),
             Delivery(f'final:{run_id}', DeliveryKind.FINAL_RUN, parent_number, parent_revision, base,
                'refs/heads/' + run_branch, 'refs/heads/' + base_branch, checkout=str(final),
                owned_root=str(final.parent), common_git_dir=str(common))), policy, language, str(root), inputs)
        owner = SourceRecord('work_jobs', (run_id, old_job['job_id']))
        aliases = tuple(SourceRecord('work_job_slots', (run_id, row['container'], row['slot']))
            for row in db.execute('SELECT * FROM work_job_slots WHERE run_id=? AND job_id=?', owner.key))
        aliases += (SourceRecord('work_job_layout', (run_id,)),)
        budget = _one(db, 'SELECT * FROM work_job_review_budget WHERE run_id=? AND job_id=?', owner.key)
        budgets = (ImportedBudget(owner=owner, record=SourceRecord('work_job_review_budget', owner.key),
            purpose=Purpose.REQUIREMENTS, window=budget['value_window'], developments_used=developments_used,
            reviews_used=reviews_used, current=True),)
        attempts = []
        for call in calls:
            attempt_id = call['attempt_id']
            attempt = _one(db, 'SELECT * FROM work_attempts WHERE run_id=? AND attempt_id=?', (run_id, attempt_id))
            raw = _one(db, 'SELECT m.body FROM work_evidence e JOIN work_evidence_materials m '
                'ON m.material_id=e.material_id WHERE e.run_id=? AND e.evidence_id=?', (run_id, call['raw_evidence_id']))[0]
            execution = None
            bound_version = None
            if physical_invocations:
                invocation = _one(db, 'SELECT * FROM work_invocations WHERE run_id=? AND attempt_id=?', (run_id, attempt_id))
                bound = _one(db, 'SELECT * FROM work_attempt_versions WHERE run_id=? AND attempt_id=? AND version_id=?',
                    (run_id, attempt_id, invocation['attempt_version_id']))
                binding = next(item for item in profile['bindings'] if item['binding_id'] == invocation['binding_id'])
                bound_version = wire_json(_wire_row(dict(bound)))
                execution = HistoricalExecution(record=SourceRecord('work_invocations',
                    (run_id, invocation['work_subject'], invocation['started_at'])), binding_id=invocation['binding_id'],
                    thread_id=invocation['reported_thread_id'], binding=wire_json(binding), invocation=wire_json(_wire_row(dict(invocation))))
            role = Role(call['role'])
            selected = call['current_version']
            attempts.append(ImportedAttempt(record=SourceRecord('work_attempts', (run_id, attempt_id)), role=role,
                role_ordinal=attempt['ordinal'], generation=attempt['generation'], purpose=Purpose.REQUIREMENTS,
                window=attempt['budget_window'], pending=False,
                accounted=role == Role.REVIEW and selected['budget_consumed'] == 1, outcome=selected['outcome'],
                raw_record=SourceRecord('work_evidence', (run_id, call['raw_evidence_id'])), raw_result=raw.encode('utf-8'),
                normalized_artifact=wire_json(call['artifact']), bound_version=bound_version,
                selected_version=wire_json(selected), current_version=wire_json(selected),
                allocation_boundary=wire_json(call['allocation_boundary']) if 'allocation_boundary' in call else None,
                execution=execution, continuation_basis=None))
        records = [owner, *(SourceRecord(table, owner.key) for table in (
            'work_job_ticket_close_intent', 'work_job_merge_intent', 'work_job_acceptance_record'))]
        if not pending_resources:
            records += [SourceRecord('work_job_ticket_close_ownership', owner.key),
                        SourceRecord('run_cleanup_items', (run_id, ticket_branch))]
        completion = ImportedCompletion(records=tuple(records), publication=Commit(publication_sha, integrated_tree),
            integrated=Commit(integrated_sha, integrated_tree), pr_number=pr_number,
            development_attempt=calls[0]['attempt_id'], review_attempt=calls[1]['attempt_id'],
            close_intent=wire_json(close_intent), close_ownership=None if pending_resources else wire_json(close_ownership),
            close_readproof=None if close_readproof is None else wire_json(close_readproof))
        workspace = ImportedResource(owner=owner, kind='ticket-workspace',
            disposition='existing' if pending_resources else 'absent', source_ref=source_ref,
            expected_oid=publication_sha if pending_resources else None,
            checkout=checkout if pending_resources else None, workspace_oid=publication_sha if pending_resources else None,
            owned_root=checkout.parent if pending_resources else None, common_git_dir=common,
            before_head=Path(_git(checkout, 'rev-parse', '--path-format=absolute', '--git-path', 'HEAD')).read_bytes()
                if pending_resources else None,
            checkout_archive_sha256=None, checkout_index_sha256=None, head_evidence=(owner,), cleanup_pending=pending_resources)
        integration = _one(db, 'SELECT m.body FROM work_evidence e JOIN work_evidence_materials m '
            'ON m.material_id=e.material_id WHERE e.run_id=? AND e.evidence_id=?', (run_id, old_job['value_ticket_integration']))[0]
        imported = ImportedSubject(subject=subject, issue=ticket, generation=1, record=owner, aliases=aliases,
            kind='completed', historical_base=base, budgets=budgets, attempts=tuple(attempts),
            candidate=Commit(candidate_sha, candidate_tree), integration=integration.encode('utf-8'),
            accepted_review=calls[1]['attempt_id'], completion=completion, resources=(workspace,))
        source = ImportSource(snapshot=snapshot, mode='original', scope='single-ticket',
            task=read_v6_host_exit(db, run_id, required=True),
            checkout_uuid=_one(db, 'SELECT value_checkout_identity FROM run_root WHERE run_id=?', (run_id,))[0],
            profile_document=profile_document, external_readproof=external_document,
            oracle_documents=tuple((row[0], row[1].encode('utf-8')) for row in db.execute(
                'SELECT e.evidence_id,m.body FROM work_evidence e JOIN work_evidence_materials m '
                'ON m.material_id=e.material_id WHERE e.run_id=? ORDER BY e.evidence_id', (run_id,))))
        run_resource = ImportedResource(owner=SourceRecord('runs', (run_id,)), kind='run-ref', disposition='existing',
            source_ref='refs/heads/' + run_branch, expected_oid=integrated_sha, checkout=None, workspace_oid=None,
            owned_root=None, common_git_dir=common, before_head=None, checkout_archive_sha256=None,
            checkout_index_sha256=None, head_evidence=(owner,), cleanup_pending=False)
        history = tuple(ImportedHistoryEvent(record=SourceRecord('run_timeline_events', (run_id, event['event_id'])),
            subject=subject, generation=1, kind=f"v6_{event['value_kind']}", identity=event['event_id'],
            created_at=_created_at_epoch(event['value_at']), body=wire_json({'event_id': event['event_id'],
                'presence_hex': event['presence'].hex(), 'values': {key: value for key, value in event.items()
                    if key.startswith('value_') and value is not None}})) for event in timeline)
        # The validated live readback closes the selected Issue. Keep it
        # distinct from the frozen graph's earlier OPEN observation.
        observed = proof['requirements']
        observed_requirements = Requirements.from_wire({**observed,
            'parent': {**observed['parent'], 'sub_issues': observed['parent']['sub_issue_numbers']}})
        return ImportedRunFacts(source=source, spec=spec, requirements=requirements, observed_requirements=observed_requirements,
            created_at=created_at, subjects=(imported,), run_resource=run_resource, history=history)

    result = _read_completed_source(snapshot.path, profile, profile_sha, snapshot.sha256, decode, proof)
    validate_snapshot(snapshot)
    if (_read_profile(profile_snapshot)[1] != profile_sha or _read_document(external_snapshot)[1] != proof_sha):
        raise MigrationRefused('source_documents_changed')
    return result
