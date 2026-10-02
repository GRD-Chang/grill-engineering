"""Present explicit Resume results without inventing progress from a request."""
from __future__ import annotations

from typing import Any

from agent_run.execution_timing import invocation_started, parse_optional_timestamp
from agent_run.guidance_actions import render_next_action
from agent_run.messages import selected_language, text
from agent_run.notification_presentation import next_action, pause_reason
from agent_run.notification_event_facts import event_envelope, event_subject
from agent_run.operator_action_facts import operator_action_facts


def ticket_resume_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    """An existing Executor observes admitted Tickets through durable work facts.

    A receipt alone is only queue admission. A subsequent actual Invocation,
    completed Ticket, or renewed operator gate proves the result. No polling or
    second business-state ledger is needed, and sibling work proves nothing.
    """
    results = []
    gates = operator_action_facts(state)
    invocations = [*state.get("agent_invocation_history", []),
                   *(state.get("ticket_agent_invocations") or {}).values()]
    for number, job in (state.get("ticket_jobs") or {}).items():
        receipt = job.get("last_ticket_resume")
        if not isinstance(receipt, dict) or receipt.get("job_generation") != job.get("ticket_branch_generation"):
            continue
        accepted = parse_optional_timestamp(receipt.get("accepted_at"))
        if accepted is None:
            continue
        subject = f"ticket:{number}"
        # A later ordinary Resume supersedes this running-Executor receipt.
        if any(resume.get("work_subject") == subject
               and (requested := parse_optional_timestamp(resume.get("requested_at"))) is not None
               and requested > accepted
               for resume in (state.get("resume_audit") or {}).get("history", [])):
            continue
        reason = ""
        target_gates = [gate for gate in gates if gate["work_subject"] == subject]
        gate = next((gate for gate in target_gates if gate["scope"] == "global"),
                    next(iter(target_gates), None))
        if gate is not None:
            outcome = "failed" if gate["action_kind"] in {
                "execution_failure", "supervision_timeout", "deterministic_contradiction",
            } else "human_action"
            evidence = "controller_boundary"
            reason = text("notification.event.separator", language=selected_language(state)).join(
                str(value) if gate["action_kind"] == "human_blocker"
                else pause_reason(value, selected_language(state)) for value in gate["reasons"]
            )
        elif job.get("phase") == "completed":
            outcome, evidence = "started", "controller_progress"
        elif (observed := parse_optional_timestamp(job.get("publication_observed_at"))) is not None and observed >= accepted:
            outcome, evidence = "started", "controller_check"
        elif any(isinstance(invocation, dict)
                 and invocation.get("work_subject") == subject
                 and invocation.get("generation") == receipt.get("job_generation")
                 and invocation_started(invocation)
                 and (started := parse_optional_timestamp(invocation.get("started_at"))) is not None
                 and started >= accepted for invocation in invocations):
            outcome, evidence = "started", "worker_started"
        else:
            continue
        results.append({"id": f"{receipt.get('executor_action_id')}:{number}:{receipt['accepted_at']}",
                        "outcome": outcome, "evidence": evidence, "reason": reason,
                        "ticket_number": int(number), "work_subject": subject,
                        **({key: gate[key] for key in ("scope", "action_kind", "generation")} if gate else {}),
                        "live": outcome == "started" and job.get("phase") != "completed"})
    return results


def apply_resume_result(
    state: dict[str, Any], projected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results = ticket_resume_results(state)
    result = state.get("_manual_resume_result")
    if isinstance(result, dict):
        results.append(result)
    for result in results:
        projected = _apply_result(state, projected, result)
    return projected


def _apply_result(state: dict[str, Any], projected: list[dict[str, Any]],
                  result: dict[str, Any]) -> list[dict[str, Any]]:
    if not result.get("id"):
        return projected
    outcome = result.get("outcome")
    if outcome not in {"started", "failed", "human_action"}:
        return projected
    if outcome == "started" and result.get("evidence") not in {
        "worker_started", "controller_progress", "controller_check",
    }:
        return projected

    language = selected_language(state)
    title = text("notification.event.resume_" + str(outcome), language=language)
    summary = text(
        "notification.event.resume_" + str(result.get("evidence")) + "_summary",
        language=language,
    ) if outcome == "started" else str(result.get("reason") or "")
    color = {"started": "blue", "failed": "red", "human_action": "yellow"}[str(outcome)]
    ticket_number = result.get("ticket_number")
    work_subject = result.get("work_subject") or (f"ticket:{ticket_number}" if ticket_number is not None else None)
    next_step = ""
    if outcome != "started":
        targets = [fact for fact in operator_action_facts(state)
                   if (work_subject is None or fact["work_subject"] == work_subject)
                   and all(result.get(key) is None or result[key] == fact[key]
                           for key in ("scope", "action_kind", "generation"))]
        if len(targets) == 1:
            next_step = render_next_action(targets[0]["action"], state, language=language, human=True)
        elif work_subject is None:
            next_step = next_action(state)
        # Only an unambiguous matching gate can own this Resume occurrence.
        # One subject can retain both a local pause and a global failure.
        boundaries = [event for event in projected
                      if event.get("current") and event.get("kind") == "boundary"
                      and event.get("status_code") not in {"completed", "abandoned", "operator_stopped"}
                      and (work_subject is None or event.get("work_subject") == work_subject)
                      and all(result.get(key) is None or result[key] == event.get(key)
                              for key in ("scope", "action_kind", "generation"))]
        boundary = boundaries[0] if len(boundaries) == 1 else None
        if boundary is not None:
            superseded = [event["id"] for event in projected if
                          event.get("kind") == "stage_end" and event.get("status_code") in {
                              "execution_failed", "review_blocked",
                          } and event.get("work_subject") == boundary.get("work_subject")]
            projected = [event for event in projected if event["id"] not in superseded]
            return [{**event,
                     "title": event["title"] if "approval_pending" in str(event.get("status_code")) else title,
                     "color": color,
                     "next_step": next_step or event.get("next_step", ""),
                     "summary": (title + text("notification.event.separator", language=language)
                                 + (summary or event.get("summary", ""))),
                     "resume_id": result["id"], "resume_outcome": outcome,
                     "supersedes": superseded}
                    if event is boundary else event for event in projected]
    return [*projected, event_envelope(
        state, "resume", [result["id"], outcome], title, color,
        summary=summary, next_step=next_step,
        resume_id=result["id"], resume_outcome=outcome,
        live=result.get("live", outcome == "started"),
        work_subject=work_subject, **event_subject(state, work_subject),
    )]
