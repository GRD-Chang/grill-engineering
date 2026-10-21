"""Integrate the completed Run Branch before overall Parent acceptance."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.git import GitError, GitRepository
from agent_run.delivery_protocol import GitHubPublisher
from agent_run.delivery_step import DeliveryStepKind
from agent_run.integration_resync_budget import (
    ResyncBudgetExhausted,
    observe_integration_snapshot,
)
from agent_run.parent_integration import (
    ParentHistoryRewritten,
    _own_run_ref_write, _published_run_head,
    finish_run_snapshot, prepare_run_snapshot,
)
from agent_run.run_currentness import invalidate_run_acceptance
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
            result = prepare_run_snapshot(
                git=owner.git, github=github, states=owner.states,
                state=state, run=run, checkout=checkout,
            )
            while result.status == "conflict":
                keep_checkout = True
                step = owner.work.develop_integration(state, run, checkout, scope="run", allow_agent=True)
                if step.kind is not DeliveryStepKind.CONTINUE:
                    return False
                result = finish_run_snapshot(
                    git=owner.git, github=github, states=owner.states,
                    state=state, run=run, checkout=checkout,
                )
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
        owner.candidate_acceptance.update_default_head(str(record["target_sha"]))
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


def run_integration_record_is_valid(
    git: GitRepository, state: dict[str, Any],
) -> bool:
    run = state.get("run_acceptance")
    if not isinstance(run, dict):
        return False
    record = run.get("run_integration")
    if not isinstance(record, dict):
        return False
    try:
        return (
            record["head_sha"] == git.resolve(str(state["run_branch"]))
            and record["head_tree"] == git.resolve(f"{record['head_sha']}^{{tree}}")
            and record["candidate_tree"] == git.resolve(f"{record['candidate_sha']}^{{tree}}")
            and git.is_ancestor(str(record["candidate_sha"]), str(record["head_sha"]))
            and git.is_ancestor(str(record["source_sha"]), str(record["head_sha"]))
            and git.is_ancestor(str(record["target_sha"]), str(record["head_sha"]))
        )
    except (GitError, KeyError, TypeError, ValueError):
        return False


def run_integration_remote_is_current(
    git: GitRepository, github: GitHubPublisher | None, state: dict[str, Any],
) -> bool:
    if github is None:
        return True
    run = state.get("run_acceptance")
    record = run.get("run_integration") if isinstance(run, dict) else None
    if not isinstance(record, dict):
        return False
    source, target = github.live_branch_heads(
        str(state["run_branch"]), str(state["base"]["branch"])
    )
    published = _published_run_head(state, git)
    if published is not None:
        if source is None:
            return False
        git.fetch_remote_commit(str(state["run_branch"]), source)
        if not git.is_ancestor(published, source):
            return False
    return (
        target == record["target_sha"]
        and (
            source == record["source_sha"]
            or (source == record["head_sha"] and _own_run_ref_write(state, str(source)))
        )
    )


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
    if not run_integration_record_is_valid(owner.git, state):
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
