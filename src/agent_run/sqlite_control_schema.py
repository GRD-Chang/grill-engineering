"""Explicit control relations sharing the business state database."""
from __future__ import annotations


COLUMNS = {
    "control_tasks": (
        "task_id", "protocol", "workspace", "repository", "parent_number", "run_id",
        "run_state_dir", "next_generation", "current_action_id",
        "current_executor_action_id", "current_executor_generation", "updated_at",
    ),
    "control_actions": (
        "task_id", "action_id", "kind", "payload_digest", "status", "run_id",
        "executor_generation", "application_observed", "submitted_at", "completed_at",
        "result_status", "failure", "final_approval_operation",
        "target_executor_action_id", "target_executor_generation",
    ),
    "control_executors": (
        "task_id", "action_id", "generation", "status", "run_id", "binding_token",
        "pid", "process_start_token", "handshake_at", "started_at", "exited_at",
        "failure", "runner_binding", "ticket_resume_closed", "worker_format",
    ),
    "control_workers": (
        "task_id", "action_id", "generation", "position", "pid", "process_start_token", "started_at",
    ),
    "control_action_history": (
        "task_id", "position", "run_id", "state_dir", "action_id",
        "executor_action_id", "executor_generation",
    ),
    "control_payloads": ("task_id", "action_id", "value", "byte_length"),
}
KEYS = {
    "control_tasks": ("task_id",),
    "control_actions": ("task_id", "action_id"),
    "control_executors": ("task_id", "action_id", "generation"),
    "control_workers": ("task_id", "action_id", "generation", "position"),
    "control_action_history": ("task_id", "position"),
    "control_payloads": ("task_id", "action_id"),
}
FOREIGN_KEYS = {
    "control_tasks": {
        ("control_actions", "task_id", "task_id", "NO ACTION"),
        ("control_actions", "current_action_id", "action_id", "NO ACTION"),
        ("control_executors", "task_id", "task_id", "NO ACTION"),
        ("control_executors", "current_executor_action_id", "action_id", "NO ACTION"),
        ("control_executors", "current_executor_generation", "generation", "NO ACTION"),
    },
    "control_actions": {
        ("control_tasks", "task_id", "task_id", "CASCADE"),
        ("control_executors", "task_id", "task_id", "NO ACTION"),
        ("control_executors", "target_executor_action_id", "action_id", "NO ACTION"),
        ("control_executors", "target_executor_generation", "generation", "NO ACTION"),
    },
    "control_executors": {("control_tasks", "task_id", "task_id", "CASCADE")},
    "control_workers": {
        ("control_executors", "task_id", "task_id", "CASCADE"),
        ("control_executors", "action_id", "action_id", "CASCADE"),
        ("control_executors", "generation", "generation", "CASCADE"),
    },
    "control_action_history": {
        ("control_tasks", "task_id", "task_id", "CASCADE"),
        ("control_actions", "task_id", "task_id", "NO ACTION"),
        ("control_actions", "action_id", "action_id", "NO ACTION"),
        ("control_executors", "task_id", "task_id", "NO ACTION"),
        ("control_executors", "executor_action_id", "action_id", "NO ACTION"),
        ("control_executors", "executor_generation", "generation", "NO ACTION"),
    },
    "control_payloads": {
        ("control_actions", "task_id", "task_id", "CASCADE"),
        ("control_actions", "action_id", "action_id", "CASCADE"),
    },
}

