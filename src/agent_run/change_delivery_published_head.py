from __future__ import annotations

"""Published-Head, Required Checks, and merge stage for Change Delivery."""

from pathlib import Path
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from agent_run.change_delivery_branches import ensure_linked_branch_display
from agent_run.branch_publication import RemoteBranchChanged
from agent_run.change_delivery_contracts import (
    ChangeDeliveryAdapter,
    ChangeDeliveryPublisher,
    ChangeJobContract,
)
from agent_run.change_delivery_required_checks import observe_required_checks
from agent_run.change_delivery_stage import ChangeDeliveryStage
from agent_run.change_delivery_state import require_mapping as _mapping
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.git import GitRepository
from agent_run.github import GitHubReadError, MergeOutcomeUnknownError
from agent_run.state_errors import IncompatibleRunStateError
from agent_run.external_supervision import (
    ensure_supervision_window,
    is_github_convergence_error,
    wait_for_github_convergence,
)
from agent_run.ticket_publication_contract import (
    require_active_ticket_publication_authorization,
)
from agent_run.required_checks_observation import (
    require_required_checks_observation,
    sync_fallback_receipt_observation,
    validate_legacy_required_checks_projection,
)
from agent_run.ticket_integration import (
    delivery_base_sha,
    delivery_candidate_sha,
    integration_record_is_valid,
    reconcile_ticket_integration,
)
from agent_run.parent_integration import (
    parent_integration_record_is_valid,
)
from agent_run.parent_only_integration import reconcile_parent_integration
from agent_run.run_repair_integration import reconcile_repair_integration


class PublishedHeadStage(ChangeDeliveryStage, Protocol):
    git: GitRepository
    github: GitHubPublisher
    contract: ChangeJobContract
    adapter: ChangeDeliveryAdapter
    publisher: ChangeDeliveryPublisher

    def save(self, state: dict[str, Any]) -> dict[str, Any]: ...

    def _reject_stale(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        checkout: Path,
        message: str,
    ) -> None: ...

    def _block(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        code: str,
        message: str,
    ) -> bool: ...

    def _record_agent_run_status(
        self,
        pr_number: int,
        job: dict[str, Any],
        checks: str,
        *,
        next_action: str | None = None,
    ) -> None: ...

    def _wait_for_merge_reconciliation(
        self,
        state: dict[str, Any],
        job: dict[str, Any],
        message: str | None = None,
    ) -> bool: ...

    def modification_budget_exhausted(self, job: dict[str, Any]) -> bool: ...


def _publication_operation(
    stage: PublishedHeadStage,
    state: dict[str, Any],
    job: dict[str, Any],
    operation: Callable[[], Any],
) -> tuple[bool, Any]:
    try:
        return False, operation()
    except (OSError, TimeoutError) as error:
        if stage._record_publication_operation_failure(state, job, error):
            return True, None
        raise


def _reconcile_delivery_snapshot(
    stage: PublishedHeadStage, state: dict[str, Any], job: dict[str, Any]
) -> bool:
    label = stage.contract.label
    if label.startswith("ticket-"):
        return reconcile_ticket_integration(stage, state, job)
    if label == "parent-only":
        return reconcile_parent_integration(stage, state, job)
    if label.startswith("run-repair-"):
        if isinstance(job.get("integrated_sha"), str):
            return True
        return reconcile_repair_integration(stage, state, job)
    return True

