"""Explicit Change Job columns and bounded, domain-specific child records."""
from __future__ import annotations

from agent_run.sqlite_change_job_archives import TABLES as _ARCHIVE_TABLES

TEXT_FIELDS = frozenset("""
run_id owner_kind work_subject effective_revision base_sha candidate_sha publication_sha
published_sha integrated_sha reviewed_head_sha managed_checkout_head ticket_branch parent_branch
repair_branch repair_checkout default_base_sha repair_base_run_head_sha parent_revision
ticket_graph_revision repair_source repair_mode repair_seed_candidate_sha development_thread_id
publication_thread_id reviewer_resume_thread_id review_resume_thread_id pending_attempt_kind
next_attempt_kind attempt_kind publication_authority final_ci_fix_failure_head last_review_candidate_sha
integration_resume_phase integration_squash_candidate_sha selection_reason
integration_reprepare_candidate_sha integration_reprepare_default_sha integration_finding_snapshot_sha
published_integration_head_sha integrated_publication_sha publication_parent_sha
publication_prior_published_sha escalation_code execution_failure last_publication_error
acceptance_state required_checks_observation_status required_checks_observed_at publication_observed_at
published_head_sha thread_id status head_sha acceptance_attempt_id supporting_attempt_id integrated_tree run_branch_local_sha required_checks_mode
""".split())
INTEGER_FIELDS = frozenset("""
ticket_number ticket_branch_generation parent_generation repair_generation repair_attempt acceptance_generation
generation modification_attempts code_modification_attempts code_modification_limit validation_attempts
publication_attempts pending_attempt pr_number pull_number integration_conflict_agent_ordinal
run_repair_integration_conflict_ordinal
human_response_generation
""".split())
BOOLEAN_FIELDS = frozenset("""
ticket_branch_initialized development_new_thread reviewer_new_thread review_new_thread publication_new_thread
development_failure_resume review_failure_resume publication_failure_resume review_human_blocker_resume
repair_trigger_pending integration_reprepare_required integration_reprepare_discard_invocation_changes
integration_squash_conversion_required parent_closed ticket_closed_by_run source_revision_changed parent_integration_stage integration_reprepare_preserved_changes
""".split())
EVIDENCE_FIELDS = frozenset("""
policy_snapshot review_context acceptance_artifact acceptance_record previous_parent_acceptance_record
previous_run_repair_acceptance_record unresolved_acceptance_artifact repair_input_artifact publication artifact
record development_summary human_feedback ci_evidence ci_failed_evidence required_checks_evidence
required_checks_origin git_integrity_evidence merge_conflict_evidence repair_trigger
fallback_publication_receipt deterministic_integration_record ci_repair_publication_receipt
branch_publication_observation pending_publication_result pending_review_result
ticket_completion_records ticket_integration parent_integration run_integration run_repair_integration
integration_history_rewrite parent_integration_history_rewrite run_repair_integration_history_rewrite
linked_branch_display human_blockers prior_human_blockers current_human_response
diagnostics last_ticket_resume approval_update execution_activity
summary failure_evidence integration_evidence action_application_receipt integrated_message required_checks
""".split())
HISTORY_FIELDS = frozenset("""
human_blocker_history publication_operation_retry_history human_response_history pause_history
candidate_acceptance_history ticket_integration_history parent_integration_history run_integration_history
run_repair_integration_history integration_work_history
superseded_integrations merge_reconciliation_history completed_repair_jobs repair_cycle_history
""".split())
THREAD_FIELDS = frozenset("""
development_thread_history reviewer_thread_ids prior_reviewer_thread_ids discarded_repair_thread_ids
superseded_candidate_shas thread_ids
""".split())
STRING_FIELDS = frozenset({"integrated_parents", "integration_conflict_paths"})
GATE_FIELDS = frozenset({"phase", "blocked_reason", "execution_resume_phase", "pause_resume_phase", "human_blocker_phase"})
ATTEMPT_FIELDS = frozenset({"pending_semantic_attempt", "semantic_attempt_history"})
BUDGET_FIELDS = frozenset({"review_budget", "review_budget_history", "ci_recovery", "integration_development_budget",
                           "integration_resync", "repair_integration_budgets"})
CYCLE_DERIVED = frozenset({"code_modification_attempts", "validation_attempts", "development_thread_id", "worktree"})

