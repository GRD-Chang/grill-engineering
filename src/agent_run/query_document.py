"""Short-lived query documents; emitting never reads or repairs a Run.

The same report supplies machine data and human sections. Nodes are consumed
in order: plain output is incremental, while Rich buffers only its current
panel. A later derivation failure therefore preserves the original prefix.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from agent_run import delivery_history as history, delivery_status as status
from agent_run.final_approval_operation import approval_failure_text, final_approval_cleanup_pending
from agent_run.guidance_actions import NextAction, render_next_action
from agent_run.messages import display_text as copy, selected_language
from agent_run.operator_action_presentation import (
    human_action_type, human_preserved_results, human_trigger_invocation, operator_action_items,
)
from agent_run.presentation_helpers import (
    human_agent_role, human_delivery_object, human_next_action, human_pause_reason,
    human_status_term, local_timestamp, status_diagnostic_command, terminal_safe,
    unknown_execution_guidance,
)
from agent_run.status_actions import approval_lines, controller_work, delivery_result_lines, status_heading, status_timing
from agent_run.waiting_presentation import scheduler_notice, waiting_presentation


@dataclass(frozen=True)
class QueryDocument:
    kind: Literal["status", "history"]
    state: dict[str, Any]
    audit: dict[str, Any]
    progress: dict[str, Any]
    machine: dict[str, Any] | None = None


@dataclass(frozen=True)
class Line:
    text: str
    style: str = ""
    outside: bool = False


@dataclass(frozen=True)
class Label:
    name: str
    value: object


@dataclass(frozen=True)
class Panel:
    title: str
    border: str
    layout: Literal["identity", "group", "history"]
    title_style: str = "bold"


Node = Line | Label | Panel | None
_PAUSED = {"interrupted", "unknown", "capacity_wait", "recovery_wait"}


def emit(document: QueryDocument, *, style: Literal["plain", "rich", "json"], details: bool = False) -> None:
    if style == "json":
        print(json.dumps(document.machine, ensure_ascii=False, sort_keys=True))
        return
    nodes = (_status(document, style == "rich") if document.kind == "status"
             else _history(document, style == "rich", details))
    if style == "plain":
        for node in nodes:
            if isinstance(node, Line):
                print(node.text)
        return
    from rich.console import Console, Group
    from rich.padding import Padding
    from rich.panel import Panel as RichPanel
    from rich.table import Table
    from rich.text import Text

    console = Console(highlight=False, soft_wrap=False)
    panel: Panel | None = None
    rows: list[Any] = []
    for node in nodes:
        if isinstance(node, Panel):
            panel, rows = node, []
        elif isinstance(node, Label):
            line = Text()
            line.append(f"{terminal_safe(node.name)}：", style="bold")
            line.append(terminal_safe(copy('presentation.status.unknown') if node.value is None else node.value))
            rows.append((node.name, line, node.value))
        elif isinstance(node, Line):
            if node.outside:
                console.file.write(node.text + "\n")
            else:
                rows.append(Text(node.text, style=node.style))
        elif panel is not None:
            if panel.layout == "identity":
                body: Any = Table.grid(expand=True, padding=(0, 1))
                narrow = console.width < 72
                if not narrow:
                    body.add_column(style="bold", no_wrap=True)
                body.add_column(ratio=1, overflow="fold")
                for name, labeled, value in rows:
                    if narrow:
                        body.add_row(labeled)
                    else:
                        body.add_row(name, Text(terminal_safe(copy('presentation.status.unknown') if value is None else value)))
            elif panel.layout == "history":
                body = Table.grid(expand=True, padding=(0, 0))
                body.add_column(no_wrap=False, overflow="fold")
                for row in rows:
                    line = row[1] if isinstance(row, tuple) else row
                    indent = len(line.plain) - len(line.plain.lstrip(" "))
                    body.add_row(Padding(line[indent:], (0, 0, 0, indent)) if indent else line)
            else:
                body = Group(*(row[1] if isinstance(row, tuple) else row for row in rows))
            rendered = RichPanel(body, title=Text(panel.title, style=panel.title_style),
                                 border_style=panel.border, expand=True)
            if panel.layout == "history":
                console.print(rendered, overflow="fold", crop=False)
            else:
                console.print(rendered)
            panel = None


def _action_lines(state: dict[str, Any], action: dict[str, Any]) -> Iterator[Line]:
    decision = action.get("action")
    next_action = None
    if isinstance(decision, dict):
        next_action = render_next_action(NextAction(**decision), state,
                                         language=selected_language(state), human=True)
    yield Line(copy('presentation.status.action_required'))
    yield Line(copy('presentation.status.type_v1', v1=terminal_safe(human_action_type(action['type']))))
    yield Line(copy('presentation.status.object_v1', v1=terminal_safe(human_delivery_object(action['object']))))
    yield Line(copy('presentation.status.phase_v1_205', v1=terminal_safe(human_status_term(action['phase']))))
    for reason in action["reasons"]:
        message = reason if action["type"] == "Human Blocker" else human_pause_reason(reason)
        yield Line(copy('presentation.status.reason_v1', v1=terminal_safe(message)))
    triggering = action.get("trigger_invocation")
    if isinstance(triggering, dict):
        yield Line(f"{copy('presentation.status.agent_that_triggered_the_blocker')}: {human_trigger_invocation(triggering)}")
    yield Line(copy('presentation.status.preserved_work_v1', v1=terminal_safe(human_preserved_results(action['preserved']))))
    yield Line(copy('presentation.status.the_entire_run_is_paused_other_tickets_will_not_continue')
               if action.get("run_paused", True) else copy('presentation.status.local_operator_gate'))
    if action["type"] == "Review Budget Checkpoint":
        yield Line(copy('presentation.status.resuming_grants_the_configured_development_and_review_allowance_t'))
    yield Line(copy('presentation.status.next_step_v1_36', v1=terminal_safe(human_next_action(
        next_action if isinstance(decision, dict) else action['next_action'],
    ))))


def _field(rich: bool, label: str, value: object, plain: str) -> Node:
    return Label(label, value) if rich else Line(plain)


def _findings(findings: object, rich: bool) -> Iterator[Node]:
    values = findings if isinstance(findings, list) else []
    if rich:
        yield Panel(copy('presentation.status.current_findings_v1', v1=len(values)),
                    "red" if values else "bright_black", "group")
    else:
        yield Line(copy('presentation.status.ncurrent_findings_v1', v1=len(values)))
    if not values:
        yield Line(copy('presentation.status.no_findings_for_this_work_and_current_candidate'
                        if rich else 'presentation.status.none_123'))
    for index, finding in enumerate(values, start=1):
        value = str(finding) if rich else terminal_safe(finding)
        match = status._FINDING_PARTS.fullmatch(value)
        if match is None:
            if rich:
                yield Line(f"{index}.", "bold red")
                yield Line(terminal_safe(finding))
            else:
                yield Line(f"  {index}. {value}")
        else:
            yield Line(f"{index}." if rich else f"  {index}.", "bold red" if rich else "")
            for name, text in zip(("problem", "evidence", "required_fix", "revalidation"), match.groups()):
                label = copy('presentation.status.' + name)
                yield (Label(label, text) if rich else Line(f"     {terminal_safe(label)}：{terminal_safe(text)}"))
    if rich:
        yield None


def _cleanup(cleanup: object, rich: bool) -> Iterator[Node]:
    if not isinstance(cleanup, dict):
        return
    if rich:
        value = {"completed": copy('presentation.status.completed'),
                 "cleanup_pending": copy('presentation.status.cleanup_pending'),
                 "pending": copy('presentation.status.pending')}.get(str(cleanup.get("status")), cleanup.get("status"))
        yield Label(copy('presentation.status.workspace_cleanup'), value)
    else:
        yield Line(copy('presentation.status.nworkspace_cleanup'))
        yield Line(copy('presentation.status.status_v1_128', v1=terminal_safe(human_status_term(cleanup.get('status')))))
        if cleanup.get("status") == "completed":
            return
    reason = status._cleanup_reason(cleanup)
    if reason:
        yield _field(rich, copy('presentation.status.preservation_reason'), reason,
                     copy('presentation.status.preservation_reason_v1', v1=terminal_safe(reason)))
    items = cleanup.get("items")
    if not rich and isinstance(items, list):
        pending = sum(isinstance(item, dict) for item in items)
        if pending:
            yield Line(copy('presentation.status.preserved_v1_development_workspaces', v1=pending))
    for path in status._cleanup_paths(cleanup):
        yield _field(rich, copy('presentation.status.workspace_requiring_attention'), path,
                     copy('presentation.status.workspace_requiring_attention_v1', v1=terminal_safe(path)))


def _scope_change(change: object, rich: bool) -> Iterator[Node]:
    if not isinstance(change, dict):
        return
    summary = change.get("graph_change_summary")
    if rich:
        yield Label(copy('presentation.status.task_and_dependency_changes'),
                    summary.get("summary") if isinstance(summary, dict) else copy('presentation.status.see_json'))
    else:
        yield Line(copy('presentation.status.ntask_and_dependency_changes'))
        if isinstance(summary, dict):
            yield Line(f"  {terminal_safe(summary.get('summary'))}")


def _status(document: QueryDocument, rich: bool) -> Iterator[Node]:
    state, audit, view = document.state, document.audit, document.progress
    raw_status = str(view.get("status") or "")
    if rich:
        yield Panel(copy('presentation.status.agent_run_delivery_status'), status._status_style(raw_status), "identity")
        label = (copy('presentation.status.merged_cleanup_pending') if raw_status == "completed"
                 and final_approval_cleanup_pending(state) else human_status_term(raw_status))
        label = status_heading(state, view, label)
        parent = view.get("parent")
        parent = parent if isinstance(parent, dict) else {}
        yield Label(copy('presentation.status.task'), f"{view.get('repository') or 'unknown'} · #{parent.get('number', '?')} {parent.get('title') or copy('presentation.status.untitled_requirement')}")
        yield Label(copy('presentation.status.status'), f"{status._status_symbol(raw_status)} {status._human_conclusion(view.get('conclusion'))} · {label}")
        phase = status._phase_term(view.get("phase"), view.get("current_object"), human_status_term)
        yield Label(copy('presentation.status.phase'), phase or copy('presentation.status.no_specific_phase_yet'))
        yield Label(copy('presentation.status.current_object'), human_delivery_object(view.get('current_object')))
    else:
        parent = view["parent"]
        yield Line(copy('presentation.status.repository_v1', v1=terminal_safe(view['repository'] or copy('presentation.status.unknown'))))
        yield Line(copy('presentation.status.parent_issue_v1_v3', v1=terminal_safe(parent.get('number', '?')), v3=terminal_safe(parent.get('title') or copy('presentation.status.untitled_requirement'))))
        label = (copy('presentation.status.merged_cleanup_pending') if state.get("status") == "completed"
                 and final_approval_cleanup_pending(state) else human_status_term(view["status"]))
        yield Line(copy('presentation.status.status_v1', v1=terminal_safe(status_heading(state, view, label))))
    if notice := scheduler_notice(view.get("scheduler")):
        yield Label(copy('presentation.status.status'), notice) if rich else Line(notice)
    for result in delivery_result_lines(state):
        yield _field(rich, copy('presentation.status.delivery_result'), result,
                     copy('presentation.status.delivery_result_v1', v1=result))
    if not rich:
        phase = status._phase_term(view["phase"], view["current_object"], human_status_term)
        yield Line(copy('presentation.status.phase_v1', v1=terminal_safe(phase if phase is not None else copy('presentation.status.no_specific_phase_yet'))))
        yield Line(copy('presentation.status.conclusion_v1', v1=terminal_safe(status._human_conclusion(view['conclusion']))))
        yield Line(copy('presentation.status.current_object_v1', v1=terminal_safe(human_delivery_object(view['current_object']))))
    for detail in approval_lines(state):
        yield Label(copy('presentation.status.approve_merge'), detail) if rich else Line(f"  {detail}")
    control = audit.get("executor_control")
    failure = control.get("approval_failure") if isinstance(control, dict) else None
    if isinstance(failure, dict):
        text = approval_failure_text(failure)
        yield Label(copy('presentation.status.approve_merge'), text) if rich else Line(f"  {terminal_safe(text)}")
    timing = status_timing(state)
    if "total_seconds" in timing:
        value = status._duration(timing['total_seconds'])
        yield _field(rich, copy('presentation.status.total_agent_execution_time'), value,
                     copy('presentation.status.total_agent_execution_time_v1', v1=value))
    elif timing.get("execution_timing_complete") is False:
        text = copy('presentation.status.timing_incomplete')
        if not rich:
            yield Line(text)
        if type(timing.get("recorded_execution_seconds")) is int:
            recorded = copy('presentation.status.recorded_execution_time', v1=status._duration(timing['recorded_execution_seconds']))
            if rich:
                text += " · " + recorded
            else:
                yield Line(recorded)
        if rich:
            yield Label(copy('presentation.status.total_agent_execution_time'), text)
    if not rich:
        yield Line(copy('presentation.status.nprogress'))
    progress = status._ticket_progress_text(state, view)
    if not rich:
        yield Line(f"  {progress}")
        for line in status._multi_ticket_lines(view):
            yield Line(f"  {terminal_safe(line)}")
    rounds = view.get("round_progress") if rich else view["round_progress"]
    if (isinstance(rounds, dict) if rich else rounds is not None) and state.get("status") != "completed":
        if rich:
            yield Label(copy('presentation.status.execution_allowance'), "；".join(status._round_details(rounds)))
        else:
            yield Line(copy('presentation.status.nexecution_allowance'))
            for line in status._round_details(rounds):
                yield Line(f"  {terminal_safe(line)}")
    for key in ("integration_development", "integration_resync", "ci_recovery"):
        value = view.get(key)
        if not isinstance(value, dict):
            continue
        if key == "ci_recovery":
            text = status._ci_recovery_usage(value)
        elif key == "integration_resync" and rich:
            text = copy('presentation.status.integration_resync_values', used=value.get('used'), limit=value.get('limit'), window=value.get('window'))
        else:
            text = copy('presentation.status.' + key + '_usage', used=value.get('used'), limit=value.get('limit'), window=value.get('window'))
        yield Label(copy('presentation.status.' + key + '_label'), text) if rich else Line(text)
        if key == "ci_recovery":
            for detail in status._ci_recovery_details(value):
                yield Label(copy('presentation.status.ci_recovery_label'), detail) if rich else Line(detail)
    if rich:
        repair = view.get("run_repair")
        if isinstance(repair, dict):
            progress += copy('presentation.status.run_repair_candidate_acceptance_v1_code_modifications_v3_v5', v1=human_status_term(repair.get('candidate_validation_status')), v3=repair.get('code_modification_attempts'), v5=repair.get('code_modification_limit'))
        if view.get("elapsed_seconds") is not None:
            progress += copy('presentation.status.task_elapsed_time_v1_48', v1=status._duration(view['elapsed_seconds']))
        yield Label(copy('presentation.status.overall_progress'), progress)
        ticket_lines = status._multi_ticket_lines(view)
        if ticket_lines:
            yield Label(copy('presentation.status.ticket_details'), "\n".join(ticket_lines))
    else:
        if view["elapsed_seconds"] is not None:
            yield Line(copy('presentation.status.task_elapsed_time_v1', v1=status._duration(view['elapsed_seconds'])))
        repair = view["run_repair"]
        if repair is not None:
            yield Line(copy('presentation.status.nrun_repair'))
            yield Line(copy('presentation.status.current_candidate_acceptance_v1', v1=terminal_safe(human_status_term(repair['candidate_validation_status']))))
            yield Line(copy('presentation.status.code_modifications_v1_v3', v1=repair['code_modification_attempts'], v3=repair['code_modification_limit']))
    automatic = controller_work(state, audit)
    waiting = waiting_presentation(state, audit)
    if rich:
        agent = view.get("current_agent") if state.get("status") != "completed" else None
        if waiting is not None:
            text = f"{waiting.work}\n{waiting.activity}"
        elif isinstance(agent, dict):
            label = copy('presentation.status.current_agent' if agent.get('is_active') else 'presentation.status.latest_agent')
            text = copy('presentation.status.v0_v2_v4_nmodel_v6_reasoning_effort_v8_n', v0=label, v2=human_agent_role(agent.get('role')), v4=human_delivery_object(agent.get('object')), v6=agent.get('model') or copy('presentation.status.unbound'), v8=agent.get('reasoning_effort') or copy('presentation.status.unbound'))
            for field, key in (("duration_seconds", "round_execution_time_v1"), ("started_at", "started_at_v1_52"), ("remaining_seconds", "round_time_remaining_v1_53")):
                if agent.get(field) is not None and (field != "started_at" or agent[field]):
                    text += copy('presentation.status.' + key, v1=local_timestamp(agent[field]) if field == "started_at" else status._duration(agent[field]))
            if isinstance(agent.get("recovery_details"), list):
                text += "\n" + "\n".join(str(item) for item in agent["recovery_details"])
        else:
            text = copy('presentation.status.no_confirmed_agent_work_record')
        yield Label(copy('presentation.status.current_work'), automatic or text)
        yield None
        yield from _status_actions(document, waiting, automatic)
    else:
        yield from _plain_work(document, waiting, automatic)


def _plain_work(document: QueryDocument, waiting: Any, automatic: str | None) -> Iterator[Node]:
    state, audit, view = document.state, document.audit, document.progress
    yield Line(copy('presentation.status.ncurrent_work'))
    if waiting is not None:
        yield Line(f"  {terminal_safe(waiting.work)}")
        yield Line(f"  {terminal_safe(waiting.activity)}")
        for label, text in waiting.details:
            yield Line(f"  {terminal_safe(label)}：{terminal_safe(text)}")
        yield from _cleanup(audit.get("delivery_cleanup"), False)
        yield from _scope_change(audit.get("scope_change"), False)
        yield Line(copy('presentation.status.nnext_step'))
        yield Line(f"  {terminal_safe(waiting.guidance)}")
        actions = operator_action_items(audit)
        if actions:
            for action in actions:
                yield from _action_lines(state, action)
        elif waiting.show_command:
            yield Line(f"  {terminal_safe(view['next_action'])}")
    else:
        control = audit.get("executor_control")
        if isinstance(control, dict) and control.get("activity") == "unknown" and state.get("status") != "completed":
            yield Line(copy('presentation.status.agent_activity_unknown_execution_status_cannot_be_confirmed'))
        activity = view["execution_activity"]
        if activity == "interrupted":
            yield Line(copy('presentation.status.execution_interrupted_waiting_to_resume_24'))
        if automatic:
            yield Line(f"  {automatic}")
        agent = view["current_agent"]
        if state.get("status") == "completed":
            agent = None
        if agent is None:
            yield Line(copy('presentation.status.no_agent_is_currently_running'))
        else:
            label = copy('presentation.status.current_agent' if agent['is_active'] else 'presentation.status.latest_agent')
            yield Line(f"  {label}: {terminal_safe(human_agent_role(agent['role']))} · {terminal_safe(human_delivery_object(agent['object']))}")
            yield Line(copy('presentation.status.model_v1', v1=terminal_safe(agent['model'])))
            yield Line(copy('presentation.status.reasoning_effort_v1', v1=terminal_safe(agent['reasoning_effort'])))
            if agent.get("started_at"):
                yield Line(copy('presentation.status.started_at_v1', v1=local_timestamp(agent['started_at'])))
            if agent["duration_seconds"] is not None:
                yield Line(f"  {copy('presentation.status.round_execution_time'):<20}{status._duration(agent['duration_seconds'])}")
            if agent["remaining_seconds"] is not None:
                yield Line(copy('presentation.status.round_time_remaining_v1', v1=status._duration(agent['remaining_seconds'])))
            for detail in agent["recovery_details"]:
                yield Line(f"  {terminal_safe(detail)}")
        if activity in _PAUSED and not operator_action_items(audit):
            yield Line(copy('presentation.status.nnext_step'))
            yield Line(f"  {terminal_safe(status.execution_guidance(state, activity))}")
        elif not isinstance(audit.get("operator_action"), dict):
            yield from _cleanup(audit.get("delivery_cleanup"), False)
            yield from _scope_change(audit.get("scope_change"), False)
            yield Line(copy('presentation.status.nnext_step'))
            cleanup = audit.get("delivery_cleanup")
            if isinstance(cleanup, dict) and cleanup.get("status") == "cleanup_pending":
                yield Line(copy('presentation.status.inspect_cleanup_diagnostics_in_json_prepare_the_managed_checkout_'))
                yield Line(copy('presentation.status.recovery_command_v1', v1=terminal_safe(human_next_action(view['next_action'], run_id=state.get('run_id')))))
            elif activity != "running" and not automatic:
                yield Line(copy('presentation.status.next_step_v1', v1=terminal_safe(human_next_action(view['next_action'], run_id=state.get('run_id')))))
            invocation = audit.get("agent_invocation")
            yield Line("  " + (automatic or status._operator_instruction(state, invocation if isinstance(invocation, dict) else None)))
        else:
            yield from _scope_change(audit.get("scope_change"), False)
            yield Line("")
            for action in operator_action_items(audit):
                yield from _action_lines(state, action)
    yield from _findings(view["findings"], False)


def _status_actions(document: QueryDocument, waiting: Any, automatic: str | None) -> Iterator[Node]:
    state, audit, view = document.state, document.audit, document.progress
    action, activity = audit.get("operator_action"), view.get("execution_activity")
    control = audit.get("executor_control")
    unknown_wait = (waiting is not None and isinstance(control, dict)
                    and control.get("activity") == "unknown" and not isinstance(state.get("supervision_wait"), dict))
    diagnostic = activity == "unknown" or unknown_wait
    running = (automatic is not None or waiting is None and activity == "running") and not isinstance(action, dict)
    command = (status_diagnostic_command(state) if diagnostic else view.get("next_action")
               or (action.get("next_action") if isinstance(action, dict) else None))
    # This panel must be derived before its outside command is printed, just as
    # the previous Rich path buffered all action lines before that dispatch.
    lines: list[Node] = []
    if waiting is not None and not isinstance(action, dict):
        lines.append(Line(terminal_safe(unknown_execution_guidance(state, include_command=False) if unknown_wait else waiting.guidance)))
    elif activity in _PAUSED and not operator_action_items(audit):
        lines.append(Line(terminal_safe(status.execution_guidance(state, str(activity), include_resume_command=False))))
    elif isinstance(action, dict):
        for item in operator_action_items(audit):
            lines.append(Line(terminal_safe(copy('presentation.status.object_v1', v1=human_delivery_object(item.get('object'))))))
            lines.append(Label(copy('presentation.status.type'), human_action_type(item.get('type'))))
            for reason in item.get("reasons", []):
                lines.append(Label(copy('presentation.status.reason'), reason if item.get('type') == 'Human Blocker' else human_pause_reason(reason)))
            lines.append(Label(copy('presentation.status.preserved_work'), human_preserved_results(item.get('preserved'))))
            lines.append(Label(copy('presentation.status.blocked_phase'), human_status_term(item.get('phase')) or item.get('phase') or copy('presentation.status.unknown')))
            if isinstance(item.get("trigger_invocation"), dict):
                lines.append(Label(copy('presentation.status.agent_that_triggered_the_blocker'), human_trigger_invocation(item['trigger_invocation'])))
            lines.append(Line(copy('presentation.status.the_entire_run_is_paused_other_tickets_will_not_continue') if item.get('scope') == 'global' else copy('presentation.status.local_operator_gate')))
            if item.get("type") == "Review Budget Checkpoint":
                lines.append(Line(copy('presentation.status.resuming_grants_the_configured_development_and_review_allowance_t')))
            lines.append(Label(copy('presentation.status.command'), item.get('next_action')))
    else:
        lines.append(Line(terminal_safe(automatic or status._operator_instruction(state, audit.get('agent_invocation')))))
    if waiting is not None:
        lines.extend(Label(label, text) for label, text in waiting.details)
    lines.extend(_cleanup(audit.get("delivery_cleanup"), True))
    lines.extend(_scope_change(audit.get("scope_change"), True))
    if not running and command and command != "无" and (diagnostic or waiting is None or waiting.show_command or isinstance(action, dict)):
        yield Line(f"{terminal_safe(copy('presentation.status.command'))}：{terminal_safe(command)}", outside=True)
    yield Panel(copy('presentation.status.next_step'), "yellow" if action is not None else "cyan", "group", "bold yellow")
    yield from lines
    yield None
    yield from _findings(view.get("findings"), True)


def _history(document: QueryDocument, rich: bool, details: bool) -> Iterator[Node]:
    state, audit, progress = document.state, document.audit, document.progress
    timezone, timezone_label = history._local_timezone()
    # Rich used to derive records before its first panel; plain derives them
    # after the identity prefix. Preserve that failure/output distinction.
    records = history.history_records(state, audit, progress["events"]) if rich else None
    parent = state.get("parent")
    parent = parent if isinstance(parent, dict) else {}
    if rich:
        border = {"interrupted": "yellow", "unknown": "yellow", "capacity_wait": "yellow", "recovery_wait": "red"}.get(str(progress.get("execution_activity")), "cyan")
        yield Panel(copy('presentation.history.agent_run_work_timeline'), border, "history")
        yield Label(copy('presentation.history.task'), f"{state.get('repository') or 'unknown'} · #{parent.get('number', '?')} {parent.get('title') or copy('presentation.history.untitled_requirement')}")
        yield Label(copy('presentation.history.timezone_19'), timezone_label)
    else:
        yield Line(copy('presentation.history.repository', v1=terminal_safe(state.get('repository') or copy('presentation.history.unknown'))))
        yield Line(copy('presentation.history.requirement', v1=terminal_safe(parent.get('number', '?')), v3=terminal_safe(parent.get('title') or copy('presentation.history.untitled_requirement'))))
        yield Line(copy('presentation.history.timezone', v1=timezone_label))
    if notice := scheduler_notice(progress.get("scheduler")):
        yield Line(notice)
    elapsed = progress["summary"]["elapsed_seconds"]
    if elapsed is not None:
        yield (Label(copy('presentation.history.elapsed_time'), history._duration(elapsed)) if rich
               else Line(copy('presentation.history.elapsed', v1=history._duration(elapsed))))
    activity = progress["execution_activity"]
    if rich:
        if activity in _PAUSED:
            yield Label(copy('presentation.history.activity'), history._execution_activity_text(str(activity)))
        yield Line("")
        yield Line(copy('presentation.history.work_timeline_21'), "bold")
    else:
        control = audit.get("executor_control")
        if isinstance(control, dict) and control.get("activity") == "unknown":
            yield Line(copy('presentation.history.agent_activity_unknown_execution_status_cannot_be_confirmed'))
        if activity == "interrupted":
            yield Line(copy('presentation.history.execution_interrupted_waiting_to_resume'))
        records = history.history_records(state, audit, progress["events"])
        yield Line(copy('presentation.history.work_timeline'))
    if records:
        for record in records:
            for line in history._record_lines(record, timezone=timezone, details=details):
                yield Line(line)
            if rich:
                yield Line("")
    else:
        yield Line(copy('presentation.history.no_confirmed_agent_work_records_yet'))
    for failure in history._approval_failures(audit):
        yield Line(f"  {terminal_safe(approval_failure_text(failure))}")
    cleanup = history._cleanup_history_lines(state, details=details)
    if cleanup:
        if rich:
            yield Line("")
        yield Line(copy('presentation.history.delivery_cleanup_22' if rich else 'presentation.history.delivery_cleanup'), "bold" if rich else "")
        for line in cleanup:
            yield Line(line)
    if rich:
        for action in operator_action_items(audit):
            yield Line("")
            yield Line(copy('presentation.history.action_required'), "bold yellow")
            yield Line(copy('presentation.history.type', v1=terminal_safe(human_action_type(action.get('type')))))
            yield Line(copy('presentation.status.object_v1', v1=human_delivery_object(action.get('object'))))
            yield Line(copy('presentation.history.command', v1=action.get('next_action')))
            reasons = action.get("reasons")
            if isinstance(reasons, list):
                for reason in reasons:
                    value = terminal_safe(reason if action.get("type") == "Human Blocker" else human_pause_reason(reason))
                    yield Line(copy('presentation.history.reason', v1=value if details else history._truncate_history_detail(value)))
            if action.get("preserved") is not None:
                yield Line(copy('presentation.history.preserved_results', v1=terminal_safe(human_preserved_results(action['preserved']))))
            if action.get("phase") is not None:
                yield Line(copy('presentation.history.phase', v1=terminal_safe(human_status_term(action['phase']))))
            yield Line(copy('presentation.history.the_entire_task_is_paused_other_subtasks_will_not_continue')
                       if action.get("scope") == "global" else copy('presentation.status.local_operator_gate'))
        yield Line("")
        yield Line(copy('presentation.history.summary_29'), "bold")
        yield Line(copy('presentation.history.agent_rounds', v1=history._round_summary_text(progress['summary']['rounds'])))
    else:
        yield Line(copy('presentation.history.summary'))
        rounds = progress["summary"]["rounds"]
        if rounds:
            for key, count in rounds.items():
                yield Line(copy('presentation.history.rounds', v1=f'{history._round_label(key):<22}', v2=count))
        else:
            yield Line(copy('presentation.history.no_agent_rounds_yet'))
        if elapsed is not None:
            yield Line(f"  {copy('presentation.history.elapsed_time'):<22}{history._duration(elapsed)}")
    waiting = history._current_wait_lines(state, audit)
    if waiting:
        if rich:
            yield Line("")
            for line in waiting:
                yield Line(line)
        else:
            yield Line("\n" + "\n".join(waiting))
    elif activity in _PAUSED and (rich or not operator_action_items(audit)):
        guidance = status.execution_guidance(state, str(activity))
        if rich:
            yield Line("")
            yield Label(copy('presentation.history.next_step_31'), guidance)
        else:
            yield Line(copy('presentation.history.next_step', v1=guidance))
    elif not rich and (actions := operator_action_items(audit)):
        for action in actions:
            yield Line("")
            bounded = dict(action)
            if isinstance(action.get("reasons"), list):
                bounded["reasons"] = [history._truncate_history_detail(str(reason)) for reason in action["reasons"]]
            yield from _action_lines(state, bounded)
    else:
        if rich:
            yield Line("")
            yield Label(copy('presentation.history.next_step_31'), human_next_action(audit.get('next_action'), run_id=state.get('run_id')))
            yield Line(history._operator_instruction(state, None))
        else:
            yield Line(copy('presentation.history.next_step_17', v1=human_next_action(audit.get('next_action'), run_id=state.get('run_id')), v3=history._operator_instruction(state, None)))
    if rich:
        yield None
