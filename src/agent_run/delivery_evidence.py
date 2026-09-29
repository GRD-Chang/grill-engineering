from __future__ import annotations

"""Ticket/Parent publication-currentness evidence invalidation.

Callers retain Git ordering, base selection and lifecycle transitions. Agent
revision drift still uses each adapter's stale disposition, not this reset.
"""

from typing import Any

from agent_run.required_checks_observation import clear_required_checks_observation


def clear_stale_delivery_evidence(job: dict[str, Any]) -> None:
    """Discard transient candidate evidence while retaining identity and budgets."""
    clear_required_checks_observation(job)
    for key in (
        "external_wait",
        "supervision_wait",
        "candidate_sha",
        "publication",
        "publication_sha",
        "acceptance_artifact",
        "acceptance_record",
        "publication_attempts",
        "publication_thread_id",
        "publication_operation_retry",
        "last_publication_error",
        "repair_source",
        "ci_evidence",
        "required_checks_origin",
        "publication_authority",
        "fallback_publication_receipt",
        "ci_repair_publication_receipt",
        "deterministic_integration_record",
        "next_attempt_kind",
        "last_review_candidate_sha",
        "final_ci_fix_failure_head",
    ):
        job.pop(key, None)
