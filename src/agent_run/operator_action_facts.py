"""Read-only operator decisions shared by queries, guidance and notifications."""
from __future__ import annotations

from typing import Any

from agent_run.final_approval_operation import has_final_approval
from agent_run.guidance_actions import NextAction
from agent_run.operator_gate import operator_gate_evidence, operator_gate_sources


_REASON_KINDS = {
    "agent_requires_human": "human_blocker",
    "reviewer_requires_human": "human_blocker",
    "ticket_execution_failed": "execution_failure",
    "ticket_supervision_timeout": "supervision_timeout",
    "ticket_paused": "operator_stopped",
    "modification_budget_exhausted": "review_budget_checkpoint",
    "review_budget_exhausted": "review_budget_checkpoint",
    "ci_inner_budget_exhausted": "review_budget_checkpoint",
    "ci_outer_budget_exhausted": "review_budget_checkpoint",
    "integration_development_exhausted": "review_budget_checkpoint",
    "integration_resync_exhausted": "review_budget_checkpoint",
    "acceptance_record_mismatch": "deterministic_contradiction",
    "candidate_or_acceptance_inconsistent": "deterministic_contradiction",
    "merged_result_mismatch": "deterministic_contradiction",
    "published_head_mismatch": "deterministic_contradiction",
    "unexpected_external_merge": "deterministic_contradiction",
}
_STATUS_KINDS = {
    "execution_failed": "execution_failure",
    "deterministic_contradiction": "deterministic_contradiction",
    "unsupported_scope_change": "deterministic_contradiction",
    "supervision_timeout": "supervision_timeout",
    "operator_stopped": "operator_stopped",
    "requeue_required": "requeue_required",
    "run_approval_pending": "final_approval",
    "parent_approval_pending": "final_approval",
    "publication_pending": "publication_retry_exhausted",
    "abandonment_pending": "abandonment_recovery",
}


