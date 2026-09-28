"""Durable Parent close intent and remote transition reconciliation."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, NoReturn

from agent_run.github import GitHubReadError


def close_parent(
    *, parent_number: int, run_id: str, pr_number: int, integrated_sha: str,
    delivery_type: str, actor: str,
    read_issue: Callable[[], dict[str, Any]],
    read_events: Callable[[], list[dict[str, Any]]],
    write: Callable[[str, str | None], None],
    close_intent: dict[str, Any] | None,
    record_intent: Callable[[dict[str, Any]], None] | None,
) -> None:
    marker = f"<!-- agent-run:{run_id}:parent-completed -->"
    prefix = "<!-- agent-run:parent-close-intent:"
    binding = dict(parent_number=parent_number, run_id=run_id, pr_number=pr_number,
                   integrated_sha=integrated_sha, delivery_type=delivery_type, actor=actor)

    def fail(message: str, *, conflict: bool = False) -> NoReturn:
        raise GitHubReadError("parent_close_external_conflict" if conflict else "github_write_outcome_unknown", message)

    def save(intent: dict[str, Any]) -> None:
        if close_intent is not None:
            close_intent.update(intent)
        if record_intent is not None:
            record_intent(dict(intent))

    def own_comments(issue: dict[str, Any]) -> list[dict[str, Any]]:
        return [comment for comment in (issue.get("comments") or [])
                if isinstance(comment, dict) and isinstance(comment.get("author"), dict)
                and comment["author"].get("login") == actor
                and marker in str(comment.get("body", ""))]

    issue = read_issue()
    comments = own_comments(issue)
    intent = dict(close_intent or {})
    if not intent and comments:
        # A remote comment proves neither local dispatch boundary. Never infer
        # that this Publisher sent a close from an operation by the same actor.
        fail("Parent completion marker has no matching persisted close intent")
    if not intent:
        if issue.get("state") != "OPEN":
            fail("Parent Issue was closed without this publication's intent", conflict=True)
        events = read_events()
        intent = {**binding, "baseline_event_id": max((event["id"] for event in events), default=0),
                  "dispatch_attempted": False, "comment_dispatch_attempted": False}
        save(intent)
    # Older journals did not record comment dispatch. Its absence is unknown,
    # not permission to send another comment; a visible comment can reconcile it.
    intent.setdefault("comment_dispatch_attempted", True)
    if (any(intent.get(key) != value for key, value in binding.items())
            or type(intent.get("baseline_event_id")) is not int
            or intent["baseline_event_id"] < 0
            or type(intent.get("dispatch_attempted")) is not bool
            or type(intent.get("comment_dispatch_attempted")) is not bool):
        fail("Parent close intent belongs to a different publication or author", conflict=True)
    remote_intent = {**binding, "baseline_event_id": intent["baseline_event_id"]}
    body = (f"{marker}\nDelivery Run `{run_id}` completed this Parent Issue "
            f"in {delivery_type} PR #{pr_number}; merge commit `{integrated_sha}` "
            "is verified on the default branch.\n"
            f"{prefix}{json.dumps(remote_intent, sort_keys=True)} -->")
    if comments and not any(comment.get("body") == body for comment in comments):
        fail("Parent Issue completion marker body does not match the intent")
    if not comments:
        if (intent["comment_dispatch_attempted"] or intent["dispatch_attempted"]
                or issue.get("state") != "OPEN"):
            fail("Parent close intent comment is not visible")
        intent["comment_dispatch_attempted"] = True
        save(intent)
        write("comment", body)
        issue = read_issue()
        comments = own_comments(issue)
    matched = next((comment for comment in comments if comment.get("body") == body), None)
    if matched is None:
        fail("Parent close intent comment is not visible")
    try:
        intent_time = datetime.fromisoformat(str(matched["createdAt"]).replace("Z", "+00:00"))
        if intent_time.tzinfo is None:
            raise ValueError("missing timezone")
    except (KeyError, ValueError):
        fail("Parent close intent timestamp is not visible")

    def reconcile(observed: dict[str, Any]) -> bool:
        transitions = [event for event in read_events() if event["id"] > intent["baseline_event_id"]]
        if not transitions:
            return False
        if not intent["dispatch_attempted"]:
            fail("Parent Issue changed before this Publisher dispatched close", conflict=True)
        if len(transitions) != 1:
            fail("Parent Issue has conflicting transitions after the close intent", conflict=True)
        event = transitions[0]
        event_actor = event.get("actor")
        if not isinstance(event_actor, dict) or not event_actor.get("login"):
            fail("Parent close event author is not visible")
        if event.get("event") != "closed" or event_actor["login"] != actor:
            fail("Parent Issue was changed by an external operation", conflict=True)
        try:
            event_time = datetime.fromisoformat(str(event["created_at"]).replace("Z", "+00:00"))
            if event_time < intent_time:
                fail("Parent close event predates its intent", conflict=True)
        except (KeyError, ValueError, TypeError):
            fail("Parent close event timestamp is not visible")
        if observed.get("state") != "CLOSED":
            fail("Parent close state and event have not converged")
        if intent.get("event_id", event["id"]) != event["id"]:
            fail("Parent close event differs from the recorded event", conflict=True)
        save({**intent, "event_id": event["id"]})
        return True

    if reconcile(issue):
        return
    if issue.get("state") != "OPEN" or intent["dispatch_attempted"]:
        fail("Parent close event is not visible; reconcile before dispatching again")
    intent["dispatch_attempted"] = True
    save(intent)  # Commit the uncertain-write boundary before touching GitHub.
    write("close", None)
    if not reconcile(read_issue()):
        fail("Parent close event is not visible; reconcile before dispatching again")
