"""Retain recovery and review evidence while bounding Invocation detail."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_run.semantic_attempt import semantic_attempt_subjects
from agent_run.execution_timing import aggregate_invocation_timing


MAX_DETAILED_INVOCATIONS = 256
_DETAIL_FIELDS = ("execution_segments", "recovery_wait_intervals")


def compact_invocation_details(state: dict[str, Any]) -> None:
    """Keep all identities, results, review facts and Resume links.

    Only old, terminal, unreferenced Invocation segment/wait arrays are
    compacted. Canonical Invocations and pending Attempts always keep detail.
    There is no independent limit on evidence needed for recovery or review;
    the existing Run byte budget rejects growth rather than erasing it.
    """
    history = state.get("agent_invocation_history")
    if not isinstance(history, list) or len(history) <= MAX_DETAILED_INVOCATIONS:
        return
    protected = [state.get("active_agent_invocation")]
    invocations = state.get("ticket_agent_invocations")
    if isinstance(invocations, dict):
        protected.extend(invocations.values())
    identities = {
        (item.get("work_subject"), item.get("started_at"))
        for item in protected if isinstance(item, dict)
    }
    pending = {
        attempt.get("attempt_id")
        for owner in semantic_attempt_subjects(state)
        if isinstance((attempt := owner.get("pending_semantic_attempt")), dict)
    }
    for invocation in history[:-MAX_DETAILED_INVOCATIONS]:
        if not isinstance(invocation, dict) or invocation.get("status") not in {"completed", "failed"}:
            continue
        attempt = invocation.get("semantic_attempt")
        if ((invocation.get("work_subject"), invocation.get("started_at")) in identities
                or isinstance(attempt, dict) and attempt.get("attempt_id") in pending):
            continue
        for field in _DETAIL_FIELDS:
            details = invocation.get(field)
            if not isinstance(details, list) or not details:
                continue
            if field == "execution_segments":
                invocation["execution_timing_summary"] = aggregate_invocation_timing([invocation])
            encoded = json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            invocation[f"{field}_summary"] = {
                "count": len(details), "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            }
            invocation[field] = []
