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

from agent_run.cli_presentation import _semantic_attempt_history, human_next_action_for_state
from agent_run.delivery_history import history_records, _role_family
from agent_run.delivery_status import _ticket_progress, current_acceptance_artifact
from agent_run.presentation_helpers import current_work_subject, human_status_term, human_pause_reason


def _identity(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _base(state: dict[str, Any], kind: str, identity: object, title: str,
          color: str = "blue", **facts: Any) -> dict[str, Any]:
    parent = state.get("parent") or {}
    repository = str(state.get("repository") or "未知仓库")
    number = parent.get("number")
    return {
        "id": _identity(state.get("run_id"), kind, identity), "kind": kind,
        "title": title, "color": color, "repository": repository,
        "task_number": number, "task_title": parent.get("title") or "",
        "phase": "整体交付", "round": None, "duration_seconds": None,
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
    facts = {"phase": record["role_label"], "round": record.get("ordinal"),
             "object": record.get("object"), **_subject(state, record.get("work_subject"))}
    result = []
    ticket = "task_number" in facts
    title = facts.get("task_title") or (f"#{facts['task_number']}" if ticket else "整体需求")
    start_title = {"development": f"正在开发：{title}", "review": f"正在验收：{title}" if ticket else "正在验收整体需求",
                   "publication": "正在整理子任务的提交和 PR 说明" if ticket else "正在整理最终 PR 说明"}[role]
    if record.get("started_at") or record.get("invocations"):
        result.append(_base(state, "stage_start", record["attempt_id"],
                            start_title, started_at=record.get("started_at"), live=record.get("activity") == "running", **facts))
    artifact = record.get("acceptance_artifact") or {}
    checks = {value.get("status") for value in (artifact.get("checks") or {}).values()
              if isinstance(value, dict)}
    blocked_review = role == "review" and "blocked" in checks
    # A blocked review has a result even though its semantic round remains
    # pending for human recovery. Process exit alone still proves no result.
    stopped = record["status_text"] == "执行失败" and state.get("status") == "execution_failed"
    if not stopped and not blocked_review and (attempt.get("status") == "pending" or not attempt.get("outcome")):
        return result
    outcome = attempt.get("outcome")
    text = record["status_text"]
    if not stopped and blocked_review:
        text = "验收受阻"
    elif not stopped and "fail" in checks:
        text = "验收未通过"
    color = "green" if outcome in {"candidate", "publication_artifact", "acceptance_artifact"} else "blue"
    next_step = ""
    if text == "执行失败":
        color, next_step = "red", "执行已停止；查看本地状态并按指引处理。"
    elif text in {"验收未通过", "验收受阻"}:
        human = blocked_review or state.get("status") in {"ready_for_human", "progress_exhausted"}
        color = "yellow" if human else "orange"
        next_step = "需要人工处理；查看当前操作指引。" if human else "将自动修复并重新验收，无需操作。"
    summary = record.get("development_summary")
    publication = record.get("publication") or {}
    summary = summary or artifact.get("summary") or publication.get("summary") or ""
    if role == "review":
        reasons = [value["evidence"] for value in (artifact.get("checks") or {}).values()
                   if isinstance(value, dict) and (not blocked_review or value.get("status") == "blocked")
                   and isinstance(value.get("evidence"), str) and value["evidence"]]
        summary = artifact.get("summary") or ("；".join(dict.fromkeys(reasons)) if blocked_review else "") or summary
    findings = record.get("findings") or []
    if text in {"验收未通过", "验收受阻"} and not findings:
        summary = f"0 个结构化问题；{summary}"
    if findings:
        summary = f"{len(findings)} 个问题：" + "；".join(findings)
        if blocked_review and reasons:
            summary += "；受阻原因：" + "；".join(reasons)
    if role == "publication" and outcome == "publication_artifact":
        text = "发布说明已准备"
    check_results = {key: value.get("status") for key, value in (artifact.get("checks") or {}).items()
                     if isinstance(value, dict)}
    title = {"development": "开发完成，等待验收", "review": "子任务验收通过" if ticket else "整体需求验收通过",
             "publication": "子任务的提交和 PR 说明已准备好" if ticket else "最终 PR 说明已准备好"}[role]
    if text in {"执行失败", "验收受阻"}:
        title = f"{record['role_label']} · {text}"
    elif text == "验收未通过":
        title = "任务暂停，需要你处理" if human else "验收未通过，将自动修复"
        if not human and (state.get("active_agent_invocation") or {}).get("role") == "development":
            title = "验收未通过，正在自动修复"
    elif role != "review" and outcome not in {"candidate", "publication_artifact"}:
        title = f"{record['role_label']}：{text}"
    elif role == "review" and (set(check_results) != {"e2e", "standards", "spec"} or checks != {"pass"}):
        title = "验收结果尚未确认"
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
        result.append(_base(state, "run_start", state.get("run_id"), "任务已开始", started_at=state.get("created_at")))
    records = history_records(state, audit)
    for record in records:
        if not record.get("event_record"):
            result.extend(_round_events(state, record))
        for point in record.get("turning_points", []):
            kind = point.get("kind")
            if kind == "resume" and point.get("resume_id"):
                result.append(_base(state, "resume", point["resume_id"], "人工恢复／继续", summary="已收到人工恢复请求"))
            if kind == "pr_creation" and point.get("pr_number"):
                number = point["pr_number"]
                result.append(_base(state, "pr_created", number, f"最终 PR #{number} 已创建",
                                    "green", summary="最终 PR 已创建，等待后续检查与批准",
                                    url=f"https://github.com/{state.get('repository')}/pull/{number}"))
    progress = _ticket_progress(state)
    for number, job in (state.get("ticket_jobs") or {}).items():
        if isinstance(job, dict) and job.get("phase") in {"completed", "merged"}:
            result.append(_base(state, "ticket_completed", [number, job.get("generation")],
                                f"子任务完成：{_subject(state, f'ticket:{number}').get('task_title') or '#' + str(number)}", "green",
                                **_subject(state, f"ticket:{number}"),
                                **execution_totals(state, records, None, work_subject=f"ticket:{number}"),
                                next_step="继续处理其他子任务" if progress['completed'] < progress['total'] else "等待整体需求验收及最终交付",
                                summary=f"子任务已完成 {progress['completed']}/{progress['total']}"))
    publication = (state.get("parent_job") if state.get("delivery_type") == "parent_only"
                   else state.get("run_publication")) or {}
    pr_number = publication.get("pr_number")
    if type(pr_number) is int:
        result.append(_base(state, "pr_created", pr_number, f"最终 PR #{pr_number} 已创建",
                            "green", summary="最终 PR 已创建，等待后续检查与批准",
                            url=f"https://github.com/{state.get('repository')}/pull/{pr_number}"))
    status = str(state.get("status") or "")
    boundary = {
        "ready_for_human": ("任务暂停，需要你处理", "yellow"),
        "blocked": ("任务暂停，需要你处理", "yellow"),
        "unsupported_scope_change": ("任务暂停，需要你处理", "yellow"),
        "deterministic_contradiction": ("任务暂停，需要你处理", "yellow"),
        "requeue_required": ("需求已变化，需要重新开始", "yellow"),
        "publication_pending": ("发布尚未完成，需要你处理", "yellow"),
        "abandonment_pending": ("放弃操作尚未完成，需要你处理", "yellow"),
        "run_approval_pending": ("请批准合并 PR", "yellow"),
        "parent_approval_pending": ("请批准合并 PR", "yellow"),
        "execution_failed": ("执行中断，等待恢复", "red"),
        "progress_exhausted": ("执行预算耗尽，任务暂停，需要你处理", "yellow"),
        "supervision_timeout": ("执行监督超时，已停止", "red"),
        "operator_stopped": ("任务已暂停", "yellow"),
        "abandoned": ("任务已放弃", "yellow"),
        "completed": ("任务已完成", "green"),
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
                    blockers.append(human_pause_reason(owner["blocked_reason"]))
        if status in {"execution_failed", "supervision_timeout"}:
            blockers.extend(item.get("message", "") for item in state.get("diagnostics", [])
                            if isinstance(item, dict))
        if state.get("blocked_reason"):
            blockers.append(human_pause_reason(state["blocked_reason"]))
        summary = "；".join(str(value) for value in blockers) or str(human_status_term(status))
        if "approval_pending" in status:
            title = f"请批准合并 PR #{pr_number}" if pr_number else "等待批准合并"
            artifact = current_acceptance_artifact(state) or {}
            verdicts = [check.get("status") for check in (artifact.get("checks") or {}).values() if isinstance(check, dict)]
            complete_checks = set(artifact.get("checks") or {}) == {"e2e", "standards", "spec"}
            latest_review = "通过" if complete_checks and verdicts and all(value == "pass" for value in verdicts) else "尚无有效通过结论"
            checks_result = (publication.get("required_checks_evidence") or {}).get("result")
            checks_text = human_status_term(checks_result or "unknown")
            summary += f"；验收：{latest_review}；检查：{checks_text}"
            for check in (publication.get("required_checks_evidence") or {}).get("checks", []):
                if isinstance(check, dict) and check.get("name"):
                    summary += f"；{check['name']}：{human_status_term(check.get('bucket') or check.get('state') or 'unknown')}"
        if status == "completed":
            summary = "；".join(part for part in (f"PR #{pr_number} 已合并" if publication.get("phase") in {"merged", "completed"} and pr_number else "", "需求已关闭" if publication.get("parent_closed") is True or (state.get("delivery_type") == "parent_only" and publication.get("phase") == "completed") else "") if part)
        cleanup_pending = status == "completed" and final_approval_cleanup_pending(state)
        if cleanup_pending:
            title, color = "已合并，待清理", "yellow"
            summary += "；工作区清理尚未完成"
        timeline = list(state.get("timeline", [])) + list(state.get("timeline_continuation", []))
        at = next((point.get("at") for point in reversed(timeline) if point.get("status") == status), None)
        boundary_identity: object = "completed_cleanup" if cleanup_pending else status
        if task_facts.get("task_number") is not None:
            boundary_identity = [status, task_facts["task_number"]]
        result.append(_base(state, "boundary", boundary_identity, title, color, summary=summary,
                            current=True, **execution_totals(state, records, at),
                            trigger_role=next((r.get("role_label") for r in reversed(records) if not r.get("event_record")), None) if status in {"ready_for_human", "execution_failed", "progress_exhausted"} else None,
                            **task_facts,
                            next_step="" if status == "abandoned" or (status == "completed" and not cleanup_pending) else str(human_next_action_for_state(state) or "")))
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
    completed = {(event.get("phase"), event.get("round"), event.get("task_number"))
                 for event in projected if event["kind"] == "stage_end"}
    eligible = [event for event in projected if event["id"] in pending_ids and (
        (event["kind"] == "run_start" and state.get("status") == "starting") or
        (event["kind"] == "stage_start" and event.get("live") and (event.get("phase"), event.get("round"), event.get("task_number")) not in completed))]
    return eligible[-1].copy() if eligible else None
