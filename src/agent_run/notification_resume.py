"""Present explicit Resume results without inventing progress from a request."""
from __future__ import annotations

from typing import Any

from agent_run.messages import selected_language, text
from agent_run.notification_presentation import next_action


def apply_resume_result(
    state: dict[str, Any], projected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # The lifecycle observer supplies this transient fact only after observing
    # actual work or an error. Notification recovery never reconstructs it from
    # an audit request, an exit code or an automatic invocation continuation.
    result = state.get("_manual_resume_result")
    if not isinstance(result, dict) or not result.get("id"):
        return projected
    outcome = result.get("outcome")
    if outcome not in {"started", "failed", "human_action"}:
        return projected
    if outcome == "started" and result.get("evidence") not in {
        "worker_started", "controller_progress", "controller_check",
    }:
        return projected
    from agent_run.notification_events import _base

    language = selected_language(state)
    title = text("notification.event.resume_" + str(outcome), language=language)
    summary = text(
        "notification.event.resume_" + str(result.get("evidence")) + "_summary",
        language=language,
    ) if outcome == "started" else str(result.get("reason") or "")
    color = {"started": "blue", "failed": "red", "human_action": "yellow"}[str(outcome)]
    if outcome != "started":
        # Retain the boundary identity and occurrence even when the transient
        # result disappears on a later observation or process restart.
        boundary = next((event for event in reversed(projected)
                         if event.get("current") and event.get("kind") == "boundary"
                         and event.get("status_code") not in {"completed", "abandoned", "operator_stopped"}), None)
        if boundary is not None:
            superseded = [event["id"] for event in projected if
                          event.get("kind") == "stage_end" and event.get("status_code") in {
                              "execution_failed", "review_blocked",
                          }]
            projected = [event for event in projected if event["id"] not in superseded]
            return [{**event,
                     "title": event["title"] if "approval_pending" in str(event.get("status_code")) else title,
                     "color": color,
                     "summary": (title + text("notification.event.separator", language=language)
                                 + (summary or event.get("summary", ""))),
                     "resume_id": result["id"], "resume_outcome": outcome,
                     "supersedes": superseded}
                    if event is boundary else event for event in projected]
    return [*projected, _base(
        state, "resume", [result["id"], outcome], title, color,
        summary=summary, next_step=next_action(state) if outcome != "started" else "",
        resume_id=result["id"], resume_outcome=outcome,
        live=outcome == "started",
    )]
