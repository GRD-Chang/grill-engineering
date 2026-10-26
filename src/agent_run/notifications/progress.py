"""Read-only compression of delayed ordinary notification progress."""
from collections.abc import Mapping, Sequence
import json
from typing import Any

from agent_run.notifications.envelope import event_envelope

from agent_run.notifications.event_projection import event_state, project_event
from agent_run.storage.sqlite import Transaction
from agent_run.queries.execution import worker_activity_start


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


def obsolete_progress(
    tx: Transaction, events: Sequence[Mapping[str, Any]], mode: str, *, current_boundary: bool,
) -> set[str]:
    """Return obsolete IDs from at most 32 ordinary, non-sending pending events.

    The caller owns important cards, cancellation and delivery budgets. Missing
    source associations stay unknown; unreadable canonical facts propagate.
    """
    if len(events) > 32:
        raise ValueError("Ordinary progress exceeds the pending notification limit")
    state = event_state(tx)
    known: dict[str, tuple[int, bool]] = {}
    for event in events:
        if not isinstance(event.get("id"), str) or not isinstance(event.get("kind"), str):
            continue
        if event["kind"] == "run_start":
            run = tx.one("SELECT id,created_at,language FROM runs")
            identity = event_envelope(dict(run) | {"run_id": run["id"]}, "run_start", run["id"], "")["id"]
            if event["id"] == identity:
                started = worker_activity_start(tx, None)
                known[identity] = (-1, not started)  # Original Run start precedes history.
            continue
        row = _source(tx, event)
        if row is not None:
            for projected in project_event(tx, row, state, mode):
                if projected.matches(event) and projected.applicable is not None:
                    known[event["id"]] = (row["position"], projected.applicable)
    latest = max((position for position, live in known.values() if live), default=-1)
    return {identity for identity, (position, live) in known.items()
            if current_boundary or not live or position < latest}