TABLES = {
    "control_tasks": "CREATE TABLE control_tasks (task_id TEXT NOT NULL PRIMARY KEY, "
    "protocol INTEGER NOT NULL, workspace TEXT NOT NULL, repository TEXT NOT NULL, "
    "parent_number INTEGER NOT NULL, run_id TEXT, run_state_dir TEXT, next_generation INTEGER NOT NULL, "
    "current_action_id TEXT, current_executor_action_id TEXT, current_executor_generation INTEGER, updated_at TEXT NOT NULL, "
    "UNIQUE(workspace, repository, parent_number), "
    "FOREIGN KEY(task_id, current_action_id) REFERENCES control_actions(task_id, action_id) DEFERRABLE INITIALLY DEFERRED, "
    "FOREIGN KEY(task_id, current_executor_action_id, current_executor_generation) "
    "REFERENCES control_executors(task_id, action_id, generation) DEFERRABLE INITIALLY DEFERRED)",
    "control_actions": "CREATE TABLE control_actions (task_id TEXT NOT NULL, action_id TEXT NOT NULL, "
    "kind TEXT NOT NULL, payload_digest TEXT NOT NULL, status TEXT NOT NULL, run_id TEXT, executor_generation INTEGER NOT NULL, "
    "application_observed INTEGER NOT NULL CHECK(application_observed IN (0, 1)), submitted_at TEXT NOT NULL, "
    "completed_at TEXT, result_status TEXT, failure TEXT, final_approval_operation INTEGER CHECK(final_approval_operation IN (0, 1)), "
    "target_executor_action_id TEXT, target_executor_generation INTEGER, PRIMARY KEY(task_id, action_id), "
    "FOREIGN KEY(task_id) REFERENCES control_tasks(task_id) ON DELETE CASCADE, "
    "FOREIGN KEY(task_id, target_executor_action_id, target_executor_generation) "
    "REFERENCES control_executors(task_id, action_id, generation) DEFERRABLE INITIALLY DEFERRED)",
    "control_executors": "CREATE TABLE control_executors (task_id TEXT NOT NULL, action_id TEXT NOT NULL, generation INTEGER NOT NULL, "
    "status TEXT NOT NULL, run_id TEXT, binding_token TEXT, pid INTEGER, process_start_token TEXT, handshake_at TEXT, "
    "started_at TEXT, exited_at TEXT, failure TEXT, runner_binding TEXT, ticket_resume_closed INTEGER CHECK(ticket_resume_closed IN (0, 1)), "
    "worker_format INTEGER NOT NULL CHECK(worker_format IN (0, 1, 2)), PRIMARY KEY(task_id, action_id, generation), "
    "FOREIGN KEY(task_id) REFERENCES control_tasks(task_id) ON DELETE CASCADE)",
    "control_workers": "CREATE TABLE control_workers (task_id TEXT NOT NULL, action_id TEXT NOT NULL, generation INTEGER NOT NULL, "
    "position INTEGER NOT NULL, pid INTEGER NOT NULL, process_start_token TEXT NOT NULL, started_at TEXT, "
    "PRIMARY KEY(task_id, action_id, generation, position), UNIQUE(task_id, action_id, generation, pid), "
    "FOREIGN KEY(task_id, action_id, generation) REFERENCES control_executors(task_id, action_id, generation) ON DELETE CASCADE)",
    "control_action_history": "CREATE TABLE control_action_history (task_id TEXT NOT NULL, position INTEGER NOT NULL, "
    "run_id TEXT, state_dir TEXT, action_id TEXT NOT NULL, executor_action_id TEXT, executor_generation INTEGER, PRIMARY KEY(task_id, position), "
    "FOREIGN KEY(task_id) REFERENCES control_tasks(task_id) ON DELETE CASCADE, "
    "FOREIGN KEY(task_id, action_id) REFERENCES control_actions(task_id, action_id) DEFERRABLE INITIALLY DEFERRED, "
    "FOREIGN KEY(task_id, executor_action_id, executor_generation) REFERENCES control_executors(task_id, action_id, generation) "
    "DEFERRABLE INITIALLY DEFERRED)",
    "control_payloads": "CREATE TABLE control_payloads (task_id TEXT NOT NULL, action_id TEXT NOT NULL, value TEXT NOT NULL, "
    "byte_length INTEGER NOT NULL, PRIMARY KEY(task_id, action_id), "
    "FOREIGN KEY(task_id, action_id) REFERENCES control_actions(task_id, action_id) ON DELETE CASCADE)",
}
INDEXES = {"control_action_identity": "CREATE INDEX control_action_identity ON control_actions(action_id)"}
SCHEMA = (*TABLES.values(), *INDEXES.values())
