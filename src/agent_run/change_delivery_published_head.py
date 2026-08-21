from __future__ import annotations

"""Published-Head, Required Checks, and merge stage for Change Delivery."""

from pathlib import Path
from typing import Any, Protocol

from agent_run.change_delivery_branches import ensure_linked_branch_display
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
from agent_run.github import MergeOutcomeUnknownError


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


def publish_and_merge(
    stage: PublishedHeadStage,
    state: dict[str, Any],
    job: dict[str, Any],
    checkout: Path,
) -> bool:
    if job.get("phase") != "merging":
        stage._reject_stale(
            state,
            job,
            checkout,
            "Published-Head Gate rejected stale requirements",
        )
    publication = _mapping(job, "publication")
    branch = stage.contract.branch
    existing_pr = job.get("pr_number")
    if isinstance(existing_pr, int):
        existing_live = stage.publisher.live_pull_request(state, job, existing_pr)
        if existing_live.get("state") == "MERGED":
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
            job["integrated_sha"] = integrated
            live_head = existing_live.get("head_sha")
            if isinstance(live_head, str):
                job["integrated_publication_sha"] = live_head
            stage.github.sync_run_branch(
                run_branch=stage.contract.base_branch,
                integrated_sha=integrated,
            )
            if not stage.publisher.after_merge(state, job, existing_live):
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
        if job.get("phase") == "merging" and isinstance(job.get("integrated_sha"), str):
            state["status"] = "waiting_merge"
            stage.save(state)
            return True
    stage._reject_stale(
        state, job, checkout, "Published-Head Gate rejected stale requirements"
    )
    stage.github.verify_ticket_pr_before_publish(
        branch=branch,
        base_branch=stage.contract.base_branch,
        expected_head_sha=str(job.get("published_sha", job["base_sha"])),
        expected_base_sha=str(job["base_sha"]),
    )
    publish_intent = {
        "action": "publish_ticket_ref",
        "branch": branch,
        "expected_remote_sha": str(job.get("published_sha", job["base_sha"])),
        "head_sha": str(job["publication_sha"]),
    }
    if job.get("ticket_write_intent") != publish_intent:
        job["ticket_write_intent"] = publish_intent
        stage.save(state)
    stage.github.publish_branch(
        branch,
        str(job["publication_sha"]),
        expected_remote_sha=str(job.get("published_sha", job["base_sha"])),
    )
    job.pop("ticket_write_intent", None)
    job["published_sha"] = str(job["publication_sha"])
    stage.save(state)
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
        "base_sha": str(job["base_sha"]),
        "head_sha": str(job["publication_sha"]),
    }
    if job.get("ticket_write_intent") != pr_intent:
        job["ticket_write_intent"] = pr_intent
        stage.save(state)
    pr_number = stage.publisher.ensure_pr(state, job, publication)
    job.pop("ticket_write_intent", None)
    job["pr_number"] = pr_number
    stage.save(state)
    stage._reject_stale(
        state,
        job,
        checkout,
        "Published-Head Gate rejected requirements changed while creating PR",
    )
    created_live = stage.publisher.live_pull_request(state, job, pr_number)
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
    check_outcome, checks = observe_required_checks(
        stage, state, job, checkout, pr_number
    )
    if check_outcome is not None:
        return check_outcome
    live = stage.publisher.live_pull_request(state, job, pr_number)
    acceptance = _mapping(job, "acceptance_record")
    if (
        live.get("head_sha") != job["publication_sha"]
        or live.get("base_branch") != stage.contract.base_branch
        or live.get("base_sha") != acceptance.get("reviewed_base_sha")
        or live.get("mergeable") is not True
        or acceptance.get("reviewed_candidate_sha") != job["candidate_sha"]
        or acceptance.get("reviewed_candidate_tree")
        != stage.git.resolve(f"{job['publication_sha']}^{{tree}}")
        or not stage.adapter.acceptance_is_current(state, job, acceptance)
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
    merge_intent = job.get("merge_intent")
    expected_intent = {
        "head_sha": str(job["publication_sha"]),
        "base_branch": stage.contract.base_branch,
        "base_sha": str(acceptance["reviewed_base_sha"]),
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
    stage.github.sync_run_branch(
        run_branch=stage.contract.base_branch, integrated_sha=integrated
    )
    live_after_merge = stage.publisher.live_pull_request(state, job, pr_number)
    if live_after_merge.get("state") != "MERGED":
        state["status"] = "waiting_merge"
        stage.save(state)
        return True
    if not stage.publisher.after_merge(state, job, live_after_merge):
        return True
    job["phase"] = "completed"
    stage.save(state)
    return True
