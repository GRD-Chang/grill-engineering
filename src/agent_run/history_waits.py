from __future__ import annotations

from copy import deepcopy
from typing import Any


_ROUTINE_CHECK_WRITES = {"ensure_final_run_ref", "refresh_final_pr_narrative"}


def collapse_check_waits(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact only consecutive observations with proven check/run identity."""

    result: list[dict[str, Any]] = []
    known_evidence = {
        event["required_checks_signature"]: event["required_checks_evidence"]
        for event in events
        if isinstance(event.get("required_checks_signature"), str)
        and event.get("required_checks_evidence") is not None
    }
    for raw in _approval_check_observations(
        _with_recorded_approvals(_without_routine_write_bridges(events))
    ):
        event = deepcopy(raw)
        signature = event.get("required_checks_signature")
        if isinstance(signature, str):
            if event.get("required_checks_evidence") is not None:
                known_evidence[signature] = event["required_checks_evidence"]
            elif signature in known_evidence:
                event["required_checks_evidence"] = known_evidence[signature]
        prior = result[-1] if result else None
        if prior is not None and (
            _same_check_wait(prior, event) or _same_approval_wait(prior, event)
        ):
            start = prior.get("wait_started_at", prior.get("at"))
            if prior.get("required_checks_signature") != event.get("required_checks_signature"):
                prior.pop("required_checks_evidence", None)
            evidence = prior.get("required_checks_evidence")
            prior.update(event)
            prior["wait_started_at"] = start
            prior["wait_observed_until"] = (
                event.get("approval_granted_at") or event.get("at")
                if event.get("kind") == "approval" else event.get("at")
            )
            if "required_checks_evidence" not in event and evidence is not None:
                prior["required_checks_evidence"] = evidence
        else:
            if event.get("kind") in {"required_checks", "approval"}:
                event["wait_started_at"] = event.get("at")
                event["wait_observed_until"] = event.get("at")
            result.append(event)
    return result


def _same_check_wait(prior: dict[str, Any], event: dict[str, Any]) -> bool:
    if prior.get("kind") != "required_checks" or event.get("kind") != "required_checks":
        return False
    if any(
        item.get("required_checks_observation_status") in {"unavailable", "unknown"}
        for item in (prior, event)
    ):
        return False
    if not _same_check_identity(prior, event):
        return False
    if not prior.get("checks_wait_identity"):
        return False
    old_result = prior.get("required_checks_result")
    new_result = event.get("required_checks_result")
    closing_without_window = (
        event.get("checks_wait_identity") is None
        and old_result == "pending" and new_result in {"pass", "none"}
    )
    if (
        prior.get("checks_wait_identity") != event.get("checks_wait_identity")
        and not closing_without_window
    ):
        return False
    if old_result == "pending" and new_result in {"pass", "none"}:
        return True
    return (
        old_result == new_result
        and old_result in {"pending", "pass", "none"}
        and prior.get("required_checks_signature") is not None
        and prior.get("required_checks_signature") == event.get("required_checks_signature")
    )


def _same_approval_wait(prior: dict[str, Any], event: dict[str, Any]) -> bool:
    if prior.get("kind") != "approval" or event.get("kind") != "approval":
        return False
    if not prior.get("pr_number") or not _same_subject_scope(prior, event):
        return False
    prior_head = prior.get("required_checks_head_sha") or prior.get("commit_sha")
    event_head = event.get("required_checks_head_sha") or event.get("commit_sha")
    if not prior_head or prior_head != event_head:
        return False
    for key in ("object", "pr_number"):
        if prior.get(key) != event.get(key):
            return False
    grant = prior.get("approval_granted_at")
    return grant is None or grant == event.get("approval_granted_at")


def _approval_check_observations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A checks pass may enter approval in the same durable snapshot."""

    result: list[dict[str, Any]] = []
    last_observation: tuple[object, ...] | None = None
    for event in events:
        identity = tuple(
            event.get(key) for key in (
                "object", "pr_number", "required_checks_head_sha", "required_checks_signature",
                "required_checks_run_identity", "checks_wait_identity",
                "history_work_subject", "history_generation", "history_budget_window",
            )
        )
        if event.get("kind") == "required_checks":
            last_observation = identity
        elif event.get("kind") == "approval" and event.get("required_checks_result") is not None:
            if not event.get("required_checks_signature") or identity != last_observation:
                check = dict(event, kind="required_checks")
                check.pop("approval_granted_at", None)
                result.append(check)
                last_observation = identity
        else:
            last_observation = None
        result.append(event)
    return result


def _without_routine_write_bridges(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Routine confirmed writes between checks are not a new external outage.

    Keep interrupted/unconfirmed intents, other writes, and every real error.
    The surrounding exact check identity must prove the operation completed.
    """

    result: list[dict[str, Any]] = []
    index = 0
    while index < len(events):
        end = index
        while end < len(events) and _routine_write_intent(events[end]):
            end += 1
        prior = result[-1] if result else None
        next_event = events[end] if end < len(events) else None
        if (
            end > index and prior is not None and next_event is not None
            and prior.get("kind") in {"required_checks", "approval"}
            and next_event.get("kind") in {"required_checks", "approval"}
            and any(
                item.get("github_write_action") in _ROUTINE_CHECK_WRITES
                for item in events[index:end]
            )
            and all(_same_check_identity(prior, item) for item in events[index:end + 1])
        ):
            index = end
            continue
        if end > index:
            result.extend(events[index:end])
            index = end
        else:
            result.append(events[index])
            index += 1
    return result


def _routine_write_intent(event: dict[str, Any]) -> bool:
    reason = event.get("external_wait_reason")
    return (
        event.get("kind") == "supervision"
        and event.get("status") == "waiting_external"
        and isinstance(reason, dict) and reason.get("code") == "github_write_pending"
        and event.get("github_write_action") in {None, *_ROUTINE_CHECK_WRITES}
        and event.get("required_checks_observation_status") not in {"unavailable", "unknown"}
    )


def _same_check_identity(prior: dict[str, Any], event: dict[str, Any]) -> bool:
    return _same_subject_scope(prior, event) and all(
        prior.get(key) is not None and prior.get(key) == event.get(key)
        for key in (
            "object", "pr_number", "required_checks_head_sha", "required_checks_run_identity",
        )
    )


def _same_subject_scope(prior: dict[str, Any], event: dict[str, Any]) -> bool:
    return (
        isinstance(prior.get("history_work_subject"), str)
        and type(prior.get("history_generation")) is int
        and type(prior.get("history_budget_window")) is int
        and all(prior.get(key) == event.get(key) for key in (
            "history_work_subject", "history_generation", "history_budget_window",
        ))
    )


def _with_recorded_approvals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An approval can resume checks directly, without a ready-for-approval save."""

    result: list[dict[str, Any]] = []
    seen_grants: set[tuple[object, ...]] = set()
    for event in events:
        granted_at = event.get("approval_granted_at")
        if isinstance(granted_at, str):
            identity = tuple(event.get(key) for key in (
                "history_work_subject", "history_generation", "history_budget_window",
                "object", "pr_number", "approval_granted_at",
            ))
            if identity not in seen_grants and event.get("kind") != "approval":
                approval = dict(event, kind="approval", at=granted_at)
                # These are the resumed check's facts; its observation follows
                # the approval and must remain on its own lifecycle node.
                for key in (
                    "required_checks_result", "required_checks_evidence",
                    "required_checks_observed_at", "required_checks_signature",
                ):
                    approval.pop(key, None)
                result.append(approval)
            seen_grants.add(identity)
        result.append(event)
    return result
