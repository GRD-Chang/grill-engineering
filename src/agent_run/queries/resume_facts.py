from collections.abc import Mapping
from typing import Any

from agent_run.storage.sqlite import Transaction


def resume_facts(tx: Transaction, row: Mapping[str, Any]) -> dict[str, Any]:
    """Bind a real outcome to the original target, reopening receipt and control interval."""
    progress = {"worker_started", "effect_consumed", "checks_observed", "role_consumed"}
    if row["kind"] not in progress | {"paused"} or row["identity"] == "operator_stopped":
        return {}
    worker = []
    if row["kind"] == "worker_started":
        worker = tx.rows("SELECT i.action_id,i.attempt,a.role,a.ordinal FROM invocations i "
                         "JOIN attempts a ON a.id=i.attempt JOIN invocation_outputs o ON o.invocation=i.id "
                         "WHERE i.id=? AND a.subject=? AND a.generation=? AND o.process_identity=? "
                         "AND o.started_at IS NOT NULL", (row["identity"], row["subject"], row["generation"], row["material_id"]))
        if not worker:
            return {}
    elif row["kind"] == "effect_consumed" and not tx.rows(
        "SELECT id FROM effects WHERE id=? AND subject=? AND generation=? AND status='confirmed' AND receipt_id=?",
        (row["identity"], row["subject"], row["generation"], row["material_id"])):
        return {}
    elif row["kind"] == "checks_observed" and not tx.rows(
        "SELECT id FROM checks WHERE id=? AND subject=? AND material_id=?",
        (row["identity"], row["subject"], row["material_id"])):
        return {}
    elif row["kind"] == "role_consumed" and not tx.rows(
        "SELECT id FROM attempts WHERE id=? AND subject=? AND generation=? AND result_id=?",
        (row["identity"], row["subject"], row["generation"], row["material_id"])):
        return {}
    action_id = worker[0]["action_id"] if worker else None
    sql = ("SELECT c.id,c.subject,h.position,h.material_id FROM actions c JOIN history h ON h.identity=c.id "
           "AND h.kind='control_applied' AND h.material_id=c.receipt_id WHERE c.kind='resume' AND h.position<? AND ")
    values: tuple[Any, ...] = (row["position"],)
    if action_id is not None:
        sql += "c.id=?"
        values += (action_id,)
    else:
        sql += "((c.subject=? AND h.generation=?) OR c.subject IS NULL)"
        values += (row["subject"], row["generation"])
    actions = tx.rows(sql + " ORDER BY h.position DESC LIMIT 1", values)
    if not actions:
        return {}
    action = actions[0]
    whole = action["subject"] is None
    if action["subject"] not in {None, row["subject"]}:
        return {}
    interval = "" if whole else " AND (c.subject=? OR c.subject IS NULL)"
    interval_values: tuple[Any, ...] = (action["position"], row["position"])
    if not whole:
        interval_values += (row["subject"],)
    if tx.rows("SELECT h.position FROM history h JOIN actions c ON c.id=h.identity WHERE h.kind='control_applied' "
               "AND h.position>? AND h.position<?" + interval + " LIMIT 1", interval_values):
        return {}
    if whole:
        reopened = tx.rows("SELECT h.position,h.material_id,c.id,c.kind FROM history h JOIN actions c ON c.id=h.identity "
                           "WHERE h.kind='control_applied' AND c.subject IS NULL AND h.position<? ORDER BY h.position DESC LIMIT 1",
                           (action["position"],))
        if not reopened or reopened[0]["kind"] != "stop":
            return {}
    else:
        reopened = tx.rows("SELECT position,material_id,NULL AS id FROM history WHERE kind='paused' AND subject=? "
                           "AND generation=? AND position<? ORDER BY position DESC LIMIT 1",
                           (row["subject"], row["generation"], action["position"]))
        if not reopened:
            return {}
    scope = "" if whole else " AND subject=? AND generation=?"
    scope_values: tuple[Any, ...] = (action["position"],)
    if not whole:
        scope_values += (row["subject"], row["generation"])
    kinds = "('paused')" if row["kind"] == "paused" else "('worker_started','effect_consumed','checks_observed','role_consumed')"
    first = tx.one("SELECT min(position) FROM history WHERE position>?" + scope + " AND kind IN " + kinds, scope_values)[0]
    if first != row["position"]:
        return {}
    failures = {"worker_unavailable", "execution_failed", "worker_start_unknown", "supervision_timeout"}
    outcome = ("failed" if row["identity"] in failures else "human_action") if row["kind"] == "paused" else "started"
    facts = {"resume_id": action["id"], "action_id": action["id"], "resume_outcome": outcome,
             "resume_scope": "run" if whole else "subject", "resume_subject": action["subject"],
             "resume_generation": None if whole else row["generation"],
             "evidence": row["kind"], "work_subject": row["subject"], "generation": row["generation"],
             "source_position": row["position"], "source_material_id": row["material_id"],
             "resume_position": action["position"], "resume_material_id": action["material_id"],
             "resumed_from_position": reopened[0]["position"], "resumed_from_material_id": reopened[0]["material_id"],
             "resumed_from_action": reopened[0]["id"]}
    if row["kind"] == "paused":
        facts["reason_code"] = row["identity"]
    if worker:
        facts.update(attempt_id=worker[0]["attempt"], invocation_id=row["identity"],
                     role=worker[0]["role"], round=worker[0]["ordinal"])
    elif row["kind"] == "effect_consumed":
        facts["effect_id"] = row["identity"]
    return facts
