"""Mechanical Job Generation replacement for stale Change Jobs.

The controller decides *that* a persisted currentness boundary is stale.  This
module deliberately does not infer why: it only archives the old identity and
releases a blank subject for the ordinary delivery loop to initialise from the
latest authoritative facts.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


class RequeueError(ValueError):
    """Raised when an operator asks to requeue an ineligible Run."""


def requeue_change_job(state: dict[str, Any]) -> dict[str, Any]:
    """Supersede exactly one stale Change Job and return its audit record.

    Candidate, acceptance, publication and Thread identities remain only in
    the immutable audit projection.  The retained state is intentionally
    sparse so the normal Job constructor creates a new generation-local
    branch/PR identity from the facts read at this command invocation.
    """

    if state.get("status") != "requeue_required":
        raise RequeueError("requeue is only allowed in requeue_required state")
    subject, job, container = current_change_job(state)
    if job is None or container is None:
        raise RequeueError("requeue requires exactly one current Change Job")
    record = _retired_record(subject, job)
    retired = state.setdefault("retired_job_generations", [])
    if not isinstance(retired, list):
        raise RequeueError("retired_job_generations must be an array")
    retired.append(record)

    if subject.startswith("ticket:"):
        ticket = subject.removeprefix("ticket:")
        generations = state.setdefault("retired_ticket_generations", {})
        if not isinstance(generations, dict):
            raise RequeueError("retired_ticket_generations must be an object")
        generations[ticket] = record["generation"]
        jobs = state.get("ticket_jobs")
        if not isinstance(jobs, dict):
            raise RequeueError("ticket_jobs must be an object")
        jobs.pop(ticket, None)
        state["active_ticket_job"] = None
        next_status = "active"
    elif subject.startswith("parent-only:"):
        state["retired_parent_generation"] = record["generation"]
        state.pop("parent_job", None)
        next_status = "parent_delivery_pending"
    else:
        container.pop("repair_job", None)
        container["phase"] = "pending"
        next_status = "run_acceptance_pending"

    state["active_agent_invocation"] = None
    state.pop("requeue_required", None)
    state.update(
        {
            "status": next_status,
            "terminal_kind": None,
            "diagnostics": [],
        }
    )
    return record


def current_change_job(
    state: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RequeueError("Delivery Run ID is missing")
    candidates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    active = state.get("active_ticket_job")
    if isinstance(active, dict):
        number = active.get("ticket_number")
        if isinstance(number, int):
            candidates.append((f"ticket:{number}", active, state))
    parent = state.get("parent_job")
    if isinstance(parent, dict):
        candidates.append((f"parent-only:{run_id}", parent, state))
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            candidates.append((f"run-repair:{run_id}", repair, acceptance))
    if not candidates:
        return "", None, None
    if len(candidates) != 1:
        raise RequeueError("requeue requires exactly one current Change Job")
    return candidates[0]


def _retired_record(subject: str, job: dict[str, Any]) -> dict[str, Any]:
    generation = _generation(job)
    thread_ids = sorted(_thread_ids(job))
    record: dict[str, Any] = {
        "work_subject": subject,
        "generation": generation,
        "phase": job.get("phase"),
        "effective_revision": job.get("effective_revision"),
        "base_sha": job.get("base_sha"),
        "candidate_sha": job.get("candidate_sha"),
        "pr_number": job.get("pr_number"),
        "thread_ids": thread_ids,
        "attempts": {
            key: job.get(key, 0)
            for key in (
                "modification_attempts",
                "validation_attempts",
                "publication_attempts",
            )
        },
        "had_candidate": isinstance(job.get("candidate_sha"), str),
        "had_acceptance": isinstance(job.get("acceptance_record"), dict),
        "had_publication": isinstance(job.get("publication"), dict),
        "superseded_integrations": deepcopy(
            job.get("superseded_integrations", [])
        ),
        "review_budget": deepcopy(job.get("review_budget")),
        "review_budget_history": deepcopy(job.get("review_budget_history", [])),
    }
    return record


def _thread_ids(job: dict[str, Any]) -> set[str]:
    """Project every persisted generation-local Thread identity into audit."""
    ids = {
        value
        for key, value in job.items()
        if key.endswith("thread_id") and isinstance(value, str) and value
    }
    for key in ("development_thread_history", "reviewer_thread_ids"):
        history = job.get(key)
        if isinstance(history, list):
            ids.update(value for value in history if isinstance(value, str) and value)
    return ids


def _generation(job: dict[str, Any]) -> int:
    for key in ("ticket_branch_generation", "parent_generation", "repair_generation"):
        value = job.get(key)
        if type(value) is int and value > 0:
            return value
    raise RequeueError("current Change Job has no valid generation")


def close_superseded_pull_request(
    publisher: Any, retired: dict[str, Any], close_nonce: str | None = None
) -> bool:
    """Close an old open Change PR through the Publisher-owned mutation seam."""
    pr_number = retired.get("pr_number")
    if not isinstance(pr_number, int):
        return True
    generation = retired.get("generation")
    if not isinstance(close_nonce, str) or not close_nonce:
        raise RequeueError("requeue close nonce is invalid")
    receipt_reader = getattr(publisher, "has_supersession_close_receipt", None)
    record_reader = getattr(publisher, "has_supersession_close_record", None)
    intent_reader = getattr(publisher, "has_supersession_close_intent", None)
    if (
        not callable(receipt_reader)
        or not callable(record_reader)
        or not callable(intent_reader)
    ):
        raise RequeueError("Publisher cannot verify supersession closure")
    if receipt_reader(pr_number, generation, close_nonce):
        return True
    if record_reader(pr_number, generation, close_nonce):
        # A known prior closure no longer has a matching live PR. An external
        # reopen or mutation is a Human Blocker, not permission to re-close it.
        return False
    if intent_reader(pr_number, generation, close_nonce):
        # An intent without the matching close receipt has an unknown outcome.
        return False
    publisher.record_agent_run_status(
        pr_number,
        {
            "scope": "superseded_generation",
            "generation": generation,
            "retirement": "closing",
            "close_nonce": close_nonce,
            "next_action": "superseded by explicit requeue",
        },
    )
    subject = retired.get("work_subject")
    if isinstance(subject, str) and subject.startswith("parent-only:"):
        closed = publisher.abandon_parent_pr(pr_number) is True
    else:
        closed = publisher.abandon_change_pr(pr_number) is True
    if not closed:
        return False
    publisher.record_agent_run_status(
        pr_number,
        {
            "scope": "superseded_generation",
            "generation": generation,
            "retirement": "closed",
            "close_nonce": close_nonce,
            "next_action": "superseded by explicit requeue",
        },
    )
    # Closing is not enough: a maintainer can reopen the PR before the audit
    # receipt lands. Re-read the live lifecycle after recording the receipt.
    return receipt_reader(pr_number, generation, close_nonce) is True


def remove_superseded_worktree(
    git: Any, state_root: Path, run_id: str, retired: dict[str, Any]
) -> None:
    """Remove only the old generation's disposable checkout."""
    subject = retired.get("work_subject")
    if not isinstance(subject, str):
        raise RequeueError("retired Change Job subject is invalid")
    if subject.startswith("ticket:"):
        checkout_name = f"ticket-{subject.removeprefix('ticket:')}"
    elif subject.startswith("parent-only:"):
        checkout_name = "parent"
    elif subject.startswith("run-repair:"):
        checkout_name = "run-repair"
    else:
        raise RequeueError("retired Change Job subject is invalid")
    checkout = state_root / "worktrees" / run_id / checkout_name
    if checkout.exists():
        git.remove_worktree(checkout)