_AUTHORITY = {key: "TEXT" for key in (
    "branch", "base_branch", "base_sha", "head_sha", "expected_remote_sha", "recovery_remote_sha", "expected_head_sha",
    "repository", "run_id", "candidate_sha", "default_base_sha", "acceptance_fingerprint", "source_sha", "target_sha")}
_WRITE = {**{key: "TEXT" for key in ("action", "branch", "base_branch", "base_sha", "head_sha", "expected_remote_sha",
          "recovery_remote_sha", "repository", "head_branch")}, "pr_number": "INTEGER",
          **{f"authority.{key}": value for key, value in _AUTHORITY.items()},
          "target_narrative": "EVIDENCE", "prior_narrative": "EVIDENCE"}
_CONFLICT = {**{key: "TEXT" for key in ("source_sha", "target_sha", "candidate_sha", "stage", "head_sha", "merge_head_sha",
             "resolution_sha", "checkout")}, "attempts": "INTEGER", "development_summary": "EVIDENCE",
             **{key: "STRINGS" for key in ("paths", "prior_paths", "conflict_paths", "resolved_stages")},
             "failure": "EVIDENCE", "human_blockers": "EVIDENCE"}
_CLOSE = {**{key: "TEXT" for key in ("run_id", "integrated_sha", "delivery_type", "actor", "intent_created_at", "intent_binding")},
          **{key: "INTEGER" for key in ("parent_number", "ticket_number", "pr_number", "baseline_event_id")}, "event_id": "IDENTITY",
          "dispatch_attempted": "BOOLEAN", "comment_dispatch_attempted": "BOOLEAN"}
_WAIT = {**{key: "TEXT" for key in ("identity", "kind", "waiting_for", "phase", "resume_action", "resume_status", "head_sha",
         "base_sha", "credential_failure_class")}, **{key: "REAL" for key in ("started_at", "deadline", "last_retry_delay_seconds", "next_retry_at")},
         **{key: "INTEGER" for key in ("retry_count", "elapsed_seconds", "budget_seconds", "credential_http_status")},
         "latest_observation": "EVIDENCE"}

# Each mapping defines one concrete record. Unknown fields are rejected rather
# than hidden in an extras/payload column.
RECORDS: dict[str, dict[str, str]] = {
    "supervision_wait": _WAIT,
    "attempts": {key: "INTEGER" for key in ("modification_attempts", "validation_attempts", "publication_attempts")},
    "candidate_commit_intent": {"kind": "TEXT", "expected_head": "TEXT", "attempt": "INTEGER", "token": "TEXT"},
    "publication_commit_intent": {"tree_sha": "TEXT", "parents": "STRINGS", "message": "EVIDENCE", "raw_commit": "EVIDENCE", "expected_sha": "TEXT"},
    "ticket_write_intent": _WRITE,
    "write_intent": _WRITE,
    "run_branch_write_intent": {key: "TEXT" for key in ("branch", "base_sha", "candidate_sha", "default_base_sha")},
    "integration_publication_intent": {key: "TEXT" for key in ("branch", "head_sha", "source_sha", "target_sha")},
    "merge_intent": {**{key: "TEXT" for key in ("head_sha", "head_branch", "base_branch", "base_sha", "effective_revision", "repository")},
                     "pr_number": "INTEGER", "attempts": "INTEGER", "commit_message": "EVIDENCE", "acceptance_boundary": "EVIDENCE"},
    "controller_candidate_reprepare": {**{key: "TEXT" for key in ("kind", "work_subject", "base_sha", "default_base_sha", "expected_head", "intent_id")},
                                      "generation": "INTEGER", "attempt": "INTEGER"},
    **{key: _CONFLICT for key in ("integration_conflict", "parent_integration_conflict", "run_integration_conflict", "run_repair_integration_conflict")},
    "parent_close_intent": _CLOSE,
    "ticket_close_intent": _CLOSE,
    "ticket_close_ownership": _CLOSE,
    "publication_operation_retry": {"attempts": "INTEGER", "limit": "INTEGER"},
    "repair_cycle": {"generation": "INTEGER", "status": "TEXT", "ended_reason": "TEXT", "promoted_candidate_sha": "TEXT", "integrated_sha": "TEXT"},
    "repair_request": {**{key: "TEXT" for key in ("repair_source", "repair_mode", "repair_candidate_sha", "development_thread_id")},
                       "development_thread_history": "STRINGS", "human_response_generation": "INTEGER", "development_new_thread": "BOOLEAN",
                       **{key: "EVIDENCE" for key in ("acceptance_artifact", "ci_evidence", "git_integrity_evidence", "human_feedback", "merge_conflict_evidence", "prior_human_blockers", "human_response_history")}},
    "approval_grant": {**{key: "TEXT" for key in ("repository", "head_branch", "head_sha", "base_branch", "base_sha", "acceptance_fingerprint", "granted_at")}, "pr_number": "INTEGER"},
    "review_budget": {"window": "INTEGER", "development_attempts": "INTEGER", "reviewer_invocations": "INTEGER", "final_ci_fix_used": "BOOLEAN", "checkpoint_reason": "TEXT", "review_artifacts": "ARTIFACTS"},
    "ci_recovery": {"window": "INTEGER", "developments_used": "INTEGER", "failure_head": "TEXT", "repair_pending": "BOOLEAN", "review_required": "BOOLEAN", "checkpoint_reason": "TEXT", "history": "EVENTS", "semantic_work_ids": "STRINGS", "previous_publication_authorization": "EVIDENCE", "reviewer_invocations": "INTEGER", "review_artifacts": "ARTIFACTS"},
    "integration_development_budget": {"window": "INTEGER", "used": "INTEGER", "limit": "INTEGER", "attempt_ids": "STRINGS", "renewals": "EVENTS"},
    "integration_resync": {"generation": "INTEGER", "window": "INTEGER", "used": "INTEGER", "limit": "INTEGER", "open": "BOOLEAN", "source_sha": "TEXT", "target_sha": "TEXT", "history": "EVENTS"},
    "external_wait": {"status": "TEXT", "terminal_kind": "TEXT", "diagnostics": "EVIDENCE",
                      **{f"{name}.{key}": value for name in ("supervision_window", "supervision_wait") for key, value in _WAIT.items()},
                      **{f"credential_availability.{key}": value for key, value in {
                          "change_job": "TEXT", "phase": "TEXT", "resume_status": "TEXT", "failure_class": "TEXT", "retry_count": "INTEGER", "http_status": "INTEGER"}.items()}},
}

