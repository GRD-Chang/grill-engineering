"""Read-only event content and applicability from original committed facts."""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from agent_run.notifications.envelope import event_copy, event_envelope
from agent_run.contracts.copy import selected_language, text

from agent_run.queries.execution import execution_facts
from agent_run.storage.sqlite import Transaction
from agent_run.queries.resume_facts import resume_facts


@dataclass(frozen=True)
class EventProjection:
    content: dict[str, Any]
    applicable: bool | None
    visible: bool = True

    def matches(self, event: Mapping[str, Any]) -> bool:
        binding = "failed_review_attempt" if "failed_review_attempt" in self.content else "attempt_id"
        return all(event.get(key) == self.content.get(key) for key in (
            "id", "kind", "source_position", "source_material_id", "work_subject", "generation",
            binding, "resume_id", "previous_approval_position"))


def event_state(tx: Transaction) -> dict[str, Any]:
    run = tx.one("SELECT id,repository,parent_issue,parent_revision,language,created_at FROM runs")
    title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                    (run["parent_issue"], run["parent_revision"]))
    return {"run_id": run["id"], "repository": run["repository"],
            "parent": {"number": run["parent_issue"], "title": title[0][0] if title else ""},
            "language": run["language"], "created_at": run["created_at"]}


def _live_attempt(tx: Transaction, subject: str, generation: int, attempt: str) -> bool:
    return bool(tx.rows("SELECT a.id FROM subjects s JOIN attempts a ON a.id=s.current_attempt "
                        "JOIN invocations i ON i.id=a.current_invocation JOIN leases l ON l.invocation=i.id "
                        "JOIN invocation_outputs o ON o.invocation=i.id AND o.lease=l.id "
                        "WHERE s.id=? AND s.generation=? AND a.id=? AND a.result_id IS NULL "
                        "AND o.ordinal=(SELECT max(ordinal) FROM invocation_outputs WHERE invocation=i.id) "
                        "AND o.dispatch_state='started' AND o.process_identity IS NOT NULL "
                        "AND o.started_at IS NOT NULL AND o.exited_at IS NULL",
                        (subject, generation, attempt)))


def _subject_facts(tx: Transaction, subject: Mapping[str, Any], revision: str,
                   state: dict[str, Any]) -> dict[str, Any]:
    if subject["kind"] != "ticket":
        return {}
    title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                    (subject["issue"], revision))
    return {"task_number": subject["issue"], "task_title": title[0][0] if title else f"#{subject['issue']}",
            "url": f"https://github.com/{state['repository']}/issues/{subject['issue']}"}


