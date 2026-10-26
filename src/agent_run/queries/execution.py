"""Shared read-only interpretation of recorded Worker segments and Thread bindings."""
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import json
import math
from typing import Any

from agent_run.storage.sqlite import Transaction


def worker_activity_start(tx: Transaction, subject: str | None, *, position: int | None = None,
                          attempt: str | None = None, role: str | None = None,
                          after: int | None = None, action: str | None = None) -> dict[str, Any] | None:
    """Read the first provable backend activity, not the bootstrap's lifetime.

    Earlier v21 writers have only the binding history anchor. Its actual
    activity must be within that original control interval; ambiguous old
    facts remain unknown. The optional observer trigger is never authority.
    """
    sql = (
        "SELECT a.*,i.id AS invocation_id,i.action_id,o.ordinal AS output_ordinal,o.dispatch_state,o.exit_code,"
        "w.observed_at AS started_at,w.rowid AS activity_position,w.evidence AS activity_evidence,h.position AS source_position "
        "FROM invocation_outputs o JOIN invocations i ON i.id=o.invocation "
        "JOIN attempts a ON a.id=i.attempt JOIN threads t ON t.id=i.thread AND t.subject=a.subject "
        "JOIN worker_activity w ON w.invocation=o.invocation AND w.ordinal=o.ordinal "
        "JOIN history h ON h.subject=a.subject AND h.generation=a.generation "
        "WHERE o.process_identity IS NOT NULL AND o.started_at IS NOT NULL AND w.active=1 "
        "AND w.rowid=(SELECT min(rowid) FROM worker_activity WHERE invocation=o.invocation AND ordinal=o.ordinal AND active=1) "
        "AND ((h.kind='worker_active' AND h.identity=o.invocation || ':' || o.ordinal AND h.material_id=w.evidence) "
        "OR (h.kind='worker_started' AND h.identity=i.id AND h.material_id=o.process_identity "
        "AND json_extract(h.status,'$.stopped')=0 AND json_extract(h.status,'$.blocked') IS NULL "
        "AND json_array_length(json_extract(h.status,'$.gates'))=0 AND w.observed_at>=h.created_at "
        "AND NOT EXISTS (SELECT 1 FROM history n WHERE n.kind='worker_active' AND n.identity=o.invocation || ':' || o.ordinal) "
        "AND NOT EXISTS (SELECT 1 FROM history c JOIN actions act ON act.id=c.identity "
        "WHERE c.kind='control_applied' AND c.position>h.position AND c.created_at<=w.observed_at "
        "AND (act.subject IS NULL OR act.subject=a.subject))))")
    values: list[Any] = []
    for column, value in (("a.subject", subject), ("h.position", position), ("a.id", attempt), ("a.role", role), ("i.action_id", action)):
        if value is not None:
            sql += f" AND {column}=?"
            values.append(value)
    if after is not None:
        sql += " AND h.position>?"
        values.append(after)
    records = tx.rows(sql + " ORDER BY w.rowid,h.position LIMIT 1", values)
    if records:
        tx.get(records[0]["activity_evidence"], records[0]["subject"])
    return dict(records[0]) if records else None


def activity(tx: Transaction, invocation: str, ordinal: int, subject: str,
             *, until: float | None = None) -> dict[str, Any]:
    sql = "SELECT * FROM worker_activity WHERE invocation=? AND ordinal=?"
    values: tuple[Any, ...] = (invocation, ordinal)
    if until is not None:
        sql += " AND observed_at<=?"
        values += (until,)
    events = [dict(row) for row in tx.all(sql + " ORDER BY observed_at,identity", values)]
    start: float | None = None
    recorded = 0.0
    closed = 0
    ambiguous = False
    valid = bool(events)
    for event in events:
        stamp = event["observed_at"]
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
            raise ValueError("Invalid recorded activity timestamp")
        try:
            datetime.fromtimestamp(stamp, UTC)
        except (OverflowError, OSError, ValueError) as error:
            raise ValueError("Recorded activity timestamp is outside the supported calendar range") from error
        event["observed_at"] = float(stamp)
        event["active"] = bool(event["active"])
        event["evidence"] = json.loads(tx.get(event["evidence"], subject))
        if event["active"]:
            if start is not None or ambiguous:
                valid, ambiguous, start = False, True, None
            else:
                start = float(stamp)
        elif ambiguous:
            valid, ambiguous = False, False
        elif start is None:
            valid = False
        else:
            recorded += stamp - start
            closed += 1
            start = None
    complete = valid and start is None and not ambiguous
    return {"events": events, "elapsed_seconds": recorded if complete else None,
            "execution_timing_complete": complete,
            "recorded_execution_seconds": recorded if closed else None}


