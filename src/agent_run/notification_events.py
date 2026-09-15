"""Read-only notification projection of the operator's semantic history.

Current boundary identities are slots: the sender assigns an occurrence when
leaving and re-entering a slot. No identity uses display text or row position.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_run.cli_presentation import _semantic_attempt_history, human_next_action_for_state
from agent_run.delivery_history import history_records, _role_family
from agent_run.delivery_status import _ticket_progress
from agent_run.presentation_helpers import human_status_term


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
        "task_number": number, "task_title": parent.get("title") or "任务标题未知",
        "phase": "整体交付", "round": None, "duration_seconds": None,
        "summary": "结果未知", "next_step": "", "current": False,
        "url": f"https://github.com/{repository}/issues/{number}" if number else None,
        "query": f"agent-run history {state.get('run_id')} --details", **facts,
    }


def _round_events(state: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    role = _role_family(str(record.get("role")))
    if role is None or not record.get("attempt_id"):
        return []
    attempt = record.get("attempt") or {}
    facts = {"phase": record["role_label"], "round": record.get("ordinal"),
             "object": record.get("object")}
    result = []
    if record.get("started_at") or record.get("invocations"):
        result.append(_base(state, "stage_start", record["attempt_id"],
                            f"{record['role_label']}启动", summary="本轮工作已启动", **facts))
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
    summary = summary or artifact.get("summary") or publication.get("summary") or "结果摘要未知"
    if role == "review":
        reasons = [value["evidence"] for value in (artifact.get("checks") or {}).values()
                   if isinstance(value, dict) and (not blocked_review or value.get("status") == "blocked")
                   and isinstance(value.get("evidence"), str) and value["evidence"]]
        summary = artifact.get("summary") or "；".join(dict.fromkeys(reasons)) or summary
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
    result.append(_base(state, "stage_end", [record["attempt_id"], outcome, stopped, check_results],
                        f"{record['role_label']} · {text}", color,
                        summary=summary, next_step=next_step, findings_count=len(findings),
                        duration_seconds=record.get("execution_seconds"), **facts))
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
    if (state.get("parent") or {}).get("title"):
        result.append(_base(state, "run_start", state.get("run_id"), "任务已启动", summary="自动交付已开始"))
    latest_review = "未知"
    for record in history_records(state, audit):
        if _role_family(str(record.get("role"))) == "review":
            latest_review = record.get("status_text") or "未知"
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
                                f"子任务 #{number} 完成", "green",
                                summary=f"子任务已完成 {progress['completed']}/{progress['total']}；整体交付仍以最终结果为准"))
    publication = (state.get("parent_job") if state.get("delivery_type") == "parent_only"
                   else state.get("run_publication")) or {}
    pr_number = publication.get("pr_number")
    if type(pr_number) is int:
        result.append(_base(state, "pr_created", pr_number, f"最终 PR #{pr_number} 已创建",
                            "green", summary="最终 PR 已创建，等待后续检查与批准",
                            url=f"https://github.com/{state.get('repository')}/pull/{pr_number}"))
    status = str(state.get("status") or "")
    boundary = {
        "ready_for_human": ("需要人工处理", "yellow"),
        "run_approval_pending": ("等待人工批准", "yellow"),
        "parent_approval_pending": ("等待人工批准", "yellow"),
        "execution_failed": ("执行故障，已停止", "red"),
        "progress_exhausted": ("执行预算耗尽，需要人工处理", "yellow"),
        "supervision_timeout": ("执行监督超时，已停止", "red"),
        "operator_stopped": ("人工停止", "yellow"),
        "abandoned": ("交付已放弃", "yellow"),
        "completed": ("整体交付完成", "green"),
    }.get(status)
    if boundary:
        title, color = boundary
        blockers = list(state.get("human_blockers") or [])
        for owner in [state.get("parent_job"), state.get("run_acceptance"), publication,
                      *(state.get("ticket_jobs") or {}).values()]:
            if isinstance(owner, dict) and owner.get("phase") == "ready_for_human":
                blockers.extend(owner.get("human_blockers") or [])
        if status in {"execution_failed", "supervision_timeout"}:
            blockers.extend(item.get("message", "") for item in state.get("diagnostics", [])
                            if isinstance(item, dict))
        summary = "；".join(str(value) for value in blockers) or str(human_status_term(status))
        if "approval_pending" in status:
            checks_result = (publication.get("required_checks_evidence") or {}).get("result")
            checks_text = human_status_term(checks_result or "unknown")
            summary += f"；验收：{latest_review}；检查：{checks_text}"
        result.append(_base(state, "boundary", status, title, color, summary=summary,
                            current=True, next_step=str(human_next_action_for_state(state) or "")))
    return list({event["id"]: event for event in result}.values())


def recovery_event(state: dict[str, Any], pending: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize missed progress using current state, never old blocker text."""
    current = next((event for event in reversed(events(state)) if event.get("current")), None)
    status = str(human_status_term(state.get("status") or "未知"))
    return _base(state, "recovery", sorted(event["id"] for event in pending),
                 "通知恢复 · 当前进度", current["color"] if current else "blue",
                 summary=f"合并 {len(pending)} 条过时进展；当前：{status}。" + (current["summary"] if current else ""),
                 next_step=current["next_step"] if current else str(human_next_action_for_state(state) or ""))
