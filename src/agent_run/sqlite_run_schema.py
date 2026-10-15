"""Explicit Run identity, scheduling, fixed inputs and global conditions."""
from __future__ import annotations

METADATA = frozenset({"repository", "status", "phase", "created_at", "updated_at"})
TEXT = frozenset("""
language delivery_type run_branch parent_branch checkout_identity terminal_kind blocked_reason
execution_resume_phase pause_resume_phase human_blocker_phase github_refresh_resume_status
accepted_parent_spec_revision observed_parent_spec_revision accepted_ticket_graph_revision
""".split())
INTEGERS = frozenset("""
branch_authority_protocol review_budget_protocol delivery_policy_protocol multi_ticket_protocol
semantic_attempt_protocol human_response_audit_protocol lifecycle_action_protocol
ticket_integration_holder retired_parent_generation
""".split())
BOOLEANS = frozenset("""
currentness_resolution_pending repository_binding_pending base_resolution_pending
locator_registration_pending github_refresh_pending
""".split())
MATERIALS = frozenset({"policy_snapshot", "prompt_resources", "runner_github_identity",
                       "github_identity", "creation_configuration"})
SCHEDULING = frozenset({"frontier", "ticket_work_queue", "ticket_integration_queue",
                        "ticket_pause_requests", "retired_ticket_generations", "pending_ticket_retirements"})
WAIT = {
    **{key: "TEXT" for key in ("identity", "kind", "waiting_for", "phase", "resume_action", "resume_status",
                                "head_sha", "base_sha", "credential_failure_class")},
    **{key: "REAL" for key in ("started_at", "deadline", "last_retry_delay_seconds", "next_retry_at")},
    **{key: "INTEGER" for key in ("retry_count", "elapsed_seconds", "budget_seconds", "credential_http_status")},
    "latest_observation": "EVIDENCE",
}
RECORDS = {
    "base": {"branch": "TEXT", "sha": "TEXT"},
    "operator_stop": {"resume_status": "TEXT", "resume_terminal_kind": "TEXT", "stopped_at": "TEXT",
                      "resume_diagnostics": "DIAGNOSTICS"},
    "requeue_required": {"work_subject": "TEXT", "generation": "INTEGER", "reason": "TEXT"},
    "unsupported_scope_change": {
        "accepted_graph_revision": "TEXT", "observed_graph_revision": "TEXT", "observed_at": "TEXT",
        "observed_ticket_graph": "GRAPH", "graph_change_summary.summary": "EVIDENCE",
        **{f"graph_change_summary.{field}": "NUMBERS" for field in
           ("added_tickets", "removed_tickets", "reopened_completed_tickets")},
        "graph_change_summary.added_dependencies": "EDGES", "graph_change_summary.removed_dependencies": "EDGES",
    },
    "supervision_window": WAIT,
    "supervision_wait": WAIT,
    "credential_availability": {**{key: "TEXT" for key in ("change_job", "phase", "resume_status", "failure_class")},
                                "retry_count": "INTEGER", "http_status": "INTEGER"},
}
FIELDS = (METADATA | TEXT | INTEGERS | BOOLEANS | MATERIALS | SCHEDULING | RECORDS.keys()
          | {"run_id", "parent", "ticket_graph", "diagnostics", "requeue_transition", "run_abandonment", "delivery_cleanup"})
CORE = {**{key: "TEXT" for key in TEXT | MATERIALS},
        **{key: "INTEGER" for key in INTEGERS | BOOLEANS},
        "parent_version_id": "TEXT", "graph_snapshot_id": "TEXT"}
DIAGNOSTIC = {
    **{key: "TEXT" for key in ("code", "phase", "work_subject", "waiting_for", "resume_action", "resume_status",
                                "next_action", "credential_failure_class")},
    **{key: "INTEGER" for key in ("ticket_number", "elapsed_seconds", "budget_seconds", "retry_count", "credential_http_status")},
    **{f"operator_gate.{key}": "TEXT" for key in ("work_subject", "action_kind", "phase", "reason")},
    "action_identity.action_id": "TEXT", "action_identity.executor_generation": "INTEGER",
    "ticket_numbers": "NUMBERS",
}


def column(path: str) -> str:
    return "value_" + path.replace(".", "_")


