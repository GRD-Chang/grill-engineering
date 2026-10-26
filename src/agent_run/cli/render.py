"""One factual document rendered as machine JSON, plain text or safe Rich text."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
import json
import re
from typing import Any, Literal, TextIO
import unicodedata

from agent_run.contracts.copy import text

from agent_run.queries.run import QueryDocument, review_findings
from agent_run.queries.execution import invocation_timing
from agent_run.queries.read_facts import QueryError
from agent_run.storage.sqlite import QUERY_LIMIT


_COPY = {
    "zh": {"run": "交付运行", "status": "状态", "phase": "阶段", "subject": "工作对象", "unknown": "未记录",
           "next": "下一步", "model": "模型", "effort": "推理强度", "history": "工作历程", "budget": "累计用量",
           "findings": "当前问题", "evidence": "证据", "duration": "任务历时（秒）", "agent": "Agent",
           "development": "开发 Agent", "review": "验收 Agent", "publication": "发布 Agent", "gates": "人工待办",
           "started": "已记录启动；当前活性未确认", "cleanup": "资源清理", "pending": "待清理", "complete": "完成",
           "time": "显示时区", "incomplete": "耗时记录不完整", "more": "后续历史页", "scope": "影响范围",
           "no_runs": "无匹配运行", "active": "进行中", "completed": "业务已完成", "operator_stopped": "已主动停止",
           "blocked": "有阻塞", "observe": "继续观察", "inspect": "核对原始事实与诊断", "resume": "显式恢复原工作",
           "approve": "核验最终 PR 后批准", "requeue": "确认最新需求后重新排队", "local": "该 Ticket 及依赖工作",
           "global": "整个交付运行", "unknown_live": "执行者当前活性未确认；查询不会恢复或停止任务",
           "notification_disabled": "本 Run 的冻结配置已禁用飞书通知",
           "notification_configuration_unknown": "本 Run 未记录飞书通知配置",
           "notification_journal_missing": "未记录飞书通知发送日志",
           "notification_binding_unknown": "通知日志未记录收件人及配置的绑定回执",
           "resume_label": "最近恢复", "resume_applied": "已应用；尚未观察到目标进展",
           "resume_unconfirmed": "已记录请求；应用尚未确认", "resume_started": "已记录目标进展",
           "resume_failed": "目标执行失败", "resume_human_action": "目标需要人工处理",
           "worker_exit_wait": "原 Worker 退出尚未确认", "supervision": "外部事实监督",
           "github_convergence": "GitHub 事实收敛", "required_checks": "必要检查",
           "remaining": "剩余秒数", "retry_count": "已记录重试", "deadline": "截止时间",
           "timeout_resume": "超时后的恢复命令"},
    "en": {"run": "Delivery Run", "status": "Status", "phase": "Phase", "subject": "Work subject", "unknown": "Not recorded",
           "next": "Next action", "model": "Model", "effort": "Reasoning effort", "history": "Work history", "budget": "Cumulative usage",
           "findings": "Current findings", "evidence": "Evidence", "duration": "Task elapsed seconds", "agent": "Agent",
           "development": "Development Agent", "review": "Review Agent", "publication": "Publication Agent", "gates": "Operator actions",
           "started": "Recorded started; current liveness unconfirmed", "cleanup": "Resource cleanup", "pending": "Pending cleanup", "complete": "Complete",
           "time": "Display timezone", "incomplete": "Execution timing incomplete", "more": "More history", "scope": "Impact scope",
           "no_runs": "No matching Runs", "active": "Active", "completed": "Business completed", "operator_stopped": "Operator stopped",
           "blocked": "Blocked", "observe": "Continue observing", "inspect": "Inspect original facts and diagnostics", "resume": "Explicitly resume original work",
           "approve": "Verify and approve the final PR", "requeue": "Confirm new requirements and requeue", "local": "This Ticket and dependent work",
           "global": "Whole Delivery Run", "unknown_live": "Executor liveness is unconfirmed; querying does not resume or stop work",
           "notification_disabled": "Feishu notifications disabled in this Run's frozen configuration",
           "notification_configuration_unknown": "Feishu notification configuration not recorded for this Run",
           "notification_journal_missing": "Feishu delivery journal not recorded",
           "notification_binding_unknown": "Feishu journal recipient/configuration binding not recorded",
           "resume_label": "Resume", "resume_applied": "applied; target progress not yet observed",
           "resume_unconfirmed": "request recorded; application unconfirmed", "resume_started": "Target progress recorded",
           "resume_failed": "Target execution failed", "resume_human_action": "Target needs human action",
           "worker_exit_wait": "Worker exit not yet confirmed", "supervision": "External fact supervision",
           "github_convergence": "GitHub convergence", "required_checks": "Required checks",
           "remaining": "Seconds remaining", "retry_count": "Recorded retries", "deadline": "Deadline",
           "timeout_resume": "Resume command after timeout"},
}

_TERMS = {
    "zh": {"development": "开发", "developing": "开发", "review": "验收", "reviewing": "验收", "publish": "准备发布文案",
           "publication_pending": "准备发布文案", "candidate": "保存候选", "integration": "集成检查", "workspace": "准备工作区",
           "ready_publication": "准备发布", "publication_commit": "保存发布提交", "push": "推送发布成果", "pr_write": "创建或更新 PR",
           "checks": "等待必需检查", "waiting_checks": "等待必需检查", "approval": "等待最终批准", "merge": "合并", "merging": "合并",
           "completion_comment": "记录完成说明", "close": "Issue 收尾", "closing": "Issue 收尾", "complete": "业务已完成", "completed": "业务已完成",
           "parallel": "多个对象并行", "requirements": "需求实现", "ci": "CI 修复", "abandoned": "已放弃", "abandonment_pending": "正在放弃",
           "ticket_completed": "子任务已完成", "ready_for_human": "等待人工处理", "parent_approval_pending": "完整需求待批准",
           "run_approval_pending": "最终 Run 待批准", "run_acceptance_pending": "整体修复或验收", "run_publication_pending": "最终 Run 发布",
           "parent_delivery_pending": "完整需求交付", "human_blocker": "需要人工答复", "operator_stopped": "已主动停止",
           "execution_failed": "执行失败", "supervision_timeout": "监督等待超时", "requeue_required": "需求已变，需重新排队",
           "review_budget_exhausted": "验收额度耗尽", "modification_budget_exhausted": "开发额度耗尽",
           "ci_development_budget_exhausted": "CI 修复额度耗尽", "worker_unavailable": "Worker 不可用",
           "write_result_unknown": "原写入结果未确认", "write_conflict": "发现第三方写入", "final_approval": "需要最终批准",
           "source_refreshed": "需求观察已记录", "agent_allocated": "调用已分配，尚无启动证明", "agent_dispatched": "调用已派发",
           "worker_started": "执行进程绑定已记录", "worker_exited": "准确进程退出已记录", "raw_returned": "原始返回已耐久保存",
           "role_consumed": "本轮结果已处理", "paused": "遇到人工边界", "human_response": "人工答复", "control_applied": "控制请求已应用",
           "checks_observed": "必需检查观察", "ci_failed": "必需检查未通过", "effect_prepared": "操作意图已保存",
           "effect_dispatched": "外部操作已派发", "effect_observed": "操作结果已读回", "effect_consumed": "操作结果已处理",
           "generation_requeued": "已建立新工作代次", "final_activated": "进入整体交付", "budget_renewed": "额度窗口已显式续期",
           "resource_retired": "资源清理已确认", "cleanup_pending": "交付后仍有资源待清理", "boundary_update": "适用工作边界已更新",
           "issue_link_acknowledged": "可选 Issue 分支关联已确认"},
    "en": {"development": "Development", "developing": "Development", "review": "Review", "reviewing": "Review", "publish": "Publication narrative",
           "publication_pending": "Publication narrative", "candidate": "Saving candidate", "integration": "Integration checks", "workspace": "Preparing checkout",
           "ready_publication": "Preparing publication", "publication_commit": "Saving publication commit", "push": "Publishing branch", "pr_write": "Creating or updating PR",
           "checks": "Waiting for required checks", "waiting_checks": "Waiting for required checks", "approval": "Waiting for final approval", "merge": "Merging", "merging": "Merging",
           "completion_comment": "Recording completion", "close": "Closing Issue", "closing": "Closing Issue", "complete": "Business completed", "completed": "Business completed",
           "parallel": "Concurrent work subjects", "requirements": "Requirements", "ci": "CI repair", "abandoned": "Abandoned", "abandonment_pending": "Abandonment pending",
           "ticket_completed": "Ticket completed", "ready_for_human": "Waiting for human action", "parent_approval_pending": "Parent awaiting approval",
           "run_approval_pending": "Final Run awaiting approval", "run_acceptance_pending": "Whole-Run repair or review", "run_publication_pending": "Final Run publication",
           "parent_delivery_pending": "Parent delivery", "human_blocker": "Human response required", "operator_stopped": "Operator stopped",
           "execution_failed": "Execution failed", "supervision_timeout": "Supervision timed out", "requeue_required": "Requirements changed; requeue required",
           "review_budget_exhausted": "Review allowance exhausted", "modification_budget_exhausted": "Development allowance exhausted",
           "ci_development_budget_exhausted": "CI repair allowance exhausted", "worker_unavailable": "Worker unavailable",
           "write_result_unknown": "Original write outcome unconfirmed", "write_conflict": "Third-party write observed", "final_approval": "Final approval required",
           "source_refreshed": "Requirements observation recorded", "agent_allocated": "Call allocated, without start evidence", "agent_dispatched": "Call dispatched",
           "worker_started": "Execution process binding recorded", "worker_exited": "Exact process exit recorded", "raw_returned": "Original return durably stored",
           "role_consumed": "Round result consumed", "paused": "Operator boundary", "human_response": "Human response", "control_applied": "Control request applied",
           "checks_observed": "Required checks observed", "ci_failed": "Required checks failed", "effect_prepared": "Operation intent stored",
           "effect_dispatched": "External operation dispatched", "effect_observed": "Operation result read back", "effect_consumed": "Operation result consumed",
           "generation_requeued": "New work generation created", "final_activated": "Whole-Run delivery activated", "budget_renewed": "Allowance explicitly renewed",
           "resource_retired": "Resource retirement confirmed", "cleanup_pending": "Business completed with resource cleanup pending", "boundary_update": "Applicable work boundary updated",
           "issue_link_acknowledged": "Optional Issue branch link acknowledged"},
}


def safe_text(value: object) -> str:
    content = str(value)
    content = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", content)
    content = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", content)
    return "".join(character for character in content if character in {"\n", "\t"}
                   or unicodedata.category(character) not in {"Cc", "Cf"})


def _time(value: float | None) -> tuple[str | None, str]:
    if value is None:
        return None, "UTC"
    try:
        stamp = datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise QueryError("incompatible_run_state", "Recorded timestamp is outside the supported calendar range") from error
    try:
        local = stamp.astimezone()
        return local.isoformat(timespec="seconds"), str(local.tzinfo)
    except (OSError, ValueError, OverflowError):
        return stamp.isoformat(timespec="seconds"), "UTC (fallback)"


def _body(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _notification_lines(document: QueryDocument) -> Iterator[str]:
    facts = document.data.get("notifications")
    if not isinstance(facts, Mapping):
        return
    copy = _COPY[document.language]
    configuration = facts["configuration"]
    disabled = isinstance(configuration, Mapping) and configuration.get("enabled") is False
    if configuration is None:
        yield copy["notification_configuration_unknown"]
    elif disabled:
        yield copy["notification_disabled"]
    if facts.get("unavailable_reason"):
        yield text("presentation.notification.unavailable", language=document.language, reason=facts["unavailable_reason"])
    elif facts["journal_state"] == "missing" and not disabled:
        yield copy["notification_journal_missing"]
    count = facts["unconfirmed_count"]
    if count:
        yield text("presentation.notification.pending", language=document.language, count=count)
    if facts["journal_state"] == "available":
        yield copy["notification_binding_unknown"]


def _budget_lines(job: Mapping[str, Any], language: str) -> Iterator[str]:
    copy, terms = _COPY[language], _TERMS[language]
    for budget in job["budgets"]:
        review_limit = budget["review_limit"] if budget["review_limit"] is not None else copy["unknown"]
        yield (f"  {copy['budget']}: {terms.get(budget['purpose'], budget['purpose'])} W{budget['window']} "
               f"D {budget['developments_used']}/{budget['development_limit']} R {budget['reviews_used']}/{review_limit}")


def _history_timing(invocations: list[dict[str, Any]], language: str) -> Iterator[str]:
    facts = invocation_timing(invocations)

    def duration(value: int) -> str:
        minutes, seconds = divmod(value, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"

    if facts["execution_timing_complete"]:
        yield text("presentation.history.round_execution_time", language=language, v1=duration(facts["execution_seconds"]))
    elif "recorded_execution_seconds" in facts:
        yield text("presentation.history.timing_incomplete", language=language)
        yield text("presentation.history.recorded_execution_time", language=language,
                   v1=duration(facts["recorded_execution_seconds"]))
    else:
        yield text("presentation.history.timing_unknown", language=language)


def _history_round(attempt: Mapping[str, Any], issue: int, language: str, details: bool) -> Iterator[str]:
    copy, terms = _COPY[language], _TERMS[language]
    yield text("presentation.history.round_identity", language=language, issue=issue,
               role=copy[attempt["role"]], ordinal=attempt["ordinal"], generation=attempt["generation"],
               window=attempt["window"], purpose=terms.get(attempt["purpose"], attempt["purpose"]))
    outputs = [output for invocation in attempt["invocations"] for output in invocation["outputs"]]
    repairs = sum(output["mode"] == "output-repair" for output in outputs)
    if repairs:
        yield text("presentation.history.output_continuations_included_in_this_round", language=language, v1=repairs)
    start = min(output["started_at"] for output in outputs) if outputs and all(
        output["started_at"] is not None for output in outputs) else None
    end = max(output["exited_at"] for output in outputs) if outputs and all(
        output["exited_at"] is not None for output in outputs) else None
    first, zone = _time(start)
    last, end_zone = _time(end)
    zone = zone if start is not None else end_zone
    yield text("presentation.history.time_range", language=language,
               v1=f"{first or copy['unknown']} → {last or copy['unknown']} ({zone})")
    yield from _history_timing(attempt["invocations"], language)
    previous = None
    for index, invocation in enumerate(attempt["invocations"], 1):
        configuration = (invocation["model"], invocation["effort"], invocation["profile_reference"])
        if details:
            yield text("presentation.history.invocation", language=language, ordinal=index)
        if details or configuration != previous:
            yield f"  {copy['model']}: {configuration[0]} | {copy['effort']}: {configuration[1]}"
        previous = configuration
        if details:
            yield from _history_timing([invocation], language)
            for output in invocation["outputs"]:
                if output["raw_id"] is not None:
                    yield text("presentation.history.original_return", language=language,
                               invocation=invocation["id"], ordinal=output["ordinal"],
                               mode=output["mode"], material=output["raw_id"])
                    yield _body(output["raw_return"])
                for event in output["activity"]:
                    stamp, zone = _time(event["observed_at"])
                    yield text("presentation.history.activity_start" if event["active"] else
                               "presentation.history.activity_end", language=language, stamp=stamp, zone=zone)
    result = attempt["result"]
    if attempt["result_id"] is None:
        yield text("presentation.history.result_not_recorded", language=language)
        return
    if attempt["status"] == "returned" or attempt["outcome"] is None or not isinstance(result, Mapping):
        yield text("presentation.history.return_awaits_consumption" if attempt["status"] == "returned" else
                   "presentation.history.result_not_recorded", language=language)
        if details and not any(output["raw_id"] == attempt["result_id"] for output in outputs):
            yield _body(result)
        return
    if attempt["role"] == "review":
        if attempt["outcome"] in {"pass", "fail", "blocked"}:
            outcome = {"pass": "passed", "fail": "failed", "blocked": "blocked"}[attempt["outcome"]]
            yield text("presentation.history.acceptance_" + outcome, language=language)
        if attempt["status"] == "stale":
            yield text("presentation.history.historical_result", language=language)
        for name, lane in result["checks"].items():
            yield f"  {text('card.check.' + name, language=language)}: {text('card.result.' + lane['status'], language=language)}"
            if details or lane["status"] == "blocked":
                yield f"  {copy['evidence']}: {lane['evidence']}"
            for finding in lane["findings"]:
                yield text("presentation.history.finding_166", language=language, v1=finding)
    elif result.get("summary"):
        yield text("presentation.history.development_summary", language=language) + ": " + result["summary"]
    elif result.get("pr_title"):
        yield f"  {text('presentation.history.pr_title', language=language)}: {result['pr_title']}"
        if details:
            for field in ("commit_message", "pr_body_markdown"):
                label = "commit_message" if field == "commit_message" else "pr_body"
                yield f"  {text('presentation.history.' + label, language=language)}: {result[field]}"
    for blocker in result.get("human_blockers") or []:
        yield text("presentation.history.finding_166", language=language, v1=blocker)


def _history_lines(document: QueryDocument, details: bool) -> Iterator[str]:
    copy, terms = _COPY[document.language], _TERMS[document.language]
    history = document.data["history"]
    yield copy["history"]
    owners = {row["subject"]: row["issue"] for row in document.data["subjects"]}
    attempts = {row["id"]: row for row in history["attempts"]}
    identities = {identity: attempt for attempt in attempts.values() for invocation in attempt["invocations"]
                  for identity in (invocation["id"], *(f"{invocation['id']}:{output['ordinal']}" for output in invocation["outputs"]))}
    shown: set[str] = set()
    milestones = {"final_activated", "paused", "human_response", "control_applied", "checks_observed", "ci_failed",
                  "effect_consumed", "generation_requeued", "budget_renewed", "resource_retired", "cleanup_pending",
                  "boundary_update", "acceptance_promoted"}
    round_events = {"agent_allocated", "agent_dispatched", "worker_started", "worker_exited", "raw_returned",
                    "role_consumed", "output_repair_allocated"}
    for event in history["events"]:
        attempt = attempts.get(event["identity"]) or identities.get(event["identity"])
        if attempt is not None and attempt["id"] not in shown:
            shown.add(attempt["id"])
            yield from _history_round(attempt, owners[attempt["subject"]], document.language, details)
        if event["kind"] not in milestones:
            if event["kind"] in round_events:
                if attempt is not None:
                    continue
                kind = text("presentation.history.page_round_unavailable", language=document.language)
            elif details:
                kind = terms.get(event["kind"], event["kind"])
            else:
                continue
        else:
            kind = (text("presentation.history.acceptance_installed", language=document.language)
                    if event["kind"] == "acceptance_promoted" else terms.get(event["kind"], event["kind"]))
        stamp, zone = _time(event["created_at"])
        subject = f"#{owners[event['subject']]}" if event["subject"] in owners else copy["global"]
        yield f"{stamp or copy['unknown']} [{zone}] {subject} {kind}"
        if event["evidence"] is not None and (details or event["kind"] == "human_response"):
            yield _body(event["evidence"])
    if history["has_more"]:
        yield f"{copy['more']}: --after {history['next_after']}"
    if details:
        yield text("presentation.history.allowance_records", language=document.language)
        for job in document.data["subjects"]:
            if job["budgets"]:
                yield f"{copy['subject']}: #{job['issue']}"
                yield from _budget_lines(job, document.language)


def _lines(document: QueryDocument, details: bool) -> Iterator[str]:
    copy = _COPY[document.language]
    terms = _TERMS[document.language]
    data = document.data
    if document.kind == "runs":
        if not data["runs"]:
            yield copy["no_runs"]
        for run in data["runs"]:
            stamp, zone = _time(run["created_at"])
            identity = json.dumps(run["run_id"], ensure_ascii=True)
            yield f"{run['repository']} Parent #{run['parent']} {identity}"
            yield f"  {run['repository_root'] or copy['unknown']} | {stamp or copy['unknown']} ({zone}) | {', '.join(run['phases'])}"
        return
    yield f"{copy['run']}: {data['repository']} Parent #{data['parent']['number']}"
    if data["parent"]["title"]:
        yield data["parent"]["title"]
    yield f"{copy['status']}: {terms.get(data['status'], copy.get(data['status'], data['status']))} | {copy['phase']}: {terms.get(data['phase'], data['phase'])}"
    tickets = [job for job in data["subjects"] if job["kind"] == "ticket"]
    if tickets:
        progress = text("presentation.status.completed_v1_v3_tickets", language=document.language,
                        v1=sum(job["phase"] == "complete" for job in tickets), v3=len(tickets))
    else:
        progress = text("presentation.status.no_tickets_working_directly_on_the_requirement" if any(
            job["kind"] == "parent_only" for job in data["subjects"]) else
            "presentation.status.progress_cannot_be_confirmed", language=document.language)
    yield f"{text('presentation.status.overall_progress', language=document.language)}: {progress}"
    yield f"{copy['duration']}: {data['elapsed_seconds'] if data['elapsed_seconds'] is not None else copy['incomplete']}"
    yield copy["unknown_live"]
    if document.kind == "history":
        yield from _history_lines(document, details)
        return
    yield from _notification_lines(document)
    for window in data.get("supervision_windows", []):
        target = window["subject"] or copy["global"]
        deadline, zone = _time(window["deadline"])
        yield (f"{copy['supervision']}: {target} — {copy.get(window['kind'], window['kind'])} | "
               f"{copy['remaining']}: {window['remaining_seconds']} | {copy['retry_count']}: {window['retry_count']}")
        yield f"{copy['deadline']}: {deadline} ({zone})"
        if window["latest_observation"] is not None:
            yield _body(window["latest_observation"])
        yield f"{copy['timeout_resume']}: {window['timeout_resume_action']}"
    latest = data.get("latest_resume")
    if isinstance(latest, Mapping):
        progress = latest["resume"]["progress"]
        outcome = progress.get("resume_outcome") if isinstance(progress, Mapping) else None
        label = copy.get("resume_" + str(outcome), copy["resume_applied"] if latest["resume"]["applied"] else copy["resume_unconfirmed"])
        owner = next((job for job in data["subjects"] if job["subject"] == latest["subject"]), None)
        target = f"#{owner['issue']}" if owner else copy["global"]
        yield f"{copy['resume_label']}: {latest['id']} — {target} — {label}"
        if isinstance(progress, Mapping) and progress.get("reason_code"):
            reason = str(progress["reason_code"])
            yield terms.get(reason, reason)
    for wait in data.get("resume_waits", []):
        yield f"{copy['worker_exit_wait']}: #{wait['issue']} ({wait['invocation']})"
    # Put actionable current boundaries before long Findings/evidence.
    for action in data["operator_actions"]:
        scope = copy["local"] if action["scope"] == "local" else copy["global"]
        target = f"#{action['issue']}" if action.get("issue") else data["repository"]
        yield f"{copy['gates']}: {target} — {terms.get(action['reason'], action['reason'])} ({copy['scope']}: {scope})"
        operation = action["operation"]
        yield f"{copy['next']}: {copy[operation]}" + (f" — {action['next_action']}" if operation != "inspect" else "")
        if action["evidence"] is not None:
            yield _body(action["evidence"])
    if not data["operator_actions"]:
        next_action = copy["complete"] if data["next_action"] == "无" else copy.get(data["next_action"], data["next_action"])
        yield f"{copy['next']}: {next_action or copy['complete']}"
    for job in data["subjects"]:
        yield f"{copy['subject']}: #{job['issue']} {job['title'] or ''} — {terms.get(job['phase'], job['phase'])} ({terms.get(job['purpose'], job['purpose'])})"
        pending = job["pending_attempt"]
        if pending:
            label = ("触发待办的 Agent" if document.language == "zh" else "Agent that triggered this boundary") if job["gates"] else copy["agent"]
            yield f"{label}: {copy.get(pending['role'], pending['role'])} #{pending['ordinal']}"
            for invocation in pending["invocations"]:
                yield f"  {copy['model']}: {invocation['model']} | {copy['effort']}: {invocation['effort']}"
                output = invocation["outputs"][-1]
                if (output["dispatch_state"] == "exited" and output["started_at"] is not None
                        and output["exited_at"] is not None and output["exited_at"] >= output["started_at"]):
                    stamp, zone = _time(output["exited_at"])
                    yield text("presentation.status.recorded_worker_exit", language=document.language, stamp=stamp, zone=zone)
                elif output["started_at"] is not None:
                    yield f"  {copy['started']}"
                yield f"  {copy['duration']}: {invocation['elapsed_seconds'] if invocation['elapsed_seconds'] is not None else copy['incomplete']}"
        if document.kind != "status" or job["phase"] not in {"complete", "abandoned"}:
            yield from _budget_lines(job, document.language)
        if job["delivery_cleanup"]:
            cleanup = job["delivery_cleanup"]
            yield f"  {copy['cleanup']}: {copy['complete'] if cleanup['state'] == 'complete' else copy['pending']}"
        review = job["review"]
        review_state = job["review_state"] if document.kind == "status" else None
        if review_state in {"stale", "pending", "unknown", "not_recorded"}:
            yield text("presentation.status.review_state_" + review_state, language=document.language)
        elif review:
            yield f"{text('presentation.status.recorded_review', language=document.language)}: {text('card.result.' + review['outcome'], language=document.language)}"
            if review["outcome"] == "pass":
                for name, lane in review["artifact"]["checks"].items():
                    yield f"  {text('card.check.' + name, language=document.language)}: {text('card.result.' + lane['status'], language=document.language)}"
                    yield f"  {copy['evidence']}: {lane['evidence']}"
            else:
                for finding in review_findings(review["artifact"]):
                    yield f"{copy['findings']}: {finding}"


def render(document: QueryDocument, *, style: Literal["json", "plain", "rich"], details: bool, stream: TextIO) -> None:
    if document.kind == "settings":
        render_settings(document.data, as_json=style == "json", stream=stream)
        return
    if style == "json":
        encoded = json.dumps(document.data, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > QUERY_LIMIT:
            raise QueryError("query_limit", "Public response exceeds its bounded limit")
        print(encoded, file=stream)
        return
    if style == "plain":
        for line in _lines(document, details):
            print(safe_text(line), file=stream)
        return
    from rich.console import Console
    from rich.text import Text
    console = Console(file=stream, markup=False, highlight=False)
    for line in _lines(document, details):
        console.print(Text(safe_text(line)))


def _setting_lines(values: Mapping[str, Any], language: str, indent: int = 0) -> Iterator[str]:
    for key, value in values.items():
        try:
            label = text("cli.settings.field." + key, language=language)
        except KeyError:
            label = key
        prefix = " " * indent + label + ":"
        if isinstance(value, dict) and value:
            yield prefix
            yield from _setting_lines(value, language, indent + 2)
        elif isinstance(value, list) and value:
            yield prefix
            for item in value:
                if isinstance(item, dict):
                    yield from _setting_lines(item, language, indent + 2)
                else:
                    yield " " * (indent + 2) + str(item)
        else:
            rendered = (text("cli.settings.unset", language=language) if value is None or value == [] or value == {} else
                        text("cli.settings.yes" if value else "cli.settings.no", language=language) if isinstance(value, bool) else str(value))
            yield f"{prefix} {rendered}"


def render_settings(values: Mapping[str, Any], *, as_json: bool, stream: TextIO) -> None:
    if as_json:
        print(json.dumps(values, ensure_ascii=False, sort_keys=True), file=stream)
        return
    language = str(values["language"])
    print(text("settings.run_title" if values.get("scope") == "run" else "settings.personal_title", language=language), file=stream)
    for line in _setting_lines(values, language):
        print(safe_text(line), file=stream)
