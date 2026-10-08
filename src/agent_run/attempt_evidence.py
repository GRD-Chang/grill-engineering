"""Associate retained Attempt evidence with its immutable identity and budget."""
from __future__ import annotations

import json
from typing import Any

from agent_run.review_budget import review_budget_owner
from agent_run.delivery_facts import role_family as _role_family

def _budget_facts_for_attempt(
    state: dict[str, Any], attempt: dict[str, Any]
) -> dict[str, Any]:
    """Resolve the immutable budget projection for this Attempt's window."""

    role = _role_family(str(attempt.get("role") or ""))
    window = attempt.get("budget_window")
    if role == "publication":
        return {"window": window}
    facts: dict[str, Any] = {
        "window": window,
        "development_attempts": None,
        "reviewer_invocations": None,
    }
    if not isinstance(window, int):
        return facts
    subject = str(attempt.get("work_subject") or "")
    policy_owner: tuple[str, dict[str, Any]] | None = None
    historical_policy_owner: tuple[str, dict[str, Any]] | None = None
    for owner_subject, owner in _subject_variants(state):
        if owner_subject != subject:
            continue
        if policy_owner is None:
            policy_owner = (owner_subject, owner)
        history = review_budget_owner(owner).get("review_budget_history")
        if not isinstance(history, list):
            continue
        for snapshot in history:
            if not isinstance(snapshot, dict):
                continue
            budget = snapshot.get("review_budget")
            if isinstance(budget, dict) and budget.get("window") == window:
                historical_policy_owner = (owner_subject, snapshot)
                break
        if historical_policy_owner is not None:
            break

    if historical_policy_owner is not None:
        policy_owner = historical_policy_owner

    stored_snapshot = attempt.get("budget_snapshot")
    if isinstance(stored_snapshot, dict) and "ci_developments_used" in stored_snapshot:
        return {
            key: stored_snapshot.get(key)
            for key in ("window", "ci_developments_used", "ci_development_limit", "ci_reviewer_invocations")
        }
    if isinstance(stored_snapshot, dict) and stored_snapshot.get("window") == window:
        facts.update(
            {
                "development_attempts": stored_snapshot.get("development_attempts"),
                "reviewer_invocations": stored_snapshot.get("reviewer_invocations"),
            }
        )
    if isinstance(stored_snapshot, dict) and "integration_development_used" in stored_snapshot:
        return {
            "window": window,
            "integration_development_used": stored_snapshot.get("integration_development_used"),
            "integration_development_limit": stored_snapshot.get("integration_development_limit"),
        }
    if policy_owner is None:
        return facts
    owner_subject, owner = policy_owner
    try:
        from agent_run.delivery_policy import (
            parent_only_budget_policy_for_job,
            run_repair_budget_policy_for_job,
            ticket_budget_policy_for_job,
        )

        if owner_subject.startswith("ticket:") or subject.startswith("ticket:"):
            policy = ticket_budget_policy_for_job(
                owner, state_snapshot=state.get("policy_snapshot")
            )
        elif owner_subject.startswith("run-") or subject.startswith("run-"):
            policy = run_repair_budget_policy_for_job(
                owner, state_snapshot=state.get("policy_snapshot")
            )
        else:
            policy = parent_only_budget_policy_for_job(
                owner, state_snapshot=state.get("policy_snapshot")
            )
        facts.update(
            {
                "development_limit": policy.development_limit,
                "reviewer_limit": policy.review_limit,
            }
        )
    except (TypeError, ValueError, KeyError):
        # A malformed or absent policy cannot justify an inferred limit.
        pass
    return facts