def record_table(field: str) -> str:
    return "run_condition_" + field


_RUN_FK = "FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE"
TABLES = {
    "run_root": "CREATE TABLE run_root (run_id TEXT PRIMARY KEY, presence BLOB NOT NULL, nulls BLOB NOT NULL, "
        + ", ".join(f"{column(key)} {kind}" for key, kind in sorted(CORE.items())) + ", " + _RUN_FK + ")",
    "run_parent_versions": "CREATE TABLE run_parent_versions (run_id TEXT NOT NULL, version_id TEXT NOT NULL, "
        "presence BLOB NOT NULL, revision TEXT, title_id TEXT, body_id TEXT, PRIMARY KEY(run_id,version_id), " + _RUN_FK + ")",
    "run_graph_versions": "CREATE TABLE run_graph_versions (run_id TEXT NOT NULL, version_id TEXT NOT NULL, "
        "revision TEXT, PRIMARY KEY(run_id,version_id), " + _RUN_FK + ")",
    "run_graph_members": "CREATE TABLE run_graph_members (run_id TEXT NOT NULL, version_id TEXT NOT NULL, "
        "ticket_number INTEGER NOT NULL, PRIMARY KEY(run_id,version_id,ticket_number), "
        "FOREIGN KEY(run_id,version_id) REFERENCES run_graph_versions(run_id,version_id) ON DELETE CASCADE)",
    "run_graph_edges": "CREATE TABLE run_graph_edges (run_id TEXT NOT NULL, version_id TEXT NOT NULL, "
        "edge_id TEXT NOT NULL, ticket_number INTEGER NOT NULL, blocker_number INTEGER NOT NULL, "
        "PRIMARY KEY(run_id,version_id,edge_id), "
        "FOREIGN KEY(run_id,version_id) REFERENCES run_graph_versions(run_id,version_id) ON DELETE CASCADE)",
    "run_graph_snapshots": "CREATE TABLE run_graph_snapshots (run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, "
        "version_id TEXT NOT NULL, evidence_id TEXT NOT NULL, PRIMARY KEY(run_id,snapshot_id), "
        "FOREIGN KEY(run_id,version_id) REFERENCES run_graph_versions(run_id,version_id) ON DELETE CASCADE, "
        "FOREIGN KEY(run_id,evidence_id) REFERENCES work_evidence(run_id,evidence_id))",
    "run_graph_scope_order": "CREATE TABLE run_graph_scope_order (run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, "
        "position INTEGER NOT NULL, ticket_number INTEGER NOT NULL, PRIMARY KEY(run_id,snapshot_id,position), "
        "FOREIGN KEY(run_id,snapshot_id) REFERENCES run_graph_snapshots(run_id,snapshot_id) ON DELETE CASCADE)",
    "run_graph_ticket_slots": "CREATE TABLE run_graph_ticket_slots (run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, "
        "position INTEGER NOT NULL, ticket_number INTEGER NOT NULL, number_present INTEGER NOT NULL, "
        "PRIMARY KEY(run_id,snapshot_id,position), "
        "FOREIGN KEY(run_id,snapshot_id) REFERENCES run_graph_snapshots(run_id,snapshot_id) ON DELETE CASCADE)",
    "run_graph_blocker_slots": "CREATE TABLE run_graph_blocker_slots (run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, "
        "ticket_position INTEGER NOT NULL, position INTEGER NOT NULL, edge_id TEXT NOT NULL, number_present INTEGER NOT NULL, "
        "PRIMARY KEY(run_id,snapshot_id,ticket_position,position), "
        "FOREIGN KEY(run_id,snapshot_id,ticket_position) REFERENCES run_graph_ticket_slots(run_id,snapshot_id,position) ON DELETE CASCADE)",
    "run_frontier": "CREATE TABLE run_frontier (run_id TEXT NOT NULL, position INTEGER NOT NULL, "
        "ticket_number INTEGER NOT NULL, PRIMARY KEY(run_id,position), " + _RUN_FK + ")",
    "run_work_queue": "CREATE TABLE run_work_queue (run_id TEXT NOT NULL, position INTEGER NOT NULL, "
        "ticket_number INTEGER NOT NULL, generation INTEGER NOT NULL, role TEXT NOT NULL, lease_id TEXT, "
        "PRIMARY KEY(run_id,position), UNIQUE(run_id,ticket_number), " + _RUN_FK + ")",
    "run_integration_queue": "CREATE TABLE run_integration_queue (run_id TEXT NOT NULL, position INTEGER NOT NULL, "
        "ticket_number INTEGER NOT NULL, ready_at TEXT, ready_at_present INTEGER NOT NULL, "
        "PRIMARY KEY(run_id,position), UNIQUE(run_id,ticket_number), " + _RUN_FK + ")",
    "run_ticket_pause_requests": "CREATE TABLE run_ticket_pause_requests (run_id TEXT NOT NULL, ticket_number INTEGER NOT NULL, "
        "requested_at TEXT NOT NULL, PRIMARY KEY(run_id,ticket_number), " + _RUN_FK + ")",
    "run_ticket_generations": "CREATE TABLE run_ticket_generations (run_id TEXT NOT NULL, ticket_number INTEGER NOT NULL, "
        "generation INTEGER NOT NULL, PRIMARY KEY(run_id,ticket_number), " + _RUN_FK + ")",
    "run_ticket_retirements": "CREATE TABLE run_ticket_retirements (run_id TEXT NOT NULL, ticket_number INTEGER NOT NULL, "
        "evidence_id TEXT NOT NULL, PRIMARY KEY(run_id,ticket_number), " + _RUN_FK + ", "
        "FOREIGN KEY(run_id,evidence_id) REFERENCES work_evidence(run_id,evidence_id))",
    "run_condition_tickets": "CREATE TABLE run_condition_tickets (run_id TEXT NOT NULL, condition_kind TEXT NOT NULL, "
        "list_name TEXT NOT NULL, position INTEGER NOT NULL, ticket_number INTEGER NOT NULL, blocker_number INTEGER, "
        "PRIMARY KEY(run_id,condition_kind,list_name,position), " + _RUN_FK + ")",
    "run_diagnostic_versions": "CREATE TABLE run_diagnostic_versions (run_id TEXT NOT NULL, diagnostic_id TEXT NOT NULL, "
        "presence BLOB NOT NULL, nulls BLOB NOT NULL, report_id TEXT NOT NULL, "
        + ", ".join(f"{column(key)} {kind}" for key, kind in DIAGNOSTIC.items() if kind != "NUMBERS")
        + ", PRIMARY KEY(run_id,diagnostic_id), " + _RUN_FK + ")",
    "run_diagnostic_slots": "CREATE TABLE run_diagnostic_slots (run_id TEXT NOT NULL, container TEXT NOT NULL, "
        "position INTEGER NOT NULL, diagnostic_id TEXT NOT NULL, PRIMARY KEY(run_id,container,position), "
        "FOREIGN KEY(run_id,diagnostic_id) REFERENCES run_diagnostic_versions(run_id,diagnostic_id) ON DELETE CASCADE)",
    "run_requeue_transition": "CREATE TABLE run_requeue_transition (run_id TEXT PRIMARY KEY, presence BLOB NOT NULL, "
        "base_sha TEXT, close_nonce TEXT, source_job_id TEXT NOT NULL, source_record_id TEXT NOT NULL, "
        "FOREIGN KEY(run_id,source_job_id) REFERENCES work_jobs(run_id,job_id) ON DELETE CASCADE)",
}
for _field, _schema in RECORDS.items():
    _columns = ", ".join(f"{column(path)} {'TEXT' if kind in {'EVIDENCE', 'GRAPH'} else kind}"
                         for path, kind in _schema.items() if kind not in {"NUMBERS", "EDGES", "DIAGNOSTICS"})
    TABLES[record_table(_field)] = (f"CREATE TABLE {record_table(_field)} (run_id TEXT PRIMARY KEY, "
                                  f"presence BLOB NOT NULL, nulls BLOB NOT NULL, {_columns}, {_RUN_FK})")
INDEXES = {
    "run_parent_revision": "CREATE INDEX run_parent_revision ON run_parent_versions(run_id,revision)",
    "run_graph_revision": "CREATE INDEX run_graph_revision ON run_graph_versions(run_id,revision)",
}