JOB_FIELDS = frozenset({*TEXT_FIELDS, *INTEGER_FIELDS, *BOOLEAN_FIELDS, *EVIDENCE_FIELDS, *HISTORY_FIELDS,
                        *THREAD_FIELDS, *STRING_FIELDS, *GATE_FIELDS, *ATTEMPT_FIELDS, *BUDGET_FIELDS, *RECORDS, "repair_job",
                        "attempts", "had_candidate", "had_acceptance", "had_publication"})
ROOT_FIELDS = ("active_ticket_job", "ticket_jobs", "parent_job", "run_acceptance", "run_publication",
               "retired_parent_job", "retired_job_generations", "retired_semantic_attempt_owners",
               "retired_run_acceptance", "retired_run_publication")


def column(field: str) -> str:
    return "value_" + field.replace(".", "_")


def record_table(field: str) -> str:
    return "work_job_" + field


def _record_ddl(field: str, schema: dict[str, str]) -> str:
    columns = ", ".join(f"{column(key)} {'TEXT' if kind == 'EVIDENCE' else 'INTEGER' if kind == 'BOOLEAN' else 'BLOB' if kind == 'IDENTITY' else kind}"
                        for key, kind in schema.items() if kind not in {"STRINGS", "EVENTS", "ARTIFACTS"})
    return (f"CREATE TABLE {record_table(field)} (run_id TEXT NOT NULL, job_id TEXT NOT NULL, presence BLOB NOT NULL, nulls BLOB NOT NULL, "
            f"{columns}, PRIMARY KEY(run_id, job_id), FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)")


_CORE_COLUMNS = {**{key: "TEXT" for key in TEXT_FIELDS | EVIDENCE_FIELDS},
                 **{key: "INTEGER" for key in INTEGER_FIELDS | BOOLEAN_FIELDS},
                 **{key: "INTEGER" for key in ("had_candidate", "had_acceptance", "had_publication")}}
