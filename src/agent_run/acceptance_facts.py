"""Read-only candidate-bound acceptance evidence shared by delivery views."""
from __future__ import annotations

from typing import Any

from agent_run.delivery_budget import delivery_budget_owner
from agent_run.delivery_facts import current_work_subject

def current_findings(state: dict[str, Any]) -> list[str]:
    current = current_work_subject(state)
    if current is None:
        return []
    subject = acceptance_subject(state, current)
    artifact = current_artifact(subject, budget_owner=delivery_budget_owner(state, subject))
    if artifact is not None:
        return artifact_findings(artifact)
    if subject.get("phase") in {
        "developing",
        "repairing",
        "committing_candidate",
        "candidate",
        "reviewing",
    }:
        unresolved = unresolved_acceptance_artifact(state, current, subject)
        if unresolved is not None:
            return artifact_findings(unresolved)
    return []


def unresolved_acceptance_artifact(
    state: dict[str, Any],
    current: tuple[str, dict[str, Any]],
    subject: dict[str, Any],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = [subject]
    if current[0] in {"run_acceptance", "run_repair"}:
        acceptance = state.get("run_acceptance")
        repair = acceptance.get("repair_job") if isinstance(acceptance, dict) else None
        if isinstance(repair, dict) and repair is not subject:
            candidates.append(repair)
        if isinstance(acceptance, dict) and acceptance is not subject:
            candidates.append(acceptance)
    for candidate in candidates:
        if candidate.get("repair_source") != "acceptance":
            continue
        unresolved = candidate.get("unresolved_acceptance_artifact")
        if isinstance(unresolved, dict):
            return unresolved
        record = candidate.get("acceptance_record")
        if not isinstance(record, dict):
            continue
        artifact = record.get("artifact")
        reviewed = record.get("reviewed_candidate_sha")
        if reviewed is None:
            reviewed = record.get("reviewed_head_sha")
        candidate_sha = subject_candidate_identity(candidate)
        if (
            isinstance(artifact, dict)
            and isinstance(reviewed, str)
            and isinstance(candidate_sha, str)
            and reviewed != candidate_sha
            and artifact_has_failures(artifact)
        ):
            return artifact
    return None


def artifact_has_failures(artifact: dict[str, Any]) -> bool:
    checks = artifact.get("checks")
    if not isinstance(checks, dict):
        return False
    return bool(artifact_findings(artifact)) or any(
        isinstance(check, dict) and check.get("status") == "fail"
        for check in checks.values()
    )


def current_artifact(
    subject: dict[str, Any], *, budget_owner: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return only an artifact proven to belong to the current candidate."""

    candidate = subject_candidate_identity(subject)
    if not isinstance(candidate, str) or not candidate:
        return None
    record = subject.get("acceptance_record")
    if isinstance(record, dict):
        reviewed = record.get("reviewed_candidate_sha")
        if reviewed is None:
            reviewed = record.get("reviewed_head_sha")
        artifact = record.get("artifact")
        if isinstance(artifact, dict) and reviewed == candidate:
            return artifact
        return None

    budget = (subject if budget_owner is None else budget_owner).get("review_budget")
    artifacts = budget.get("review_artifacts") if isinstance(budget, dict) else None
    if isinstance(artifacts, list):
        for review in reversed(artifacts):
            if not isinstance(review, dict):
                continue
            review_candidate = review.get("candidate_sha")
            if review_candidate is None:
                identity = review.get("review_identity")
                if isinstance(identity, dict):
                    review_candidate = identity.get("run_head_sha")
            if review_candidate != candidate:
                continue
            artifact = review.get("artifact")
            if isinstance(artifact, dict):
                return artifact

    return None


def current_acceptance_artifact(state: dict[str, Any]) -> dict[str, Any] | None:
    """Read the verdict bound to the current work and candidate, without mutation."""
    current = current_work_subject(state)
    if current is None:
        return completed_acceptance_artifact(state)
    subject = acceptance_subject(state, current)
    return current_artifact(subject, budget_owner=delivery_budget_owner(state, subject))


def completed_acceptance_artifact(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return a terminal acceptance artifact only when its candidate matches."""

    for key in ("parent_job", "active_ticket_job", "run_acceptance"):
        subject = state.get(key)
        if not isinstance(subject, dict) or subject.get("phase") not in {
            "accepted",
            "completed",
            "merged",
        }:
            continue
        artifact = current_artifact(subject, budget_owner=delivery_budget_owner(state, subject))
        if not isinstance(artifact, dict):
            continue
        checks = artifact.get("checks")
        if not isinstance(checks, dict):
            continue
        statuses = {
            check.get("status")
            for check in checks.values()
            if isinstance(check, dict)
        }
        if statuses == {"pass"}:
            return artifact
    return None


def acceptance_subject(
    state: dict[str, Any], current: tuple[str, dict[str, Any]]
) -> dict[str, Any]:
    """Use Run Acceptance evidence while publication is waiting on its gate."""

    location, subject = current
    if location == "run_publication":
        acceptance = state.get("run_acceptance")
        publication_head = publication_boundary_head(subject) or invocation_publication_head(state, subject)
        acceptance_head = (
            acceptance_publication_boundary_head(acceptance)
            if isinstance(acceptance, dict)
            else None
        )
        if (
            isinstance(acceptance, dict)
            and isinstance(publication_head, str)
            and publication_head == acceptance_head
        ):
            return acceptance
        return {}
    return subject


def invocation_publication_head(state: dict[str, Any], subject: dict[str, Any]) -> object:
    """Resolve the real invocation boundary, authenticated by its semantic attempt."""
    from agent_run.agent_invocation import canonical_fingerprint

    attempt = subject.get("pending_semantic_attempt")
    invocation = state.get("active_agent_invocation")
    if not isinstance(invocation, dict):
        history = state.get("agent_invocation_history")
        invocation = next((item for item in reversed(history) if isinstance(item, dict)
                           and isinstance(item.get("semantic_attempt"), dict)
                           and isinstance(attempt, dict)
                           and item["semantic_attempt"].get("attempt_id") == attempt.get("attempt_id")), None) if isinstance(history, list) else None
    if not isinstance(attempt, dict) or not isinstance(invocation, dict):
        return None
    identity = invocation.get("semantic_attempt")
    boundary = invocation.get("currentness_boundary")
    if (
        not isinstance(identity, dict) or not isinstance(boundary, dict)
        or not attempt.get("attempt_id")
        or identity.get("attempt_id") != attempt.get("attempt_id")
        or invocation.get("generation") != attempt.get("generation")
        or identity.get("generation") != attempt.get("generation")
        or invocation.get("work_subject") != f"run-publication:{state.get('run_id')}"
        or canonical_fingerprint(boundary) != attempt.get("currentness_boundary_fingerprint")
    ):
        return None
    for key, owner_key in (("parent_revision", "parent"), ("ticket_graph_revision", "ticket_graph")):
        owner = state.get(owner_key)
        if isinstance(owner, dict) and boundary.get(key) != owner.get("revision"):
            return None
    acceptance = state.get("run_acceptance")
    if not isinstance(acceptance, dict) or attempt.get("generation") != acceptance.get("acceptance_generation"):
        return None
    record = acceptance.get("acceptance_record")
    if not isinstance(record, dict):
        return None
    for key in ("reviewed_default_base_sha", "expected_merge_tree", "parent_revision", "ticket_graph_revision", "ticket_completion_records_fingerprint"):
        if key in record and boundary.get(key) != record[key]:
            return None
    return boundary.get("reviewed_head_sha")


def acceptance_publication_boundary_head(
    acceptance: dict[str, Any],
) -> object:
    """Return the explicitly promoted Run head, when its proof is intact."""

    record = acceptance.get("acceptance_record")
    if not isinstance(record, dict) or record.get("acceptance_state") != "integrated":
        return subject_candidate_identity(acceptance)

    candidate = acceptance.get("candidate_sha")
    reviewed_candidate = record.get("reviewed_candidate_sha")
    reviewed_head = record.get("reviewed_head_sha")
    if (
        not isinstance(candidate, str)
        or not candidate
        or not isinstance(reviewed_candidate, str)
        or reviewed_candidate != candidate
        or not isinstance(reviewed_head, str)
        or not reviewed_head
    ):
        return None

    for key in ("reviewed_head_sha", "integrated_sha"):
        value = acceptance.get(key)
        if value is not None and value != reviewed_head:
            return None
    record_run_head = record.get("run_head_sha")
    if record_run_head is not None and record_run_head != reviewed_head:
        return None
    return reviewed_head


def publication_boundary_head(subject: dict[str, Any]) -> object:
    record = subject.get("record")
    if isinstance(record, dict) and isinstance(record.get("run_head_sha"), str):
        return record["run_head_sha"]
    for key in ("head_sha", "run_head_sha"):
        value = subject.get(key)
        if isinstance(value, str) and value:
            return value
    pending = subject.get("pending_semantic_attempt")
    history = subject.get("semantic_attempt_history")
    attempts: list[object] = []
    if isinstance(pending, dict):
        attempts.append(pending)
    if isinstance(history, list):
        attempts.extend(reversed(history))
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("role") != "publication":
            continue
        boundary = attempt.get("currentness_boundary")
        if not isinstance(boundary, dict):
            continue
        for key in ("reviewed_head_sha", "run_head_sha"):
            value = boundary.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def subject_candidate_identity(subject: dict[str, Any]) -> object:
    """Read the current version key used by each delivery subject shape."""

    for key in ("candidate_sha", "reviewed_head_sha", "head_sha", "run_head_sha"):
        value = subject.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def has_prior_acceptance_failure(
    subject: dict[str, Any], *, budget_owner: dict[str, Any] | None = None,
) -> bool:
    if isinstance(subject.get("unresolved_acceptance_artifact"), dict):
        return True
    record = subject.get("acceptance_record")
    if isinstance(record, dict):
        artifact = record.get("artifact")
        reviewed = record.get("reviewed_candidate_sha")
        if reviewed is None:
            reviewed = record.get("reviewed_head_sha")
        candidate = subject_candidate_identity(subject)
        if (
            isinstance(artifact, dict)
            and isinstance(reviewed, str)
            and isinstance(candidate, str)
            and reviewed != candidate
            and artifact_has_failures(artifact)
        ):
            return True
    budget = (subject if budget_owner is None else budget_owner).get("review_budget")
    artifacts = budget.get("review_artifacts") if isinstance(budget, dict) else None
    if not isinstance(artifacts, list):
        return False
    for review in artifacts:
        if not isinstance(review, dict):
            continue
        artifact = review.get("artifact")
        if not isinstance(artifact, dict):
            continue
        checks = artifact.get("checks")
        if not isinstance(checks, dict):
            continue
        if any(
            isinstance(check, dict)
            and (check.get("status") == "fail" or artifact_findings(artifact))
            for check in checks.values()
        ):
            return True
    return False


def artifact_findings(artifact: dict[str, Any]) -> list[str]:
    checks = artifact.get("checks")
    if not isinstance(checks, dict):
        return []
    return [
        finding
        for lane in ("e2e", "standards", "spec")
        if isinstance((check := checks.get(lane)), dict)
        and isinstance((lane_findings := check.get("findings")), list)
        for finding in lane_findings
        if isinstance(finding, str)
    ]




def acceptance_lanes(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Select only operator-visible lane fields; never expose nested audit data."""
    checks = artifact.get("checks")
    if not isinstance(checks, dict):
        return {}
    lanes = {}
    for lane in ("e2e", "standards", "spec"):
        check = checks.get(lane)
        if not isinstance(check, dict):
            continue
        findings = check.get("findings")
        lanes[lane] = {
            "status": check.get("status"),
            "evidence": check.get("evidence"),
            "findings": [value for value in findings if isinstance(value, str)]
            if isinstance(findings, list) else [],
        }
    return lanes
