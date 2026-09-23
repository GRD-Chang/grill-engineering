"""Integrate the completed Run Branch before overall Parent acceptance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.agent_invocation import invocation_event_recorder
from agent_run.agents import HumanBlockerResult
from agent_run.change_delivery_threads import (
    record_development_thread,
    require_development_thread,
)
from agent_run.delivery_policy import run_repair_budget_policy_for_job
from agent_run.git import GitError
from agent_run.integration_resync_budget import (
    ResyncBudgetExhausted,
    observe_integration_snapshot,
)
from agent_run.parent_integration import (
    ParentHistoryRewritten,
    finish_parent_snapshot_conflict,
    prepare_parent_snapshot,
)
from agent_run.review_budget import reset_budget
from agent_run.run_currentness import invalidate_run_acceptance, ticket_completion_records
from agent_run.semantic_attempt import (
    allocate_semantic_attempt,
    close_semantic_attempt,
    pending_semantic_attempt,
)

if TYPE_CHECKING:
    from agent_run.run_acceptance import RunAcceptanceEngine


def prepare_run_parent_integration(
    owner: RunAcceptanceEngine, state: dict[str, Any], run: dict[str, Any]
) -> bool:
    """Use live refs and retain the exact integrated result for the Reviewer."""

    github = owner.github
    if github is None:
        # Direct, local-only acceptance tests do not have an external ref.
        return True
    if run.get("phase") == "repairing" and isinstance(run.get("repair_job"), dict):
        return observe_run_repair_snapshot(owner, state, run)
    run.setdefault("base_sha", str(state["base"]["sha"]))
    checkout = owner.states.root / "worktrees" / str(state["run_id"]) / "run-integration"
    keep_checkout = False
    try:
        owner.git.prepare_run_repair_checkout(
            branch=str(state["run_branch"]), checkout=checkout
        )
        for _ in range(5):
            candidate = owner.git.resolve(str(state["run_branch"]))
            result = prepare_parent_snapshot(
                git=owner.git,
                github=github,
                subject=run,
                checkout=checkout,
                source_branch=str(state["run_branch"]),
                target_branch=str(state["base"]["branch"]),
                candidate_sha=candidate,
                policy_snapshot=run.get("policy_snapshot") or state["policy_snapshot"],
                record_key="run_integration",
                save=lambda: owner._save(state),
                own_source_write=candidate if _own_run_ref_write(state, candidate) else False,
            )
            while result.status == "conflict":
                keep_checkout = True
                conflict = result.conflict
                if not _resolve_conflict(owner, state, run, checkout, conflict):
                    return False
                result = finish_parent_snapshot_conflict(
                    git=owner.git,
                    subject=run,
                    checkout=checkout,
                    record_key="run_integration",
                    save=lambda: owner._save(state),
                )
                semantic = pending_semantic_attempt(run, role="development")
                if semantic is not None:
                    close_semantic_attempt(
                        run, semantic, outcome="integration_conflict_resolved",
                        result={"development_summary": str(conflict.get("development_summary", "")) if isinstance(conflict, dict) else ""},
                    )
                    owner._save(state)
                if result.status == "conflict" and isinstance(result.conflict, dict):
                    result.conflict.pop("development_summary", None)
                    owner._save(state)
            record = result.record
            if not isinstance(record, dict):
                raise ValueError("Run integration did not produce a record")
            live_source, live_target = github.live_branch_heads(
                str(state["run_branch"]), str(state["base"]["branch"])
            )
            if live_target == record["target_sha"] and (
                live_source == record["source_sha"]
                or (
                    live_source == record["head_sha"]
                    and _own_run_ref_write(state, str(live_source))
                )
            ):
                break
        else:
            _pause(owner, state, run, "run_integration_moving", "Remote Parent snapshot kept changing during integration")
            keep_checkout = True
            return False
        if not isinstance(record, dict):
            raise ValueError("Run integration did not produce a record")
        owner.default_head_sha = str(record["target_sha"])
        owner.candidate_acceptance.update_default_head(owner.default_head_sha)
        window = run.get("integration_resync")
        if isinstance(window, dict):
            current_window = int(window["window"])
            prior_window = int(run.get("reviewed_integration_window", 1))
            if current_window > prior_window:
                reset_budget(run, owner._run_budget_policy(state, run))
                run["reviewed_integration_window"] = current_window
        accepted = run.get("acceptance_record")
        if isinstance(accepted, dict) and (
            accepted.get("reviewed_head_sha") != record["head_sha"]
            or accepted.get("reviewed_default_base_sha") != record["target_sha"]
        ):
            invalidate_run_acceptance(state)
        owner._save(state)
        keep_checkout = False
        return True
    except (ParentHistoryRewritten, ResyncBudgetExhausted, GitError) as error:
        code = (
            "integration_history_rewritten"
            if isinstance(error, ParentHistoryRewritten)
            else "integration_resync_exhausted"
            if isinstance(error, ResyncBudgetExhausted)
            else "run_integration_failed"
        )
        _pause(owner, state, run, code, str(error))
        keep_checkout = True
        return False
    finally:
        if not keep_checkout:
            owner.git.remove_worktree(checkout)
            owner._remove_empty_directories(checkout)


def run_integration_record_is_valid(owner: RunAcceptanceEngine, state: dict[str, Any]) -> bool:
    run = state.get("run_acceptance")
    if not isinstance(run, dict):
        return False
    record = run.get("run_integration")
    if not isinstance(record, dict):
        return False
    try:
        return (
            record["head_sha"] == owner.git.resolve(str(state["run_branch"]))
            and record["head_tree"] == owner.git.resolve(f"{record['head_sha']}^{{tree}}")
            and record["candidate_tree"] == owner.git.resolve(f"{record['candidate_sha']}^{{tree}}")
            and owner.git.is_ancestor(str(record["candidate_sha"]), str(record["head_sha"]))
            and owner.git.is_ancestor(str(record["source_sha"]), str(record["head_sha"]))
            and owner.git.is_ancestor(str(record["target_sha"]), str(record["head_sha"]))
        )
    except (GitError, KeyError, TypeError, ValueError):
        return False


def run_integration_remote_is_current(
    owner: RunAcceptanceEngine, state: dict[str, Any]
) -> bool:
    if owner.github is None:
        return True
    run = state.get("run_acceptance")
    record = run.get("run_integration") if isinstance(run, dict) else None
    if not isinstance(record, dict):
        return False
    source, target = owner.github.live_branch_heads(
        str(state["run_branch"]), str(state["base"]["branch"])
    )
    return (
        target == record["target_sha"]
        and (
            source == record["source_sha"]
            or (source == record["head_sha"] and _own_run_ref_write(state, str(source)))
        )
    )


def _own_run_ref_write(state: dict[str, Any], candidate: str) -> bool:
    publication = state.get("run_publication")
    if isinstance(publication, dict) and publication.get("published_head_sha") == candidate:
        return True
    run = state.get("run_acceptance")
    completed = run.get("completed_repair_jobs") if isinstance(run, dict) else None
    return isinstance(completed, list) and any(
        isinstance(job, dict)
        and job.get("phase") == "completed"
        and job.get("acceptance_state") == "promoted"
        and job.get("integrated_sha") == candidate
        for job in completed
    )


def observe_run_repair_snapshot(
    owner: RunAcceptanceEngine, state: dict[str, Any], run: dict[str, Any]
) -> bool:
    """Charge external drift before an active Repair rebinds and reviews it."""

    github = owner.github
    assert github is not None
    try:
        source, target = github.live_branch_heads(
            str(state["run_branch"]), str(state["base"]["branch"])
        )
        if source is None:
            raise GitError("remote Run source ref is missing")
        owner.git.fetch_remote_commit(str(state["run_branch"]), source)
        owner.git.fetch_remote_commit(str(state["base"]["branch"]), target)
        record = run.get("run_integration")
        previous_source = (
            str(record["source_sha"]) if isinstance(record, dict)
            else str(state["base"]["sha"])
        )
        previous_target = (
            str(record["target_sha"]) if isinstance(record, dict)
            else str(state["base"]["sha"])
        )
        if not owner.git.is_ancestor(previous_source, source):
            raise ParentHistoryRewritten("source", previous_source, source)
        if not owner.git.is_ancestor(previous_target, target):
            raise ParentHistoryRewritten("target", previous_target, target)
        repair = run["repair_job"]
        own_source = _own_run_ref_write(state, source) or (
            repair.get("integrated_sha") == source
        )
        observe_integration_snapshot(
            run,
            source_sha=source,
            target_sha=target,
            policy_snapshot=run.get("policy_snapshot") or state["policy_snapshot"],
            own_source_write=own_source,
        )
        resync = run["integration_resync"]
        current_window = int(resync["window"])
        if current_window > int(run.get("reviewed_integration_window", 1)):
            owner._sync_repair_cycle_counters(run, repair)
            # The budget is per integration window, but these ordinals track
            # the still-active Repair Cycle and any in-flight Candidate work.
            repair_ordinals = {
                key: repair[key]
                for key in ("modification_attempts", "validation_attempts")
                if key in repair
            }
            reset_budget(run, owner._run_budget_policy(state, run))
            reset_budget(
                repair,
                run_repair_budget_policy_for_job(
                    repair, state_snapshot=run.get("policy_snapshot")
                ),
            )
            repair.update(repair_ordinals)
            run["reviewed_integration_window"] = current_window
            owner._sync_repair_cycle_counters(run, repair)
        owner.default_head_sha = target
        owner.candidate_acceptance.update_default_head(target)
        owner._save(state)
        return True
    except (ParentHistoryRewritten, ResyncBudgetExhausted, GitError) as error:
        code = (
            "integration_history_rewritten"
            if isinstance(error, ParentHistoryRewritten)
            else "integration_resync_exhausted"
            if isinstance(error, ResyncBudgetExhausted)
            else "run_integration_failed"
        )
        _pause(owner, state, run, code, str(error))
        return False


def _resolve_conflict(
    owner: RunAcceptanceEngine,
    state: dict[str, Any],
    run: dict[str, Any],
    checkout: Path,
    conflict: dict[str, Any] | None,
) -> bool:
    if not isinstance(conflict, dict):
        raise ValueError("Run integration conflict record is missing")
    attempt = pending_semantic_attempt(run, role="development")
    if conflict.get("attempts", 0) >= 1 and attempt is None and not conflict.get("development_summary"):
        _pause(owner, state, run, "integration_conflict_unresolved", "Run integration conflict already attempted")
        return False
    if not conflict.get("development_summary"):
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
            budget_window=int(run["review_budget"]["window"]),
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
            "repair_source": "merge_conflict",
            "merge_conflict_evidence": json.dumps(conflict, ensure_ascii=False, sort_keys=True),
            "thread_id": run.get("development_thread_id"),
            "_prompt_resources": state["prompt_resources"],
        }
        request["_invocation_event"] = invocation_event_recorder(
            state,
            role="development",
            phase="run_integration_conflict",
            work_subject=f"run-integration:{state['run_id']}",
            generation=int(run["acceptance_generation"]),
            invocation_input=request,
            currentness_boundary=boundary,
            semantic_attempt=semantic,
            save=owner._save,
        )
        github = owner.github
        assert github is not None
        request["_currentness_check"] = lambda: (
            github.live_branch_heads(
                str(state["run_branch"]), str(state["base"]["branch"])
            ) == (conflict["source_sha"], conflict["target_sha"])
            and state["parent"]["revision"] == boundary["parent_revision"]
            and state["ticket_graph"]["revision"] == boundary["ticket_graph_revision"]
        )
        if run.get("development_new_thread") is True:
            request["_invocation_mode"] = "new-thread"
        elif run.get("development_failure_resume") is True:
            request["_invocation_mode"] = "resume"
        owner._save(state)
        result = owner.agents.develop(request)
        require_development_thread(
            run, result.thread_id, requested_thread=request.get("thread_id")
        )
        record_development_thread(run, result.thread_id, result.replaced_thread_id)
        run.pop("development_new_thread", None)
        run.pop("development_failure_resume", None)
        if isinstance(result, HumanBlockerResult):
            _pause(owner, state, run, "integration_conflict_requires_human", "; ".join(result.human_blockers))
            return False
        if not result.summary.strip():
            _pause(owner, state, run, "integration_conflict_unresolved", "Conflict Agent returned no result")
            return False
        conflict["development_summary"] = result.summary
        owner._save(state)
    return True


def _pause(owner: RunAcceptanceEngine, state: dict[str, Any], run: dict[str, Any], code: str, message: str) -> None:
    run["phase"] = "ready_for_human"
    run["blocked_reason"] = code
    state.update({
        "status": "ready_for_human",
        "terminal_kind": "waiting_human",
        "diagnostics": [{"code": code, "message": message}],
    })
    owner._save(state)