TABLES = {
    "work_jobs": "CREATE TABLE work_jobs (run_id TEXT NOT NULL, job_id TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_key TEXT NOT NULL, "
        "revision INTEGER NOT NULL, stage TEXT, presence BLOB NOT NULL, evidence_owner_id TEXT NOT NULL, "
        "budget_owner_id TEXT, repair_job_id TEXT, cycle_evidence_id TEXT, "
        + ", ".join(f"{column(key)} {kind}" for key, kind in sorted(_CORE_COLUMNS.items()))
        + ", PRIMARY KEY(run_id, job_id), FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "work_job_layout": "CREATE TABLE work_job_layout (run_id TEXT PRIMARY KEY, presence BLOB NOT NULL, active_job_id TEXT, "
        "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE)",
    "work_job_slots": "CREATE TABLE work_job_slots (run_id TEXT NOT NULL, container TEXT NOT NULL, slot TEXT NOT NULL, position INTEGER NOT NULL, "
        "job_id TEXT NOT NULL, PRIMARY KEY(run_id, container, slot), FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
    "work_job_gates": "CREATE TABLE work_job_gates (run_id TEXT NOT NULL, job_id TEXT NOT NULL, gate_id TEXT NOT NULL, gate_kind TEXT NOT NULL, "
        "reason TEXT, public_phase TEXT NOT NULL, resume_field TEXT, attempt_id TEXT, priority INTEGER NOT NULL, "
        "PRIMARY KEY(run_id, job_id, gate_id), FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
    "work_job_archives": "CREATE TABLE work_job_archives (run_id TEXT NOT NULL, job_id TEXT NOT NULL, history_kind TEXT NOT NULL, "
        "position INTEGER NOT NULL, evidence_id TEXT, thread_id TEXT, logical_bytes INTEGER NOT NULL, "
        "PRIMARY KEY(run_id, job_id, history_kind, position), FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
    "work_job_record_strings": "CREATE TABLE work_job_record_strings (run_id TEXT NOT NULL, job_id TEXT NOT NULL, record_kind TEXT NOT NULL, "
        "list_name TEXT NOT NULL, position INTEGER NOT NULL, text_value TEXT NOT NULL, PRIMARY KEY(run_id, job_id, record_kind, list_name, position), "
        "FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
    "work_job_windows": "CREATE TABLE work_job_windows (run_id TEXT NOT NULL, job_id TEXT NOT NULL, window INTEGER NOT NULL, snapshot_job_id TEXT NOT NULL, "
        "logical_bytes INTEGER NOT NULL, PRIMARY KEY(run_id, job_id, window), FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE, "
        "FOREIGN KEY(run_id, snapshot_job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
    "work_job_budget_events": "CREATE TABLE work_job_budget_events (run_id TEXT NOT NULL, job_id TEXT NOT NULL, budget_kind TEXT NOT NULL, "
        "list_name TEXT NOT NULL, position INTEGER NOT NULL, presence BLOB NOT NULL, kind TEXT, reason TEXT, window INTEGER, generation INTEGER, "
        "used INTEGER, grant_amount INTEGER, developments_used INTEGER, failure_head TEXT, source_sha TEXT, target_sha TEXT, renewed INTEGER, "
        "PRIMARY KEY(run_id, job_id, budget_kind, list_name, position), FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
    "work_job_review_artifacts": "CREATE TABLE work_job_review_artifacts (run_id TEXT NOT NULL, job_id TEXT NOT NULL, budget_kind TEXT NOT NULL, "
        "position INTEGER NOT NULL, presence BLOB NOT NULL, reviewer_thread_id TEXT, candidate_sha TEXT, reviewed_base_sha TEXT, expected_merge_tree TEXT, review_identity_id TEXT, artifact_id TEXT, material_id TEXT, "
        "PRIMARY KEY(run_id, job_id, budget_kind, position), FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
    "work_job_repair_budgets": "CREATE TABLE work_job_repair_budgets (run_id TEXT NOT NULL, job_id TEXT NOT NULL, repair_generation INTEGER NOT NULL, "
        "budget_job_id TEXT NOT NULL, PRIMARY KEY(run_id, job_id, repair_generation), FOREIGN KEY(run_id, job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE, "
        "FOREIGN KEY(run_id, budget_job_id) REFERENCES work_jobs(run_id, job_id) ON DELETE CASCADE)",
}
TABLES.update({record_table(field): _record_ddl(field, schema) for field, schema in RECORDS.items()})
TABLES.update(_ARCHIVE_TABLES)
INDEXES = {
    "work_jobs_owner": "CREATE INDEX work_jobs_owner ON work_jobs(run_id, owner_kind, owner_key)",
    "work_job_slots_owner": "CREATE INDEX work_job_slots_owner ON work_job_slots(run_id, job_id)",
}
