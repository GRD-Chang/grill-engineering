from __future__ import annotations

import hashlib
import json
import re
from typing import Any


CHECKS_HISTORY_FIELDS = (
    "history_work_subject",
    "history_generation",
    "history_budget_window",
    "required_checks_head_sha",
    "required_checks_run_identity",
    "required_checks_signature",
    "checks_wait_identity",
    "required_checks_observation_status",
    "external_wait_reason",
    "github_write_action",
)
# A generic context URL does not prove which check execution was observed.
_CHECK_EXECUTION_LINK = re.compile(
    r"/(?:job/\d+|jobs/\d+|check-runs/\d+|runs/\d+/attempts/\d+)(?:[/?#]|$)"
)
_MAX_HISTORY_CHECKS = 32
_MAX_HISTORY_CHECK_BYTES = 4096


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def checks_timeline_facts(
    state: dict[str, Any], marker: dict[str, object],
) -> dict[str, Any]:
    """Retain existing identity/evidence; never backfill history during reads."""

    if marker.get("kind") == "run_publication":
        subject = state.get("run_publication")
    elif marker.get("kind") == "run_acceptance":
        acceptance = state.get("run_acceptance")
        subject = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
    else:
        subject = state.get("active_ticket_job") or state.get("parent_job")
    facts: dict[str, Any] = {}
    if not isinstance(subject, dict):
        return facts
    if type(marker.get("pr_number")) is int:
        facts.update(_subject_scope(state, subject, marker))
    observation = subject.get("required_checks_evidence")
    if (
        isinstance(observation, dict)
        and observation.get("pr_number") == marker.get("pr_number")
    ):
        facts.update(_observation_facts(observation))
        facts["required_checks_observed_at"] = subject.get("required_checks_observed_at")
    status = subject.get("required_checks_observation_status")
    if status is not None:
        facts["required_checks_observation_status"] = status
    window = state.get("supervision_window")
    if not isinstance(window, dict):
        window = state.get("supervision_wait")
    if (
        isinstance(window, dict)
        and window.get("identity") is not None
        and window.get("started_at") is not None
    ):
        facts["checks_wait_identity"] = _digest([window["identity"], window["started_at"]])
    intent = subject.get("write_intent")
    if isinstance(intent, dict):
        facts["github_write_action"] = intent.get("action")
    if state.get("status") in {"waiting_external", "supervision_timeout"}:
        diagnostics = state.get("diagnostics")
        if isinstance(diagnostics, list) and diagnostics and isinstance(diagnostics[0], dict):
            facts["external_wait_reason"] = {
                key: _bounded_text(diagnostics[0][key])
                for key in ("code", "message") if key in diagnostics[0]
            }
    return facts


def _observation_facts(observation: dict[str, Any]) -> dict[str, Any]:
    checks = observation.get("checks")
    if not isinstance(checks, list) or not all(isinstance(check, dict) for check in checks):
        return {}
    ordered_checks = sorted(checks, key=lambda check: json.dumps(check, sort_keys=True))
    facts: dict[str, Any] = {
        "required_checks_head_sha": observation.get("head_sha"),
        "required_checks_result": observation.get("result"),
        "required_checks_signature": _digest({**observation, "checks": ordered_checks}),
        "required_checks_evidence": _bounded_check_details(ordered_checks),
    }
    if checks and all(
        _CHECK_EXECUTION_LINK.search(str(check.get("link") or "")) for check in checks
    ):
        identities = sorted(
            (str(check.get("workflow") or ""), str(check.get("name") or ""), str(check["link"]))
            for check in checks
        )
        facts["required_checks_run_identity"] = _digest(identities)
    return facts


def _subject_scope(
    state: dict[str, Any], subject: dict[str, Any], marker: dict[str, object],
) -> dict[str, Any]:
    run_id = state.get("run_id")
    if not isinstance(run_id, str):
        return {}
    budget_owner = subject
    if marker.get("kind") == "run_publication":
        acceptance = state.get("run_acceptance")
        budget_owner = acceptance if isinstance(acceptance, dict) else {}
        work_subject = f"run-publication:{run_id}"
        generation = budget_owner.get("acceptance_generation")
    elif marker.get("kind") == "run_acceptance":
        work_subject = f"run-repair:{run_id}"
        generation = subject.get("repair_generation")
    elif type(subject.get("ticket_number")) is int:
        work_subject = f"ticket:{subject['ticket_number']}"
        generation = subject.get("ticket_branch_generation")
    else:
        work_subject = f"parent-only:{run_id}"
        generation = subject.get("parent_generation")
    budget = budget_owner.get("review_budget")
    return {
        "history_work_subject": work_subject,
        "history_generation": generation,
        "history_budget_window": budget.get("window") if isinstance(budget, dict) else None,
    }


def _bounded_check_details(checks: list[dict[str, Any]]) -> dict[str, Any]:
    details: list[dict[str, str]] = []
    remaining = _MAX_HISTORY_CHECK_BYTES
    for check in checks[:_MAX_HISTORY_CHECKS]:
        compact = {
            key: _bounded_text(check[key], limit=512 if key in {"link", "description"} else 128)
            for key in ("name", "workflow", "link", "bucket", "state", "description")
            if check.get(key) is not None
        }
        size = len(json.dumps(compact, ensure_ascii=False).encode()) + 2
        if size > remaining:
            break
        details.append(compact)
        remaining -= size
    return {"checks": details, "omitted_checks": len(checks) - len(details)}


def _bounded_text(value: object, *, limit: int = 512) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit - 8] + "…（已截断）"
