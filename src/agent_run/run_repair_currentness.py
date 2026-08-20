from __future__ import annotations

"""Currentness and supervised readback for one Run Repair trigger."""

from dataclasses import dataclass
from typing import Any

from agent_run.agent_invocation import canonical_fingerprint
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.external_supervision import (
    ensure_supervision_window,
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError
from agent_run.run_currentness import ticket_completion_records
from agent_run.state import StateStore


class RunRepairObservationPending(Exception):
    """Stop this pass while Controller supervises a recoverable GitHub read."""


@dataclass(frozen=True)
class RunRepairCurrentness:
    git: GitRepository
    github: GitHubPublisher
    states: StateStore

    def create_trigger(
        self,
        state: dict[str, Any],
        repair_source: str,
        repair_request: dict[str, Any],
    ) -> dict[str, Any] | None:
        publication = state.get("run_publication")
        pr_number = (
            publication.get("pr_number") if isinstance(publication, dict) else None
        )
        if not isinstance(pr_number, int):
            return None
        live = self.live_pull_request(
            state,
            pr_number,
            waiting_for=f"Run PR #{pr_number} repair trigger",
        )
        trigger = {
            "pr_number": pr_number,
            "state": live.get("state"),
            "head_sha": live.get("head_sha"),
            "base_branch": live.get("base_branch"),
            "base_sha": live.get("base_sha"),
        }
        if repair_source == "required_checks":
            evidence = repair_request.get("ci_evidence")
            if not isinstance(evidence, dict):
                raise ValueError("required-check repair evidence must be an object")
            trigger["ci_evidence_fingerprint"] = canonical_fingerprint(evidence)
        return trigger

    def non_default_revision_changed(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        default_head_sha: str,
    ) -> bool:
        run_head = self.git.resolve(str(state["run_branch"]))
        expected_run_heads = {job.get("base_sha")}
        integrated = job.get("integrated_sha")
        if isinstance(integrated, str):
            expected_run_heads.add(integrated)
        return (
            not self.trigger_is_current(
                state, job, default_head_sha=default_head_sha
            )
            or run_head not in expected_run_heads
            or _mapping(state, "parent").get("revision")
            != job.get("parent_revision")
            or _mapping(state, "ticket_graph").get("revision")
            != job.get("ticket_graph_revision")
            or ticket_completion_records(state)
            != job.get("ticket_completion_records")
        )

    def trigger_is_current(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        *,
        default_head_sha: str,
    ) -> bool:
        trigger = job.get("repair_trigger")
        if trigger is None:
            return True
        if not isinstance(trigger, dict):
            return False
        pr_number = trigger.get("pr_number")
        if not isinstance(pr_number, int):
            return False
        live = self.live_pull_request(
            state,
            pr_number,
            waiting_for=f"Run PR #{pr_number} repair currentness",
        )
        if trigger.get("state") != live.get("state"):
            return False
        if trigger.get("base_branch") != live.get("base_branch"):
            return False
        trigger_base = trigger.get("base_sha")
        live_base = live.get("base_sha")
        if trigger_base != live_base and live_base != default_head_sha:
            return False
        head_transitioned_after_merge = False
        if trigger.get("head_sha") != live.get("head_sha"):
            integrated = job.get("integrated_sha")
            head_transitioned_after_merge = (
                isinstance(integrated, str)
                and live.get("head_sha") == integrated
                and self.git.resolve(str(state["run_branch"])) == integrated
            )
            if not head_transitioned_after_merge:
                return False
        evidence_fingerprint = trigger.get("ci_evidence_fingerprint")
        if head_transitioned_after_merge or not isinstance(evidence_fingerprint, str):
            return True
        try:
            current_evidence = self.github.required_check_evidence(pr_number)
        except (GitHubReadError, OSError, TimeoutError) as error:
            if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                error.code
            ):
                raise
            wait_for_github_convergence(
                state,
                code="github_check_evidence_fingerprint_pending",
                message="GitHub Required Check evidence fingerprint has not converged",
                waiting_for=f"Run PR #{pr_number} Required Check evidence fingerprint",
            )
            ensure_supervision_window(state)
            self.states.save_run(str(state["run_id"]), state)
            raise RunRepairObservationPending from error
        return evidence_fingerprint == canonical_fingerprint(current_evidence)

    def live_pull_request(
        self,
        state: dict[str, Any],
        pr_number: int,
        *,
        waiting_for: str,
    ) -> dict[str, Any]:
        """Read one Repair-related PR through the Controller supervision seam."""

        try:
            return self.github.live_pull_request(pr_number)
        except (GitHubReadError, OSError, TimeoutError) as error:
            if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                error.code
            ):
                raise
            wait_for_github_convergence(
                state,
                code="github_pull_request_observation_pending",
                message="GitHub pull request readback has not converged",
                waiting_for=waiting_for,
            )
            ensure_supervision_window(state)
            self.states.save_run(str(state["run_id"]), state)
            raise RunRepairObservationPending from error


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value