def publish_and_merge(
    stage: PublishedHeadStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> bool:
    ticket = stage.contract.label.startswith("ticket-")
    parent_only = stage.contract.label == "parent-only"
    run_repair = stage.contract.label.startswith("run-repair-")
    parent_record = job.get("parent_integration") if parent_only else None
    repair_record = job.get("run_repair_integration") if run_repair else None
    publication_base = delivery_base_sha(job) if ticket else (
        str(parent_record["target_sha"])
        if isinstance(parent_record, dict) else (
            str(repair_record["target_sha"])
            if isinstance(repair_record, dict) else str(job["base_sha"])
        )
    )
    publication_candidate = delivery_candidate_sha(job) if ticket else str(job["candidate_sha"])
    publication_source = (
        str(job["ticket_integration"]["source_sha"])
        if ticket and isinstance(job.get("ticket_integration"), dict)
        else (
            str(parent_record["source_sha"])
            if isinstance(parent_record, dict)
            else (
                str(repair_record["source_sha"])
                if isinstance(repair_record, dict)
                else str(job.get("published_sha", job["base_sha"]))
            )
        )
    )
    if (
        stage.contract.label.startswith("ticket-")
        and job.get("phase") in {
            "accepted",
            "publication_pending",
            "publishing",
            "waiting_checks",
            "waiting_merge",
            "merging",
        }
    ):
        candidate_sha = job.get("candidate_sha")
        if not isinstance(candidate_sha, str) or not candidate_sha.strip():
            raise ValueError("active Ticket publication is missing candidate_sha")
        active_candidate_tree = stage.git.resolve(f"{candidate_sha}^{{tree}}")
        require_active_ticket_publication_authorization(
            job,
            candidate_tree=active_candidate_tree,
            location=f"ticket_jobs[{job.get('ticket_number', 'active')}]",
        )
    if job.get("phase") not in {"merging", "merged"}:
        stage._reject_stale(
            state,
            job,
            checkout,
            "Published-Head Gate rejected stale requirements",
        )
    publication = _mapping(job, "publication")
    branch = stage.contract.branch
    publish_intent = {
        "action": "publish_ticket_ref",
        "branch": branch,
        "expected_remote_sha": publication_source,
        "head_sha": str(job["publication_sha"]),
    }
    observed_published = False
    existing_pr = job.get("pr_number")
    if isinstance(existing_pr, int):
        try:
            existing_live = stage.publisher.live_pull_request(
                state, job, existing_pr
            )
        except (GitHubReadError, OSError, TimeoutError) as error:
            if isinstance(error, GitHubReadError) and not is_github_convergence_error(
                error.code
            ):
                raise
            wait_for_github_convergence(
                state,
                code="github_pr_head_observation_pending",
                message=str(error),
                waiting_for=f"Change PR #{existing_pr} live identity observation",
            )
            ensure_supervision_window(state)
            stage.save(state)
            return True
        # Retain proof of an actual Controller observation. Queue admission
        # and restoring a waiting phase alone cannot prove Resume progressed.
        job["publication_observed_at"] = datetime.now(UTC).isoformat()
        if existing_live.get("state") == "MERGED":
            if (
                existing_live.get("head_branch") != stage.contract.branch
                or existing_live.get("head_repository") != state.get("repository")
                or existing_live.get("base_repository") != state.get("repository")
                or existing_live.get("head_sha") != job.get("publication_sha")
                or existing_live.get("base_branch") != stage.contract.base_branch
            ):
                return stage._block(
                    state, job, "merged_result_mismatch",
                    "Merged Change PR does not match the original publication identity",
                )
            integrated = existing_live.get("integrated_sha")
            if (
                job.get("phase") != "merging"
                and job.get("integrated_sha") != integrated
            ):
                return stage._block(
                    state,
                    job,
                    "unexpected_external_merge",
                    "Change Job PR merged without a persisted Publisher merge intent",
                )
            if not isinstance(integrated, str) or not integrated:
                raise ValueError("merged Change Job PR is missing integrated SHA")
            integrated_sha = integrated
            job["integrated_sha"] = integrated_sha
            live_head = existing_live.get("head_sha")
            if isinstance(live_head, str):
                job["integrated_publication_sha"] = live_head
            if stage.contract.label != "parent-only":
                exhausted, _ = _publication_operation(
                    stage,
                    state,
                    job,
                    lambda: stage.github.sync_run_branch(
                        run_branch=stage.contract.base_branch,
                        integrated_sha=integrated_sha,
                    ),
                )
                if exhausted:
                    return True
            exhausted, after_merge = _publication_operation(
                stage,
                state,
                job,
                lambda: stage.publisher.after_merge(state, job, existing_live),
            )
            if exhausted or not after_merge:
                return True
            job["phase"] = "completed"
            stage.save(state)
            return True
        if existing_live.get("state") not in {None, "OPEN"}:
            return stage._block(
                state,
                job,
                "ticket_pr_closed_unmerged",
                "Current Change Job PR was closed without merging",
            )
        if (
            ticket and stage._ticket_stop_requested(state, job)
            and job.get("phase") in {"merging", "waiting_merge"}
        ):
            return stage._wait_for_merge_reconciliation(state, job, "Ticket stop is waiting for the sent merge result")
        if job.get("phase") == "merging" and isinstance(job.get("integrated_sha"), str):
            state["status"] = "waiting_merge"
            stage.save(state)
            return True
        if not _reconcile_delivery_snapshot(stage, state, job):
            return False
        observed_published = (
            existing_live.get("head_sha") == job["publication_sha"]
            and (
                job.get("published_sha") == job["publication_sha"]
                or job.get("ticket_write_intent") == publish_intent
            )
        )
        if (
            existing_live.get("head_sha") not in {
                publication_source, job["publication_sha"]
            }
            or existing_live.get("base_sha") != publication_base
            or existing_live.get("base_branch") != stage.contract.base_branch
        ):
            return stage._block(
                state, job, "published_head_mismatch",
                "Published-Head Gate rejected live PR state",
            )
    stage._reject_stale(
        state, job, checkout, "Published-Head Gate rejected stale requirements"
    )
    if not isinstance(existing_pr, int):
        if not _reconcile_delivery_snapshot(stage, state, job):
            return False
    if (ticket or parent_only or run_repair) and not isinstance(existing_pr, int):
        # A PR creation response may be lost after the source commit was
        # published. Its durable publication SHA and exact live ref prove
        # whether a second push is needed before recovering the original PR.
        live_source, live_target = stage.github.live_branch_heads(
            branch, stage.contract.base_branch
        )
        if live_target != publication_base or live_source not in {
            publication_source, str(job["publication_sha"]),
        }:
            current = _reconcile_delivery_snapshot(stage, state, job)
            if not current:
                return False
        observed_published = (
            live_source == job["publication_sha"]
            and (
                job.get("published_sha") == job["publication_sha"]
                or job.get("ticket_write_intent") == publish_intent
            )
        )
    try:
        exhausted, _ = _publication_operation(
            stage,
            state,
            job,
            lambda: stage.github.verify_ticket_pr_before_publish(
                branch=branch,
                base_branch=stage.contract.base_branch,
                expected_head_sha=str(
                    job["publication_sha"] if observed_published
                    else publication_source
                ),
                expected_base_sha=publication_base,
            ),
        )
    except GitHubReadError:
        if not _reconcile_delivery_snapshot(stage, state, job):
            return False
        raise
    if exhausted:
        return True
    if not observed_published and job.get("ticket_write_intent") != publish_intent:
        # Remote reads may outlive Stop. Check before admitting a new write;
        # an existing intent still needs its original reconciliation path.
        if stage._pause_ticket_if_requested(state, job):
            return True
        job["ticket_write_intent"] = publish_intent
        stage.save(state)
    if not observed_published:
        try:
            exhausted, publish_result = _publication_operation(
                stage,
                state,
                job,
                lambda: stage.github.publish_branch(
                    branch,
                    str(job["publication_sha"]),
                    expected_remote_sha=publication_source,
                ),
            )
        except RemoteBranchChanged as error:
            job["branch_publication_observation"] = asdict(error.observation)
            if not _reconcile_delivery_snapshot(stage, state, job):
                return False
            return stage._block(
                state, job, "remote_source_changed",
                "Remote source branch changed after publication preparation; local Candidate preserved",
            )
        if exhausted:
            return True
        job["branch_publication_observation"] = {
            **asdict(publish_result.observation),
            "head_sha": publish_result.head_sha,
            "disposition": publish_result.disposition,
        }
    # This readback settles the push only. A later PR write may still have
    # an unknown response and must retain its intent until ensure_pr settles it.
    if job.get("ticket_write_intent") == publish_intent:
        job.pop("ticket_write_intent", None)
    job["published_sha"] = str(job["publication_sha"])
    stage.save(state)
    if stage._pause_ticket_if_requested(state, job):
        return True
    stage._reject_stale(
        state,
        job,
        checkout,
        "Published-Head Gate rejected requirements changed during publish",
    )
    pr_intent = {
        "action": "ensure_ticket_pr",
        "branch": branch,
        "base_branch": stage.contract.base_branch,
        "base_sha": publication_base,
        "head_sha": str(job["publication_sha"]),
    }
    if job.get("ticket_write_intent") != pr_intent:
        if stage._pause_ticket_if_requested(state, job):
            return True
        job["ticket_write_intent"] = pr_intent
        stage.save(state)
    exhausted, pr_number = _publication_operation(
        stage,
        state,
        job,
        lambda: stage.publisher.ensure_pr(state, job, publication),
    )
    if exhausted:
        return True
    if type(pr_number) is not int:
        raise ValueError("Publisher returned an invalid Change PR number")
    job.pop("ticket_write_intent", None)
    job["pr_number"] = pr_number
    sync_fallback_receipt_observation(job, pr_number)
    stage.save(state)
    if stage._pause_ticket_if_requested(state, job):
        return True
    stage._reject_stale(
        state,
        job,
        checkout,
        "Published-Head Gate rejected requirements changed while creating PR",
    )
    exhausted, created_live = _publication_operation(
        stage,
        state,
        job,
        lambda: stage.publisher.live_pull_request(state, job, pr_number),
    )
    if exhausted:
        return True
    if created_live.get("state") == "MERGED":
        return stage._block(
            state,
            job,
            "unexpected_external_merge",
            "Change Job PR merged without a persisted Publisher merge intent",
        )
    if created_live.get("state") not in {None, "OPEN"}:
        return stage._block(
            state,
            job,
            "ticket_pr_closed_unmerged",
            "Current Change Job PR was closed without merging",
        )
    link_display = getattr(stage.github, "link_issue_branch_display", None)
    linked_issue_number = stage.adapter.linked_issue_number(state, job)
    if linked_issue_number is not None and callable(link_display):
        ensure_linked_branch_display(
            github=stage.github,
            state=state,
            job=job,
            issue_number=linked_issue_number,
            branch=branch,
            head_sha=str(job["publication_sha"]),
            save=stage.save,
        )
    check_outcome, _checks = observe_required_checks(
        stage, state, job, checkout, pr_number
    )
    if check_outcome is not None:
        return check_outcome
    required_checks_evidence = job.get("required_checks_evidence")
    validate_legacy_required_checks_projection(
        job,
        location="active_ticket_job",
        observation=required_checks_evidence,
    )
    try:
        required_checks_evidence = require_required_checks_observation(
            required_checks_evidence,
            location="active_ticket_job.required_checks_evidence",
            expected_pr_number=pr_number,
            expected_head_sha=str(job["publication_sha"]),
            allowed_results=frozenset({"none", "pass"}),
        )
    except IncompatibleRunStateError:
        return stage._block(
            state,
            job,
            "published_head_mismatch",
            "Published-Head Gate rejected the final Required Checks snapshot",
        )
    checks = str(required_checks_evidence["result"])
    exhausted, live = _publication_operation(
        stage,
        state,
        job,
        lambda: stage.publisher.live_pull_request(state, job, pr_number),
    )
    if exhausted:
        return True
    fallback = job.get("publication_authority") == "fallback"
    ci_repair = job.get("publication_authority") == "ci_repair"
    acceptance = job.get("acceptance_record")
    receipt = job.get("fallback_publication_receipt")
    ci_receipt = job.get("ci_repair_publication_receipt")
    candidate_tree = stage.git.resolve(f"{job['candidate_sha']}^{{tree}}")
    if fallback:
        boundary_current = (
            isinstance(receipt, dict)
            and receipt.get("base_sha") == job.get("base_sha")
            and receipt.get("candidate_sha") == job.get("candidate_sha")
            and receipt.get("candidate_tree") == candidate_tree
            and receipt.get("effective_revision") == job.get("effective_revision")
            and acceptance is None
        )
        reviewed_base_sha = publication_base
        candidate_current = boundary_current
    elif ci_repair:
        candidate_current = (
            isinstance(ci_receipt, dict)
            and ci_receipt.get("base_sha") == job.get("base_sha")
            and ci_receipt.get("candidate_sha") == job.get("candidate_sha")
            and ci_receipt.get("candidate_tree") == candidate_tree
            and ci_receipt.get("effective_revision") == job.get("effective_revision")
            and ci_receipt.get("failure_head") == job.get("ci_recovery", {}).get("failure_head")
            and acceptance is None
        )
        reviewed_base_sha = publication_base
    else:
        if not isinstance(acceptance, dict):
            candidate_current = False
            reviewed_base_sha = publication_base
        else:
            reviewed_base_sha = publication_base if (ticket or parent_only or run_repair) else str(acceptance.get("reviewed_base_sha"))
            candidate_current = (
                acceptance.get("reviewed_candidate_sha") == job["candidate_sha"]
                and acceptance.get("reviewed_candidate_tree") == candidate_tree
                and stage.adapter.acceptance_is_current(state, job, acceptance)
            )
    if ticket:
        candidate_current = (
            candidate_current
            and integration_record_is_valid(stage.git, job)
            and stage.git.resolve(f"{publication_candidate}^{{tree}}")
            == stage.git.resolve(f"{job['publication_sha']}^{{tree}}")
        )
    if parent_only:
        candidate_current = (
            candidate_current
            and parent_integration_record_is_valid(stage.git, job)
            and stage.git.resolve(f"{publication_candidate}^{{tree}}")
            == stage.git.resolve(f"{job['publication_sha']}^{{tree}}")
        )
    if isinstance(repair_record, dict):
        candidate_current = (
            candidate_current
            and parent_integration_record_is_valid(stage.git, {
                **job, "parent_integration": repair_record,
            })
            and stage.git.resolve(f"{publication_candidate}^{{tree}}")
            == stage.git.resolve(f"{job['publication_sha']}^{{tree}}")
        )
    if (
        live.get("head_sha") != job["publication_sha"]
        or live.get("base_branch") != stage.contract.base_branch
        or live.get("base_sha") != reviewed_base_sha
        or live.get("mergeable") is not True
        or not candidate_current
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
            "Published-Head Gate rejected live PR state",
        )
    if not _reconcile_delivery_snapshot(stage, state, job):
        return False
    required_checks_evidence = deepcopy(required_checks_evidence)
    required_checks_evidence.update({"pr_number": pr_number})
    if fallback and isinstance(receipt, dict):
        receipt.update(
            {
                "pr_number": pr_number,
                "publication_sha": str(job["publication_sha"]),
                "required_checks_evidence": deepcopy(required_checks_evidence),
            }
        )
    if ci_repair and isinstance(ci_receipt, dict):
        ci_receipt.update(
            {
                "pr_number": pr_number,
                "publication_sha": str(job["publication_sha"]),
                "required_checks_evidence": deepcopy(required_checks_evidence),
            }
        )
    if stage._pause_ticket_if_requested(state, job):
        return True
    merge_intent = job.get("merge_intent")
    integration_record = {
        "source": "fallback" if fallback else "ci_repair" if ci_repair else "accepted",
        "policy_snapshot": deepcopy(job.get("policy_snapshot")),
        "pr_number": pr_number,
        "base_sha": publication_base,
        "candidate_sha": str(job["candidate_sha"]),
        "candidate_tree": candidate_tree,
        "publication_sha": str(job["publication_sha"]),
        "required_checks_evidence": required_checks_evidence,
        "pr": {
            "number": pr_number,
            "state": live.get("state"),
            "head_branch": branch,
            "head_sha": live.get("head_sha"),
            "head_repository": state.get("repository"),
            "base_branch": live.get("base_branch"),
            "base_sha": live.get("base_sha"),
            "base_repository": state.get("repository"),
            "mergeable": live.get("mergeable"),
        },
        "final_ci_fix_used": (
            job.get("review_budget", {}).get("final_ci_fix_used")
            if isinstance(job.get("review_budget"), dict)
            else False
        ),
        "window": job.get("review_budget", {}).get("window")
        if isinstance(job.get("review_budget"), dict)
        else None,
        "review_budget": deepcopy(job.get("review_budget")),
    }
    required_checks_origin = job.get("required_checks_origin")
    if isinstance(required_checks_origin, dict):
        integration_record["required_checks_origin"] = deepcopy(
            required_checks_origin
        )
    effective_revision = job.get("effective_revision")
    if isinstance(effective_revision, str) and effective_revision:
        integration_record["effective_revision"] = effective_revision
    if fallback:
        if not isinstance(receipt, dict):
            raise ValueError("fallback publication is missing its receipt")
        integration_record["fallback_receipt"] = dict(receipt)
    elif ci_repair:
        if not isinstance(ci_receipt, dict):
            raise ValueError("CI repair publication is missing its receipt")
        integration_record["ci_repair_publication_receipt"] = deepcopy(ci_receipt)
    else:
        if not isinstance(acceptance, dict):
            raise ValueError("accepted publication is missing its Acceptance Record")
        integration_record["acceptance_record"] = dict(acceptance)
    if ticket:
        integration_record["ticket_integration"] = deepcopy(job["ticket_integration"])
    job["deterministic_integration_record"] = integration_record
    expected_intent = {
        "head_sha": str(job["publication_sha"]),
        "base_branch": stage.contract.base_branch,
        "base_sha": reviewed_base_sha,
        "commit_message": str(publication["commit_message"]),
    }
    if "effective_revision" in job:
        expected_intent["effective_revision"] = str(job["effective_revision"])
    if merge_intent is None:
        merge_intent = {**expected_intent, "attempts": 0}
        job["merge_intent"] = merge_intent
    elif not isinstance(merge_intent, dict) or any(
        merge_intent.get(key) != value for key, value in expected_intent.items()
    ):
        return stage._block(
            state,
            job,
            "merge_intent_mismatch",
            "Persisted merge intent no longer matches the current PR boundary",
        )
    if stage.adapter.requires_explicit_approval(state, job):
        job["phase"] = "ready_for_approval"
        state["status"] = "parent_approval_pending"
        state["terminal_kind"] = "waiting_human"
        state["diagnostics"] = []
        stage.save(state)
        stage._record_agent_run_status(
            pr_number,
            job,
            checks,
            next_action="await explicit maintainer approval",
        )
        return True
    job["phase"] = "merging"
    stage.save(state)
    stage._record_agent_run_status(
        pr_number,
        job,
        checks,
        next_action=f"{stage.publisher.merge_description(job)} into the Run Branch",
    )
    if stage._pause_ticket_if_requested(state, job):
        return True
    attempts = merge_intent.get("attempts", 0)
    if type(attempts) is not int or attempts < 0:
        raise ValueError("merge intent attempts must be a non-negative integer")
    if attempts >= 3:
        return stage._wait_for_merge_reconciliation(state, job)
    merge_intent["attempts"] = attempts + 1
    stage.save(state)
    try:
        integrated = stage.publisher.merge(state, job, publication)
    except MergeOutcomeUnknownError as error:
        history = job.setdefault("merge_reconciliation_history", [])
        if not isinstance(history, list):
            raise ValueError("merge reconciliation history must be an array")
        history.append(
            {
                "attempt": merge_intent["attempts"],
                "result": "outcome_unknown",
                "message": str(error),
            }
        )
        del history[:-3]
        stage.save(state)
        return stage._wait_for_merge_reconciliation(state, job, str(error))
    job["integrated_sha"] = integrated
    job["integrated_publication_sha"] = str(job["publication_sha"])
    stage.save(state)
    if stage.contract.label != "parent-only":
        exhausted, _ = _publication_operation(
            stage,
            state,
            job,
            lambda: stage.github.sync_run_branch(
                run_branch=stage.contract.base_branch, integrated_sha=integrated
            ),
        )
        if exhausted:
            return True
    exhausted, live_after_merge = _publication_operation(
        stage,
        state,
        job,
        lambda: stage.publisher.live_pull_request(state, job, pr_number),
    )
    if exhausted:
        return True
    if live_after_merge.get("state") != "MERGED":
        state["status"] = "waiting_merge"
        stage.save(state)
        return True
    exhausted, after_merge = _publication_operation(
        stage,
        state,
        job,
        lambda: stage.publisher.after_merge(state, job, live_after_merge),
    )
    if exhausted or not after_merge:
        return True
    job["phase"] = "completed"
    stage.save(state)
    return True