def operator_action_facts(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return independent current actions; no state writes or external reads."""
    result: list[dict[str, Any]] = []
    binding = operator_gate_evidence(state)
    sources = operator_gate_sources(state)
    for source in sources:
        location, subject, scope = source.location, source.subject, source.scope
        work_subject = _work_subject(state, location)
        bound_local = (
            binding is not None and binding["work_subject"] == work_subject
            and binding["reason"] == subject.get("blocked_reason")
        )
        top_level = source.top_level or bound_local
        if bound_local and state.get("status") in {"execution_failed", "deterministic_contradiction"}:
            scope = "global"
        evidence = binding if top_level else None
        reason = evidence["reason"] if evidence else (
            _diagnostic_code(state) if source.top_level and location != "run"
            else subject.get("blocked_reason")
        )
        if top_level and not reason:
            reason = _diagnostic_code(state)
        phase = subject.get("phase", state.get("status"))
        status_code = (
            state.get("status") if top_level else
            "publication_pending" if phase == "publication_pending" else
            "operator_stopped" if reason == "ticket_paused" else
            "supervision_timeout" if reason == "ticket_supervision_timeout" else
            "execution_failed" if reason == "ticket_execution_failed" else
            "ready_for_human"
        )
        if phase == "ready_for_approval" and not source.top_level:
            status_code = "parent_approval_pending" if location == "parent" else "run_approval_pending"
        if evidence:
            kind = evidence["action_kind"]
        elif source.top_level:
            kind = _STATUS_KINDS.get(str(status_code), _REASON_KINDS.get(str(reason), "operator_action"))
        else:
            kind = _REASON_KINDS.get(str(reason), _STATUS_KINDS.get(str(status_code), "operator_action"))
        if kind == "review_budget_checkpoint":
            status_code = "progress_exhausted"
        diagnostics = state.get("diagnostics") if top_level else subject.get("diagnostics")
        if not diagnostics and not top_level and scope == "global" and reason and sum(
            item.subject.get("blocked_reason") == reason for item in sources
        ) == 1:
            # Run integration/publication keep their raw pause evidence on the
            # Run. Attribute only an exact, uniquely owned cause; never donate
            # another Ticket's current diagnostic to every enumerated gate.
            diagnostics = [item for item in state.get("diagnostics", [])
                           if isinstance(item, dict) and item.get("code") == reason
                           and (not isinstance(item.get("operator_gate"), dict)
                                or item["operator_gate"].get("work_subject") == work_subject)]
        execution_failure = subject.get("execution_failure") if reason == "ticket_execution_failed" else None
        blockers = subject.get("human_blockers")
        if kind == "human_blocker" and isinstance(blockers, list):
            reasons = [str(item) for item in blockers]
            phase = subject.get("human_blocker_phase", phase)
        else:
            reasons = [str(item["message"]) for item in diagnostics
                       if isinstance(item, dict) and item.get("message")] if isinstance(diagnostics, list) else []
            if isinstance(execution_failure, str) and execution_failure:
                reasons = [execution_failure]
            reasons = reasons or (["Run abandonment recovery is incomplete."] if kind == "abandonment_recovery" and not reason else [str(reason or status_code)])
            if reason in {"ticket_execution_failed", "ticket_supervision_timeout"}:
                phase = subject.get("execution_resume_phase", phase)
            elif reason == "ticket_paused":
                phase = subject.get("pause_resume_phase", phase)
            elif evidence:
                phase = evidence["phase"]
        result.append({
            "location": location, "work_subject": work_subject,
            "generation": _generation(state, location, subject),
            "reason": reason or status_code, "reasons": reasons,
            "evidence": {
                "operator_gate": evidence,
                "human_blockers": blockers if kind == "human_blocker" else None,
                "diagnostics": diagnostics,
                "failure_evidence": subject.get("failure_evidence"),
                "execution_failure": execution_failure,
                "blocked_reason": subject.get("blocked_reason"),
            },
            "scope": scope, "action_kind": kind, "phase": phase,
            "status_code": status_code, "subject": subject,
            "action": _next_action(state, location, kind, reason, scope),
        })
    return result


def _work_subject(state: dict[str, Any], location: str) -> str:
    prefix = {"parent": "parent-only", "run_acceptance": "run-acceptance",
              "run_repair": "run-repair", "run_publication": "run-publication"}.get(location, location)
    return location if location.startswith("ticket:") else f"{prefix}:{state.get('run_id')}"


def _generation(state: dict[str, Any], location: str, subject: dict[str, Any]) -> object:
    key = {"parent": "parent_generation", "run_acceptance": "acceptance_generation",
           "run_repair": "repair_generation", "run_publication": "publication_generation"}.get(location, "ticket_branch_generation")
    value = subject.get(key, subject.get("generation"))
    if value is None and location in {"run_repair", "run_publication"}:
        acceptance = state.get("run_acceptance")
        if isinstance(acceptance, dict):
            value = acceptance.get("repair_generation" if location == "run_repair" else "acceptance_generation")
    if location == "run":
        stop = state.get("operator_stop")
        value = stop.get("executor_generation") if isinstance(stop, dict) else None
        value = value if value is not None else state.get("generation")
    return value


def _diagnostic_code(state: dict[str, Any]) -> str | None:
    diagnostics = state.get("diagnostics")
    if isinstance(diagnostics, list) and len(diagnostics) == 1:
        code = diagnostics[0].get("code") if isinstance(diagnostics[0], dict) else None
        return code if isinstance(code, str) else None
    return None


def _next_action(
    state: dict[str, Any], location: str, kind: str, reason: object, scope: str,
) -> NextAction:
    run_id = state.get("run_id")
    ticket_number = int(location.removeprefix("ticket:")) if scope == "local" else None
    if kind in {"human_blocker", "review_budget_checkpoint", "execution_failure",
                "supervision_timeout", "operator_stopped"}:
        return NextAction("command", "resume", run_id, ticket_number)
    if kind == "final_approval":
        return NextAction("command", "resume" if has_final_approval(state) else "approve", run_id)
    if kind == "requeue_required":
        return NextAction("command", "requeue", run_id)
    if kind == "abandonment_recovery":
        return NextAction("command", "abandon", run_id)
    if kind == "publication_retry_exhausted":
        return NextAction("command", "resume", run_id, ticket_number) if ticket_number else NextAction("publication_retry")
    if kind == "deterministic_contradiction":
        if state.get("status") == "unsupported_scope_change":
            return NextAction("scope_change")
        if state.get("status") == "deterministic_contradiction":
            return NextAction("contradiction")
        if reason in {"candidate_or_acceptance_inconsistent", "foreign_run_pr"}:
            return NextAction("command", "abandon", run_id)
        return NextAction("external_contradiction")
    return NextAction("human_items")
