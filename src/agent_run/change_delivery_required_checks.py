from __future__ import annotations

"""Required Check observation and Run-only repair classification."""

from pathlib import Path
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
        if not stage.adapter.classify_required_check_failures:
            evidence = stage.github.required_check_evidence(pr_number)
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
        if not is_explicitly_repairable_code_failure(evidence):
            supervise_unrepairable_check_failure(
                state,
                job,
                phase="waiting_checks",
                waiting_for=f"Change PR #{pr_number} Required Check repairability",
            )
            stage.save(state)
            return True, checks
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
    if checks not in {"none", "pass"}:
        raise ValueError(f"unknown Required Checks state: {checks}")
    return None, checks
