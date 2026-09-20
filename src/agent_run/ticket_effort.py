"""Read-only Ticket effort aggregates over the same facts as History."""
from __future__ import annotations

from typing import Any

from agent_run.delivery_history import _role_family
from agent_run.delivery_status import invocation_execution_seconds
from agent_run.execution_timing import invocation_links_complete


def ticket_effort(
    state: dict[str, Any], records: list[dict[str, Any]], work_subject: str,
) -> dict[str, dict[str, Any]]:
    """Count semantic identities, assigning each Invocation to its real config.

    Unknown timing does not erase proven rounds or configuration participation.
    A configuration participating in a resumed round counts that round once;
    consequently its count must not be added to other configurations' counts.
    """
    result: dict[str, dict[str, Any]] = {}
    subject_records = [record for record in records
                       if not record.get("event_record")
                       and record.get("work_subject") == work_subject]
    raw = [invocation for invocation in state.get("agent_invocation_history", [])
           if (invocation.get("work_subject")
               or (invocation.get("semantic_attempt") or {}).get("work_subject")) == work_subject]
    # Unknown-role work cannot safely be assigned to either role, even if
    # History retains it as an unclassified record.
    unclassified = any(
        not ((invocation.get("semantic_attempt") or {}).get("role")
             or invocation.get("role") or invocation.get("invocation_role"))
        for invocation in raw
    )
    # Timeline truncation does not truncate either durable Attempt owners or
    # Invocation history. Their independently provable aggregates survive.
    for role in ("development", "review"):
        selected = [record for record in subject_records if _role_family(str(record.get("role"))) == role]
        if not selected:
            continue
        known = [invocation for record in selected for invocation in record.get("invocations", [])]
        # An unassociated invocation could be another round or configuration.
        # Publication uses a Development binding too, so classify by semantic
        # role rather than the thread's binding_role.
        missing = [invocation for invocation in raw if invocation not in known
                   and _role_family(str((invocation.get("semantic_attempt") or {}).get("role")
                                        or invocation.get("role") or invocation.get("invocation_role"))) == role]
        ids = [record.get("attempt_id") for record in selected]
        if unclassified or missing or any(not isinstance(identity, str) or not identity for identity in ids):
            continue
        unique = {record["attempt_id"]: record for record in selected}
        facts: dict[str, Any] = {"rounds": len(unique), "shared_rounds": False}
        result[role] = facts
        invocations = [(identity, invocation) for identity, record in unique.items()
                       for invocation in record.get("invocations", [])]
        complete = (all(record.get("invocations") for record in unique.values())
                    and invocation_links_complete(state, selected))
        durations = [invocation_execution_seconds(invocation) for _, invocation in invocations]
        if complete and all(value is not None for value in durations):
            facts["execution_seconds"] = sum(value for value in durations if value is not None)
        configs: dict[tuple[str, str], dict[str, Any]] = {}
        participation: dict[str, set[tuple[str, str]]] = {}
        for (identity, invocation), duration in zip(invocations, durations):
            model, effort = invocation.get("model"), invocation.get("reasoning_effort")
            if not isinstance(model, str) or not model or not isinstance(effort, str) or not effort:
                complete = False
                continue
            key = (model, effort)
            group = configs.setdefault(key, {"ids": set(), "durations": []})
            group["ids"].add(identity)
            group["durations"].append(duration)
            participation.setdefault(identity, set()).add(key)
        # Missing binding might belong to any group: do not present partial
        # groups as exhaustive. The role's independently proven time survives.
        if not complete:
            continue
        facts["shared_rounds"] = any(len(keys) > 1 for keys in participation.values())
        configurations: list[dict[str, Any]] = []
        for (model, effort), group in configs.items():
            configuration: dict[str, Any] = {
                "model": model, "reasoning_effort": effort, "rounds": len(group["ids"]),
            }
            if all(value is not None for value in group["durations"]):
                configuration["execution_seconds"] = sum(group["durations"])
            configurations.append(configuration)
        facts["configurations"] = configurations
    return result
