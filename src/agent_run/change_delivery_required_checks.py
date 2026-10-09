from __future__ import annotations

"""Exact-head Required Checks observation and repair classification."""

from copy import deepcopy
from pathlib import Path
from typing import Any

from agent_run.delivery_budget import delivery_budget_owner

from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.ci_recovery_budget import (
    ci_limit_for_job,
    observe_repairable_ci_failure,
)
from agent_run.change_delivery_fallback import (
    preserve_required_checks_publication_authorization,
    previous_publication_authorization,
)
from agent_run.external_supervision import (
    clear_supervision_window,
    ensure_supervision_window,
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.github import GitHubReadError
from agent_run.required_checks import (
    supervise_check_evidence_mismatch,
)
from agent_run.required_checks_observation import (
    clear_required_checks_observation,
    failure_evidence_matches_observation,
    read_required_checks_observation,
    required_checks_observation_is_current,
)
from agent_run.ticket_integration import delivery_base_sha


def _clear_required_checks_read_retry(job: dict[str, Any]) -> None:
    """Keep Required Checks convergence out of Publication Operation Retry."""

    job.pop("publication_operation_retry", None)
    job.pop("last_publication_error", None)


def _set_required_checks_repair_evidence(
    job: dict[str, Any], evidence: dict[str, Any]
) -> None:
    """Keep the failed PR/head as immutable provenance for Change Delivery."""

    job["ci_evidence"] = deepcopy(evidence)
    observation = job.get("required_checks_evidence")
    if not isinstance(observation, dict):
        raise ValueError("CI repair is missing its failed Required Checks observation")
    job["required_checks_origin"] = deepcopy(observation)


def _route_repairable_ci_failure(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    evidence: dict[str, Any],
) -> bool:
    limit = ci_limit_for_job(job)
    decision = observe_repairable_ci_failure(
        delivery_budget_owner(state, job),
        head_sha=str(job["publication_sha"]),
        limit=limit,
        is_ticket=stage.contract.label.startswith("ticket-"),
    )
    budget = delivery_budget_owner(state, job)["ci_recovery"]
    if (
        stage.contract.label.startswith("ticket-")
        and not budget["review_required"]
    ):
        authorization = previous_publication_authorization(job)
        if authorization is None:
            raise ValueError("Ticket CI repair is missing its previous publication authorization")
        budget["previous_publication_authorization"] = authorization
    _set_required_checks_repair_evidence(job, evidence)
    job["repair_source"] = "required_checks"
    job["final_ci_fix_failure_head"] = str(job["publication_sha"])
    if decision == "repair":
        job["phase"] = "repairing"
        job["next_attempt_kind"] = "ci_repair"
    else:
        job["phase"] = "escalating"
        job["escalation_code"] = "ci_development_budget_exhausted"
        job.pop("next_attempt_kind", None)
    should_yield = _resume_change_delivery_after_evidence(stage, state, job)
    stage.save(state)
    return should_yield


def _resume_change_delivery_after_evidence(
    stage: ChangeDeliveryStage, state: dict[str, Any], job: dict[str, Any]
) -> bool:
    if job.get("phase") != "waiting_checks":
        preserve_required_checks_publication_authorization(job)
        clear_required_checks_observation(job)
    should_yield = stage.adapter.resume_after_required_checks_failure(state, job)
    clear_supervision_window(state)
    return should_yield


def _expected_publication_base_sha(job: dict[str, Any]) -> str | None:
    if isinstance(job.get("ticket_integration"), dict):
        return delivery_base_sha(job)
    if job.get("publication_authority") in {"fallback", "ci_repair"}:
        value = job.get("base_sha")
    else:
        acceptance = job.get("acceptance_record")
        value = (
            acceptance.get("reviewed_base_sha")
            if isinstance(acceptance, dict)
            else None
        )
    return value if isinstance(value, str) and value else None


def _live_publication_identity_matches(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    live: dict[str, Any],
) -> bool:
    repository = state.get("repository")
    expected_base_sha = _expected_publication_base_sha(job)
    return (
        isinstance(repository, str)
        and bool(repository)
        and expected_base_sha is not None
        and live.get("state") in {None, "OPEN"}
        and live.get("head_branch") == stage.contract.branch
        and live.get("head_sha") == job.get("publication_sha")
        and live.get("head_repository") == repository
        and live.get("base_branch") == stage.contract.base_branch
        and live.get("base_sha") == expected_base_sha
        and live.get("base_repository") == repository
    )


def _verify_live_publication_identity(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    pr_number: int,
    checks: str,
) -> bool | None:
    """Confirm the complete live PR identity around Required Check reads.

    Required Check APIs can briefly report jobs for a previous PR head.  The
    Publisher seam owns the supervised live PR read, so failure evidence is
    never accepted from that ambiguous interval.
    """

    try:
        live = stage.publisher.live_pull_request(state, job, pr_number)
    except (GitHubReadError, OSError, TimeoutError) as error:
        if isinstance(error, GitHubReadError) and error.code in {
            "change_pr_head_drift",
            "change_pr_identity_mismatch",
        }:
            stage._record_agent_run_status(
                pr_number,
                job,
                checks,
                next_action="blocked: Published-Head Gate rejected live PR state",
            )
            return stage._block(
                state,
                job,
                "published_head_mismatch",
                "Live PR identity drifted while Required Checks were observed",
            )
        if isinstance(error, GitHubReadError) and not is_github_convergence_error(
            error.code
        ):
            raise
        stage._record_agent_run_status(
            pr_number,
            job,
            "unavailable",
            next_action="retry live PR identity observation",
        )
        wait_for_github_convergence(
            state,
            code="github_pr_head_observation_pending",
            message="GitHub live PR identity observation has not converged",
            waiting_for=(f"Change PR #{pr_number} live identity observation"),
        )
        ensure_supervision_window(state)
        stage.save(state)
        return True

    if not isinstance(live, dict) or not _live_publication_identity_matches(
        stage, state, job, live
    ):
        if stage.contract.label == "parent-only" and isinstance(live, dict):
            from agent_run.parent_only_integration import reconcile_parent_integration

            if not reconcile_parent_integration(stage, state, job, live=live):
                return job.get("phase") != "accepted"
        stage._record_agent_run_status(
            pr_number,
            job,
            checks,
            next_action="blocked: Published-Head Gate rejected live PR state",
        )
        return stage._block(
            state,
            job,
            "published_head_mismatch",
            "Live PR identity drifted while Required Checks were observed",
        )
    return None


def observe_required_checks(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
    pr_number: int,
) -> tuple[bool | None, str]:
    _clear_required_checks_read_retry(job)
    checks = "unavailable"
    try:
        observation = read_required_checks_observation(
            stage.github,
            pr_number,
            expected_head_sha=str(job["publication_sha"]),
        )
    except (GitHubReadError, OSError, TimeoutError) as error:
        if (
            isinstance(error, GitHubReadError)
            and error.code == "change_pr_head_drift"
            and stage.contract.label == "parent-only"
        ):
            # A check snapshot can race an allowed source append. Re-read the
            # complete identity and apply the same append-only integration gate
            # used for drift observed before/after the check read.
            outcome = _verify_live_publication_identity(
                stage, state, job, pr_number, checks
            )
            if outcome is not None:
                return outcome, checks
        if isinstance(error, GitHubReadError) and error.code in {
            "change_pr_head_drift",
            "change_pr_identity_mismatch",
        }:
            stage._record_agent_run_status(
                pr_number,
                job,
                "unavailable",
                next_action="blocked: Published-Head Gate rejected live PR state",
            )
            return stage._block(
                state,
                job,
                "published_head_mismatch",
                "Required Checks snapshot did not match the published PR identity",
            ), checks
        if isinstance(error, GitHubReadError) and not is_github_convergence_error(
            error.code
        ):
            raise
        stage._record_agent_run_status(
            pr_number,
            job,
            "unavailable",
            next_action="retry Required Checks observation",
        )
        wait_for_github_convergence(
            state,
            code="github_checks_observation_pending",
            message="GitHub Required Checks read has not converged",
            waiting_for=f"Change PR #{pr_number} Required Checks observation",
        )
        ensure_supervision_window(state)
        stage.save(state)
        return True, checks
    except ValueError:
        return stage._block(
            state,
            job,
            "published_head_mismatch",
            "Required Checks snapshot contradicted its check buckets",
        ), checks
    if not required_checks_observation_is_current(
        observation, pr_number=pr_number, head_sha=str(job["publication_sha"])
    ):
        return stage._block(
            state, job, "published_head_mismatch",
            "Required Checks observation no longer applies to the published PR",
        ), checks
    checks = str(observation["result"])
    live_identity_outcome = _verify_live_publication_identity(
        stage, state, job, pr_number, checks
    )
    if live_identity_outcome is not None:
        return live_identity_outcome, checks
    stage._reject_stale(
        state,
        job,
        checkout,
        "Published-Head Gate rejected requirements changed while reading checks",
    )
    job["required_checks_evidence"] = observation
    stage._record_agent_run_status(
        pr_number,
        job,
        checks,
        next_action=(
            "retry Required Checks observation" if checks == "unknown" else None
        ),
    )
    if checks == "pending":
        job["phase"] = "waiting_checks"
        state["status"] = "waiting_checks"
        state["terminal_kind"] = "waiting_checks"
        state["diagnostics"] = []
        ensure_supervision_window(state)
        stage.commit_required_checks_observation(state, job, pr_number)
        return True, checks
    if checks == "unknown":
        job["phase"] = "waiting_checks"
        wait_for_github_convergence(
            state,
            code="github_checks_observation_unknown",
            message="GitHub Required Checks returned an unknown state",
            waiting_for=f"Change PR #{pr_number} Required Checks observation",
        )
        ensure_supervision_window(state)
        stage.commit_required_checks_observation(state, job, pr_number)
        return True, checks
    if checks == "fail" and stage.contract.label.startswith("ticket-") and any(
        check.get("bucket") == "pending" for check in observation["checks"]
    ):
        job["phase"] = "waiting_checks"
        wait_for_github_convergence(
            state, code="github_checks_observation_pending",
            message="GitHub Required Checks still contain pending checks",
            waiting_for=f"Change PR #{pr_number} Required Checks completion",
        )
        ensure_supervision_window(state)
        stage.commit_required_checks_observation(state, job, pr_number)
        return True, checks
    if checks == "fail":
        job["phase"] = "waiting_checks"
        wait_for_github_convergence(
            state,
            code="github_check_evidence_observation_pending",
            message="GitHub Required Check failure evidence has not converged",
            waiting_for=f"Change PR #{pr_number} failed Required Check evidence",
        )
        ensure_supervision_window(state)
    stage.commit_required_checks_observation(state, job, pr_number)
    if checks == "fail":
        live_head_outcome = _verify_live_publication_identity(
            stage, state, job, pr_number, checks
        )
        if live_head_outcome is not None:
            return live_head_outcome, checks
        if not stage.adapter.classify_required_check_failures:
            try:
                evidence = stage.github.required_check_evidence(pr_number)
            except (GitHubReadError, OSError, TimeoutError) as error:
                if isinstance(
                    error, GitHubReadError
                ) and not is_github_convergence_error(error.code):
                    raise
                wait_for_github_convergence(
                    state,
                    code="github_check_evidence_observation_pending",
                    message="GitHub Required Check failure evidence has not converged",
                    waiting_for=(
                        f"Change PR #{pr_number} failed Required Check evidence"
                    ),
                )
                ensure_supervision_window(state)
                stage.save(state)
                return True, checks
            live_head_outcome = _verify_live_publication_identity(
                stage, state, job, pr_number, checks
            )
            if live_head_outcome is not None:
                return live_head_outcome, checks
            evidence = {
                **evidence,
                "pr_number": pr_number,
                "head_sha": str(job["publication_sha"]),
                "result": "fail",
            }
            return _route_repairable_ci_failure(
                stage, state, job, evidence
            ), checks
        try:
            evidence = stage.github.required_check_evidence(
                pr_number, expected_head_sha=str(job["publication_sha"])
            )
        except (GitHubReadError, OSError, TimeoutError) as error:
            if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                error.code
            ):
                raise
            stage._record_agent_run_status(
                pr_number,
                job,
                "unavailable",
                next_action="retry failed Required Check evidence observation",
            )
            wait_for_github_convergence(
                state,
                code="github_check_evidence_observation_pending",
                message="GitHub Required Check failure evidence has not converged",
                waiting_for=(f"Change PR #{pr_number} failed Required Check evidence"),
            )
            ensure_supervision_window(state)
            stage.save(state)
            return True, checks
        live_head_outcome = _verify_live_publication_identity(
            stage, state, job, pr_number, checks
        )
        if live_head_outcome is not None:
            return live_head_outcome, checks
        canonical_evidence = {
            **evidence,
            "pr_number": pr_number,
            "head_sha": str(job["publication_sha"]),
            "result": "fail",
        }
        if not failure_evidence_matches_observation(
            observation,
            evidence,
            pr_number=pr_number,
            head_sha=str(job["publication_sha"]),
        ):
            job["ci_evidence"] = canonical_evidence
            supervise_check_evidence_mismatch(
                state,
                job,
                phase="waiting_checks",
                waiting_for=f"Change PR #{pr_number} Required Check failure evidence",
            )
            stage.save(state)
            return True, checks
        return _route_repairable_ci_failure(
            stage, state, job, canonical_evidence
        ), checks
    if checks not in {"none", "pass"}:
        raise ValueError(f"unknown Required Checks state: {checks}")
    clear_supervision_window(state)
    return None, checks
