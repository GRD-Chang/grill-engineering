from __future__ import annotations

from typing import Any

from agent_run.artifacts import AcceptanceArtifact
from agent_run.review_budget import TICKET_POLICY
from agent_run.required_checks_observation import require_required_checks_observation
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.ticket_acceptance_contract import (
    _require_accepted_integration_authorization,
)


def _require_fallback_integration_authorization(
    authorization: dict[str, Any],
    record: dict[str, Any],
    location: str,
    *,
    require_publication_facts: bool = True,
    require_previous_publication_authorization: bool = True,
) -> None:
    if authorization.get("kind") != "ticket_fallback_publication_receipt":
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt kind is invalid"
        )
    expected = {
        "base_sha": record["base_sha"],
        "candidate_sha": record["candidate_sha"],
        "candidate_tree": record["candidate_tree"],
        "effective_revision": record["effective_revision"],
    }
    if require_publication_facts:
        expected.update(
            {
                "publication_sha": record["publication_sha"],
                "pr_number": record["pr_number"],
            }
        )
    else:
        for key in ("publication_sha", "pr_number"):
            if key in authorization and key not in record:
                raise IncompatibleRunStateError(
                    f"incompatible_run_state: {location}.fallback_receipt {key} is not bound to the active publication"
                )
            if key in authorization and key in record:
                expected[key] = record[key]
    if any(authorization.get(key) != value for key, value in expected.items()):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt boundary is invalid"
        )
    try:
        AcceptanceArtifact.parse(authorization.get("last_acceptance_artifact"))
    except (TypeError, ValueError):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt artifact is invalid"
        ) from None
    receipt_evidence = authorization.get("required_checks_evidence")
    if require_publication_facts:
        if receipt_evidence != record["required_checks_evidence"]:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.fallback_receipt checks are invalid"
            )
    elif receipt_evidence is not None:
        if not isinstance(receipt_evidence, dict):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.fallback_receipt checks are invalid"
            )
        if not isinstance(record.get("required_checks_evidence"), dict):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.fallback_receipt checks are not bound to the active publication"
            )
        if isinstance(record.get("required_checks_evidence"), dict):
            if receipt_evidence != record["required_checks_evidence"]:
                raise IncompatibleRunStateError(
                    f"incompatible_run_state: {location}.fallback_receipt checks are invalid"
                )
        if (
            isinstance(record.get("pr_number"), int)
            and receipt_evidence.get("pr_number") != record["pr_number"]
        ) or (
            isinstance(record.get("publication_sha"), str)
            and receipt_evidence.get("head_sha") != record["publication_sha"]
        ):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.fallback_receipt checks are not bound to the published head"
            )
    required_checks_origin = authorization.get("required_checks_origin")
    if required_checks_origin is not None:
        require_required_checks_observation(
            required_checks_origin,
            location=f"{location}.fallback_receipt.required_checks_origin",
            expected_pr_number=record["pr_number"],
            allowed_results=frozenset({"fail"}),
        )
    expected_budget_keys = {
        "window",
        "development_attempts",
        "reviewer_invocations",
        "final_ci_fix_used",
        "review_artifacts",
        "checkpoint_reason",
    }
    review_budget = authorization.get("review_budget")
    if not isinstance(review_budget, dict) or set(review_budget) != expected_budget_keys:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt.review_budget is invalid"
        )
    if (
        review_budget.get("window") != record["window"]
        or review_budget.get("development_attempts") != TICKET_POLICY.development_limit
        or review_budget.get("reviewer_invocations") != TICKET_POLICY.review_limit
        or review_budget.get("final_ci_fix_used") != record["final_ci_fix_used"]
        or review_budget.get("checkpoint_reason") is not None
        or review_budget.get("review_artifacts") != authorization.get("review_artifacts")
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt budget consumption is invalid"
        )
    if (
        authorization.get("window") != record["window"]
        or authorization.get("reviewer_invocations") != TICKET_POLICY.review_limit
        or authorization.get("development_attempts")
        != TICKET_POLICY.development_limit
        or authorization.get("final_ci_fix_used") != record["final_ci_fix_used"]
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt budget is invalid"
        )
    review_artifacts = authorization.get("review_artifacts")
    if not isinstance(review_artifacts, list) or len(review_artifacts) != TICKET_POLICY.review_limit:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt review artifacts are invalid"
        )
    for index, item in enumerate(review_artifacts):
        item_location = f"{location}.fallback_receipt.review_artifacts[{index}]"
        if not isinstance(item, dict) or set(item) != {
            "reviewer_thread_id",
            "candidate_sha",
            "reviewed_base_sha",
            "review_identity",
            "artifact",
        }:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {item_location} is invalid"
            )
        if (
            not isinstance(item["reviewer_thread_id"], str)
            or not item["reviewer_thread_id"].strip()
            or not isinstance(item["candidate_sha"], str)
            or not item["candidate_sha"].strip()
            or item["reviewed_base_sha"] != record["base_sha"]
        ):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {item_location} identity is invalid"
            )
        identity = item["review_identity"]
        if not isinstance(identity, dict) or set(identity) != {
            "reviewed_base_sha",
            "reviewed_candidate_sha",
            "reviewed_candidate_tree",
        } or (
            identity.get("reviewed_base_sha") != record["base_sha"]
            or identity.get("reviewed_candidate_sha") != item["candidate_sha"]
            or not isinstance(identity.get("reviewed_candidate_tree"), str)
            or not identity["reviewed_candidate_tree"].strip()
        ):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {item_location}.review_identity is invalid"
            )
        try:
            AcceptanceArtifact.parse(item["artifact"])
        except (TypeError, ValueError):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {item_location}.artifact is invalid"
            ) from None
    latest = review_artifacts[-1]
    if (
        authorization.get("last_acceptance_artifact") != latest["artifact"]
        or authorization.get("last_review_candidate_sha") != latest["candidate_sha"]
        or latest["candidate_sha"] == record["candidate_sha"]
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt latest review binding is invalid"
        )
    validation_attempts = authorization.get("validation_attempts")
    if type(validation_attempts) is not int or validation_attempts < TICKET_POLICY.review_limit:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt validation attempts are invalid"
        )
    attempt_kind = authorization.get("last_attempt_kind")
    final_failure_head = authorization.get("final_ci_fix_failure_head")
    if attempt_kind not in {"ordinary", "final_ci_fix"}:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt attempt kind is invalid"
        )
    if record["final_ci_fix_used"] != (attempt_kind == "final_ci_fix") or (
        attempt_kind == "final_ci_fix"
        and (not isinstance(final_failure_head, str) or not final_failure_head.strip())
    ) or (attempt_kind == "ordinary" and final_failure_head is not None):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt final CI-fix facts are invalid"
        )
    repair_source = authorization.get("repair_source")
    if repair_source not in {"acceptance", "required_checks", "git_integrity"}:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt repair source is invalid"
        )
    if repair_source == "required_checks" and (
        not isinstance(record.get("publication_sha"), str)
        or not record["publication_sha"].strip()
        or type(record.get("pr_number")) is not int
        or record["pr_number"] < 1
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt required-check repair is missing its published head"
        )
    failure_source = authorization.get("failure_evidence_source")
    failure_evidence = authorization.get("failure_evidence")
    check_failure = authorization.get("required_check_failure_evidence")
    integrity_failure = authorization.get("git_integrity_evidence")
    failure_head = authorization.get("required_check_failure_head")
    if repair_source == "acceptance":
        valid_source = (
            failure_source == "acceptance"
            and failure_evidence == authorization["last_acceptance_artifact"]
            and check_failure is None
            and integrity_failure is None
            and failure_head is None
        )
    elif repair_source == "required_checks":
        valid_source = (
            failure_source == "required_checks"
            and isinstance(check_failure, dict)
            and failure_evidence == check_failure
            and isinstance(failure_head, str)
            and bool(failure_head.strip())
            and check_failure.get("head_sha") == failure_head
            and integrity_failure is None
        )
        if valid_source and isinstance(check_failure, dict):
            checks = check_failure.get("checks")
            valid_source = (
                check_failure.get("pr_number") == record["pr_number"]
                and check_failure.get("result") == "fail"
                and isinstance(checks, list)
                and bool(checks)
                and all(
                    isinstance(check, dict)
                    and check.get("bucket") == "fail"
                    and check.get("state") == "FAILURE"
                    and check.get("repairability") == "code_failure"
                    and all(
                        isinstance(check.get(key), str)
                        and bool(check[key].strip())
                        for key in ("name", "workflow", "link")
                    )
                    for check in checks
                )
            )
    else:
        valid_source = (
            failure_source == "git_integrity"
            and isinstance(integrity_failure, dict)
            and failure_evidence == integrity_failure
            and check_failure is None
            and failure_head is None
        )
    if not valid_source:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt failure evidence is invalid"
        )
    development_summary = authorization.get("development_summary")
    if not isinstance(development_summary, str) or not development_summary.strip():
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt development summary is invalid"
        )
    _require_delta(
        authorization.get("code_delta"),
        f"{location}.fallback_receipt.code_delta",
    )
    _require_delta(
        authorization.get("repair_delta"),
        f"{location}.fallback_receipt.repair_delta",
    )
    code_delta_base = authorization.get("code_delta_base_sha")
    repair_delta_base = authorization.get("repair_delta_base_sha")
    if (
        code_delta_base != record["base_sha"]
        or not isinstance(repair_delta_base, str)
        or not repair_delta_base.strip()
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt delta boundaries are invalid"
        )
    expected_repair_base = authorization["last_review_candidate_sha"]
    if repair_source == "required_checks":
        expected_repair_base = failure_head
    elif repair_source == "git_integrity" and isinstance(integrity_failure, dict):
        expected_repair_base = integrity_failure.get("expected_head") or expected_repair_base
    if repair_delta_base != expected_repair_base:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt repair delta boundary is invalid"
        )
    previous_authority = authorization.get("previous_publication_authority")
    if previous_authority not in {None, "acceptance", "fallback"}:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt previous authority is invalid"
        )
    previous_publication = authorization.get(
        "previous_publication_authorization"
    )
    if repair_source == "required_checks" and require_previous_publication_authorization:
        if previous_authority is None:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {location}.fallback_receipt required-check repair is missing its previous publication authority"
            )
        _require_previous_publication_authorization(
            previous_publication,
            previous_authority=previous_authority,
            failure_head=failure_head,
            base_sha=record["base_sha"],
            effective_revision=record["effective_revision"],
            location=location,
        )
    elif repair_source == "required_checks" and previous_authority not in {
        "acceptance",
        "fallback",
    }:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt required-check repair has an invalid previous authority"
        )
    elif previous_publication is not None:
        _require_previous_publication_authorization(
            previous_publication,
            previous_authority=previous_authority,
            failure_head=None,
            base_sha=record["base_sha"],
            effective_revision=record["effective_revision"],
            location=location,
        )
    integrity = authorization.get("git_integrity")
    if not isinstance(integrity, dict) or any(
        integrity.get(key) != value
        for key, value in {
            "status": "pass",
            "base_sha": record["base_sha"],
            "candidate_sha": record["candidate_sha"],
            "candidate_tree": record["candidate_tree"],
            "base_is_ancestor": "true",
        }.items()
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location}.fallback_receipt Git Integrity is invalid"
        )


