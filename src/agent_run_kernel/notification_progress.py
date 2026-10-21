"""Read-only compression of delayed ordinary notification progress."""
from collections.abc import Mapping, Sequence
import json
from typing import Any

from agent_run.notification_event_facts import event_envelope

from .notification_events import resume_facts
from .storage import Transaction


def _source(tx: Transaction, event: Mapping[str, Any]) -> dict[str, Any] | None:
    fields = {"source_position", "source_material_id", "work_subject", "generation"}
    if (not fields.issubset(event) or type(event["source_position"]) is not int
            or not isinstance(event["work_subject"], str) or type(event["generation"]) is not int
            or event["source_material_id"] is not None and not isinstance(event["source_material_id"], str)):
        return None
    rows = tx.rows("SELECT * FROM history WHERE position=? AND subject=? AND generation=? AND material_id IS ?",
                   (event["source_position"], event["work_subject"], event["generation"], event["source_material_id"]))
    if not rows:
        return None
    row = dict(rows[0])
    if row["material_id"] is not None:
        if not tx.rows("SELECT id FROM material WHERE id=?", (row["material_id"],)):
            return None
        tx.get(row["material_id"], row["subject"])  # Corruption is a read failure, never an obsolete card.
    return row


def _live_attempt(tx: Transaction, subject: str, generation: int, attempt: str) -> bool:
    return bool(tx.rows("SELECT a.id FROM subjects s JOIN attempts a ON a.id=s.current_attempt "
                        "JOIN invocations i ON i.id=a.current_invocation JOIN leases l ON l.invocation=i.id "
                        "JOIN invocation_outputs o ON o.invocation=i.id AND o.lease=l.id "
                        "WHERE s.id=? AND s.generation=? AND a.id=? AND a.result_id IS NULL "
                        "AND o.ordinal=(SELECT max(ordinal) FROM invocation_outputs WHERE invocation=i.id) "
                        "AND o.dispatch_state='started' AND o.process_identity IS NOT NULL "
                        "AND o.started_at IS NOT NULL AND o.exited_at IS NULL",
                        (subject, generation, attempt)))


