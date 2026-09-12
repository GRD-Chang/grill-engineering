from __future__ import annotations

"""Thread identity bookkeeping for Change Delivery workers."""

from typing import Any

from agent_run.change_delivery_state import require_string_list
from agent_run.delivery_policy import policy_snapshot_for_state


def select_development_thread(
    state: dict[str, Any], job: dict[str, Any], *, new_attempt: bool
) -> None:
    """Rotate only at allocation of semantic work, before the durable save."""

    policy = policy_snapshot_for_state(state).get("development_thread_policy", "reuse")
    if not new_attempt or policy != "new-per-attempt":
        return
    current = job.get("development_thread_id")
    if isinstance(current, str) and current:
        history = require_string_list(job, "development_thread_history")
        if current not in history:
            history.append(current)
        job["development_thread_history"] = history
    job["development_thread_id"] = None
    # The new Attempt has no failed invocation to resume. Manual replacement
    # remains a separate same-Attempt operation handled by the Controller.
    job.pop("development_failure_resume", None)


def require_development_thread(
    job: dict[str, Any], thread_id: str, *, requested_thread: object
) -> None:
    """A fresh worker cannot return a historical developer or reviewer ID."""

    if thread_id in require_string_list(job, "reviewer_thread_ids"):
        raise ValueError(
            "Change Job Development Thread is not independent: "
            "Development cannot reuse a Reviewer Thread"
        )
    if requested_thread is None and thread_id in require_string_list(
        job, "development_thread_history"
    ):
        raise ValueError(
            "Change Job Development Thread is not independent: "
            "Fresh Development cannot reuse a historical Thread"
        )


def record_development_thread(
    job: dict[str, Any], thread_id: str, replaced_thread_id: str | None
) -> None:
    if not thread_id.strip():
        raise ValueError("Development Thread ID is empty")
    current = job.get("development_thread_id")
    if current is not None and current != thread_id:
        if replaced_thread_id != current:
            raise ValueError(
                "Development Thread changed without a verified replacement"
            )
        history = require_string_list(job, "development_thread_history")
        history.append(str(current))
        job["development_thread_history"] = history
    job["development_thread_id"] = thread_id


def record_reviewer(
    job: dict[str, Any], thread_id: str, *, new_thread: bool = False
) -> None:
    history = require_string_list(job, "development_thread_history")
    development_ids = {str(job.get("development_thread_id", "")), *history}
    reviewers = require_string_list(job, "reviewer_thread_ids")
    if thread_id in development_ids:
        raise ValueError("Fresh Acceptance cannot reuse the Development Thread")
    resumed = bool(
        job.get("review_human_blocker_resume")
        or job.get("review_failure_resume")
        or job.get("review_resume_thread_id")
    )
    if resumed and not new_thread and thread_id != latest_reviewer_thread(job):
        raise ValueError("Human Blocker resume requires the latest Reviewer Thread")
    if not thread_id.strip() or (thread_id in reviewers and not resumed):
        raise ValueError("Fresh Acceptance requires a new Reviewer Thread")
    if thread_id not in reviewers:
        reviewers.append(thread_id)
    job["reviewer_thread_ids"] = reviewers


def latest_reviewer_thread(subject: dict[str, Any]) -> str | None:
    """Return the Reviewer Thread eligible for a same-Thread resume."""

    resume_thread = subject.get("review_resume_thread_id")
    if isinstance(resume_thread, str) and resume_thread.strip():
        return resume_thread
    threads = subject.get("reviewer_thread_ids")
    if isinstance(threads, list) and threads and isinstance(threads[-1], str):
        return threads[-1]
    return None
