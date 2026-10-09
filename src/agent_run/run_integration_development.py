"""Run-scoped Development at the Parent integration boundary."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_run.agent_execution import execute_agent_work, select_development_thread
from agent_run.agents import DevelopmentResult, HumanBlockerResult
from agent_run.artifacts import clear_current_human_blocker, record_human_blocker
from agent_run.change_delivery_threads import (
    record_development_thread,
    require_development_thread,
)
from agent_run.human_responses import current_human_response_history
from agent_run.review_budget import can_start_development, ensure_budget, mark_development
from agent_run.run_currentness import ticket_completion_records
from agent_run.run_development_review import RunDevelopmentReview
from agent_run.semantic_attempt import allocate_semantic_attempt, pending_semantic_attempt

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine


def resolve_run_integration_work(
    owner: RunAcceptanceEngine,
    state: dict[str, Any],
    run: dict[str, Any],
    checkout: Path,
    conflict: dict[str, Any] | None,
) -> bool:
    from agent_run.run_parent_integration import _pause

    if not isinstance(conflict, dict):
        raise ValueError("Run integration conflict record is missing")
    attempt = pending_semantic_attempt(run, role="development")
    policy = owner._run_budget_policy(state, run)
    if attempt is None and not conflict.get("development_summary"):
        if not can_start_development(run, policy):
            RunDevelopmentReview(owner, state, run).checkpoint("modification_budget_exhausted")
            return False
        mark_development(run, policy, attempt_kind="ordinary")
    if not conflict.get("development_summary"):
        select_development_thread(state, run, new_attempt=attempt is None)
        conflict["attempts"] = 1
        ordinal = int(run.get("integration_conflict_agent_ordinal", 0)) + (attempt is None)
        run["integration_conflict_agent_ordinal"] = ordinal
        boundary = {
            "source_sha": conflict["source_sha"],
            "target_sha": conflict["target_sha"],
            "candidate_sha": conflict["candidate_sha"],
            "parent_revision": state["parent"]["revision"],
            "ticket_graph_revision": state["ticket_graph"]["revision"],
        }
        semantic = allocate_semantic_attempt(
            run,
            role="development",
            work_subject=f"run-integration:{state['run_id']}",
            generation=int(run["acceptance_generation"]),
            currentness_boundary=boundary,
            ordinal=ordinal,
            budget_window=int(ensure_budget(run, policy)["window"]),
        )
        request = {
            "acceptance_scope": "run",
            "parent_issue_url": owner._issue_url(state, int(state["parent"]["number"])),
            "run_id": state["run_id"],
            "repository": state["repository"],
            "parent": state["parent"],
            "ticket_graph": state["ticket_graph"],
            "ticket_completion_records": ticket_completion_records(state),
            "base_sha": conflict["target_sha"],
            "head_sha": owner.git.checkout_head(checkout),
            "checkout": str(checkout),
            "repair_source": "integration",
            "work_source": "integration",
            "integration_evidence": json.dumps(conflict, ensure_ascii=False, sort_keys=True),
            "thread_id": run.get("development_thread_id"),
            "human_response_history": current_human_response_history(run, generation=int(run["acceptance_generation"])),
            "prior_human_blockers": run.get("prior_human_blockers", []),
        }
        completions = ticket_completion_records(state)
        github = owner.github
        assert github is not None
        def scene_is_current() -> bool:
            if not owner._refresh_run_currentness(state):
                return False
            source_branch = str(state["run_branch"])
            target_branch = str(state["base"]["branch"])
            source, target = github.live_branch_heads(source_branch, target_branch)
            if source is None:
                return False
            owner.git.fetch_remote_commit(source_branch, source)
            owner.git.fetch_remote_commit(target_branch, target)
            return (
                # A pending exact scene is completed before observing a later
                # append-only snapshot. Rewrites never inherit its authority.
                owner.git.is_ancestor(str(conflict["source_sha"]), source)
                and owner.git.is_ancestor(str(conflict["target_sha"]), target)
                and state["parent"]["revision"] == boundary["parent_revision"]
                and state["ticket_graph"]["revision"] == boundary["ticket_graph_revision"]
                and ticket_completion_records(state) == completions
            )

        result = cast(DevelopmentResult | HumanBlockerResult, execute_agent_work(
            state, run, request, semantic, agents=owner.agents, save=owner._save,
            phase="run_integration_conflict", boundary=boundary, is_current=scene_is_current,
        ))
        require_development_thread(
            run, result.thread_id, requested_thread=request.get("thread_id")
        )
        record_development_thread(run, result.thread_id, result.replaced_thread_id)
        run.pop("development_new_thread", None)
        run.pop("development_failure_resume", None)
        if isinstance(result, HumanBlockerResult):
            record_human_blocker(
                state, run, resume_phase="pending", blocked_phase="ready_for_human",
                blockers=result.human_blockers, code="agent_requires_human",
            )
            conflict["human_blockers"] = list(result.human_blockers)
            owner._save(state)
            return False
        if not result.summary.strip():
            _pause(owner, state, run, "integration_conflict_unresolved", "Conflict Agent returned no result")
            return False
        if not scene_is_current():
            _pause(owner, state, run, "run_integration_failed", "Run integration work no longer matches the recorded requirements or source/target")
            return False
        conflict["development_summary"] = result.summary
        clear_current_human_blocker(run)
        owner._save(state)
    return True