def owners_for_attempt(
    state: dict[str, Any], attempt: dict[str, Any]
) -> list[dict[str, Any]]:
    attempt_id = attempt.get("attempt_id")
    subject = str(attempt.get("work_subject") or "")
    generation = attempt.get("generation")
    owners: list[dict[str, Any]] = []
    for work_subject, owner in _subject_variants(state):
        if work_subject != subject:
            continue
        owner_attempts = _owner_attempts(owner)
        if any(
            isinstance(candidate, dict)
            and (
                candidate.get("attempt_id") == attempt_id
                if isinstance(attempt_id, str)
                else candidate.get("generation") == generation
                and candidate.get("role") == attempt.get("role")
                and candidate.get("ordinal") == attempt.get("ordinal")
            )
            for candidate in owner_attempts
        ):
            owners.append(owner)
    if owners:
        return owners
    same_subject = [
        owner
        for work_subject, owner in _subject_variants(state)
        if work_subject == subject
    ]
    if isinstance(attempt_id, str) and any(
        _owner_attempts(owner) for owner in same_subject
    ):
        # An identified Attempt which is absent from a retained owner history
        # cannot safely borrow the current Candidate's Artifact.
        return []
    return same_subject

def _subject_variants(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    run_id = state.get("run_id")
    values: list[tuple[str, dict[str, Any]]] = []
    active = state.get("active_ticket_job")
    if isinstance(active, dict) and isinstance(active.get("ticket_number"), int):
        values.append((f"ticket:{active['ticket_number']}", active))
    jobs = state.get("ticket_jobs")
    if isinstance(jobs, dict):
        for number, job in jobs.items():
            if isinstance(job, dict):
                values.append((f"ticket:{number}", job))
    parent = state.get("parent_job")
    if isinstance(parent, dict):
        values.append((f"parent-only:{run_id}", parent))
    acceptance = state.get("run_acceptance")
    if isinstance(acceptance, dict):
        values.append((f"run-acceptance:{run_id}", acceptance))
        repair = acceptance.get("repair_job")
        if isinstance(repair, dict):
            values.append((f"run-repair:{run_id}", repair))
    publication = state.get("run_publication")
    if isinstance(publication, dict):
        values.append((f"run-publication:{run_id}", publication))
    retired = state.get("retired_semantic_attempt_owners")
    if isinstance(retired, list):
        values.extend(
            (str(owner.get("work_subject")), owner)
            for owner in retired
            if isinstance(owner, dict) and isinstance(owner.get("work_subject"), str)
        )
    variants: list[tuple[str, dict[str, Any]]] = []
    for subject, owner in values:
        variants.append((subject, owner))
        history = review_budget_owner(owner).get("review_budget_history")
        if isinstance(history, list):
            variants.extend(
                (subject, snapshot)
                for snapshot in history
                if isinstance(snapshot, dict)
            )
    return variants

def _owner_attempts(owner: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    history = owner.get("semantic_attempt_history")
    if isinstance(history, list):
        values.extend(item for item in history if isinstance(item, dict))
    pending = owner.get("pending_semantic_attempt")
    if isinstance(pending, dict):
        values.append(pending)
    return values

def review_artifact_for_attempt(
    owners: list[dict[str, Any]],
    attempt: dict[str, Any],
    invocations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for owner in owners:
        snapshot = attempt.get("budget_snapshot")
        budget = (
            owner.get("ci_recovery")
            if isinstance(snapshot, dict) and "ci_developments_used" in snapshot
            else review_budget_owner(owner).get("review_budget")
        )
        if not isinstance(budget, dict):
            continue
        if budget.get("window") != attempt.get("budget_window"):
            continue
        artifacts = budget.get("review_artifacts")
        if isinstance(artifacts, list):
            for item in artifacts:
                if not isinstance(item, dict) or not isinstance(
                    item.get("artifact"), dict
                ):
                    continue
                identity = json.dumps(
                    {
                        "reviewer_thread_id": item.get("reviewer_thread_id"),
                        "candidate_sha": item.get("candidate_sha"),
                        "review_identity": item.get("review_identity"),
                        "artifact": item.get("artifact"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if identity in seen_candidates:
                    continue
                seen_candidates.add(identity)
                candidates.append(item)
    if not candidates:
        return None
    reviewer_ids = {
        value
        for invocation in invocations
        for key in ("reported_thread_id", "requested_thread_id", "thread_id")
        if isinstance((value := invocation.get(key)), str) and value
    }
    candidate_heads = {
        value
        for invocation in invocations
        for boundary in (invocation.get("currentness_boundary"),)
        if isinstance(boundary, dict)
        for key in (
            "candidate_sha",
            "reviewed_candidate_sha",
            "reviewed_head_sha",
            "run_head_sha",
            "head_sha",
            "repair_candidate_sha",
        )
        if isinstance((value := boundary.get(key)), str) and value
    }
    history_facts = attempt.get("history_facts")
    if isinstance(history_facts, dict):
        reviewer_thread_id = history_facts.get("reviewer_thread_id")
        if isinstance(reviewer_thread_id, str) and reviewer_thread_id:
            reviewer_ids.add(reviewer_thread_id)
        candidate_sha = history_facts.get("candidate_sha")
        if isinstance(candidate_sha, str) and candidate_sha:
            candidate_heads.add(candidate_sha)
        review_identity = history_facts.get("review_identity")
        if isinstance(review_identity, dict):
            for key in (
                "candidate_sha",
                "reviewed_candidate_sha",
                "reviewed_head_sha",
                "run_head_sha",
                "head_sha",
                "repair_candidate_sha",
            ):
                value = review_identity.get(key)
                if isinstance(value, str) and value:
                    candidate_heads.add(value)
    matched = candidates
    if reviewer_ids:
        matched = [
            item for item in matched if item.get("reviewer_thread_id") in reviewer_ids
        ]
    if candidate_heads:
        matched = [
            item for item in matched if item.get("candidate_sha") in candidate_heads
        ]
    # Both sides of the review identity are required. A canonical Attempt
    # without its Reviewer and Candidate boundary cannot safely borrow even a
    # unique Artifact from the same window.
    if not reviewer_ids or not candidate_heads:
        return None
    if len(matched) != 1:
        return None
    artifact = matched[0].get("artifact")
    return artifact if isinstance(artifact, dict) else None

def _development_summary_for_attempt(
    attempt: dict[str, Any], owners: list[dict[str, Any]]
) -> str | None:
    value = attempt.get("development_summary")
    if isinstance(value, str) and value.strip():
        return value
    if attempt.get("role") != "development":
        return None
    for owner in owners:
        value = owner.get("development_summary")
        if isinstance(value, str) and value.strip() and _owner_attempt_is_latest(owner, attempt):
            return value
    return None

def _publication_for_attempt(
    attempt: dict[str, Any], owners: list[dict[str, Any]]
) -> dict[str, Any] | None:
    value = attempt.get("publication")
    if isinstance(value, dict):
        return value
    if attempt.get("role") != "publication":
        return None
    for owner in owners:
        value = owner.get("publication")
        if isinstance(value, dict) and _owner_attempt_is_latest(owner, attempt):
            return value
    return None

def _owner_attempt_is_latest(owner: dict[str, Any], attempt: dict[str, Any]) -> bool:
    field = {
        "development": "modification_attempts",
        "reviewer": "validation_attempts",
        "publication": "publication_attempts",
    }.get(str(attempt.get("role")))
    if field is None:
        return False
    if field in owner:
        return owner.get(field) == attempt.get("ordinal")
    # A legacy/read-only projection with no owner history has no competing
    # attempt from which this record could have been borrowed.
    return not _owner_attempts(owner)

_SUPPORTING_RECORD_KEYS = (
    "required_checks_evidence",
    "deterministic_integration_record",
    "fallback_publication_receipt",
)

_SUPPORTING_IDENTITY_GROUPS = {
    "candidate": {
        "candidate_sha",
        "candidate_commit_sha",
        "reviewed_candidate_sha",
        "reviewed_head_sha",
        "run_head_sha",
        "repair_candidate_sha",
        "commit_sha",
    },
    "publication": {
        "publication_sha",
        "published_sha",
        "integrated_publication_sha",
    },
    "integration": {
        "integrated_sha",
        "merge_commit_sha",
    },
}

_SUPPORTING_SCOPE_KEYS = {
    "base_sha",
    "reviewed_base_sha",
    "reviewed_default_base_sha",
    "default_base_sha",
    "repair_base_run_head_sha",
    "effective_revision",
    "parent_revision",
    "ticket_graph_revision",
    "candidate_tree",
    "candidate_tree_sha",
    "reviewed_candidate_tree",
    "expected_merge_tree",
}

_SUPPORTING_NESTED_KEYS = {
    "acceptance_record",
    "candidate",
    "currentness_boundary",
    "deterministic_integration_record",
    "fallback_publication_receipt",
    "fallback_receipt",
    "pr",
    "review_budget",
    "publication",
    "required_checks_evidence",
    "review_identity",
    "semantic_attempt",
    "version",
}

_SUPPORTING_PUBLICATION_HEAD_KEYS = {"pr", "required_checks_evidence"}

def _supporting_scalar_values(value: object) -> set[str]:
    if isinstance(value, str) and value:
        return {value}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {str(value)}
    return set()

def _supporting_identity_bundle(
    value: object, *, default_head_group: str = "candidate"
) -> dict[str, set[str]]:
    bundle: dict[str, set[str]] = {
        "attempt": set(),
        "candidate": set(),
        "publication": set(),
        "integration": set(),
        "window": set(),
        "pr": set(),
        "thread": set(),
    }
    bundle.update({key: set() for key in _SUPPORTING_SCOPE_KEYS})
    if not isinstance(value, dict):
        return bundle
    for key, item in value.items():
        if key in {"attempt_id", "semantic_attempt_id"}:
            bundle["attempt"].update(_supporting_scalar_values(item))
        for group, keys in _SUPPORTING_IDENTITY_GROUPS.items():
            if key in keys:
                bundle[group].update(_supporting_scalar_values(item))
        if key == "head_sha":
            bundle[default_head_group].update(_supporting_scalar_values(item))
        elif key in _SUPPORTING_SCOPE_KEYS:
            bundle[key].update(_supporting_scalar_values(item))
        elif key in {"window", "budget_window"}:
            bundle["window"].update(_supporting_scalar_values(item))
        elif key == "pr_number":
            bundle["pr"].update(_supporting_scalar_values(item))
        elif key in {
            "reviewer_thread_id",
            "reported_thread_id",
            "requested_thread_id",
            "thread_id",
        }:
            bundle["thread"].update(_supporting_scalar_values(item))
        if key in _SUPPORTING_NESTED_KEYS:
            if key == "pr" and isinstance(item, dict):
                bundle["pr"].update(_supporting_scalar_values(item.get("number")))
            nested_head_group = (
                "publication"
                if key in _SUPPORTING_PUBLICATION_HEAD_KEYS
                else "candidate"
            )
            nested = _supporting_identity_bundle(
                item, default_head_group=nested_head_group
            )
            for nested_key, nested_values in nested.items():
                bundle[nested_key].update(nested_values)
    return bundle

def _merge_supporting_bundles(*values: object) -> dict[str, set[str]]:
    merged = _supporting_identity_bundle(None)
    for value in values:
        bundle = _supporting_identity_bundle(value)
        for key, items in bundle.items():
            merged[key].update(items)
    return merged

def _supporting_attempt_ids(value: object) -> set[str]:
    return _supporting_identity_bundle(value)["attempt"]

def _attempt_invocations(
    attempt: dict[str, Any], invocations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    attempt_id = attempt.get("attempt_id")
    if isinstance(attempt_id, str) and attempt_id:
        matched = [
            invocation
            for invocation in invocations
            if isinstance(invocation.get("semantic_attempt"), dict)
            and invocation["semantic_attempt"].get("attempt_id") == attempt_id
        ]
        if matched:
            return matched
    return [
        invocation
        for invocation in invocations
        if isinstance(invocation.get("semantic_attempt"), dict)
        and all(
            invocation["semantic_attempt"].get(key) == attempt.get(key)
            for key in ("work_subject", "generation", "role", "ordinal")
            if attempt.get(key) is not None
        )
    ]

def _supporting_record_matches_attempt(
    kind: str,
    value: dict[str, Any],
    attempt: dict[str, Any],
    invocations: list[dict[str, Any]],
) -> bool:
    record = _supporting_identity_bundle(
        value,
        default_head_group=(
            "publication" if kind == "required_checks_evidence" else "candidate"
        ),
    )
    record_attempt_ids = record["attempt"]
    attempt_id = attempt.get("attempt_id")
    if record_attempt_ids:
        if not isinstance(attempt_id, str) or attempt_id not in record_attempt_ids:
            return False

    attempt_bundles = [attempt]
    history_facts = attempt.get("history_facts")
    if isinstance(history_facts, dict):
        attempt_bundles.append(history_facts)
    attempt_bundles.extend(_attempt_invocations(attempt, invocations))
    expected = _merge_supporting_bundles(*attempt_bundles)
    for key in (
        *_SUPPORTING_SCOPE_KEYS,
        "window",
        "pr",
        "thread",
    ):
        expected_values = expected[key]
        record_values = record[key]
        if len(expected_values) > 1 or len(record_values) > 1:
            return False
        if expected_values and record_values and not expected_values & record_values:
            return False

    # Candidate aliases must all identify one reviewed object. Publication and
    # integration identities are intentionally separate: a publication commit
    # and its later merge commit are both valid parts of one record.
    expected_candidates = expected["candidate"]
    record_candidates = record["candidate"]
    if len(expected_candidates) > 1 or len(record_candidates) > 1:
        return False
    if record_candidates and not expected_candidates & record_candidates:
        return False
    expected_publications = expected["publication"]
    record_publications = record["publication"]
    if (
        expected_publications
        and record_publications
        and not expected_publications & record_publications
    ):
        return False
    expected_integrations = expected["integration"]
    record_integrations = record["integration"]
    if (
        expected_integrations
        and record_integrations
        and not expected_integrations & record_integrations
    ):
        return False

    # A supporting record must prove the reviewed object or publication. A
    # shared effective revision, base, or window is not sufficient: two
    # candidates may intentionally share all of those values.
    identity_matches = bool(
        record["candidate"] & expected["candidate"]
        or record["publication"] & expected["publication"]
        or record["integration"] & expected["integration"]
    )
    if not identity_matches:
        return False
    return True

def _supporting_records(
    owners: list[dict[str, Any]],
    attempt: dict[str, Any],
    invocations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for owner in owners:
        matched_values: dict[str, dict[str, Any]] = {}
        for key in _SUPPORTING_RECORD_KEYS:
            value = owner.get(key)
            if isinstance(value, dict) and _supporting_record_matches_attempt(
                key, value, attempt, invocations
            ):
                matched_values[key] = value
        # The live Job-level Observation has only the published ``head_sha``.
        # When it is an exact copy of a versioned integration/fallback record's
        # nested Observation, that parent record supplies the missing Candidate
        # binding. Otherwise leave the projection explicitly unrecorded.
        required_checks = owner.get("required_checks_evidence")
        if isinstance(required_checks, dict):
            for parent_key in (
                "deterministic_integration_record",
                "fallback_publication_receipt",
            ):
                parent = matched_values.get(parent_key)
                if (
                    parent is not None
                    and parent.get("required_checks_evidence") == required_checks
                ):
                    matched_values["required_checks_evidence"] = required_checks
                    break
        for key in _SUPPORTING_RECORD_KEYS:
            value = matched_values.get(key)
            if value is None:
                continue
            identity = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if identity in seen:
                continue
            seen.add(identity)
            records.append({"kind": key, "value": value})
    return records

def artifact_outcome(artifact: dict[str, Any]) -> str:
    checks = artifact.get("checks")
    if not isinstance(checks, dict):
        return "unknown"
    statuses = {
        check.get("status")
        for check in checks.values()
        if isinstance(check, dict)
    }
    if "fail" in statuses:
        return "fail"
    if "blocked" in statuses:
        return "blocked"
    if statuses and statuses == {"pass"}:
        return "pass"
    return "unknown"

def invocation_attempt_view(invocation: dict[str, Any]) -> dict[str, Any]:
    attempt = invocation.get("semantic_attempt")
    view = dict(attempt) if isinstance(attempt, dict) else {}
    for key in ("work_subject", "generation", "role"):
        if view.get(key) is None and invocation.get(key) is not None:
            view[key] = invocation[key]
    if view.get("role") is None and invocation.get("invocation_role") is not None:
        view["role"] = invocation["invocation_role"]
    return view
