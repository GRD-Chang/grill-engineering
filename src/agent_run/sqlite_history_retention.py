"""Compact precisely the Invocation details whose retention protection changed."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_collections import read_path
from agent_run.sqlite_encoding import encode_json, object_path
from agent_run.state_retention import MAX_DETAILED_INVOCATIONS, compact_invocation_detail
from agent_run.semantic_attempt import semantic_attempt_subjects

_HISTORY = "agent_invocation_history"


def retain_details(
    connection: sqlite3.Connection, run_id: str, state: Mapping[str, Any], fields: set[str],
    keyed_changes: Mapping[str, set[str]],
) -> tuple[Mapping[str, Any], set[str]]:
    from agent_run.sqlite_objects import invocation_identity, invocation_attempt_identity, _require_history_claim
    row = connection.execute("SELECT value_json FROM run_objects WHERE run_id=? AND object_path=?",
                             (run_id, encode_json((_HISTORY,)))).fetchone()
    if row is None:
        return state, fields
    old_length = int(row[0])
    incoming = list(state.get(_HISTORY, [])) if _HISTORY in fields else []
    by_identity = {invocation_identity(item): item for item in incoming}
    new_length = old_length
    for identity in by_identity:
        if connection.execute("SELECT 1 FROM run_object_index WHERE run_id=? AND invocation_id=?", (run_id, identity)).fetchone() is None:
            new_length += 1
    cutoff = max(new_length - MAX_DETAILED_INVOCATIONS, 0)
    if cutoff == 0:
        return state, fields
    candidates = {encode_json((_HISTORY, position)) for position in range(max(old_length - MAX_DETAILED_INVOCATIONS, 0), min(cutoff, old_length))}

    def old_value(path: tuple[str | int, ...]) -> Any:
        try:
            return read_path(connection, run_id, path)
        except KeyError:
            return None
    def proposed_value(path: tuple[str, ...]) -> Any:
        field = path[0]
        if field not in fields or len(path) > 1 and field in keyed_changes and path[1] not in keyed_changes[field]:
            return old_value(path)
        value: Any = state.get(field)
        for part in path[1:]:
            value = value.get(part) if isinstance(value, dict) else None
        return value
    def protect(item: dict[str, Any]) -> bool:
        identity = invocation_identity(item)
        active = proposed_value(("active_agent_invocation",))
        if isinstance(active, dict) and invocation_identity(active) == identity:
            return True
        subject = item.get("work_subject")
        owner_paths: list[tuple[str, ...]]
        if isinstance(subject, str) and subject.startswith("ticket:"):
            number = subject.split(":", 1)[1]
            canonical = proposed_value(("ticket_agent_invocations", number))
            if isinstance(canonical, dict) and invocation_identity(canonical) == identity:
                return True
            owner_paths = [("ticket_jobs", number), ("active_ticket_job",)]
        elif isinstance(subject, str) and subject.startswith("run-publication:"):
            owner_paths = [("run_publication",)]
        elif isinstance(subject, str) and subject.startswith(("run-acceptance:", "run-integration:", "run-repair:")):
            owner_paths = [("run_acceptance",)]
        else:
            owner_paths = [("parent_job",)]
        attempt_id = invocation_attempt_identity(item)
        if attempt_id is None:
            return False
        for path in owner_paths:
            owner = proposed_value(path)
            owners = [owner, owner.get("repair_job")] if path == ("run_acceptance",) and isinstance(owner, dict) else [owner]
            if any(isinstance(value, dict) and isinstance((pending := value.get("pending_semantic_attempt")), dict)
                   and pending.get("attempt_id") == attempt_id for value in owners):
                return True
        return False

    canonical_paths: list[tuple[str, ...]] = [("active_agent_invocation",)] if "active_agent_invocation" in fields else []
    if "ticket_agent_invocations" in fields:
        numbers = keyed_changes.get("ticket_agent_invocations")
        if numbers is None:
            # This explicit collection owner boundary already changes its
            # canonical objects; only its source keys are enumerated here.
            from agent_run.sqlite_objects import _rows, _decode
            old = _decode(connection, _rows(connection, run_id, encode_json(("ticket_agent_invocations",))), run_id=run_id)
            numbers = set(old.get("ticket_agent_invocations", {})) | set(state.get("ticket_agent_invocations", {}))
        canonical_paths.extend(("ticket_agent_invocations", number) for number in numbers)
    for path in canonical_paths:
        previous = old_value(path)
        current = proposed_value(path)
        if isinstance(previous, dict) and (not isinstance(current, dict) or invocation_identity(previous) != invocation_identity(current)):
            candidates.update(location for (location,) in connection.execute(
                "SELECT object_path FROM run_object_index WHERE run_id=? AND invocation_id=?", (run_id, invocation_identity(previous))))

    owner_paths: list[tuple[str, ...]] = [(field,) for field in ("parent_job", "run_acceptance", "run_publication", "active_ticket_job") if field in fields]
    owner_paths.extend(("ticket_jobs", number) for number in keyed_changes.get("ticket_jobs", set()))
    for path in owner_paths:
        previous, current = old_value(path), proposed_value(path)
        def pending(value: Any) -> set[str]:
            if not isinstance(value, dict):
                return set()
            mapped = {"run_acceptance" if path == ("run_acceptance",) else "parent_job": value}
            return {attempt["attempt_id"] for owner in semantic_attempt_subjects(mapped)
                    if isinstance((attempt := owner.get("pending_semantic_attempt")), dict) and isinstance(attempt.get("attempt_id"), str)}
        for identity in pending(previous) - pending(current):
            candidates.update(location for (location,) in connection.execute(
                "SELECT object_path FROM run_object_index WHERE run_id=? AND attempt_id=?", (run_id, identity)))
    changed = False
    for location in candidates:
        history_path = object_path(location)
        if len(history_path) != 2 or history_path[0] != _HISTORY or type(history_path[1]) is not int or not 0 <= history_path[1] < cutoff:
            continue
        item = old_value((_HISTORY, history_path[1]))
        if not isinstance(item, dict):
            raise ValueError("Missing indexed SQLite Invocation")
        _require_history_claim(connection, run_id, location, item)
        identity = invocation_identity(item)
        item = by_identity.get(identity, item)
        if not protect(item) and compact_invocation_detail(item):
            by_identity[identity] = item
            changed = True
    if not changed:
        return state, fields
    return {**state, _HISTORY: list(by_identity.values())}, fields | {_HISTORY}
