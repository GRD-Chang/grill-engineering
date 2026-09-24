"""Read-only notification projection of the operator's semantic history.

Current boundary identities are slots: the sender assigns an occurrence when
leaving and re-entering a slot. No identity uses display text or row position.
"""
from __future__ import annotations

from agent_run.notification_event_facts import event_copy, event_envelope, event_subject

from agent_run.execution_timing import execution_totals
from agent_run.ticket_effort import ticket_effort
from agent_run.final_approval_operation import final_approval_cleanup_pending
from typing import Any

from agent_run.attempt_projection import semantic_attempt_history
from agent_run.history_facts import history_records
from agent_run.delivery_facts import role_family
from agent_run.delivery_facts import ticket_progress
from agent_run.delivery_facts import current_work_subject
from agent_run.messages import selected_language
from agent_run.notification_milestones import milestones
from agent_run.notification_presentation import status_term, pause_reason, next_action


def _round_events(state: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    role = role_family(str(record.get("role")))
    if role is None or not record.get("attempt_id"):
        return []
    attempt = record.get("attempt") or {}
    facts = {"phase": event_copy(state, "role_" + role), "round": record.get("ordinal"),
             "role": record.get("role"), "attempt_id": record["attempt_id"], **event_subject(state, record.get("work_subject"))}
    result = []
    ticket = "task_number" in facts
    title = facts.get("task_title") or (f"#{facts['task_number']}" if ticket else event_copy(state, "parent"))
    start_title = {"development": event_copy(state, "development_start", title=title), "review": event_copy(state, "review_ticket_start", title=title) if ticket else event_copy(state, "review_start"),
                   "publication": event_copy(state, "publication_ticket_start") if ticket else event_copy(state, "publication_start")}[role]
    if any(invocation.get("reported_thread_id") for invocation in record.get("invocations", [])
           if isinstance(invocation, dict)):
        result.append(event_envelope(state, "stage_start", record["attempt_id"],
                            start_title, started_at=record.get("started_at"), live=record.get("activity") == "running", **facts))
    artifact = record.get("acceptance_artifact") or {}
    checks = {value.get("status") for value in (artifact.get("checks") or {}).values()
              if isinstance(value, dict)}
    blocked_review = role == "review" and "blocked" in checks
    # A blocked review has a result even though its semantic round remains
    # pending for human recovery. Process exit alone still proves no result.
    stopped = record["status_code"] == "execution_failed" and state.get("status") == "execution_failed"
    if not stopped and not blocked_review and (attempt.get("status") == "pending" or not attempt.get("outcome")):
        return result
    outcome = attempt.get("outcome")
    status_code = record["status_code"]
    if not stopped and blocked_review:
        status_code = "review_blocked"
    elif not stopped and "fail" in checks:
        status_code = "review_failed"
    color = "green" if outcome in {"candidate", "publication_artifact", "acceptance_artifact"} else "blue"
    next_step = ""
    if status_code == "execution_failed":
        color, next_step = "red", event_copy(state, "execution_next")
    elif status_code in {"review_failed", "review_blocked"}:
        human = blocked_review or state.get("status") in {"ready_for_human", "progress_exhausted"}
        color = "yellow" if human else "orange"
        next_step = event_copy(state, "human_next") if human else event_copy(state, "repair_next")
    summary = record.get("development_summary")
    publication = record.get("publication") or {}
    summary = summary or artifact.get("summary") or publication.get("summary") or ""
    if role == "review":
        reasons = [value["evidence"] for value in (artifact.get("checks") or {}).values()
                   if isinstance(value, dict) and (not blocked_review or value.get("status") == "blocked")
                   and isinstance(value.get("evidence"), str) and value["evidence"]]
        summary = artifact.get("summary") or (event_copy(state, "separator").join(dict.fromkeys(reasons)) if blocked_review else "") or summary
    findings = record.get("findings") or []
    if status_code in {"review_failed", "review_blocked"} and not findings:
        summary = event_copy(state, "no_findings", summary=summary)
    if findings:
        summary = event_copy(state, "findings", count=len(findings)) + event_copy(state, "separator").join(findings)
        if blocked_review and reasons:
            summary += event_copy(state, "blocked_reasons") + event_copy(state, "separator").join(reasons)
    if role == "publication" and outcome == "publication_artifact":
        status_code = "publication_artifact"
    check_results = {key: value.get("status") for key, value in (artifact.get("checks") or {}).items()
                     if isinstance(value, dict)}
    title = {"development": event_copy(state, "development_end"), "review": event_copy(state, "review_ticket_end") if ticket else event_copy(state, "review_result_pending"),
             "publication": event_copy(state, "publication_ticket_end") if ticket else event_copy(state, "publication_end")}[role]
    if status_code in {"execution_failed", "review_blocked"}:
        title = event_copy(state, "role_result", role=facts["phase"], result=status_term(status_code, selected_language(state)))
    elif status_code == "review_failed":
        title = event_copy(state, "human_title") if human else event_copy(state, "repair_title")
        if not human and (state.get("active_agent_invocation") or {}).get("role") == "development":
            title = event_copy(state, "repair_active")
    elif role != "review" and outcome not in {"candidate", "publication_artifact"}:
        title = event_copy(state, "role_result", role=facts["phase"], result=status_term(status_code, selected_language(state)))
    elif role == "review" and (set(check_results) != {"e2e", "standards", "spec"} or checks != {"pass"}):
        title = event_copy(state, "review_unknown")
        color = "yellow"
    result.append(event_envelope(state, "stage_end", [record["attempt_id"], outcome, stopped, check_results],
                        title, color, checks=check_results, status_code=status_code,
                        summary=summary, next_step=next_step, findings_count=len(findings),
                        duration_seconds=record.get("execution_seconds") if record.get("ended_at") else None, **facts))
    return result


def events(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return bounded history facts and the current actionable boundary.

    The caller must retain delivery receipts independently of this rolling view,
    and scope ``current`` event receipts to each observed boundary occurrence.
    """
    audit = {
        "timeline": state.get("timeline", []),
        "timeline_continuation": state.get("timeline_continuation", []),
        "semantic_agent_attempts": semantic_attempt_history(state, include_history_facts=True),
        "agent_invocations": state.get("agent_invocation_history", []),
        "agent_resumes": (state.get("resume_audit") or {}).get("history", []),
    }
    result = []
    if (state.get("parent") or {}).get("number"):
        result.append(event_envelope(state, "run_start", state.get("run_id"), event_copy(state, "run_start"), started_at=state.get("created_at")))
    records = history_records(state, audit)
    for record in records:
        if not record.get("event_record"):
            result.extend(_round_events(state, record))
        for point in record.get("turning_points", []):
            kind = point.get("kind")
            if kind == "pr_creation" and point.get("pr_number"):
                number = point["pr_number"]
                result.append(event_envelope(state, "pr_created", number, event_copy(state, "pr_created", number=number),
                                    "green", summary=event_copy(state, "pr_summary"),
                                    url=f"https://github.com/{state.get('repository')}/pull/{number}"))
    key_events = milestones(state, records)
    # Detailed mode retains later rounds, but a milestone replaces its own
    # process-level start instead of describing the same fact twice.
    milestone_starts = {event.get("attempt_id") for event in key_events
                        if event["kind"] in {"ticket_started", "acceptance_started"}}
    result = [event for event in result if not (
        event["kind"] == "stage_start" and event.get("attempt_id") in milestone_starts)]
    replaced_results: set[object] = set()
    # The first failed overall review is represented by its subsequent repair
    # transition (or human boundary), even if those are separate saves.
    failed = next((record for record in records
                   if str(record.get("work_subject")) == f"run-acceptance:{state.get('run_id')}"
                   and any(check.get("status") == "fail" for check in
                           ((record.get("acceptance_artifact") or {}).get("checks") or {}).values()
                           if isinstance(check, dict))), None)
    if failed:
        replaced_results.add(failed.get("attempt_id"))
    if any(event["kind"] == "acceptance_passed" for event in key_events):
        owner = state.get("parent_job" if state.get("delivery_type") == "parent_only" else "run_acceptance") or {}
        formal_artifact = (owner.get("acceptance_record") or {}).get("artifact")
        matching = next((record for record in reversed(records)
                         if record.get("acceptance_artifact") == formal_artifact
                         and not str(record.get("work_subject")).startswith("ticket:")), None)
        if matching:
            replaced_results.add(matching.get("attempt_id"))
            for event in key_events:
                if event["kind"] == "acceptance_passed":
                    event.update(attempt_id=matching.get("attempt_id"), round=matching.get("ordinal"),
                                 phase=event_copy(state, "role_review"),
                                 duration_seconds=matching.get("execution_seconds") if matching.get("ended_at") else None)
    result = [event for event in result if not (
        event["kind"] == "stage_end" and event.get("attempt_id") in replaced_results)]
    result.extend(key_events)
    progress = ticket_progress(state)
    for number, job in (state.get("ticket_jobs") or {}).items():
        if isinstance(job, dict) and job.get("phase") == "completed":
            result.append(event_envelope(state, "ticket_completed", [number, job.get("generation")],
                                event_copy(state, "ticket_completed", title=event_subject(state, f"ticket:{number}").get("task_title") or "#" + str(number)), "green",
                                **event_subject(state, f"ticket:{number}"),
                                **execution_totals(state, records, None, work_subject=f"ticket:{number}"),
                                effort=ticket_effort(state, records, f"ticket:{number}"),
                                next_step=event_copy(state, "ticket_next") if progress['completed'] < progress['total'] else event_copy(state, "ticket_final_next"),
                                summary=event_copy(state, "progress", completed=progress["completed"], total=progress["total"])))
    publication = (state.get("parent_job") if state.get("delivery_type") == "parent_only"
                   else state.get("run_publication")) or {}
    pr_number = publication.get("pr_number")
    if type(pr_number) is int:
        result.append(event_envelope(state, "pr_created", pr_number, event_copy(state, "pr_created", number=pr_number),
                            "green", summary=event_copy(state, "pr_summary"),
                            url=f"https://github.com/{state.get('repository')}/pull/{pr_number}"))
    status = str(state.get("status") or "")
    boundary = {
        "ready_for_human": (event_copy(state, "human_title"), "yellow"),
        "blocked": (event_copy(state, "human_title"), "yellow"),
        "unsupported_scope_change": (event_copy(state, "human_title"), "yellow"),
        "deterministic_contradiction": (event_copy(state, "human_title"), "yellow"),
        "requeue_required": (event_copy(state, "requeue"), "yellow"),
        "publication_pending": (event_copy(state, "publication_pending"), "yellow"),
        "abandonment_pending": (event_copy(state, "abandonment_pending"), "yellow"),
        "run_approval_pending": (event_copy(state, "approval"), "yellow"),
        "parent_approval_pending": (event_copy(state, "approval"), "yellow"),
        "execution_failed": (event_copy(state, "execution_failed"), "red"),
        "progress_exhausted": (event_copy(state, "budget"), "yellow"),
        "supervision_timeout": (event_copy(state, "timeout"), "red"),
        "operator_stopped": (event_copy(state, "stopped"), "yellow"),
        "abandoned": (event_copy(state, "abandoned"), "yellow"),
        "completed": (event_copy(state, "completed"), "green"),
    }.get(status)
    if boundary:
        title, color = boundary
        current = current_work_subject(state)
        # Approval and lifecycle results describe the whole delivery. Other
        # boundaries use the same current work object as Status, not history.
        task_facts = event_subject(state, current[0]) if current and status not in {
            "run_approval_pending", "parent_approval_pending", "completed",
            "abandoned", "abandonment_pending", "operator_stopped",
        } else {}
        if not task_facts and pr_number and ("approval_pending" in status or status == "completed"):
            task_facts["url"] = f"https://github.com/{state.get('repository')}/pull/{pr_number}"
        blockers = list(state.get("human_blockers") or [])
        for owner in [current[1]] if current else []:
            if isinstance(owner, dict) and owner.get("phase") in {"ready_for_human", "blocked"}:
                blockers.extend(owner.get("human_blockers") or [])
                if owner.get("blocked_reason"):
                    blockers.append(pause_reason(owner["blocked_reason"], selected_language(state)))
        if status in {"execution_failed", "supervision_timeout"}:
            blockers.extend(item.get("message", "") for item in state.get("diagnostics", [])
                            if isinstance(item, dict))
        if state.get("blocked_reason"):
            blockers.append(pause_reason(state["blocked_reason"], selected_language(state)))
        summary = event_copy(state, "separator").join(str(value) for value in blockers) or str(status_term(status, selected_language(state)))
        if "approval_pending" in status:
            title = event_copy(state, "approve_number", number=pr_number) if pr_number else event_copy(state, "approval_wait")
            latest_review = event_copy(state, "review_pass") if any(
                event["kind"] == "acceptance_passed" for event in key_events
            ) else event_copy(state, "review_no_pass")
            checks_result = (publication.get("required_checks_evidence") or {}).get("result")
            checks_text = status_term(checks_result or "unknown", selected_language(state))
            summary += event_copy(state, "approval_summary", review=latest_review, checks=checks_text)
            for check in (publication.get("required_checks_evidence") or {}).get("checks", []):
                if isinstance(check, dict) and check.get("name"):
                    summary += event_copy(state, "check_result", name=check["name"], result=status_term(check.get("bucket") or check.get("state") or "unknown", selected_language(state)))
        if status == "completed":
            summary = event_copy(state, "separator").join(part for part in (event_copy(state, "merged", number=pr_number) if publication.get("phase") in {"merged", "completed"} and pr_number else "", event_copy(state, "closed") if publication.get("parent_closed") is True or (state.get("delivery_type") == "parent_only" and publication.get("phase") == "completed") else "") if part)
        cleanup_pending = status == "completed" and final_approval_cleanup_pending(state)
        if cleanup_pending:
            title, color = event_copy(state, "cleanup"), "yellow"
            summary += event_copy(state, "cleanup_summary")
        timeline = list(state.get("timeline", [])) + list(state.get("timeline_continuation", []))
        at = next((point.get("at") for point in reversed(timeline) if point.get("status") == status), None)
        boundary_identity: object = "completed_cleanup" if cleanup_pending else status
        if task_facts.get("task_number") is not None:
            boundary_identity = [status, task_facts["task_number"]]
        result.append(event_envelope(state, "boundary", boundary_identity, title, color, summary=summary,
                            current=True, status_code=status, **execution_totals(state, records, at),
                            trigger_role=next((event_copy(state, "role_" + str(role_family(str(r.get("role"))))) for r in reversed(records) if not r.get("event_record")), None) if status in {"ready_for_human", "execution_failed", "progress_exhausted"} else None,
                            **task_facts,
                            next_step="" if status == "abandoned" or (status == "completed" and not cleanup_pending) else str(next_action(state) or "")))
    if "approval_pending" in status or status == "completed" or publication.get("approval_grant"):
        result = [event for event in result if event["kind"] != "pr_created"]
    from agent_run.notification_resume import apply_resume_result
    result = apply_resume_result(state, result)
    if (state.get("notifications") or {}).get("mode", "detailed") == "concise":
        result = [event for event in result if event["kind"] in {"run_start", "ticket_started", "ticket_completed", "acceptance_started", "acceptance_repair", "acceptance_passed", "resume", "boundary"}]
    return list({event["id"]: event for event in result}.values())


def recovery_event(state: dict[str, Any], pending: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the latest still-valid business fact without reviving old todo items."""
    projected = events(state)
    current = next((event for event in reversed(projected) if event.get("current")), None)
    if current:
        return current.copy()
    pending_ids = {event["id"] for event in pending}
    # Finished rounds are historical facts; they must not become a current
    # result just because their message was delayed. Only a live round start
    # or a Run that has not progressed yet is eligible after recovery.
    completed = {event.get("attempt_id")
                 for event in projected if event["kind"] == "stage_end"}
    eligible = [event for event in projected if event["id"] in pending_ids and (
        (event["kind"] == "run_start" and state.get("status") == "starting") or
        (event["kind"] in {"stage_start", "ticket_started", "acceptance_started", "acceptance_repair", "resume"}
         and event.get("live") and event.get("attempt_id") not in completed)
        or event["kind"] == "acceptance_passed"
        or (event["kind"] == "acceptance_repair"
            and (state.get("run_acceptance") or {}).get("phase") == "repairing"))]
    return eligible[-1].copy() if eligible else None