def recorded_profile(tx: Transaction, revision: int) -> dict[str, Any]:
    row = tx.one("SELECT subject,material_id FROM profile_revisions WHERE id=?", (revision,))
    value = json.loads(tx.get(row["material_id"], row["subject"]))
    if not isinstance(value, dict) or not isinstance(value.get("profiles"), dict):
        raise ValueError("Invalid recorded Profile Revision")
    return value


def _timing(samples: list[dict[str, Any]]) -> dict[str, Any]:
    complete = bool(samples) and all(sample["elapsed_seconds"] is not None for sample in samples)
    result: dict[str, Any] = {"execution_timing_complete": complete}
    if complete:
        result["execution_seconds"] = int(sum(sample["elapsed_seconds"] for sample in samples))
    known = [sample["recorded_execution_seconds"] for sample in samples if sample["recorded_execution_seconds"] is not None]
    if known:
        result["recorded_execution_seconds"] = int(sum(known))
    return result


def invocation_timing(invocations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize loaded activity facts; an open output cannot prove a total."""
    return _timing([{"elapsed_seconds": output["elapsed_seconds"] if output["exited_at"] is not None else None,
                     "recorded_execution_seconds": output["recorded_execution_seconds"]}
                    for invocation in invocations for output in invocation["outputs"]])


def execution_facts(tx: Transaction, row: Mapping[str, Any], *, attempt_id: str | None = None,
                    whole_run: bool = False) -> dict[str, Any]:
    """Aggregate original allocations through this history record, never process lifetime."""
    sql = ("SELECT i.id,i.attempt,a.subject,a.role,t.role AS binding_role,t.profile_revision,t.subject AS thread_subject "
           "FROM invocations i JOIN attempts a ON a.id=i.attempt LEFT JOIN threads t ON t.id=i.thread "
           "WHERE EXISTS (SELECT 1 FROM history h WHERE h.kind='agent_allocated' AND h.identity=i.id AND h.position<=?)")
    values: tuple[Any, ...] = (row["position"],)
    if not whole_run:
        sql += " AND a.subject=?"
        values += (row["subject"],)
    if attempt_id is not None:
        sql += " AND a.id=?"
        values += (attempt_id,)
    samples: list[dict[str, Any]] = []
    for invocation in tx.all(sql + " ORDER BY i.rowid", values):
        profile = (recorded_profile(tx, invocation["profile_revision"])["profiles"].get(invocation["binding_role"], {})
                   if invocation["thread_subject"] == invocation["subject"] else {})
        outputs = tx.all("SELECT * FROM invocation_outputs WHERE invocation=? ORDER BY ordinal", (invocation["id"],))
        for output in outputs:
            if output["ordinal"] != outputs[0]["ordinal"] and not tx.rows(
                "SELECT position FROM history WHERE kind='output_repair_allocated' AND identity=? AND position<=?",
                (f'{invocation["id"]}:{output["ordinal"]}', row["position"])):
                continue
            measured = activity(tx, invocation["id"], output["ordinal"], invocation["subject"], until=row["created_at"])
            if output["exited_at"] is None or output["exited_at"] > row["created_at"]:
                measured["elapsed_seconds"] = None
            if (output["dispatch_state"] == "unavailable" and output["started_at"] is None and tx.rows(
                "SELECT position FROM history WHERE subject=? AND kind='paused' AND identity='worker_unavailable' "
                "AND material_id=? AND position<=? LIMIT 1",
                (invocation["subject"], output["start_evidence"], row["position"]))):
                measured.update(elapsed_seconds=0.0, recorded_execution_seconds=0.0)
            samples.append({"attempt": invocation["attempt"], "role": invocation["role"],
                            "model": profile.get("model"), "reasoning_effort": profile.get("reasoning_effort"),
                            "elapsed_seconds": measured["elapsed_seconds"],
                            "recorded_execution_seconds": measured["recorded_execution_seconds"]})
    effort: dict[str, Any] = {}
    for role in ("development", "review"):
        participating = [sample for sample in samples if sample["role"] == role]
        if not participating:
            continue
        configurations = []
        keys = dict.fromkeys((sample["model"], sample["reasoning_effort"]) for sample in participating)
        for model, reasoning in keys:
            group = [sample for sample in participating if (sample["model"], sample["reasoning_effort"]) == (model, reasoning)]
            configurations.append({"model": model, "reasoning_effort": reasoning,
                                   "rounds": len({sample["attempt"] for sample in group}), "rounds_complete": True,
                                   **_timing(group)})
        rounds = len({sample["attempt"] for sample in participating})
        effort[role] = {"rounds": rounds, "rounds_complete": True, "configurations": configurations,
                        "configurations_complete": all(model is not None and reasoning is not None for model, reasoning in keys),
                        "shared_rounds": sum(config["rounds"] for config in configurations) > rounds,
                        **_timing(participating)}
    return {"effort": effort, **_timing(samples)}
