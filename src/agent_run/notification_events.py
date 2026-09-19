"""Read-only notification projection of the operator's semantic history.

Current boundary identities are slots: the sender assigns an occurrence when
leaving and re-entering a slot. No identity uses display text or row position.
"""
from __future__ import annotations

import hashlib
import json
import re
from agent_run.execution_timing import execution_totals
from agent_run.final_approval_operation import final_approval_cleanup_pending
from typing import Any

from agent_run.cli_presentation import _semantic_attempt_history
from agent_run.delivery_history import history_records, _role_family
from agent_run.delivery_status import _ticket_progress, current_acceptance_artifact
from agent_run.presentation_helpers import current_work_subject
from agent_run.messages import selected_language, text
from agent_run.notification_presentation import status_term, pause_reason, next_action


def _copy(state: dict[str, Any], key: str, **values: object) -> str:
    return text("notification.event." + key, language=selected_language(state), **values)


def _identity(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _base(state: dict[str, Any], kind: str, identity: object, title: str,
          color: str = "blue", **facts: Any) -> dict[str, Any]:
    parent = state.get("parent") or {}
    repository = str(state.get("repository") or _copy(state, "unknown_repository"))
    number = parent.get("number")
    return {
        "id": _identity(state.get("run_id"), kind, identity), "kind": kind,
        "title": title, "color": color, "repository": repository,
        "language": selected_language(state),
        "task_number": number, "task_title": parent.get("title") or "",
        "phase": _copy(state, "delivery"), "round": None, "duration_seconds": None,
        "summary": "", "next_step": "", "current": False,
        "url": f"https://github.com/{repository}/issues/{number}" if number else None,
        "query": f"agent-run history {state.get('run_id')} --details", **facts,
    }


def _subject(state: dict[str, Any], subject: object) -> dict[str, Any]:
    match = re.search(r"ticket:(\d+)", str(subject))
    if not match:
        return {}
    number = match.group(1)
    ticket = ((state.get("ticket_graph") or {}).get("tickets") or {}).get(number, {})
    return {"task_number": int(number), "task_title": ticket.get("title") or "",
            "url": f"https://github.com/{state.get('repository')}/issues/{number}"}


def _round_events(state: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    role = _role_family(str(record.get("role")))
    if role is None or not record.get("attempt_id"):
        return []
    attempt = record.get("attempt") or {}
    facts = {"phase": _copy(state, "role_" + role), "round": record.get("ordinal"),
             "role": record.get("role"), "attempt_id": record["attempt_id"], **_subject(state, record.get("work_subject"))}
    result = []
    ticket = "task_number" in facts
    title = facts.get("task_title") or (f"#{facts['task_number']}" if ticket else _copy(state, "parent"))
    start_title = {"development": _copy(state, "development_start", title=title), "review": _copy(state, "review_ticket_start", title=title) if ticket else _copy(state, "review_start"),
                   "publication": _copy(state, "publication_ticket_start") if ticket else _copy(state, "publication_start")}[role]
    if record.get("started_at") or record.get("invocations"):
        result.append(_base(state, "stage_start", record["attempt_id"],
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
        color, next_step = "red", _copy(state, "execution_next")
    elif status_code in {"review_failed", "review_blocked"}:
        human = blocked_review or state.get("status") in {"ready_for_human", "progress_exhausted"}
        color = "yellow" if human else "orange"
        next_step = _copy(state, "human_next") if human else _copy(state, "repair_next")
    summary = record.get("development_summary")
    publication = record.get("publication") or {}
    summary = summary or artifact.get("summary") or publication.get("summary") or ""
    if role == "review":
        reasons = [value["evidence"] for value in (artifact.get("checks") or {}).values()
                   if isinstance(value, dict) and (not blocked_review or value.get("status") == "blocked")
                   and isinstance(value.get("evidence"), str) and value["evidence"]]
        summary = artifact.get("summary") or (_copy(state, "separator").join(dict.fromkeys(reasons)) if blocked_review else "") or summary
    findings = record.get("findings") or []
    if status_code in {"review_failed", "review_blocked"} and not findings:
        summary = _copy(state, "no_findings", summary=summary)
    if findings:
        summary = _copy(state, "findings", count=len(findings)) + _copy(state, "separator").join(findings)
        if blocked_review and reasons:
            summary += _copy(state, "blocked_reasons") + _copy(state, "separator").join(reasons)
    if role == "publication" and outcome == "publication_artifact":
        status_code = "publication_artifact"
    check_results = {key: value.get("status") for key, value in (artifact.get("checks") or {}).items()
                     if isinstance(value, dict)}
    title = {"development": _copy(state, "development_end"), "review": _copy(state, "review_ticket_end") if ticket else _copy(state, "review_end"),
             "publication": _copy(state, "publication_ticket_end") if ticket else _copy(state, "publication_end")}[role]
    if status_code in {"execution_failed", "review_blocked"}:
        title = _copy(state, "role_result", role=facts["phase"], result=status_term(status_code, selected_language(state)))
    elif status_code == "review_failed":
        title = _copy(state, "human_title") if human else _copy(state, "repair_title")
        if not human and (state.get("active_agent_invocation") or {}).get("role") == "development":
            title = _copy(state, "repair_active")
    elif role != "review" and outcome not in {"candidate", "publication_artifact"}:
        title = _copy(state, "role_result", role=facts["phase"], result=status_term(status_code, selected_language(state)))
    elif role == "review" and (set(check_results) != {"e2e", "standards", "spec"} or checks != {"pass"}):
        title = _copy(state, "review_unknown")
        color = "yellow"
    result.append(_base(state, "stage_end", [record["attempt_id"], outcome, stopped, check_results],
                        title, color, checks=check_results,
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
        "semantic_agent_attempts": _semantic_attempt_history(state, include_history_facts=True),
        "agent_invocations": state.get("agent_invocation_history", []),
        "agent_resumes": (state.get("resume_audit") or {}).get("history", []),
    }
    result = []
    if (state.get("parent") or {}).get("number"):
        result.append(_base(state, "run_start", state.get("run_id"), _copy(state, "run_start"), started_at=state.get("created_at")))
    records = history_records(state, audit)
    for record in records:
        if not record.get("event_record"):
            result.extend(_round_events(state, record))
        for point in record.get("turning_points", []):
            kind = point.get("kind")
            if kind == "resume" and point.get("resume_id"):
                result.append(_base(state, "resume", point["resume_id"], _copy(state, "resume"), summary=_copy(state, "resume_summary")))
            if kind == "pr_creation" and point.get("pr_number"):
                number = point["pr_number"]
                result.append(_base(state, "pr_created", number, _copy(state, "pr_created", number=number),
                                    "green", summary=_copy(state, "pr_summary"),
                                    url=f"https://github.com/{state.get('repository')}/pull/{number}"))
    progress = _ticket_progress(state)
    for number, job in (state.get("ticket_jobs") or {}).items():
        if isinstance(job, dict) and job.get("phase") in {"completed", "merged"}:
            result.append(_base(state, "ticket_completed", [number, job.get("generation")],
                                _copy(state, "ticket_completed", title=_subject(state, f"ticket:{number}").get("task_title") or "#" + str(number)), "green",
                                **_subject(state, f"ticket:{number}"),
                                **execution_totals(state, records, None, work_subject=f"ticket:{number}"),
                                next_step=_copy(state, "ticket_next") if progress['completed'] < progress['total'] else _copy(state, "ticket_final_next"),
                                summary=_copy(state, "progress", completed=progress["completed"], total=progress["total"])))
    publication = (state.get("parent_job") if state.get("delivery_type") == "parent_only"
                   else state.get("run_publication")) or {}
    pr_number = publication.get("pr_number")
    if type(pr_number) is int:
        result.append(_base(state, "pr_created", pr_number, _copy(state, "pr_created", number=pr_number),
                            "green", summary=_copy(state, "pr_summary"),
                            url=f"https://github.com/{state.get('repository')}/pull/{pr_number}"))
    status = str(state.get("status") or "")
    boundary = {
        "ready_for_human": (_copy(state, "human_title"), "yellow"),
        "blocked": (_copy(state, "human_title"), "yellow"),
        "unsupported_scope_change": (_copy(state, "human_title"), "yellow"),
        "deterministic_contradiction": (_copy(state, "human_title"), "yellow"),
        "requeue_required": (_copy(state, "requeue"), "yellow"),
        "publication_pending": (_copy(state, "publication_pending"), "yellow"),
        "abandonment_pending": (_copy(state, "abandonment_pending"), "yellow"),
        "run_approval_pending": (_copy(state, "approval"), "yellow"),
        "parent_approval_pending": (_copy(state, "approval"), "yellow"),
        "execution_failed": (_copy(state, "execution_failed"), "red"),
        "progress_exhausted": (_copy(state, "budget"), "yellow"),
        "supervision_timeout": (_copy(state, "timeout"), "red"),
        "operator_stopped": (_copy(state, "stopped"), "yellow"),
        "abandoned": (_copy(state, "abandoned"), "yellow"),
        "completed": (_copy(state, "completed"), "green"),
    }.get(status)
    if boundary:
        title, color = boundary
        current = current_work_subject(state)
        # Approval and lifecycle results describe the whole delivery. Other
        # boundaries use the same current work object as Status, not history.
        task_facts = _subject(state, current[0]) if current and status not in {
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
        summary = _copy(state, "separator").join(str(value) for value in blockers) or str(status_term(status, selected_language(state)))
        if "approval_pending" in status:
            title = _copy(state, "approve_number", number=pr_number) if pr_number else _copy(state, "approval_wait")
            artifact = current_acceptance_artifact(state) or {}
            verdicts = [check.get("status") for check in (artifact.get("checks") or {}).values() if isinstance(check, dict)]
            complete_checks = set(artifact.get("checks") or {}) == {"e2e", "standards", "spec"}
            latest_review = _copy(state, "review_pass") if complete_checks and verdicts and all(value == "pass" for value in verdicts) else _copy(state, "review_no_pass")
            checks_result = (publication.get("required_checks_evidence") or {}).get("result")
            checks_text = status_term(checks_result or "unknown", selected_language(state))
            summary += _copy(state, "approval_summary", review=latest_review, checks=checks_text)
            for check in (publication.get("required_checks_evidence") or {}).get("checks", []):
                if isinstance(check, dict) and check.get("name"):
                    summary += _copy(state, "check_result", name=check["name"], result=status_term(check.get("bucket") or check.get("state") or "unknown", selected_language(state)))
        if status == "completed":
            summary = _copy(state, "separator").join(part for part in (_copy(state, "merged", number=pr_number) if publication.get("phase") in {"merged", "completed"} and pr_number else "", _copy(state, "closed") if publication.get("parent_closed") is True or (state.get("delivery_type") == "parent_only" and publication.get("phase") == "completed") else "") if part)
        cleanup_pending = status == "completed" and final_approval_cleanup_pending(state)
        if cleanup_pending:
            title, color = _copy(state, "cleanup"), "yellow"
            summary += _copy(state, "cleanup_summary")
        timeline = list(state.get("timeline", [])) + list(state.get("timeline_continuation", []))
        at = next((point.get("at") for point in reversed(timeline) if point.get("status") == status), None)
        boundary_identity: object = "completed_cleanup" if cleanup_pending else status
        if task_facts.get("task_number") is not None:
            boundary_identity = [status, task_facts["task_number"]]
        result.append(_base(state, "boundary", boundary_identity, title, color, summary=summary,
                            current=True, **execution_totals(state, records, at),
                            trigger_role=next((_copy(state, "role_" + str(_role_family(str(r.get("role"))))) for r in reversed(records) if not r.get("event_record")), None) if status in {"ready_for_human", "execution_failed", "progress_exhausted"} else None,
                            **task_facts,
                            next_step="" if status == "abandoned" or (status == "completed" and not cleanup_pending) else str(next_action(state) or "")))
    if "approval_pending" in status or status == "completed" or publication.get("approval_grant"):
        result = [event for event in result if event["kind"] != "pr_created"]
    if (state.get("notifications") or {}).get("mode", "detailed") == "concise":
        result = [event for event in result if event["kind"] in {"run_start", "ticket_completed", "boundary"}]
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
        (event["kind"] == "stage_start" and event.get("live") and event.get("attempt_id") not in completed))]
    return eligible[-1].copy() if eligible else None