def _require_previous_publication_authorization(
    value: object,
    *,
    previous_authority: str | None,
    failure_head: object,
    base_sha: str,
    effective_revision: str,
    location: str,
) -> None:
    nested_location = (
        f"{location}.fallback_receipt.previous_publication_authorization"
    )
    if not isinstance(value, dict) or set(value) != {
        "authority",
        "pr_number",
        "publication_sha",
        "base_sha",
        "effective_revision",
        "candidate_sha",
        "candidate_tree",
        "acceptance_record",
        "fallback_receipt",
    }:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {nested_location} is invalid"
        )
    if value.get("authority") != previous_authority or previous_authority not in {
        "acceptance",
        "fallback",
    }:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {nested_location}.authority is invalid"
        )
    if type(value.get("pr_number")) is not int or value["pr_number"] < 1:
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {nested_location}.pr_number is invalid"
        )
    for key, expected in (
        ("publication_sha", failure_head if failure_head is not None else value.get("publication_sha")),
        ("base_sha", base_sha),
        ("effective_revision", effective_revision),
        ("candidate_sha", value.get("candidate_sha")),
        ("candidate_tree", value.get("candidate_tree")),
    ):
        current = value.get(key)
        if not isinstance(current, str) or not current.strip() or current != expected:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {nested_location}.{key} is invalid"
            )
    if previous_authority == "acceptance":
        if not isinstance(value.get("acceptance_record"), dict) or value.get(
            "fallback_receipt"
        ) is not None:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {nested_location} acceptance evidence is invalid"
            )
        acceptance = value["acceptance_record"]
        nested_record = {
            "base_sha": base_sha,
            "candidate_sha": value["candidate_sha"],
            "candidate_tree": value["candidate_tree"],
            "effective_revision": effective_revision,
        }
        _require_accepted_integration_authorization(
            acceptance, nested_record, nested_location
        )
    else:
        if not isinstance(value.get("fallback_receipt"), dict) or value.get(
            "acceptance_record"
        ) is not None:
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {nested_location} fallback evidence is invalid"
            )
        receipt = value["fallback_receipt"]
        nested_record = {
            "base_sha": base_sha,
            "candidate_sha": value["candidate_sha"],
            "candidate_tree": value["candidate_tree"],
            "publication_sha": value["publication_sha"],
            "effective_revision": effective_revision,
            "pr_number": value["pr_number"],
            "required_checks_evidence": receipt.get("required_checks_evidence"),
            "window": receipt.get("window"),
            "final_ci_fix_used": receipt.get("final_ci_fix_used"),
        }
        if not isinstance(nested_record["required_checks_evidence"], dict):
            raise IncompatibleRunStateError(
                f"incompatible_run_state: {nested_location}.fallback_receipt checks are invalid"
            )
        _require_fallback_integration_authorization(
            receipt,
            nested_record,
            nested_location,
            require_previous_publication_authorization=False,
        )


def _require_delta(value: object, location: str) -> None:
    if not isinstance(value, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("status"), str)
        and bool(item["status"].strip())
        and isinstance(item.get("path"), str)
        and bool(item["path"].strip())
        for item in value
    ):
        raise IncompatibleRunStateError(
            f"incompatible_run_state: {location} is invalid"
        )
