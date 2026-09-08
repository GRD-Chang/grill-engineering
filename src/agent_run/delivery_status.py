from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from agent_run.presentation_helpers import (
    current_work_subject,
    delivery_object_label,
    elapsed_seconds_since,
    human_next_action,
)

def status_progress_view(
    state: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    """Build the operator-facing status facts without weakening the audit view."""

    parent = state.get("parent")
    parent_view = parent if isinstance(parent, dict) else {}
    invocation = _progress_invocation(state, audit)
    worker = audit.get("worker")
    current = current_work_subject(state)
    current_object = _current_object(state, current)
    current_agent = _agent_view(
        state,
        invocation,
        worker if isinstance(worker, dict) else None,
        current_object=current_object,
    )
    activity = invocation_activity(invocation, audit)
    if current_agent is not None:
        current_agent["activity"] = activity
        if activity != "running":
            current_agent.update(
                is_active=False,
                duration_seconds=_invocation_duration(invocation or {}),
                remaining_seconds=None,
            )
    return {
        "repository": state.get("repository"),
        "parent": {
            "number": parent_view.get("number"),
            "title": parent_view.get("title"),
        },
        "status": audit.get("status"),
        "phase": audit.get("phase"),
        "current_object": current_object,
        "ticket_progress": _ticket_progress(state),
        "round_progress": _round_progress(current, audit.get("review_budget")),
        "run_repair": _run_repair_progress(audit.get("run_repair")),
        "elapsed_seconds": audit.get("elapsed_seconds"),
        "current_agent": current_agent,
        "execution_activity": activity,
        "findings": _current_findings(state),
        "next_action": audit.get("next_action"),
    }


def invocation_activity(
    invocation: dict[str, Any] | None, audit: dict[str, Any]
) -> str:
    """Project execution evidence separately from persisted Invocation status."""
    if not invocation or invocation.get("status") not in {"running", "resuming"}:
        return "not_running"
    control = audit.get("executor_control")
    activity = control.get("activity") if isinstance(control, dict) else "unknown"
    if activity == "not_running":
        return "interrupted"
    if activity != "running":
        return "unknown"
    if invocation.get("recovery_waiting") is True:
        return (
            "capacity_wait"
            if invocation.get("recovery_kind") == "capacity"
            else "recovery_wait"
        )
    return "running"


def execution_guidance(state: dict[str, Any], activity: str) -> str:
    if activity == "capacity_wait":
        return "模型容量不足，等待 30 秒后自动续接；当前 Agent 未在生成，可使用 stop 停止。"
    if activity == "recovery_wait":
        return "执行异常，正在自动续接原工作；当前 Agent 未在生成。"
    if activity == "interrupted":
        return f"执行已中断，等待恢复；使用 agent-run resume {state.get('run_id')} 继续原工作。"
    return "运行状态无法确认；请先检查 Executor Host 与 Task Control，再决定是否恢复。"


def print_status_progress(
    state: dict[str, Any],
    audit: dict[str, Any],
    view: dict[str, Any],
    *,
    display_term: Callable[[object], object],
    print_operator_action: Callable[[dict[str, Any]], None],
) -> None:
    parent = view["parent"]
    print(f"Repository: {view['repository'] or 'unknown'}")
    print(
        "Parent:     "
        f"#{parent.get('number', '?')} {parent.get('title') or '未命名 Parent'}"
    )
    print(f"Status:     {display_term(view['status'])}")
    phase = display_term(view["phase"])
    print(f"阶段:       {phase if phase is not None else '未进入具体阶段'}")
    print(f"当前对象:   {view['current_object']}")

    print("\n进度")
    tickets = view["ticket_progress"]
    if tickets["total"]:
        print(f"  Tickets              {tickets['completed']} / {tickets['total']} 已完成")
    rounds = view["round_progress"]
    if rounds is not None:
        print(
            f"  {rounds['development_label']:<20}"
            f"{rounds['development_attempts']} / {rounds['development_limit']} 轮"
        )
        print(
            f"  {rounds['review_label']:<20}"
            f"{rounds['reviewer_invocations']} / {rounds['reviewer_limit']} 轮"
        )
    print(f"  总运行时长           {_duration(view['elapsed_seconds'])}")

    repair = view["run_repair"]
    if repair is not None:
        print("\nRun Repair")
        print(f"  Run Acceptance Generation {repair['acceptance_generation']}")
        print(f"  Repair Cycle Generation {repair['repair_cycle_generation']}")
        print(
            "  Candidate 验证状态 "
            f"{display_term(repair['candidate_validation_status'])}"
        )
        print(
            "  Code Modification Attempts "
            f"{repair['code_modification_attempts']} / "
            f"{repair['code_modification_limit']}"
        )

    print("\n当前工作")
    executor_control = audit.get("executor_control")
    if (
        isinstance(executor_control, dict)
        and executor_control.get("activity") == "unknown"
    ):
        print("  Agent 活跃状态: 无法确认（运行状态无法确认）")
    activity = view["execution_activity"]
    if activity == "interrupted":
        print("  执行已中断，等待恢复")
    agent = view["current_agent"]
    if agent is None:
        print("  当前没有运行中的 Agent")
    else:
        agent_label = "当前 Agent" if agent["is_active"] else "最近 Agent"
        print(f"  {agent_label}: {agent['role']} · {agent['object']}")
        print(f"  模型                  {agent['model']}")
        print(f"  推理强度              {agent['reasoning_effort']}")
        if agent.get("started_at"):
            print(f"  开始时间              {agent['started_at']}")
        if agent["duration_seconds"] is None:
            print("  实际执行时长未知")
        if agent["duration_seconds"] is not None:
            duration_label = "本轮已运行" if agent["is_active"] else "本轮耗时"
            print(f"  {duration_label:<20}{_duration(agent['duration_seconds'])}")
        if agent["remaining_seconds"] is not None:
            print(f"  本轮剩余              {_duration(agent['remaining_seconds'])}")

    findings = view["findings"]
    print(f"\n当前 Findings（{len(findings)}）")
    if findings:
        for index, finding in enumerate(findings, start=1):
            print(f"  {index}. {finding}")
    else:
        print("  无")

    if activity in {"interrupted", "unknown", "capacity_wait", "recovery_wait"}:
        print("\n下一步")
        print(f"  {execution_guidance(state, activity)}")
        return

    operator_action = audit.get("operator_action")
    if not isinstance(operator_action, dict):
        _print_wait(
            audit.get("supervision"),
            display_term=display_term,
            run_id=state.get("run_id"),
        )
        _print_cleanup(audit.get("delivery_cleanup"))
        _print_scope_change(audit.get("scope_change"))
        print("\n下一步")
        cleanup = audit.get("delivery_cleanup")
        if isinstance(cleanup, dict) and cleanup.get("status") == "cleanup_pending":
            print("  查看 --json 中的交付清理诊断，整理受管工作区后按恢复操作继续。")
        else:
            print(
                "  下一步: "
                f"{human_next_action(view['next_action'], run_id=state.get('run_id'))}"
            )
        current_invocation = audit.get("agent_invocation")
        running_invocation = (
            current_invocation if isinstance(current_invocation, dict) else None
        )
        print(
            "  "
            f"{_operator_instruction(state, running_invocation)}"
        )
        return

    _print_wait(
        audit.get("supervision"),
        display_term=display_term,
        run_id=state.get("run_id"),
    )
    _print_scope_change(audit.get("scope_change"))
    print()
    print_operator_action(operator_action)


def _agent_view(
    state: dict[str, Any],
    invocation: dict[str, Any] | None,
    worker: dict[str, Any] | None,
    *,
    current_object: str,
) -> dict[str, Any] | None:
    if invocation is None and worker is None:
        return None
    source = invocation or {}
    role = (
        source.get("invocation_role")
        or source.get("role")
        or (worker or {}).get("role")
        or "Agent"
    )
    started_at = source.get("started_at")
    deadline_at = source.get("deadline_at")
    is_active = source.get("status") in {"running", "resuming"}
    work_subject = source.get("work_subject")
    agent_object = (
        _subject_label(state, work_subject)
        if isinstance(work_subject, str) and work_subject
        else current_object
    )
    return {
        "role": _role_label(str(role)),
        "started_at": started_at,
        "model": source.get("model") or "未绑定",
        "reasoning_effort": source.get("reasoning_effort") or "未绑定",
        "duration_seconds": (
            elapsed_seconds_since(started_at)
            if is_active
            else _invocation_duration(source)
        ),
        "remaining_seconds": _remaining_until(deadline_at) if is_active else None,
        "is_active": is_active and agent_object == current_object,
        "object": agent_object,
    }


def _progress_invocation(
    state: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any] | None:
    active = audit.get("agent_invocation")
    if isinstance(active, dict):
        return active
    history = state.get("agent_invocation_history")
    if isinstance(history, list):
        for invocation in reversed(history):
            if isinstance(invocation, dict):
                return invocation
    return None


def _run_repair_progress(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        "acceptance_generation": value.get("acceptance_generation"),
        "repair_cycle_generation": value.get("repair_cycle_generation"),
        "candidate_validation_status": value.get("candidate_validation_status"),
        "cycle_status": value.get("cycle_status"),
        "code_modification_attempts": value.get("code_modification_attempts"),
        "code_modification_limit": value.get("code_modification_limit"),
    }


def _ticket_progress(state: dict[str, Any]) -> dict[str, int]:
    graph = state.get("ticket_graph")
    tickets = graph.get("tickets") if isinstance(graph, dict) else None
    total = len(tickets) if isinstance(tickets, dict) else 0
    jobs = state.get("ticket_jobs")
    graph_numbers = set(tickets) if isinstance(tickets, dict) else set()
    completed = (
        sum(
            1
            for number, job in jobs.items()
            if (not graph_numbers or str(number) in graph_numbers)
            if isinstance(job, dict)
            and job.get("phase") in {"completed", "merged"}
        )
        if isinstance(jobs, dict)
        else 0
    )
    return {"completed": completed, "total": total}


def _round_progress(
    current: tuple[str, dict[str, Any]] | None, budget: object
) -> dict[str, Any] | None:
    if not isinstance(budget, dict):
        return None
    location = current[0] if current is not None else ""
    if location.startswith("ticket:"):
        prefix = "Ticket"
    elif location == "parent":
        prefix = "Parent"
    else:
        prefix = "Run"
    return {
        **budget,
        "development_label": f"{prefix} Development",
        "review_label": f"{prefix} Review",
    }


def _current_object(
    state: dict[str, Any], current: tuple[str, dict[str, Any]] | None
) -> str:
    if current is not None:
        return delivery_object_label(state, current[0])
    return "Delivery Run"


def _current_findings(state: dict[str, Any]) -> list[str]:
    for subject in _current_subjects(state):
        artifact = _latest_artifact(subject)
        if artifact is not None:
            findings = _artifact_findings(artifact)
            if findings:
                return findings
    return []


def _current_subjects(state: dict[str, Any]) -> Iterable[dict[str, Any]]:
    current = current_work_subject(state)
    selected = current[1] if current is not None else None
    if selected is not None:
        yield selected
    acceptance = state.get("run_acceptance")
    repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
    active = state.get("active_ticket_job")
    current_ticket = (
        active
        if isinstance(active, dict)
        and active.get("phase") not in {"completed", "merged", "abandoned"}
        else None
    )
    for subject in (
        current_ticket,
        state.get("parent_job"),
        repair,
        acceptance,
        state.get("run_publication"),
    ):
        if isinstance(subject, dict) and subject is not selected:
            yield subject


def _latest_artifact(subject: dict[str, Any]) -> dict[str, Any] | None:
    record = subject.get("acceptance_record")
    record_artifact = record.get("artifact") if isinstance(record, dict) else None
    if isinstance(record_artifact, dict):
        return record_artifact
    artifact = subject.get("acceptance_artifact")
    if isinstance(artifact, dict):
        return artifact
    budget = subject.get("review_budget")
    artifacts = budget.get("review_artifacts") if isinstance(budget, dict) else None
    if isinstance(artifacts, list) and artifacts:
        latest = artifacts[-1]
        latest_artifact = latest.get("artifact") if isinstance(latest, dict) else None
        if isinstance(latest_artifact, dict):
            return latest_artifact
    return None


def _artifact_findings(artifact: dict[str, Any]) -> list[str]:
    checks = artifact.get("checks")
    if not isinstance(checks, dict):
        return []
    return [
        finding
        for lane in ("e2e", "standards", "spec")
        if isinstance((check := checks.get(lane)), dict)
        and isinstance((lane_findings := check.get("findings")), list)
        for finding in lane_findings
        if isinstance(finding, str)
    ]


def _subject_label(state: dict[str, Any], subject: str) -> str:
    return delivery_object_label(state, subject)


def _role_family(role: str) -> str | None:
    if role == "development":
        return "development"
    if role in {"reviewer", "fresh_acceptance"}:
        return "review"
    if role in {"publication", "final_publication"}:
        return "publication"
    return None


def _role_label(role: str) -> str:
    family = _role_family(role)
    if family is None:
        return role
    return {
        "development": "Development Agent",
        "review": "Review Agent",
        "publication": "Publication Agent",
    }[family]


def _invocation_duration(invocation: dict[str, Any]) -> int | None:
    start = _parse_optional_timestamp(invocation.get("started_at"))
    end = _parse_optional_timestamp(invocation.get("ended_at"))
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _remaining_until(value: object) -> int | None:
    deadline = _parse_optional_timestamp(value)
    return max(0, int((deadline - datetime.now(UTC)).total_seconds())) if deadline else None


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _parse_optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_timestamp(value).astimezone(UTC)
    except ValueError:
        return None


def _duration(seconds: object) -> str:
    if not isinstance(seconds, int):
        return "未知"
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分钟"
    return f"{seconds} 秒"


def _operator_instruction(
    state: dict[str, Any], invocation: dict[str, Any] | None
) -> str:
    if invocation is not None and invocation.get("status") in {"running", "resuming"}:
        return "你暂时无需操作。"
    if state.get("status") in {"completed", "abandoned"}:
        return "无需操作。"
    return "按上述命令继续；不要重复启动另一个 Run。"


def _print_wait(
    wait: object,
    *,
    display_term: Callable[[object], object],
    run_id: object,
) -> None:
    if not isinstance(wait, dict):
        return
    print("\n当前等待")
    print(f"  等待种类: {display_term(wait.get('kind'))}")
    print(f"  等待对象: {wait.get('subject')}")
    boundary_status = (
        "已记录（完整值见 --json）"
        if wait.get("head_sha") is not None or wait.get("base_sha") is not None
        else "尚未取得"
    )
    print(f"  等待 head/base: {boundary_status}")
    print(
        "  等待窗口: "
        f"截止={wait.get('deadline')}；剩余={wait.get('remaining_seconds')} 秒"
    )
    print(f"  重试次数: {wait.get('retry_count')}")
    observation = wait.get("latest_observation")
    if isinstance(observation, dict):
        print(f"  最新观测: {observation.get('message')}")
    else:
        print("  最新观测: 无")
    if wait.get("timeout_resume_action"):
        print(
            "  超时恢复: "
            f"{human_next_action(wait['timeout_resume_action'], run_id=run_id)}"
        )
    if wait.get("credential_failure_class"):
        print(f"  凭据失败类别: {wait['credential_failure_class']}")
    if wait.get("credential_http_status") is not None:
        print(f"  凭据 HTTP 状态: {wait['credential_http_status']}")


def _print_cleanup(cleanup: object) -> None:
    if not isinstance(cleanup, dict):
        return
    print("\n交付清理")
    print(f"  状态: {cleanup.get('status')}")
    items = cleanup.get("items")
    if isinstance(items, list):
        pending = sum(1 for item in items if isinstance(item, dict))
        print(f"  已保留 {pending} 个受管工作区；完整诊断与恢复操作见 --json")


def _print_scope_change(scope_change: object) -> None:
    if not isinstance(scope_change, dict):
        return
    summary = scope_change.get("graph_change_summary")
    print("\nTicket Graph 变化")
    if isinstance(summary, dict):
        print(f"  {summary.get('summary')}")
