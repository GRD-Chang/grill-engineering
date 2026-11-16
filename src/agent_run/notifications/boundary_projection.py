"""Read-only card content from current gates and proven lifecycle receipts.

Delivery supplies occurrence identities; this module neither allocates them nor
owns cursors, outbox claims, retry budgets or physical calls.
"""
from collections.abc import Mapping
import json
from typing import Any

from agent_run.notifications.envelope import event_copy, event_envelope
from agent_run.notifications.event_projection import completion_context
from agent_run.queries.execution import execution_facts
from agent_run.queries.read_facts import material
from agent_run.queries.resume_facts import resume_facts
from agent_run.storage.run_scope import run_statement
from agent_run.storage.sqlite import Transaction


def boundary_scopes(row: Mapping[str, Any], generations: Mapping[str, int]) -> list[tuple[str | None, int | None, list[str]]]:
    """Interpret a committed history snapshot; delivery tracks its occurrences."""
    if row["status"] is None:
        return []
    status = json.loads(row["status"])
    reason = "operator_stopped" if status["stopped"] else status["blocked"]
    scopes: list[tuple[str | None, int | None, list[str]]] = [(None, None, [reason] if reason else [])]
    if generations.get(row["subject"]) == row["generation"]:
        reasons = list(status["gates"])
        if row["phase"] == "approval":
            reasons.append("final_approval")
        scopes.append((row["subject"], row["generation"], reasons))
    return scopes


def project_terminal(tx: Transaction, view: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """Describe a committed lifecycle result, without granting completion."""
    if not view["is_terminal"]:
        return None
    if view["status"] == "abandoned":
        action = view["abandonment"]
        if action is None or action["status"] != "complete":
            raise ValueError("abandoned notification has no completed original Action")
        return event_envelope(state, "boundary", ["abandoned", action["action"]],
                              event_copy(state, "abandoned"), "yellow",
                              current=True, terminal=True, status_code="abandoned",
                              execution_timing_complete=False)
    if view["status"] != "completed":
        return None
    closures = view["closures"]
    if len(closures) != 1 or material(tx, closures[0]["receipt_id"], closures[0]["subject"]).get("disposition") != "target":
        raise ValueError("completed notification has no unique original close receipt")
    closed = closures[0]
    number = closed["pr_number"]
    if type(number) is not int or number <= 0:
        raise ValueError("completed notification has no original Pull Request")
    cleanup = view["delivery_cleanup"]
    pending = cleanup is not None and cleanup["status"] == "cleanup_pending"
    status = "completed_cleanup" if pending else "completed"
    summary = event_copy(state, "separator").join((
        event_copy(state, "merged", number=number), event_copy(state, "closed")))
    if pending:
        summary += event_copy(state, "cleanup_summary")
    event = event_envelope(state, "boundary", [status, closed["effect_id"], closed["receipt_id"]],
                          event_copy(state, "cleanup" if pending else "completed"),
                          "yellow" if pending else "green", current=True, terminal=True,
                          status_code="completed", summary=summary,
                          url=f"https://github.com/{state['repository']}/pull/{number}",
                          execution_timing_complete=False, **completion_context(view, state))

    original = tx.one(*run_statement(tx, "SELECT * FROM history WHERE run_id=? AND kind='effect_consumed' AND identity=? AND material_id=?", (closed["effect_id"], closed["receipt_id"])))
    timing = execution_facts(tx, dict(original), whole_run=True)
    total = timing.pop("execution_seconds", None)
    event.update(timing, total_seconds=total)
    return event


def project_gate(tx: Transaction, view: Mapping[str, Any], state: dict[str, Any],
                 action: Mapping[str, Any], key: str, occurrence: int) -> dict[str, Any]:
    """Map one current operator boundary using its original delivery identity."""
    reason = action["reason"]
    title_key = ("stopped" if reason == "operator_stopped" else "approval"
                 if reason == "final_approval" else "human_title"
                 if reason == "human_blocker" else "execution_failed"
                 if reason == "execution_failed" else "budget" if "budget" in reason else "timeout"
                 if reason == "supervision_timeout" else "requeue" if reason == "requeue_required"
                 else "human_title")
    event = event_envelope(state, "boundary", [key, occurrence],
                           event_copy(state, title_key),
                           "red" if reason in {"execution_failed", "supervision_timeout"} else "yellow",
                           current=True,
                           slot_id=key, occurrence=occurrence, work_subject=action["subject"],
                           generation=action.get("generation"), object=action["object"],
                           scope=action["scope"],
                           summary="; ".join(str(value) for value in action["reasons"]),
                           next_step=action["next_action"])
    if reason == "final_approval":
        owner = next(job for job in view["subjects"] if job["subject"] == action["subject"])
        number = owner["pr_number"]
        if type(number) is int and number > 0:
            event.update(approval_pr=number,
                         url=f"https://github.com/{state['repository']}/pull/{number}")
    facts: dict[str, Any] = {}
    if action["subject"] is not None:
        gate_history = tx.rows(*run_statement(tx, "SELECT h.* FROM history h JOIN gates g ON g.run_id=h.run_id AND g.subject=h.subject AND g.code=h.identity AND g.evidence IS h.material_id WHERE h.run_id=? AND h.kind='paused' AND h.subject=? AND h.generation=? AND h.identity=? ORDER BY h.position DESC LIMIT 1", (action["subject"], action.get("generation"), action["reason"])))
        if gate_history:
            facts = resume_facts(tx, dict(gate_history[0]))
    if facts:
        event.update(facts)
        event["title"] = event_copy(state, "resume_" + facts["resume_outcome"])
        event["color"] = "red" if facts["resume_outcome"] == "failed" else "yellow"
        event["summary"] = (event_copy(state, "resume_" + facts["resume_outcome"])
                            + event_copy(state, "separator") + event["summary"])
    return event