def _eligible(tx: Transaction, event: Mapping[str, Any], row: Mapping[str, Any], mode: str) -> bool | None:
    kind = event["kind"]
    job = tx.one("SELECT * FROM subjects WHERE id=?", (row["subject"],))
    same_generation = job["generation"] == row["generation"]
    if kind == "resume":
        facts = resume_facts(tx, row)
        if not facts or facts["resume_id"] != event.get("resume_id"):
            return None
        if row["kind"] != "worker_started":
            return False  # A consumed controller result is historical, like a finished role.
    if kind in {"ticket_started", "parent_started", "acceptance_started", "stage_start", "resume"}:
        if row["kind"] != "worker_started":
            return None
        original = tx.rows("SELECT a.id FROM invocations i JOIN attempts a ON a.id=i.attempt "
                           "JOIN invocation_outputs o ON o.invocation=i.id WHERE i.id=? AND a.id=? "
                           "AND a.subject=? AND a.generation=? AND o.process_identity=? AND o.started_at IS NOT NULL",
                           (row["identity"], event.get("attempt_id"), row["subject"], row["generation"], row["material_id"]))
        if not original:
            return None
        return (same_generation and (kind != "stage_start" or mode == "detailed")
                and _live_attempt(tx, row["subject"], row["generation"], event["attempt_id"]))
    if kind == "stage_end" and row["kind"] == "role_consumed":
        original = tx.rows("SELECT id FROM attempts WHERE id=? AND id=? AND subject=? AND generation=? AND result_id=?",
                           (event.get("attempt_id"), row["identity"], row["subject"], row["generation"], row["material_id"]))
        return False if original else None
    if kind in {"pr_created", "ticket_completed"} and row["kind"] == "effect_consumed":
        original = (tx.rows("SELECT id FROM effects WHERE id=? AND subject=? AND generation=? "
                            "AND operation='pr_write' AND status='confirmed' AND receipt_id=?",
                            (row["identity"], row["subject"], row["generation"], row["material_id"]))
                    if kind == "pr_created" else tx.rows(
                        "SELECT close_effect FROM completions WHERE subject=? AND generation=? AND close_effect=?",
                        (row["subject"], row["generation"], row["identity"])))
        return False if original else None
    if kind == "acceptance_passed" and row["kind"] == "acceptance_promoted":
        promotion = json.loads(tx.get(row["material_id"], row["subject"]))
        if promotion["review_attempt"] != event.get("attempt_id") or row["identity"] != event.get("attempt_id"):
            return None
        if not tx.rows("SELECT id FROM attempts WHERE id=? AND subject=? AND generation=? "
                       "AND role='review' AND outcome='pass' AND result_id IS NOT NULL",
                       (row["identity"], row["subject"], row["generation"])):
            return None
        return bool(same_generation and job["review_attempt"] == promotion["review_attempt"]
                and job["integrated_sha"] == promotion["promoted_sha"]
                and job["integrated_tree"] == promotion["promoted_tree"])
    if kind == "acceptance_repair" and row["kind"] == "role_consumed":
        original = tx.rows("SELECT id FROM attempts WHERE id=? AND id=? AND subject=? AND generation=? "
                           "AND role='review' AND outcome='fail' AND result_id=?",
                           (event.get("failed_review_attempt"), row["identity"], row["subject"], row["generation"], row["material_id"]))
        if not original:
            return None
        return (same_generation and job["repair_source"] in {"acceptance", "integration"}
                and job["last_failure"] == row["material_id"] and not tx.rows(
                    "SELECT position FROM history WHERE subject=? AND generation=? AND kind='acceptance_promoted' AND position>? LIMIT 1",
                    (row["subject"], row["generation"], row["position"])))
    if kind == "approval_update" and row["kind"] == "boundary_update":
        previous = tx.rows("SELECT position FROM history WHERE position=? AND subject=? AND generation=? AND phase='approval' AND position<?",
                           (event.get("previous_approval_position"), row["subject"], row["generation"], row["position"]))
        if not previous:
            return None
        latest = tx.one("SELECT max(position) FROM history WHERE subject=? AND generation=? AND kind='boundary_update'",
                        (row["subject"], row["generation"]))[0]
        return bool(same_generation and latest == row["position"] and job["phase"] == row["phase"])
    return None


def obsolete_progress(
    tx: Transaction, events: Sequence[Mapping[str, Any]], mode: str, *, current_boundary: bool,
) -> set[str]:
    """Return obsolete IDs from at most 32 ordinary, non-sending pending events.

    The caller owns important cards, cancellation and delivery budgets. Missing
    source associations stay unknown; unreadable canonical facts propagate.
    """
    if len(events) > 32:
        raise ValueError("Ordinary progress exceeds the pending notification limit")
    known: dict[str, tuple[int, bool]] = {}
    for event in events:
        if not isinstance(event.get("id"), str) or not isinstance(event.get("kind"), str):
            continue
        if event["kind"] == "run_start":
            run = tx.one("SELECT id,created_at,language FROM runs")
            identity = event_envelope(dict(run) | {"run_id": run["id"]}, "run_start", run["id"], "")["id"]
            if event["id"] == identity:
                started = tx.rows("SELECT position FROM history WHERE kind='worker_started' LIMIT 1")
                known[identity] = (-1, not started)  # Original Run start precedes history.
            continue
        row = _source(tx, event)
        if row is not None:
            eligible = _eligible(tx, event, row, mode)
            if eligible is not None:
                known[event["id"]] = (row["position"], eligible)
    latest = max((position for position, live in known.values() if live), default=-1)
    return {identity for identity, (position, live) in known.items()
            if current_boundary or not live or position < latest}
