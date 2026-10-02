"""Read-only Ticket effort aggregates over the same facts as History."""
from __future__ import annotations

from typing import Any

from agent_run.delivery_facts import role_family
from agent_run.execution_timing import aggregate_invocation_timing, invocation_links_complete


def ticket_effort(
    state: dict[str, Any], records: list[dict[str, Any]], work_subject: str,
) -> dict[str, dict[str, Any]]:
    """Keep proven rounds and configuration participation when timing is partial.

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
    unclassified = any(
        not role_family(str((invocation.get("semantic_attempt") or {}).get("role")
                            or invocation.get("role") or invocation.get("invocation_role")))
        for invocation in raw
    )
    for role in ("development", "review"):
        selected = [record for record in subject_records if role_family(str(record.get("role"))) == role]
        known = [invocation for record in selected for invocation in record.get("invocations", [])]
        missing = [invocation for invocation in raw if invocation not in known
                   and role_family(str((invocation.get("semantic_attempt") or {}).get("role")
                                       or invocation.get("role") or invocation.get("invocation_role"))) == role]
        if not selected and not missing:
            continue
        unique = {record["attempt_id"]: record for record in selected if record.get("attempt_id")}
        associations_complete = not unclassified and not missing and len(unique) == len(selected)
        complete = (associations_complete and all(record.get("invocations") for record in selected)
                    and invocation_links_complete(state, selected))
        facts: dict[str, Any] = {"shared_rounds": False}
        if unique:
            facts.update(rounds=len(unique), rounds_complete=associations_complete)
        result[role] = facts
        # Unassociated invocations still prove time/configuration, but cannot
        # prove a full role total or be assigned an invented semantic round.
        invocations = [(record.get("attempt_id"), invocation) for record in selected
                       for invocation in record.get("invocations", [])]
        invocations.extend((None, invocation) for invocation in missing)
        facts.update({key: value for key, value in aggregate_invocation_timing(
            [invocation for _, invocation in invocations], complete=complete,
        ).items() if value is not None})
        configs: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        participation: dict[str, set[tuple[str | None, str | None]]] = {}
        bindings_complete = True
        for identity, invocation in invocations:
            model = invocation.get("model")
            effort = invocation.get("reasoning_effort")
            model = model if isinstance(model, str) and model else None
            effort = effort if isinstance(effort, str) and effort else None
            if model is None or effort is None:
                bindings_complete = False
            if model is None and effort is None:
                continue
            key = (model, effort)
            group = configs.setdefault(key, {"ids": set(), "invocations": []})
            if identity:
                group["ids"].add(identity)
                participation.setdefault(identity, set()).add(key)
            group["invocations"].append(invocation)
        facts["shared_rounds"] = any(len(keys) > 1 for keys in participation.values())
        configurations: list[dict[str, Any]] = []
        for (model, effort), group in configs.items():
            configuration: dict[str, Any] = {"model": model, "reasoning_effort": effort}
            if group["ids"]:
                configuration.update(rounds=len(group["ids"]),
                                     rounds_complete=associations_complete and bindings_complete)
            configuration.update({key: value for key, value in aggregate_invocation_timing(
                group["invocations"], complete=complete and bindings_complete,
            ).items() if value is not None})
            configurations.append(configuration)
        if configurations:
            facts["configurations"] = configurations
            facts["configurations_complete"] = complete and bindings_complete
    return result
