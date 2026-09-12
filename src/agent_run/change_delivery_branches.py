from __future__ import annotations

"""Change branch authority and optional linked-branch presentation."""

from typing import Any, Callable

from agent_run.delivery_protocol import GitHubPublisher


def ensure_linked_branch_display(
    *,
    github: GitHubPublisher,
    state: dict[str, Any],
    job: dict[str, Any],
    issue_number: int,
    branch: str,
    head_sha: str,
    save: Callable[[dict[str, Any]], Any],
) -> None:
    """Make the one optional Linked Branch display attempt durable.

    The ref and PR are already authoritative when this function is reached.
    A crash after recording the attempt deliberately becomes indeterminate on
    recovery instead of replaying a potentially successful GitHub mutation.
    """
    existing = job.get("linked_branch_display")
    if isinstance(existing, dict) and existing.get("display_attempted") is True:
        if existing.get("status") not in {"linked", "unavailable", "indeterminate"}:
            existing["status"] = "indeterminate"
            save(state)
        return

    display: dict[str, Any] = {
        "display_attempted": True,
        "status": "indeterminate",
    }
    job["linked_branch_display"] = display
    save(state)
    status = github.link_issue_branch_display(
        issue_number=issue_number, branch=branch, head_sha=head_sha
    )
    display["status"] = status if status in {"linked", "unavailable"} else "unavailable"
    save(state)


def ensure_change_branch_authority(
    *,
    github: GitHubPublisher,
    state: dict[str, Any],
    job: dict[str, Any],
    branch: str,
    base_branch: str,
    save: Callable[[dict[str, Any]], Any],
    ticket_number: int | None = None,
) -> None:
    """Persist exact ref intent before creating or recovering a Change ref."""
    # A persisted merge may already have advanced the target and GitHub may
    # have deleted its source ref. Let the shared delivery loop reconcile the
    # PR and commit against this intent before attempting any branch setup.
    intent = job.get("merge_intent")
    publication = job.get("publication")
    if (
        job.get("phase") in {"merging", "merged"}
        and type(job.get("pr_number")) is int
        and isinstance(intent, dict)
        and isinstance(publication, dict)
        and intent.get("head_sha") == job.get("publication_sha")
        and intent.get("base_branch") == base_branch
        and intent.get("base_sha") == job.get("base_sha")
        and intent.get("commit_message") == publication.get("commit_message")
        and intent.get("effective_revision") == job.get("effective_revision")
        and type(intent.get("attempts")) is int
        and intent["attempts"] > 0
    ):
        return
    pending = job.get("ticket_write_intent")
    expected_remote_sha = str(job.get("published_sha", job["base_sha"]))
    recovery_remote_sha = expected_remote_sha
    if isinstance(pending, dict) and pending.get("action") == "publish_ticket_ref":
        expected = pending.get("expected_remote_sha")
        head = pending.get("head_sha")
        if not isinstance(expected, str) or not isinstance(head, str):
            raise ValueError("Change publish intent has invalid ref identity")
        expected_remote_sha = expected
        recovery_remote_sha = head
    authority = {
        "branch": branch,
        "base_branch": base_branch,
        "base_sha": str(job["base_sha"]),
        "expected_remote_sha": expected_remote_sha,
        "recovery_remote_sha": recovery_remote_sha,
    }
    created_intent = pending is None
    pending_action: str | None = None
    if created_intent:
        job["ticket_write_intent"] = {
            "action": "ensure_change_branch",
            "authority": authority,
        }
        pending_action = "ensure_change_branch"
        save(state)
    elif not isinstance(pending, dict):
        raise ValueError("Change ref has an invalid write intent")
    elif pending.get("action") == "ensure_change_branch":
        pending_action = "ensure_change_branch"
        if pending.get("authority") != authority:
            raise ValueError("Change branch intent does not match durable authority")
    elif pending.get("action") == "ensure_ticket_pr":
        # The ref is already published.  Preserve the durable PR intent so
        # the shared delivery loop can recover it by exact PR readback.
        pass
    elif pending.get("action") != "publish_ticket_ref":
        raise ValueError("Change ref has an unknown write intent")
    if ticket_number is None:
        github.ensure_change_branch(
            branch=branch,
            base_branch=base_branch,
            expected_base_sha=authority["base_sha"],
            expected_remote_sha=expected_remote_sha,
            recovery_remote_sha=recovery_remote_sha,
        )
    else:
        github.ensure_ticket_branch(
            ticket_number=ticket_number,
            branch=branch,
            base_branch=base_branch,
            expected_base_sha=authority["base_sha"],
            expected_remote_sha=expected_remote_sha,
            recovery_remote_sha=recovery_remote_sha,
        )
    if created_intent or pending_action == "ensure_change_branch":
        job.pop("ticket_write_intent", None)
        save(state)