def _started(tx: Transaction, row: Mapping[str, Any], subject: Mapping[str, Any],
             state: dict[str, Any], mode: str) -> list[EventProjection]:
    matches = tx.rows("SELECT a.*,o.started_at,o.dispatch_state,o.exit_code FROM invocations i JOIN attempts a ON a.id=i.attempt "
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
    applicable = _live_attempt(tx, row["subject"], row["generation"], attempt["id"])
    started_at = datetime.fromtimestamp(attempt["started_at"], UTC).isoformat()
    if subject["kind"] == "final_run":
        if attempt["role"] == "review":
            first_review = tx.one("SELECT min(h.position) FROM history h JOIN invocations i ON i.id=h.identity "
                                  "JOIN attempts a ON a.id=i.attempt WHERE h.subject=? "
                                  "AND h.kind='worker_started' AND a.role='review'", (row["subject"],))[0]
            if first_review == row["position"]:
                return [EventProjection(event_envelope(state, "acceptance_started", state["run_id"],
                                       event_copy(state, "acceptance_started"), attempt_id=attempt["id"], started_at=started_at), applicable)]
        failed = _first_repair(tx, row["subject"])
        if (attempt["role"] == "development" and failed is not None
                and attempt["generation"] == failed["generation"] and row["position"] > failed["position"]):
            context = json.loads(tx.get(attempt["context_id"], row["subject"]))
            source = context.get("repair_source")
            evidence = {"acceptance": "acceptance_artifact", "integration": "integration_evidence"}.get(source)
            first_repair = tx.one("SELECT min(h.position) FROM history h JOIN invocations i ON i.id=h.identity "
                                  "JOIN attempts a ON a.id=i.attempt WHERE h.subject=? AND h.generation=? AND h.position>? "
                                  "AND h.kind='worker_started' AND a.role='development'",
                                  (row["subject"], failed["generation"], failed["position"]))[0]
            if (first_repair == row["position"] and context.get("generation") == failed["generation"]
                    and evidence is not None and context.get(evidence) == json.loads(tx.get(failed["material_id"], row["subject"]))):
                live = attempt["dispatch_state"] == "started" and attempt["exit_code"] is None
                return [_repair_event(tx, subject, state, live, failed, attempt["id"])]
    first_subject = tx.one("SELECT min(position) FROM history WHERE subject=? AND kind='worker_started'",
                           (row["subject"],))[0]
    if (first_subject == row["position"] and attempt["role"] == "development"
            and subject["kind"] in {"ticket", "parent_only"}):
        kind = "ticket_started" if subject["kind"] == "ticket" else "parent_started"
        facts = _subject_facts(tx, subject, subject["revision"], state)
        name = facts.get("task_title") or state["parent"]["title"] or f"#{subject['issue']}"
        identity = f"ticket:{subject['issue']}" if kind == "ticket_started" else f"parent-only:{state['run_id']}"
        return [EventProjection(event_envelope(state, kind, identity, event_copy(state, kind, title=name),
                               attempt_id=attempt["id"], started_at=started_at, **facts), applicable)]
    if mode != "detailed":
        return []
    facts = _subject_facts(tx, subject, attempt["revision"], state)
    role = attempt["role"]
    name = facts.get("task_title") or state["parent"]["title"] or event_copy(state, "parent")
    title = (event_copy(state, "development_start", title=name) if role == "development"
             else event_copy(state, "review_ticket_start", title=name) if role == "review" and facts
             else event_copy(state, "review_start") if role == "review"
             else event_copy(state, "publication_ticket_start" if facts else "publication_start"))
    return [EventProjection(event_envelope(state, "stage_start", attempt["id"], title, started_at=started_at,
                           phase=event_copy(state, "role_" + role), role=role, round=attempt["ordinal"],
                           attempt_id=attempt["id"], work_subject=row["subject"], generation=row["generation"], **facts), applicable)]


def _first_repair(tx: Transaction, subject: str) -> dict[str, Any] | None:
    rows = tx.rows("SELECT h.position,h.generation,h.material_id,h.identity FROM history h JOIN attempts a ON a.id=h.identity "
                   "WHERE h.subject=? AND a.subject=h.subject AND a.generation=h.generation "
                   "AND h.kind='role_consumed' AND h.phase='development' AND h.purpose='integration' "
                   "AND a.role='review' AND a.outcome='fail' ORDER BY h.position LIMIT 1", (subject,))
    return dict(rows[0]) if rows else None


def _repair_event(tx: Transaction, subject: Mapping[str, Any], state: dict[str, Any], live: bool,
                  failure: Mapping[str, Any], attempt_id: str | None = None) -> EventProjection:
    applicable = (subject["generation"] == failure["generation"]
                  and subject["repair_source"] in {"acceptance", "integration"}
                  and subject["last_failure"] == failure["material_id"] and not tx.rows(
                      "SELECT position FROM history WHERE subject=? AND generation=? AND kind='acceptance_promoted' AND position>? LIMIT 1",
                      (subject["id"], failure["generation"], failure["position"])))
    return EventProjection(event_envelope(state, "acceptance_repair", state["run_id"],
                          event_copy(state, "acceptance_repair_active" if live else "acceptance_repair_planned"),
                          "orange", live=live, work_subject=subject["id"], generation=failure["generation"],
                          source_position=failure["position"], source_material_id=failure["material_id"],
                          failed_review_attempt=failure["identity"], attempt_id=attempt_id or failure["identity"]), applicable)


def _consumed(tx: Transaction, row: Mapping[str, Any], subject: Mapping[str, Any],
              state: dict[str, Any], mode: str) -> list[EventProjection]:
    attempt = dict(tx.one("SELECT * FROM attempts WHERE id=? AND subject=? AND generation=? AND result_id=?",
                          (row["identity"], row["subject"], row["generation"], row["material_id"])))
    if subject["kind"] == "final_run":
        failed = _first_repair(tx, row["subject"])
        if failed is not None and failed["position"] == row["position"]:
            return [_repair_event(tx, subject, state, False, failed)]
    if mode != "detailed":
        return []
    role = attempt["role"]
    # A formal promotion is a separate committed fact. Its milestone replaces
    # this same Review result when both are visible in the original snapshot.
    promoted = role == "review" and bool(tx.rows("SELECT position FROM history WHERE subject=? AND generation=? "
                                                "AND kind='acceptance_promoted' AND identity=? LIMIT 1",
                                                (row["subject"], row["generation"], attempt["id"])))
    return _result(tx, row, subject, state, attempt, human=False, visible=not promoted)


def _human(tx: Transaction, row: Mapping[str, Any], subject: Mapping[str, Any],
           state: dict[str, Any], mode: str) -> list[EventProjection]:
    if mode != "detailed" or row["identity"] != "human_blocker" or row["material_id"] is None:
        return []
    # Resume can clear result_id and replace current_invocation. The original
    # allocation preceding this gate binds its own validated material forever.
    attempt = dict(tx.one("SELECT a.* FROM history h JOIN invocations i ON i.id=h.identity "
                          "JOIN attempts a ON a.id=i.attempt JOIN invocation_outputs o ON o.invocation=i.id "
                          "WHERE h.subject=? AND h.generation=? AND a.subject=h.subject AND a.generation=h.generation "
                          "AND o.raw_id=? AND h.kind='agent_allocated' AND h.position<? ORDER BY h.position DESC LIMIT 1",
                          (row["subject"], row["generation"], row["material_id"], row["position"])))
    return _result(tx, row, subject, state, attempt, human=True)


def _result(tx: Transaction, row: Mapping[str, Any], subject: Mapping[str, Any],
            state: dict[str, Any], attempt: Mapping[str, Any], *, human: bool, visible: bool = True) -> list[EventProjection]:
    role = attempt["role"]
    result = json.loads(tx.get(row["material_id"], row["subject"]))
    facts = _subject_facts(tx, subject, attempt["revision"], state)
    checks = {key: lane["status"] for key, lane in result.get("checks", {}).items()}
    findings = [finding for lane in result.get("checks", {}).values() for finding in lane["findings"]]
    outcome = {"development": "candidate", "review": "acceptance_artifact", "publication": "publication_artifact"}[role]
    status_code = "review_failed" if role == "review" and attempt["outcome"] == "fail" else outcome
    if human:
        outcome = "blocked" if role == "review" else "human_blocker"
        status_code = "review_blocked" if role == "review" else "human_blocker"
    summary = (result["summary"] if role == "development" else result["pr_title"] if role == "publication"
               else event_copy(state, "separator").join(dict.fromkeys(lane["evidence"] for lane in result["checks"].values())))
    if human:
        summary = event_copy(state, "separator").join(
            lane["evidence"] for lane in result["checks"].values() if lane["status"] == "blocked"
        ) if role == "review" else event_copy(state, "separator").join(result["human_blockers"])
    if findings:
        reasons = summary if human else ""
        summary = event_copy(state, "findings", count=len(findings)) + event_copy(state, "separator").join(findings)
        if reasons:
            summary += event_copy(state, "blocked_reasons") + reasons
    title = (event_copy(state, "development_end") if role == "development"
             else event_copy(state, "publication_ticket_end" if facts else "publication_end") if role == "publication"
             else event_copy(state, "repair_title") if status_code == "review_failed"
             else event_copy(state, "review_ticket_end" if facts else "review_result_pending"))
    if human:
        title = event_copy(state, "role_result", role=event_copy(state, "role_" + role),
                           result=text("guidance.status.review_blocked" if role == "review" else "guidance.status.ready_for_human",
                                       language=selected_language(state)))
    effort = execution_facts(tx, row, attempt_id=attempt["id"])
    duration = effort.pop("execution_seconds", None)
    return [EventProjection(event_envelope(state, "stage_end", [attempt["id"], outcome, False, checks], title,
                           "yellow" if human else "orange" if status_code == "review_failed" else "green",
                           phase=event_copy(state, "role_" + role), role=role, round=attempt["ordinal"],
                           attempt_id=attempt["id"], work_subject=row["subject"], generation=row["generation"],
                           checks=checks, status_code=status_code, summary=summary, findings_count=len(findings),
                           next_step=event_copy(state, "human_next") if human else event_copy(state, "repair_next") if status_code == "review_failed" else "",
                           duration_seconds=duration, **effort, **facts), None if human else False, visible)]


def _project(tx: Transaction, row: Mapping[str, Any], state: dict[str, Any], mode: str) -> list[EventProjection]:
    """Project this original history record without changing its source facts."""
    if row["kind"] not in {"worker_started", "role_consumed", "paused", "effect_consumed", "acceptance_promoted", "boundary_update"}:
        return []
    subject = dict(tx.one("SELECT * FROM subjects WHERE id=?", (row["subject"],)))
    if row["kind"] == "boundary_update":
        previous = tx.rows("SELECT position,phase FROM history WHERE subject=? AND generation=? AND position<? "
                           "AND phase IS NOT NULL ORDER BY position DESC LIMIT 1",
                           (row["subject"], row["generation"], row["position"]))
        if subject["kind"] == "ticket" or not previous or previous[0]["phase"] != "approval":
            return []
        latest = tx.one("SELECT max(position) FROM history WHERE subject=? AND generation=? AND kind='boundary_update'",
                        (row["subject"], row["generation"]))[0]
        applicable = subject["generation"] == row["generation"] and latest == row["position"] and subject["phase"] == row["phase"]
        return [EventProjection(event_envelope(state, "approval_update", [row["subject"], row["generation"], row["position"]],
                               event_copy(state, "approval_update"), "orange",
                               summary=event_copy(state, "approval_update_summary"),
                               previous_approval_position=previous[0]["position"]), applicable)]
    if row["kind"] == "worker_started":
        return _started(tx, row, subject, state, mode)
    if row["kind"] == "role_consumed":
        return _consumed(tx, row, subject, state, mode)
    if row["kind"] == "paused":
        return _human(tx, row, subject, state, mode)
    if row["kind"] == "acceptance_promoted":
        promotion = json.loads(tx.get(row["material_id"], row["subject"]))
        review = tx.one("SELECT * FROM attempts WHERE id=? AND subject=? AND generation=? AND role='review'",
                        (row["identity"], row["subject"], row["generation"]))
        if (subject["kind"] == "ticket" or promotion["review_attempt"] != review["id"]
                or review["outcome"] != "pass" or review["result_id"] is None):
            raise ValueError("formal acceptance notification has no original Review")
        identity = [row["generation"], promotion["promoted_sha"], review["base_sha"],
                    review["expected_tree"], review["revision"], review["graph_revision"]]
        effort = execution_facts(tx, row, attempt_id=review["id"])
        duration = effort.pop("execution_seconds", None)
        applicable = (subject["generation"] == row["generation"] and subject["review_attempt"] == review["id"]
                      and subject["integrated_sha"] == promotion["promoted_sha"] and subject["integrated_tree"] == promotion["promoted_tree"])
        return [EventProjection(event_envelope(state, "acceptance_passed", identity,
                               event_copy(state, "parent_acceptance_passed" if subject["kind"] == "parent_only" else "acceptance_passed"),
                               "green", summary=event_copy(state, "acceptance_passed_summary"),
                               attempt_id=review["id"], round=review["ordinal"],
                               phase=event_copy(state, "role_review"), duration_seconds=duration, **effort), applicable)]
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
            return [EventProjection(event_envelope(state, "pr_created", number, event_copy(state, "pr_created", number=number), "green",
                                   summary=event_copy(state, "pr_summary"),
                                   url=f"https://github.com/{pr['repository']}/pull/{number}"), False)]
    if subject["kind"] == "ticket" and tx.rows("SELECT generation FROM completions WHERE subject=? AND generation=? AND close_effect=?",
                                             (row["subject"], row["generation"], row["identity"])):
        facts = _subject_facts(tx, subject, subject["revision"], state)
        effort = execution_facts(tx, row)
        total = effort.pop("execution_seconds", None)
        return [EventProjection(event_envelope(state, "ticket_completed", [str(subject["issue"]), row["generation"]],
                               event_copy(state, "ticket_completed", title=facts["task_title"]), "green",
                               total_seconds=total, **effort, **facts), False)]
    return []


def _resumed(tx: Transaction, row: Mapping[str, Any], state: dict[str, Any],
             ordinary: list[EventProjection]) -> list[EventProjection]:
    facts = resume_facts(tx, row)
    if not facts:
        return ordinary
    if facts["resume_outcome"] != "started":
        return []  # The exact existing Gate owns the failure/human card and these facts.
    if ordinary and ordinary[0].content["kind"] not in {"stage_start", "acceptance_repair"}:
        content = ordinary[0].content
        content.update(facts)
        content["summary"] = event_copy(state, "resume_started") + event_copy(state, "separator") + content.get("summary", "")
        return ordinary
    summary = "resume_worker_started_summary" if row["kind"] == "worker_started" else "resume_controller_check_summary" if row["kind"] == "checks_observed" else "resume_controller_progress_summary"
    applicable = _live_attempt(tx, row["subject"], row["generation"], facts["attempt_id"]) if row["kind"] == "worker_started" else False
    return [EventProjection(event_envelope(state, "resume", facts["resume_id"], event_copy(state, "resume_started"),
                           "blue", summary=event_copy(state, summary), **facts), applicable)]


def project_event(tx: Transaction, row: Mapping[str, Any], state: dict[str, Any], mode: str) -> list[EventProjection]:
    """Project one visible event and keep suppressed original facts for rechecks."""
    ordinary = _project(tx, row, state, mode)
    result = _resumed(tx, row, state, [item for item in ordinary if item.visible])
    result.extend(item for item in ordinary if not item.visible)
    for projected in result:
        event = projected.content
        event.setdefault("source_position", row["position"])
        event.setdefault("source_material_id", row["material_id"])
        event.setdefault("work_subject", row["subject"])
        event.setdefault("generation", row["generation"])
    return result


def host_exit_event(database: Path, notice: Mapping[str, Any]) -> dict[str, Any] | None:
    """Use the original notification envelope and stable Host exit identity."""
    from agent_run.core.kernel import Kernel
    from agent_run.queries.host_exit import host_exit_current

    if not host_exit_current(database, notice):
        return None
    with Kernel(database).storage.transaction(write=False) as tx:
        run = tx.one("SELECT id,repository,parent_issue,parent_revision,language,created_at FROM runs")
        title = tx.rows("SELECT title FROM requirement_versions WHERE issue=? AND revision=?",
                        (run["parent_issue"], run["parent_revision"]))
        state = {"run_id": run["id"], "repository": run["repository"], "language": run["language"],
                 "created_at": run["created_at"],
                 "parent": {"number": run["parent_issue"], "title": title[0][0] if title else ""}}
    return event_envelope(state, "executor_exit", [notice["action_id"], notice["epoch"], notice["nonce"], notice["runner"]],
                          event_copy(state, "executor_exit"), "red", important=True,
                          generation=notice["epoch"], exit_evidence=dict(notice),
                          summary=event_copy(state, "executor_exit_summary"),
                          next_step=event_copy(state, "execution_next"))
