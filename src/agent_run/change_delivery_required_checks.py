from __future__ import annotations

"""Required Check observation and Run-only repair classification."""

from pathlib import Path
from copy import deepcopy
from typing import Any

from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.external_supervision import (
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


def _snapshot_result(snapshot: dict[str, Any], initial_result: str) -> str:
    result = snapshot.get("result")
    if result == "observed":
        # The fixture adapter uses this marker for a detailed evidence
        # payload, not for an aggregate Required Checks result. Real GitHub
        # snapshots return one of the canonical aggregate states below.
        return initial_result
    if result not in {"none", "pass", "pending", "fail", "unknown"}:
        raise ValueError("Required Checks snapshot result is invalid")
    return str(result)


def _required_checks_snapshot(
    stage: ChangeDeliveryStage,
    job: dict[str, Any],
    pr_number: int,
    initial_result: str,
) -> dict[str, Any]:
    expected_head = str(job["publication_sha"])
    snapshot_reader = getattr(stage.github, "required_checks_snapshot", None)
    if callable(snapshot_reader):
        snapshot = snapshot_reader(pr_number, expected_head_sha=expected_head)
        if not isinstance(snapshot, dict):
            raise ValueError("Required Checks snapshot must be an object")
        snapshot = deepcopy(snapshot)
        if snapshot.get("head_sha") != expected_head:
            raise GitHubReadError(
                "change_pr_head_drift",
                "Required Checks snapshot does not match the expected PR head",
            )
        result = _snapshot_result(snapshot, initial_result)
    else:
        snapshot = {"checks": []}
        result = initial_result
    snapshot.update(
        {
            "pr_number": pr_number,
            "head_sha": expected_head,
            "result": result,
        }
    )
    return snapshot


def _verify_live_publication_head(
    stage: ChangeDeliveryStage,
    state: dict[str, Any],
    job: dict[str, Any],
    pr_number: int,
    checks: str,
) -> bool | None:
    """Confirm the PR head before reading failure evidence.

    Required Check APIs can briefly report jobs for a previous PR head.  The
    Publisher seam owns the supervised live PR read, so failure evidence is
    never accepted from that ambiguous interval.
    """

    expected_head = str(job["publication_sha"])
    try:
        live = stage.publisher.live_pull_request(state, job, pr_number)
    except (GitHubReadError, OSError, TimeoutError) as error:
        if isinstance(error, GitHubReadError) and error.code == "change_pr_head_drift":
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
                "Live PR head drifted before Required Check failure evidence was read",
            )
        if isinstance(error, GitHubReadError) and not is_github_convergence_error(
            error.code
        ):
            raise
        stage._record_agent_run_status(
            pr_number,
            job,
            "unavailable",
            next_action="retry live PR head observation before Required Check evidence",
        )
        wait_for_github_convergence(
            state,
            code="github_pr_head_observation_pending",
            message="GitHub live PR head observation has not converged",
            waiting_for=(f"Ticket PR #{pr_number} live head observation"),
        )
        ensure_supervision_window(state)
        stage.save(state)
        return True

    if not isinstance(live, dict) or live.get("head_sha") != expected_head:
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
            "Live PR head drifted before Required Check failure evidence was read",
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
        checks = stage.github.required_checks(pr_number)
    except (GitHubReadError, OSError, TimeoutError) as error:
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
        state.update(
            {
                "status": "waiting_external",
                "terminal_kind": "waiting_external",
                "diagnostics": [
                    {
                        "code": "github_checks_observation_pending",
                        "message": "GitHub Required Checks read has not converged",
                        "waiting_for": f"Ticket PR #{pr_number} Required Checks observation",
                    }
                ],
            }
        )
        stage.save(state)
        return True, checks
    stage._reject_stale(
        state,
        job,
        checkout,
        "Published-Head Gate rejected requirements changed while reading checks",
    )
    job["required_checks"] = checks
    job["required_checks_mode"] = (
        "not_configured" if checks == "none" else "configured"
    )
    if checks in {"none", "pass"}:
        try:
            job["required_checks_evidence"] = _required_checks_snapshot(
                stage, job, pr_number, checks
            )
        except (GitHubReadError, OSError, TimeoutError) as error:
            if isinstance(error, GitHubReadError) and error.code == "change_pr_head_drift":
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
                    "Required Checks snapshot did not match the published PR head",
                ), checks
            if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                error.code
            ):
                raise
            stage._record_agent_run_status(
                pr_number,
                job,
                "unavailable",
                next_action="retry Required Checks evidence observation",
            )
            wait_for_github_convergence(
                state,
                code="github_checks_evidence_observation_pending",
                message="GitHub Required Checks evidence has not converged",
                waiting_for=(f"Ticket PR #{pr_number} Required Checks evidence"),
            )
            ensure_supervision_window(state)
            stage.save(state)
            return True, checks
        checks = str(job["required_checks_evidence"]["result"])
        job["required_checks"] = checks
        job["required_checks_mode"] = (
            "not_configured" if checks == "none" else "configured"
        )
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
        stage.save(state)
        return True, checks
    if checks == "unknown":
        job["phase"] = "waiting_checks"
        wait_for_github_convergence(
            state,
            code="github_checks_observation_unknown",
            message="GitHub Required Checks returned an unknown state",
            waiting_for=f"Ticket PR #{pr_number} Required Checks observation",
        )
        ensure_supervision_window(state)
        stage.save(state)
        return True, checks
    if checks == "fail":
        live_head_outcome = _verify_live_publication_head(
            stage, state, job, pr_number, checks
        )
        if live_head_outcome is not None:
            return live_head_outcome, checks
        if not stage.adapter.classify_required_check_failures:
            evidence = stage.github.required_check_evidence(pr_number)
            live_head_outcome = _verify_live_publication_head(
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
            job["required_checks_evidence"] = evidence
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
            stage.save(state)
            return False, checks
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
                waiting_for=(f"Ticket PR #{pr_number} failed Required Check evidence"),
            )
            ensure_supervision_window(state)
            stage.save(state)
            return True, checks
        live_head_outcome = _verify_live_publication_head(
            stage, state, job, pr_number, checks
        )
        if live_head_outcome is not None:
            return live_head_outcome, checks
        job["required_checks_evidence"] = {
            **evidence,
            "pr_number": pr_number,
            "head_sha": str(job["publication_sha"]),
            "result": "fail",
        }
        if not is_explicitly_repairable_code_failure(evidence):
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
                    "ci_evidence": evidence,
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
                }
            )
        else:
            job.update(
                {
                    "phase": "repairing",
                    "repair_source": "required_checks",
                    "ci_evidence": evidence,
                    "next_attempt_kind": "ordinary",
                }
            )
        stage.save(state)
        return False, checks
    if checks not in {"none", "pass"}:
        raise ValueError(f"unknown Required Checks state: {checks}")
    return None, checks
