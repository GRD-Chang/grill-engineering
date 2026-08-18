"""Shared recoverable boundary for a Worker without its first read credential."""

from __future__ import annotations

from typing import Any

from agent_run.external_supervision import wait_for_github_convergence


def wait_for_initial_credential(
    state: dict[str, Any],
    *,
    work_subject: str,
    phase: str,
    resume_status: str,
) -> None:
    """Persist a sanitized ten-minute credential-availability wait.

    Callers invoke this only after catching an initial-mint failure, before a
    Worker process has started.  The shared record is consumed by foreground
    supervision and remains intentionally independent of a particular Worker
    role.
    """

    state["active_agent_invocation"] = None
    previous = state.get("credential_availability")
    retries = (
        int(previous.get("retry_count", 0))
        if isinstance(previous, dict) and previous.get("change_job") == work_subject
        else 0
    )
    state["credential_availability"] = {
        "change_job": work_subject,
        "phase": phase,
        "resume_status": resume_status,
        "failure_class": "credential_unavailable",
        "retry_count": retries,
    }
    wait_for_github_convergence(
        state,
        code="credential_availability",
        message="initial Worker read credential is temporarily unavailable",
        waiting_for=f"{work_subject} Worker credential availability",
    )


def resume_initial_credential_wait(
    state: dict[str, Any], *, work_subject: str
) -> bool:
    """Restore a matching Worker stage before retrying its initial mint."""

    availability = state.get("credential_availability")
    if not isinstance(availability, dict) or availability.get("change_job") != work_subject:
        return False
    resume_status = availability.get("resume_status")
    if isinstance(resume_status, str) and resume_status:
        state["status"] = resume_status
        state["terminal_kind"] = resume_status
    state["diagnostics"] = []
    return True


def clear_initial_credential_wait(
    state: dict[str, Any], *, work_subject: str
) -> None:
    """Clear only the matching wait after its Worker successfully starts."""

    availability = state.get("credential_availability")
    if isinstance(availability, dict) and availability.get("change_job") == work_subject:
        state.pop("credential_availability", None)
