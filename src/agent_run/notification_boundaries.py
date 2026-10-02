"""Cards for every current Operator Gate and the final lifecycle result."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agent_run.delivery_facts import delivery_object_label, role_family
from agent_run.execution_timing import execution_totals
from agent_run.final_approval_operation import final_approval_cleanup_pending
from agent_run.guidance_actions import render_next_action
from agent_run.messages import selected_language
from agent_run.notification_event_facts import event_copy, event_envelope, event_subject
from agent_run.notification_presentation import next_action, pause_reason, status_term


_TITLES = {
    "human_blocker": ("human_title", "yellow"),
    "execution_failure": ("execution_failed", "red"),
    "review_budget_checkpoint": ("budget", "yellow"),
    "supervision_timeout": ("timeout", "red"),
    "operator_stopped": ("stopped", "yellow"),
    "requeue_required": ("requeue", "yellow"),
    "final_approval": ("approval", "yellow"),
    "publication_retry_exhausted": ("publication_pending", "yellow"),
    "abandonment_recovery": ("abandonment_pending", "yellow"),
}


def boundary_events(
    state: dict[str, Any], records: list[dict[str, Any]], milestones: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    language = selected_language(state)
    result = []
    for fact in actions:
        subject = fact["subject"]
        kind = fact["action_kind"]
        title_key, color = _TITLES.get(kind, ("human_title", "yellow"))
        title = event_copy(state, title_key)
        summary = event_copy(state, "separator").join(
            str(reason) if kind == "human_blocker" else pause_reason(reason, language)
            for reason in fact["reasons"]
        ) or status_term(fact["status_code"], language)
        task = event_subject(state, fact["work_subject"])
        if kind == "final_approval":
            pr_number = subject.get("pr_number")
            title = event_copy(state, "approve_number", number=pr_number) if pr_number else event_copy(state, "approval_wait")
            review = event_copy(state, "review_pass" if any(
                event["kind"] == "acceptance_passed" for event in milestones
            ) else "review_no_pass")
            checks = subject.get("required_checks_evidence") or {}
            summary += event_copy(state, "approval_summary", review=review,
                                  checks=status_term(checks.get("result") or "unknown", language))
            for check in checks.get("checks", []):
                if isinstance(check, dict) and check.get("name"):
                    summary += event_copy(state, "check_result", name=check["name"], result=status_term(
                        check.get("bucket") or check.get("state") or "unknown", language))
            if pr_number:
                task["url"] = f"https://github.com/{state.get('repository')}/pull/{pr_number}"
        # Wording, current Ticket selection and unrelated progress are not identity.
        identity = [fact["work_subject"], fact["generation"], kind, fact["reason"], fact["scope"]]
        trigger = next((record for record in reversed(records)
                        if record.get("work_subject") == fact["work_subject"]
                        and (fact["generation"] is None or record.get("generation") == fact["generation"])
                        and not record.get("event_record")), None)
        role = role_family(str(trigger.get("role"))) if trigger else None
        result.append(event_envelope(
            state, "boundary", identity, title, color, current=True,
            status_code=fact["status_code"], summary=summary,
            work_subject=fact["work_subject"], generation=fact["generation"],
            action_kind=kind, action=asdict(fact["action"]), scope=fact["scope"],
            reason=fact["reason"], evidence=fact["evidence"],
            trigger_role=event_copy(state, "role_" + role) if role and kind in {
                "human_blocker", "execution_failure", "review_budget_checkpoint",
            } else None,
            object=delivery_object_label(state, fact["location"], include_parent_for_run=True),
            next_step=render_next_action(fact["action"], state, language=language, human=True),
            **execution_totals(state, records, None), **task,
        ))
    terminal = _terminal_event(state, records)
    if terminal is not None:
        result.append(terminal)
    return result


def _terminal_event(state: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any] | None:
    status = state.get("status")
    if status not in {"completed", "abandoned"}:
        return None
    publication = (state.get("parent_job") if state.get("delivery_type") == "parent_only"
                   else state.get("run_publication")) or {}
    pr_number = publication.get("pr_number")
    summary = ""
    if status == "completed":
        summary = event_copy(state, "separator").join(part for part in (
            event_copy(state, "merged", number=pr_number) if publication.get("phase") in {"merged", "completed"} and pr_number else "",
            event_copy(state, "closed") if publication.get("parent_closed") is True or (
                state.get("delivery_type") == "parent_only" and publication.get("phase") == "completed") else "",
        ) if part)
    cleanup = status == "completed" and final_approval_cleanup_pending(state)
    title = event_copy(state, "cleanup" if cleanup else str(status))
    if cleanup:
        summary += event_copy(state, "cleanup_summary")
    timeline = list(state.get("timeline", [])) + list(state.get("timeline_continuation", []))
    at = next((point.get("at") for point in reversed(timeline) if point.get("status") == status), None)
    task = {"url": f"https://github.com/{state.get('repository')}/pull/{pr_number}"} if pr_number else {}
    return event_envelope(
        state, "boundary", "completed_cleanup" if cleanup else status, title,
        "green" if status == "completed" and not cleanup else "yellow",
        current=True, status_code=status, summary=summary,
        next_step=str(next_action(state)) if cleanup else "",
        **execution_totals(state, records, at), **task,
    )
