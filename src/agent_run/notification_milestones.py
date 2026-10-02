"""Project delivery milestones from durable execution and acceptance facts."""
from __future__ import annotations

from agent_run.notification_event_facts import event_copy, event_envelope, event_subject

from typing import Any

from agent_run.acceptance_facts import current_artifact
from agent_run.delivery_facts import role_family
from agent_run.execution_timing import invocation_started
from agent_run.run_currentness import ticket_completion_records


def milestones(state: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:

    result: list[dict[str, Any]] = []
    parent_only = state.get("delivery_type") == "parent_only"
    started = [record for record in records if record.get("started_at") and not record.get("event_record")
               and any(invocation_started(invocation) for invocation in record.get("invocations", []))]
    subjects: set[str] = set()
    for record in started:
        subject = str(record.get("work_subject") or "")
        eligible = subject == f"parent-only:{state.get('run_id')}" if parent_only else subject.startswith("ticket:")
        if not eligible or role_family(str(record.get("role"))) != "development" or subject in subjects:
            continue
        subjects.add(subject)
        facts = event_subject(state, subject)
        kind = "parent_started" if parent_only else "ticket_started"
        first = next(invocation for invocation in record["invocations"] if invocation_started(invocation))
        result.append(event_envelope(state, kind, subject,
                            event_copy(state, kind, title=facts.get("task_title") or subject),
                            attempt_id=record.get("attempt_id"), live=record.get("activity") == "running",
                            started_at=first["started_at"], **facts))
    if not parent_only:
        reviews = [record for record in started if record.get("work_subject") == f"run-acceptance:{state.get('run_id')}"
                   and role_family(str(record.get("role"))) == "review"]
        run = state.get("run_acceptance") or {}
        if reviews or run.get("acceptance_generation"):
            first = reviews[0] if reviews else {}
            result.append(event_envelope(state, "acceptance_started", state.get("run_id"), event_copy(state, "acceptance_started"),
                                attempt_id=first.get("attempt_id"), live=first.get("activity") == "running",
                                started_at=first.get("started_at")))
        if run.get("phase") == "repairing" or run.get("repair_job") or run.get("completed_repair_jobs"):
            active = state.get("active_agent_invocation") or {}
            repairing = (active.get("work_subject") == f"run-repair:{state.get('run_id')}"
                         and active.get("role") == "development" and active.get("status") == "running"
                         and invocation_started(active))
            result.append(event_envelope(state, "acceptance_repair", state.get("run_id"),
                                event_copy(state, "acceptance_repair_active" if repairing else "acceptance_repair_planned"),
                                "orange", live=repairing))
    owner = state.get("parent_job" if parent_only else "run_acceptance") or {}
    # A repair candidate's verdict is not the formal Run conclusion. Promotion
    # installs the formal record and removes the repair job atomically.
    artifact = current_artifact(owner)
    checks = (artifact or {}).get("checks") or {}
    record = owner.get("acceptance_record") or {}
    parent = state.get("parent") or {}
    if not record:
        return result
    if parent_only:
        current = (record.get("effective_revision") == owner.get("effective_revision") == parent.get("revision")
                   and record.get("reviewed_base_sha") == owner.get("base_sha"))
        phases = {"accepted", "publishing", "creating_pr", "waiting_checks", "waiting_merge", "ready_for_approval", "merging", "merged", "completed"}
    else:
        current = (record.get("parent_revision") == parent.get("revision")
                   and record.get("ticket_graph_revision") == (state.get("ticket_graph") or {}).get("revision")
                   and record.get("ticket_completion_records", []) == ticket_completion_records(state))
        phases = {"accepted"}
    formal = bool(record) and current and owner.get("phase") in phases
    if formal and not owner.get("repair_job") and set(checks) == {"e2e", "standards", "spec"} and all(
        isinstance(check, dict) and check.get("status") == "pass" and check.get("findings") == []
        for check in checks.values()
    ):
        record = owner["acceptance_record"]
        identity = [owner.get("generation", owner.get("acceptance_generation")),
                    record.get("reviewed_candidate_sha", record.get("reviewed_head_sha")),
                    record.get("reviewed_default_base_sha", record.get("reviewed_base_sha")),
                    record.get("expected_merge_tree"), record.get("effective_revision", record.get("parent_revision")),
                    record.get("ticket_graph_revision")]
        result.append(event_envelope(state, "acceptance_passed", identity,
                            event_copy(state, "parent_acceptance_passed" if parent_only else "acceptance_passed"),
                            "green", summary=event_copy(state, "acceptance_passed_summary")))
    return result
