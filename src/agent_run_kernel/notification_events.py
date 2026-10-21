"""Read-only notification projection of committed native history facts."""
from collections.abc import Mapping
from datetime import UTC, datetime
import json
from typing import Any

from agent_run.notification_event_facts import event_copy, event_envelope

from .storage import Transaction


def _subject_facts(tx: Transaction, subject: Mapping[str, Any], revision: str,
                   state: dict[str, Any]) -> dict[str, Any]:
    if subject["kind"] != "ticket":
        return {}
    title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                    (subject["issue"], revision))
    return {"task_number": subject["issue"], "task_title": title[0][0] if title else f"#{subject['issue']}",
            "url": f"https://github.com/{state['repository']}/issues/{subject['issue']}"}


def _started(tx: Transaction, row: Mapping[str, Any], subject: Mapping[str, Any],
             state: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    matches = tx.rows("SELECT a.*,o.started_at FROM invocations i JOIN attempts a ON a.id=i.attempt "
                      "JOIN invocation_outputs o ON o.invocation=i.id "
                      "WHERE i.id=? AND a.subject=? AND a.generation=? AND o.process_identity=? "
                      "AND o.started_at IS NOT NULL ORDER BY o.ordinal LIMIT 1",
                      (row["identity"], row["subject"], row["generation"], row["material_id"]))
    if not matches:
        return []
    attempt = dict(matches[0])
    first = tx.one("SELECT min(h.position) FROM history h JOIN invocations i ON i.id=h.identity "
                   "WHERE h.subject=? AND h.generation=? AND h.kind='worker_started' AND i.attempt=?",
                   (row["subject"], row["generation"], attempt["id"]))[0]
    if first != row["position"]:
        return []  # Resuming or correcting the same Attempt is not a new role start.
    started_at = datetime.fromtimestamp(attempt["started_at"], UTC).isoformat()
    first_subject = tx.one("SELECT min(position) FROM history WHERE subject=? AND kind='worker_started'",
                           (row["subject"],))[0]
    if (first_subject == row["position"] and attempt["role"] == "development"
            and subject["kind"] in {"ticket", "parent_only"}):
        kind = "ticket_started" if subject["kind"] == "ticket" else "parent_started"
        facts = _subject_facts(tx, subject, subject["revision"], state)
        name = facts.get("task_title") or state["parent"]["title"] or f"#{subject['issue']}"
        identity = f"ticket:{subject['issue']}" if kind == "ticket_started" else f"parent-only:{state['run_id']}"
        return [event_envelope(state, kind, identity, event_copy(state, kind, title=name),
                               attempt_id=attempt["id"], started_at=started_at, **facts)]
    if mode != "detailed":
        return []
    facts = _subject_facts(tx, subject, attempt["revision"], state)
    role = attempt["role"]
    name = facts.get("task_title") or state["parent"]["title"] or event_copy(state, "parent")
    title = (event_copy(state, "development_start", title=name) if role == "development"
             else event_copy(state, "review_ticket_start", title=name) if role == "review" and facts
             else event_copy(state, "review_start") if role == "review"
             else event_copy(state, "publication_ticket_start" if facts else "publication_start"))
    return [event_envelope(state, "stage_start", attempt["id"], title, started_at=started_at,
                           phase=event_copy(state, "role_" + role), role=role, round=attempt["ordinal"],
                           attempt_id=attempt["id"], work_subject=row["subject"], generation=row["generation"], **facts)]


def _consumed(tx: Transaction, row: Mapping[str, Any], subject: Mapping[str, Any],
              state: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    if mode != "detailed":
        return []
    attempt = dict(tx.one("SELECT * FROM attempts WHERE id=? AND subject=? AND generation=? AND result_id=?",
                          (row["identity"], row["subject"], row["generation"], row["material_id"])))
    role = attempt["role"]
    # A formal promotion is a separate committed fact. Its milestone replaces
    # this same Review result when both are visible in the original snapshot.
    if role == "review" and tx.rows("SELECT position FROM history WHERE subject=? AND generation=? "
                                    "AND kind='acceptance_promoted' AND identity=? LIMIT 1",
                                    (row["subject"], row["generation"], attempt["id"])):
        return []
    result = json.loads(tx.get(row["material_id"], row["subject"]))
    facts = _subject_facts(tx, subject, attempt["revision"], state)
    checks = {key: lane["status"] for key, lane in result.get("checks", {}).items()}
    findings = [finding for lane in result.get("checks", {}).values() for finding in lane["findings"]]
    outcome = {"development": "candidate", "review": "acceptance_artifact", "publication": "publication_artifact"}[role]
    status_code = "review_failed" if role == "review" and attempt["outcome"] == "fail" else outcome
    summary = (result["summary"] if role == "development" else result["pr_title"] if role == "publication"
               else event_copy(state, "separator").join(dict.fromkeys(lane["evidence"] for lane in result["checks"].values())))
    if findings:
        summary = event_copy(state, "findings", count=len(findings)) + event_copy(state, "separator").join(findings)
    title = (event_copy(state, "development_end") if role == "development"
             else event_copy(state, "publication_ticket_end" if facts else "publication_end") if role == "publication"
             else event_copy(state, "repair_title") if status_code == "review_failed"
             else event_copy(state, "review_ticket_end" if facts else "review_result_pending"))
    return [event_envelope(state, "stage_end", [attempt["id"], outcome, False, checks], title,
                           "orange" if status_code == "review_failed" else "green",
                           phase=event_copy(state, "role_" + role), role=role, round=attempt["ordinal"],
                           attempt_id=attempt["id"], work_subject=row["subject"], generation=row["generation"],
                           checks=checks, status_code=status_code, summary=summary, findings_count=len(findings),
                           next_step=event_copy(state, "repair_next") if status_code == "review_failed" else "",
                           execution_timing_complete=False, **facts)]


def events(tx: Transaction, row: Mapping[str, Any], state: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    """Project this original history record without changing its source facts."""
    if row["kind"] not in {"worker_started", "role_consumed", "effect_consumed", "acceptance_promoted"}:
        return []
    subject = dict(tx.one("SELECT id,kind,issue,revision FROM subjects WHERE id=?", (row["subject"],)))
    if row["kind"] == "worker_started":
        return _started(tx, row, subject, state, mode)
    if row["kind"] == "role_consumed":
        return _consumed(tx, row, subject, state, mode)
    if row["kind"] == "acceptance_promoted":
        promotion = json.loads(tx.get(row["material_id"], row["subject"]))
        review = tx.one("SELECT * FROM attempts WHERE id=? AND subject=? AND generation=? AND role='review'",
                        (row["identity"], row["subject"], row["generation"]))
        if (subject["kind"] == "ticket" or promotion["review_attempt"] != review["id"]
                or review["outcome"] != "pass" or review["result_id"] is None):
            raise ValueError("formal acceptance notification has no original Review")
        identity = [row["generation"], promotion["promoted_sha"], review["base_sha"],
                    review["expected_tree"], review["revision"], review["graph_revision"]]
        return [event_envelope(state, "acceptance_passed", identity,
                               event_copy(state, "parent_acceptance_passed" if subject["kind"] == "parent_only" else "acceptance_passed"),
                               "green", summary=event_copy(state, "acceptance_passed_summary"),
                               attempt_id=review["id"], round=review["ordinal"],
                               phase=event_copy(state, "role_review"), execution_timing_complete=False)]
    if mode == "detailed":
        original = tx.rows("SELECT payload_id FROM effects WHERE id=? AND subject=? AND generation=? "
                           "AND operation='pr_write' AND status='confirmed' AND receipt_id=?",
                           (row["identity"], row["subject"], row["generation"], row["material_id"]))
        if original:
            intent = json.loads(tx.get(original[0]["payload_id"], row["subject"]))
            if "prior" not in intent or intent["prior"] is not None:
                return []
            receipt = json.loads(tx.get(row["material_id"], row["subject"]))
            pr = receipt.get("pr")
            if (receipt.get("disposition") != "target" or not isinstance(pr, dict)
                    or pr.get("repository") != state["repository"]
                    or type(pr.get("number")) is not int or pr["number"] <= 0):
                return []
            number = pr["number"]
            return [event_envelope(state, "pr_created", number, event_copy(state, "pr_created", number=number), "green",
                                   summary=event_copy(state, "pr_summary"),
                                   url=f"https://github.com/{pr['repository']}/pull/{number}")]
    if subject["kind"] == "ticket" and tx.rows("SELECT generation FROM completions WHERE subject=? AND generation=? AND close_effect=?",
                                             (row["subject"], row["generation"], row["identity"])):
        facts = _subject_facts(tx, subject, subject["revision"], state)
        return [event_envelope(state, "ticket_completed", [str(subject["issue"]), row["generation"]],
                               event_copy(state, "ticket_completed", title=facts["task_title"]), "green", **facts)]
    return []
