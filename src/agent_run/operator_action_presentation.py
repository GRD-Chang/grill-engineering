from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from itertools import chain
from typing import Any

from agent_run.delivery_facts import current_delivery_identity, delivery_object_label
from agent_run.execution_timing import aggregate_invocation_timing
from agent_run.guidance_actions import next_action_fact, render_next_action
from agent_run.history_facts import history_records
from agent_run.messages import display_text, selected_language
from agent_run.operator_action_facts import operator_action_facts
from agent_run.presentation_helpers import (
    execution_duration,
    human_agent_role,
    human_delivery_object,
    human_next_action,
    human_pause_reason,
    human_status_term,
    terminal_safe,
)


def operator_action_views(
    state: dict[str, Any], *, language: str = "zh", human: bool = False,
) -> list[dict[str, Any]]:
    """Render every independent action from the shared read-only facts."""
    current_identity = current_delivery_identity(state)
    actions: list[dict[str, Any]] = []
    for fact in operator_action_facts(state):
        location, subject = fact["location"], fact["subject"]
        actions.append({
            **{key: value for key, value in fact.items() if key not in {"subject", "action"}},
            "action": asdict(fact["action"]),
            "type": _operator_action_label(fact["action_kind"]),
            "run_paused": fact["scope"] == "global",
            "object": delivery_object_label(
                state, location, ticket_number=subject.get("ticket_number"),
                include_parent_for_run=True,
            ),
            "trigger_invocation": (
                _triggering_invocation(state, location, generation=fact["generation"])
                if fact["action_kind"] == "human_blocker" else None
            ),
            "preserved": _preserved_results(subject, {} if location.startswith("ticket:") else current_identity),
            "next_action": render_next_action(
                fact["action"], state, language=language,
                human=human or (fact["action"].kind == "command" and fact["action_kind"] not in {
                    "abandonment_recovery", "requeue_required",
                }),
            ),
        })
    return actions


def operator_action_items(audit: dict[str, Any]) -> list[dict[str, Any]]:
    actions = audit.get("operator_actions")
    if isinstance(actions, list):
        return [action for action in actions if isinstance(action, dict)]
    action = audit.get("operator_action")
    return [action] if isinstance(action, dict) else []


def print_operator_action(
    action: dict[str, Any], *, run_id: object = None
) -> None:
    print(display_text('presentation.status.action_required'))
    print(display_text('presentation.status.type_v1', v1=terminal_safe(human_action_type(action['type']))))
    print(display_text('presentation.status.object_v1', v1=terminal_safe(human_delivery_object(action['object']))))
    print(display_text('presentation.status.phase_v1_205', v1=terminal_safe(human_status_term(action['phase']))))
    for reason in action["reasons"]:
        message = reason if action["type"] == "Human Blocker" else human_pause_reason(reason)
        print(display_text('presentation.status.reason_v1', v1=terminal_safe(message)))
    invocation = action.get("trigger_invocation")
    if isinstance(invocation, dict):
        print(
            f"{display_text('presentation.status.agent_that_triggered_the_blocker')}: "
            f"{human_trigger_invocation(invocation)}"
        )
    print(
        display_text('presentation.status.preserved_work_v1', v1=terminal_safe(human_preserved_results(action['preserved'])))
    )
    if action.get("run_paused", True):
        print(display_text('presentation.status.the_entire_run_is_paused_other_tickets_will_not_continue'))
    else:
        print(display_text('presentation.status.local_operator_gate'))
    if action["type"] == "Review Budget Checkpoint":
        print(display_text('presentation.status.resuming_grants_the_configured_development_and_review_allowance_t'))
    print(
        display_text('presentation.status.next_step_v1_36', v1=terminal_safe(human_next_action(action['next_action'], run_id=run_id)))
    )


def human_trigger_invocation(invocation: dict[str, Any]) -> str:
    duration = invocation.get("duration_seconds")
    if duration is not None and invocation.get("execution_timing_complete") is not False:
        timing = display_text(
            'presentation.status.blocker_round_duration', duration=execution_duration(duration)
        )
    else:
        timing = display_text('presentation.status.timing_incomplete')
        recorded = invocation.get("recorded_execution_seconds")
        if recorded is not None:
            timing += " · " + display_text(
                'presentation.status.recorded_execution_time', v1=execution_duration(recorded)
            )
    return display_text(
        'presentation.status.blocker_agent_summary',
        role=terminal_safe(_human_agent_role(invocation.get("role"))),
        model=terminal_safe(invocation.get("model")),
        effort=terminal_safe(invocation.get("reasoning_effort")),
        timing=timing,
    )


def human_action_type(value: object) -> str:
    raw = str(value)
    localized = {
        "Human Blocker": display_text('presentation.status.human_action_required'),
        "Review Budget Checkpoint": display_text('presentation.status.development_or_review_allowance_exhausted'),
        "Execution Failure": display_text('presentation.status.execution_failed'),
        "Deterministic Contradiction": display_text('presentation.status.delivery_records_conflict_with_actual_results'),
        "Supervision Timeout Pause": display_text('presentation.status.automatic_waiting_timed_out'),
        "Operator Stopped": display_text('presentation.status.stopped_by_operator'),
        "Requeue Required": display_text('presentation.status.restart_required_with_updated_requirements'),
        "Final Approval": display_text('presentation.status.awaiting_final_approval'),
        "Publication Retry Exhausted": display_text('presentation.status.publication_retries_exhausted'),
        "Abandonment Recovery": display_text('presentation.status.completing_abandonment'),
    }.get(raw)
    return localized or display_text('presentation.status.human_action_required')


