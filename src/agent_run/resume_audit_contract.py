"""Canonical identity and digest helpers for persisted Resume audit facts."""

from __future__ import annotations

from typing import Any

from agent_run.semantic_attempt import canonical_fingerprint


RESUME_AUDIT_KINDS = frozenset(
    {
        "agent_invocation",
        "budget_checkpoint",
        "execution_failure",
        "github_refresh_retry",
        "human_blocker",
        "supervision_timeout",
    }
)
RESUME_AUDIT_EVENT_KEYS = frozenset(
    {
        "resume_id",
        "sequence",
        "requested_at",
        "kind",
        "source_status",
        "failure_code",
        "work_subject",
        "generation",
        "semantic_attempt_id",
        "source_invocation_started_at",
        "source_invocation_status",
        "thread_id",
        "new_thread",
        "human_response_supplied",
        "successor_invocation_started_at",
        "event_digest",
    }
)


def resume_identity(run_id: str, event: dict[str, Any]) -> str:
    return canonical_fingerprint(
        {
            "run_id": run_id,
            "sequence": event.get("sequence"),
            "requested_at": event.get("requested_at"),
            "kind": event.get("kind"),
            "source_invocation_started_at": event.get(
                "source_invocation_started_at"
            ),
            "new_thread": event.get("new_thread"),
        }
    )


def resume_event_digest(event: dict[str, Any]) -> str:
    facts = {key: value for key, value in event.items() if key != "event_digest"}
    return canonical_fingerprint(facts)


def resume_history_digest(history: list[dict[str, Any]]) -> str | None:
    digest: str | None = None
    for event in history:
        digest = canonical_fingerprint(
            {"previous": digest, "event_digest": event.get("event_digest")}
        )
    return digest
