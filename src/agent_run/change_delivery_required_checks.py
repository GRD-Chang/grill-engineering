from __future__ import annotations

"""Exact-head Required Checks observation and repair classification."""

from pathlib import Path
from typing import Any

from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.change_delivery_fallback import (
    preserve_required_checks_publication_authorization,
)
from agent_run.external_supervision import (
    clear_supervision_window,
    ensure_supervision_window,
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.github import GitHubReadError
from agent_run.required_checks import (
    is_explicitly_repairable_code_failure,
    supervise_unrepairable_check_failure,
)
from agent_run.review_budget import can_start_development, ensure_budget
from agent_run.required_checks_observation import (
    clear_required_checks_observation,
    failure_evidence_matches_observation,
    read_required_checks_observation,
)


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
    if job.get("publication_authority") == "fallback":
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
        if stage._record_publication_operation_failure_in_memory(state, job, error):
            stage.save(state)
            return True
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
    checks = "unavailable"
    try:
        observation = read_required_checks_observation(
            stage.github,
            pr_number,
            expected_head_sha=str(job["publication_sha"]),
        )
    except (GitHubReadError, OSError, TimeoutError) as error:
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
        if stage._record_publication_operation_failure_in_memory(state, job, error):
            stage.save(state)
            return True, checks
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
                if stage._record_publication_operation_failure_in_memory(
                    state, job, error
                ):
                    stage.save(state)
                    return True, checks
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
            if stage.modification_budget_exhausted(job):
                job.update(
                    {
                        "phase": "escalating",
                        "escalation_code": "modification_budget_exhausted",
                    }
                )
            else:
                job.update(
                    {
                        "phase": "repairing",
                        "repair_source": "required_checks",
                        "ci_evidence": evidence,
                    }
                )
            should_yield = _resume_change_delivery_after_evidence(stage, state, job)
            stage.save(state)
            return should_yield, checks
        try:
            evidence = stage.github.required_check_evidence(
                pr_number, expected_head_sha=str(job["publication_sha"])
            )
        except (GitHubReadError, OSError, TimeoutError) as error:
            if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                error.code
            ):
                raise
            if stage._record_publication_operation_failure_in_memory(
                state, job, error
            ):
                stage.save(state)
                return True, checks
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
        ) or not is_explicitly_repairable_code_failure(evidence):
            job["ci_evidence"] = canonical_evidence
            supervise_unrepairable_check_failure(
                state,
                job,
                phase="waiting_checks",
                waiting_for=f"Change PR #{pr_number} Required Check repairability",
            )
            stage.save(state)
            return True, checks
        policy = stage.review_budget_policy()
        if stage.modification_budget_exhausted(job) and can_start_development(
            job, policy, attempt_kind="final_ci_fix"
        ):
            budget = ensure_budget(job, policy)
            job.update(
                {
                    "phase": "repairing",
                    "repair_source": "required_checks",
                    "ci_evidence": canonical_evidence,
                    "next_attempt_kind": "final_ci_fix",
                    "final_ci_fix_failure_head": str(job["publication_sha"]),
                    "final_ci_fix_used_before_attempt": budget[
                        "final_ci_fix_used"
                    ],
                }
            )
        elif stage.modification_budget_exhausted(job):
            job.update(
                {
                    "phase": "escalating",
                    "escalation_code": "modification_budget_exhausted",
                    "repair_source": "required_checks",
                    "ci_evidence": canonical_evidence,
                }
            )
        else:
            job.update(
                {
                    "phase": "repairing",
                    "repair_source": "required_checks",
                    "ci_evidence": canonical_evidence,
                    "next_attempt_kind": "ordinary",
                }
            )
        should_yield = _resume_change_delivery_after_evidence(stage, state, job)
        stage.save(state)
        return should_yield, checks
    if checks not in {"none", "pass"}:
        raise ValueError(f"unknown Required Checks state: {checks}")
    clear_supervision_window(state)
    return None, checks