def _human_agent_role(value: object) -> str:
    return human_agent_role(value)


def human_preserved_results(value: object) -> str:
    if not isinstance(value, str) or value == "当前状态与已有审计证据":
        return display_text('presentation.status.current_state_and_existing_audit_evidence')
    parts: list[str] = []
    for item in value.split("；"):
        if item.startswith("Candidate "):
            parts.append(display_text('presentation.status.current_candidate_saved'))
        elif item.startswith("Managed Checkout "):
            parts.append(display_text('presentation.status.development_workspace_preserved'))
        else:
            parts.append(item)
    return "；".join(parts)


def _operator_action_label(kind: str) -> str:
    return {
        "human_blocker": "Human Blocker",
        "review_budget_checkpoint": "Review Budget Checkpoint",
        "execution_failure": "Execution Failure",
        "deterministic_contradiction": "Deterministic Contradiction",
        "supervision_timeout": "Supervision Timeout Pause",
        "operator_stopped": "Operator Stopped",
        "requeue_required": "Requeue Required",
        "final_approval": "Final Approval",
        "publication_retry_exhausted": "Publication Retry Exhausted",
        "abandonment_recovery": "Abandonment Recovery",
    }.get(kind, "Operator Action")


def _triggering_invocation(
    state: dict[str, Any], location: str, *, generation: object = None
) -> dict[str, Any] | None:
    run_id = state.get("run_id")
    expected_subject = {
        "parent": f"parent-only:{run_id}",
        "run_acceptance": f"run-acceptance:{run_id}",
        "run_repair": f"run-repair:{run_id}",
        "run_publication": f"run-publication:{run_id}",
    }.get(location, location if location.startswith("ticket:") else None)
    active = state.get("active_agent_invocation")
    invocations = state.get("ticket_agent_invocations")
    if location.startswith("ticket:") and isinstance(invocations, dict):
        active = invocations.get(location.removeprefix("ticket:"), active)
    history = state.get("agent_invocation_history")
    history_candidates = (
        (item for item in reversed(history) if isinstance(item, dict))
        if isinstance(history, list)
        else iter(())
    )
    candidates = (
        chain((active,), history_candidates)
        if isinstance(active, dict)
        else history_candidates
    )
    for invocation in candidates:
        if (
            expected_subject is not None
            and invocation.get("work_subject") != expected_subject
        ):
            continue
        if generation is not None and invocation.get("generation") != generation:
            continue
        semantic_attempt = invocation.get("semantic_attempt")
        role = (
            semantic_attempt.get("role")
            if isinstance(semantic_attempt, dict)
            else invocation.get("role")
        )
        attempt_id = semantic_attempt.get("attempt_id") if isinstance(semantic_attempt, dict) else None
        records = history_records(state, {"agent_invocations": history})
        timing = next(
            (record for record in records if attempt_id and record.get("attempt_id") == attempt_id),
            aggregate_invocation_timing([invocation], complete=False),
        )
        return {
            "role": role or "unknown",
            "model": invocation.get("model") or display_text("presentation.status.unbound"),
            "reasoning_effort": invocation.get("reasoning_effort") or display_text("presentation.status.unbound"),
            "duration_seconds": timing.get("execution_seconds"),
            "execution_timing_complete": timing["execution_timing_complete"],
            "recorded_execution_seconds": timing.get("recorded_execution_seconds"),
        }
    return None


def _preserved_results(
    subject: dict[str, Any], current_identity: dict[str, object]
) -> str:
    candidate = subject.get("candidate_sha") or current_identity.get("candidate_sha")
    pr_number = subject.get("pr_number") or current_identity.get("pr_number")
    results: list[str] = []
    if isinstance(candidate, str):
        results.append(f"Candidate {candidate}")
    if isinstance(pr_number, int):
        results.append(f"PR #{pr_number}")
    for key in ("managed_checkout", "repair_checkout", "parent_checkout"):
        checkout = subject.get(key)
        if isinstance(checkout, str):
            results.append(f"Managed Checkout {checkout}")
            break
    return "；".join(results) if results else "当前状态与已有审计证据"


def human_next_action_for_state(state: Mapping[str, Any], *, language: str | None = None) -> object:
    """Render the shared recovery decision using the public Parent selector."""
    public_state = state if isinstance(state, dict) else dict(state)
    language = selected_language(public_state) if language is None else language
    fallback = render_next_action(
        next_action_fact(public_state), public_state, language=language, human=True,
    )
    actions = operator_action_views(
        public_state, language=language, human=True,
    )
    return "\n".join(dict.fromkeys(action["next_action"] for action in actions)) if actions else fallback
