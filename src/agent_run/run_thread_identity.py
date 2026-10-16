from __future__ import annotations

"""Authoritative Thread identities that a Run role may not reuse."""

from typing import Any
from agent_run.work_projection import materialize_history


def prior_thread_identities(
    state: dict[str, Any], run: dict[str, Any]
) -> set[str]:
    """Collect every Ticket and Run Thread reserved before a fresh role starts."""

    identities = set(_string_list(run, "reviewer_thread_ids"))
    development_thread = run.get("development_thread_id")
    if isinstance(development_thread, str):
        identities.add(development_thread)
    identities.update(_string_list(run, "development_thread_history"))
    if materialize_history(run, "discarded_repair_thread_ids") is not None:
        identities.update(_string_list(run, "discarded_repair_thread_ids"))

    repair_job = run.get("repair_job")
    if isinstance(repair_job, dict):
        reviewer_threads = materialize_history(repair_job, "reviewer_thread_ids")
        if isinstance(reviewer_threads, list):
            identities.update(
                thread for thread in reviewer_threads if isinstance(thread, str)
            )

    for job in _mapping(state, "ticket_jobs").values():
        if not isinstance(job, dict):
            continue
        development_thread = job.get("development_thread_id")
        if isinstance(development_thread, str):
            identities.add(development_thread)
        for key in ("development_thread_history", "reviewer_thread_ids"):
            values = materialize_history(job, key)
            if isinstance(values, list):
                identities.update(value for value in values if isinstance(value, str))
    return identities


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = materialize_history(data, key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"{key} must contain strings")
    return list(value)
