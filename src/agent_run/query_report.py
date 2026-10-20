"""One read-only execution report for query and notification consumers.

Records have already been selected and read. This module has no reader, writer,
authority or transport; grouping never replaces an Invocation's bound Attempt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_run.delivery_facts import role_family
from agent_run.execution_timing import (
    _time, aggregate_invocation_timing, invocation_links_complete,
    record_execution_timing,
)


def _unassociated(
    raw: list[dict[str, Any]], known: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    def identity(value: dict[str, Any]) -> tuple[str, str] | None:
        subject, started = value.get("work_subject"), value.get("started_at")
        return (subject, started) if isinstance(subject, str) and isinstance(started, str) else None

    indexed: dict[tuple[str, str] | None, list[dict[str, Any]]] = {}
    for invocation in known:
        indexed.setdefault(identity(invocation), []).append(invocation)
    # Equal identity with a different body is still unassociated evidence.
    return [value for value in raw if value not in indexed.get(identity(value), [])]


def _role(invocation: dict[str, Any]) -> str | None:
    return role_family(str((invocation.get("semantic_attempt") or {}).get("role")
                           or invocation.get("role") or invocation.get("invocation_role")))


def _timing(values: list[dict[str, Any]], complete: bool) -> dict[str, Any]:
    return {key: value for key, value in aggregate_invocation_timing(
        values, complete=complete,
    ).items() if value is not None}


@dataclass(frozen=True)
class ExecutionReport:
    """Borrow one selected snapshot; callers must not mutate it while emitting.

    The report preserves raw, unassociated Invocation facts independently of
    semantic rounds. A subject report shares this scope rather than selecting
    a current owner or reading additional history.
    """

    state: dict[str, Any]
    records: list[dict[str, Any]]
    _subject: str | None = None

    def subject(self, work_subject: str) -> ExecutionReport:
        return ExecutionReport(self.state, self.records, work_subject)

    def _scope(self, raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rounds = [record for record in self.records if not record.get("event_record")]
        if self._subject is not None:
            rounds = [record for record in rounds if record.get("work_subject") == self._subject]
            raw = [value for value in raw if (value.get("work_subject")
                   or (value.get("semantic_attempt") or {}).get("work_subject")) == self._subject]
        return rounds, raw

    def totals(self, at: object = None) -> dict[str, Any]:
        end, start = _time(at), _time(self.state.get("created_at"))
        facts: dict[str, Any] = {"execution_timing_complete": False}
        if end and start and end >= start:
            facts["elapsed_seconds"] = int((end - start).total_seconds())
        rounds, raw = self._scope(self.state.get("agent_invocation_history") or [])
        known = [value for record in rounds for value in record.get("invocations", [])]
        missing = _unassociated(raw, known)
        included = [value for value in [*known, *missing]
                    if end is None or (ended := _time(value.get("ended_at"))) is None or ended <= end]
        recorded = aggregate_invocation_timing(included)["recorded_execution_seconds"]
        if recorded is not None:
            facts["recorded_execution_seconds"] = recorded
        complete = bool(rounds) and not missing and all(
            record_execution_timing(self.state, record)["execution_timing_complete"]
            for record in rounds
        )
        if complete and end and any(
            (ended := _time(value.get("ended_at"))) is not None and ended > end
            for value in known
        ):
            complete = False
        facts["execution_timing_complete"] = complete
        if complete:
            facts["total_seconds"] = recorded
        return facts

    def effort(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        rounds, raw = self._scope(self.state.get("agent_invocation_history", []))
        unclassified = any(not _role(value) for value in raw)
        for role in ("development", "review"):
            selected = [record for record in rounds if role_family(str(record.get("role"))) == role]
            known = [value for record in selected for value in record.get("invocations", [])]
            missing = [value for value in _unassociated(raw, known) if _role(value) == role]
            if not selected and not missing:
                continue
            unique = {record["attempt_id"] for record in selected if record.get("attempt_id")}
            associated = not unclassified and not missing and len(unique) == len(selected)
            complete = (associated and all(record.get("invocations") for record in selected)
                        and invocation_links_complete(self.state, selected))
            facts: dict[str, Any] = {"shared_rounds": False}
            if unique:
                facts.update(rounds=len(unique), rounds_complete=associated)
            result[role] = facts
            invocations = [(record.get("attempt_id"), value) for record in selected
                           for value in record.get("invocations", [])]
            invocations.extend((None, value) for value in missing)
            facts.update(_timing([value for _, value in invocations], complete))
            configurations: dict[tuple[str | None, str | None], dict[str, Any]] = {}
            participation: dict[str, set[tuple[str | None, str | None]]] = {}
            bindings_complete = True
            for identity, value in invocations:
                model, effort = value.get("model"), value.get("reasoning_effort")
                model = model if isinstance(model, str) and model else None
                effort = effort if isinstance(effort, str) and effort else None
                bindings_complete = bindings_complete and model is not None and effort is not None
                if model is None and effort is None:
                    continue
                key = (model, effort)
                group = configurations.setdefault(key, {"ids": set(), "invocations": []})
                if identity:
                    group["ids"].add(identity)
                    participation.setdefault(identity, set()).add(key)
                group["invocations"].append(value)
            facts["shared_rounds"] = any(len(keys) > 1 for keys in participation.values())
            values = []
            for (model, effort), group in configurations.items():
                configuration: dict[str, Any] = {"model": model, "reasoning_effort": effort}
                if group["ids"]:
                    configuration.update(rounds=len(group["ids"]),
                                         rounds_complete=associated and bindings_complete)
                configuration.update(_timing(group["invocations"], complete and bindings_complete))
                values.append(configuration)
            if values:
                facts.update(configurations=values, configurations_complete=complete and bindings_complete)
        return result
