"""Integrate the completed Run Branch before overall Parent acceptance."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.git import GitError, GitRepository
from agent_run.integration_resync_budget import (
    ResyncBudgetExhausted,
    observe_integration_snapshot,
)
from agent_run.parent_integration import (
    ParentHistoryRewritten,
    finish_parent_snapshot_conflict,
    prepare_parent_snapshot,
    record_parent_integration_failure,
)
from agent_run.run_currentness import invalidate_run_acceptance
from agent_run.semantic_attempt import (
    close_semantic_attempt,
    pending_semantic_attempt,
)

from agent_run.run_integration_development import resolve_run_integration_work
from agent_run.resume_materials import require_change_checkout

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
    # Check before preparation or cleanup can replace an interrupted scene.
    require_change_checkout(state, run, f"run-acceptance:{state['run_id']}", owner.states.root, owner.git)
    keep_checkout = False
    try:
        if isinstance(run.get("integration_publication_intent"), dict):
            # Recover the exact write before a new snapshot replaces its source.
            ensure_run_repair_base(owner, state, run)
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
                confirmed_source_sha=_published_run_head(state, owner.git),
                handoff_git_failures=True,
            )
            while result.status == "conflict":
                keep_checkout = True
                conflict = result.conflict
                if not resolve_run_integration_work(owner, state, run, checkout, conflict):
                    return False
                work_evidence = deepcopy(conflict)
                try:
                    result = finish_parent_snapshot_conflict(
                        git=owner.git, subject=run, checkout=checkout,
                        record_key="run_integration", save=lambda: owner._save(state),
                    )
                except GitError as error:
                    semantic = pending_semantic_attempt(run, role="development")
                    if semantic is not None:
                        close_semantic_attempt(run, semantic, outcome="integration_git_check_failed",
                                               result={"development_summary": (work_evidence or {}).get("development_summary")})
                    assert isinstance(conflict, dict)
                    if not record_parent_integration_failure(run, conflict, error):
                        _pause(owner, state, run, "run_integration_failed", str(error))
                        return False
                    owner._save(state)
                    continue
                semantic = pending_semantic_attempt(run, role="development")
                if semantic is not None:
                    close_semantic_attempt(run, semantic, outcome="integration_conflict_resolved",
                                           result={"development_summary": (work_evidence or {}).get("development_summary")})
                    owner._save(state)
                run.setdefault("integration_work_history", []).append(work_evidence)
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
    published = _published_run_head(state, owner.git)
    if published is not None:
        if source is None:
            return False
        owner.git.fetch_remote_commit(str(state["run_branch"]), source)
        if not owner.git.is_ancestor(published, source):
            return False
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
    if isinstance(run, dict) and run.get("published_integration_head_sha") == candidate:
        return True
    completed = run.get("completed_repair_jobs") if isinstance(run, dict) else None
    return isinstance(completed, list) and any(
        isinstance(job, dict)
        and job.get("phase") == "completed"
        and job.get("acceptance_state") == "promoted"
        and job.get("integrated_sha") == candidate
        for job in completed
    )


def _published_run_head(state: dict[str, Any], git: GitRepository) -> str | None:
    publication = state.get("run_publication")
    run = state.get("run_acceptance")
    integrated = run.get("published_integration_head_sha") if isinstance(run, dict) else None
    head = publication.get("published_head_sha") if isinstance(publication, dict) else None
    if isinstance(integrated, str) and isinstance(head, str):
        if git.is_ancestor(integrated, head):
            return head
        if not git.is_ancestor(head, integrated):
            raise GitError("Published Run authorities have divergent histories")
    return integrated if isinstance(integrated, str) else head if isinstance(head, str) else None


def ensure_run_repair_base(owner: RunAcceptanceEngine, state: dict[str, Any], run: dict[str, Any]) -> None:
    """Publish the exact integration snapshot needed by Parent repair work.

    A durable intent and exact ref readback recover a lost write response. This
    updates only the Run Branch, never creates or approves the final Run PR.
    """
    record = run.get("run_integration")
    if not isinstance(record, dict):
        return
    head = str(record["head_sha"])
    if owner.git.resolve(str(state["run_branch"])) != head:
        return  # A later Repair already owns a newer Run head.
    if not run_integration_record_is_valid(owner, state):
        raise GitError("Run Repair base lacks a valid integration record")
    github = owner.github
    assert github is not None
    branch = str(state["run_branch"])
    source, target = github.live_branch_heads(branch, str(state["base"]["branch"]))
    intent = run.get("integration_publication_intent")
    authority = {"branch": branch, "head_sha": head,
                 "source_sha": record["source_sha"], "target_sha": record["target_sha"]}
    if isinstance(intent, dict) and intent != authority:
        raise GitError("Run integration publication intent no longer matches the Repair base")
    if source == head and (isinstance(intent, dict) or _own_run_ref_write(state, head) or source == record["source_sha"]):
        run["published_integration_head_sha"] = head
        run.pop("integration_publication_intent", None)
        owner._save(state)
        return
    if source != record["source_sha"] or target != record["target_sha"]:
        raise GitError("Run Repair integration base changed before publication")
    run["integration_publication_intent"] = authority
    owner._save(state)
    github.publish_branch(branch, head, expected_remote_sha=str(record["source_sha"]))
    if not github.final_run_ref_matches(branch=branch, expected_head_sha=head):
        raise GitError("Run integration publication readback has not matched its intent")
    run["published_integration_head_sha"] = head
    run.pop("integration_publication_intent", None)
    owner._save(state)


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
        published = _published_run_head(state, owner.git)
        if published is not None and not owner.git.is_ancestor(published, source):
            raise ParentHistoryRewritten("source", published, source)
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



def _pause(owner: RunAcceptanceEngine, state: dict[str, Any], run: dict[str, Any], code: str, message: str) -> None:
    run["execution_resume_phase"] = run["phase"]
    run["phase"] = "ready_for_human"
    run["blocked_reason"] = code
    state.update({
        "status": "ready_for_human",
        "terminal_kind": "waiting_human",
        "diagnostics": [{"code": code, "message": message}],
    })
    owner._save(state)
