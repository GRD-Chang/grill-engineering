"""One Invocation fact record, selected and archived only through identities."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.semantic_attempt import canonical_fingerprint, semantic_attempt_subjects
from agent_run.sqlite_encoding import copy_json_tree, encode_json
from agent_run.sqlite_mutations import _same
from agent_run.sqlite_work_evidence import get_evidence, put_evidence
from agent_run.sqlite_work_execution_attempts import load_version, save_version
from agent_run.sqlite_work_execution_details import (
    DETAIL_COLUMNS, restore_details, save_arrays, summary_values,
)
from agent_run.sqlite_work_execution_schema import (
    INVOCATION_EVIDENCE, INVOCATION_FIELDS, INVOCATION_SCALARS, presence,
    present_fields, restore_scalars, scalar_values,
)

_COLUMNS = (
    "presence", *INVOCATION_SCALARS, "attempt_id", "attempt_version_id",
    *(f"{field}_evidence_id" for field in INVOCATION_EVIDENCE),
    "raw_result_evidence_id", "resume_pending", "details_retained", *DETAIL_COLUMNS,
)
_ROOT_FIELDS = ("active_agent_invocation", "ticket_agent_invocations", "agent_invocation_history")
_FIXED_COLUMNS = (
    "generation", "role", "phase", "mode", "input_fingerprint", "requested_thread_id",
    "binding_id", "binding_role", "profile_role", "invocation_role", "model",
    "reasoning_effort", "profile_revision", "resume_id", "resume_sequence",
    "predecessor_invocation_started_at", "currentness_boundary_evidence_id",
    "attempt_id", "attempt_version_id", "deadline_seconds",
)


def identity(value: Mapping[str, Any]) -> tuple[str, str]:
    subject, started = value.get("work_subject"), value.get("started_at")
    if not isinstance(subject, str) or not subject or not isinstance(started, str) or not started:
        raise ValueError("Invocation requires its exact Work Subject and start identity")
    return subject, started


def _row(c: sqlite3.Connection, run_id: str, key: tuple[str, str]) -> dict[str, Any] | None:
    row = c.execute("SELECT " + ",".join(_COLUMNS) + " FROM work_invocations WHERE run_id=? "
                    "AND work_subject=? AND started_at=?", (run_id, *key)).fetchone()
    return None if row is None else dict(zip(_COLUMNS, row, strict=True))


def load_invocation(c: sqlite3.Connection, run_id: str, key: tuple[str, str]) -> dict[str, Any]:
    row = _row(c, run_id, key)
    if row is None:
        raise ValueError("Missing Agent Invocation")
    included = present_fields(row["presence"], INVOCATION_FIELDS)
    record: dict[str, Any] = {"work_subject": key[0], "started_at": key[1]}
    restore_scalars(record, INVOCATION_SCALARS, tuple(row[field] for field in INVOCATION_SCALARS), included)
    owner = "invocation:" + encode_json(key)
    for field in INVOCATION_EVIDENCE:
        reference = row[field + "_evidence_id"]
        if field in included:
            record[field] = None if reference is None else get_evidence(c, run_id, reference, owner_id=owner, kind=field)
        elif reference is not None:
            raise ValueError("Unexpected Invocation evidence")
    if "semantic_attempt" in included:
        record["semantic_attempt"] = (None if row["attempt_id"] is None else load_version(
            c, run_id, row["attempt_id"], row["attempt_version_id"],
        ))
    elif row["attempt_id"] is not None:
        raise ValueError("Unexpected Invocation Semantic Attempt")
    raw = row["raw_result_evidence_id"]
    if raw is not None:
        owned = c.execute("SELECT owner_id,kind FROM work_evidence WHERE run_id=? AND evidence_id=?", (run_id, raw)).fetchone()
        if row["attempt_id"] is None or owned != ("attempt:" + row["attempt_id"], "returned_result"):
            raise ValueError("Invocation raw result belongs to another Attempt")
    restore_details(c, (run_id, *key), row, record, included)
    return record


def _first_binding_attachment(
    c: sqlite3.Connection, run_id: str, key: tuple[str, str], old: Mapping[str, Any], record: Mapping[str, Any],
) -> bool:
    reported = record.get("reported_thread_id")
    current = record.get("thread_execution_binding")
    reference = old["thread_execution_binding_evidence_id"]
    if (old["reported_thread_id"] is not None or not isinstance(reported, str) or not reported
            or not isinstance(current, dict) or reference is None):
        return False
    previous = get_evidence(c, run_id, reference, owner_id="invocation:" + encode_json(key), kind="thread_execution_binding")
    return (isinstance(previous, dict) and "thread_id" in previous and previous["thread_id"] is None
            and previous.get("binding_id") == old["binding_id"]
            and current.get("binding_id") == record.get("binding_id")
            and current.get("thread_id") == reported
            and all(field in previous and field in current and _same(previous[field], current[field])
                    for field in ("role", "model", "reasoning_effort", "profile_revision")))


def _save_record(
    c: sqlite3.Connection, run_id: str, record: Mapping[str, Any],
    before: Mapping[str, Any] | None,
) -> None:
    key = identity(record)
    old = _row(c, run_id, key)
    row: dict[str, Any] = {"presence": presence(record, INVOCATION_FIELDS)}
    row.update(zip(INVOCATION_SCALARS, scalar_values(record, INVOCATION_SCALARS), strict=True))
    if record.get("status") not in {"running", "resuming", "completed", "failed"}:
        raise ValueError("Invalid Agent Invocation status")
    attempt = record.get("semantic_attempt")
    if attempt is not None and not isinstance(attempt, dict):
        raise ValueError("Invalid Invocation Semantic Attempt")
    if isinstance(attempt, dict):
        if old is not None and before is not None and _same(attempt, before.get("semantic_attempt")):
            row.update(attempt_id=old["attempt_id"], attempt_version_id=old["attempt_version_id"])
        else:
            row.update(attempt_id=attempt["attempt_id"], attempt_version_id=save_version(c, run_id, attempt))
        semantic_role = {"fresh_acceptance": "reviewer", "reviewer": "reviewer",
                         "final_publication": "publication", "publication": "publication"}.get(str(record.get("role")), "development")
        if (attempt.get("work_subject") != key[0] or attempt.get("generation") != record.get("generation")
                or attempt.get("role") != semantic_role):
            raise ValueError("Invocation Semantic Attempt identity does not match")
        boundary = record.get("currentness_boundary")
        if not isinstance(boundary, dict) or canonical_fingerprint(boundary) != attempt.get("currentness_boundary_fingerprint"):
            raise ValueError("Invocation Semantic Attempt currentness boundary does not match")
    else:
        row.update(attempt_id=None, attempt_version_id=None)
    for field in INVOCATION_EVIDENCE:
        value = record.get(field)
        if value is None:
            row[field + "_evidence_id"] = None
        elif old is not None and before is not None and _same(value, before.get(field)):
            row[field + "_evidence_id"] = old[field + "_evidence_id"]
        else:
            row[field + "_evidence_id"] = put_evidence(c, run_id, "invocation:" + encode_json(key), field, value)
    if old is not None:
        for column in _FIXED_COLUMNS:
            if old[column] is not None and row[column] != old[column]:
                # AgentProfileStore can merge a provisional allocation into
                # the same profile's existing Thread at the first handshake.
                if column == "binding_id" and _first_binding_attachment(c, run_id, key, old, record):
                    continue
                raise ValueError(f"Invocation immutable identity changed: {column}")
        if old["reported_thread_id"] is not None and row["reported_thread_id"] != old["reported_thread_id"]:
            raise ValueError("Invocation reported Thread identity changed")
        if old["status"] == "completed" and row["status"] != "completed":
            raise ValueError("Completed Invocation cannot be replaced by a late observation")
        if (old["status"] == "failed" and row["status"] in {"running", "resuming"}
                and not (row["status"] == "resuming" and row["recovery_waiting"] == 1)):
            raise ValueError("Failed Invocation cannot be replaced by a late observation")
    row["raw_result_evidence_id"] = old["raw_result_evidence_id"] if old is not None else None
    row["resume_pending"] = old["resume_pending"] if old is not None and row["status"] == "failed" else 0
    row["details_retained"] = int(bool(record.get("execution_segments") or record.get("recovery_wait_intervals")))
    row.update(summary_values(record))
    values = {"run_id": run_id, "work_subject": key[0], "started_at": key[1], **row}
    columns = ",".join(values)
    c.execute("INSERT INTO work_invocations(" + columns + ") VALUES(" + ",".join("?" for _ in values)
              + ") ON CONFLICT(run_id,work_subject,started_at) DO UPDATE SET "
              + ",".join(f"{column}=excluded.{column}" for column in row), tuple(values.values()))
    save_arrays(c, (run_id, *key), record, before)


def _collect(state: Mapping[str, Any]) -> tuple[dict[tuple[str, str], Mapping[str, Any]], list[Mapping[str, Any]]]:
    current: dict[tuple[str, str], Mapping[str, Any]] = {}
    active = state.get("active_agent_invocation")
    if active is not None:
        if not isinstance(active, dict):
            raise ValueError("Invalid active Agent Invocation")
        current[("active", "")] = active
    tickets = state.get("ticket_agent_invocations", {})
    if not isinstance(tickets, dict):
        raise ValueError("Ticket Invocations must be a mapping")
    for number, record in tickets.items():
        if not isinstance(number, str) or not isinstance(record, dict):
            raise ValueError("Invalid Ticket Agent Invocation")
        current[("ticket", number)] = record
    history = state.get("agent_invocation_history", [])
    if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
        raise ValueError("Agent Invocation history must be an array of records")
    keys = [identity(record) for record in history]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate Agent Invocation identity")
    return current, history


def _resuming(record: Mapping[str, Any], failed: bool) -> Mapping[str, Any]:
    if failed and record.get("status") == "resuming" and record.get("recovery_waiting") is not True:
        return {**record, "status": "failed"}
    return record


def _canonical_records(
    c: sqlite3.Connection, run_id: str, current: Mapping[tuple[str, str], Mapping[str, Any]],
    history: list[Mapping[str, Any]], previous_current: Mapping[tuple[str, str], Mapping[str, Any]],
    previous_history: list[Mapping[str, Any]],
) -> dict[tuple[str, str], tuple[Mapping[str, Any], Mapping[str, Any] | None]]:
    groups: dict[tuple[str, str], list[tuple[Mapping[str, Any], Mapping[str, Any] | None]]] = {}
    old_history = {identity(record): record for record in previous_history}
    for slot, record in current.items():
        previous = previous_current.get(slot)
        groups.setdefault(identity(record), []).append((record, previous if previous is not None and identity(previous) == identity(record) else None))
    for record in history:
        groups.setdefault(identity(record), []).append((record, old_history.get(identity(record))))
    result = {}
    for key, appearances in groups.items():
        stored = _row(c, run_id, key)
        failed = (stored is not None and stored["status"] == "failed"
                  or any(value.get("status") == "failed" for value, _ in appearances))
        normalized = [(_resuming(value, failed), _resuming(before, failed) if before is not None else None)
                      for value, before in appearances]
        changed = [(value, before) for value, before in normalized if not _same(value, before)]
        choices = changed or normalized
        selected, before = choices[0]
        if any(not _same(value, selected) for value, _ in choices[1:]):
            raise ValueError("Conflicting facts for one Agent Invocation identity")
        result[key] = selected, before
    return result


def _update_current(
    c: sqlite3.Connection, run_id: str, current: Mapping[tuple[str, str], Mapping[str, Any]],
    previous: Mapping[tuple[str, str], Mapping[str, Any]], canonical: Mapping[tuple[str, str], tuple[Mapping[str, Any], Any]],
) -> None:
    for kind, key in current.keys() | previous.keys():
        value, before = current.get((kind, key)), previous.get((kind, key))
        if _same(value, before):
            continue
        existing = c.execute("SELECT work_subject,started_at FROM work_invocation_current "
                             "WHERE run_id=? AND reference_kind=? AND reference_key=?", (run_id, kind, key)).fetchone()
        expected = identity(before) if before is not None else None
        incoming = identity(value) if value is not None else None
        if existing is not None and existing != expected and existing != incoming:
            raise ValueError("Current Invocation identity changed before save")
        if value is None:
            c.execute("DELETE FROM work_invocation_current WHERE run_id=? AND reference_kind=? AND reference_key=?",
                      (run_id, kind, key))
        else:
            c.execute("INSERT INTO work_invocation_current VALUES (?,?,?,?,?) ON CONFLICT(run_id,reference_kind,reference_key) "
                      "DO UPDATE SET work_subject=excluded.work_subject,started_at=excluded.started_at",
                      (run_id, *identity(value), kind, key))
    changed_keys = {identity(value) for slot in current.keys() | previous.keys()
                    for value in (current.get(slot), previous.get(slot)) if value is not None
                    and not _same(current.get(slot), previous.get(slot))}
    for invocation_key in changed_keys:
        pending = int(any(identity(value) == invocation_key and value.get("status") == "resuming"
                          and canonical[invocation_key][0].get("status") == "failed" for value in current.values()))
        c.execute("UPDATE work_invocations SET resume_pending=? WHERE run_id=? AND work_subject=? AND started_at=? "
                  "AND resume_pending!=?", (pending, run_id, *invocation_key, pending))


def bind_returns(c: sqlite3.Connection, run_id: str, state: Mapping[str, Any]) -> None:
    """Bind saved returns using current SQL identities, even for Job-only saves."""
    pending = {
        attempt["attempt_id"]: attempt for owner in semantic_attempt_subjects(dict(state))
        if isinstance((attempt := owner.get("pending_semantic_attempt")), dict) and "returned_result" in attempt
    }
    for attempt_id, attempt in pending.items():
        invocations = c.execute(
            "SELECT DISTINCT v.work_subject,v.started_at,v.raw_result_evidence_id "
            "FROM work_invocation_current r JOIN work_invocations v USING(run_id,work_subject,started_at) "
            "WHERE r.run_id=? AND v.attempt_id=?", (run_id, attempt_id),
        ).fetchall()
        if not invocations:
            continue
        result = attempt["returned_result"]
        row = c.execute(
            "SELECT v.returned_result_evidence_id FROM work_attempt_current p JOIN work_attempt_versions v "
            "USING(run_id,attempt_id,version_id) WHERE p.run_id=? AND p.attempt_id=?", (run_id, attempt_id),
        ).fetchone()
        if row is None or row[0] is None or not _same(get_evidence(c, run_id, row[0], owner_id="attempt:" + attempt_id, kind="returned_result"), result):
            raise ValueError("Invocation result does not match its saved Semantic Attempt")
        for subject, started, previous in invocations:
            if previous is not None and previous != row[0]:
                raise ValueError("Invocation already has a different saved result")
            if previous is None:
                c.execute("UPDATE work_invocations SET raw_result_evidence_id=? WHERE run_id=? AND work_subject=? AND started_at=?",
                          (row[0], run_id, subject, started))


def save_invocations(c: sqlite3.Connection, run_id: str, state: Mapping[str, Any], baseline: Mapping[str, Any]) -> None:
    current, history = _collect(state)
    previous_current, previous_history = _collect(baseline)
    canonical = _canonical_records(c, run_id, current, history, previous_current, previous_history)
    for key, (record, before) in canonical.items():
        if not _same(record, before) or _row(c, run_id, key) is None:
            _save_record(c, run_id, record, before)
    _update_current(c, run_id, current, previous_current, canonical)
    # A selected-object commit supplies a delta, not the complete audit list.
    # Retain every prior identity and its original global position; detail
    # compaction below releases only bounded execution detail, never a call.
    position = c.execute("SELECT COALESCE(MAX(position),-1)+1 FROM work_invocation_history WHERE run_id=?", (run_id,)).fetchone()[0]
    for record in history:
        key = identity(record)
        existing = c.execute("SELECT position FROM work_invocation_history WHERE run_id=? AND work_subject=? AND started_at=?", (run_id, *key)).fetchone()
        if existing is None:
            c.execute("INSERT INTO work_invocation_history VALUES (?,?,?,?)", (run_id, *key, position))
            position += 1
    roots = c.execute("SELECT active_present,tickets_present,history_present FROM work_invocation_roots WHERE run_id=?", (run_id,)).fetchone() or (0, 0, 0)
    flags = [int(field in state) if field in state or field in baseline else old for field, old in zip(_ROOT_FIELDS, roots, strict=True)]
    if any(field in state or field in baseline for field in _ROOT_FIELDS):
        c.execute("INSERT INTO work_invocation_roots VALUES (?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                  "active_present=excluded.active_present,tickets_present=excluded.tickets_present,history_present=excluded.history_present",
                  (run_id, *flags))
    bind_returns(c, run_id, state)
    from agent_run.sqlite_work_execution_retention import compact_details
    compact_details(c, run_id)


def load_invocations(
    c: sqlite3.Connection, run_id: str, *, ticket_numbers: set[int] | None = None,
    current_only: bool = True, resume_ids: set[str] | frozenset[str] = frozenset(), history: bool = False,
) -> tuple[dict[str, Any], dict[str, int]]:
    roots = c.execute("SELECT active_present,tickets_present,history_present FROM work_invocation_roots WHERE run_id=?", (run_id,)).fetchone()
    if roots is None:
        return {}, {}
    result: dict[str, Any] = {}
    if roots[0]:
        result["active_agent_invocation"] = None
    if roots[1]:
        result["ticket_agent_invocations"] = {}
    selected = set()
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    selected_tickets = None if ticket_numbers is None else {str(number) for number in ticket_numbers}
    for subject, started, kind, number, pending in c.execute(
        "SELECT r.work_subject,r.started_at,r.reference_kind,r.reference_key,v.resume_pending "
        "FROM work_invocation_current r LEFT JOIN work_invocations v USING(run_id,work_subject,started_at) WHERE r.run_id=?", (run_id,),
    ):
        if kind == "ticket" and selected_tickets is not None and subject.startswith("ticket:") and subject.removeprefix("ticket:") not in selected_tickets:
            continue
        key = subject, started
        if key not in cache:
            cache[key] = load_invocation(c, run_id, key)
        record = cache[key]
        current = copy_json_tree(record)
        if pending:
            current["status"] = "resuming"
        if kind == "active" and roots[0]:
            result["active_agent_invocation"] = current
        elif kind == "ticket" and roots[1]:
            result["ticket_agent_invocations"][number] = current
        selected.add(key)
    positions: dict[str, int] = {}
    if history and roots[2]:
        predicates, parameters = [], [run_id]
        if not current_only and ticket_numbers is None:
            predicates.append("1")
        if not current_only and ticket_numbers is not None:
            subjects = ["ticket:" + str(number) for number in sorted(ticket_numbers)]
            if subjects:
                predicates.append("h.work_subject IN (" + ",".join("?" for _ in subjects) + ")")
                parameters.extend(subjects)
            else:
                predicates.append("h.work_subject NOT LIKE 'ticket:%'")
        for subject, started in sorted(selected):
            predicates.append("(h.work_subject=? AND h.started_at=?)")
            parameters.extend((subject, started))
        if resume_ids:
            predicates.append("v.resume_id IN (" + ",".join("?" for _ in resume_ids) + ")")
            parameters.extend(sorted(resume_ids))
        result["agent_invocation_history"] = []
        if predicates:
            for subject, started, position in c.execute(
                "SELECT h.work_subject,h.started_at,h.position FROM work_invocation_history h "
                "LEFT JOIN work_invocations v USING(run_id,work_subject,started_at) WHERE h.run_id=? AND ("
                + " OR ".join(predicates) + ") ORDER BY h.position", parameters,
            ):
                key = subject, started
                loaded = cache.get(key)
                if loaded is None:
                    loaded = cache[key] = load_invocation(c, run_id, key)
                result["agent_invocation_history"].append(copy_json_tree(loaded))
                positions[encode_json(key)] = int(position)
    return result, positions
